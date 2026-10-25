"""Mirror the Windows desktop to an AirPlay receiver through open-airplay's sender."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SENDER_EXE = REPO_ROOT / "bin" / "open-airplay.exe"
