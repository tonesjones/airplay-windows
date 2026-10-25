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
| T1 | Scaffold, toolchain, `setup.ps1`, build | `setup.ps1` builds `bin\open-airplay.exe`; `go test ./...` passes | in progress |
| T2 | NTP fallback patch | Patched build passes the fixture test for a receiver without a ClockID | todo |
| T3 | Simulation and LAN discovery | Capture smoke shows SPS/PPS/IDR at 1080p; fixture session counts video and audio packets with a clean TEARDOWN; `-list-receivers` shows the TV | todo |
| T4/5 | Protocol client and tkinter GUI | pytest and ruff pass; the GUI lists real receivers and displays | todo |
| T6 | Diagnostics and `start.ps1` | Redaction tests pass; `diagnose.ps1` writes a zip; `start.ps1` opens the GUI | todo |
| T7 | Browser fallback | Stream plays in a desktop browser; pytest and ruff pass | todo |
| T8 | Integrate and merge | All PRs merged; GUI works end to end with the real binary | todo |
| T9 | README and final pass | README covers setup, pairing, limits and troubleshooting | todo |
| T10 | Hardware test (owner) | Desktop and sound on the TV for 5+ minutes; second Start reuses the pairing | todo |

## Status log

- 2026-10-25: Research done (see Approach). Go 1.27.0 and FFmpeg 9.0.2 installed. FFmpeg has
  `ddagrab`, libx264, h264_nvenc, h264_amf, h264_qsv and h264_mf. GPU: RTX 4070 SUPER.

## Next action

Finish T1: scaffold branch, `setup.ps1`, build, and merge. Then start the three parallel sessions.

## Open decisions

- None yet.
