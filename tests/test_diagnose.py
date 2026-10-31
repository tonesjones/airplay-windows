import json
import zipfile

import pytest

from airmirror import diagnose
from airmirror.diagnose import build_checks, parse_receivers, redact

NETSH = """\
There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6E AX211 160MHz
    GUID                   : 5c2e3f1a-1111-2222-3333-444455556666
    Physical address       : a4:c3:f0:12:34:56
    State                  : connected
    SSID                   : HomeNet-5G
    BSSID                  : 9c:3d:cf:aa:bb:cc
    Network type           : Infrastructure
    Signal                 : 91%
    Profile                : HomeNet-5G
"""


def levels(checks):
    return {msg: level for level, msg in checks}


def level_of(checks, fragment):
    return [level for level, msg in checks if fragment in msg]


# ---------------------------------------------------------------- redaction


def test_mac_colon_and_hyphen_forms():
    out = redact("eth a4:c3:f0:12:34:56 and A4-C3-F0-12-34-56 and 192.168.1.20")
    assert "a4:c3" not in out.lower()
    assert "a4-c3" not in out.lower()
    assert out.count("<mac>") == 2
    assert "192.168.1.20" in out


def test_mdns_txt_records():
    line = (
        "[debug] mdns txt: model=UNU710D deviceid=A4:C3:F0:12:34:56 features=0x4A7FFFF7,0xE "
        "pk=3f2b9a1c5d7e8f00112233445566778899aabbccddeeff0011223344556677ab "
        "pi=2e388006-13ba-4041-9a67-25dd4a43d536 psi=5f1c2b3a-aaaa-bbbb-cccc-0123456789ab "
        "gid=11111111-2222-3333-4444-555555555555 srcvers=770.8.1"
    )
    out = redact(line)
    for secret in ("A4:C3", "3f2b9a1c", "2e388006", "5f1c2b3a", "11111111-2222"):
        assert secret not in out
    for key in ("deviceid=", "pk=", "pi=", "psi=", "gid="):
        assert key + "<redacted>" in out
    assert "model=UNU710D" in out
    assert "srcvers=770.8.1" in out


def test_debug_log_lines_keep_useful_content():
    line = "2026/10/25 12:00:01 [airplay] SETUP ok to 192.168.1.20:41000 video 1920x1080@30"
    assert redact(line) == line


def test_pin_prompt_reply_is_redacted():
    text = (
        "PAIRING REQUIRED: a PIN should now be visible on Living Room TV.\n"
        "Enter that PIN here: 4821\nnext"
    )
    out = redact(text)
    assert "4821" not in out
    assert "Enter that PIN here: <redacted>" in out
    assert "next" in out
    out2 = redact("Enter its configured password or displayed pairing PIN: hunter2")
    assert "hunter2" not in out2


def test_code_flag_and_env():
    assert "9999" not in redact("open-airplay.exe -target 192.168.1.20 -code 9999")
    assert "9999" not in redact("OPEN_AIRPLAY_CODE=9999")


def test_json_secret_fields():
    text = json.dumps(
        {
            "cmd": "connect",
            "target": "192.168.1.20",
            "pin": "1234",
            "Password": "hunter2",
            "pairingKey": "abc",
            "client_secret": "s3",
            "credentials": "xyz",
            "port": 41000,
        }
    )
    out = redact(text)
    for secret in ("1234", "hunter2", '"abc"', '"s3"', "xyz"):
        assert secret not in out
    assert '"target": "192.168.1.20"' in out
    assert '"port": 41000' in out


def test_log_style_secret_fields_but_not_lookalikes():
    out = redact("auth pin=1234 key=abcd keyframe_interval=30 mapping=none timing=ptp")
    assert "1234" not in out
    assert "abcd" not in out
    assert "keyframe_interval=30" in out
    assert "mapping=none" in out
    assert "timing=ptp" in out


def test_long_hex_and_base64():
    hexrun = "ab" * 20
    b64 = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A="
    out = redact(f"curve25519 {hexrun} token {b64} short deadbeef")
    assert hexrun not in out
    assert b64 not in out
    assert "deadbeef" in out


def test_netsh_ssid_and_bssid():
    out = redact(NETSH, ["HomeNet-5G"])
    assert "HomeNet" not in out
    assert "9c:3d" not in out
    assert "a4:c3" not in out
    assert "State                  : connected" in out


