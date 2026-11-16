"""CDSL's monthly Consolidated Account Statement (CAS): every share in your demat accounts (any broker)
and every mutual fund folio under your PAN (any platform, or direct), as of the month's last day.

It arrives by email as a PDF locked with your PAN (margin setup-pan keeps it in the Keychain). Rows are read from
the PDF's layout (one table row per line); a row is only accepted when units x price comes to the value printed
beside it, and the sums are checked against the statement's own totals. Names and fund ISINs come from the
statement's folio list. Cost is read for mutual funds (the statement prints the amount invested); shares and ETFs
in demat have none.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from . import gmail, pdf

QUERY = "from:cdslstatement.com has:attachment"
ISIN_RE = re.compile(r"\b(IN[EF][A-Z0-9]{9})(?![A-Z0-9])")
NUM_RE = re.compile(r"(?<![\w/.])-?\(?(?:\d[\d,]*(?:\.\d+)?|\.\d+)\)?(?![\w/])")
ASOF_RE = re.compile(r"(?:as on|to)\s+(\d{2})[-/ ]([A-Za-z]{3}|\d{2})[-/ ](\d{4})", re.I)
FOLIO_RE = re.compile(r"(?<![\w.])(\d{6,}(?:/\d+)?)(?![\w.])")
TOTAL_RE = re.compile(r"Portfolio Value\s*\S?\s*([\d,]+\.\d\d)\s*as on")
MF_TOTAL_RE = re.compile(r"Grand Total\s+([\d,]+\.\d\d)\s+([\d,]+\.\d\d)")


def _num(s: str) -> float | None:
    neg = s.startswith("(") or s.startswith("-")
    try:
        v = float(s.strip("()-").replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


def _nums(s: str) -> list[float]:
    return [n for n in (_num(x) for x in NUM_RE.findall(s)) if n is not None]


def _date(d: str, m: str, y: str) -> str | None:
    for f in ("%d %m %Y", "%d %b %Y"):
        try:
            return datetime.strptime(f"{d} {m} {y}", f).date().isoformat()
        except ValueError:
            continue
    return None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(2.0, 0.006 * abs(b))


def _names(plain: str) -> dict:
    """ISIN -> security name, from the plain text (a demat row prints its name between the ISIN and the numbers)."""
    out = {}
    for m in ISIN_RE.finditer(plain):
        after = plain[m.end():m.end() + 260]
        cut = re.search(r"\s\d[\d,]*\.\d{3}\s", after)
        name = re.sub(r"\s+", " ", (after[:cut.start()] if cut else "")).strip(" !")
        if name and re.search(r"[A-Za-z]{3}", name) and m.group(1) not in out:
            out[m.group(1)] = name
    return out


BAD_NAME = re.compile(r"ISIN|Email|Mobile|UCC|Folio|Nominee|Distributor|\bSTT\b|Redemption|Purchase|\d{6,}")


def _fund_list(flat: str) -> list[tuple[str, str, str]]:
    """[(isin, folio, scheme name)] in the statement's order. Newer statements say "Scheme Name : X Folio No : F",
    older ones "ABCD - X Folio No : F"; the ISIN follows ("ISIN : INF...")."""
    out, seen = [], set()
    for m in re.finditer(r"Folio No : (\S+)", flat):
        before = flat[max(0, m.start() - 240):m.start()]
        # Where this fund's name starts: "Scheme Name :" (newer) or its scheme code "ABCD - " (older); never one that
        # belongs to the fund before it, nor a "FUND - " inside the name itself.
        clean = lambda c: not BAD_NAME.search(before[c.end():])   # noqa: E731
        named = [c for c in re.finditer(r"Scheme Name :\s*", before) if clean(c)]
        codes = [c for c in re.finditer(r"(?<![\w-])[A-Z0-9]{3,6} - ", before) if clean(c)]
        start = named[-1].end() if named else codes[0].end() if codes else None
        isin = re.search(r"ISIN : (IN[EF][A-Z0-9]{9})", flat[m.end():m.end() + 500])
        if start is None or not isin or (isin.group(1), m.group(1)) in seen:
            continue
        seen.add((isin.group(1), m.group(1)))
        out.append((isin.group(1), m.group(1), before[start:].strip()))
    return out


def _clean(name: str, isin: str) -> str:
    """'DEMO INDUSTRIES LIMITED#NEW EQUITY SHARES ...' -> 'DEMO INDUSTRIES LIMITED';
    'DEMO AM LTD#DEMO MF-DEMO NIFTY ETF' -> 'DEMO NIFTY ETF'."""
    left, _, right = name.partition("#")
    if isin.startswith("INF") and right:
        name = re.sub(r"^.*?MF-", "", right)
    else:
        name = left
    return re.sub(r"\s+", " ", name).strip(" -#")[:90] or isin


def parse(layout: str, plain: str = "") -> dict:
    """{as_of, positions: [{kind, key, name, units, price, value, cost, extra}], checks} from a CDSL CAS.

    layout: the PDF's text with each table row on one line; plain: its ordinary text (for full names and the
    list of fund folios). Demat holdings (shares and ETFs) are 'in_stock'; fund folios are 'mf'."""
    flat = re.sub(r"[ \t]+", " ", plain.replace(" ", " "))
    dates = [x for x in (_date(*m.groups()) for m in ASOF_RE.finditer(layout + " " + flat)) if x]
    as_of = max(dates) if dates else None
    names = _names(flat)
    funds = _fund_list(re.sub(r"\s+", " ", flat))
    positions, checks = [], {}
    lines = layout.splitlines()
    # Demat accounts: only inside each "HOLDING STATEMENT AS ON" table (the transactions table has ISINs too).
    # A row is ISIN, balances, market price, value; its numbers may sit on the next line.
    holding = False
    for i, line in enumerate(lines):
        if re.search(r"HOLDING STATEMENT AS ON", line, re.I):
            holding = True
            continue
        if re.search(r"Portfolio Value|STATEMENT OF TRANSACTIONS|UNITS HELD", line, re.I):
            holding = False
        m = ISIN_RE.search(line) if holding else None
        if not m:
            continue
        nums = _row_nums(line[m.end():])
        if len(nums) < 3 and i + 1 < len(lines) and not ISIN_RE.search(lines[i + 1]):
            nums += _row_nums(lines[i + 1])
        found = _equity(nums)
        if found:
            isin = m.group(1)
            positions.append({"kind": "in_stock", "key": isin, "name": _clean(names.get(isin, isin), isin),
                              "units": found["units"], "price": found["price"], "value": found["value"],
                              "cost": None, "extra": {}})
    # Fund folios: after "UNITS HELD AS ON" until "Grand Total". A row is units, NAV, amount invested, valuation
    # (then gain columns); its ISIN may be on the line before, or split across lines (then the folio list decides).
    used, pending, in_mf = set(), None, False
    fund_isins = {x[0] for x in funds}
    for line in lines:
        if re.search(r"UNITS HELD AS ON", line, re.I):
            in_mf = True
            continue
        if not in_mf:
            continue
        if re.match(r"\s*Grand Total", line):
            break
        isin_m = ISIN_RE.search(line)
        row = _fund_row(_row_nums(line[isin_m.end():] if isin_m else line))
        if not row:
            if isin_m:
                pending = isin_m.group(1)
            continue
        isin = isin_m.group(1) if isin_m else pending
        pending = None
        if not isin or (fund_isins and isin not in fund_isins) or isin in used:
            f = FOLIO_RE.search(line)
            isin = next((x[0] for x in funds if f and x[1] == f.group(1) and x[0] not in used), None) or \
                next((x[0] for x in funds if x[0] not in used), isin)
        if not isin:
            continue
        used.add(isin)
        f = next((x for x in funds if x[0] == isin), None)
        name = f[2] if f else names.get(isin, isin)
        positions.append({"kind": "mf", "key": isin, "name": re.sub(r"\s+-\s+Direct.*$", " (Direct)", name, flags=re.I)[:90],
                          "units": row["units"], "price": row["price"], "value": row["value"], "cost": row["cost"],
                          "extra": {"folio": f[1]} if f else {}})
    t = TOTAL_RE.findall(layout)
    g = MF_TOTAL_RE.search(layout)
    checks = {"demat_printed": sum(_num(x) for x in t) if t else None,
              "demat_read": round(sum(p["value"] for p in positions if p["kind"] == "in_stock"), 2),
              "funds_printed": _num(g.group(2)) if g else None,
              "funds_read": round(sum(p["value"] for p in positions if p["kind"] == "mf"), 2)}
    return {"as_of": as_of, "positions": positions, "checks": checks,
            "total": sum(p["value"] for p in positions)}


def _row_nums(s: str) -> list[float]:
    """Numbers in a table row, without dates, folio/reference numbers ('123456789', 'TX:000123') and '90/0'."""
    s = re.sub(r"\b\d{2}[-/]\d{2}[-/]\d{4}\b|\b\d{2}-[A-Za-z]{3}-\d{4}\b|\S*:\S*|\b\d+/\d+\b", " ", s)
    return [n for tok in NUM_RE.findall(s) if not re.fullmatch(r"\d{6,}", tok) and (n := _num(tok)) is not None]


def _fund_row(nums: list[float]) -> dict | None:
    """units, NAV, invested, valuation (or units, NAV, valuation): the first place units x NAV = valuation."""
    for i in range(len(nums) - 2):
        u, nav = nums[i], nums[i + 1]
        if u > 0 and nav > 0 and i + 3 < len(nums) and nums[i + 3] > 0 and _close(u * nav, nums[i + 3]):
            return {"units": u, "price": nav, "cost": nums[i + 2] if nums[i + 2] > 0 else None, "value": nums[i + 3]}
    for i in range(len(nums) - 2):
        u, nav = nums[i], nums[i + 1]
        if u > 0 and nav > 0 and nums[i + 2] > 0 and _close(u * nav, nums[i + 2]):
            return {"units": u, "price": nav, "cost": None, "value": nums[i + 2]}
    return None


def _equity(nums: list[float]) -> dict | None:
    """Demat row: balances first (current, free, pledged...), then market price and value. Find units x price = value."""
    for j in range(len(nums) - 1, 1, -1):
        value, price = nums[j], nums[j - 1]
        if value <= 0 or price <= 0:
            continue
        for units in nums[:j - 1]:
            if units > 0 and _close(units * price, value):
                return {"units": units, "price": price, "value": value}
    return None


def _off(c: dict) -> float:
    """How far the holdings read are from the statement's printed totals (₹)."""
    return sum(abs(c[f"{k}_printed"] - c[f"{k}_read"]) for k in ("demat", "funds") if c.get(f"{k}_printed") is not None)


