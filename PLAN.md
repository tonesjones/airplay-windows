# PLAN: Windows → Samsung TV AirPlay mirroring

Goal: launch one app on Windows, pick the Samsung TV, and see the whole desktop (with sound)
mirrored over AirPlay, using free software only. If native AirPlay can't be made to work on the
TV, the fallback is a browser stream that the TV opens.

## Approach

- **Sender:** [parsakhaz/open-airplay](https://github.com/parsakhaz/open-airplay) (GPL-3.0). It is
  a Windows port of [omarroth/doubletake](https://github.com/omarroth/doubletake)'s AirPlay
  mirroring sender. `setup.ps1` builds it from a pinned commit into `bin\open-airplay.exe`, plus
  our `patches\`. It isn't committed here.
- **Capture and encode:** FFmpeg `ddagrab` (DXGI Desktop Duplication) → H.264, NVENC when
  available, otherwise libx264. Sound is captured with WASAPI loopback and sent as ALAC.
- **Samsung pairing:** SRP-6a PIN shown on the TV, then pair-verify. No FairPlay, because Samsung
  doesn't advertise FPSAP.
- **Known Samsung risk:** PTP SETUP may omit `timingPeerInfo.ClockID`. doubletake PR #61 retries
  over NTP, and it's ported as `patches\0001-ntp-fallback.patch` if open-airplay lacks it. One
  Samsung (QAQ60) still showed a frozen picture over NTP.
- **Our code:** MIT, Python stdlib at runtime (tkinter GUI) plus PowerShell. Dev tools (pytest,
  ruff) live in `.venv`.

## Pinned versions

| Component | Version |
|---|---|
| open-airplay | `1e879a982df78b630a005fcee3ce278a56b3181d` |
| Go | 1.27.0 (winget `GoLang.Go`). go.mod's toolchain line fetches 1.27.3 from proxy.golang.org. |
| FFmpeg | 9.0.2 (winget `Gyan.FFmpeg`) |
| Python | 3.14 |

## Contracts (fixed, so parallel work doesn't overlap)

- Binary: `bin\open-airplay.exe`.
  - GUI → `open-airplay.exe -stdio [-debug]`. JSON-lines protocol:
    `third_party\open-airplay\docs\control-protocol.md`.
  - Diagnostics → CLI flags: `-list-receivers`, `-list-displays`, `-list-audio-devices`, `-debug`.
- `fallback\start-browser.ps1 [-Display N] [-Port 8080]` prints the URL to open on the TV and runs
  until Ctrl+C.
- `start.ps1` runs `setup.ps1` if `bin\open-airplay.exe` is missing, then `py -3 -m airmirror`.
  `start.ps1 -Browser` → `fallback\start-browser.ps1`.
- File ownership:
  - GUI session: `airmirror/sender.py`, `airmirror/app.py`, `airmirror/__main__.py`,
    `tests/test_sender.py`.
  - Diagnostics session: `diagnose.ps1`, `start.ps1`, `airmirror/diagnose.py`,
    `tests/test_diagnose.py`.
  - Fallback session: `fallback/**`, `tests/test_fallback.py`.
  - Main: everything else (`setup.ps1`, `patches/`, `pyproject.toml`, `README.md`, `PLAN.md`).

## Items and acceptance criteria

| ID | Item | Done when | Status |
|---|---|---|---|
| T1 | Scaffold, toolchain, `setup.ps1`, build | `setup.ps1` builds `bin\open-airplay.exe`; `go test ./...` passes | done (#1) |
| T2 | NTP fallback patch | Patched build passes the fixture test for a receiver without a ClockID | done (#2) |
| T3 | Simulation and LAN discovery | Capture smoke shows SPS/PPS/IDR at 1080p; fixture session counts video and audio packets with a clean TEARDOWN; `-list-receivers` shows the TV | done (simulated). Clean TEARDOWN is left to the GUI's `disconnect` |
| T3b | Speaker mute + keep-awake helpers | Mute and restore work; the display stays on while mirroring | done, branch `pc-mute` (pushed, no PR); wire into the GUI hooks |
| T4/5 | Protocol client and tkinter GUI | pytest and ruff pass; the GUI lists real receivers and displays | built by a subagent, branch `gui-client` (pushed, **unreviewed**). 14 tests pass; real `displays()` works |
| T6 | Diagnostics and `start.ps1` | Redaction tests pass; `diagnose.ps1` writes a zip; `start.ps1` opens the GUI | branch `diagnostics` (pushed, **unreviewed**, flagged privacy). 31 tests pass; the .ps1 files have **never run** (the subagent sandbox blocked pwsh) |
| T7 | Browser fallback | Stream plays in a desktop browser; pytest and ruff pass | branch `browser-fallback` (pushed, **unreviewed**). 16 tests pass; 1920x1080 at 15.2 fps to 127.0.0.1; `start-browser.ps1` never ran |
| T7b | Never change TV volume | No SET_PARAMETER volume at any point | done (#3); TV volume unchanged on the real TV |
| T8 | Integrate and merge | All PRs merged; GUI works end to end with the real binary | todo |
| T9 | README and final pass | README covers setup, pairing, limits and troubleshooting | todo |
| T10 | Hardware test (owner) | Desktop and sound on the TV for 5+ minutes; second Start reuses the pairing | **mostly done**: picture, sound and volume confirmed live on the TV (see the log). The pairing reuse worked: no PIN on the second connect |

## Status log

- 2026-10-25: Research done (see Approach). Go 1.27.0 and FFmpeg 9.0.2 installed. FFmpeg has
  `ddagrab`, libx264, h264_nvenc, h264_amf, h264_qsv and h264_mf. GPU: RTX 4070 SUPER.

- 2026-10-25: T1 merged. The pinned open-airplay builds, and `go test ./...` passes.
- 2026-10-25: T2 merged. doubletake PR #61 is ported by hand (open-airplay changed the
  `prepareVideoCapture` signature). `TestReceiverWithoutPTPTimelineFallsBackToNTP` passes.
- 2026-10-25: Three Sonnet sessions started: `gui-client`, `diagnostics`, `browser-fallback`.
- 2026-10-25 T3 findings:
  - `-list-displays` reports `2048x1152`. That's the DPI-scaled size, since the desktop is 2560x1440 at
    125%. ddagrab opens the output at its real 2560x1440, and the sender scales to the TV canvas.
  - Capture smoke **times out**. The user had been idle for 32 minutes, and the monitor turns off after
    15 (power plan), so Desktop Duplication gets no frames. Rerun with the screen on.
  - Fixture with PIN auth: pair-setup with PIN and FairPlay setup complete. `-test` synthetic
    video needs GStreamer, which isn't installed, so a full media session also needs live capture
    with the screen on.
  - `-list-receivers` found **no receivers**. The Wi-Fi profile is **Public**, and on Public
    Windows Firewall blocks inbound mDNS replies. The user must switch the network to Private.
    It's also possible the TV was off.
  - WASAPI loopback still captures a muted endpoint (GStreamer issue #1306). `airmirror/pcmute.py`
    mutes and restores the default speakers. Tested on this PC.

- 2026-10-25 T3 follow-up, run from the Terminal panel (outside the agent sandbox):
  - **TV found:** `Samsung 7 Series (55)`, model `UNU710D`, at `192.168.0.45:34247`,
    mirroring=yes. The agent sandbox blocks LAN and mDNS, so run discovery from a real terminal.
    The port isn't 7000, so `-target` alone won't reach it without `-port`.
  - Windows Firewall has two **Block** rules named `open-airplay` (Public, inbound). They were probably
    created when the first-run prompt went unanswered. They will block the TV's NTP timing traffic
    back to the PC.
  - Capture still failed: the user had been idle for 44 minutes and the monitor was asleep. ddagrab,
    gfxcapture and gdigrab all stall or crawl with the display off. `keepawake.hold()` doesn't wake a
    sleeping display; it only keeps an awake one on.
  - Added `airmirror/keepawake.py`, which keeps the display on while mirroring (branch `pc-mute`).

- 2026-10-25 T3 with the monitor awake (Terminal panel; simulated, not the TV):
  - Capture smoke passes with `-hwaccel auto` (NVENC) and `none` (libx264) at 1920x1080@30.
  - Fixture `modern` with PIN auth: pairing, FairPlay, 3 SETUPs, audio capture started; about 31 video
    packets/s and steady audio.
  - Fixture `lg` with PIN auth, which has no FPSAP like Samsung: the sender skipped FairPlay and used
    pair-verify. Video and audio flowed at the same rates. This is the Samsung path, simulated.
  - Teardown wasn't exercised, because the script killed the sender.
  - The user allowed the firewall prompts. `bin\open-airplay.exe` now has inbound **Allow** rules
    (Public profile). The network is still Public; switching to Private is optional.

- 2026-10-25 **first real TV test** (Samsung UNU710D, 2018), driven over `-stdio` from a scratch script:
  - Transient pairing was refused (M4 error 2), then the TV showed a PIN. The PIN pair-setup and
    pair-verify both worked.
  - FPSAP isn't advertised, so FairPlay was skipped. The TV chose NTP timing, and its NTP requests
    reached the PC.
  - The TV offered a 1920x1080@30 canvas. Audio was ALAC 44.1 kHz. SETUP x3 and RECORD succeeded,
    and the state reached `streaming`.
  - About 1 s after the first IDR (libx264, High@4.0, multi-slice), **the TV closed the video data
    channel with a TCP reset**. It went back to the previous programme with no picture. Afterwards
    its AirPlay service stopped advertising, and the next connect to the old port was refused.
  - **Bug: the TV volume was left at 100.** The sender sent `SET_PARAMETER volume: 0.000000`
    (0 dB is full scale) twice at SETUP, and Samsung maps that onto the speaker volume. Fixed by
    `patches/0002-never-set-receiver-volume.patch`: no volume writes at all, and mute refuses.
  - Suspects for the reset, not yet tested:
    - video key derivation or key mixing, the same symptom as doubletake issue #56 (Xiaomi);
    - multi-slice frames from x264 `sliced-threads`, to be tested with `hwaccel=nvenc`;
    - an AirTunes version difference from the TU8300 that works.

- 2026-10-25 **native AirPlay mirroring works on the real TV** (15:40, Samsung UNU710D):
  - The setup was `hwaccel=nvenc`, sound on, 1920x1080@30, NTP timing, no FairPlay, and the saved
    pairing (no PIN). The TV had just been rebooted with the remote's power button.
  - The owner confirmed the desktop on the TV, a picture that keeps up, and unchanged volume. They
    used it to watch a show.
  - The log shows real audio levels (peak about 7000/32768) and **0 stream errors** after more than
    3 minutes.
  - The owner set the TV's AirPlay "Require code" to **First time only**, so the saved pairing
    reconnects without a PIN. Keep that in the README.
  - **Not yet known:** why the first attempt was reset. It differed in three ways: libx264 (multi-slice)
    instead of NVENC, the TV not freshly rebooted, and "require code every time". Test libx264 again
    before choosing the GUI default; until then, default the encoder to `nvenc` when present.
  - **The TV's AirPlay stops advertising after a session ends badly** (a reset, or the sender being
    killed). Turning the TV off and on with the remote brought it back after a minute or two. The
    GUI must always stop with a clean `disconnect`, and the README needs a "TV not found →
    turn the TV off and on" step.
  - The Samsung's AirPlay port changes after every TV reboot (34247, then 44559). Always use the
    discovered port.
- 2026-10-25 16:10: the session ran the driver's full 30 minutes (15:40 to 16:10) with 0 stream
  errors, then ended through `disconnect`. The log shows a clean TEARDOWN, so a clean stop is verified on the TV.
- The session was driven by `tv_test.py`, a scratch driver (untracked, repo root). It mirrors over
  `-stdio`, takes the PIN from `diagnostics\pin.txt`, stops on `diagnostics\stop.txt`, and stops
  itself 30 minutes after connecting. Delete it once the GUI is merged.

## Next action

1. Main: review the three subagent branches (`gui-client`, `diagnostics` (read in full),
   `browser-fallback`). Run their .ps1 files from the Terminal panel, open PRs, and merge.
2. Main: wire `pcmute` and `keepawake` (branch `pc-mute`) into the GUI's
   `on_streaming_started`/`on_streaming_stopped` hooks. Default the encoder to `nvenc`.
3. Main: README (technical-writing skill), then `/deslop`, tests, `/unslop`.
4. Owner: the final GUI test. Start, watch 5+ minutes, Stop, Start again (no PIN). Also try one
   libx264 session to settle the encoder default.

**Don't run `setup.ps1` while a mirror session is running.** It rebuilds `bin\open-airplay.exe`,
which is locked while the sender runs.

## Open decisions

- None.
