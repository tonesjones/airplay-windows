import socket
import threading

from airmirror.probe import find_port


def listener(reply: bytes) -> tuple[socket.socket, int]:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                conn.recv(512)
                conn.sendall(reply)

    threading.Thread(target=serve, daemon=True).start()
    return server, server.getsockname()[1]


def test_find_port_finds_an_airtunes_reply():
    decoy, decoy_port = listener(b"HTTP/1.1 200 OK\r\nServer: nginx\r\n\r\n")
    tv, tv_port = listener(b"RTSP/1.0 200 OK\r\nServer: AirTunes/377.40.00\r\n\r\n")
    try:
        low, high = sorted((decoy_port, tv_port))
        assert find_port("127.0.0.1", range(low, high + 1)) == tv_port
    finally:
        decoy.close()
        tv.close()


def test_find_port_returns_none_when_nothing_answers():
    decoy, port = listener(b"SSH-2.0-OpenSSH\r\n")
    try:
        assert find_port("127.0.0.1", range(port, port + 1)) is None
    finally:
        decoy.close()
