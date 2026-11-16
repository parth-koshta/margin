"""NPS: the monthly Transaction Statement from KFintech CRA, a PDF locked with your 12-digit PRAN
(margin setup-pran keeps it in the Keychain). Holdings are the scheme rows (units x NAV = value), checked against
the statement's current valuation; cost is the total contributed (less withdrawals), split by each scheme's value.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from . import gmail, pdf
from .cas import ASOF_RE, _close, _date, _num, _nums

QUERY = 'from:KCRA@kfintech.com subject:"Monthly Transaction Statement" has:attachment'
SCHEME_RE = re.compile(r"SCHEME\s+([A-Z0-9]+)\s*-\s*TIER\s*(I{1,2})\b", re.I)
PFM_RE = re.compile(r"(?:A/C\s+)?([A-Z][A-Za-z&]*(?:\s+[A-Z][A-Za-z&]*){0,3}?)\s+PENSION\s+(?:FUND|Management)", re.I)
ACRONYMS = {"HDFC", "SBI", "LIC", "UTI", "ICICI", "DSP", "HSBC", "AXIS"}
ASOF_RES = [re.compile(r"Investment Details as on\s+(\d{2})-(\d{2})-(\d{4})", re.I),
            re.compile(r"as of\s+([A-Za-z]{3})[a-z]*\s+(\d{1,2}),\s*(\d{4})", re.I)]


def _as_of(lines: list[str]) -> tuple[str | None, int]:
    for i, l in enumerate(lines):
        m = ASOF_RES[0].search(l)
        if m:
            return _date(*m.groups()), i
        m = ASOF_RES[1].search(l)
        if m:
            return _date(m.group(2).zfill(2), m.group(1), m.group(3)), i
    return None, -1


STOP = {"NPS", "TRUST-", "TRUST", "A/C", "AT", "NAV", "XIRR", "TIER", "I", "II", "VALUE", "LATEST", "SCHEME", "POP",
        "TOTAL", "UNITS", "NAME", "PFM/SCHEME", "PFM", "-", "DATE", "RETURN", "ON", "INVESTMENT"}


def _pfm(text: str) -> str:
    """The fund manager's name: the words right before 'Pension Fund' / 'Pension Management' ('HDFC')."""
    m = re.search(r"(.*?)\bPENSION\s+(?:FUND|Management)", text, re.I)
    words = []
    for w in reversed((m.group(1) if m else "").split()):
        if w.upper().strip("-:") in STOP or not re.fullmatch(r"[A-Za-z&.]+", w) or len(words) == 3:
            break
        words.insert(0, w)
    return " ".join(w.upper() if w.upper() in ACRONYMS else w.title() for w in words)