def test_ssid_elsewhere_case_insensitive():
    assert "homenet" not in redact("joined homenet-5g ok", ["HomeNet-5G"]).lower()


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\Jdoe\AppData\Local\Temp\x.log",
        r"c:\users\JDOE\Desktop",
        r"C:\USERS\jdoe\file",
        "C:/Users/Jdoe/file",
        r"C:\\Users\\Jdoe\\file",
    ],
)
def test_user_profile_paths(path):
    out = redact(path)
    assert "jdoe" not in out.lower()
    assert "<user>" in out


def test_user_path_keeps_rest_of_path():
    assert redact(r"C:\Users\Jdoe\AppData\x") == r"C:\Users\<user>\AppData\x"


def test_lan_ips_survive():
    text = "endpoint=192.168.1.20:41000 gw 10.0.0.1 local 172.16.5.9/24"
    assert redact(text) == text


# ---------------------------------------------------------------- parsing

RECEIVERS = (
    '[1] name="Living Room TV" model="UNU710D" mirroring=yes endpoint=192.168.1.20:41000\n'
    '[2] name="Speaker" model="AudioAccessory6,1" mirroring=no endpoint=192.168.1.60:7000\n'
)


def test_parse_receivers():
    r = parse_receivers(RECEIVERS)
    assert len(r) == 2
    assert r[0]["mirroring"] is True and r[0]["port"] == 41000 and r[0]["host"] == "192.168.1.20"
    assert r[1]["mirroring"] is False


def test_monitor_timeout():
    text = (
        "    Current AC Power Setting Index: 0x00000384\n"
        "    Current DC Power Setting Index: 0x000001e0\n"
    )
    assert diagnose.parse_monitor_timeout_minutes(text) == 15
    assert diagnose.parse_monitor_timeout_minutes("garbage") is None


# ---------------------------------------------------------------- report checks


def good_facts(**over):
    facts = {
        "exe_present": True,
        "ddagrab": True,
        "smoke": {"ok": True, "code": 0, "timed_out": False},
        "idle_minutes": 1.0,
        "monitor_timeout_minutes": 15.0,
        "network_categories": ["Private"],
        "firewall": [
            {
                "Program": r"C:\x\bin\open-airplay.exe",
                "Action": "Allow",
                "Enabled": "True",
                "Profile": "Private",
                "Direction": "Inbound",
            }
        ],
        "receivers": parse_receivers(RECEIVERS.replace(":41000", ":7000")),
        "local_ipv4": [{"IPAddress": "192.168.1.10", "PrefixLength": 24}],
    }
    facts.update(over)
    return facts


def test_all_pass():
    checks = build_checks(good_facts())
    assert {level for level, _ in checks} == {"PASS"}
    assert len(checks) == 7


def test_missing_binary_and_ddagrab_fail():
    checks = build_checks(good_facts(exe_present=False, ddagrab=False, smoke=None, receivers=None))
    assert level_of(checks, "open-airplay.exe is missing") == ["FAIL"]
    assert level_of(checks, "ddagrab") == ["FAIL"]
    assert level_of(checks, "smoke") == ["WARN"]


def test_public_network_warns():
    checks = build_checks(good_facts(network_categories=["Public"]))
    assert level_of(checks, "Public") == ["WARN"]


def test_block_rule_fails():
    block = {
        "Program": r"C:\x\bin\open-airplay.exe",
        "Action": "Block",
        "Enabled": "True",
        "Profile": "Public",
        "Direction": "Inbound",
    }
    checks = build_checks(good_facts(firewall=[block]))
    assert level_of(checks, "Block rule") == ["FAIL"]
    disabled = dict(block, Enabled="False")
    assert level_of(build_checks(good_facts(firewall=[disabled])), "Block rules") == ["PASS"]
    python_block = dict(block, Program=r"C:\Python\python.exe")
    assert level_of(build_checks(good_facts(firewall=[python_block])), "Block rules") == ["PASS"]


def test_idle_monitor_capture_failure_adds_hint():
    smoke = {"ok": False, "code": None, "timed_out": True}
    checks = build_checks(good_facts(smoke=smoke, idle_minutes=44.0, monitor_timeout_minutes=15.0))
    assert level_of(checks, "smoke test failed") == ["FAIL"]
    assert level_of(checks, "probably asleep") == ["WARN"]


