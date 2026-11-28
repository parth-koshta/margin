"""Names you give payees.

Some alerts don't say who was paid. A UPI payment made with a credit card arrives as only the masked end of a UPI
id ("to ********fruit9@okaxis"), and some card alerts print a merchant code. The UPI app shows a name because it
looks the id up online; Margin never sends ids anywhere to look them up. Instead you name the payee once, on the
Sort out page or a payment in Transactions, and every payment to it, past and future, shows that name.

The names are kept on this Mac (the payee_names table) and put on the payments themselves: counterparty becomes
the name, and payee_was keeps the alert's own wording, so a name can be changed or taken back and rules made on
the old wording still match (sync.recategorize). Two payees given the same name are shown as one. Like any payee
name, a name you give is part of what your AI sees when it sorts payments or writes a review.
"""
from __future__ import annotations

import re
import sqlite3

# A payment's payee as its alert named it, lowercased: the key a name is saved under.
ORIG = "LOWER(COALESCE(NULLIF(COALESCE(payee_was, counterparty), ''), vpa, ''))"
MAX_NAME = 48


def clean(name) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(name or ""))).strip()[:MAX_NAME].strip()


def apply(conn: sqlite3.Connection) -> int:
    """Puts the names on the payments that need them (new ones included) and takes back the ones no longer named.
    Returns how many payments changed."""
    names = dict(conn.execute("SELECT key, name FROM payee_names").fetchall())
    changed = 0
    for r in conn.execute(f"""SELECT id, counterparty, payee_was, {ORIG} AS k FROM transactions
                              WHERE payee_was IS NOT NULL OR {ORIG} IN (SELECT key FROM payee_names)""").fetchall():
        name = names.get(r["k"])
        if name is None:            # taken back: the alert's own wording returns
            conn.execute("UPDATE transactions SET counterparty=NULLIF(payee_was, ''), payee_was=NULL WHERE id=?",
                         (r["id"],))
        elif r["payee_was"] is None:
            conn.execute("UPDATE transactions SET payee_was=COALESCE(counterparty, ''), counterparty=? WHERE id=?",
                         (name, r["id"]))
        elif r["counterparty"] != name:
            conn.execute("UPDATE transactions SET counterparty=? WHERE id=?", (name, r["id"]))
        else:
            continue
        changed += 1
    return changed


def set_name(conn: sqlite3.Connection, key: str, name: str) -> dict:
    """Names the payee the page shows as `key` (its name, lowercased); an empty name takes it back. Payees you
    already gave that name go with it, so they stay together. Returns the names as they were, for restore (undo)."""
    key, name = str(key or "").strip().lower(), clean(name)
    if not key:
        return {}
    existing = dict(conn.execute("SELECT key, name FROM payee_names").fetchall())
    keys = {k for k, n in existing.items() if n.lower() == key}
    if conn.execute(f"SELECT 1 FROM transactions WHERE {ORIG}=? LIMIT 1", (key,)).fetchone():
        keys.add(key)
    before = {k: existing.get(k, "") for k in keys}
    restore(conn, {k: name for k in keys})
    return before


def restore(conn: sqlite3.Connection, names: dict) -> None:
    """Sets each payee's name exactly ('' takes it back), then updates the payments."""
    for k, n in names.items():
        k, n = str(k).strip().lower(), clean(n)
        if not k:
            continue
        if n:
            conn.execute("INSERT OR REPLACE INTO payee_names(key, name) VALUES(?,?)", (k, n))
        else:
            conn.execute("DELETE FROM payee_names WHERE key=?", (k,))
    apply(conn)
