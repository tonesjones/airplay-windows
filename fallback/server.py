"""Serve the desktop as MJPEG over HTTP for old TV browsers.

Run with ``py -3 -m fallback.server``. One shared FFmpeg process grabs the screen with ddagrab and
encodes MJPEG. Only the latest frame is kept, so slow clients skip frames instead of queueing them.
All routes live under a random token so other people on the LAN cannot watch the screen.
"""

from __future__ import annotations

import argparse
import contextlib
import secrets
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"
BOUNDARY = "frame"
TOKEN_ALPHABET = string.ascii_lowercase + string.digits

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, flush=True)


def make_token() -> str:
    return "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(4))


class FrameSplitter:
    """Cuts a byte stream into JPEG frames on SOI/EOI markers, across any chunking."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        frames: list[bytes] = []
        while True:
            start = self._buf.find(SOI)
            if start < 0:
                # Keep a trailing 0xFF: its partner byte may arrive in the next chunk.
                self._buf = self._buf[-1:] if self._buf.endswith(b"\xff") else bytearray()
                break
            if start:
                del self._buf[:start]
            end = self._buf.find(EOI, 2)
            if end < 0:
                break
            frames.append(bytes(self._buf[: end + 2]))
            del self._buf[: end + 2]
        return frames


class FrameSource:
    """Holds the latest frame. Waiters wake on each new one; nothing is queued."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame: bytes | None = None
        self._seq = 0
        self._closed = False

    def publish(self, frame: bytes) -> None:
        with self._cond:
            self._frame = frame
            self._seq += 1
            self._cond.notify_all()

    def wait_next(self, last_seq: int, timeout: float) -> tuple[int, bytes] | None:
        """Block until a frame newer than last_seq exists. None on timeout or close."""
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq or self._closed, timeout)
            if self._closed or self._frame is None or self._seq == last_seq:
                return None
            return self._seq, self._frame

    def start(self) -> None:
        pass

    def stop(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


def ffmpeg_command(ffmpeg: str, display: int, fps: int, width: int, quality: int) -> list[str]:
    graph = (
        f"ddagrab=output_idx={display}:framerate={fps},hwdownload,format=bgra,"
        f"scale={width}:-2,format=yuvj420p"
    )
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-filter_complex", graph,
        "-c:v", "mjpeg", "-q:v", str(quality),
        "-f", "mjpeg", "pipe:1",
    ]  # fmt: skip


class FFmpegSource(FrameSource):
    """Runs FFmpeg, restarts it when it dies, and publishes the frames it produces."""

    def __init__(self, command: list[str]) -> None:
        super().__init__()
        self._command = command
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name="ffmpeg", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        super().stop()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run(self) -> None:
        delay = 1.0
        while not self._closed:
            frames = self._run_once()
            if self._closed:
                return
            delay = 1.0 if frames else min(delay * 2, 10.0)
            log(f"restarting FFmpeg in {delay:.0f}s")
            time.sleep(delay)

    def _run_once(self) -> int:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=flags,
            )
        except OSError as exc:
            log(f"could not start FFmpeg: {exc}")
            return 0
        self._proc = proc
        errors: list[str] = []

        def drain_stderr() -> None:
            for line in iter(proc.stderr.readline, b""):
                errors.append(line.decode("utf-8", "replace").rstrip())
                del errors[:-10]

        threading.Thread(target=drain_stderr, daemon=True).start()
        splitter = FrameSplitter()
        count = 0
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            for frame in splitter.feed(chunk):
                count += 1
                self.publish(frame)
        code = proc.wait()
        if not self._closed:
            reason = " | ".join(errors) or "no error output"
            log(f"FFmpeg exited with code {code} after {count} frames: {reason}")
        return count


PAGE_STYLE = """<style type="text/css">
html, body { margin: 0; padding: 0; background: #000; width: 100%; height: 100%; overflow: hidden; }
#s { position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: contain; }
</style>"""

