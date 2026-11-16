"""Fixed deposits, from the bank's deposit advice emails (HDFC: "Your Fixed Deposit Advice", "Your Renewal Fixed
deposit advice"; the PDF opens with your PAN). Each advice gives the amount, start, rate, maturity date and maturity
amount. A renewal continues the same deposit account, so the money you first put in stays its cost.

A reinvestment deposit (interest paid at maturity) is valued each day by spreading its growth evenly over the term
(compounded), which lands exactly on the bank's maturity amount. A payout deposit is worth its principal (the
interest goes to your savings account as it's paid). Past maturity with no newer advice, a deposit set to renew is
assumed to have renewed on the same terms until the next advice arrives.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from . import gmail, pdf

QUERY = 'from:hdfcbank subject:"fixed deposit advice" has:attachment'
SCHEMA = """CREATE TABLE IF NOT EXISTS deposits (
    id TEXT PRIMARY KEY, bank TEXT, account TEXT NOT NULL, principal REAL NOT NULL, start TEXT NOT NULL,
    maturity TEXT NOT NULL, rate REAL, maturity_amount REAL, reinvest INTEGER, renews INTEGER, advice_date TEXT)"""
DATE = r"(\d{2})[- ]([A-Za-z]{3})[- ](\d{4})"
ROW_RE = re.compile(r"([\d,]+\.\d{2})\s+" + DATE + r"\s+(.{1,40}?)\s+(\d{1,2}\.\d{1,4})\s+" + DATE + r"\s+([\d,]+\.\d{2})")


def _d(d: str, m: str, y: str) -> str:
    return datetime.strptime(f"{d} {m.title()} {y}", "%d %b %Y").date().isoformat()


def _n(s: str) -> float:
    return float(s.replace(",", ""))


def parse(text: str) -> dict | None:
    """{account, principal, start, maturity, rate, maturity_amount, reinvest, renews} from an advice's text."""
    flat = re.sub(r"\s+", " ", text)
    m = ROW_RE.search(flat)
    acct = re.search(r"Deposit Account\s+(\d{6,})", flat)
    if not m or not acct:
        return None
    return {"account": f"••{acct.group(1)[-4:]}", "principal": _n(m.group(1)), "start": _d(*m.group(2, 3, 4)),
            "rate": float(m.group(6)), "maturity": _d(*m.group(7, 8, 9)), "maturity_amount": _n(m.group(10)),
            "reinvest": bool(re.search(r"REINVEST|ON MATURITY", flat, re.I)),
            "renews": bool(re.search(r"Maturity Instruction\s+RENEW", flat, re.I))}


def read_new(conn: sqlite3.Connection, settings: dict) -> int:
    conn.execute(SCHEMA)
    pan = pdf.secret(pdf.PAN_ACCOUNT)
    if not pan:
        return 0
    tok = gmail._Token()
    have = {r[0] for r in conn.execute("SELECT id FROM deposits")}
    n = 0
    for mid in gmail.search(QUERY, tok, limit=100):
        if mid in have or conn.execute("SELECT 1 FROM state WHERE key=?", (f"fd:{mid}",)).fetchone():
            continue
        m = gmail.message(mid, tok)
        name, ref = next((f for f in m["files"] if f[0].lower().endswith(".pdf")), (None, None))
        conn.execute("INSERT OR IGNORE INTO state(key, value) VALUES (?, '1')", (f"fd:{mid}",))
        if not name:
            continue
        try:
            d = parse(pdf.text(gmail.attachment(mid, ref, tok), pan, layout=True))
        except pdf.WrongPassword:
            continue
        if not d:
            continue
        conn.execute("""INSERT OR REPLACE INTO deposits(id, bank, account, principal, start, maturity, rate,
                        maturity_amount, reinvest, renews, advice_date) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (mid, "HDFC", d["account"], d["principal"], d["start"], d["maturity"], d["rate"],
                      d["maturity_amount"], int(d["reinvest"]), int(d["renews"]),
                      datetime.fromtimestamp(m["ts_epoch"]).date().isoformat()))
        n += 1
    return n


def value_on(term: dict, day: str) -> float:
    """A deposit term's value on a day (see the module note)."""
    p, mat = term["principal"], term["maturity_amount"] or term["principal"]
    s, e, t = date.fromisoformat(term["start"]), date.fromisoformat(term["maturity"]), date.fromisoformat(day)
    if not term["reinvest"]:
        return p
    span = max(1, (e - s).days)
    return p * (mat / p) ** (max(0, (t - s).days) / span)   # past maturity (renewed): same growth per term


def deposits(conn: sqlite3.Connection, day: str | None = None) -> list[dict]:
    """[{bank, account, value, cost, term}] for each deposit account on a day (default today): its term then, and
    the first amount you put in as its cost. Deposits closed at maturity (no renewal) drop out after it."""
    conn.execute(SCHEMA)
    day = day or date.today().isoformat()
    out = []
    for (acct,) in conn.execute("SELECT DISTINCT account FROM deposits").fetchall():
        terms = [dict(r) for r in conn.execute("SELECT * FROM deposits WHERE account=? ORDER BY start", (acct,))]
        live = [t for t in terms if t["start"] <= day]
        if not live:
            continue
        term = live[-1]
        if day > term["maturity"] and not term["renews"]:
            continue
        out.append({"bank": term["bank"], "account": acct, "value": value_on(term, day), "cost": terms[0]["principal"],
                    "term": term, "assumed_renewal": day > term["maturity"]})
    return out


def record_history(conn: sqlite3.Connection) -> None:
    """Month-end values from the first deposit, plus today."""
    conn.execute(SCHEMA)
    first = conn.execute("SELECT MIN(start) FROM deposits").fetchone()[0]
    if not first:
        return
    d, today = date.fromisoformat(first), date.today()
    days = []
    while d < today:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        days.append(min(nxt - timedelta(days=1), today))
        d = nxt
    days.append(today)
    for day in sorted(set(days)):
        ds = deposits(conn, day.isoformat())
        if ds:
            conn.execute("INSERT OR REPLACE INTO inv_history(date, kind, value, cost) VALUES (?, 'fd', ?, ?)",
                         (day.isoformat(), sum(x["value"] for x in ds), sum(x["cost"] for x in ds)))