def parse(layout: str) -> dict:
    """{as_of, positions, checks} from the statement's layout text (pdf.text(layout=True)). KFintech has changed the
    layout over the years, so rows are found by their numbers: units x NAV = value, with the scheme ("Scheme E -
    Tier I") on that line or the next two and the fund manager on that line or the one before."""
    lines = [l for l in layout.splitlines() if l.strip()]
    as_of, at = _as_of(lines)
    if as_of is None:
        return {"as_of": None, "positions": [], "checks": {}}
    totals = next((_nums(l) for l in lines[at + 1:at + 10] if len(_nums(l)) >= 5), [])
    out, printed = [], None
    for i in range(at + 1, min(len(lines), at + 60)):
        line = lines[i]
        tot = re.match(r"\s*Total((?:\s+[\d,]+\.\d{2})+)\s*$", re.sub(r"(\d),\s+(\d)", r"\1,\2", line))
        if tot:
            if out:                     # the schemes' total right after their rows (one number, or cost/value/gain)
                read = sum(p["value"] for p in out)
                printed = min(_nums(tot.group(1)), key=lambda x: abs(x - read))
                break
            continue
        nums = [x for x in _nums(re.sub(r"\d+(?:\.\d+)?%", " ", line)) if x > 0]
        row = next(((nums[k], nums[k + 1], nums[k + 2]) for k in range(len(nums) - 2)
                    if _close(nums[k] * nums[k + 1], nums[k + 2]) and nums[k + 2] >= 1), None)
        if not row:
            continue
        near = " ".join(lines[i:i + 3])
        sch = SCHEME_RE.search(near)
        if not sch:
            continue
        pfm = _pfm(lines[i - 1] + " " + line) or _pfm(near)
        scheme, tier = sch.group(1).upper(), sch.group(2).upper()
        out.append({"kind": "nps", "key": f"NPS:{pfm or 'PFM'}:{scheme}:{tier}",
                    "name": f"{pfm + ' ' if pfm else ''}Pension Fund, Scheme {scheme} (Tier {tier})",
                    "units": row[0], "price": row[1], "value": row[2], "cost": None})
    total = sum(p["value"] for p in out)
    # amount contributed: printed with a label (older statements), else the number C in the totals row for which
    # (valuation - C) is also printed (the gain)
    flat = " ".join(lines)
    lab = re.search(r"Total Contribution \(₹\)\s+([\d,]+\.\d{2})", flat)
    wd = re.search(r"Total Withdrawal \(₹\)\s+([\d,]+\.\d{2})", flat)
    contributed = _num(lab.group(1)) - (_num(wd.group(1)) if wd else 0) if lab else None
    if contributed is None:
        vals = [x for x in totals if x > 0]
        v = min(vals, key=lambda x: abs(x - total)) if vals and total else None
        contributed = next((c for c in vals if v and c != v and any(abs((v - c) - g) <= 2 for g in totals)), None)

    if out and contributed:
        for p in out:           # what you contributed, split by each scheme's share of the value
            p["cost"] = contributed * p["value"] / total
    return {"as_of": as_of, "positions": out, "checks": {"printed": printed, "read": round(total, 2)}}


def read_new(conn: sqlite3.Connection, settings: dict) -> list[str]:
    pran = pdf.secret(pdf.PRAN_ACCOUNT)
    if not pran:
        return []
    tok = gmail._Token()
    done = {r[0] for r in conn.execute("SELECT id FROM inv_statements WHERE kind='nps' AND ok=1")}
    read = []
    for mid in reversed(gmail.search(QUERY, tok, limit=60)):
        if mid in done:
            continue
        m = gmail.message(mid, tok)
        name, ref = next((f for f in m["files"] if f[0].lower().endswith(".pdf")), (None, None))
        if not name:
            continue
        try:
            st = parse(pdf.text(gmail.attachment(mid, ref, tok), pran, layout=True))
        except pdf.WrongPassword:
            _mark(conn, mid, None, 0, "the PRAN in the Keychain didn't open it")
            continue
        if not st["positions"] or not st["as_of"]:
            _mark(conn, mid, st["as_of"], 0, "couldn't find holdings in it")
            continue
        conn.execute("DELETE FROM inv_positions WHERE stmt=?", (mid,))
        conn.executemany("""INSERT INTO inv_positions(stmt, as_of, kind, key, name, units, price, value, cost, extra)
                            VALUES (?,?,?,?,?,?,?,?,?,'{}')""",
                         [(mid, st["as_of"], "nps", p["key"], p["name"], p["units"], p["price"], p["value"], p["cost"])
                          for p in st["positions"]])
        cost = sum(p["cost"] or 0 for p in st["positions"]) or None
        conn.execute("INSERT OR REPLACE INTO inv_history(date, kind, value, cost) VALUES (?,?,?,?)",
                     (st["as_of"], "nps", sum(p["value"] for p in st["positions"]), cost))
        c = st["checks"]
        off = c.get("printed") is not None and abs(c["printed"] - c["read"]) > 1
        _mark(conn, mid, st["as_of"], 1, f"{len(st['positions'])} schemes" +
              (f" (check: read ₹{c['read']:,.0f} of ₹{c['printed']:,.0f})" if off else ""))
        read.append(st["as_of"])
    return read


def _mark(conn: sqlite3.Connection, mid: str, as_of: str | None, ok: int, note: str) -> None:
    conn.execute("INSERT OR REPLACE INTO inv_statements(id, kind, as_of, read_at, ok, note) VALUES (?,?,?,?,?,?)",
                 (mid, "nps", as_of, datetime.now().isoformat(timespec="seconds"), ok, note))
