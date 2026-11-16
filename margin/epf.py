"""EPF: EPFO texts you after each monthly contribution ("your passbook balance against ZZDEM…1234 is
Rs. 6,54,321/-. Contribution of Rs. 12,345/- for due month Jul-26 has been received."). Each one is a dated
balance. When you change jobs the old member ID is transferred into the new one, so only the most recent
balance counts (older member IDs are already inside it). Interest shows up once EPFO credits it to the passbook.

The EPFO passbook (a PDF from the member portal, e.g. ZZDEM…1234_2026.pdf) is more complete: the year's opening
balance with interest, every contribution, transfers and withdrawals. Drop it into the statements folder and it's
read each night; for the months it covers it replaces the texts (which can lag the yearly interest credit). Only
your EPF (employee + employer shares) counts, not the pension (EPS) balance, which isn't paid out as a lump sum.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

from . import parse, paths

SCHEMA = """CREATE TABLE IF NOT EXISTS epf (
    uid TEXT PRIMARY KEY, ts TEXT NOT NULL, member TEXT, balance REAL NOT NULL, contribution REAL, due_month TEXT)"""
BAL_RE = re.compile(r"passbook balance against\s+(\S+?)\s+is\s+Rs\.?\s*([\d,]+(?:\.\d+)?)(?:/-)?\.?"
                    r"(?:\s*Contribution of Rs\.?\s*([\d,]+(?:\.\d+)?)(?:/-)?\s*for due month\s+([A-Za-z]{3}-\d{2}))?", re.I)


def is_epfo(sender: str) -> bool:
    return "EPFOHO" in parse.sender_key(sender or "").upper()


def parse_text(text: str) -> dict | None:
    """{member, balance, contribution, due_month} from an EPFO balance text, None for other EPFO texts."""
    m = BAL_RE.search(re.sub(r"\s+", " ", text or ""))
    if not m:
        return None
    raw = re.sub(r"[*•.…]+", "…", m.group(1))
    member = re.sub(r"^([A-Z]{5}).*?(\d{3,4})$", r"\1…\2", raw) if re.match(r"[A-Z]{5}", raw) else raw
    num = lambda s: float(s.replace(",", "")) if s else None   # noqa: E731
    return {"member": member, "balance": num(m.group(2)), "contribution": num(m.group(3)), "due_month": m.group(4)}


def store(conn: sqlite3.Connection, uid: str, ts: str, text: str) -> bool:
    conn.execute(SCHEMA)
    e = parse_text(text)
    if not e:
        return False
    conn.execute("INSERT OR REPLACE INTO epf(uid, ts, member, balance, contribution, due_month) VALUES (?,?,?,?,?,?)",
                 (uid, ts, e["member"], e["balance"], e["contribution"], e["due_month"]))
    return True


def from_review(conn: sqlite3.Connection) -> int:
    """EPFO texts already kept in the 'not counted' list (before EPF was read on its own)."""
    n = 0
    for r in conn.execute("SELECT uid, ts, sender, snippet FROM review").fetchall():
        if is_epfo(r["sender"]) and store(conn, r["uid"], r["ts"], r["snippet"] or ""):
            n += 1
    return n


def backfill(conn: sqlite3.Connection, messages) -> int:
    """Every EPFO text in the message history (run once from the nightly job, which can read Messages)."""
    from . import sms   # noqa: PLC0415
    n = 0
    for m in messages:
        if is_epfo(m["sender"]) and store(conn, f"sms:{m['id']}", sms.iso_local(m["ts_epoch"]), m["text"] or ""):
            n += 1
    return n


# --- the EPFO passbook ------------------------------------------------------------------------------------------
PB_ROW = re.compile(r"^([A-Z][a-z]{2}-\d{4})\s+(\d{2})-(\d{2})-(\d{4})\s+(CR|DR)\s+(.*?)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$")
PB_OB = re.compile(r"OB Int\. Updated upto (\d{2})/(\d{2})/(\d{4})\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")
PB_INT = re.compile(r"^\s*Int\. Updated upto (\d{2})/(\d{2})/(\d{4})\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")
PB_CLOSE = re.compile(r"Closing Balance as on (\d{2})/(\d{2})/(\d{4})\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)")


def _member(raw: str) -> str:
    return re.sub(r"^([A-Z]{5}).*?(\d{4})$", r"\1…\2", raw)


def parse_passbook(text: str) -> dict | None:
    """{member, fy, opening: (date, balance), rows: [{date, wage_month, due_month, kind, amount, balance}],
    closing, printed, checks}. Balances are employee + employer shares."""
    member = re.search(r"Member ID/Name\s+([A-Z]{5}[A-Z0-9]*\d)\s*/", text)
    fy = re.search(r"Financial Year\s*-\s*(\d{4})-(\d{4})", text)
    ob = PB_OB.search(text)
    if not (member and fy and ob):
        return None
    n = lambda x: float(x.replace(",", ""))   # noqa: E731
    d = lambda dd, mm, yy: f"{yy}-{mm}-{dd}"   # noqa: E731
    bal = n(ob.group(4)) + n(ob.group(5))
    opening = (d(*ob.group(1, 2, 3)), bal)
    rows = []
    for line in text.splitlines():
        m = PB_ROW.match(line.strip())
        if m:
            amt = n(m.group(7)) + n(m.group(8))
            bal += amt if m.group(5) == "CR" else -amt
            due = re.search(r"\b(20\d{2})(\d{2})\b", m.group(6))
            rows.append({"date": d(*m.group(2, 3, 4)), "wage_month": m.group(1), "kind": m.group(6)[:40],
                         "due_month": datetime(int(due.group(1)), int(due.group(2)), 1).strftime("%b-%y") if due else None,
                         "amount": amt if m.group(5) == "CR" else -amt, "balance": bal})
            continue
        i = PB_INT.match(line)
        if i and n(i.group(4)) + n(i.group(5)) > 0:     # the year's interest, once EPFO has credited it
            amt = n(i.group(4)) + n(i.group(5))
            bal += amt
            rows.append({"date": d(*i.group(1, 2, 3)), "wage_month": None, "kind": "interest", "due_month": None,
                         "amount": amt, "balance": bal})
    c = PB_CLOSE.search(text)
    closing = n(c.group(4)) + n(c.group(5)) if c else None
    printed = re.search(r"Printed On\s*:\s*(\d{2})-(\d{2})-(\d{4})", text)
    return {"member": _member(member.group(1)), "fy": f"{fy.group(1)}-{fy.group(2)}", "opening": opening,
            "rows": rows, "closing": closing, "printed": d(*printed.group(1, 2, 3)) if printed else None,
            "checks": {"read": round(bal, 2), "printed": closing}}


def store_passbook(conn: sqlite3.Connection, pb: dict) -> None:
    """The passbook's balances as dated EPF points; texts inside the months it covers are set aside."""
    conn.execute(SCHEMA)
    conn.execute("DELETE FROM epf WHERE uid LIKE ?", (f"passbook:{pb['member']}:{pb['fy']}:%",))
    end = pb["printed"] or max([r["date"] for r in pb["rows"]] + [pb["opening"][0]])
    points = [(pb["opening"][0], pb["opening"][1], None, None)]
    points += [(r["date"], r["balance"], r["amount"], r["due_month"]) for r in pb["rows"] if r["date"] <= end]
    points.append((end, pb["closing"] if pb["closing"] is not None else points[-1][1], None, None))
    for i, (day, bal, amt, due) in enumerate(points):
        conn.execute("INSERT OR REPLACE INTO epf(uid, ts, member, balance, contribution, due_month) VALUES (?,?,?,?,?,?)",
                     (f"passbook:{pb['member']}:{pb['fy']}:{i:03d}", day + "T23:59:00", pb["member"], bal, amt, due))
    conn.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (f"epfpb:{pb['member']}:{pb['fy']}",
                 json.dumps({"start": pb["opening"][0], "end": end, "checks": pb["checks"]})))