def _note_off(note: str) -> float:
    return sum(float(a.replace(",", "")) and abs(float(a.replace(",", "")) - float(b.replace(",", "")))
               for a, b in re.findall(r"read ₹([\d,]+) of ₹([\d,]+)", note or ""))


def _mismatch(c: dict) -> str:
    out = []
    for k in ("demat", "funds"):
        printed, read = c.get(f"{k}_printed"), c.get(f"{k}_read")
        if printed is not None and abs(printed - read) > 1:
            out.append(f"{k}: read ₹{read:,.0f} of ₹{printed:,.0f}")
    return "; ".join(out)


def read_new(conn: sqlite3.Connection, settings: dict) -> list[str]:
    """Read CDSL statements not read yet (oldest first, so history fills in). Returns the as-of dates read."""
    pan = pdf.secret(pdf.PAN_ACCOUNT)
    if not pan:
        return []
    tok = gmail._Token()
    done = {r[0] for r in conn.execute("SELECT id FROM inv_statements WHERE kind='cas' AND ok=1")}
    read = []
    for mid in reversed(gmail.search(QUERY, tok, limit=60)):
        if mid in done:
            continue
        m = gmail.message(mid, tok)
        name, ref = next((f for f in m["files"] if f[0].lower().endswith(".pdf")), (None, None))
        if not name:
            continue
        try:
            data = gmail.attachment(mid, ref, tok)
            st = parse(pdf.text(data, pan, layout=True), pdf.text(data, pan))
        except pdf.WrongPassword:
            _mark(conn, mid, None, 0, "the PAN in the Keychain didn't open it")
            continue
        if not st["positions"] or not st["as_of"]:
            _mark(conn, mid, st["as_of"], 0, "couldn't find holdings in it")
            continue
        off = _off(st["checks"])
        other = conn.execute("SELECT id, note FROM inv_statements WHERE kind='cas' AND ok=1 AND as_of=? AND id!=?",
                             (st["as_of"], mid)).fetchone()
        if other:
            if off >= float(json.loads(other["note"] or "{}").get("off", 0) if (other["note"] or "").startswith("{")
                            else _note_off(other["note"])):
                _mark(conn, mid, st["as_of"], 0, "another copy of this month was read")
                continue
            conn.execute("UPDATE inv_statements SET ok=0, note='replaced by a better copy' WHERE id=?", (other["id"],))
            conn.execute("DELETE FROM inv_positions WHERE stmt=?", (other["id"],))
        conn.execute("DELETE FROM inv_positions WHERE stmt=?", (mid,))
        for p in st["positions"]:
            if p["name"] == p["key"] or BAD_NAME.search(p["name"]):
                r = conn.execute("SELECT name FROM inv_positions WHERE key=? AND name!=key ORDER BY as_of DESC",
                                 (p["key"],)).fetchall()
                p["name"] = next((x[0] for x in r if not BAD_NAME.search(x[0])), p["key"])
        conn.executemany("""INSERT INTO inv_positions(stmt, as_of, kind, key, name, units, price, value, cost, extra)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         [(mid, st["as_of"], p["kind"], p["key"], p["name"], p["units"], p["price"], p["value"],
                           p["cost"], json.dumps(p["extra"])) for p in st["positions"]])
        for kind in ("in_stock", "mf"):
            ps = [p for p in st["positions"] if p["kind"] == kind]
            if ps:
                cost = sum(p["cost"] or 0 for p in ps) if all(p["cost"] for p in ps) else None
                conn.execute("INSERT OR REPLACE INTO inv_history(date, kind, value, cost) VALUES (?,?,?,?)",
                             (st["as_of"], kind, sum(p["value"] for p in ps), cost))
        miss = _mismatch(st["checks"])
        _mark(conn, mid, st["as_of"], 1, f"{len(st['positions'])} holdings" + (f" (check: {miss})" if miss else ""))
        read.append(st["as_of"])
    # a holding unnamed in one month takes its name from any other month
    for (key,) in conn.execute("SELECT DISTINCT key FROM inv_positions WHERE name=key").fetchall():
        good = conn.execute("SELECT name FROM inv_positions WHERE key=? AND name!=key ORDER BY as_of DESC LIMIT 1",
                            (key,)).fetchone()
        if good:
            conn.execute("UPDATE inv_positions SET name=? WHERE key=? AND name=key", (good[0], key))
    return read


def _mark(conn: sqlite3.Connection, mid: str, as_of: str | None, ok: int, note: str) -> None:
    conn.execute("INSERT OR REPLACE INTO inv_statements(id, kind, as_of, read_at, ok, note) VALUES (?,?,?,?,?,?)",
                 (mid, "cas", as_of, datetime.now().isoformat(timespec="seconds"), ok, note))