PAGE_STREAM = (
    """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Screen</title>
"""
    + PAGE_STYLE
    + """</head>
<body><img id="s" alt="">
<script type="text/javascript">
var img = document.getElementById('s');
function load() { img.src = 'stream.mjpg?t=' + new Date().getTime(); }
img.onerror = function () { setTimeout(load, 1000); };
load();
</script></body></html>
"""
)

PAGE_POLL = (
    """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Screen</title>
"""
    + PAGE_STYLE
    + """</head>
<body><img id="s" alt="">
<script type="text/javascript">
var shown = document.getElementById('s');
var delay = @DELAY@;
function next() {
  var loader = new Image();
  loader.onload = function () { shown.src = loader.src; setTimeout(next, delay); };
  loader.onerror = function () { setTimeout(next, 1000); };
  loader.src = 'frame.jpg?t=' + new Date().getTime();
}
next();
</script></body></html>
"""
)


class Handler(BaseHTTPRequestHandler):
    server_version = "airmirror-fallback"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _send(self, code: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        prefix = f"/{self.server.token}/"
        if not parts.path.startswith(prefix):
            self._send(404, b"Not found")
            return
        route = parts.path[len(prefix) :]
        if route == "":
            if parse_qs(parts.query).get("mode") == ["poll"]:
                delay = max(1000 // max(self.server.fps, 1) // 2, 20)
                page = PAGE_POLL.replace("@DELAY@", str(delay))
            else:
                page = PAGE_STREAM
            self._send(200, page.encode(), "text/html; charset=utf-8")
        elif route == "frame.jpg":
            got = self.server.source.wait_next(0, timeout=5)
            if got is None:
                self._send(503, b"No frame yet")
            else:
                self._send(200, got[1], "image/jpeg")
        elif route == "stream.mjpg":
            self._stream()
        else:
            self._send(404, b"Not found")

    def _stream(self) -> None:
        self.connection.settimeout(10)  # drop clients that stop reading
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seq = 0
        try:
            while not self.server.stopping:
                got = self.server.source.wait_next(seq, timeout=1)
                if got is None:
                    continue
                seq, frame = got
                head = (
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                )
                self.wfile.write(head.encode() + frame + b"\r\n")
        except OSError:
            pass  # client went away or stalled


class FallbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, source: FrameSource, token: str, fps: int = 15) -> None:
        self.source = source
        self.token = token
        self.fps = fps
        self.stopping = False
        super().__init__(address, Handler)

    def handle_error(self, request, client_address) -> None:
        if not isinstance(sys.exc_info()[1], OSError):  # TV browsers drop connections often
            super().handle_error(request, client_address)


def lan_ip() -> str:
    """Address of the interface Windows would use to reach the internet."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # UDP connect sends nothing
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


@contextlib.contextmanager
def keep_awake():
    """Stop the PC sleeping or turning the monitor off while serving (Windows only)."""
    if sys.platform != "win32":
        yield
        return
    import ctypes

    set_state = ctypes.windll.kernel32.SetThreadExecutionState
    set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
    try:
        yield
    finally:
        set_state(ES_CONTINUOUS)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--display", type=int, default=0, help="monitor index (default 0)")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--width", type=int, default=1920, help="output width in pixels")
    p.add_argument("--quality", type=int, default=5, help="MJPEG q:v, 2 best to 31 worst")
    p.add_argument("--host", default="0.0.0.0", help="address to listen on")
    p.add_argument("--public-host", help="address to show in the printed URL")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg not found on PATH. Install it with: winget install Gyan.FFmpeg", flush=True)
        return 1
    command = ffmpeg_command(ffmpeg, args.display, args.fps, args.width, args.quality)
    source = FFmpegSource(command)
    token = make_token()
    try:
        server = FallbackServer((args.host, args.port), source, token, args.fps)
    except OSError as exc:
        print(f"Cannot listen on port {args.port}: {exc}", flush=True)
        return 1
    shown = args.public_host or lan_ip()
    port = server.server_address[1]
    with keep_awake():
        source.start()
        print(f"URL: http://{shown}:{port}/{token}/", flush=True)
        print("Open that address in the TV browser. Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopping...", flush=True)
        finally:
            server.stopping = True
            source.stop()
            server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
