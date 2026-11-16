"""What `margin doctor` checks, for people (text) and for Claude Code (`--json`).

The JSON is safe to put in a conversation: statuses, yes/no answers, counts and fixed instructions only. Never
amounts, payees, message text, account numbers, names or the text of sync problems (those can quote an error);
problems are counted by area ("Gmail", "SMS", ...). Doctor only reads: it never writes settings or opens the
database for writing.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime

from . import config, paths

SECRETS = {"pan": "margin setup-pan", "pran": "margin setup-pran", "ppf_statement": "margin setup-ppf"}
AREAS = {"Gmail", "SMS", "Claude sorting", "Investments", "Statements", "Review"}   # where sync problems come from


def python() -> str:
    """The Python the nightly job runs: the one that needs Full Disk Access for bank texts."""
    return os.path.realpath(sys.executable)


def _loaded(label: str) -> bool:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
    return r.returncode == 0


def sync_running() -> bool:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{paths.LAUNCHD_LABEL}"], capture_output=True,
                       text=True)
    return r.returncode == 0 and "state = running" in r.stdout


def _db() -> sqlite3.Connection | None:
    if not paths.DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _state(conn, key: str):
    try:
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None
    except (sqlite3.Error, ValueError):
        return None


def _git(*args: str) -> str | None:
    r = subprocess.run(["git", "-C", str(paths.PROJECT_DIR), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def check_sms(settings: dict, last_sms: str | None) -> dict:
    from . import sms
    if settings.get("read_sms") is False:
        return {"id": "sms", "ok": True, "text": "Bank SMS off (read_sms is false in settings.json)", "state": "off"}
    if last_sms == "ok" or sms.available()[0]:
        return {"id": "sms", "ok": True, "text": "Bank SMS readable by the sync job", "state": "ok"}
    return {"id": "sms", "ok": False, "state": "no_access",
            "text": "Bank SMS: the sync job couldn't read Messages yet. Give Full Disk Access to "
                    f"{python()} (System Settings > Privacy & Security > Full Disk Access), then run margin sync.",
            "fix": f"the person gives Full Disk Access to {python()}, or: margin config set read_sms false"}


def last_sync() -> dict | None:
    conn = _db()
    if not conn:
        return None
    try:
        return _state(conn, "last_sync")
    finally:
        conn.close()


def secrets_saved() -> dict:
    """Which of Margin's statement passwords are in the Keychain (yes/no; the values are never read out)."""
    from . import keychain
    out = {}
    for account in SECRETS:
        try:
            out[account] = bool(keychain.get_secret(account))
        except OSError:
            out[account] = False
    return out


def checks() -> list[dict]:
    """The doctor's checks in order: {id, ok, text (for people), fix?, ...}."""
    from . import gmail
    out = []

    def add(id_: str, ok: bool, text: str, **extra) -> dict:
        c = {"id": id_, "ok": bool(ok), "text": text, **extra}
        out.append(c)
        return c

    folder_ok = paths.DATA_DIR.exists() and (paths.DATA_DIR.stat().st_mode & 0o077) == 0
    add("data_folder", folder_ok, f"Private data folder (only you can open it): {paths.DATA_DIR}",
        **({} if folder_ok else {"fix": "margin setup"}))
    connected = gmail.is_configured()
    add("gmail", connected, "Gmail connected (read-only, token in Keychain)" if connected
        else "Gmail not connected: margin setup-gmail <client_secret.json>",
        **({} if connected else {"fix": "margin setup (it walks through the Google sign-in)"}))
    if connected:
        try:
            gmail.access_token()
            add("gmail_token", True, "Gmail token still valid")
        except gmail.AuthError as e:
            add("gmail_token", False, f"Gmail token problem: {e}",
                fix="the person removes margin at https://myaccount.google.com/permissions, then "
                    "margin setup-gmail <client_secret.json> again")
    settings = config.read()
    conn = _db()
    last = _state(conn, "last_sync") if conn else None
    last_sms = (last or {}).get("sms")       # Terminal has other permissions: judge by what the sync job saw
    out.append(check_sms(settings, last_sms))
    scheduled = _loaded(paths.LAUNCHD_LABEL)
    add("schedule", scheduled, "Nightly 00:00 sync scheduled" if scheduled else
        "Nightly sync not scheduled: margin schedule install", **({} if scheduled else {"fix": "margin setup"}))
    if conn:
        if last:
            age_h = (datetime.now() - datetime.fromisoformat(last["at"])).total_seconds() / 3600
            areas: dict = {}
            for p in last.get("problems", []):
                area = re.split(r"[:,(]", p, 1)[0].strip()
                area = area if area in AREAS else "Other"
                areas[area] = areas.get(area, 0) + 1
            add("last_sync", last["ok"] and age_h < 30,
                f"Last sync {last['at']} ({'ok' if last['ok'] else 'had problems'})"
                + ("" if age_h < 30 else " — more than a day ago"),
                at=last["at"], hours_ago=round(age_h, 1), problems=len(last.get("problems", [])),
                problem_areas=areas, lines=[f"    · {p}" for p in last.get("problems", [])])
        else:
            add("last_sync", False, "Never synced: margin sync", fix="margin sync")
        conn.close()
    return out


