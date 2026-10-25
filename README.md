# airplay-windows

Mirror your whole Windows desktop, with sound, to a Samsung TV over AirPlay. It uses the same
AirPlay receiver the TV uses for iPhone mirroring, so it works on TVs where Windows' own
Miracast (`Win + K`) finds the TV but can't connect. Everything here is free and open source.

The AirPlay sender is [open-airplay](https://github.com/parsakhaz/open-airplay), a Windows port
of [doubletake](https://github.com/omarroth/doubletake). This repo builds it from a pinned commit,
adds two Samsung fixes in [`patches/`](patches/), and wraps it in setup scripts.

## Status

Mirroring works on a Samsung 7 Series 55" (model UNU710D, 2018): a live 1920x1080 desktop at
30 fps, the PC's sound from the TV speakers, and no pairing code after the first connection.
That is the only TV tested so far.

Mirroring currently runs from the command line. A window with Start and Stop buttons, a
diagnostics bundle, and a browser fallback are built but not merged yet. [`PLAN.md`](PLAN.md)
tracks what is verified and what comes next.

## Before you start

You need:

- Windows 10 or 11, with `git` and `winget`. Setup installs Go 1.27.0 and FFmpeg 9.0.2 with
  `winget` if they are missing.
- A TV that mirrors from an iPhone with AirPlay. Samsung TVs from 2018 onward support it.
- The PC and the TV on the same home network. The PC can be on 5 GHz and the TV on 2.4 GHz, as
  long as both connect to the same router and not to a guest network.

On the TV, open **Settings > General > Apple AirPlay Settings** and set **Require Code** to
**First Time Only**. With **Every Time**, the TV asks for a new code on every connection.

## Set up

1. Clone the repo:

   ```bash
   git clone https://github.com/tonesjones/airplay-windows
   ```

2. In PowerShell, from the repo folder, run setup:

   ```bash
   .\setup.ps1
   ```

   Setup installs Go and FFmpeg if needed, then builds `bin\open-airplay.exe`. Windows may ask
   for admin approval while Go installs. If PowerShell refuses to run the script, run
   `powershell -ExecutionPolicy Bypass -File .\setup.ps1` instead. That bypasses the policy for
   this one run only.

You only run setup once. Later runs rebuild only when the pinned commit or the patches change.
Don't run setup while you're mirroring, because it rebuilds the sender that's running.

## Mirror your desktop

1. Wake the PC's screen. The PC can't capture its screen while the monitor is asleep.
2. Start the sender:

   ```bash
   .\bin\open-airplay.exe -display 0 -hwaccel nvenc
   ```

   Use `-hwaccel nvenc` on an NVIDIA graphics card. On other cards, leave the flag out.
3. Pick your TV from the list that the sender prints.
4. The first time you connect, the TV shows a 4-digit code. Type it into PowerShell and press
   **Enter**. The sender saves the pairing in Windows Credential Manager, so later connections
   skip this step.
5. The first time you mirror, Windows Firewall asks about `open-airplay.exe`. Allow it.
   Mirroring needs the TV to reach the PC.

The TV switches to your desktop within a few seconds. Sound plays from the TV. The TV keeps its
own volume, so use the TV remote to change it.

To stop mirroring, press **Ctrl+C** in PowerShell.

To list your displays or the AirPlay devices on your network, run
`.\bin\open-airplay.exe -list-displays` or `.\bin\open-airplay.exe -list-receivers`.

## Fix common problems

**The TV isn't in the list.**
- Check that the TV is on.
- Check that AirPlay is on in **Settings > General > Apple AirPlay Settings**.
- Check that the PC is on the same network as the TV.
- If mirroring ended badly earlier, the TV can stop announcing itself. Turn the TV off and on
  with the remote, then wait up to two minutes before you try again.

**The sender says the capture "failed startup validation".** The PC's monitor is asleep. Move
the mouse, then start again.

**The TV shows the code, but pairing fails.** After a wrong code, the TV drops the attempt.
Stop with **Ctrl+C**, start again, and type the new code the TV shows.

**The picture appears, then the TV goes back to its previous input.** On the Samsung that was
tested, this happened once with software encoding and not with `-hwaccel nvenc`. Restart the TV
and try with `-hwaccel nvenc`. The cause isn't confirmed yet.

**The TV is found, but the connection fails.** Windows Firewall may be blocking
`open-airplay.exe`. Open **Windows Security > Firewall & network protection > Allow an app
through firewall** and allow `open-airplay.exe`. If your Wi-Fi is set to **Public**, consider
setting it to **Private** under **Settings > Network & internet > Wi-Fi**, since this is your
home network.

## Limits

- One TV model is tested. Other AirPlay TVs may need fixes like the ones in `patches/`.
- The PC monitor must stay awake. Windows turns it off after its idle timeout, and the TV picture
  then freezes. The planned window app keeps the display awake while mirroring.
- The tested TV accepts up to 1920x1080, so a 1440p desktop is scaled down to fit.
- Streaming apps that protect their video can show a black area on the TV.

## How it works

The sender finds the TV with mDNS and pairs with the code the TV shows. FairPlay isn't needed,
because Samsung TVs don't ask for it. FFmpeg captures the screen through Windows Desktop
Duplication (`ddagrab`) and encodes it as H.264. The sender captures the PC's sound with WASAPI
loopback and sends it as ALAC. Video and sound go to the TV encrypted with ChaCha20-Poly1305.

The two patches fix problems found on Samsung TVs:

- `0001-ntp-fallback.patch` ports doubletake PR #61. Some Samsung TVs advertise one clock-sync
  method (PTP) but only work with another (NTP), and the patch retries with NTP.
- `0002-never-set-receiver-volume.patch` stops the sender from setting the TV's volume.
  Unpatched, it set the volume to maximum.

## License

The code in this repo is MIT licensed (see [`LICENSE`](LICENSE)). `setup.ps1` builds
`bin\open-airplay.exe` on your PC from open-airplay, which is GPL-3.0, and from doubletake, which
is LGPL-3.0. Neither is distributed from this repo. The files in `patches/` carry their upstream
licenses.