def test_capture_failure_without_idle_has_no_hint():
    smoke = {"ok": False, "code": 1, "timed_out": False}
    checks = build_checks(good_facts(smoke=smoke, idle_minutes=2.0))
    assert level_of(checks, "smoke test failed") == ["FAIL"]
    assert not level_of(checks, "probably asleep")
    never = build_checks(good_facts(smoke=smoke, idle_minutes=99.0, monitor_timeout_minutes=0))
    assert not level_of(never, "probably asleep")


def test_no_receiver_fails():
    checks = build_checks(good_facts(receivers=[]))
    assert level_of(checks, "no mirroring-capable receiver") == ["FAIL"]


def test_audio_only_receiver_is_not_capable():
    only = parse_receivers(RECEIVERS)[1:]
    assert level_of(build_checks(good_facts(receivers=only)), "no mirroring-capable") == ["FAIL"]


def test_non_7000_port_noted():
    checks = build_checks(good_facts(receivers=parse_receivers(RECEIVERS)))
    assert any("-port 41000" in msg for _, msg in checks)


def test_different_subnet_fails():
    facts = good_facts(local_ipv4=[{"IPAddress": "10.1.2.3", "PrefixLength": 24}])
    assert level_of(build_checks(facts), "same subnet") == ["FAIL"]


# ---------------------------------------------------------------- zip building


def test_zip_building_with_fake_subprocess(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "open-airplay.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")

    profile = json.dumps(
        {"Name": "HomeNet-5G", "InterfaceAlias": "Wi-Fi", "NetworkCategory": "Private"}
    )
    ips = json.dumps({"IPAddress": "192.168.1.10", "PrefixLength": 24, "InterfaceAlias": "Wi-Fi"})
    user = r"C:\Users\Jdoe"

    def fake_run(args, timeout=30):
        joined = " ".join(str(a) for a in args)
        if "-list-receivers" in joined:
            return 0, RECEIVERS.replace(":41000", ":7000"), False
        if "-capture-smoke" in joined:
            return 0, f"SPS PPS IDR from {user}\\x\n", False
        if "-list-displays" in joined or "-list-audio-devices" in joined:
            return 0, f"[0] display {user}\n", False
        if args[0] == "netsh":
            return 0, NETSH, False
        if args[0] == "powercfg":
            return 0, "Current AC Power Setting Index: 0x00000384\n", False
        if "Get-NetConnectionProfile" in joined:
            return 0, profile, False
        if "Get-NetIPAddress" in joined:
            return 0, ips, False
        if "Get-NetFirewall" in joined:
            return 0, "[]", False
        if "Win32_VideoController" in joined:
            return 0, "NVIDIA GeForce RTX 4070 SUPER\n", False
        if "-filters" in joined:
            return 0, " T.. ddagrab  V->V  Grab Windows Desktop images\n", False
        if "-encoders" in joined:
            return 0, " V....D libx264  H.264\n V....D h264_nvenc  NVENC H.264\n", False
        return 0, "version 1.0\n", False

    monkeypatch.setattr(diagnose, "_run", fake_run)
    monkeypatch.setattr(
        diagnose, "_powershell", lambda script, timeout=60: fake_run(["powershell", script])
    )
    monkeypatch.setattr(diagnose, "idle_minutes", lambda: 0.5)
    monkeypatch.setattr(diagnose.shutil, "which", lambda name: name)

    out_dir = tmp_path / "out"
    assert diagnose.main(["--exe", str(exe), "--out", str(out_dir)]) == 0

    (zip_path,) = out_dir.glob("airmirror-diag-*.zip")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        contents = {n: zf.read(n).decode("utf-8") for n in names}
    assert "report.txt" in names
    assert names[0] == "report.txt"
    assert len(names) > 10
    report = contents["report.txt"]
    assert report.startswith("PASS")
    assert "Overall: PASS" in report
    blob = "\n".join(contents.values())
    assert "HomeNet" not in blob
    assert "9c:3d:cf" not in blob.lower()
    assert "a4:c3:f0" not in blob.lower()
    assert "jdoe" not in blob.lower()
    assert "192.168.1.10" in blob
    assert "192.168.1.20" in blob
    assert "ddagrab" in contents["03-ffmpeg.txt"]


def test_global_ipv6_is_redacted_but_link_local_and_times_survive():
    out = redact("to [2601:646:8a00:1e0::5]:7000 via fe80::1%12 at 15:28:03 ::1")
    assert "2601" not in out and "<ipv6>" in out
    assert "fe80::1" in out and "15:28:03" in out and "::1" in out
