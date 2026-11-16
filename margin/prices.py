"""Closing prices for investments.

India: whole-market files, so nothing about what you hold is sent anywhere.
  - Stocks: NSE's daily bhavcopy (every listed stock's close, by ISIN); BSE's when NSE's isn't reachable.
  - Mutual funds: AMFI's NAVAll.txt (every scheme's latest NAV, by ISIN).
US stocks and the two benchmark indices: Yahoo Finance, one request per ticker (only the ticker is sent).
USD -> INR: the ECB rate (fx.py).
Everything lands in the prices table, keyed by ISIN or ticker, so a day is fetched once.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

UA = {"User-Agent": "Margin/1.0 (personal portfolio tracker)"}
NSE = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
BSE = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"
AMFI = "https://portal.amfiindia.com/spages/NAVAll.txt"
YAHOO = "https://query2.finance.yahoo.com"
BENCHMARKS = {"^NSEI": "Nifty 50", "^GSPC": "S&P 500"}
NAV = "NAV:"      # fund NAVs are stored as NAV:<ISIN>; plain <ISIN> is the exchange close (shares and ETFs)


def _get(url: str, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _store(conn: sqlite3.Connection, rows) -> int:
    rows = list(rows)
    conn.executemany("INSERT OR REPLACE INTO prices(key, day, close, prev, cur) VALUES (?,?,?,?,?)", rows)
    return len(rows)


def parse_bhavcopy(text: str) -> list[tuple]:
    """(isin, day, close, prev close, 'INR') for equity rows of an NSE or BSE bhavcopy (same column names)."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        isin, close = (r.get("ISIN") or "").strip(), (r.get("ClsPric") or "").strip()
        if not isin.startswith("IN") or not close or (r.get("FinInstrmTp") or "STK").strip() not in ("STK", ""):
            continue
        day = (r.get("TradDt") or "").strip()[:10]
        try:
            out.append((isin, day, float(close), float(r.get("PrvsClsgPric") or 0) or None, "INR"))
        except ValueError:
            continue
    return out


def india_stocks(conn: sqlite3.Connection, days_back: int = 6) -> str:
    """Latest trading day's closes for every NSE (or BSE) stock. Returns where they came from."""
    for back in range(days_back):
        d = date.today() - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        if conn.execute("SELECT 1 FROM prices WHERE day=? AND key LIKE 'INE%' LIMIT 1", (d.isoformat(),)).fetchone():
            return f"stock closes for {d} (already had them)"
        for name, url in (("NSE", NSE), ("BSE", BSE)):
            try:
                raw = _get(url.format(d=d))
                if raw[:2] == b"PK":
                    with zipfile.ZipFile(io.BytesIO(raw)) as z:
                        raw = z.read(z.namelist()[0])
                n = _store(conn, parse_bhavcopy(raw.decode("utf-8", errors="replace")))
                if n:
                    return f"{n} stock closes for {d} from {name}"
            except (OSError, zipfile.BadZipFile):
                continue
    return "no stock closes (NSE and BSE unreachable)"


