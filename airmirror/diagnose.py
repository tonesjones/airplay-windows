"""Collect a redacted diagnostics zip: py -3 -m airmirror.diagnose

Everything is passed through redact() before it is written. The zip is meant to be reviewed by the
user and then shared on purpose. This module never reads the sender's credential store or the
Windows Credential Manager.
"""

from __future__ import annotations

import argparse
import ctypes
import ipaddress
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from airmirror import REPO_ROOT, SENDER_EXE

DIAG_DIR = REPO_ROOT / "diagnostics"
PLACEHOLDER = "<redacted>"

# ---------------------------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------------------------

_MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])"
)
_MAC_DOTTED_RE = re.compile(
    r"(?<![0-9A-Fa-f.])(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}(?![0-9A-Fa-f.])"
)
_TXT_RE = re.compile(r"(?<![A-Za-z0-9_])(deviceid|pk|pi|psi|gid)=([^\s,;\"'\]\[)]+)", re.IGNORECASE)
_FIELD_RE = re.compile(
    r"""(?<![\w.-])(?P<q>["']?)(?P<name>[A-Za-z][A-Za-z0-9_.-]*)(?P=q)"""
    r"""(?P<sep>\s*[:=]\s*)"""
    r"""(?P<val>"(?:[^"\\]|\\.)*"|'[^']*'|[^\s,}\]]+)"""
)
_PROMPT_RE = re.compile(
    r"(?im)^(?P<prompt>[^\n]*\b(?:enter|type)\b[^\n:]*"
    r"\b(?:pin|password|passcode|credential)\b[^\n:]*:[ \t]*)"
    r"(?P<reply>\S[^\n]*)$"
)
_CODE_FLAG_RE = re.compile(r"(?i)(-code(?:\s+|=)|OPEN_AIRPLAY_CODE\s*=\s*)(\"[^\"]*\"|\S+)")
_HEX_RUN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{32,}(?![0-9A-Za-z])")
_B64_RE = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/_=-])")
_SSID_LINE_RE = re.compile(r"(?im)^(?P<k>[ \t]*B?SSID(?:[ \t]*\d+)?[ \t]*:[ \t]*)(?P<v>.*?)[ \t]*$")
_IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Fa-f:])"
)
_USER_PATH_RE = re.compile(
    r"(?i)(?P<head>[A-Za-z]:(?P<sep>[\\/]{1,4})users(?P=sep))(?P<name>[^\\/\s\"'<>|:*?]+)"
)

_SENSITIVE_TOKENS = {"pin", "password", "passwd", "pwd", "passcode", "credential", "credentials"}
_SENSITIVE_TOKENS |= {"secret", "secrets", "key", "keys", "token"}
_SENSITIVE_SUFFIXES = ("password", "passwd", "secret", "credential", "credentials", "key", "token")


def _name_is_sensitive(name: str) -> bool:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    tokens = [t.lower() for t in re.split(r"[^A-Za-z0-9]+|\s", spaced) if t]
    return any(t in _SENSITIVE_TOKENS or t.endswith(_SENSITIVE_SUFFIXES) for t in tokens)


def _field_sub(match: re.Match[str]) -> str:
    if not _name_is_sensitive(match["name"]):
        return match[0]
    q = match["q"]
    val = match["val"]
    new = f"{val[0]}{PLACEHOLDER}{val[0]}" if val[0] in "\"'" else PLACEHOLDER
    return f"{q}{match['name']}{q}{match['sep']}{new}"


def _looks_like_key(match: re.Match[str]) -> str:
    text = match[0]
    has_digit = any(c.isdigit() for c in text)
    has_alpha = any(c.isalpha() for c in text)
    return PLACEHOLDER if has_digit and has_alpha else text


def _ipv6_sub(match: re.Match[str]) -> str:
    try:
        addr = ipaddress.IPv6Address(match[0])
    except ValueError:
        return match[0]
    return match[0] if addr.is_link_local or addr.is_loopback else "<ipv6>"


