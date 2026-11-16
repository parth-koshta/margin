"""PPF. Banks don't email PPF statements, so it starts from a balance you give once, in settings.json:

    "ppf": [{"name": "SBI PPF", "balance": 320000, "as_of": "2026-03-31", "invested": 280000,
             "account": "SBI a/c ••1234", "deposits": [{"date": "2026-05-02", "amount": 60000}], "rate": 7.1}]

(invested, rate and accrued are optional: rate defaults to 7.1% a year; accrued is interest already earned in the
year before as_of, 0 when as_of is a 31 March). Deposits after that come from your bank alerts: any
debit that mentions PPF (not Parag Parikh's "PPFAS") counts, and so does any credit to "account" (the label Margin
gives the PPF account when the bank texts about it), besides the ones listed. That account isn't a bank balance.

Interest follows the scheme's rules: each month earns rate/12 on the lowest balance between the 5th and the month's
end (so a deposit by the 5th earns that month), and the year's interest is credited on 31 March. The value shown is
the balance plus interest earned so far this year (credited next 31 March).
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

DEFAULT_RATE = 7.1
STATEMENT_Q = 'from:yonobysbi@alerts.sbi.bank.in subject:"PPF Account Statement" has:attachment'
PASSWORD = "ppf_statement"      # Keychain: last 5 digits of the mobile number + date of birth (DDMMYY), SBI's rule
ROW_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})\s+(.*?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$")
PPF_RE = re.compile(r"\bPPF\b(?!\s*AS)|PUBLIC PROVIDENT", re.I)


def parse_statement(text: str) -> dict | None:
    """SBI's PPF statement: {account, as_of, balance, rate, opened, txns: [{date, kind, amount, balance}]} (oldest
    first). kind is 'interest' (the 31 March credit), 'deposit' or 'withdrawal' (when the balance goes down)."""
    flat = re.sub(r"[ \t]+", " ", text)
    acct = re.search(r"Account Number\s*:\s*(\d{6,})", flat)
    asof = re.search(r"(?:Date of Statement|As on)\s*:?\s*(\d{2})-(\d{2})-(\d{4})", flat)
    bal = re.search(r"Clear Balance\s*:\s*([\d,]+\.\d{2})", flat)
    if not (acct and asof and bal):
        return None
    rate = re.search(r"Interest Rate\s*:\s*([\d.]+)", flat)
    opened = re.search(r"Account Open Date\s*:\s*(\d{2})-(\d{2})-(\d{4})", flat)
    txns = []
    for line in text.splitlines():
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        d, details = f"{m.group(3)}-{m.group(2)}-{m.group(1)}", m.group(4)
        amount, balance = (float(x.replace(",", "")) for x in m.group(5, 6))
        txns.append({"date": d, "details": details, "amount": amount, "balance": balance})
    txns.sort(key=lambda t: (t["date"], t["balance"]))
    prev = 0.0
    for t in txns:
        t["kind"] = ("interest" if re.search(r"INTEREST", t["details"], re.I)
                     else "withdrawal" if t["balance"] < prev else "deposit")
        prev = t["balance"]
    num = lambda x: float(x.replace(",", ""))   # noqa: E731
    return {"account": acct.group(1)[-4:], "as_of": f"{asof.group(3)}-{asof.group(2)}-{asof.group(1)}",
            "balance": num(bal.group(1)), "rate": float(rate.group(1)) if rate else None,
            "opened": f"{opened.group(3)}-{opened.group(2)}-{opened.group(1)}" if opened else
            (txns[0]["date"] if txns else None), "txns": txns}


def read_statement(conn: sqlite3.Connection) -> str | None:
    """Read the newest SBI PPF statement email into the database. Returns its date, None when nothing new."""
    import json   # noqa: PLC0415
    from . import gmail, pdf   # noqa: PLC0415
    password = pdf.secret(PASSWORD)
    if not password:
        return None
    tok = gmail._Token()
    ids = gmail.search(STATEMENT_Q, tok, limit=5)
    if not ids or conn.execute("SELECT 1 FROM state WHERE key=?", (f"ppfstmt:{ids[0]}",)).fetchone():
        return None
    m = gmail.message(ids[0], tok)
    name, ref = next((f for f in m["files"] if f[0].lower().endswith(".pdf")), (None, None))
    conn.execute("INSERT OR IGNORE INTO state(key, value) VALUES (?, '1')", (f"ppfstmt:{ids[0]}",))
    if not name:
        return None
    st = parse_statement(pdf.text(gmail.attachment(ids[0], ref, tok), password, layout=True))
    if not st:
        return None
    conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (f"ppf:{st['account']}", json.dumps(st)))
    return st["as_of"]


def statement(conn: sqlite3.Connection, account_label: str | None) -> dict | None:
    import json   # noqa: PLC0415
    if not account_label:
        return None
    r = conn.execute("SELECT value FROM state WHERE key=?", (f"ppf:{account_label[-4:]}",)).fetchone()
    return json.loads(r[0]) if r else None


def _effective(conn: sqlite3.Connection, acct: dict) -> dict:
    """With a statement: start from nothing before the account opened, its deposits and SBI's actual interest
    credits; bank alerts add deposits after the statement's date. Without one: the balance in settings."""
    st = statement(conn, acct.get("account"))
    if not st:
        return acct
    opened = date.fromisoformat(st["opened"]) - timedelta(days=1)
    return {**acct, "balance": 0.0, "as_of": opened.isoformat(), "rate": st["rate"] or acct.get("rate"),
            "deposits": [{"date": t["date"], "amount": t["amount"] if t["kind"] == "deposit" else -t["amount"]}
                         for t in st["txns"] if t["kind"] != "interest"] + list(acct.get("deposits") or []),
            "credits": {t["date"]: t["amount"] for t in st["txns"] if t["kind"] == "interest"},
            "alerts_after": st["as_of"], "statement": st["as_of"],
            "invested": sum(t["amount"] if t["kind"] == "deposit" else -t["amount"]
                            for t in st["txns"] if t["kind"] != "interest")}


