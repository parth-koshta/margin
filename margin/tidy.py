"""margin tidy: sort out what the nightly sync couldn't, one question at a time.

1. Payees with no category: pick one by number and a rule is saved, so past and future payments to
   that payee are sorted.
2. Senders that aren't a known bank: say whether each is a bank/card issuer (count it) or not
   (receipts, newsletters: stop showing it in Needs a look).

It shows payee names and amounts, so it only runs in a real terminal, never through a chat.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys

from . import categorize, db, money, parse, paths, sync

SPEND = ["Food & dining", "Groceries", "Shopping", "Transport", "Car", "Travel", "Rent & housing",
         "Bills & utilities", "Subscriptions", "Home services", "Medical", "Health & fitness", "Entertainment",
         "Education", "Insurance", "Loans & EMI", "Bank charges", "Cash", "Friends & family",
         "Miscellaneous", "Other spending"]   # Miscellaneous = known but not worth a category; Other = not sorted yet
INCOME = ["Salary", "Interest & dividends", "Refunds & cashback", "Friends & family", "Other income"]
BOTH = ["Transfer", "Investments"]   # your own money moving: not income or spending


def _own_categories() -> dict:
    """Your own categories, from settings.json: "extra_categories": {"out": [...], "in": [...]}, plus
    "project_categories" (big one-off projects such as renovating a home, counted as spending but shown apart)."""
    try:
        s = json.loads(paths.SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        s = {}
    extra = s.get("extra_categories") or {}
    return {"out": list(extra.get("out", [])) + list(s.get("project_categories", [])), "in": list(extra.get("in", []))}


_OWN = _own_categories()
SPEND = SPEND[:-2] + [c for c in dict.fromkeys(_OWN["out"]) if c not in SPEND] + SPEND[-2:]
INCOME = INCOME[:-1] + [c for c in dict.fromkeys(_OWN["in"]) if c not in INCOME] + INCOME[-1:]


def inr(x: float) -> str:
    """An amount in the home currency, written its own way (₹1,23,456 / $123,456)."""
    from . import money
    return money.fmt(abs(x), money.configured(), 0)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


def _menu(direction: str) -> list[str]:
    cats = (SPEND if direction == "out" else INCOME) + BOTH
    width = max(len(c) for c in cats) + 6
    cols = 3
    rows = (len(cats) + cols - 1) // cols
    for r in range(rows):
        cells = [f"{i + 1:>3} {cats[i]}".ljust(width) for i in range(r, len(cats), rows)]
        print("  " + "".join(cells))
    return cats


# Sort out asks only about the last year. Older uncategorized payments (e.g. years of texts that arrived from
# iCloud) stay Other spending / Other income; any of them can still be changed in Transactions, and a rule made
# here applies to old payments too.
RECENT = "date('now', '-12 months')"


def _payee_groups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"""SELECT LOWER(COALESCE(NULLIF(counterparty,''), vpa)) AS k, direction,
                  MAX(COALESCE(NULLIF(counterparty,''), vpa)) AS name, COUNT(*) AS n, SUM(amount) AS total,
                  MAX(ts) AS last
           FROM transactions
           WHERE duplicate_of IS NULL AND category IN ('Other spending','Other income') AND ts >= {RECENT}
             AND COALESCE(NULLIF(counterparty,''), vpa, '') != ''
           GROUP BY k, direction ORDER BY n DESC, total DESC""").fetchall()


def payee_list(conn: sqlite3.Connection) -> list[dict]:
    return [{"key": r["k"], "dir": r["direction"], "name": r["name"], "n": r["n"], "total": round(r["total"], 2),
             "last": r["last"][:10]} for r in _payee_groups(conn)]


def uncategorized_counts(conn: sqlite3.Connection) -> dict:
    left = conn.execute(f"""SELECT COUNT(*) FROM transactions WHERE duplicate_of IS NULL
                           AND category IN ('Other spending','Other income') AND ts >= {RECENT}""").fetchone()[0]
    named = sum(r["n"] for r in _payee_groups(conn))
    return {"uncategorized": left, "nameless": left - named}


def sender_list(conn: sqlite3.Connection, settings: dict) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in conn.execute("SELECT sender, ts, snippet FROM review WHERE reason LIKE 'sender isn%' ORDER BY ts DESC"):
        key = parse.sender_key(r["sender"])
        if key in settings["ignored_senders"]:
            continue
        g = groups.setdefault(key, {"key": key, "n": 0, "pay": 0, "example": None, "any": r["snippet"], "last": r["ts"][:10]})
        g["n"] += 1
        if parse.parse_alert(r["snippet"] or "", "", parse.currency_for(None, money.home(settings))).txns:
            g["pay"] += 1
            g["example"] = g["example"] or r["snippet"]
    out = sorted(groups.values(), key=lambda g: (-g["pay"], -g["n"]))
    for g in out:
        ex = re.sub(r"<[^>]*>?", " ", g.pop("example") or g.pop("any", "") or "")   # HTML bits in emails
        g["example"] = re.sub(r"\s+", " ", ex).strip()[:160]
        g.pop("any", None)
    return out


def payees(conn: sqlite3.Connection, settings: dict) -> int:
    rows = _payee_groups(conn)
    left = conn.execute("""SELECT COUNT(*) FROM transactions WHERE duplicate_of IS NULL
                           AND category IN ('Other spending','Other income')""").fetchone()[0]
    nameless = left - sum(r["n"] for r in rows)
    print(f"\n── Step 1 of 2: payees without a category ── {left:,} payments"
          + (f" ({nameless:,} have no payee name; those need a parser fix, not a rule)" if nameless else ""))
    print("Pick a category number. Enter = skip, k = match on part of the name (e.g. 'swiggy'), ? = show categories, q = next step.\n")
    added, skipped, i, shown_for = 0, set(), 0, None
    while True:
        rows = [r for r in _payee_groups(conn) if (r["k"], r["direction"]) not in skipped]
        if not rows:
            print("No uncategorized payees left.")
            return added
        r = rows[0]
        i += 1
        kind = "spent" if r["direction"] == "out" else "received"
        print(f"[{i}] {r['name']}  ·  {r['n']} payments, {inr(r['total'])} {kind}, last {r['last'][:10]}"
              f"  ({len(rows) - 1} more after this)")
        if shown_for != r["direction"]:
            cats = _menu(r["direction"])
            shown_for = r["direction"]
        else:
            cats = (SPEND if r["direction"] == "out" else INCOME) + BOTH
        keyword = r["name"]
        while True:
            a = _ask("  > ").lower()
            if a == "q":
                return added
            if a in ("", "s"):
                skipped.add((r["k"], r["direction"]))
                break
            if a == "?":
                _menu(r["direction"])
                continue
            if a == "k":
                kw = _ask(f"  match on (part of '{r['name']}'): ").strip()
                if kw and kw.lower() in r["name"].lower():
                    keyword = kw
                    print(f"  matching on '{keyword}' — now pick a category")
                else:
                    print("  that text isn't part of the name; keeping the full name")
                continue
            if a.isdigit() and 1 <= int(a) <= len(cats):
                cat = cats[int(a) - 1]
                categorize.add_user_rule(keyword, cat, r["direction"], match_text=False)
                sync.recategorize(conn, settings)
                conn.commit()
                added += 1
                still = conn.execute(
                    """SELECT COUNT(*) FROM transactions WHERE duplicate_of IS NULL AND category=?""",
                    (cat,)).fetchone()[0]
                print(f"  ✓ '{keyword}' → {cat}  ({still:,} payments in {cat} now)\n")
                break
            print("  type a number from the list, Enter to skip, k or q")


def trust_sender(conn: sqlite3.Connection, settings: dict, token: str) -> int:
    """Count this sender as a bank from now on, and re-read what it already sent. Returns messages queued."""
    if token not in settings["trusted_senders"]:
        settings["trusted_senders"].append(token)
    settings["ignored_senders"] = [s for s in settings["ignored_senders"] if s != token]
    paths.SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    rows = [r for r in conn.execute("SELECT uid, source, sender FROM review")
            if token.upper() in (r["sender"] or "").upper() or parse.sender_key(r["sender"]) == token]
    emails = [r["uid"].split(":", 1)[1] for r in rows if r["source"] == "email"]
    if emails:
        queued = json.loads(db.get_state(conn, "refetch_email_ids") or "[]")
        db.set_state(conn, "refetch_email_ids", json.dumps(sorted(set(queued) | set(emails))))
    if any(r["source"] == "sms" for r in rows):
        conn.execute("DELETE FROM state WHERE key='sms_after_rowid'")   # re-scan texts; 'seen' skips the rest
    for r in rows:
        conn.execute("DELETE FROM seen WHERE uid=?", (r["uid"],))
        conn.execute("DELETE FROM review WHERE uid=?", (r["uid"],))
    conn.commit()
    return len(rows)


def ignore_sender(conn: sqlite3.Connection, settings: dict, token: str) -> int:
    """Hide a sender from Needs a look / Sort out. Its rows stay, so this can be undone."""
    if token not in settings["ignored_senders"]:
        settings["ignored_senders"].append(token)
    paths.SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")
    return sum(1 for r in conn.execute("SELECT sender FROM review WHERE reason LIKE 'sender isn%'")
               if parse.sender_key(r["sender"]) == token)


def senders(conn: sqlite3.Connection, settings: dict) -> tuple[int, int, int]:
    groups: dict[str, dict] = {}
    for r in conn.execute("SELECT sender, ts, snippet FROM review WHERE reason LIKE 'sender isn%' ORDER BY ts DESC"):
        key = parse.sender_key(r["sender"])
        if key in settings["ignored_senders"]:
            continue
        g = groups.setdefault(key, {"n": 0, "pay": 0, "example": None, "label": r["sender"]})
        g["n"] += 1
        if parse.parse_alert(r["snippet"] or "", "", parse.currency_for(None, money.home(settings))).txns:
            g["pay"] += 1
            g["example"] = g["example"] or r["snippet"]
        g["example_any"] = g.get("example_any") or r["snippet"]
    order = sorted(groups.items(), key=lambda kv: (-kv[1]["pay"], -kv[1]["n"]))
    print(f"\n── Step 2 of 2: senders that aren't a known bank ── {len(order)} senders")
    print("Only banks and card issuers should count. A merchant's receipt (Swiggy, Amazon, Uber...) repeats a")
    print("payment your bank already reported, so counting it would double it.")
    print("t = it's my bank/card, count it · i = not a bank, stop showing it · Enter = skip · q = finish\n")
    trusted = ignored = queued = 0
    for i, (key, g) in enumerate(order, 1):
        ex = (g["example"] or g["example_any"] or "")[:140]
        print(f"[{i}/{len(order)}] {key}  ·  {g['n']} messages, {g['pay']} read like payments")
        print(f"    e.g. {ex}")
        while True:
            a = _ask("  > ").lower()
            if a == "q":
                return trusted, ignored, queued
            if a in ("", "s"):
                break
            if a == "t":
                queued += trust_sender(conn, settings, key)
                trusted += 1
                print("  ✓ counted from now on\n")
                break
            if a == "i":
                ignore_sender(conn, settings, key)
                ignored += 1
                print("  ✓ hidden\n")
                break
            print("  t, i, Enter or q")
    return trusted, ignored, queued


def run() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("margin tidy shows payee names and amounts, so it only runs in a Terminal window you type into,\n"
              "not through a chat or a script. Open Terminal (or the app's Terminal pane) and run: margin tidy")
        return 2
    conn = db.connect()
    settings = sync.load_settings()
    print("Margin tidy: sorts what the nightly sync couldn't. Everything stays on this Mac. q moves on at any time.")
    added = payees(conn, settings)
    trusted, ignored, queued = senders(conn, settings)
    sync.recategorize(conn, settings)
    conn.commit()
    sync.refresh_dashboard(conn)
    conn.commit()
    print(f"\nDone: {added} payee rules, {trusted} senders counted, {ignored} hidden. Dashboard updated.")
    if trusted:
        print(f"{queued} messages from the senders you counted get read on the next sync: run margin sync.")
    return 0
