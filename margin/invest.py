"""Investments: Indian stocks and mutual funds (CDSL CAS), US stocks (INDmoney), NPS.

update() runs in the nightly sync and does everything that needs the network: new statements, new INDmoney orders,
closing prices, today's values, stop-loss checks and Claude's weekly / monthly analysis. snapshot() only reads the
database, for the pages.

Stop-losses here are alerts only. Nothing in Margin can place, change or cancel an order.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import date, datetime, timedelta

from . import fx, money, prices, usstocks

KINDS = {"in_stock": "Stocks & ETFs", "mf": "Mutual funds", "us_stock": "US stocks", "nps": "NPS", "epf": "EPF",
         "fd": "Fixed deposits", "ppf": "PPF", "retirement": "Retirement", "cash": "Savings & CDs",
         "other": "Other investments"}
OTHER_KINDS = ("retirement", "cash", "other")    # from "other_investments" in settings.json, valued as you give them


def kind_label(kind: str, home: str) -> str:
    """Names that make sense where you live: US shares are just "Stocks & ETFs" with a dollar home currency."""
    if home == "USD" and kind == "us_stock":
        return "Stocks & ETFs"
    if home != "INR" and kind in ("in_stock", "mf"):
        return "Indian " + KINDS[kind][0].lower() + KINDS[kind][1:]
    return KINDS[kind]


# --- holdings --------------------------------------------------------------------------------------------------
def _latest_stmt(conn: sqlite3.Connection, kind: str, on: str | None = None):
    return conn.execute("SELECT id, as_of FROM inv_statements WHERE kind=? AND ok=1 AND as_of<=? ORDER BY as_of DESC "
                        "LIMIT 1", (kind, on or "9999")).fetchone()


def _prev_close(conn: sqlite3.Connection, key: str, day: str) -> float | None:
    r = conn.execute("SELECT close FROM prices WHERE key=? AND day<? ORDER BY day DESC LIMIT 1", (key, day)).fetchone()
    return r[0] if r else None


def holdings(conn: sqlite3.Connection, settings: dict, online: bool = False) -> tuple[list[dict], list[str]]:
    """Every holding valued at the latest close Margin has: [{kind, key, name, units, price, prev, day, cur
    (the holding's own currency), value, prev_value, cost (None if unknown), as_of (statement), stop}]. value,
    prev_value and cost are in your home currency (money.py)."""
    out, notes = [], []
    stops = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM stops")}
    home = money.home(settings)
    today = date.today().isoformat()
    rates: dict[str, float] = {}

    def factor(cur: str) -> float:
        """Home-currency units per 1 cur (the rate already fetched unless online)."""
        if cur not in rates:
            r = fx.rate(conn, cur, home, today, settings)[0] if online else fx.cached(conn, cur, home)
            rates[cur] = r or 0
            if not r:
                notes.append(f"No {cur}->{home} exchange rate yet; those holdings count as 0 until the next sync")
        return rates[cur]

    def add(kind, key, name, units, price, prev, day, cur, rate, cost, as_of, extra=None, cost_home=False):
        """cost is in the holding's own currency unless cost_home (already in the home currency)."""
        rate = factor(cur) if rate is None else rate
        cost = cost * factor(cur) if cost is not None and not cost_home else cost
        out.append({"kind": kind, "label": kind_label(kind, home), "key": key, "name": name, "units": units,
                    "price": price, "prev": prev,
                    "day": day, "cur": cur, "value": units * price * rate,
                    "prev_value": units * prev * rate if prev else None, "cost": cost, "as_of": as_of,
                    "stop": stops.get(key), "extra": extra or {}})

    cas = _latest_stmt(conn, "cas")
    if cas:
        for p in conn.execute("SELECT * FROM inv_positions WHERE stmt=?", (cas["id"],)):
            pkey = (prices.NAV if p["kind"] == "mf" else "") + p["key"]
            px = prices.latest(conn, pkey)
            if px and px[2] >= cas["as_of"]:
                price, day = px[0], px[2]
                prev = px[1] or _prev_close(conn, pkey, day)
            else:                                   # no newer close: the statement's own price
                price, prev, day = p["price"] or (p["value"] / p["units"] if p["units"] else 0), None, cas["as_of"]
            add(p["kind"], p["key"], p["name"], p["units"] or 0, price, prev, day, "INR", None, p["cost"], cas["as_of"],
                json.loads(p["extra"] or "{}"))
    nps = _latest_stmt(conn, "nps")
    if nps:
        for p in conn.execute("SELECT * FROM inv_positions WHERE stmt=?", (nps["id"],)):
            add("nps", p["key"], p["name"], p["units"] or 0, p["price"] or 0, None, nps["as_of"], "INR", None,
                p["cost"], nps["as_of"])
    from . import epf   # noqa: PLC0415
    e = epf.latest(conn)
    if e:
        add("epf", f"EPF:{e['member']}", f"EPF ({e['member']})", 1, e["balance"], None, e["as_of"], "INR", None, None,
            e["as_of"], {"transferred": e["transferred"], "due_month": e["due_month"], "age_days": e["age_days"],
                         "source": e["source"]})
    from . import fd   # noqa: PLC0415
    for d in fd.deposits(conn):
        t = d["term"]
        add("fd", f"FD:{d['bank']}:{d['account']}", f"{d['bank']} FD {d['account']}", 1, d["value"], None,
            date.today().isoformat(), "INR", None, d["cost"], t["start"],
            {"rate": t["rate"], "maturity": t["maturity"], "maturity_amount": t["maturity_amount"],
             "renews": bool(t["renews"]), "reinvest": bool(t["reinvest"]), "assumed_renewal": d["assumed_renewal"]})
    from . import ppf   # noqa: PLC0415
    for a in ppf.accounts(conn, settings):
        add("ppf", f"PPF:{a['name']}", a["name"], 1, a["value"], None, date.today().isoformat(), "INR", None, a["cost"],
            a["as_of"], {"rate": a["rate"], "balance": a["balance"], "accrued": a["accrued"],
                         "deposits": len(a["deposits"]), "from_statement": a["from_statement"]})
    for o in settings.get("other_investments", []):     # e.g. a 401(k), an IRA, a savings account, a CD
        kind = o.get("kind") if o.get("kind") in OTHER_KINDS else "other"
        cur = str(o.get("currency") or home).upper()
        value = float(o.get("value") or 0)
        cost = float(o["cost"]) if o.get("cost") is not None else None
        add(kind, f"OTHER:{o.get('name', kind)}", o.get("name") or KINDS[kind], 1, value, None,
            o.get("as_of") or today, cur, None, cost, o.get("as_of") or today, {"source": "settings.json"})
    usd = factor("USD")
    pos, unotes = usstocks.positions(conn, settings, lookup=online)
    notes += unotes
    for t, p in pos.items():
        px = prices.latest(conn, t)
        if not px:
            notes.append(f"No price yet for {t}")
            continue
        cost = p["cost_usd"] if home == "USD" else (p["cost_inr"] * factor("INR") if p["cost_inr"] is not None else None)
        add("us_stock", t, p["name"], p["shares"], px[0], px[1] or _prev_close(conn, t, px[2]), px[2], "USD", usd,
            cost, usstocks.history_start(settings), {"cost_usd": p["cost_usd"], "usd_rate": usd}, cost_home=True)
    return out, notes


def by_kind(rows: list[dict]) -> dict:
    out = {}
    for h in rows:
        k = out.setdefault(h["kind"], {"kind": h["kind"], "label": h.get("label") or KINDS[h["kind"]], "value": 0.0, "cost": 0.0,
                                       "cost_known": True, "prev_value": 0.0, "n": 0})
        k["value"] += h["value"]
        k["n"] += 1
        k["prev_value"] += h["prev_value"] if h["prev_value"] is not None else h["value"]
        if h["cost"] is None:
            k["cost_known"] = False
        else:
            k["cost"] += h["cost"]
    return out


# --- history -----------------------------------------------------------------------------------------------------
def record_today(conn: sqlite3.Connection, rows: list[dict]) -> None:
    today = date.today().isoformat()
    for k, v in by_kind(rows).items():
        conn.execute("INSERT OR REPLACE INTO inv_history(date, kind, value, cost) VALUES (?,?,?,?)",
                     (today, k, v["value"], v["cost"] if v["cost_known"] else None))


def history(conn: sqlite3.Connection) -> list[dict]:
    """[{date, in_stock, mf, us_stock, nps, total}] with each kind carried forward from its last known value."""
    rows = conn.execute("SELECT date, kind, value FROM inv_history ORDER BY date").fetchall()
    out, last = [], {}
    for d in sorted({r["date"] for r in rows}):
        for r in rows:
            if r["date"] == d:
                last[r["kind"]] = r["value"]
        out.append({"date": d, **{k: last.get(k) for k in KINDS}, "total": sum(last.values())})
    return out


def bank_on(conn: sqlite3.Connection, day: str) -> float | None:
    """Bank balances as last reported on or before a day (ones older than 90 days left out), for the trend.
    None when no account had a recent balance (months of texts missing): unknown, not zero."""
    total = None
    for r in conn.execute("""SELECT account, balance, ts FROM balances b WHERE ts = (SELECT MAX(ts) FROM balances
                             WHERE account=b.account AND ts<=?)""", (day + "T99",)):
        if (date.fromisoformat(day) - date.fromisoformat(r["ts"][:10])).days <= 90:
            total = (total or 0.0) + r["balance"]
    return total


def networth_series(conn: sqlite3.Connection, months: int = 24) -> list[dict]:
    """Month-end points: bank balances (as reported) + investments. Today's point is the live net worth."""
    inv = history(conn)
    pts, d = [], date.today().replace(day=1)
    for _ in range(months):
        end = (d - timedelta(days=1)).isoformat()
        iv = next((h["total"] for h in reversed(inv) if h["date"] <= end), 0.0)
        bank = bank_on(conn, end)
        if bank is not None or iv:
            pts.append({"date": end, "bank": bank, "invest": iv})
        d = (d - timedelta(days=1)).replace(day=1)
    return list(reversed(pts))


# --- stop-loss alerts --------------------------------------------------------------------------------------------
def _notify(title: str, msg: str) -> None:
    safe = lambda s: s.replace("\\", "").replace('"', "'")   # noqa: E731
    subprocess.run(["/usr/bin/osascript", "-e", f'display notification "{safe(msg)}" with title "{safe(title)}"'],
                   capture_output=True)


def _px(h: dict) -> str:
    return money.fmt(h["price"], h["cur"], 2)


def check_stops(conn: sqlite3.Connection, rows: list[dict], notify=_notify) -> list[str]:
    """Alert once when a close is at or below your stop; re-arm when it closes above again."""
    hits = []
    for h in rows:
        s = h["stop"]
        if not s or not h["day"]:
            continue
        if h["price"] <= s["price"] and not s["hit_at"]:
            conn.execute("UPDATE stops SET hit_at=?, hit_close=? WHERE key=?", (h["day"], h["price"], h["key"]))
            stop = money.fmt(s["price"], h["cur"], 2)
            msg = f"{h['name']} closed at {_px(h)} on {h['day']}, at or below your stop of {stop}."
            notify("Margin: stop-loss reached", msg + " Nothing was sold.")
            hits.append(msg)
        elif h["price"] > s["price"] and s["hit_at"]:
            conn.execute("UPDATE stops SET hit_at=NULL, hit_close=NULL WHERE key=?", (h["key"],))
    return hits


def set_stop(conn: sqlite3.Connection, key: str, kind: str, name: str, price: float | None) -> None:
    if price is None or price <= 0:
        conn.execute("DELETE FROM stops WHERE key=?", (key,))
    else:
        conn.execute("""INSERT INTO stops(key, kind, name, price, set_at) VALUES (?,?,?,?,?)
                        ON CONFLICT(key) DO UPDATE SET price=excluded.price, set_at=excluded.set_at,
                        hit_at=NULL, hit_close=NULL""",
                     (key, kind, name, price, datetime.now().isoformat(timespec="seconds")))


# --- nightly update ----------------------------------------------------------------------------------------------
def update(conn: sqlite3.Connection, settings: dict, progress=None) -> list[str]:
    """Network part of the nightly sync. Returns problems worth showing (nothing here can break the sync)."""
    from . import cas, nps, invreview   # noqa: PLC0415  (statement readers need Gmail; keep imports light)
    problems = []
    step = (lambda label: progress(label, force=True)) if progress else (lambda label: None)

    def guard(label, fn, *a):
        try:
            return fn(*a)
        except Exception as e:   # noqa: BLE001
            problems.append(f"Investments, {label}: {e}")
            return None

    step("Reading investment statements")
    new_cas = guard("CDSL statement", cas.read_new, conn, settings) or []
    guard("NPS statement", nps.read_new, conn, settings)
    guard("EPF texts", _epf, conn)
    guard("fixed deposits", _fd, conn, settings)
    guard("PPF", _ppf, conn, settings)
    guard("INDmoney orders", usstocks.read_orders, conn, settings)
    conn.commit()
    step("Fetching closing prices")
    guard("stock prices", prices.india_stocks, conn)
    guard("fund NAVs", prices.mutual_funds, conn)
    pos, _ = usstocks.positions(conn, settings)
    since = date.today() - timedelta(days=10)
    snap = usstocks.history_start(settings)
    if snap:
        since = min(since, date.fromisoformat(snap) - timedelta(days=10))
    have = conn.execute("SELECT MIN(day) FROM prices WHERE key='^NSEI'").fetchone()[0]
    bench_since = date.today() - timedelta(days=10) if have and have <= str(date.today() - timedelta(days=380)) \
        else date.today() - timedelta(days=400)
    notes = guard("US prices", prices.tickers, conn, sorted(pos), since, True) or []
    notes += guard("benchmarks", prices.tickers, conn, list(prices.BENCHMARKS), bench_since) or []
    problems += [f"Investments, prices: {n}" for n in notes]
    conn.commit()
    rows, _ = holdings(conn, settings, online=True)
    record_today(conn, rows)
    hits = check_stops(conn, rows)
    conn.commit()
    step("Investment analysis")
    guard("analysis", invreview.run_due, conn, settings, bool(new_cas))
    if not pdf_ready():
        problems.append("Investments: run margin setup-pan in Terminal so Margin can open your CDSL statement.")
    last = conn.execute("SELECT as_of, note FROM inv_statements WHERE kind='cas' AND ok=1 ORDER BY as_of DESC LIMIT 1"
                        ).fetchone()
    if last and "check:" in (last["note"] or ""):
        problems.append(f"Investments: the CDSL statement for {last['as_of']} didn't fully add up ({last['note']}).")
    return problems + [f"Stop-loss: {h}" for h in hits]


def _ppf(conn: sqlite3.Connection, settings: dict) -> None:
    from . import ppf   # noqa: PLC0415
    ppf.read_statement(conn)
    ppf.record_history(conn, settings)


def _fd(conn: sqlite3.Connection, settings: dict) -> None:
    from . import fd   # noqa: PLC0415
    fd.read_new(conn, settings)
    fd.record_history(conn)


def _epf(conn: sqlite3.Connection) -> None:
    from . import epf   # noqa: PLC0415
    epf.from_review(conn)
    epf.read_passbooks(conn)
    epf.record_history(conn)


def epf_latest(conn: sqlite3.Connection) -> dict | None:
    from . import epf   # noqa: PLC0415
    return epf.latest(conn)


def pdf_ready() -> bool:
    from . import pdf   # noqa: PLC0415
    return bool(pdf.secret(pdf.PAN_ACCOUNT))


# --- for the pages -----------------------------------------------------------------------------------------------
def snapshot(conn: sqlite3.Connection, settings: dict) -> dict:
    from . import invreview, pdf   # noqa: PLC0415
    rows, notes = holdings(conn, settings)
    kinds = by_kind(rows)
    total = sum(k["value"] for k in kinds.values())
    month_start = (date.today().replace(day=1) - timedelta(days=1)).isoformat()
    hist = history(conn)
    base = next((h for h in reversed(hist) if h["date"] <= month_start), None)
    for k in kinds.values():
        k["month_start"] = base.get(k["kind"]) if base else None
    cas = _latest_stmt(conn, "cas")
    nps_s = _latest_stmt(conn, "nps")
    for h in rows:
        h["weight"] = h["value"] / total if total else 0
    rows.sort(key=lambda h: -h["value"])
    return {
        "holdings": rows, "kinds": list(kinds.values()), "total": total,
        "cost": sum(k["cost"] for k in kinds.values() if k["cost_known"]),
        "cost_value": sum(k["value"] for k in kinds.values() if k["cost_known"]),
        "prev_total": sum(k["prev_value"] for k in kinds.values()),
        "month_start_total": base["total"] if base else None,
        # this month's change, only for types that had a value at the month's start (a type newly tracked isn't a gain)
        "month_base": sum(k["month_start"] for k in kinds.values() if k.get("month_start")) or None,
        "month_change": sum(k["value"] - k["month_start"] for k in kinds.values() if k.get("month_start")),
        "history": hist, "networth": networth_series(conn),
        "benchmarks": {k: {"name": v, "series": [list(r) for r in conn.execute(
            "SELECT day, close FROM prices WHERE key=? AND day>=? ORDER BY day",
            (k, (date.today() - timedelta(days=400)).isoformat()))]} for k, v in prices.BENCHMARKS.items()
            if money.home(settings) == "INR" or k != "^NSEI"},        # the Nifty only matters with rupees at home
        "currency": money.home(settings),
        "sources": {
            "cas": cas["as_of"] if cas else None, "nps": nps_s["as_of"] if nps_s else None,
            "us": usstocks.history_start(settings),
            "us_orders": conn.execute("SELECT MAX(ts) FROM us_orders").fetchone()[0],
            "prices": conn.execute("SELECT MAX(day) FROM prices WHERE key LIKE 'INE%'").fetchone()[0],
            "pan": bool(pdf.secret(pdf.PAN_ACCOUNT)), "pran": bool(pdf.secret(pdf.PRAN_ACCOUNT)),
            "epf": epf_latest(conn),
        },
        "reviews": invreview.listing(conn), "notes": notes,
    }
