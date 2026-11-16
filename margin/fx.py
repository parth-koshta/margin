"""Exchange rates: anything in another currency is converted to your home currency (money.py) with these.

The rate is the European Central Bank reference rate for the day of the payment (the day your bank converted
it), from frankfurter.dev: free, no account. Only the currency code and the date are sent, never amounts or
anything about you. Weekends and holidays use the last business day before. Rates are cached in the database,
so each (currency, day) is fetched once. Offline, `fx_rates` in settings.json (home currency per unit) is the
fallback. Rates into rupees are cached under the currency code ("USD"); into any other currency under "USD>EUR".
"""
from __future__ import annotations

import json
import sqlite3
import urllib.request

API = "https://api.frankfurter.dev/v1/{day}?from={cur}&to={to}"


def _cache(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS fx_rates (cur TEXT NOT NULL, day TEXT NOT NULL, rate REAL NOT NULL,
                    rate_day TEXT, PRIMARY KEY (cur, day))""")


def inr_rate(conn: sqlite3.Connection, cur: str, day: str, settings: dict | None = None) -> tuple[float | None, str]:
    """(₹ per 1 unit of cur on day, where the rate came from). (None, why) if there's no rate at all."""
    cur = cur.upper()
    if cur == "INR":
        return 1.0, ""
    _cache(conn)
    row = conn.execute("SELECT rate, rate_day FROM fx_rates WHERE cur=? AND day=?", (cur, day)).fetchone()
    if row:
        return row[0], f"ECB rate for {row[1]}"
    try:
        req = urllib.request.Request(API.format(day=day, cur=cur, to="INR"), headers={"User-Agent": "margin"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        rate, rate_day = float(data["rates"]["INR"]), data.get("date", day)
        conn.execute("INSERT OR REPLACE INTO fx_rates VALUES(?,?,?,?)", (cur, day, rate, rate_day))
        return rate, f"ECB rate for {rate_day}"
    except (OSError, ValueError, KeyError):
        fallback = ((settings or {}).get("fx_rates") or {}).get(cur)
        if fallback:
            return float(fallback), "fallback rate from settings.json (rate service unreachable)"
        return None, f"no {cur} rate (rate service unreachable and no fallback in settings.json)"


def rate(conn: sqlite3.Connection, cur: str, to: str, day: str, settings: dict | None = None) -> tuple[float | None, str]:
    """(units of `to` per 1 `cur` on day, where the rate came from). (None, why) if there's no rate at all."""
    cur, to = cur.upper(), to.upper()
    if cur == to:
        return 1.0, ""
    if to == "INR":
        return inr_rate(conn, cur, day, settings)
    _cache(conn)
    key = f"{cur}>{to}"
    row = conn.execute("SELECT rate, rate_day FROM fx_rates WHERE cur=? AND day=?", (key, day)).fetchone()
    if row:
        return row[0], f"ECB rate for {row[1]}"
    try:
        req = urllib.request.Request(API.format(day=day, cur=cur, to=to), headers={"User-Agent": "margin"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        value, rate_day = float(data["rates"][to]), data.get("date", day)
        conn.execute("INSERT OR REPLACE INTO fx_rates VALUES(?,?,?,?)", (key, day, value, rate_day))
        return value, f"ECB rate for {rate_day}"
    except (OSError, ValueError, KeyError):
        from . import money
        fallback = ((settings or {}).get("fx_rates") or {}).get(cur) if money.home(settings) == to else None
        if fallback:
            return float(fallback), "fallback rate from settings.json (rate service unreachable)"
        return None, f"no {cur}->{to} rate (rate service unreachable and no fallback in settings.json)"


def cached(conn: sqlite3.Connection, cur: str, to: str) -> float | None:
    """The latest cur->to rate already fetched (pages never wait on the network)."""
    cur, to = cur.upper(), to.upper()
    if cur == to:
        return 1.0
    try:
        r = conn.execute("SELECT rate FROM fx_rates WHERE cur=? ORDER BY day DESC LIMIT 1",
                         (cur if to == "INR" else f"{cur}>{to}",)).fetchone()
    except sqlite3.OperationalError:
        return None
    return r[0] if r else None
