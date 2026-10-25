"""Keep the display on while mirroring.

Desktop Duplication delivers no frames once the monitor sleeps, so the TV picture
would freeze when nobody touches the PC. Video players use the same request.
The state belongs to the calling thread, so call both functions from one thread.
"""

import ctypes

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def hold() -> None:
    if not ctypes.windll.kernel32.SetThreadExecutionState(
        _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
    ):
        raise OSError("SetThreadExecutionState refused the display request")


def release() -> None:
    ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
