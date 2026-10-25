"""Client for open-airplay's stdio control protocol (see docs/control-protocol.md)."""

from __future__ import annotations

import itertools
import json
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from typing import Any

from . import SENDER_EXE

_RESOLUTION_RE = re.compile(r"screen capture started at (\d+)x(\d+)")
_AUDIO_NAME_RE = re.compile(r"^(\*?)\s*\d+\.\s+(.*\S)\s*$")
_AUDIO_ID_RE = re.compile(r"^\s+id:\s*(\S.*?)\s*$")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class SenderError(RuntimeError):
    """The sender process is gone, silent, or refused a request."""


def parse_audio_devices(text: str) -> list[dict[str, Any]]:
    """Parse `-list-audio-devices` output into [{name, id, default}]."""
    devices: list[dict[str, Any]] = []
    for line in text.splitlines():
        if m := _AUDIO_NAME_RE.match(line):
            devices.append({"name": m.group(2), "id": "", "default": bool(m.group(1))})
        elif (m := _AUDIO_ID_RE.match(line)) and devices:
            devices[-1]["id"] = m.group(1)
    return [d for d in devices if d["id"]]


def list_audio_devices(exe=SENDER_EXE) -> list[dict[str, Any]]:
    try:
        out = subprocess.run(
            [str(exe), "-list-audio-devices"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SenderError(f"Could not list audio devices: {exc}") from exc
    return parse_audio_devices(out)


class SenderProcess:
    def __init__(
        self,
        extra_args: Sequence[str] = (),
        *,
        argv: Sequence[str] | None = None,
        on_state: Callable[[dict], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        on_exit: Callable[[int | None], None] | None = None,
        request_timeout: float = 10.0,
    ):
        self.on_state = on_state
        self.on_log = on_log
        self.on_exit = on_exit
        self.request_timeout = request_timeout
        self.capabilities: dict[str, Any] = {}
        self.resolution: tuple[int, int] | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list]] = {}
        self._hello = threading.Event()
        self._dead = threading.Event()
        cmd = list(argv) if argv is not None else [str(SENDER_EXE), "-stdio", *extra_args]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            raise SenderError(f"Could not start the sender: {exc}") from exc
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._err_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._err_reader.start()

    # -- threads ---------------------------------------------------------
    def _read_stdout(self) -> None:
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._log(f"[stdout] {line}")
                continue
            kind = msg.get("type")
            if kind == "hello":
                self.capabilities = msg.get("capabilities") or {}
                self._hello.set()
            elif kind == "state":
                if self.on_state:
                    self.on_state(msg)
            elif kind == "response":
                with self._lock:
                    slot = self._pending.pop(msg.get("id"), None)
                if slot:
                    slot[1].append(msg)
                    slot[0].set()
        code = self._proc.wait()
        self._dead.set()
        self._hello.set()
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for event, _ in pending:
            event.set()
        if self.on_exit:
            self.on_exit(code)

    def _read_stderr(self) -> None:
        for line in self._proc.stderr:
            line = line.rstrip()
            if m := _RESOLUTION_RE.search(line):
                self.resolution = (int(m.group(1)), int(m.group(2)))
            if self.on_log:
                self.on_log(line)

    # -- protocol --------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def _dead_message(self) -> str:
        code = self._proc.poll()
        if code is None:
            return "The sender did not respond."
        return f"The sender process exited (code {code})."

    def wait_hello(self, timeout: float = 10.0) -> dict:
        if not self._hello.wait(timeout) or not self.capabilities and self._dead.is_set():
            raise SenderError(self._dead_message())
        return self.capabilities

    def request(self, cmd: str, **fields: Any) -> dict:
        if self._dead.is_set():
            raise SenderError(self._dead_message())
        rid = next(self._ids)
        slot: tuple[threading.Event, list] = (threading.Event(), [])
        with self._lock:
            self._pending[rid] = slot
        payload = {"id": rid, "cmd": cmd, **{k: v for k, v in fields.items() if v is not None}}
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            with self._lock:
                self._pending.pop(rid, None)
            raise SenderError(self._dead_message()) from exc
        if not slot[0].wait(self.request_timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise SenderError(f"The sender did not answer '{cmd}' in time.")
        if not slot[1]:
            raise SenderError(self._dead_message())
        return slot[1][0]

    def devices(self) -> list[dict]:
        return self.request("devices").get("devices") or []

    def displays(self) -> list[dict]:
        return self.request("displays").get("displays") or []

    def status(self) -> dict:
        return self.request("status")

    def connect(self, target: str, port: int | None = None, options: dict | None = None) -> dict:
        return self.request("connect", target=target, port=port, options=options)

    def submit_pin(self, target: str, pin: str) -> dict:
        # The PIN goes straight onto the pipe; it is never logged or kept.
        return self.request("connect", target=target, pin=pin)

    def disconnect(self, target: str | None = None) -> dict:
        return self.request("disconnect", target=target)

    def close(self, timeout: float = 5.0) -> int | None:
        try:
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._reader.join(2)
        self._err_reader.join(2)
        return self._proc.returncode
