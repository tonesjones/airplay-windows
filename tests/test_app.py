import json
import sys
import time
import tkinter as tk
import zlib
from pathlib import Path

import pytest

from airmirror.app import ENCODERS, App, error_text, load_settings, tv_id
from airmirror.ui import rounded_png

FAKE = str(Path(__file__).with_name("fake_sender.py"))


def test_rounded_png_is_a_valid_rgba_image():
    data = rounded_png(20, 10, 4, "#4cc2ff")
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    idat = data.index(b"IDAT")
    size = int.from_bytes(data[idat - 4 : idat], "big")
    raw = zlib.decompress(data[idat + 4 : idat + 4 + size])
    assert len(raw) == 10 * (1 + 20 * 4)
    first_row, middle_row = raw[1:81], raw[5 * 81 + 1 : 6 * 81]
    assert first_row[3] == 0  # corner is transparent
    assert middle_row[40 + 3] == 255  # inside is opaque
    assert middle_row[40:43] == bytes((0x4C, 0xC2, 0xFF))


def test_load_settings_defaults_and_bad_files(tmp_path):
    missing = load_settings(tmp_path / "none.json")
    assert missing["sound"] and missing["mute_pc"] and missing["keep_awake"]
    assert missing["encoder"] in ENCODERS

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", "utf-8")
    assert load_settings(broken) == missing

    saved = tmp_path / "saved.json"
    saved.write_text(json.dumps({"quality": "Smooth", "unknown": 1}), "utf-8")
    loaded = load_settings(saved)
    assert loaded["quality"] == "Smooth" and "unknown" not in loaded


def test_tv_id_prefers_device_id():
    assert tv_id({"device_id": "a", "name": "TV"}) == "a"
    assert tv_id({"name": "TV"}) == "TV"


def test_error_text_never_shows_raw_kinds():
    assert error_text("wrong_code", "TV") == "That code didn't work. Press Start to get a new one."
    assert error_text("something_new", "") == "Couldn't mirror to the TV."


def pump(root, pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if pred():
            return True
        time.sleep(0.02)
    return False


def enter_code(app, code):
    widgets = app.pin_dialog.winfo_children()
    next(w for w in widgets if w.winfo_class() == "Entry").insert(0, code)
    buttons = next(w for w in widgets if w.winfo_class() == "Frame").winfo_children()
    next(b for b in buttons if b.cget("text") == "Connect").invoke()


@pytest.fixture(scope="module")
def tk_root():
    # One Tk per process: a second Tk() in the same process can fail to find init.tcl.
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root, tmp_path):
    app = App(
        tk.Toplevel(tk_root),
        argv=[sys.executable, FAKE],
        auto_refresh=False,
        settings_path=tmp_path / "settings.json",
    )
    app.events_seen = []
    app.on_streaming_started = lambda: app.events_seen.append("up")
    app.on_streaming_stopped = lambda: app.events_seen.append("down")
    yield app
    if not app.closing:
        app.close()


def test_gui_pairs_streams_and_stops(app):
    root = app.root
    assert pump(root, lambda: app.tv is not None)  # the only TV is picked automatically
    assert app.audio_capable
    assert json.loads(app.settings_path.read_text("utf-8"))["tv"] == "a"

    app._press_start()
    assert pump(root, lambda: app.pin_dialog is not None)
    enter_code(app, "1234")
    assert pump(root, lambda: app.streaming)
    assert app.events_seen == ["up"]
    assert app.active

    app._press_start()  # the bar reads "Stop mirroring" now
    assert pump(root, lambda: not app.active)
    assert app.events_seen == ["up", "down"]


def test_gui_wrong_code_shows_friendly_error(app):
    root = app.root
    assert pump(root, lambda: app.tv is not None)
    app.start()
    assert pump(root, lambda: app.pin_dialog is not None)
    enter_code(app, "0000")
    assert pump(root, lambda: app._error_text is not None)
    assert "secret" not in app._error_text
    assert not app.active and not app.streaming


def test_setting_tiles_lock_while_mirroring(app):
    root = app.root
    assert pump(root, lambda: app.tv is not None)
    app.start()
    assert pump(root, lambda: app.active)
    enabled = {t.key: t.enabled for t in app._tiles()}
    assert enabled == {
        "sound": False,
        "mute_pc": True,
        "keep_awake": True,
        "quality": False,
        "encoder": False,
        "display": False,
    }


def test_gui_options_follow_settings(app):
    root = app.root
    assert pump(root, lambda: app.displays and app.audio_capable)
    app._set(quality="Weak Wi-Fi")
    app._set(encoder="none")
    app._set(sound=False)
    opts = app._options()
    assert opts == {
        "fps": 30,
        "bitrate_kbps": 3000,
        "hwaccel": "none",
        "display": 0,
        "audio": False,
    }
    saved = json.loads(app.settings_path.read_text("utf-8"))
    assert saved["quality"] == "Weak Wi-Fi" and saved["encoder"] == "none"


def test_gui_scans_a_remembered_tv_that_mdns_doesnt_list(tk_root, tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"tv": "old", "tv_ip": "10.9.9.9", "tv_name": "Old TV"}), "utf-8"
    )
    scanned = []
    monkeypatch.setattr("airmirror.app.probe.find_port", lambda ip: scanned.append(ip) or 4242)
    app = App(
        tk.Toplevel(tk_root),
        argv=[sys.executable, FAKE],
        auto_refresh=False,
        settings_path=settings,
    )
    app.on_streaming_started = app.on_streaming_stopped = lambda: None
    try:
        assert app.tv["name"] == "Old TV"  # shown before any mDNS reply
        assert pump(app.root, lambda: app.searched)
        assert app.tv["ip"] == "10.9.9.9"  # the fake's other TV doesn't replace it
        app.start()
        assert pump(app.root, lambda: app.pin_dialog is not None)
        assert scanned == ["10.9.9.9"]
        assert app.probed == ("10.9.9.9", 4242)
        assert app.target == "10.9.9.9"
    finally:
        app.close()
