"""tkinter window: pick a receiver and a display, press Start, see the desktop on the TV."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk

from .sender import SenderError, SenderProcess, list_audio_devices

QUALITY = {
    "Auto": {"fps": 0, "bitrate_kbps": 0},
    "Smooth": {"fps": 30, "bitrate_kbps": 8000},
    "Weak Wi-Fi": {"fps": 30, "bitrate_kbps": 3000},
}
ENCODERS = ("auto", "nvenc", "none")
DEFAULT_AUDIO = "Default output"
REFRESH_MS = 5000
MAX_LOG_LINES = 1000

# User-facing wording per error kind (from the protocol doc). Raw messages are never shown.
ERROR_TEXT = {
    "busy": "Already mirroring to {tv}.",
    "unreachable": "{tv} didn't answer. Check it's on and on the same network.",
    "wrong_code": "That code didn't work. Press Start to get a new one.",
    "rejected": "{tv} didn't accept this PC.",
    "capture_failed": "Couldn't capture this PC's screen.",
    "ended": "Mirroring to {tv} ended.",
    "failed": "Couldn't mirror to {tv}.",
}
GENERIC_ERROR = "Couldn't mirror to {tv}."


def error_text(kind: str, tv: str) -> str:
    return ERROR_TEXT.get(kind, GENERIC_ERROR).format(tv=tv or "the TV")


class App:
    def __init__(self, root: tk.Tk, argv: Sequence[str] | None = None, auto_refresh: bool = True):
        self.root = root
        self.argv = argv  # test override for the sender command
        self.events: queue.Queue = queue.Queue()
        self.sender: SenderProcess | None = None
        self.generation = 0
        self.audio_id = ""  # "" means follow the default output device
        self.audio_devices: list[dict] = []
        self.receivers: list[dict] = []
        self.displays: list[dict] = []
        self.target: str | None = None  # ip we last pressed Start for
        self.target_name = ""
        self.streaming = False
        self.pin_dialog: tk.Toplevel | None = None
        self.shown_errors: set[tuple[str, str]] = set()
        self.last_state = "starting"
        self.closing = False
        self._error_text: str | None = None

        root.title("AirMirror")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._start_sender()
        root.after(100, self._pump)
        if auto_refresh:
            root.after(REFRESH_MS, self._auto_refresh)

    # -- hooks for the main session --------------------------------------
    def on_streaming_started(self) -> None:
        """Called on the transition into streaming."""

    def on_streaming_stopped(self) -> None:
        """Called on the transition out of streaming."""

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        f = ttk.Frame(self.root, padding=10)
        f.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Receiver").grid(row=0, column=0, sticky="nw")
        self.receiver_list = tk.Listbox(f, height=5, exportselection=False, width=44)
        self.receiver_list.grid(row=0, column=1, sticky="ew")
        self.refresh_btn = ttk.Button(f, text="Refresh", command=self.refresh_receivers)
        self.refresh_btn.grid(row=0, column=2, padx=(6, 0), sticky="n")

        self.display_var = tk.StringVar()
        self.quality_var = tk.StringVar(value="Auto")
        self.encoder_var = tk.StringVar(value="auto")
        self.sound_var = tk.BooleanVar(value=True)
        self.audio_var = tk.StringVar(value=DEFAULT_AUDIO)

        def row(n: int, label: str, var: tk.StringVar, values: Sequence[str]):
            ttk.Label(f, text=label).grid(row=n, column=0, sticky="w", pady=3)
            box = ttk.Combobox(f, textvariable=var, values=list(values), state="readonly")
            box.grid(row=n, column=1, sticky="ew", pady=3)
            return box

        self.display_box = row(1, "Display", self.display_var, [])
        self.quality_box = row(2, "Quality", self.quality_var, list(QUALITY))
        self.encoder_box = row(3, "Encoder", self.encoder_var, ENCODERS)

        # Sound controls stay hidden until hello says this build can capture audio.
        self.sound_frame = ttk.Frame(f)
        self.sound_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(self.sound_frame, text="Sound", variable=self.sound_var).grid(
            row=0, column=0, sticky="w"
        )
        self.audio_box = ttk.Combobox(
            self.sound_frame, textvariable=self.audio_var, values=[DEFAULT_AUDIO], state="readonly"
        )
        self.audio_box.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.audio_box.bind("<<ComboboxSelected>>", self._audio_changed)
        self.sound_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=3)
        self.sound_frame.grid_remove()

        buttons = ttk.Frame(f)
        buttons.grid(row=5, column=0, columnspan=3, pady=8, sticky="w")
        self.start_btn = ttk.Button(buttons, text="Start", command=self.start)
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=6)

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(f, textvariable=self.status_var, wraplength=480).grid(
            row=6, column=0, columnspan=3, sticky="w"
        )

        self.log_visible = False
        self.log_btn = ttk.Button(f, text="Show log", command=self._toggle_log)
        self.log_btn.grid(row=7, column=0, sticky="w", pady=(8, 0))
        self.log_text = tk.Text(f, height=10, width=60, state="disabled", wrap="none")
        self.log_text.grid(row=8, column=0, columnspan=3, sticky="nsew")
        self.log_text.grid_remove()

    def _toggle_log(self) -> None:
        self.log_visible = not self.log_visible
        if self.log_visible:
            self.log_text.grid()
            self.log_btn.configure(text="Hide log")
        else:
            self.log_text.grid_remove()
            self.log_btn.configure(text="Show log")

    def _log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        count = int(self.log_text.index("end-1c").split(".")[0])
        if count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{count - MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- sender lifecycle -------------------------------------------------
    def _start_sender(self) -> None:
        self.generation += 1
        gen = self.generation
        post = self.events.put
        extra = ["-audio-device", self.audio_id] if self.audio_id else []
        try:
            sender = SenderProcess(
                extra,
                argv=self.argv,
                on_state=lambda m: post((gen, "state", m)),
                on_log=lambda s: post((gen, "log", s)),
                on_exit=lambda c: post((gen, "exit", c)),
            )
        except SenderError as exc:
            self.status_var.set(f"Can't start the sender. Run setup.ps1 first. ({exc})")
            return
        self.sender = sender

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
        self.target = None
        if old:
            threading.Thread(target=self._shutdown, args=(old,), daemon=True).start()
        self._start_sender()

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
            self._update_status()
        elif kind == "hello":
            self._on_hello(data)
        elif kind == "state":
            self._on_state(data)
        elif kind == "call":
            data()
        elif kind == "error":
            self._log(f"[error] {data}")
            self.status_var.set("Lost contact with the sender. Try Start again.")
        elif kind == "exit":
            self._log(f"[sender exited with code {data}]")
            self._set_streaming(False)
            self.last_state = "stopped"
            self.status_var.set("The sender stopped. Close and reopen AirMirror.")
            self._buttons(starting=False, active=False)
            self.start_btn.configure(state="disabled")

    def _on_hello(self, caps: dict) -> None:
        if caps.get("audio"):
            self.sound_frame.grid()
            if not self.audio_devices:
                self._bg(list_audio_devices, self._set_audio_devices)
        else:
            self.sound_frame.grid_remove()
        self.refresh_receivers()
        self._bg(lambda: self.sender.displays(), self._set_displays)

    def _set_audio_devices(self, devices) -> None:
        self.audio_devices = devices
        self.audio_box.configure(values=[DEFAULT_AUDIO, *[d["name"] for d in devices]])

    def _audio_changed(self, _event=None) -> None:
        name = self.audio_var.get()
        new_id = next((d["id"] for d in self.audio_devices if d["name"] == name), "")
        if new_id != self.audio_id:
            self.audio_id = new_id
            self.status_var.set("Switching sound device...")
            self._restart_sender()

    def _set_displays(self, displays) -> None:
        self.displays = displays
        names = [
            f"{d.get('index')}: {d.get('name') or 'Display'} "
            f"({d.get('width')}x{d.get('height')})" + (" primary" if d.get("primary") else "")
            for d in displays
        ]
        self.display_box.configure(values=names)
        if names and self.display_var.get() not in names:
            primary = next((i for i, d in enumerate(displays) if d.get("primary")), 0)
            self.display_var.set(names[primary])

    # -- receivers --------------------------------------------------------
    def refresh_receivers(self) -> None:
        if self.sender and self.sender.alive:
            self._bg(lambda: self.sender.devices(), self._set_receivers)

    def _auto_refresh(self) -> None:
        if self.closing:
            return
        if not self.streaming:
            self.refresh_receivers()
        self.root.after(REFRESH_MS, self._auto_refresh)

    def _set_receivers(self, devices) -> None:
        sel = self._selected_receiver()
        keep = sel["ip"] if sel else None
        self.receivers = devices
        self.receiver_list.delete(0, "end")
        for i, d in enumerate(devices):
            self.receiver_list.insert("end", f"{d.get('name', '?')}  ({d.get('ip')})")
            if d.get("ip") == keep:
                self.receiver_list.selection_set(i)
        if keep is None and len(devices) == 1:
            self.receiver_list.selection_set(0)

    def _selected_receiver(self) -> dict | None:
        idx = self.receiver_list.curselection()
        if idx and idx[0] < len(self.receivers):
            return self.receivers[idx[0]]
        return None

    # -- start / stop -----------------------------------------------------
    def _options(self) -> dict:
        opts: dict = dict(QUALITY.get(self.quality_var.get(), QUALITY["Auto"]))
        opts["hwaccel"] = self.encoder_var.get()
        text = self.display_var.get()
        if text:
            opts["display"] = int(text.split(":")[0])
        if self.sender and self.sender.capabilities.get("audio"):
            opts["audio"] = bool(self.sound_var.get())
        return opts

    def start(self) -> None:
        rec = self._selected_receiver()
        if not rec:
            self.status_var.set("Pick a receiver first.")
            return
        if not self.sender or not self.sender.alive:
            return
        self.target = rec["ip"]
        self.target_name = rec.get("name", "")
        self.shown_errors.clear()
        self._error_text = None
        port = rec.get("port") or None  # always the discovered port; TVs often aren't on 7000
        opts, target = self._options(), self.target
        self.status_var.set(f"Connecting to {self.target_name}...")
        self._buttons(starting=True, active=True)
        self._bg(lambda: self.sender.connect(target, port, opts), self._connect_reply)

    def _connect_reply(self, reply) -> None:
        if not reply.get("ok", True):
            kind = reply.get("error_kind", "failed")
            self._log(f"[connect refused: {kind}]")
            self.status_var.set(error_text(kind, self.target_name))
            self._buttons(starting=False, active=False)

    def stop(self) -> None:
        target = self.target
        self.status_var.set("Stopping...")
        self._bg(lambda: self.sender.disconnect(target))

    def _buttons(self, starting: bool, active: bool) -> None:
        self.start_btn.configure(state="disabled" if active else "normal")
        self.stop_btn.configure(state="normal" if active else "disabled")
        for w in (self.receiver_list, self.display_box, self.quality_box, self.encoder_box):
            w.configure(
                state="disabled"
                if active
                else ("readonly" if w is not self.receiver_list else "normal")
            )

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

        text = None
        for err in msg.get("errors") or []:
            key = (err.get("target", ""), err.get("kind", ""))
            if err.get("target") == self.target and key not in self.shown_errors:
                self.shown_errors.add(key)
                self._log(f"[error kind={key[1]}] {err.get('message', '')}")
                text = error_text(key[1], err.get("device") or self.target_name)
        if mine is None and self.target is not None:
            self._buttons(starting=False, active=False)
        elif mine is not None:
            self._buttons(starting=False, active=True)
        if text:
            self._error_text = text
        elif mine is not None:
            self._error_text = None
        self._update_status(mine)

    def _update_status(self, mine: dict | None = None) -> None:
        if self.last_state == "stopped":
            return
        err = self._error_text
        state = self.last_state
        parts = []
        if err and (mine is None):
            parts.append(err)
        parts.append(f"State: {state}")
        if self.target_name and self.target is not None:
            parts.append(f"Device: {self.target_name}")
        if self.sender and self.sender.resolution and self.streaming:
            w, h = self.sender.resolution
            parts.append(f"{w}x{h}")
        if mine and mine.get("audio_error"):
            parts.append("No sound")
        self.status_var.set("  |  ".join(parts))

    def _set_streaming(self, value: bool) -> None:
        if value and not self.streaming:
            self.streaming = True
            self.on_streaming_started()
        elif not value and self.streaming:
            self.streaming = False
            self.on_streaming_stopped()

    # -- PIN dialog -------------------------------------------------------
    def _show_pin_dialog(self, kind: str) -> None:
        if self.pin_dialog:
            return
        what = "password" if kind == "password" else "code"
        dlg = tk.Toplevel(self.root)
        dlg.title("Enter code")
        dlg.transient(self.root)
        ttk.Label(dlg, text=f"Enter the {what} shown on {self.target_name or 'the TV'}:").grid(
            row=0, column=0, columnspan=2, padx=12, pady=(12, 4)
        )
        entry = ttk.Entry(dlg, show="*", width=16)
        entry.grid(row=1, column=0, columnspan=2, padx=12, pady=4)
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

        ttk.Button(dlg, text="OK", command=ok).grid(row=2, column=0, pady=12)
        ttk.Button(dlg, text="Cancel", command=cancel).grid(row=2, column=1, pady=12)
        entry.bind("<Return>", ok)
        dlg.protocol("WM_DELETE_WINDOW", cancel)
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
    root = tk.Tk()
    App(root)
    root.mainloop()