def read_passbooks(conn: sqlite3.Connection) -> list[str]:
    """EPFO passbook PDFs in the statements folder, each read once (again if the file changes)."""
    from . import pdf   # noqa: PLC0415
    read = []
    for f in sorted(paths.STATEMENTS_DIR.glob("*.pdf")):
        key = f"epfpbfile:{f.name}:{int(f.stat().st_mtime)}"
        if conn.execute("SELECT 1 FROM state WHERE key=?", (key,)).fetchone():
            continue
        conn.execute("INSERT OR IGNORE INTO state(key, value) VALUES (?, '1')", (key,))
        try:
            text = pdf.text(f.read_bytes(), None, layout=True)
        except Exception:   # noqa: BLE001  (locked or not a PDF Margin can read: some other statement)
            continue
        pb = parse_passbook(text) if "Member Passbook" in text or "EPF Passbook" in text else None
        if pb:
            store_passbook(conn, pb)
            read.append(f"{pb['member']} {pb['fy']}")
    return read


def _covered(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    out = []
    for k, v in conn.execute("SELECT key, value FROM state WHERE key LIKE 'epfpb:%'"):
        c = json.loads(v)
        out.append((k.split(":")[1], c["start"], c["end"]))
    return out


def balances(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every dated EPF balance, texts set aside where a passbook covers the same account and months."""
    conn.execute(SCHEMA)
    cov = _covered(conn)
    rows = conn.execute("SELECT uid, ts, member, balance, contribution, due_month FROM epf ORDER BY ts").fetchall()
    return [r for r in rows if r["uid"].startswith("passbook:") or
            not any(r["member"] == m and s <= r["ts"][:10] <= e for m, s, e in cov)]


def record_history(conn: sqlite3.Connection) -> None:
    """One history point per day with a text: that day's highest balance (EPFO sometimes texts a late month and the
    current one on the same day; the current account holds the transferred ones)."""
    days: dict[str, float] = {}
    for r in balances(conn):
        days[r["ts"][:10]] = max(days.get(r["ts"][:10], 0), r["balance"])
    conn.execute("DELETE FROM inv_history WHERE kind='epf'")
    for day, bal in days.items():
        conn.execute("INSERT OR REPLACE INTO inv_history(date, kind, value, cost) VALUES (?, 'epf', ?, NULL)", (day, bal))


def latest(conn: sqlite3.Connection) -> dict | None:
    rows = balances(conn)
    if not rows:
        return None
    day = rows[-1]["ts"][:10]
    r = max((x for x in rows if x["ts"][:10] == day), key=lambda x: x["balance"])
    older = sorted({x["member"] for x in rows if x["member"] != r["member"]})
    pb = next((x for x in reversed(rows) if x["uid"].startswith("passbook:") and x["due_month"]), None)
    return {"member": r["member"], "balance": r["balance"], "as_of": r["ts"][:10], "contribution": r["contribution"],
            "due_month": r["due_month"] or (pb["due_month"] if pb else None), "transferred": older,
            "source": "passbook" if r["uid"].startswith("passbook:") else "text",
            "age_days": (datetime.now() - datetime.fromisoformat(r["ts"][:19])).days}