def _month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def bank_deposits(conn: sqlite3.Connection, after: str) -> list[dict]:
    """Debits from your bank alerts that mention PPF, after a day."""
    out = []
    for r in conn.execute("""SELECT ts, amount, raw, counterparty FROM transactions WHERE direction='out'
                             AND duplicate_of IS NULL AND ts > ? ORDER BY ts""", (after + "T99",)):
        if PPF_RE.search(f"{r['raw'] or ''} {r['counterparty'] or ''}") and not re.search(r"PPFAS|Parag Parikh",
                                                                                          r["raw"] or "", re.I):
            out.append({"date": r["ts"][:10], "amount": r["amount"], "from": "bank alert"})
    return out


def _deposits(conn: sqlite3.Connection, acct: dict) -> list[dict]:
    listed = [{"date": d["date"], "amount": float(d["amount"]), "from": "settings"} for d in acct.get("deposits", [])
              if d.get("date", "") > acct["as_of"]]
    seen = {(d["date"], round(d["amount"])) for d in listed}
    after = acct.get("alerts_after") or acct["as_of"]
    auto = bank_deposits(conn, after)
    if acct.get("account"):         # the bank texts the PPF account itself ("Your A/C XXXXX001234 credited ...")
        auto += [{"date": r["ts"][:10], "amount": r["amount"], "from": "bank alert"} for r in conn.execute(
            """SELECT ts, amount FROM transactions WHERE account=? AND direction='in' AND duplicate_of IS NULL
               AND ts > ?""", (acct["account"], after + "T99"))]
    auto = [d for d in auto if (d["date"], round(d["amount"])) not in seen]
    return sorted(listed + auto, key=lambda d: d["date"])


def value_on(conn: sqlite3.Connection, acct: dict, day: str) -> dict:
    """{balance (credited), accrued (this year's interest so far), value, deposited_since} at the end of a day.

    Walks month by month from the starting balance: deposits up to the 5th count for that month's interest, later
    ones from the next month; each finished month earns rate/12; on 31 March the year's interest is credited.
    "accrued" in settings is interest already earned this year before the starting date (0 if it's 31 March)."""
    rate = float(acct.get("rate") or DEFAULT_RATE) / 100
    deps = [d for d in _deposits(conn, acct) if d["date"] <= day]
    bal, accrued = float(acct["balance"]), float(acct.get("accrued") or 0)
    start, end = date.fromisoformat(acct["as_of"]), date.fromisoformat(day)
    counted = acct["as_of"]                         # deposits after this day aren't in bal yet
    m = start.replace(day=1) if start.day <= 5 else _month_end(start) + timedelta(days=1)
    while _month_end(m) <= end:
        fifth, me = m.replace(day=5).isoformat(), _month_end(m).isoformat()
        bal += sum(d["amount"] for d in deps if counted < d["date"] <= fifth)
        accrued += bal * rate / 12                  # lowest balance from the 5th to month end
        bal += sum(d["amount"] for d in deps if max(counted, fifth) < d["date"] <= me)
        counted = me
        if m.month == 3:                            # the year's interest is credited on 31 March (SBI's figure if known)
            bal, accrued = bal + (acct.get("credits") or {}).get(me, round(accrued)), 0.0
        m = _month_end(m) + timedelta(days=1)
    bal += sum(d["amount"] for d in deps if d["date"] > counted)   # this unfinished month's deposits
    return {"balance": bal, "accrued": accrued, "value": bal + accrued,
            "deposited_since": sum(d["amount"] for d in deps)}


def accounts(conn: sqlite3.Connection, settings: dict, day: str | None = None) -> list[dict]:
    day = day or date.today().isoformat()
    out = []
    for a in settings.get("ppf") or []:
        a = _effective(conn, a)
        if not a.get("as_of") or a.get("balance") is None or day < a["as_of"]:
            continue
        v = value_on(conn, a, day)
        invested = a.get("invested")
        out.append({"name": a.get("name") or "PPF", "as_of": a.get("statement") or a["as_of"],
                    "rate": float(a.get("rate") or DEFAULT_RATE), "from_statement": bool(a.get("statement")),
                    **v, "cost": (v["deposited_since"] if a.get("statement") else float(invested) + v["deposited_since"])
                    if (invested is not None or a.get("statement")) else None,
                    "deposits": _deposits(conn, a)})
    return out


def record_history(conn: sqlite3.Connection, settings: dict) -> None:
    starts = [_effective(conn, a)["as_of"] for a in settings.get("ppf") or [] if a.get("as_of")]
    if not starts:
        return
    d, today = date.fromisoformat(min(starts)), date.today()
    days = []
    while d <= today:
        days.append(min(_month_end(d), today))
        d = _month_end(d) + timedelta(days=1)
    for day in sorted(set(days + [today, date.fromisoformat(min(starts))])):
        acc = accounts(conn, settings, day.isoformat())
        if acc and sum(a["value"] for a in acc) > 0:
            cost = [a["cost"] for a in acc]
            conn.execute("INSERT OR REPLACE INTO inv_history(date, kind, value, cost) VALUES (?, 'ppf', ?, ?)",
                         (day.isoformat(), sum(a["value"] for a in acc), sum(cost) if None not in cost else None))
