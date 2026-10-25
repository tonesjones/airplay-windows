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
| T3 | Simulation and LAN discovery | Capture smoke shows SPS/PPS/IDR at 1080p; fixture session counts video and audio packets with a clean TEARDOWN; `-list-receivers` shows the TV | **blocked**: see the status log |
| T3b | Speaker mute helper | Mute and restore work on this PC | done locally, branch `pc-mute`, unpushed; wired into the GUI after S-A merges |
| T4/5 | Protocol client and tkinter GUI | pytest and ruff pass; the GUI lists real receivers and displays | session `gui-client` |
| T6 | Diagnostics and `start.ps1` | Redaction tests pass; `diagnose.ps1` writes a zip; `start.ps1` opens the GUI | session `diagnostics` |
| T7 | Browser fallback | Stream plays in a desktop browser; pytest and ruff pass | session `browser-fallback` |
| T8 | Integrate and merge | All PRs merged; GUI works end to end with the real binary | todo |
| T9 | README and final pass | README covers setup, pairing, limits and troubleshooting | todo |
| T10 | Hardware test (owner) | Desktop and sound on the TV for 5+ minutes; second Start reuses the pairing | todo |

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

## Next action

1. User, at the PC, from an **admin** PowerShell:
   `Set-NetConnectionProfile -InterfaceAlias Wi-Fi -NetworkCategory Private`, then
   `Get-NetFirewallRule -DisplayName open-airplay | Remove-NetFirewallRule`. Choose **Allow** on
   Private when the firewall prompt reappears.
2. Main, with the user at the PC and the monitor awake: rerun `bin\open-airplay.exe -display 0
   -capture-smoke` from the Terminal panel, then the fixture session with audio.
3. Main: review and merge the three session PRs, then wire `pcmute` and `keepawake` into the GUI.

## Open decisions

- None.
