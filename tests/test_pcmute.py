import sys

import pytest

if sys.platform != "win32":
    pytest.skip("Core Audio is Windows only", allow_module_level=True)

from airmirror import pcmute


@pytest.fixture
def original_mute():
    try:
        state = pcmute.get_mute()
    except OSError as exc:
        pytest.skip(f"no default audio endpoint: {exc}")
    yield state
    pcmute.set_mute(state)


def test_mute_then_restore_puts_back_previous_state(original_mute):
    speakers = pcmute.SpeakerMute()
    speakers.mute()
    assert pcmute.get_mute() is True
    speakers.restore()
    assert pcmute.get_mute() is original_mute


def test_restore_without_mute_changes_nothing(original_mute):
    pcmute.SpeakerMute().restore()
    assert pcmute.get_mute() is original_mute
