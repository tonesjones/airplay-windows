# Browser fallback

If native AirPlay does not work with the TV, this serves the PC desktop as a web page. Open the page in the TV's built-in browser. It needs no software on the TV and no pairing.

## Use

```powershell
pwsh -NoProfile -File .\fallback\start-browser.ps1
```

Options: `-Display 0`, `-Port 8080`, `-Fps 15`, `-Width 1920`. Windows PowerShell 5.1 works too.

The script prints a URL like `http://192.168.0.20:8080/k3x9/`. Type it into the TV browser. Press Ctrl+C on the PC to stop.

- The first run shows a Windows Firewall prompt for `python.exe`. Tick **Private networks** and click Allow. If the network profile is Public, switch it to Private or the TV cannot connect.
- The 4-character token in the URL changes on every start. Requests without it get a 404, so others on the LAN cannot watch your screen.
- Pages under the token:
  - `/<token>/` shows the live stream (`stream.mjpg`, MJPEG).
  - `/<token>/?mode=poll` fetches `frame.jpg` repeatedly. Use it if the stream page stays black.
  - `/<token>/frame.jpg` is a single frame.
- Run without the script: `py -3 -m fallback.server --display 0 --port 8080 --fps 15 --width 1920 --quality 5`.
- The server stops the PC from sleeping or turning off the monitor while it runs. Capture returns no frames while the monitor is asleep.

Requires FFmpeg with `ddagrab` (`winget install Gyan.FFmpeg`).

## Known limits

- **Latency:** expect roughly 0.3 to 1 s. Fine for slides and browsing, poor for games or video.
- **Frame rate:** 15 fps at best. Over 2.4 GHz Wi-Fi a 1080p MJPEG stream (several MB/s at quality 5) may drop frames. Lower `-Width` (1280) or raise `--quality` (a higher number is lower quality) to help.
- **No audio.**
- **Typing the URL** with the TV remote is slow. Keep the token short as it is, and use the TV browser's bookmarks or history afterwards (the token changes on restart, though).
- **Tizen playback is unverified.** Written for old browsers (HTML4, ES5) but not yet tested on the 2018 TV. Tested only with a desktop browser.
