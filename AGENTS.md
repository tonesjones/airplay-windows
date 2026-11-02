# AirMirror development

This is a personal Windows desktop/audio mirroring tool. Read README.md and PLAN.md for what is verified and what remains hardware-dependent. The Python UI uses the standard library; setup builds the pinned upstream sender and applies the repository patches.

Use the existing development environment: `ruff check .`, `ruff format --check .`, and `python -m pytest -q` match CI. Simulated sender tests do not establish that a real TV session worked.

Setup may install Go/FFmpeg and rebuild the sender. Do not run setup while mirroring. Starting a live TV session can expose the desktop and audio; obtain the user's authorization for that session instead of starting one as routine verification. Keep network setup, pairing credentials, and local settings private.
