"""Where everything lives. Code is in the project folder; data never is."""
from __future__ import annotations

import getpass
import os
import re
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MARGIN_DATA", Path.home() / "Library" / "Application Support" / "Margin"))
DB_PATH = DATA_DIR / "money.db"
RULES_PATH = DATA_DIR / "rules.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
ASSETS_PATH = DATA_DIR / "assets.csv"
STATEMENTS_DIR = DATA_DIR / "statements"
PROGRESS_PATH = DATA_DIR / "progress.json"
LOG_DIR = DATA_DIR / "logs"
TMP_DIR = DATA_DIR / "tmp"

_USER = re.sub(r"[^a-z0-9-]", "", getpass.getuser().lower()) or "user"
LAUNCHD_LABEL = f"com.{_USER}.margin"           # the nightly sync (launchd)
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
WEB_LABEL = f"{LAUNCHD_LABEL}.web"      # keeps http://margin.localhost/ available (starts the page server on demand)
WEB_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{WEB_LABEL}.plist"
MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"


def ensure_data_dir() -> None:
    """Create the data folder readable only by this Mac user (0700 dirs, 0600 files)."""
    os.umask(0o077)
    for d in (DATA_DIR, STATEMENTS_DIR, LOG_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    for f in DATA_DIR.rglob("*"):       # anything written before (or by another tool) gets the same treatment
        try:
            mode = f.stat().st_mode & 0o777
            want = (0o700 if f.is_dir() or mode & 0o100 else 0o600)
            if mode != want:
                os.chmod(f, want)
        except OSError:
            pass
    if not ASSETS_PATH.exists():
        ASSETS_PATH.write_text(
            "# Things the tracker can't see in email/SMS. kind = asset or liability. value in your home currency.\n"
            "# Edit freely; the nightly sync reads this file every time.\n"
            "name,kind,value,as_of\n"
            "# Gold coins,asset,0,2026-11-01\n"
            "# Car (resale value),asset,0,2026-11-01\n"
            "# Education loan,liability,0,2026-11-01\n"
        )
