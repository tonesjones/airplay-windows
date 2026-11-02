"""Draws the blue Cast icon and writes it as airmirror-cast.ico.

Run `py -3 -m airmirror.icon` after changing the drawing; the window and shortcut.ps1 use the
committed .ico.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ICON_PATH = Path(__file__).with_name("airmirror-cast.ico")
SIZES = (16, 20, 24, 30, 32, 36, 40, 42, 48, 64, 256)
BLUE, WHITE, SIGNAL = "#005fb8", "#ffffff", "#9fd8ff"


def _box(cx, cy, hw, hh, r):
    def sd(x, y):
        qx, qy = abs(x - cx) - hw + r, abs(y - cy) - hh + r
        return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r

    return sd


def _outline(sd, width):
    return lambda x, y: abs(sd(x, y)) - width / 2


def _segment(x0, y0, x1, y1, width):
    def sd(x, y):
        dx, dy = x1 - x0, y1 - y0
        t = max(0, min(1, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - x0 - t * dx, y - y0 - t * dy) - width / 2

    return sd


def _line(v, w, k):
    """Centre a stroke `w` pixels wide so both its edges fall on pixel boundaries."""
    return (math.floor(v / k) + 0.5) * k if w % 2 else round(v / k) * k


def layers(k):
    """Signed distance and colour per layer, back to front, in a 64x64 design space at `k` units
    per pixel. Strokes are whole pixels and snapped to the grid so small sizes stay sharp."""
    w = max(1, round(3.5 / k))
    stroke = w * k
    # Screen outline: snap its outer edges, then centre the stroke inside them.
    left, right = round(12 / k) * k + stroke / 2, round(52 / k) * k - stroke / 2
    top, bottom = round(16 / k) * k + stroke / 2, round(44 / k) * k - stroke / 2
    frame = _box((left + right) / 2, (top + bottom) / 2, (right - left) / 2, (bottom - top) / 2, 3)
    outline = _outline(frame, stroke)
    gap = _box(16, 44, 15, 15, 0)

    def screen(x, y):
        # The casting signal interrupts the screen's lower-left corner.
        return max(outline(x, y), -gap(x, y))

    stand_y = _line(51, w, k)
    arc_w = max(1, round(3.6 / k)) * k

    def signal(x, y):
        radius = math.hypot(x - 12, y - 46)
        arcs = min(abs(radius - 12) - arc_w / 2, abs(radius - 20) - arc_w / 2)
        return max(arcs, 12 - x, y - 46)

    def signal_corner(x, y):
        return max(math.hypot(x - 12, y - 46) - 4, 12 - x, y - 46)

    return [
        (_box(32, 32, 30, 30, 14), BLUE),
        (screen, WHITE),
        (_segment(34, stand_y, 42, stand_y, stroke), WHITE),
        (signal, SIGNAL),
        (signal_corner, WHITE),
    ]


def render(size: int) -> bytes:
    """RGBA pixels, top row first."""
    k = 64 / size
    shapes = layers(k)
    colours = [tuple(int(c[i : i + 2], 16) for i in (1, 3, 5)) for _, c in shapes]
    out = bytearray()
    for j in range(size):
        for i in range(size):
            x, y = (i + 0.5) * k, (j + 0.5) * k
            r = g = b = a = 0.0
            for (sd, _), (cr, cg, cb) in zip(shapes, colours, strict=True):
                cov = max(0.0, min(1.0, 0.5 - sd(x, y) / k))
                if cov:
                    na = cov + a * (1 - cov)
                    r = (cr * cov + r * a * (1 - cov)) / na
                    g = (cg * cov + g * a * (1 - cov)) / na
                    b = (cb * cov + b * a * (1 - cov)) / na
                    a = na
            out += bytes((round(r), round(g), round(b), round(a * 255)))
    return bytes(out)


def png(size: int, rgba: bytes) -> bytes:
    stride = size * 4
    rows = b"".join(b"\0" + rgba[y * stride : (y + 1) * stride] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def ico() -> bytes:
    images = [png(size, render(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, data in zip(SIZES, images, strict=True):
        side = size % 256  # 0 means 256
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(images)


if __name__ == "__main__":
    ICON_PATH.write_bytes(ico())
    print(f"Wrote {ICON_PATH}")
