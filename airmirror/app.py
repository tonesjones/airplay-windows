"""AirMirror window: pick a TV, press Start, see the desktop on the TV.

The Quiet controller keeps TV and screen selection up front, with less-used options
behind a disclosure. Standard tkinter controls support mouse and keyboard use.
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
from tkinter import simpledialog
from typing import NamedTuple

from . import keepawake, probe, ui
from .icon import ICON_PATH
from .pcmute import SpeakerMute
from .sender import SenderError, SenderProcess, list_audio_devices

QUALITY = {
    "Auto": {"fps": 0, "bitrate_kbps": 0},
    "Smooth": {"fps": 30, "bitrate_kbps": 8000},
    "Weak Wi-Fi": {"fps": 30, "bitrate_kbps": 3000},
}
ENCODERS = {"nvenc": "NVENC", "auto": "Auto", "none": "Software"}
MENU_TILES = ("quality", "encoder", "display")
DEFAULT_AUDIO = "Default output"
REFRESH_MS = 5000
HINT_MS = 4000
PROBE_RETRY_S = 15  # wait between direct scans of a TV that mDNS doesn't list
MAX_LOG_LINES = 1000
MAX_RECENT_TVS = 5
WIDTH = 400
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
    "lock": "",
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


def display_names(displays: list[dict]) -> dict[int, str]:
    """Names each display by where it sits next to the main one: Main, Left, Right, Above, Below."""
    main = next((d for d in displays if d.get("primary")), None)

    def centre(d):
        return d.get("x", 0) + d.get("width", 0) / 2, d.get("y", 0) + d.get("height", 0) / 2

    names = {}
    for d in displays:
        index = d.get("index", 0)
        if d is main:
            names[index] = "Main"
        elif main is None:
            names[index] = f"Screen {index + 1}"
        else:
            (x, y), (mx, my) = centre(d), centre(main)
            if abs(x - mx) >= abs(y - my):
                names[index] = "Right" if x > mx else "Left"
            else:
                names[index] = "Below" if y > my else "Above"
    taken = list(names.values())
    return {i: f"Screen {i + 1}" if taken.count(n) > 1 else n for i, n in names.items()}


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
        self.probing = False
        self.last_probe = 0.0
        self.probed: tuple[str, int] | None = None  # (ip, port) a direct scan found
        self.start_when_found = False
        self.connect_started = 0.0
        self.switching: str | None = None  # "stopping", then "starting", while changing screens
        self.hint: str | None = None
        self.hint_id: str | None = None
        self.details_open = False
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
        self._build_ui()

        root.title("AirMirror")
        root.iconbitmap(default=str(ICON_PATH))
        root.configure(bg=self.c["bg"])
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        ui.match_title_bar(root, self.dark)
        ui.set_window_icon(root, str(ICON_PATH))
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

    # -- layout -----------------------------------------------------------
    def px(self, n: float) -> int:
        return round(n * self.scale)

    def _button(self, parent, text, command, **kwargs) -> tk.Button:
        c = self.c
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Segoe UI", 10),
            bg=c["card"],
            fg=c["text"],
            activebackground=c["card_hover"],
            activeforeground=c["text"],
            disabledforeground=c["muted"],
            relief="flat",
            bd=0,
            padx=self.px(10),
            pady=self.px(8),
            highlightthickness=2,
            highlightbackground=c["bg"],
            highlightcolor=c["accent"],
            cursor="hand2",
            **kwargs,
        )

    def _label(self, parent, text, size=10, muted=False, bold=False) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            font=("Segoe UI", size, "bold" if bold else "normal"),
            bg=self.c["bg"],
            fg=self.c["muted"] if muted else self.c["text"],
            anchor="w",
            justify="left",
            wraplength=self.px(WIDTH - 52),
        )

    def _build_ui(self) -> None:
        c = self.c
        self.panel = tk.Frame(self.root, bg=c["bg"], padx=self.px(24), pady=self.px(20))
        self.panel.pack(fill="both", expand=True)
        self.panel.columnconfigure(0, weight=1, minsize=self.px(WIDTH - 48))
        self._label(self.panel, "AirMirror", 11, bold=True).grid(sticky="w")
        self._label(self.panel, "Your desktop. On TV.", 20, bold=True).grid(
            sticky="w", pady=(self.px(18), self.px(4))
        )
        self._label(self.panel, "Pick a screen and start mirroring.", muted=True).grid(
            sticky="w", pady=(0, self.px(20))
        )
        self._label(self.panel, "Mirror to", 9, muted=True).grid(sticky="w")
        self.tv_button = self._button(
            self.panel,
            "Looking for TVs…",
            self._open_tv_menu,
            anchor="w",
            wraplength=self.px(WIDTH - 80),
        )
        self.tv_button.grid(sticky="ew", pady=(self.px(4), self.px(10)))
        self.controls = {}
        screen_row = tk.Frame(self.panel, bg=c["bg"])
        screen_row.grid(sticky="ew")
        screen_row.columnconfigure(1, weight=1)
        self._label(screen_row, "Screen").grid(row=0, column=0, sticky="w")
        self.controls["display"] = self._button(
            screen_row, "Main", lambda: self._press_tile("display", None), anchor="e"
        )
        self.controls["display"].grid(row=0, column=1, sticky="e")
        self.toggle_vars = {}
        for key, title in (
            ("sound", "TV sound"),
            ("mute_pc", "Mute PC while mirroring"),
            ("keep_awake", "Keep screen awake"),
        ):
            var = tk.BooleanVar(self.root)
            self.toggle_vars[key] = var
            control = tk.Checkbutton(
                self.panel,
                text=title,
                variable=var,
                command=lambda k=key: self._press_tile(k, None),
                font=("Segoe UI", 10),
                bg=c["bg"],
                fg=c["text"],
                activebackground=c["bg"],
                activeforeground=c["text"],
                selectcolor=c["card"],
                disabledforeground=c["muted"],
                anchor="w",
                padx=0,
                pady=self.px(5),
                highlightthickness=2,
                highlightbackground=c["bg"],
                highlightcolor=c["accent"],
                cursor="hand2",
            )
            control.grid(sticky="ew", pady=(self.px(4), 0))
            self.controls[key] = control
        self.details_button = self._button(
            self.panel, "Quality & audio settings  ▸", self._toggle_details, anchor="w"
        )
        self.details_button.configure(bg=c["bg"], fg=c["muted"])
        self.details_button.grid(sticky="ew", pady=(self.px(12), 0))
        self.details = tk.Frame(self.panel, bg=c["bg"])
        self.details.grid(sticky="ew")
        self.details.columnconfigure(1, weight=1)
        for row, (key, title) in enumerate(
            (("quality", "Quality"), ("encoder", "Encoder"), ("audio", "Audio source"))
        ):
            self._label(self.details, title).grid(row=row, column=0, sticky="w")
            command = (
                self._open_sound_menu if key == "audio" else lambda k=key: self._press_tile(k, None)
            )
            self.controls[key] = self._button(
                self.details, "", command, anchor="e", wraplength=self.px(205)
            )
            self.controls[key].grid(row=row, column=1, sticky="e", pady=self.px(3))
        self.start_button = self._button(self.panel, "Start mirroring", self._press_start)
        self.start_button.configure(font=("Segoe UI", 11, "bold"), pady=self.px(11))
        self.start_button.grid(sticky="ew", pady=(self.px(20), self.px(10)))
        self.status_label = self._label(self.panel, "Starting…", 9, muted=True)
        self.status_label.grid(sticky="ew")
        self.summary_label = self._label(self.panel, "", 9, muted=True)
        self.summary_label.grid(sticky="ew")
        tk.Frame(self.panel, bg=c["line"], height=1).grid(
            sticky="ew", pady=(self.px(14), self.px(10))
        )
        footer = tk.Frame(self.panel, bg=c["bg"])
        footer.grid(sticky="ew")
        footer.columnconfigure(0, weight=1)
        self._label(footer, "Whole desktop and audio", 9, muted=True).grid(sticky="w")
        self.log_button = self._button(footer, "Open log", self._show_log)
        self.log_button.configure(bg=c["bg"], fg=c["muted"], font=("Segoe UI", 9))
        self.log_button.grid(row=0, column=1, sticky="e")
        # Reserve the details row before hiding it so expansion cannot overlap Start.
        self.details.grid_remove()

    def _open_tv_menu(self) -> None:
        if not self.active:
            self._popup(None, self._tv_menu(), self.tv_button)

    def _open_sound_menu(self) -> None:
        if self.audio_capable and not self.active:
            self._popup(None, self._sound_menu(), self.controls["audio"])

    def _toggle_details(self) -> None:
        self.details_open = not self.details_open
        if self.details_open:
            self.details.grid()
        else:
            self.details.grid_remove()
        self.details_button.configure(
            text="Quality & audio settings  " + ("▾" if self.details_open else "▸")
        )

    def render(self) -> None:
        c = self.c
        name = (
            self.tv.get("name", "TV")
            if self.tv
            else "No TV found yet"
            if self.searched
            else "Looking for TVs…"
        )
        self.tv_button.configure(text=name + "  ▾", state="disabled" if self.active else "normal")
        for tile in self._tiles():
            control = self.controls[tile.key]
            control.configure(state="normal" if tile.enabled else "disabled")
            if tile.key in self.toggle_vars:
                self.toggle_vars[tile.key].set(tile.on)
            else:
                value = tile.sub
                if tile.key == "display":
                    value = f"Screen {(self._display_index() or 0) + 1} — {value}"
                control.configure(text=value + "  ▾")
        audio_name = next(
            (d["name"] for d in self.audio_devices if d["id"] == self.settings["audio_id"]),
            DEFAULT_AUDIO,
        )
        self.controls["audio"].configure(
            text=audio_name + "  ▾",
            state="normal" if self.audio_capable and not self.active else "disabled",
        )
        if self.sender_dead:
            title = "Restart sender"
        elif self.active or self.switching:
            title = "Stop mirroring" if self.streaming and not self.switching else "Cancel"
        else:
            title = "Start mirroring"
        stopping = self.streaming and not self.switching
        self.start_button.configure(
            text=title,
            bg=c["card"] if stopping else c["accent"],
            fg=c["red"] if stopping else c["on_accent"],
            activebackground=c["card_hover"] if stopping else c["accent_hover"],
            activeforeground=c["red"] if stopping else c["on_accent"],
            state="normal" if self.tv or self.active or self.sender_dead else "disabled",
        )
        text, color = self._status()
        self.status_label.configure(text=text, fg=color)
        self.summary_label.configure(text=self._stream_summary() if self.streaming else "")

    @staticmethod
    def _elapsed(since: float) -> str:
        secs = int(time.monotonic() - since)
        if secs >= 3600:
            return f"{secs // 3600}:{secs // 60 % 60:02}:{secs % 60:02}"
        return f"{secs // 60}:{secs % 60:02}"

    def _stream_summary(self) -> str:
        parts = [self._elapsed(self.stream_started)]
        if len(self.displays) > 1:
            parts.append(f"{self._display_label()} screen")
        if self.sender and self.sender.resolution:
            parts.append("{}×{}".format(*self.sender.resolution))
        parts.append("sound on" if self.settings["sound"] and self.audio_capable else "no sound")
        return "  /  ".join(parts)

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
            Tile(
                "display",
                GLYPH["display"],
                "Screen",
                self._display_label(),
                False,
                not locked or self._can_switch_display(),
            ),
        ]

    def _can_switch_display(self) -> bool:
        return self.streaming and not self.switching

    def _locked(self, key: str, enabled: bool) -> bool:
        """Disabled only until mirroring stops (TV sound without audio support never unlocks)."""
        return not enabled and self.active and (key != "sound" or self.audio_capable)

    def _display_label(self) -> str:
        index = self._display_index()
        if index is None:
            return "Main"
        return display_names(self.displays).get(index, "Main")

    def _status(self) -> tuple[str, str]:
        c = self.c
        if self.hint:
            return self.hint, c["accent"]
        if self._error_text and not self.streaming:
            return self._error_text, c["red"]
        if self.switching and not self.pin_dialog:
            return f"Switching to the {self._display_label()} screen…", c["accent"]
        if self.streaming:
            text = "Mirroring"
            if self.sender and self.sender.resolution:
                text += " {}×{}".format(*self.sender.resolution)
            return text + (
                " with sound" if self.settings["sound"] and self.audio_capable else ""
            ), c["green"]
        if self.pin_dialog:
            return f"Enter the code shown on {self.target_name or 'the TV'}.", c["accent"]
        if self.active and self.start_when_found:
            return "After the TV restarts, it can take a few minutes to answer.", c["accent"]
        if self.active:
            return f"Connecting to {self.target_name or 'the TV'}…", c["accent"]
        if self.last_state == "starting":
            return "Starting…", c["muted"]
        if not self.tv and self.searched:
            return "No TV found. Is it on? Click the TV box for options.", c["muted"]
        if not self.tv:
            return "Looking for AirPlay TVs on your network…", c["muted"]
        return "Ready. Your TV may show a pairing code.", c["muted"]

    def _press_start(self) -> None:
        if self.sender_dead:
            self._restart_sender()
        elif self.active or self.switching:
            self.stop()
        elif self.tv:
            self.start()

    def _press_tile(self, key: str, event) -> None:
        tile = next(t for t in self._tiles() if t.key == key)
        if self._locked(key, tile.enabled):
            self._show_hint(f"Stop mirroring to change {tile.title}.")
        if not tile.enabled:
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
            self._popup(
                event,
                self._choice_menu("quality", [(q, q) for q in QUALITY]),
                self.controls[key],
            )
        elif key == "encoder":
            self._popup(
                event, self._choice_menu("encoder", list(ENCODERS.items())), self.controls[key]
            )
        elif key == "display":
            self._popup(event, self._display_menu(), self.controls[key])

    def _show_hint(self, text: str) -> None:
        if self.hint_id:
            self.root.after_cancel(self.hint_id)
        self.hint = text
        self.hint_id = self.root.after(HINT_MS, self._clear_hint)
        self.render()

    def _clear_hint(self) -> None:
        self.hint = self.hint_id = None
        self.render()

    # -- menus ------------------------------------------------------------
    def _menu(self) -> tk.Menu:
        c = self.c
        menu = tk.Menu(
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
        menu.marks = []  # the check mark variables must outlive this call
        return menu

    @staticmethod
    def _option(menu: tk.Menu, label: str, chosen: bool, command, detail: str = "") -> None:
        """A menu item with a check mark when chosen and detail right-aligned."""
        mark = tk.BooleanVar(menu, chosen)
        menu.marks.append(mark)
        menu.add_checkbutton(label=label, accelerator=detail, variable=mark, command=command)

    def _heading(self, menu: tk.Menu, label: str) -> None:
        # Not state="disabled": Windows draws disabled menu text etched, which looks broken
        # on the dark theme. This entry doesn't highlight and does nothing when clicked.
        c = self.c
        menu.add_command(
            label=label,
            foreground=c["muted"],
            activeforeground=c["muted"],
            activebackground=c["card"],
            font=("Segoe UI", 9),
        )

    def _popup(self, event, menu: tk.Menu, anchor=None) -> None:
        if event is None:
            anchor = anchor or self.root.focus_get() or self.details_button
            x, y = anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height()
        else:
            x, y = event.x_root, event.y_root
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _choice_menu(self, key: str, choices: list[tuple]) -> tk.Menu:
        menu = self._menu()
        for value, label in choices:
            chosen = value == self.settings[key]
            self._option(menu, label, chosen, lambda v=value: self._set(**{key: v}))
        return menu

    def _display_menu(self) -> tk.Menu:
        menu = self._menu()
        if not self.displays:
            self._heading(menu, "No screens found yet")
        names, current = display_names(self.displays), self._display_index()
        for d in self.displays:
            index = d.get("index")
            self._option(
                menu,
                f"{names[index]} screen",
                index == current,
                lambda i=index: self.choose_display(i),
                f"{d.get('width')}×{d.get('height')}",
            )
        menu.add_separator()
        menu.add_command(label="Find screens again", command=self.refresh_displays)
        return menu

    def _tv_menu(self) -> tk.Menu:
        menu = self._menu()
        self._heading(menu, "On your network")
        if not self.receivers:
            self._heading(menu, "    None found right now" if self.searched else "    Searching…")
        current = tv_id(self.tv) if self.tv else None
        for d in self.receivers:
            self._option(
                menu,
                d.get("name", "TV"),
                tv_id(d) == current,
                lambda d=d: self._choose_tv(d),
                d.get("ip", ""),
            )
        listed = {d.get("ip") for d in self.receivers}
        recent = [t for t in self.settings["recent_tvs"] if t["ip"] not in listed]
        if recent:
            menu.add_separator()
            self._heading(menu, "Worked before")
            current_ip = self.tv["ip"] if self.tv else None
            for t in recent:
                self._option(
                    menu,
                    t.get("name") or "TV",
                    t["ip"] == current_ip,
                    lambda t=t: self._use_unlisted_tv(dict(t)),
                    t["ip"],
                )
        menu.add_separator()
        menu.add_command(label="Search again", command=self.refresh_receivers)
        menu.add_command(label="Type the TV's IP address…", command=self._ask_tv_ip)
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
            chosen = value == self.settings["audio_id"]
            self._option(menu, label, chosen, lambda v=value: self._choose_audio(v))
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
        self._cancel_switch()
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

    def _bg(
        self,
        fn: Callable[[], object],
        done: Callable[[object], None] | None = None,
        quiet: bool = False,
    ) -> None:
        """Runs fn off the UI thread. A quiet failure only goes to the log."""
        sender, gen = self.sender, self.generation

        def work():
            try:
                result = fn()
            except SenderError as exc:
                event = (gen, "log", f"[error] {exc}") if quiet else (gen, "error", str(exc))
                self.events.put(event)
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
            self._cancel_switch()
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
            self._bg(list_audio_devices, self._set_audio_devices, quiet=True)
        if self.last_state == "starting":
            self.last_state = "idle"
        self.refresh_receivers()
        self.refresh_displays()
        self.render()

    def _set_audio_devices(self, devices) -> None:
        self.audio_devices = devices

    def refresh_displays(self) -> None:
        """Reads the screen list again: a monitor can wake up or be plugged in later."""
        if self.sender and self.sender.alive:
            self._bg(lambda: self.sender.displays(), self._set_displays, quiet=True)

    def _set_displays(self, displays) -> None:
        if displays != self.displays:
            self.displays = displays
            self.render()

    def choose_display(self, index: int) -> None:
        """Picks a screen. While mirroring, reconnects to show it; the TV keeps the pairing."""
        if index == self._display_index():
            return
        self._set(display=index)
        if self._can_switch_display():
            self.switching = "stopping"
            target = self.target
            self._bg(lambda: self.sender.disconnect(target))
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
        self.refresh_displays()
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
        self.connect_started = time.monotonic()
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
        self._cancel_switch()
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
            if self.switching == "stopping":
                self.switching = "starting"
                self.start()  # the old stream has ended; connect again with the new screen
            else:
                self._cancel_switch()
        if self.switching == "starting" and self.streaming:
            self.switching = None
        self.render()

    def _cancel_switch(self) -> None:
        if self.switching:
            self.switching = None
            if not self.streaming:
                self.on_streaming_stopped()

    def _set_streaming(self, value: bool) -> None:
        if value and not self.streaming:
            self.streaming = True
            if not self.switching:
                self.stream_started = time.monotonic()
            self._remember_tv()
            self._add_recent_tv()
            self.on_streaming_started()
            self.root.after(1000, self._tick)
        elif not value and self.streaming:
            self.streaming = False
            if not self.switching:  # PC mute and keep-awake stay on through a screen switch
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
            highlightthickness=self.px(2),
            highlightbackground=c["line"],
            highlightcolor=c["accent"],
            show="*" if kind == "password" else "",
        )
        if kind != "password":
            digits_only = dlg.register(lambda text: text.isdigit() and len(text) <= 8 or not text)
            entry.configure(validate="key", validatecommand=(digits_only, "%P"))
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
        self._cancel_switch()
        self._close_pin_dialog()
        self._set_streaming(False)
        sender, self.sender = self.sender, None
        if sender:
            self._shutdown(sender)
        self.root.destroy()


def main() -> None:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
    # Its own taskbar button and icon, instead of grouping with other Python windows.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AirMirror")
    root = tk.Tk()
    App(root)
    root.mainloop()