def _count(conn, sql: str) -> int | None:
    try:
        return conn.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return None


def counts() -> dict:
    """How much is there, as numbers only."""
    out: dict = {}
    try:
        out["rules"] = len(json.loads(paths.RULES_PATH.read_text()))
    except (OSError, ValueError, TypeError):
        out["rules"] = 0
    conn = _db()
    if not conn:
        return out
    from . import tidy
    out["transactions"] = _count(conn, "SELECT COUNT(*) FROM transactions WHERE duplicate_of IS NULL")
    out["accounts"] = _count(conn, "SELECT COUNT(DISTINCT account) FROM transactions WHERE account IS NOT NULL")
    out["not_counted_to_look_at"] = _count(conn, "SELECT COUNT(*) FROM review")
    out["reviews"] = _count(conn, "SELECT COUNT(*) FROM reviews")
    out["investment_statements_read"] = _count(conn, "SELECT COUNT(*) FROM inv_statements WHERE ok=1")
    try:
        out["uncategorized_last_year"] = tidy.uncategorized_counts(conn)["uncategorized"]
        out["unknown_senders"] = len(tidy.sender_list(conn, config.read()))
    except (sqlite3.Error, KeyError):
        pass
    conn.close()
    return out


def report(app_path: str) -> dict:
    """Everything `margin doctor --json` prints."""
    from . import gmail
    found = checks()
    for c in found:
        c.pop("text", None)
        c.pop("lines", None)
    settings = config.read()
    progress = None
    if sync_running():
        try:
            p = json.loads(paths.PROGRESS_PATH.read_text())
            progress = {"phase": p.get("phase"), "done": p.get("done"), "total": p.get("total")}
        except (OSError, ValueError):
            progress = {}
    secrets = {"gmail": gmail.is_configured(), **secrets_saved()}
    return {
        "ok": all(c["ok"] for c in found),
        "checks": found,
        "counts": counts(),
        "settings": {
            "exists": paths.SETTINGS_PATH.exists(),
            **{k: settings.get(k) for k in ("currency", "read_sms", "ai_sort", "ai_review", "lock_minutes",
                                            "misc_below")},
            "personal": {k: len(settings.get(k) or []) for k in sorted(config.PERSONAL)},
        },
        "secrets_saved": secrets,
        "sync": {"running": progress is not None, "progress": progress},
        "app": {"installed": os.path.exists(app_path), "page_service": _loaded(paths.WEB_LABEL)},
        "margin": {"commit": _git("rev-parse", "--short", "HEAD"), "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                   "folder": str(paths.PROJECT_DIR)},
        "python_for_full_disk_access": python(),
    }


def main(json_out: bool, app_path: str) -> int:
    if json_out:
        r = report(app_path)
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    ok = True
    for c in checks():
        ok &= c["ok"]
        print(("✓ " if c["ok"] else "✗ ") + c["text"])
        for line in c.get("lines", []):
            print(line)
    return 0 if ok else 1