def redact(text: str, ssids: tuple[str, ...] | list[str] = ()) -> str:
    """Remove identifiers and secrets. LAN IPv4 and link-local IPv6 addresses are kept."""
    # Paths first, so a user name is not mistaken for something else later.
    text = _USER_PATH_RE.sub(lambda m: f"{m['head']}<user>", text)
    text = _SSID_LINE_RE.sub(lambda m: f"{m['k']}<ssid>", text)
    for ssid in sorted({s for s in ssids if s and s.strip()}, key=len, reverse=True):
        text = re.sub(re.escape(ssid.strip()), "<ssid>", text, flags=re.IGNORECASE)
    text = _PROMPT_RE.sub(lambda m: f"{m['prompt']}{PLACEHOLDER}", text)
    text = _CODE_FLAG_RE.sub(lambda m: f"{m[1]}{PLACEHOLDER}", text)
    text = _TXT_RE.sub(lambda m: f"{m[1]}={PLACEHOLDER}", text)
    text = _FIELD_RE.sub(_field_sub, text)
    text = _MAC_RE.sub("<mac>", text)
    text = _MAC_DOTTED_RE.sub("<mac>", text)
    text = _IPV6_RE.sub(_ipv6_sub, text)
    text = _HEX_RUN_RE.sub(PLACEHOLDER, text)
    return _B64_RE.sub(_looks_like_key, text)


# ---------------------------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------------------------


