"""Colours, the Windows theme, and anti-aliased rounded shapes for the AirMirror window.

tkinter's canvas draws without anti-aliasing, so rounded tiles are rendered as PNGs here
and cached as images.
"""

from __future__ import annotations

import base64
import ctypes
import struct
import tkinter as tk
import winreg
import zlib
from tkinter import font as tkfont

DARK = {
    "bg": "#1c1c1f",
    "card": "#2a2a2e",
    "card_hover": "#34343a",
    "line": "#3a3a40",
    "text": "#f2f2f2",
    "muted": "#9a9aa2",
    "accent": "#a0d6b5",
    "accent_hover": "#b9e4c9",
    "accent_dim": "#8abe9f",
    "green": "#3ddc84",
    "green_hover": "#62e39b",
    "red": "#ff6b6b",
    "on_accent": "#101014",
}
LIGHT = {
    "bg": "#f5f5f3",
    "card": "#ffffff",
    "card_hover": "#f9f9f9",
    "line": "#e0e0e0",
    "text": "#1a1a1a",
    "muted": "#6b6b6b",
    "accent": "#226044",
    "accent_hover": "#2d7455",
    "accent_dim": "#194d35",
    "green": "#0f7b0f",
    "green_hover": "#2a8c2a",
    "red": "#c42b1c",
    "on_accent": "#ffffff",
}

_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20


def windows_uses_dark() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


_WM_SETICON, _ICON_SMALL, _ICON_BIG = 0x80, 0, 1
_IMAGE_ICON, _LR_LOADFROMFILE = 1, 0x10


def match_title_bar(win: tk.Misc, dark: bool) -> None:
    win.update_idletasks()
    value = ctypes.c_int(int(dark))
    ctypes.windll.dwmapi.DwmSetWindowAttribute(
        int(win.wm_frame(), 16),
        _DWMWA_USE_IMMERSIVE_DARK_MODE,
        ctypes.byref(value),
        ctypes.sizeof(value),
    )


def set_window_icon(win: tk.Misc, path: str) -> None:
    """Give the window icons at the exact sizes Windows draws them.

    Tk only sets 32 px and 16 px icons (scaled by DPI), and the taskbar draws at 24 px, so it
    shrinks the big one and blurs it.
    """
    win.update_idletasks()
    hwnd = int(win.wm_frame(), 16)
    dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
    load = ctypes.windll.user32.LoadImageW
    load.restype = ctypes.c_void_p
    send = ctypes.windll.user32.SendMessageW
    send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    for kind, size in ((_ICON_SMALL, 16), (_ICON_BIG, 24)):
        side = round(size * dpi / 96)
        icon = load(None, path, _IMAGE_ICON, side, side, _LR_LOADFROMFILE)
        if icon:
            send(hwnd, _WM_SETICON, kind, icon)


def icon_font(root: tk.Misc) -> str:
    # Windows 11 ships Segoe Fluent Icons; Windows 10 has the older set with the same code points.
    if "Segoe Fluent Icons" in tkfont.families(root):
        return "Segoe Fluent Icons"
    return "Segoe MDL2 Assets"


def rounded_png(width: int, height: int, radius: int, color: str) -> bytes:
    rgb = bytes(int(color[i : i + 2], 16) for i in (1, 3, 5))
    rows = []
    for y in range(height):
        cy = min(max(y + 0.5, radius), height - radius)
        row = bytearray(b"\0")
        for x in range(width):
            cx = min(max(x + 0.5, radius), width - radius)
            dist = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5
            row += rgb + bytes((round(max(0.0, min(1.0, radius - dist + 0.5)) * 255),))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


class Shapes:
    """Caches rounded-rectangle images; tkinter needs a live reference to each one."""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.cache: dict[tuple, tk.PhotoImage] = {}

    def get(self, width: int, height: int, radius: int, color: str) -> tk.PhotoImage:
        key = (width, height, radius, color)
        if key not in self.cache:
            data = base64.b64encode(rounded_png(width, height, radius, color))
            self.cache[key] = tk.PhotoImage(master=self.root, data=data, format="png")
        return self.cache[key]
