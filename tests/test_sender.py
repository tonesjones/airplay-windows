import sys
import time
from pathlib import Path

import pytest

from airmirror.sender import SenderError, SenderProcess, parse_audio_devices

FAKE = str(Path(__file__).with_name("fake_sender.py"))


def spawn(*flags, **kw):
    return SenderProcess(argv=[sys.executable, FAKE, *flags], **kw)


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def sender():
    states, logs = [], []
    s = spawn(on_state=states.append, on_log=logs.append)
    s.states, s.logs = states, logs
    yield s
    s.close()


def test_hello_and_capabilities(sender):
    assert sender.wait_hello() == {"audio": True}
    assert wait_for(lambda: sender.states)
    assert sender.states[0]["state"] == "idle"


def test_no_audio_capability():
    s = spawn("--no-audio")
    try:
        assert s.wait_hello() == {"audio": False}
    finally:
        s.close()


def test_devices_and_displays(sender):
    devs = sender.devices()
    assert devs[0]["ip"] == "10.0.0.5" and devs[0]["port"] == 34247
    assert sender.displays()[0]["primary"] is True


def test_resolution_from_stderr(sender):
    sender.wait_hello()
    assert wait_for(lambda: sender.resolution == (1920, 1080))


def test_pin_flow_to_streaming(sender):
    reply = sender.connect("10.0.0.5", 34247, {"display": 0})
    assert reply["echo_port"] == 34247 and reply["echo_opts"] == {"display": 0}
    assert wait_for(lambda: sender.states[-1]["state"] == "pin_required")
    assert sender.states[-1]["streams"][0]["credential_kind"] == "pin"
    assert sender.submit_pin("10.0.0.5", "1234")["ok"]
    assert wait_for(lambda: sender.states[-1]["state"] == "streaming")


def test_wrong_code(sender):
    sender.connect("10.0.0.5", 34247)
    assert wait_for(lambda: sender.states[-1]["state"] == "pin_required")
    sender.submit_pin("10.0.0.5", "0000")
    assert wait_for(lambda: sender.states[-1]["state"] == "idle")
    assert sender.states[-1]["errors"][0]["kind"] == "wrong_code"


def test_disconnect_and_clean_close(sender):
    sender.connect("10.0.0.5", 34247)
    assert sender.disconnect("10.0.0.5")["state"] == "idle"
    assert sender.close() == 0
    assert not sender.alive


def test_dead_child_raises_clear_error():
    s = spawn("--die")
    try:
        with pytest.raises(SenderError, match="exited"):
            s.wait_hello()
        with pytest.raises(SenderError, match="exited"):
            s.devices()
    finally:
        s.close()


def test_exit_callback_fires():
    codes = []
    s = spawn("--die", on_exit=codes.append)
    s.close()
    assert wait_for(lambda: codes == [3])


def test_missing_executable_raises():
    with pytest.raises(SenderError):
        SenderProcess(argv=["definitely-not-a-real-program-xyz"])


def test_pin_never_logged(sender):
    sender.connect("10.0.0.5", 34247)
    sender.submit_pin("10.0.0.5", "1234")
    sender.close()
    assert not any("1234" in line for line in sender.logs)


def test_parse_audio_devices():
    text = (
        "* 1. Headphones (Arctis)\n     id: {0.0.0.0}.{aaa}\n"
        "  2. Dell S2417DG (NVIDIA)\n     id: {0.0.0.0}.{bbb}\n"
        "* = default output device (followed when -audio-device is not set)\n"
    )
    devs = parse_audio_devices(text)
    assert [d["name"] for d in devs] == ["Headphones (Arctis)", "Dell S2417DG (NVIDIA)"]
    assert devs[0]["default"] and not devs[1]["default"]
    assert devs[1]["id"] == "{0.0.0.0}.{bbb}"
