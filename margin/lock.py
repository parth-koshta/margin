"""Margin's lock: after you've been away from the page for a while (2 minutes by default), Margin asks for Touch ID
(or your Mac password when Touch ID isn't available) before it shows anything again.

The server holds the lock, so it isn't just a cover over the page: while locked it sends no data at all. It starts
locked. The open page tells it "still here" every 30 seconds while it's in front of you; when that stops (you switched
tab or app, or the Mac slept), the server locks once the time is up, and the page reloads to the lock screen so
nothing stays in the browser. "lock_minutes" in settings.json sets the time (0 turns the lock off).

The prompt is macOS's own (LocalAuthentication), from the small Swift program next to this file, compiled once into
the data folder like the PDF reader.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from . import paths

SOURCE = Path(__file__).with_name("touchid.swift")
DEFAULT_MINUTES = 2
_STATE = {"until": 0.0}                  # shared by the IPv4 and IPv6 servers; starts locked
_ASKING = threading.Lock()                # one prompt at a time


def _binary() -> Path:
    out = paths.DATA_DIR / "bin" / "Margin"          # macOS names the program in the prompt: "Margin"
    if not out.exists() or out.stat().st_mtime < SOURCE.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["/usr/bin/swiftc", "-O", str(SOURCE), "-o", str(out)], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"couldn't build the Touch ID helper: {r.stderr.strip()[:300]}")
    return out


def minutes(settings: dict) -> float:
    try:
        return max(0.0, float(settings.get("lock_minutes", DEFAULT_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_MINUTES


def locked(settings: dict) -> bool:
    return minutes(settings) > 0 and time.time() >= _STATE["until"]


def still_here(settings: dict) -> bool:
    """The page is in front of you: keep it unlocked for another lock period. Returns whether it's locked."""
    if locked(settings):
        return True
    _STATE["until"] = time.time() + minutes(settings) * 60
    return False


def lock_now() -> None:
    _STATE["until"] = 0.0


def available() -> str:
    """'touchid', 'password', or why neither can be used."""
    r = subprocess.run([str(_binary()), "--check"], capture_output=True, text=True, timeout=20)
    return r.stdout.strip() if r.returncode == 0 else (r.stderr.strip() or "unavailable")


def unlock(settings: dict) -> tuple[bool, str]:
    """Show the Touch ID prompt. (unlocked?, why not)."""
    if not _ASKING.acquire(blocking=False):
        return False, "Touch ID is already asking"
    try:
        r = subprocess.run([str(_binary()), "show your money"], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "Touch ID timed out"
    finally:
        _ASKING.release()
    if r.returncode == 0:
        _STATE["until"] = time.time() + minutes(settings) * 60
        return True, ""
    if r.returncode == 1:
        return False, "Cancelled"
    if r.returncode == 3:
        return False, "Touch ID and your Mac password aren't available right now"
    code, _, why = (r.stderr or "").strip().partition(" ")
    if code == "-4":
        return False, "macOS cancelled the prompt (another window came to the front). Try again."
    return False, why or "Not unlocked"
