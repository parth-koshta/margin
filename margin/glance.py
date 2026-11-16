"""A quick glance for the Margin app's menu bar panel: what you spent today and this month, net worth, what's waiting
in Sort out, and the last sync. Served at /api/glance only with the key and only while Margin is unlocked (the
server answers 423 when it's locked), read only while the panel is open, and never written anywhere.

The figures are the page's own (dashboard.py), worked out the same way so the two always agree:
- Transfer and Investments are neither spending nor income; money paid back into a spending category (a friend's
  share of a bill) reduces that spending.
- "This month" is the pay cycle once two salaries are on record (it starts on payday; a missing payday is estimated
  as the same day of the month), otherwise the calendar month. A month still running is compared with the same
  number of days of the one before.
- Net worth's change this month is like for like: bank balances plus investments now, against the last month-end
  that had a bank balance.
"""
from __future__ import annotations

import calendar
import math
import sqlite3
from datetime import date, timedelta

from . import invest, lock, money, networth, review, tidy
from .categorize import NON_FLOW

MON = [m[:3] for m in calendar.month_name]


def _round(x: float) -> int:
    return math.floor(x + 0.5)                       # like the page's Math.round


def _ym_add(key: str, months: int) -> str:
    y, m = int(key[:4]), int(key[5:7]) + months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y}-{m:02d}"


class Months:
    """The page's months: pay cycles (from salary credits) or calendar months."""

    def __init__(self, salary_days: list[str], today: date):
        real: list[date] = []
        for d in sorted({date.fromisoformat(s) for s in salary_days}):
            if not real or (d - real[-1]).days > 20:
                real.append(d)
        self.starts: list[date] = []
        for k, d in enumerate(real):
            self.starts.append(d)
            nxt = real[k + 1] if k + 1 < len(real) else None
            until = nxt or today - timedelta(days=7)       # a week's grace before the latest one counts as missing
            y, m = d.year, d.month
            while True:
                m += 1
                if m > 12:
                    y, m = y + 1, 1
                g = date(y, m, min(d.day, calendar.monthrange(y, m)[1]))
                if ((until - g).days < 20) if nxt else (g > until):
                    break
                self.starts.append(g)
        self.pay = len(real) >= 2

    def key(self, d: date) -> str:
        """'YYYY-MM' of the month a day counts in (a pay cycle counts for the month it mostly pays for)."""
        if self.pay:
            before = [s for s in self.starts if s <= d]
            if before:
                return (before[-1] + timedelta(days=15)).isoformat()[:7]
        return d.isoformat()[:7]

    def start(self, key: str) -> date:
        if self.pay:
            for s in self.starts:
                if self.key(s) == key:
                    return s
        return date.fromisoformat(key + "-01")


def _amount(r, spend_cats: set) -> float:
    """What a payment adds to spending."""
    if r["category"] in NON_FLOW:
        return 0.0
    if r["direction"] == "out":
        return r["amount"]
    return -r["amount"] if r["category"] in spend_cats else 0.0       # paid back


def _net(conn: sqlite3.Connection, settings: dict, code: str, today: date) -> dict:
    nw = networth.snapshot(conn)
    out = {"value": money.fmt(nw["net"], code), "change": None}
    try:
        inv = invest.snapshot(conn, settings)
    except Exception:   # noqa: BLE001  (the page carries on without investments too)
        return out
    bank = sum(b["value"] for b in nw["balances"])
    first = today.replace(day=1).isoformat()
    base = next((p for p in reversed(inv.get("networth") or []) if p["date"] < first and p["bank"] is not None), None)
    if base:
        b0 = base["bank"] + base["invest"]
        ch = bank + (inv.get("total") or 0) - b0
        pct = f" · {abs(ch / b0 * 100):.1f}%" if b0 > 0 else ""
        out["change"] = {"amount": money.fmt(ch, code, sign=True) + pct, "since": "this month",
                         "trend": "same" if abs(ch) < 0.5 else ("down" if ch < 0 else "up")}
    return out


def summary(conn: sqlite3.Connection, settings: dict, status: dict, running: bool,
            today: date | None = None) -> dict:
    today = today or date.today()
    code = money.home(settings)
    spend_cats = {c for c in tidy.SPEND if c != "Other spending"}
    months = Months([r[0] for r in conn.execute(
        """SELECT DISTINCT substr(ts,1,10) FROM transactions WHERE duplicate_of IS NULL AND category='Salary'
           AND direction='in'""")], today)
    rows = conn.execute("""SELECT substr(ts,1,10) AS d, amount, direction, category FROM transactions
                           WHERE duplicate_of IS NULL AND ts >= ?""",
                        ((today - timedelta(days=80)).isoformat(),)).fetchall()
    day = today.isoformat()
    spent_today = sum(_amount(r, spend_cats) for r in rows if r["d"] == day)

    now = months.key(today)
    spent = sum(_amount(r, spend_cats) for r in rows if months.key(date.fromisoformat(r["d"])) == now)
    prev = _ym_add(now, -1)
    end = months.start(prev) + timedelta(days=(today - months.start(now)).days + 1)
    so_far = sum(_amount(r, spend_cats) for r in rows
                 if months.key(date.fromisoformat(r["d"])) == prev and r["d"] < end.isoformat())
    pmn = MON[int(prev[5:7])]
    vs, trend = f"{MON[int(now[5:7])]} {now[:4]}", None
    if so_far:
        pct = _round((spent - so_far) / so_far * 100)
        trend = "same" if pct == 0 else ("down" if pct < 0 else "up")
        vs = f"Same as {pmn} by now" if pct == 0 else f"{abs(pct)}% {'less' if pct < 0 else 'more'} than {pmn} by now"

    sort_out = len(tidy.payee_list(conn)) + len(tidy.sender_list(conn, settings))
    return {
        "currency": code,
        "today": {"spent": money.fmt(spent_today, code)},
        "month": {"label": f"Spent this {'cycle' if months.pay else 'month'}", "spent": money.fmt(spent, code),
                  "vs": vs, "trend": trend},
        "net": _net(conn, settings, code, today),
        "sort_out": sort_out,
        "checks": len(review.open_flags(conn)),
        "lock_on": lock.minutes(settings) > 0,
        "sync": {"at": status.get("at"), "ok": bool(status.get("ok")) and not status.get("problems"),
                 "problems": len(status.get("problems") or []), "running": running},
    }
