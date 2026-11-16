"""Net worth = latest bank balances + holdings files + assets.csv - card dues - liabilities."""
from __future__ import annotations

import csv
import json
import io
import re
import sqlite3
from datetime import date, datetime

from . import paths

NAME_COLS = re.compile(r"^(instrument|scheme(?: name)?|symbol|name|stock|fund|security|asset|holding)", re.I)
VALUE_COLS = re.compile(r"(cur(?:rent)?\.?\s*val(?:ue)?|market\s*value|present\s*value|value\s*\(|^value$|valuation)", re.I)
UNITS_COLS = re.compile(r"^(qty|quantity|units|balance units)", re.I)


def _num(s: str) -> float | None:
    s = re.sub(r"[^\d.\-]", "", (s or "").replace(",", ""))     # any currency symbol or code
    try:
        return float(s)
    except ValueError:
        return None


def import_holdings(conn: sqlite3.Connection) -> list[str]:
    """Re-read every CSV in statements/ as fixed values (a name and a current-value column). Bank and card
    downloads and broker positions files are read by imports.py instead (priced each night), so they're skipped
    here and never counted twice. Returns notes about files that couldn't be read."""
    from . import imports, money   # noqa: PLC0415
    notes = []
    home = money.configured()
    conn.execute("DELETE FROM holdings")
    for f in sorted(paths.STATEMENTS_DIR.glob("*.csv")):
        if imports.classify(f, home):
            continue
        text = f.read_text(errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        hdr_i = next((i for i, r in enumerate(rows[:30])
                      if any(NAME_COLS.search(c or "") for c in r) and any(VALUE_COLS.search(c or "") for c in r)), None)
        if hdr_i is None:
            notes.append(f"{f.name}: couldn't find a name column and a current-value column")
            continue
        hdr = rows[hdr_i]
        ni = next(i for i, c in enumerate(hdr) if NAME_COLS.search(c or ""))
        vi = next(i for i, c in enumerate(hdr) if VALUE_COLS.search(c or ""))
        ui = next((i for i, c in enumerate(hdr) if UNITS_COLS.search(c or "")), None)
        as_of = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        kind = "Mutual funds" if re.search(r"mf|mutual|cas|coin|kuvera", f.name, re.I) else "Stocks & funds"
        n = 0
        for r in rows[hdr_i + 1:]:
            if len(r) <= max(ni, vi) or not r[ni].strip() or re.match(r"total", r[ni].strip(), re.I):
                continue
            v = _num(r[vi])
            if v is None:
                continue
            units = _num(r[ui]) if ui is not None and ui < len(r) else None
            conn.execute("INSERT INTO holdings(source_file,name,type,units,value,as_of) VALUES(?,?,?,?,?,?)",
                         (f.name, r[ni].strip(), kind, units, v, as_of))
            n += 1
        if n == 0:
            notes.append(f"{f.name}: no rows with a value")
    return notes


def manual_items() -> list[dict]:
    items = []
    if not paths.ASSETS_PATH.exists():
        return items
    lines = [l for l in paths.ASSETS_PATH.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]
    for r in csv.DictReader(lines):
        v = _num(r.get("value", ""))
        if v is None or not (r.get("name") or "").strip():
            continue
        kind = (r.get("kind") or "asset").strip().lower()
        items.append({"name": r["name"].strip(), "kind": "liability" if kind.startswith("liab") else "asset",
                      "value": v, "as_of": (r.get("as_of") or "").strip()})
    return items


# A balance last printed longer ago than this can't be trusted (months of texts may be missing), so the account
# is left out (list and total) until the bank sends a fresh one.
STALE_DAYS = 90


def bank_balances(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""SELECT b.account, b.balance, b.ts FROM balances b
                           JOIN (SELECT account, MAX(ts) ts FROM balances GROUP BY account) l
                           ON l.account = b.account AND l.ts = b.ts ORDER BY b.balance DESC""").fetchall()
    try:   # a PPF account the bank texts about (credits, "Avl Bal") is an investment, not money in the bank
        ppf = {a.get("account") for a in json.loads(paths.SETTINGS_PATH.read_text()).get("ppf") or []}
    except (OSError, ValueError):
        ppf = set()
    rows = [r for r in rows if r["account"] not in ppf]
    out = []
    accounts_per_bank: dict[str, int] = {}
    for r in rows:
        accounts_per_bank[r["account"].split(" ")[0]] = accounts_per_bank.get(r["account"].split(" ")[0], 0) + 1
    for r in rows:
        # Banks often print "Avl bal" only on some alerts (HDFC: on credits), so the last reported balance
        # can be weeks old. Estimate today's from the payments in and out of that account since then.
        moved, n = conn.execute(
            """SELECT COALESCE(SUM(CASE direction WHEN 'in' THEN amount ELSE -amount END), 0), COUNT(*)
               FROM transactions WHERE account=? AND ts>? AND duplicate_of IS NULL""",
            (r["account"], r["ts"])).fetchone()
        # A card of the same bank paid by auto-pay from this account: the bank alerts only "payment received"
        # on the card, never the debit. Card payments with no matching debit anywhere came from here
        # (when you have one account at that bank).
        bank = r["account"].split(" ")[0]
        autopay = []
        if accounts_per_bank.get(bank) == 1:
            autopay = [x["amount"] for x in conn.execute(
                """SELECT c.amount FROM transactions c WHERE c.direction='in' AND c.duplicate_of IS NULL AND c.ts>?
                   AND c.account LIKE ? AND c.category='Transfer'
                   AND NOT EXISTS (SELECT 1 FROM transactions o WHERE o.direction='out' AND o.duplicate_of IS NULL
                       AND ABS(o.amount - c.amount) < 0.01 AND o.account NOT LIKE '%card%'
                       AND ABS(julianday(o.ts) - julianday(c.ts)) <= 3)""",
                (r["ts"], f"{bank} card%"))]
        stale = (date.today() - date.fromisoformat(r["ts"][:10])).days > STALE_DAYS
        out.append({"name": r["account"], "value": r["balance"] + moved - sum(autopay), "reported": r["balance"],
                    "as_of": r["ts"][:10], "since": n + len(autopay), "autopay": round(sum(autopay), 2),
                    "stale": stale})
    return sorted(out, key=lambda b: (b["stale"], -b["value"]))


def card_dues(conn: sqlite3.Connection) -> list[dict]:
    """Latest statement per card, dropped once a payment at least that large arrives after it."""
    out = []
    rows = conn.execute("""SELECT d.account, d.total_due, d.ts, d.due_date FROM card_dues d
                           JOIN (SELECT account, MAX(ts) ts FROM card_dues GROUP BY account) l
                           ON l.account = d.account AND l.ts = d.ts""").fetchall()
    for r in rows:
        last4 = r["account"][-4:]
        paid = conn.execute("""SELECT COALESCE(SUM(amount),0) FROM transactions
                               WHERE direction='in' AND last4=? AND ts>=? AND duplicate_of IS NULL""",
                            (last4, r["ts"])).fetchone()[0]
        remaining = max(0.0, r["total_due"] - paid)
        if remaining > 0.5:
            out.append({"name": r["account"], "value": remaining, "as_of": r["ts"][:10], "due_date": r["due_date"]})
    return out


def snapshot(conn: sqlite3.Connection) -> dict:
    balances = [b for b in bank_balances(conn) if not b["stale"]]
    holdings = [dict(r) for r in conn.execute(
        "SELECT name, type, units, value, as_of, source_file FROM holdings ORDER BY value DESC")]
    manual = manual_items()
    dues = card_dues(conn)
    from . import money   # noqa: PLC0415
    from .invest import KINDS, history, kind_label   # noqa: PLC0415
    last = (history(conn) or [{}])[-1]
    home = money.configured()
    investments = [{"name": kind_label(k, home), "kind": k, "value": last[k], "as_of": last["date"]}
                   for k in KINDS if last.get(k)]
    assets = sum(b["value"] for b in balances) + sum(h["value"] for h in holdings) + \
        sum(i["value"] for i in investments) + sum(m["value"] for m in manual if m["kind"] == "asset")
    liabilities = sum(d["value"] for d in dues) + sum(m["value"] for m in manual if m["kind"] == "liability")
    today = date.today().isoformat()
    conn.execute("""INSERT INTO networth_history(date,assets,liabilities,net) VALUES(?,?,?,?)
                    ON CONFLICT(date) DO UPDATE SET assets=excluded.assets, liabilities=excluded.liabilities,
                    net=excluded.net""", (today, assets, liabilities, assets - liabilities))
    return {"balances": balances, "holdings": holdings, "investments": investments, "manual": manual, "card_dues": dues,
            "assets": assets, "liabilities": liabilities, "net": assets - liabilities}
