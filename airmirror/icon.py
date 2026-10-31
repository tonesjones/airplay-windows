"""Draws the AirMirror icon, a cloud on a TV, and writes it as airmirror.ico.

Run `py -3 -m airmirror.icon` after changing the drawing; the window and shortcut.ps1 use the
committed .ico.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ICON_PATH = Path(__file__).with_name("airmirror.ico")
SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
BLUE, WHITE, CLOUD = "#005fb8", "#ffffff", "#9fd8ff"


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


def _cloud(x, y):
    puffs = [((22.8, 31), 5), ((31.8, 27.5), 7.5), ((39.8, 30), 6)]
    sd = min(math.hypot(x - cx, y - cy) - r for (cx, cy), r in puffs)
    sd = min(sd, _box(31.3, 33, 8.5, 3, 0)(x, y))
    return max(sd, y - 36)  # flat bottom


# Signed distance and colour per layer, in a 64x64 design space, back to front.
LAYERS = [
    (_box(32, 32, 30, 30, 14), BLUE),
    (_outline(_box(32, 28, 22, 15, 3), 4), WHITE),
    (_segment(24, 51, 40, 51, 4), WHITE),
    (_cloud, CLOUD),
]


def render(size: int) -> bytes:
    """RGBA pixels, top row first."""
    k = 64 / size
    colours = [tuple(int(c[i : i + 2], 16) for i in (1, 3, 5)) for _, c in LAYERS]
    out = bytearray()
    for j in range(size):
        for i in range(size):
            x, y = (i + 0.5) * k, (j + 0.5) * k
            r = g = b = a = 0.0
            for (sd, _), (cr, cg, cb) in zip(LAYERS, colours, strict=True):
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
