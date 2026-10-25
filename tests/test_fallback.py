import http.client
import threading
import time

import pytest

from fallback.server import SOI, FallbackServer, FrameSource, FrameSplitter, make_token

TOKEN = "ab12"


def jpeg(n: int, size: int = 100) -> bytes:
    return SOI + bytes([n]) * size + b"\xff\xd9"


def test_splitter_frame_across_chunks():
    s = FrameSplitter()
    data = jpeg(1) + jpeg(2)
    out = []
    for i in range(0, len(data), 7):
        out += s.feed(data[i : i + 7])
    assert out == [jpeg(1), jpeg(2)]


def test_splitter_marker_split_between_chunks():
    s = FrameSplitter()
    frame = jpeg(3)
    assert s.feed(frame[:1]) == []
    assert s.feed(frame[1:-1]) == []
    assert s.feed(frame[-1:]) == [frame]


def test_splitter_several_frames_in_one_chunk():
    s = FrameSplitter()
    assert s.feed(jpeg(1) + jpeg(2) + jpeg(3) + SOI + b"\x05") == [jpeg(1), jpeg(2), jpeg(3)]
    assert s.feed(b"\x05\xff\xd9") == [SOI + b"\x05\x05\xff\xd9"]


def test_splitter_garbage_before_soi():
    s = FrameSplitter()
    assert s.feed(b"junk\x00\xff\x01" + jpeg(4)) == [jpeg(4)]
    assert s.feed(b"\x00\x00" + jpeg(5)) == [jpeg(5)]


def test_token_shape():
    for _ in range(50):
        t = make_token()
        assert len(t) == 4 and t.isalnum() and t == t.lower()


class FakeSource(FrameSource):
    """Publishes a new frame every few ms until stopped."""

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        n = 0
        while not self._closed:
            n += 1
            self.publish(jpeg(n % 200, 2000))
            time.sleep(0.01)


@pytest.fixture
def server():
    source = FakeSource()
    srv = FallbackServer(("127.0.0.1", 0), source, TOKEN, fps=15)
    source.start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.stopping = True
    source.stop()
    srv.shutdown()
    srv.server_close()


def get(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    return conn, resp


def test_page_loads_with_token(server):
    conn, resp = get(server, f"/{TOKEN}/")
    body = resp.read()
    assert resp.status == 200
    assert b"stream.mjpg" in body and b"<img" in body
    conn.close()


def test_poll_page(server):
    conn, resp = get(server, f"/{TOKEN}/?mode=poll")
    body = resp.read()
    assert resp.status == 200 and b"frame.jpg" in body and b"@DELAY@" not in body
    conn.close()


@pytest.mark.parametrize(
    "path", ["/", "/wrong/", "/wrong/frame.jpg", f"/{TOKEN}x/", "/stream.mjpg"]
)
def test_wrong_token_is_404(server, path):
    conn, resp = get(server, path)
    assert resp.status == 404
    conn.close()


def test_frame_jpg(server):
    conn, resp = get(server, f"/{TOKEN}/frame.jpg")
    body = resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Type") == "image/jpeg"
    assert body.startswith(SOI) and body.endswith(b"\xff\xd9")
    conn.close()


def read_part(resp):
    """Read one multipart part from a stream response and return its body."""
    assert resp.fp.readline().strip() == b"--frame"
    length = 0
    while True:
        line = resp.fp.readline().strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    data = resp.fp.read(length)
    resp.fp.readline()
    return data


def test_stream_part_starts_with_soi(server):
    conn, resp = get(server, f"/{TOKEN}/stream.mjpg")
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("multipart/x-mixed-replace")
    part = read_part(resp)
    assert part.startswith(SOI)
    assert read_part(resp).startswith(SOI)
    conn.close()


def test_slow_client_does_not_block_fast_client(server):
    slow_conn, slow = get(server, f"/{TOKEN}/stream.mjpg")  # never reads
    assert slow.status == 200
    fast_conn, fast = get(server, f"/{TOKEN}/stream.mjpg")
    start = time.monotonic()
    parts = [read_part(fast) for _ in range(30)]
    assert all(p.startswith(SOI) for p in parts)
    assert time.monotonic() - start < 4
    slow_conn.close()
    fast_conn.close()
