"""AirMirror window: pick a TV, press Start, see the desktop on the TV.

The layout follows Windows 11 quick settings: a TV picker, a Start/Stop bar and six tiles,
all drawn on one canvas that is redrawn from state on every change.
"""

from __future__ import annotations

import collections
import ctypes
import ipaddress
import json
import os
import queue
import shutil
import threading
import time
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import font as tkfont
from tkinter import simpledialog
from typing import NamedTuple

from . import keepawake, probe, ui
from .pcmute import SpeakerMute
from .sender import SenderError, SenderProcess, list_audio_devices

QUALITY = {
    "Auto": {"fps": 0, "bitrate_kbps": 0},
    "Smooth": {"fps": 30, "bitrate_kbps": 8000},
    "Weak Wi-Fi": {"fps": 30, "bitrate_kbps": 3000},
}
ENCODERS = {"nvenc": "NVENC", "auto": "Auto", "none": "Software"}
DEFAULT_AUDIO = "Default output"
REFRESH_MS = 5000
PROBE_RETRY_S = 15  # wait between direct scans of a TV that mDNS doesn't list
MAX_LOG_LINES = 1000
MAX_RECENT_TVS = 5
WIDTH, HEIGHT = 400, 420
SETTINGS_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "AirMirror" / "settings.json"

# Segoe Fluent Icons / MDL2 Assets code points.
GLYPH = {
    "tv": "",
    "power": "",
    "sound": "",
    "mute": "",
    "screen_on": "",
    "quality": "",
    "encoder": "",
    "display": "",
    "down": "",
    "right": "",
}

# User-facing wording per error kind (from the protocol doc). Raw messages are never shown.
ERROR_TEXT = {
    "busy": "Already mirroring to {tv}.",
    "unreachable": "{tv} didn't answer. Check it's on and on the same network.",
    "wrong_code": "That code didn't work. Press Start to get a new one.",
    "rejected": "{tv} didn't accept this PC.",
    "capture_failed": "Couldn't capture this PC's screen. Is the monitor awake?",
    "ended": "Mirroring to {tv} ended.",
    "failed": "Couldn't mirror to {tv}.",
}
GENERIC_ERROR = "Couldn't mirror to {tv}."


def error_text(kind: str, tv: str) -> str:
    return ERROR_TEXT.get(kind, GENERIC_ERROR).format(tv=tv or "the TV")


def default_encoder() -> str:
    # NVIDIA drivers install nvidia-smi. NVENC is the encoder verified on the Samsung.
    return "nvenc" if shutil.which("nvidia-smi") else "auto"


def load_settings(path: Path) -> dict:
    settings = {
        "tv": "",
        "tv_ip": "",
        "tv_name": "",
        "sound": True,
        "mute_pc": True,
        "keep_awake": True,
        "quality": "Auto",
        "encoder": default_encoder(),
        "display": None,
        "audio_id": "",
        "recent_tvs": [],  # TVs that streamed, newest first: {name, ip, device_id}
    }
    try:
        saved = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(saved, dict):
        settings.update({k: v for k, v in saved.items() if k in settings})
    recent = settings["recent_tvs"]
    recent = recent if isinstance(recent, list) else []
    settings["recent_tvs"] = [t for t in recent if isinstance(t, dict) and t.get("ip")]
    return settings


class Tile(NamedTuple):
    key: str
    glyph: str
    title: str
    sub: str
    on: bool
    enabled: bool


def tv_id(device: dict) -> str:
    return device.get("device_id") or device.get("name", "")