def _run(args: list[str], timeout: float = 30) -> tuple[int | None, str, bool]:
    """Run a command; return (exit code, combined output, timed_out). Never raises."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"") + (exc.stderr or b"")
        return None, partial.decode("utf-8", "replace"), True
    except OSError as exc:
        return None, f"could not run {args[0]}: {exc}", False
    out = (proc.stdout or b"") + (proc.stderr or b"")
    return proc.returncode, out.decode("utf-8", "replace"), False


def _powershell(script: str, timeout: float = 60) -> tuple[int | None, str, bool]:
    exe = shutil.which("powershell") or "powershell.exe"
    return _run([exe, "-NoProfile", "-NonInteractive", "-Command", script], timeout)


def _json_list(text: str) -> list[dict]:
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        return [data]
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def idle_minutes() -> float | None:
    """Minutes since the last keyboard or mouse input, via GetLastInputInfo."""
    try:

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        millis = (kernel32.GetTickCount() - info.dwTime) % 2**32
        return millis / 60000
    except (AttributeError, OSError):
        return None


def parse_monitor_timeout_minutes(powercfg_text: str) -> float | None:
    """Monitor-off timeout on AC power, in minutes. 0 means never; None means unknown."""
    match = re.search(r"Current AC Power Setting Index:\s*(0x[0-9a-fA-F]+)", powercfg_text)
    if not match:
        match = re.search(r"Power Setting Index:\s*(0x[0-9a-fA-F]+)", powercfg_text)
    return int(match[1], 16) / 60 if match else None


def parse_receivers(text: str) -> list[dict]:
    """Parse `-list-receivers` lines: [1] name="X" model="Y" mirroring=yes endpoint=1.2.3.4:7000"""
    found = []
    pattern = re.compile(
        r'^\s*\[\d+\]\s+name="(?P<name>(?:[^"\\]|\\.)*)"\s+model="(?P<model>(?:[^"\\]|\\.)*)"\s+'
        r"mirroring=(?P<mirroring>\w+)\s+endpoint=(?P<endpoint>\S+)",
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        host, _, port = m["endpoint"].rpartition(":")
        found.append(
            {
                "name": m["name"],
                "model": m["model"],
                "mirroring": m["mirroring"].lower() == "yes",
                "host": host.strip("[]"),
                "port": int(port) if port.isdigit() else None,
            }
        )
    return found


def _ssids_from(netsh_text: str, profiles: list[dict]) -> list[str]:
    names = [
        m["v"] for m in _SSID_LINE_RE.finditer(netsh_text) if m["k"].strip().upper() != "BSSID"
    ]
    names += [str(p.get("Name", "")) for p in profiles]
    return sorted({n.strip() for n in names if n and n.strip()})


# ---------------------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------------------


def build_checks(facts: dict) -> list[tuple[str, str]]:
    """Turn collected facts into (PASS|WARN|FAIL, message) lines. Pure, so it is easy to test."""
    checks: list[tuple[str, str]] = []

    def add(level: str, message: str) -> None:
        checks.append((level, message))

    exe_ok = bool(facts.get("exe_present"))
    if exe_ok:
        add("PASS", "open-airplay.exe is present")
    else:
        add("FAIL", "bin\\open-airplay.exe is missing: run setup.ps1")

    dda = facts.get("ddagrab")
    if dda is True:
        add("PASS", "FFmpeg has the ddagrab filter")
    elif dda is False:
        add("FAIL", "FFmpeg has no ddagrab filter (or ffmpeg is not on PATH): rerun setup.ps1")
    else:
        add("WARN", "could not check FFmpeg for ddagrab")

    smoke = facts.get("smoke")
    if smoke is None:
        add("WARN", "capture smoke test was skipped")
    elif smoke.get("ok"):
        add("PASS", "capture smoke test passed")
    else:
        why = "timed out" if smoke.get("timed_out") else f"exit code {smoke.get('code')}"
        add("FAIL", f"capture smoke test failed ({why})")
        idle, limit = facts.get("idle_minutes"), facts.get("monitor_timeout_minutes")
        if idle is not None and limit and idle > limit:
            add(
                "WARN",
                f"hint: idle for {idle:.0f} min and the monitor turns off after {limit:.0f} min, "
                "so the display was probably asleep and capture gets no frames. Wake the screen "
                "and rerun.",
            )

    cats = [str(c).lower() for c in facts.get("network_categories", [])]
    if not cats:
        add("WARN", "could not read the network profile")
    elif "public" in cats:
        add("WARN", "network profile is Public: switch it to Private (Settings > Network)")
    else:
        add("PASS", "network profile is Private")

    rules = facts.get("firewall") or []
    blocks = [
        r
        for r in rules
        if "open-airplay" in str(r.get("Program", "")).lower()
        and str(r.get("Action", "")).lower() == "block"
        and str(r.get("Enabled", "")).lower() in ("true", "1")
    ]
    if blocks:
        profiles = ", ".join(sorted({f"{r.get('Profile')}/{r.get('Direction')}" for r in blocks}))
        add(
            "FAIL",
            f"{len(blocks)} firewall Block rule(s) for open-airplay ({profiles}); remove them",
        )
    elif facts.get("firewall") is None:
        add("WARN", "could not read firewall rules")
    else:
        add("PASS", "no firewall Block rules for open-airplay")

    receivers = facts.get("receivers")
    capable = [r for r in receivers or [] if r.get("mirroring")]
    if receivers is None:
        add("WARN", "receiver discovery was skipped")
    elif not capable:
        add("FAIL", "no mirroring-capable receiver found (TV off, or the network is Public?)")
    else:
        for r in capable:
            note = ""
            if r.get("port") not in (None, 7000):
                note = f" (port {r['port']}, not 7000: pass -port {r['port']})"
            add("PASS", f"receiver found: {r['name']} at {r['host']}{note}")

    nets = []
    for entry in facts.get("local_ipv4", []):
        try:
            nets.append(
                ipaddress.ip_interface(f"{entry['IPAddress']}/{entry['PrefixLength']}").network
            )
        except (KeyError, ValueError):
            continue
    if capable:
        same = []
        for r in capable:
            try:
                addr = ipaddress.ip_address(r["host"])
            except ValueError:
                continue
            if any(addr in n for n in nets):
                same.append(r)
        if same:
            add("PASS", "a receiver is on the same subnet as this PC")
        else:
            add("FAIL", "no receiver is on the same subnet as this PC (different network or VLAN?)")
    else:
        add("WARN", "subnet check skipped: no capable receiver")
    return checks


def build_report(checks: list[tuple[str, str]], extra: str = "") -> str:
    lines = [f"{level}  {message}" for level, message in checks]
    worst = "FAIL" if any(c[0] == "FAIL" for c in checks) else "WARN"
    if all(c[0] == "PASS" for c in checks):
        worst = "PASS"
    lines += ["", f"Overall: {worst}", f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}"]
    lines.append("Review every file in this zip before sharing it.")
    if extra:
        lines += ["", extra]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------------------------


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else "(no output)"


def collect(exe: Path) -> tuple[dict[str, str], dict]:
    """Run every check. Returns ({filename: raw text}, facts)."""
    files: dict[str, str] = {}
    facts: dict = {"exe_present": exe.is_file()}

    files["01-system.txt"] = (
        f"OS: {platform.platform()}\nPython: {sys.version}\nMachine: {platform.machine()}\n"
    )
    go = shutil.which("go")
    files["02-versions.txt"] = "go: " + (
        _first_line(_run([go, "version"], 20)[1]) if go else "not on PATH"
    )
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        files["02-versions.txt"] += "\nffmpeg: " + _first_line(_run([ffmpeg, "-version"], 20)[1])
        enc = _run([ffmpeg, "-hide_banner", "-encoders"], 30)[1]
        h264 = [ln for ln in enc.splitlines() if re.search(r"264|h264", ln, re.IGNORECASE)]
        flt = _run([ffmpeg, "-hide_banner", "-filters"], 30)[1]
        facts["ddagrab"] = bool(re.search(r"\bddagrab\b", flt))
        files["03-ffmpeg.txt"] = (
            "H.264 encoders:\n" + "\n".join(h264) + f"\n\nddagrab: {facts['ddagrab']}\n"
        )
    else:
        facts["ddagrab"] = False
        files["02-versions.txt"] += "\nffmpeg: not on PATH"
        files["03-ffmpeg.txt"] = "ffmpeg is not on PATH\n"

    stamp = REPO_ROOT / "bin" / "build-stamp.txt"
    if not stamp.is_file():
        stamp = exe.parent / "build-stamp.txt"
    files["04-build-stamp.txt"] = (
        stamp.read_text(encoding="utf-8", errors="replace") if stamp.is_file() else "missing\n"
    )

    _, gpus, _ = _powershell("Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }")
    files["05-gpu.txt"] = gpus

    if exe.is_file():
        files["06-displays.txt"] = _run([str(exe), "-list-displays"], 30)[1]
        files["07-audio-devices.txt"] = _run([str(exe), "-list-audio-devices"], 30)[1]
        _, recv, _ = _run([str(exe), "-list-receivers"], 40)
        files["08-receivers.txt"] = recv
        facts["receivers"] = parse_receivers(recv)
        code, out, timed_out = _run([str(exe), "-display", "0", "-capture-smoke", "-no-audio"], 45)
        files["09-capture-smoke.txt"] = out + f"\nexit={code} timed_out={timed_out}\n"
        facts["smoke"] = {"ok": code == 0 and not timed_out, "code": code, "timed_out": timed_out}
    else:
        for name in ("06-displays", "07-audio-devices", "08-receivers", "09-capture-smoke"):
            files[f"{name}.txt"] = "skipped: bin\\open-airplay.exe is missing\n"

    idle = idle_minutes()
    _, powercfg, _ = _run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE"], 20)
    facts["idle_minutes"] = idle
    facts["monitor_timeout_minutes"] = parse_monitor_timeout_minutes(powercfg)
    files["10-idle-and-monitor.txt"] = (
        f"Minutes since last input: {idle if idle is None else round(idle, 1)}\n"
        f"Monitor-off timeout on AC (minutes, 0 = never): {facts['monitor_timeout_minutes']}\n\n"
        + powercfg
    )

    _, prof, _ = _powershell(
        "Get-NetConnectionProfile | ForEach-Object { [pscustomobject]@{ Name=$_.Name; "
        "InterfaceAlias=$_.InterfaceAlias; NetworkCategory=[string]$_.NetworkCategory; "
        "IPv4Connectivity=[string]$_.IPv4Connectivity } } | ConvertTo-Json"
    )
    profiles = _json_list(prof)
    facts["network_categories"] = [p.get("NetworkCategory", "") for p in profiles]
    files["11-network-profile.txt"] = prof

    _, netsh, _ = _run(["netsh", "wlan", "show", "interfaces"], 20)
    files["12-wifi.txt"] = netsh

    _, ips, _ = _powershell(
        "Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { [pscustomobject]@{ "
        "IPAddress=$_.IPAddress; PrefixLength=$_.PrefixLength; "
        "InterfaceAlias=$_.InterfaceAlias } } "
        "| ConvertTo-Json"
    )
    facts["local_ipv4"] = [
        e
        for e in _json_list(ips)
        if not str(e.get("IPAddress", "")).startswith(("127.", "169.254."))
    ]
    files["13-ipv4.txt"] = ips

    rc, fw, _ = _powershell(
        "Get-NetFirewallApplicationFilter | "
        "Where-Object { $_.Program -match 'open-airplay|python' } "
        "| ForEach-Object { $r = $_ | Get-NetFirewallRule; [pscustomobject]@{ "
        "Name=$r.DisplayName; Program=$_.Program; Profile=[string]$r.Profile; "
        "Direction=[string]$r.Direction; Action=[string]$r.Action; Enabled=[string]$r.Enabled } } "
        "| ConvertTo-Json",
        timeout=120,
    )
    facts["firewall"] = _json_list(fw) if rc == 0 else None
    files["14-firewall-rules.txt"] = fw if fw.strip() else "(no matching rules)\n"
    facts["ssids"] = _ssids_from(netsh, profiles)
    return files, facts


def run_session(exe: Path, target: str, port: int, seconds: int) -> str:
    """Run the sender with -debug for a while, console attached so a PIN can be typed.

    stdin and stdout stay on the console (the PIN prompt is there); stderr, which carries the
    log, is copied to the console and kept for redaction.
    """
    lines: list[str] = []
    proc = subprocess.Popen(
        [str(exe), "-target", target, "-port", str(port), "-debug"],
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    def pump() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            text = raw.decode("utf-8", "replace")
            lines.append(text)
            sys.stderr.write(text)
            sys.stderr.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + seconds
    try:
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    if proc.poll() is None:
        # Ask the sender to stop so it sends TEARDOWN. After a hard kill the Samsung stops
        # advertising AirPlay for minutes.
        proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT))
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    reader.join(5)
    return "".join(lines)


def write_zip(files: dict[str, str], report: str, ssids: list[str], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"airmirror-diag-{datetime.now():%Y%m%d-%H%M%S}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.txt", redact(report, ssids))
        for name, text in files.items():
            zf.writestr(name, redact(text, ssids))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a redacted airmirror diagnostics zip.")
    parser.add_argument("--session", metavar="TARGET", help="also run a live -debug session")
    parser.add_argument(
        "--port", type=int, default=7000, help="receiver port for --session (see 08-receivers.txt)"
    )
    parser.add_argument("--seconds", type=int, default=60, help="length of --session")
    parser.add_argument("--exe", type=Path, default=SENDER_EXE, help="path to open-airplay.exe")
    parser.add_argument("--out", type=Path, default=DIAG_DIR, help="output directory")
    args = parser.parse_args(argv)

    files, facts = collect(args.exe)
    if args.session:
        if not args.exe.is_file():
            print("Cannot run --session: the sender binary is missing.", file=sys.stderr)
        else:
            files["15-session-log.txt"] = run_session(
                args.exe, args.session, args.port, args.seconds
            )
    checks = build_checks(facts)
    report = build_report(checks)
    path = write_zip(files, report, facts.get("ssids", []), args.out)
    print(redact(report, facts.get("ssids", [])))
    print(f"Diagnostics zip: {path}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
