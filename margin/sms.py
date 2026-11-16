"""Bank SMS from this Mac's Messages app (needs iPhone text forwarding + Full Disk Access)."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone

from . import paths

APPLE_EPOCH = 978307200  # 2001-01-01 in unix time


class NoAccess(RuntimeError):
    pass


def _decode_attributed(blob: bytes | None) -> str | None:
    """Newer macOS stores message text only inside an NSAttributedString typedstream."""
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    j = blob.find(b"+", i + 8)
    if j < 0:
        return None
    j += 1
    n = blob[j]
    if n == 0x81:
        n = int.from_bytes(blob[j + 1:j + 3], "little"); j += 3
    elif n == 0x82:
        n = int.from_bytes(blob[j + 1:j + 4], "little"); j += 4
    else:
        j += 1
    return blob[j:j + n].decode("utf-8", errors="replace")


def _to_epoch(apple_date: int) -> float:
    if apple_date > 10**12:          # nanoseconds since 2001
        apple_date = apple_date / 1e9
    return apple_date + APPLE_EPOCH


def available() -> tuple[bool, str]:
    if not paths.MESSAGES_DB.exists():
        return False, "No Messages database on this Mac."
    try:
        with open(paths.MESSAGES_DB, "rb") as f:
            f.read(16)
        return True, "ok"
    except PermissionError:
        return False, "No permission to read Messages (Full Disk Access needed)."


def fetch_since(after_rowid: int):
    """Yield dicts {id, rowid, sender, ts_epoch, text} for incoming texts from non-phone-number senders."""
    ok, why = available()
    if not ok:
        raise NoAccess(why)
    tmp = paths.TMP_DIR / "chat-copy"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for suffix in ("", "-wal", "-shm"):
            src = paths.MESSAGES_DB.with_name("chat.db" + suffix)
            if src.exists():
                shutil.copy2(src, tmp / ("chat.db" + suffix))
        conn = sqlite3.connect(f"file:{tmp / 'chat.db'}?mode=ro", uri=True)
        rows = conn.execute(
            """SELECT m.ROWID, m.guid, m.date, m.text, m.attributedBody, h.id
               FROM message m LEFT JOIN handle h ON m.handle_id = h.ROWID
               WHERE m.is_from_me = 0 AND m.ROWID > ? ORDER BY m.ROWID""", (after_rowid,)).fetchall()
        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for rowid, guid, date, text, body, sender in rows:
        sender = sender or ""
        # Bank alerts come from alphanumeric sender IDs (e.g. JD-HDFCBK-S), not phone numbers or emails.
        if not any(c.isalpha() for c in sender) or "@" in sender:
            continue
        text = text or _decode_attributed(body)
        if not text:
            continue
        yield {"id": guid, "rowid": rowid, "sender": sender, "ts_epoch": _to_epoch(date or 0), "text": text}


def max_rowid_before(epoch: float) -> int:
    """Row to start from on first run (so a backfill covers the same window as email)."""
    ok, _ = available()
    if not ok:
        return 0
    tmp = paths.TMP_DIR / "chat-copy"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for suffix in ("", "-wal", "-shm"):
            src = paths.MESSAGES_DB.with_name("chat.db" + suffix)
            if src.exists():
                shutil.copy2(src, tmp / ("chat.db" + suffix))
        conn = sqlite3.connect(f"file:{tmp / 'chat.db'}?mode=ro", uri=True)
        apple_ns = int((epoch - APPLE_EPOCH) * 1e9)
        row = conn.execute("SELECT MAX(ROWID) FROM message WHERE date < ?", (apple_ns,)).fetchone()
        conn.close()
        return row[0] or 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def iso_local(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