class App:
    def __init__(
        self,
        root: tk.Tk,
        argv: Sequence[str] | None = None,
        auto_refresh: bool = True,
        settings_path: Path = SETTINGS_PATH,
    ):
        self.root = root
        self.argv = argv  # test override for the sender command
        self.settings_path = settings_path
        self.settings = load_settings(settings_path)
        self.events: queue.Queue = queue.Queue()
        self.sender: SenderProcess | None = None
        self.generation = 0
        self.audio_capable = False
        self.audio_devices: list[dict] = []
        self.receivers: list[dict] = []
        self.searched = False  # a devices reply has arrived at least once
        self.displays: list[dict] = []
        self.tv: dict | None = None  # the TV the picker shows
        self.target: str | None = None  # ip we last pressed Start for
        self.target_name = ""
        self.active = False  # connecting, waiting for a code, or streaming
        self.streaming = False
        self.stream_started = 0.0
        self.sender_dead = False
        self.pin_dialog: tk.Toplevel | None = None
        self.log_window: tk.Toplevel | None = None
        self.logs: collections.deque[str] = collections.deque(maxlen=MAX_LOG_LINES)
        self.shown_errors: set[tuple[str, str]] = set()
        self.last_state = "starting"
        self.closing = False
        self._error_text: str | None = None
        self.speaker: SpeakerMute | None = None
        self.awake = False
        self.hover: str | None = None
        self.probing = False
        self.last_probe = 0.0
        self.probed: tuple[str, int] | None = None  # (ip, port) a direct scan found
        self.start_when_found = False
        if self.settings["tv_ip"]:
            # Show the last TV right away: it can stay silent over mDNS for minutes.
            self.tv = {
                "name": self.settings["tv_name"] or "TV",
                "ip": self.settings["tv_ip"],
                "device_id": self.settings["tv"],
            }

        self.dark = ui.windows_uses_dark()
        self.c = ui.DARK if self.dark else ui.LIGHT
        self.scale = root.winfo_fpixels("1i") / 96
        self.icons = ui.icon_font(root)
        self.shapes = ui.Shapes(root)
        self.canvas = tk.Canvas(root, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(None))

        root.title("AirMirror")
        root.configure(bg=self.c["bg"])
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        ui.match_title_bar(root, self.dark)
        self.render()
        self._start_sender()
        root.after(100, self._pump)
        if auto_refresh:
            root.after(REFRESH_MS, self._auto_refresh)

    # -- mirroring side effects -------------------------------------------
    def on_streaming_started(self) -> None:
        """Called on the transition into streaming."""
        if self.settings["mute_pc"]:
            self._mute_pc(True)
        if self.settings["keep_awake"]:
            self._keep_awake(True)

    def on_streaming_stopped(self) -> None:
        """Called on the transition out of streaming."""
        self._mute_pc(False)
        self._keep_awake(False)

    def _mute_pc(self, on: bool) -> None:
        try:
            if on and not self.speaker:
                speaker = SpeakerMute()
                speaker.mute()
                self.speaker = speaker
            elif not on and self.speaker:
                self.speaker.restore()
                self.speaker = None
        except OSError as exc:
            self._log(f"[mute] {exc}")

    def _keep_awake(self, on: bool) -> None:
        try:
            if on and not self.awake:
                keepawake.hold()
                self.awake = True
            elif not on and self.awake:
                keepawake.release()
                self.awake = False
        except OSError as exc:
            self._log(f"[keep awake] {exc}")

    # -- settings ---------------------------------------------------------
    def _set(self, **changes) -> None:
        self.settings.update(changes)
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(self.settings, indent=2), "utf-8")
        except OSError as exc:
            self._log(f"[settings] {exc}")
        self.render()

    # -- drawing ----------------------------------------------------------
    def px(self, n: float) -> int:
        return round(n * self.scale)

    def _box(self, x0, y0, x1, y1, radius, color, tag=()) -> None:
        img = self.shapes.get(self.px(x1 - x0), self.px(y1 - y0), self.px(radius), color)
        self.canvas.create_image(self.px(x0), self.px(y0), image=img, anchor="nw", tags=tag)

    def _text(self, x, y, text, color, size=10, bold=False, anchor="w", tag=(), icon=False):
        family = self.icons if icon else "Segoe UI"
        font = (family, size, "bold") if bold else (family, size)
        self.canvas.create_text(
            self.px(x), self.px(y), text=text, fill=color, font=font, anchor=anchor, tags=tag
        )

    def _fit(self, text: str, size: int, width: float, bold=False) -> str:
        font = tkfont.Font(family="Segoe UI", size=size, weight="bold" if bold else "normal")
        limit = self.px(width)
        if font.measure(text) <= limit:
            return text
        while text and font.measure(text + "…") > limit:
            text = text[:-1]
        return text + "…"

    def render(self) -> None:
        c, cv = self.c, self.canvas
        cv.delete("all")
        cv.configure(width=self.px(WIDTH), height=self.px(HEIGHT), bg=c["bg"])
        self._draw_picker()
        self._draw_start_bar()
        self._draw_tiles()

        y = 374
        cv.create_line(self.px(16), self.px(y), self.px(WIDTH - 16), self.px(y), fill=c["line"])
        text, dot = self._status()
        cv.create_oval(
            self.px(20), self.px(y + 21), self.px(28), self.px(y + 29), fill=dot, outline=""
        )
        self._text(36, y + 25, self._fit(text, 9, WIDTH - 100), c["muted"], size=9)
        link = c["accent_hover"] if self.hover == "log" else c["accent"]
        self._text(WIDTH - 20, y + 25, "Log", link, size=9, anchor="e", tag="log")

    def _draw_picker(self) -> None:
        c = self.c
        locked = self.active
        hovered = self.hover == "tv" and not locked
        self._box(16, 16, WIDTH - 16, 64, 10, c["card_hover"] if hovered else c["card"], "tv")
        ink = c["muted"] if locked else c["text"]
        self._text(36, 40, GLYPH["tv"], ink, size=14, tag="tv", icon=True)
        if self.tv:
            name = self.tv.get("name", "TV")
        elif self.searched:
            name = "No TV found yet"
        else:
            name = "Looking for TVs…"
        self._text(
            66, 40, self._fit(name, 11, WIDTH - 190, bold=True), ink, size=11, bold=True, tag="tv"
        )
        if not locked:
            self._text(
                WIDTH - 36, 40, GLYPH["down"], c["muted"], size=10, anchor="e", tag="tv", icon=True
            )

    def _draw_start_bar(self) -> None:
        c = self.c
        hovered = self.hover == "start"
        if self.sender_dead:
            fill, ink = c["card_hover"] if hovered else c["card"], c["text"]
            title, sub = "Restart", "The sender stopped. Click to start it again."
        elif self.streaming:
            fill, ink = c["green_hover"] if hovered else c["green"], c["on_accent"]
            title, sub = "Stop mirroring", self._stream_summary()
        elif self.active:
            fill, ink = c["accent_hover"] if hovered else c["accent"], c["on_accent"]
            title, sub = "Cancel", f"Connecting to {self.target_name or 'the TV'}…"
        elif self.tv:
            fill, ink = c["accent_hover"] if hovered else c["accent"], c["on_accent"]
            title, sub = "Start mirroring", "Off"
        else:
            fill, ink = c["card"], c["muted"]
            title, sub = "Start mirroring", "Pick a TV first"
        self._box(16, 80, WIDTH - 16, 152, 14, fill, "start")
        self._text(40, 116, GLYPH["power"], ink, size=22, tag="start", icon=True)
        self._text(84, 104, title, ink, size=13, bold=True, tag="start")
        self._text(84, 130, self._fit(sub, 9, WIDTH - 120), ink, size=9, tag="start")

    def _stream_summary(self) -> str:
        secs = int(time.monotonic() - self.stream_started)
        parts = [
            f"{secs // 3600}:{secs // 60 % 60:02}:{secs % 60:02}"
            if secs >= 3600
            else f"{secs // 60}:{secs % 60:02}"
        ]
        if self.sender and self.sender.resolution:
            parts.append("{}×{}".format(*self.sender.resolution))
        parts.append("sound on" if self.settings["sound"] and self.audio_capable else "no sound")
        return "  ·  ".join(parts)

    def _tiles(self) -> list[Tile]:
        s, locked = self.settings, self.active
        sound_on = bool(s["sound"] and self.audio_capable)
        return [
            Tile(
                "sound",
                GLYPH["sound"],
                "TV sound",
                ("On" if sound_on else "Off") if self.audio_capable else "Unavailable",
                sound_on,
                self.audio_capable and not locked,
            ),
            Tile(
                "mute_pc",
                GLYPH["mute"],
                "Mute PC",
                "On" if s["mute_pc"] else "Off",
                s["mute_pc"],
                True,
            ),
            Tile(
                "keep_awake",
                GLYPH["screen_on"],
                "Screen on",
                "On" if s["keep_awake"] else "Off",
                s["keep_awake"],
                True,
            ),
            Tile("quality", GLYPH["quality"], "Quality", s["quality"], False, not locked),
            Tile(
                "encoder",
                GLYPH["encoder"],
                "Encoder",
                ENCODERS.get(s["encoder"], "Auto"),
                False,
                not locked,
            ),
            Tile("display", GLYPH["display"], "Screen", self._display_label(), False, not locked),
        ]

    def _draw_tiles(self) -> None:
        c = self.c
        tw, th, gap = (WIDTH - 32 - 24) / 3, 88, 12
        for i, (key, glyph, title, sub, on, enabled) in enumerate(self._tiles()):
            x0 = 16 + (i % 3) * (tw + gap)
            y0 = 166 + (i // 3) * (th + gap)
            tag = f"tile:{key}"
            hovered = enabled and self.hover == tag
            if on:
                fill = c["accent_hover"] if hovered else c["accent"]
                ink = sub_ink = c["on_accent"]
            else:
                fill = c["card_hover"] if hovered else c["card"]
                ink, sub_ink = (c["text"], c["muted"]) if enabled else (c["muted"], c["muted"])
            self._box(x0, y0, x0 + tw, y0 + th, 12, fill, tag)
            self._text(x0 + 14, y0 + 22, glyph, ink, size=15, tag=tag, icon=True)
            self._text(x0 + 14, y0 + 52, title, ink, size=10, bold=True, tag=tag)
            self._text(x0 + 14, y0 + 71, self._fit(sub, 9, tw - 24), sub_ink, size=9, tag=tag)
            if key == "sound" and enabled:
                # Split tile, as in Windows quick settings: the body toggles, the chevron opens
                # the sound device menu.
                chev = "chev:sound"
                shade = c["accent_dim"] if on else c["line"]
                if self.hover == chev:
                    shade = c["accent_hover"] if on else c["card_hover"]
                self._box(x0 + tw - 34, y0 + 8, x0 + tw - 8, y0 + 34, 8, shade, chev)
                self._text(
                    x0 + tw - 21,
                    y0 + 21,
                    GLYPH["right"],
                    ink,
                    size=9,
                    anchor="center",
                    tag=chev,
                    icon=True,
                )

    def _display_label(self) -> str:
        index = self._display_index()
        if index is None:
            return "Main"
        d = next((d for d in self.displays if d.get("index") == index), {})
        return "Main" if d.get("primary") else f"Screen {index + 1}"

    def _status(self) -> tuple[str, str]:
        c = self.c
        if self._error_text and not self.streaming:
            return self._error_text, c["red"]
        if self.streaming:
            text = "Mirroring"
            if self.sender and self.sender.resolution:
                text += " {}×{}".format(*self.sender.resolution)
            return text + (
                " with sound" if self.settings["sound"] and self.audio_capable else ""
            ), c["green"]
        if self.pin_dialog:
            return f"Enter the code shown on {self.target_name or 'the TV'}.", c["accent"]
        if self.active:
            return f"Connecting to {self.target_name or 'the TV'}…", c["accent"]
        if self.last_state == "starting":
            return "Starting…", c["muted"]
        if not self.tv and self.searched:
            return "No TV found. Is it on? Click the TV box for options.", c["muted"]
        if not self.tv:
            return "Looking for AirPlay TVs on your network…", c["muted"]
        return "Ready. The TV shows a code the first time.", c["muted"]

    # -- mouse ------------------------------------------------------------
    def _tag_at_pointer(self) -> str | None:
        for tag in self.canvas.gettags("current"):
            if tag != "current":
                return tag
        return None

    def _motion(self, _event) -> None:
        self._set_hover(self._tag_at_pointer())

    def _set_hover(self, tag: str | None) -> None:
        if tag != self.hover:
            self.hover = tag
            self.canvas.configure(cursor="hand2" if tag else "")
            self.render()

    def _click(self, event) -> None:
        tag = self._tag_at_pointer()
        if tag == "tv" and not self.active:
            self._popup(event, self._tv_menu())
        elif tag == "start":
            self._press_start()
        elif tag == "log":
            self._show_log()
        elif tag == "chev:sound":
            self._popup(event, self._sound_menu())
        elif tag and tag.startswith("tile:"):
            self._press_tile(tag[5:], event)

    def _press_start(self) -> None:
        if self.sender_dead:
            self._restart_sender()
        elif self.active:
            self.stop()
        elif self.tv:
            self.start()

    def _press_tile(self, key: str, event) -> None:
        if not next(t for t in self._tiles() if t.key == key).enabled:
            return
        if key == "sound":
            self._set(sound=not self.settings["sound"])
        elif key == "mute_pc":
            self._set(mute_pc=not self.settings["mute_pc"])
            if self.streaming:
                self._mute_pc(self.settings["mute_pc"])
        elif key == "keep_awake":
            self._set(keep_awake=not self.settings["keep_awake"])
            if self.streaming:
                self._keep_awake(self.settings["keep_awake"])
        elif key == "quality":
            self._popup(event, self._choice_menu("quality", [(q, q) for q in QUALITY]))
        elif key == "encoder":
            self._popup(event, self._choice_menu("encoder", list(ENCODERS.items())))
        elif key == "display":
            choices = [
                (
                    d.get("index"),
                    f"Screen {d.get('index', 0) + 1}  ({d.get('width')}×"
                    f"{d.get('height')})" + ("  ·  main" if d.get("primary") else ""),
                )
                for d in self.displays
            ]
            self._popup(event, self._choice_menu("display", choices))

    # -- menus ------------------------------------------------------------
    def _menu(self) -> tk.Menu:
        c = self.c
        return tk.Menu(
            self.root,
            tearoff=0,
            bg=c["card"],
            fg=c["text"],
            activebackground=c["accent"],
            activeforeground=c["on_accent"],
            selectcolor=c["text"],
            bd=0,
            font=("Segoe UI", 10),
        )

    def _popup(self, event, menu: tk.Menu) -> None:
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _choice_menu(self, key: str, choices: list[tuple]) -> tk.Menu:
        menu = self._menu()
        if not choices:
            menu.add_command(label="Nothing to choose yet", state="disabled")
        current = self._display_index() if key == "display" else self.settings[key]
        for value, label in choices:
            mark = "✓  " if value == current else "     "
            menu.add_command(label=mark + label, command=lambda v=value: self._set(**{key: v}))
        return menu

    def _tv_menu(self) -> tk.Menu:
        menu = self._menu()
        if not self.receivers:
            label = "No TVs found on the network" if self.searched else "Looking for TVs…"
            menu.add_command(label=label, state="disabled")
        current = tv_id(self.tv) if self.tv else None
        for d in self.receivers:
            mark = "✓  " if tv_id(d) == current else "     "
            label = f"{mark}{d.get('name', 'TV')}   {d.get('ip', '')}"
            menu.add_command(label=label, command=lambda d=d: self._choose_tv(d))
        listed = {d.get("ip") for d in self.receivers}
        recent = [t for t in self.settings["recent_tvs"] if t["ip"] not in listed]
        if recent:
            menu.add_separator()
            menu.add_command(label="Worked before", state="disabled")
            current_ip = self.tv["ip"] if self.tv else None
            for t in recent:
                mark = "✓  " if t["ip"] == current_ip else "     "
                label = f"{mark}{t.get('name') or 'TV'}   {t['ip']}"
                menu.add_command(label=label, command=lambda t=t: self._use_unlisted_tv(dict(t)))
        menu.add_separator()
        menu.add_command(label="     Search again", command=self.refresh_receivers)
        menu.add_command(label="     Type the TV's IP address…", command=self._ask_tv_ip)
        return menu

    def _ask_tv_ip(self) -> None:
        text = simpledialog.askstring(
            "TV IP address",
            "Use this only if your TV isn't in the list.\n\n"
            "On a Samsung TV, the IP address is under Settings > General >\n"
            "Network > Network Status > IP Settings. Your router's app\n"
            "usually lists it too.\n\n"
            "TV IP address:",
            initialvalue=self.tv["ip"] if self.tv else "",
            parent=self.root,
        )
        if text is not None and not self.use_tv_ip(text):
            self._error_text = f"{text.strip()} isn't an IP address."
            self.render()

    def use_tv_ip(self, text: str) -> bool:
        """Pick the TV at this address, for when mDNS doesn't list it. False if not an IP."""
        try:
            ip = str(ipaddress.ip_address(text.strip()))
        except ValueError:
            return False
        known = next((t for t in self.settings["recent_tvs"] if t["ip"] == ip), None)
        self._use_unlisted_tv(dict(known) if known else {"name": ip, "ip": ip})
        return True

    def _use_unlisted_tv(self, tv: dict) -> None:
        """Pick a TV that mDNS doesn't list, then ask it for its AirPlay port directly."""
        self._choose_tv(tv)
        self.probed = None
        self._probe()
        self.render()

    def _sound_menu(self) -> tk.Menu:
        menu = self._menu()
        choices = [("", DEFAULT_AUDIO)] + [(d["id"], d["name"]) for d in self.audio_devices]
        for value, label in choices:
            mark = "✓  " if value == self.settings["audio_id"] else "     "
            menu.add_command(label=mark + label, command=lambda v=value: self._choose_audio(v))
        return menu

    def _choose_tv(self, device: dict) -> None:
        self.tv = device
        self._error_text = None
        self._remember_tv()

    def _choose_audio(self, audio_id: str) -> None:
        if audio_id != self.settings["audio_id"]:
            self._set(audio_id=audio_id)
            self._restart_sender()

    # -- log --------------------------------------------------------------
    def _log(self, line: str) -> None:
        self.logs.append(line)
        if self.log_window:
            text = self.log_window.text
            text.configure(state="normal")
            text.insert("end", line + "\n")
            count = int(text.index("end-1c").split(".")[0])
            if count > MAX_LOG_LINES:
                text.delete("1.0", f"{count - MAX_LOG_LINES}.0")
            text.see("end")
            text.configure(state="disabled")

    def _show_log(self) -> None:
        if self.log_window:
            self.log_window.lift()
            return
        c = self.c
        win = tk.Toplevel(self.root, bg=c["bg"])
        win.title("AirMirror log")
        text = tk.Text(
            win,
            width=90,
            height=24,
            wrap="none",
            bg=c["card"],
            fg=c["text"],
            insertbackground=c["text"],
            relief="flat",
            font=("Cascadia Mono", 9),
            padx=8,
            pady=8,
        )
        text.pack(fill="both", expand=True, padx=12, pady=12)
        text.insert("end", "".join(line + "\n" for line in self.logs))
        text.see("end")
        text.configure(state="disabled")
        win.text = text

        def closed():
            self.log_window = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", closed)
        ui.match_title_bar(win, self.dark)
        self.log_window = win

    # -- sender lifecycle -------------------------------------------------
    def _start_sender(self) -> None:
        self.generation += 1
        gen = self.generation
        post = self.events.put
        audio_id = self.settings["audio_id"]
        extra = ["-audio-device", audio_id] if audio_id else []
        try:
            sender = SenderProcess(
                extra,
                argv=self.argv,
                on_state=lambda m: post((gen, "state", m)),
                on_log=lambda s: post((gen, "log", s)),
                on_exit=lambda c: post((gen, "exit", c)),
            )
        except SenderError as exc:
            self._log(f"[sender] {exc}")
            self.sender_dead = True
            self._error_text = "Can't start the sender. Run setup.ps1 first."
            self.render()
            return
        self.sender = sender
        self.sender_dead = False

        def hello():
            try:
                sender.wait_hello()
                post((gen, "hello", sender.capabilities))
            except SenderError:
                pass  # the exit event reports it

        threading.Thread(target=hello, daemon=True).start()

    def _restart_sender(self) -> None:
        old, self.sender = self.sender, None
        self.generation += 1  # drops late events from the old process
        self._set_streaming(False)
        self.active = False
        self.target = None
        self._error_text = None
        self.last_state = "starting"
        if old:
            threading.Thread(target=self._shutdown, args=(old,), daemon=True).start()
        self._start_sender()
        self.render()

    @staticmethod
    def _shutdown(sender: SenderProcess) -> None:
        try:
            sender.disconnect()
        except SenderError:
            pass
        sender.close()

    def _bg(self, fn: Callable[[], object], done: Callable[[object], None] | None = None) -> None:
        sender, gen = self.sender, self.generation

        def work():
            try:
                result = fn()
            except SenderError as exc:
                self.events.put((gen, "error", str(exc)))
                return
            if done:
                self.events.put((gen, "call", lambda: done(result)))

        if sender:
            threading.Thread(target=work, daemon=True).start()

    # -- event pump -------------------------------------------------------
    def _pump(self) -> None:
        try:
            while True:
                gen, kind, data = self.events.get_nowait()
                if gen != self.generation:
                    continue
                self._handle(kind, data)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(100, self._pump)

    def _handle(self, kind: str, data) -> None:
        if kind == "log":
            self._log(data)
            if self.streaming:
                self.render()  # the resolution comes from a log line
        elif kind == "hello":
            self._on_hello(data)
        elif kind == "state":
            self._on_state(data)
        elif kind == "call":
            data()
        elif kind == "error":
            self._log(f"[error] {data}")
            self._error_text = "Lost contact with the sender. Try Start again."
            self.render()
        elif kind == "exit":
            self._log(f"[sender exited with code {data}]")
            self._close_pin_dialog()
            self._set_streaming(False)
            self.active = False
            self.sender_dead = True
            self.last_state = "stopped"
            self._error_text = "The sender stopped."
            self.render()

    def _on_hello(self, caps: dict) -> None:
        self.audio_capable = bool(caps.get("audio"))
        if self.audio_capable and not self.audio_devices:
            self._bg(list_audio_devices, self._set_audio_devices)
        if self.last_state == "starting":
            self.last_state = "idle"
        self.refresh_receivers()
        self._bg(lambda: self.sender.displays(), self._set_displays)
        self.render()

    def _set_audio_devices(self, devices) -> None:
        self.audio_devices = devices

    def _set_displays(self, displays) -> None:
        self.displays = displays
        self.render()

    def _display_index(self) -> int | None:
        indices = [d.get("index") for d in self.displays]
        if self.settings["display"] in indices:
            return self.settings["display"]
        return next((d.get("index") for d in self.displays if d.get("primary")), None)

    # -- receivers --------------------------------------------------------
    def refresh_receivers(self) -> None:
        if self.sender and self.sender.alive:
            self._bg(lambda: self.sender.devices(), self._set_receivers)

    def _auto_refresh(self) -> None:
        if self.closing:
            return
        if not self.active:
            self.refresh_receivers()
            stale = time.monotonic() - self.last_probe > PROBE_RETRY_S
            if self.searched and self._tv_port() is None and stale:
                self._probe()
        self.root.after(REFRESH_MS, self._auto_refresh)

    def _set_receivers(self, devices) -> None:
        self.receivers = devices
        self.searched = True
        wanted = tv_id(self.tv) if self.tv else self.settings["tv"]
        match = next((d for d in devices if tv_id(d) == wanted), None)
        if not match and self.tv and not self.tv.get("device_id"):
            # A TV typed in by IP has no device id until mDNS lists it.
            match = next((d for d in devices if d.get("ip") == self.tv["ip"]), None)
            if match:
                self._choose_tv(match)  # save its real name and device id
        if match:
            self.tv = match  # fresh ip and port; the Samsung's port changes on every reboot
        elif self.tv is None and len(devices) == 1:
            self._choose_tv(devices[0])
        self.render()

    def _listed(self) -> dict | None:
        if not self.tv:
            return None
        return next((d for d in self.receivers if tv_id(d) == tv_id(self.tv)), None)

    def _tv_port(self) -> int | None:
        """The TV's AirPlay port from mDNS, else from a direct scan, else None."""
        listed = self._listed()
        if listed:
            return listed.get("port") or None
        if self.tv and self.probed and self.probed[0] == self.tv["ip"]:
            return self.probed[1]
        return None

    def _remember_tv(self) -> None:
        self._set(tv=tv_id(self.tv), tv_ip=self.tv["ip"], tv_name=self.tv.get("name", ""))

    def _add_recent_tv(self) -> None:
        tv = {
            "name": self.target_name or self.tv.get("name", ""),  # the TV's own name once streaming
            "ip": self.tv["ip"],
            "device_id": self.tv.get("device_id", ""),
        }
        older = [
            t
            for t in self.settings["recent_tvs"]
            if t["ip"] != tv["ip"]
            and not (tv["device_id"] and t.get("device_id") == tv["device_id"])
        ]
        self._set(recent_tvs=[tv, *older][:MAX_RECENT_TVS])

    def _probe(self) -> None:
        if self.probing or not self.tv:
            return
        self.probing = True
        self.last_probe = time.monotonic()
        ip = self.tv["ip"]

        def work():
            port = probe.find_port(ip)
            self.events.put((self.generation, "call", lambda: self._probed(ip, port)))

        threading.Thread(target=work, daemon=True).start()

    def _probed(self, ip: str, port: int | None) -> None:
        self.probing = False
        if not self.tv or self.tv["ip"] != ip:
            return
        if port:
            self.probed = (ip, port)
            self._log(f"[probe] {self.tv.get('name', ip)} answered directly on port {port}")
        if self.start_when_found:
            if port:
                self.start_when_found = False
                self.active = False
                self.start()
            else:
                self._probe()  # keep trying quietly until it answers or Cancel

    # -- start / stop -----------------------------------------------------
    def _options(self) -> dict:
        opts: dict = dict(QUALITY.get(self.settings["quality"], QUALITY["Auto"]))
        opts["hwaccel"] = self.settings["encoder"]
        index = self._display_index()
        if index is not None:
            opts["display"] = index
        if self.audio_capable:
            opts["audio"] = bool(self.settings["sound"])
        return opts

    def start(self) -> None:
        if not self.tv or not self.sender or not self.sender.alive:
            return
        self.target = self.tv["ip"]
        self.target_name = self.tv.get("name", "")
        self.shown_errors.clear()
        self._error_text = None
        self.active = True
        self.render()
        # Always the discovered port: the Samsung's changes on every reboot, and it's never 7000.
        port = self._tv_port()
        if port is None:
            self.start_when_found = True
            self._probe()
            return
        opts, target = self._options(), self.target
        self._bg(lambda: self.sender.connect(target, port, opts), self._connect_reply)

    def _connect_reply(self, reply) -> None:
        if not reply.get("ok", True):
            kind = reply.get("error_kind", "failed")
            self._log(f"[connect refused: {kind}]")
            self._error_text = error_text(kind, self.target_name)
            self.active = False
            self.render()

    def stop(self) -> None:
        if self.start_when_found:
            self.start_when_found = False
            self.active = False
            self.render()
            return
        target = self.target
        self._bg(lambda: self.sender.disconnect(target))

    # -- state handling ---------------------------------------------------
    def _on_state(self, msg: dict) -> None:
        self.last_state = msg.get("state", "idle")
        mine = next(
            (s for s in msg.get("streams") or [] if s.get("device_ip") == self.target), None
        )
        if mine and mine.get("device"):
            self.target_name = mine["device"]
        self._set_streaming(bool(mine and mine.get("state") == "streaming"))

        if mine and mine.get("state") == "pin_required":
            self._show_pin_dialog(mine.get("credential_kind", "pin"))
        elif self.pin_dialog:
            self._close_pin_dialog()

        for err in msg.get("errors") or []:
            key = (err.get("target", ""), err.get("kind", ""))
            if err.get("target") == self.target and key not in self.shown_errors:
                self.shown_errors.add(key)
                self._log(f"[error kind={key[1]}] {err.get('message', '')}")
                self._error_text = error_text(key[1], err.get("device") or self.target_name)
                if key[1] == "unreachable":
                    self.probed = None  # the port may have changed; scan again next time
        if mine is not None:
            self.active = True
            if not mine.get("audio_error"):
                self._error_text = None
        elif self.target is not None:
            self.active = False
        self.render()

    def _set_streaming(self, value: bool) -> None:
        if value and not self.streaming:
            self.streaming = True
            self.stream_started = time.monotonic()
            self._remember_tv()
            self._add_recent_tv()
            self.on_streaming_started()
            self.root.after(1000, self._tick)
        elif not value and self.streaming:
            self.streaming = False
            self.on_streaming_stopped()

    def _tick(self) -> None:
        if self.streaming and not self.closing:
            self.render()
            self.root.after(1000, self._tick)

    # -- PIN dialog -------------------------------------------------------
    def _show_pin_dialog(self, kind: str) -> None:
        if self.pin_dialog:
            return
        c = self.c
        what = "password" if kind == "password" else "code"
        dlg = tk.Toplevel(self.root, bg=c["bg"], padx=24, pady=20)
        dlg.title("Enter code")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        tk.Label(
            dlg,
            text=f"Enter the {what} shown on {self.target_name or 'the TV'}",
            bg=c["bg"],
            fg=c["text"],
            font=("Segoe UI", 11),
        ).pack(pady=(0, 12))
        entry = tk.Entry(
            dlg,
            width=8,
            justify="center",
            font=("Segoe UI", 20),
            bg=c["card"],
            fg=c["text"],
            insertbackground=c["text"],
            relief="flat",
            show="*" if kind == "password" else "",
        )
        entry.pack(ipady=6, pady=(0, 16))
        entry.focus_set()
        target = self.target

        def ok(_e=None):
            pin = entry.get().strip()
            if not pin:
                return
            self._close_pin_dialog()
            self._bg(lambda: self.sender.submit_pin(target, pin))

        def cancel():
            self._close_pin_dialog()
            self.stop()

        buttons = tk.Frame(dlg, bg=c["bg"])
        buttons.pack(fill="x")
        for text, command, bg, fg in (
            ("Connect", ok, c["accent"], c["on_accent"]),
            ("Cancel", cancel, c["card"], c["text"]),
        ):
            tk.Button(
                buttons,
                text=text,
                command=command,
                bg=bg,
                fg=fg,
                activebackground=bg,
                activeforeground=fg,
                relief="flat",
                font=("Segoe UI", 10),
                padx=16,
                pady=6,
                cursor="hand2",
            ).pack(side="left", expand=True, fill="x", padx=4)
        entry.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda _e: cancel())
        dlg.protocol("WM_DELETE_WINDOW", cancel)
        ui.match_title_bar(dlg, self.dark)
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + self.px(120)
        dlg.geometry(f"+{x}+{y}")
        self.pin_dialog = dlg

    def _close_pin_dialog(self) -> None:
        if self.pin_dialog:
            self.pin_dialog.destroy()
            self.pin_dialog = None

    # -- shutdown ---------------------------------------------------------
    def close(self) -> None:
        self.closing = True
        self._close_pin_dialog()
        self._set_streaming(False)
        sender, self.sender = self.sender, None
        if sender:
            self._shutdown(sender)
        self.root.destroy()


def main() -> None:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
    root = tk.Tk()
    App(root)
    root.mainloop()
