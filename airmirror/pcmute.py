"""Mute this PC's default speakers while mirroring.

WASAPI loopback still captures a muted render endpoint, so the TV keeps its sound
while the PC goes quiet. Uses the Core Audio COM API through ctypes (stdlib only).
"""

import ctypes
from ctypes import HRESULT, POINTER, byref, c_int, c_void_p, wintypes

_CLSCTX_ALL = 0x17
_E_RENDER = 0
_E_CONSOLE = 0
_RPC_E_CHANGED_MODE = -2147417850  # COM already initialised as STA on this thread


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str):
        super().__init__()
        ctypes.oledll.ole32.CLSIDFromString(text, byref(self))


_CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_IID_IAUDIO_ENDPOINT_VOLUME = "{5CDF2C82-841E-4546-9722-0CF74078229A}"

# Vtable slots after IUnknown's QueryInterface/AddRef/Release (0-2).
_ENUM_GET_DEFAULT_ENDPOINT = 4
_DEVICE_ACTIVATE = 3
_VOLUME_SET_MUTE = 14
_VOLUME_GET_MUTE = 15
_RELEASE = 2


def _call(obj: c_void_p, slot: int, *argtypes):
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)(vtable[slot])


def _release(obj: c_void_p) -> None:
    vtable = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    ctypes.WINFUNCTYPE(wintypes.ULONG, c_void_p)(vtable[_RELEASE])(obj)


def _with_endpoint_volume(action):
    ole32 = ctypes.windll.ole32
    hr = ole32.CoInitialize(None)
    uninit = hr >= 0
    if hr < 0 and hr != _RPC_E_CHANGED_MODE:
        raise OSError(f"CoInitialize failed: 0x{hr & 0xFFFFFFFF:08X}")
    enumerator, device, volume = c_void_p(), c_void_p(), c_void_p()
    try:
        ctypes.oledll.ole32.CoCreateInstance(
            byref(_GUID(_CLSID_MMDEVICE_ENUMERATOR)),
            None,
            _CLSCTX_ALL,
            byref(_GUID(_IID_IMMDEVICE_ENUMERATOR)),
            byref(enumerator),
        )
        _call(enumerator, _ENUM_GET_DEFAULT_ENDPOINT, c_int, c_int, POINTER(c_void_p))(
            enumerator, _E_RENDER, _E_CONSOLE, byref(device)
        )
        _call(
            device,
            _DEVICE_ACTIVATE,
            POINTER(_GUID),
            wintypes.DWORD,
            c_void_p,
            POINTER(c_void_p),
        )(device, byref(_GUID(_IID_IAUDIO_ENDPOINT_VOLUME)), _CLSCTX_ALL, None, byref(volume))
        return action(volume)
    finally:
        for obj in (volume, device, enumerator):
            if obj:
                _release(obj)
        if uninit:
            ole32.CoUninitialize()


def get_mute() -> bool:
    def read(volume):
        muted = wintypes.BOOL()
        _call(volume, _VOLUME_GET_MUTE, POINTER(wintypes.BOOL))(volume, byref(muted))
        return bool(muted.value)

    return _with_endpoint_volume(read)


def set_mute(muted: bool) -> None:
    _with_endpoint_volume(
        lambda volume: _call(volume, _VOLUME_SET_MUTE, wintypes.BOOL, c_void_p)(volume, muted, None)
    )


class SpeakerMute:
    """Mutes the default speakers and later puts back whatever state they had."""

    def __init__(self):
        self._previous: bool | None = None

    def mute(self) -> None:
        if self._previous is None:
            self._previous = get_mute()
        set_mute(True)

    def restore(self) -> None:
        if self._previous is not None:
            set_mute(self._previous)
            self._previous = None