def parse_amfi(text: str) -> list[tuple]:
    """('NAV:'+isin, day, nav, None, 'INR') for every scheme in NAVAll.txt (both its ISINs, growth and reinvest)."""
    out = []
    for line in text.splitlines():
        p = line.split(";")
        if len(p) < 6:
            continue
        try:
            nav = float(p[-2])
            day = datetime.strptime(p[-1].strip(), "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        for isin in (p[1].strip(), p[2].strip()):
            if isin.startswith("INF"):
                out.append((NAV + isin, day, nav, None, "INR"))
    return out


def mutual_funds(conn: sqlite3.Connection) -> str:
    try:
        n = _store(conn, parse_amfi(_get(AMFI, timeout=60).decode("utf-8", errors="replace")))
        return f"{n} fund NAVs from AMFI"
    except OSError as e:
        return f"no fund NAVs (AMFI unreachable: {e})"


def yahoo_chart(ticker: str, start: date, interval: str = "1d") -> dict:
    """{closes: [(day, close)], splits: [(day, ratio)], currency, price}. Raises OSError when unreachable."""
    p1 = int(datetime.combine(start, datetime.min.time()).timestamp())
    q = urllib.parse.urlencode({"period1": p1, "period2": int(time.time()), "interval": interval, "events": "split"})
    res = json.loads(_get(f"{YAHOO}/v8/finance/chart/{urllib.parse.quote(ticker)}?{q}", timeout=20))
    r = (res.get("chart", {}).get("result") or [None])[0]
    if not r:
        raise OSError(f"Yahoo has no data for {ticker}")
    closes = r.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    day = lambda ts: datetime.utcfromtimestamp(ts + (r["meta"].get("gmtoffset") or 0)).date().isoformat()  # noqa: E731
    pts = [(day(t), c) for t, c in zip(r.get("timestamp") or [], closes) if c is not None]
    spl = [(day(int(k)), v["numerator"] / v["denominator"]) for k, v in (r.get("events", {}).get("splits") or {}).items()
           if v.get("denominator")]
    return {"closes": pts, "splits": sorted(spl), "currency": r["meta"].get("currency"),
            "price": r["meta"].get("regularMarketPrice")}


US_EXCHANGES = {"NYQ", "NMS", "NGM", "NCM", "PCX", "ASE", "BTS", "NYS", "NAS"}


def yahoo_search(name: str) -> str | None:
    """The US-listed ticker for a company or fund name, trying a few cleaned-up forms of the name."""
    cleaned = re.sub(r"(?i)\b(class [abc]|shares|common stock)\b|\s-\s|,\s*the\b", " ", name)
    core = re.split(r"(?i)[,.]|\s(?:inc|corp|corporation|ltd|plc|company|co)\b", cleaned)[0]
    tried = set()
    for q in (name, cleaned, core):
        q = re.sub(r"\s+", " ", q).strip()
        if not q or q.lower() in tried:
            continue
        tried.add(q.lower())
        qs = urllib.parse.urlencode({"q": q, "quotesCount": 6, "newsCount": 0})
        for x in json.loads(_get(f"{YAHOO}/v1/finance/search?{qs}", timeout=20)).get("quotes", []):
            if x.get("quoteType") in ("EQUITY", "ETF") and x.get("exchange") in US_EXCHANGES and "." not in x["symbol"]:
                return x["symbol"]
        time.sleep(0.6)
    return None


def tickers(conn: sqlite3.Connection, symbols: list[str], start: date, splits: bool = False) -> list[str]:
    """Daily closes since start for each ticker (and its splits). Returns notes for ones that failed."""
    notes = []
    for i, sym in enumerate(symbols):
        if i:
            time.sleep(0.6)    # gentle: one request at a time
        try:
            c = yahoo_chart(sym, start)
        except (OSError, ValueError) as e:
            notes.append(f"{sym}: {e}")
            continue
        prev = None
        rows = []
        for d, close in c["closes"]:
            rows.append((sym, d, close, prev, c["currency"] or "USD"))
            prev = close
        _store(conn, rows)
        if splits:
            conn.executemany("INSERT OR REPLACE INTO splits(ticker, day, ratio) VALUES (?,?,?)",
                             [(sym, d, r) for d, r in c["splits"]])
    return notes


def latest(conn: sqlite3.Connection, key: str, on_or_before: str | None = None) -> tuple[float, float | None, str] | None:
    """(close, previous close, day) for a key, the latest on or before a day."""
    r = conn.execute("SELECT close, prev, day FROM prices WHERE key=? AND day<=? ORDER BY day DESC LIMIT 1",
                     (key, on_or_before or "9999")).fetchone()
    return (r[0], r[1], r[2]) if r else None
