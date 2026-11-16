"""US stocks bought through INDmoney, rebuilt from its order emails.

INDmoney emails every order: "Your BUY order for Demo Corp is successful ... Amount: $250 Price: $187.5
Shares: 1.333333". Successful buys add shares, successful sells remove them; cancelled and rejected orders change
nothing (but they often name the real ticker, "Ticker: DEMO", which is how names are matched to tickers). Splits
come from Yahoo, so 0.4 shares bought before a 10-for-1 split count as 4 now. Cost is the average cost
of what you still hold, in dollars and in rupees at the ECB rate of each order's day.

The emails miss some old orders, so holdings start from a snapshot of the INDmoney app ("us_snapshot" in
settings.json: shares and invested dollars per ticker on a day); orders after that day are added to it. A ticker
matched wrongly can be fixed with "us_tickers": {"demo corp": "DEMO"} in settings.json.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime

from . import fx, gmail, prices

ORDER_SUBJECT = 'from:transactions.indmoney.com subject:"order of"'
BODY_RE = re.compile(r"Your (BUY|SELL) order for (.+?) is (successful+|cancelled|rejected)", re.I)
FIELD = {k: re.compile(rf"\b{k}:\s*\$?([\d,]+(?:\.\d+)?)", re.I) for k in ("Amount", "Price", "Shares")}
TICKER_RE = re.compile(r"\bTicker:\s*([A-Z][A-Z.]{0,5})\s+Order Type", re.S)
_DROP = re.compile(r"\b(inc|incorporated|corp|corporation|co|company|the|ltd|limited|plc|class [abc]|shares|"
                   r"common stock|holdings?|group|n\.?v|s\.?a|etf|trust|series \d|fund)\b\.?", re.I)


def norm(name: str) -> str:
    """'Demo, Inc.' and 'Demo Inc.' and 'DEMO INC' are the same company."""
    s = _DROP.sub(" ", name.lower().replace("&", " and "))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def parse_order(subject: str, text: str) -> dict | None:
    """{side, name, status, shares, price, amount, ticker} from one INDmoney order email, None if it isn't one."""
    t = re.sub(r"\s+", " ", text or "")
    m = BODY_RE.search(t) or BODY_RE.search(subject or "")
    if not m:
        return None
    out = {"side": m.group(1).upper(), "name": m.group(2).strip(), "status": m.group(3).lower()[:9]}
    for k, rx in FIELD.items():
        f = rx.search(t)
        out[k.lower()] = float(f.group(1).replace(",", "")) if f else None
    tk = TICKER_RE.search(t)
    out["ticker"] = tk.group(1) if tk else None
    return out


def read_orders(conn: sqlite3.Connection, settings: dict, progress=None) -> tuple[int, list[str]]:
    """Read new INDmoney order emails into us_orders (and learn tickers). Returns (new orders, notes)."""
    tok = gmail._Token()
    have = {r[0] for r in conn.execute("SELECT id FROM us_orders")}
    done = {r[0] for r in conn.execute("SELECT key FROM state WHERE key LIKE 'usorder:%'")}
    ids = [i for i in gmail.search(ORDER_SUBJECT, tok, limit=2000) if i not in have and f"usorder:{i}" not in done]
    added, notes = 0, []
    for n, mid in enumerate(ids):
        if progress:
            progress(n, len(ids))
        m = gmail.message(mid, tok)
        o = parse_order(m["subject"], m["text"])
        conn.execute("INSERT OR IGNORE INTO state(key, value) VALUES (?, '1')", (f"usorder:{mid}",))
        if not o:
            continue
        if o["ticker"]:
            conn.execute("INSERT OR IGNORE INTO us_tickers(name, ticker, how) VALUES (?,?,?)",
                         (norm(o["name"]), o["ticker"], "INDmoney email"))
        if o["status"] != "successfu" or not o["shares"]:
            continue
        ts = datetime.fromtimestamp(m["ts_epoch"]).isoformat(timespec="seconds")
        conn.execute("INSERT OR IGNORE INTO us_orders(id, ts, side, name, shares, price, amount) VALUES (?,?,?,?,?,?,?)",
                     (mid, ts, o["side"], o["name"], o["shares"], o["price"], o["amount"]))
        added += 1
    return added, notes


# Names a search can't settle: Alphabet has two share classes, and INDmoney spells SPY's name backwards.
KNOWN = {"berkshire hathaway a": "BRK-A", "berkshire hathaway b": "BRK-B", "s and p 500 spdr": "SPY",
         "state street spdr s and p 500": "SPY"}    # names Yahoo search gets wrong


def ticker_for(conn: sqlite3.Connection, settings: dict, name: str, lookup: bool = True) -> str | None:
    key = norm(name)
    cls = re.search(r"(?i)\bclass ([abc])\b", name)
    known = KNOWN.get(f"{key} {cls.group(1).lower()}" if cls else key)
    override = {**({key: known} if known else {}),
                **{norm(k): v for k, v in (settings.get("us_tickers") or {}).items()}}
    if key in override:
        return override[key]
    r = conn.execute("SELECT ticker FROM us_tickers WHERE name=?", (key,)).fetchone()
    if r:
        return r[0]
    if not lookup:
        return None
    try:
        sym = prices.yahoo_search(name)
    except (OSError, ValueError):
        return None
    if sym:
        conn.execute("INSERT OR REPLACE INTO us_tickers(name, ticker, how) VALUES (?,?,?)", (key, sym, "Yahoo search"))
    return sym


def _replay(conn: sqlite3.Connection, settings: dict, on: str, after: str = "", start: dict | None = None,
            lookup: bool = True):
    """Holdings at the end of `on`: `start` (as of `after`) plus the orders and splits after it."""
    events: dict[str, list] = {t: [] for t in (start or {})}
    names: dict[str, str] = {t: v.get("name", t) for t, v in (start or {}).items()}
    for o in conn.execute("SELECT ts, side, name, shares, amount FROM us_orders WHERE ts > ? AND ts < ? ORDER BY ts",
                          (after + "T99" if after else "", on + "T99")):
        t = ticker_for(conn, settings, o["name"], lookup)
        if t:
            names.setdefault(t, o["name"])
            events.setdefault(t, []).append((o["ts"][:10], 1, o))
    for t in events:
        for d, r in conn.execute("SELECT day, ratio FROM splits WHERE ticker=? AND day>? AND day<=?", (t, after, on)):
            events[t].append((d, 0, r))                      # 0 sorts before same-day orders: splits apply at open
    out, notes = {}, []
    for t, ev in events.items():
        s0 = (start or {}).get(t, {})
        shares, cost, cost_inr = s0.get("shares", 0.0), s0.get("cost_usd", 0.0), s0.get("cost_inr", 0.0)
        for d, kind, x in sorted(ev, key=lambda e: (e[0], e[1])):
            if kind == 0:
                shares *= x
            elif x["side"] == "BUY":
                rate = fx.inr_rate(conn, "USD", d, settings)[0] or 0
                shares += x["shares"]
                cost += x["amount"] or 0
                cost_inr += (x["amount"] or 0) * rate
            else:
                if x["shares"] > shares * 1.0001 + 1e-6:
                    notes.append(f"{t}: sold {x['shares']:g} on {d} but only {shares:g} were bought in the emails")
                frac = min(1.0, x["shares"] / shares) if shares else 1.0
                shares = max(0.0, shares - x["shares"])
                cost *= 1 - frac
                cost_inr *= 1 - frac
        if shares > 1e-6:
            out[t] = {"name": names.get(t, t), "shares": shares, "cost_usd": cost, "cost_inr": cost_inr}
    return out, notes


def positions(conn: sqlite3.Connection, settings: dict, on: str | None = None,
              lookup: bool = True) -> tuple[dict, list[str]]:
    """{ticker: {name, shares, cost_usd, cost_inr}} held at the end of day `on` (default today).

    With a snapshot in settings.json ("us_snapshot": {"as_of", "holdings": {ticker: {shares, cost_usd}}}, taken
    from the INDmoney app), holdings from that day on are the snapshot plus later orders. Before it, they're
    rebuilt from the emails alone, which miss some old orders (notes say which)."""
    on = on or date.today().isoformat()
    snap = settings.get("us_snapshot") or {}
    if not snap.get("as_of") or on < snap["as_of"]:
        return _replay(conn, settings, on, lookup=lookup)
    emails, _ = _replay(conn, settings, snap["as_of"], lookup=lookup)
    start = {}
    for t, h in (snap.get("holdings") or {}).items():
        e = emails.get(t)
        shares, cost = float(h["shares"]), float(h.get("cost_usd") or 0)
        if e and e["cost_usd"] and abs(e["shares"] - shares) <= 0.01 * shares:
            cost_inr = e["cost_inr"] * cost / e["cost_usd"]   # the emails agree: use their rupee cost
        else:
            cost_inr = cost * (fx.inr_rate(conn, "USD", snap["as_of"], settings)[0] or 0)
        start[t] = {"name": (e or {}).get("name") or h.get("name") or t, "shares": shares, "cost_usd": cost,
                    "cost_inr": cost_inr}
    return _replay(conn, settings, on, snap["as_of"], start, lookup)


def history_start(settings: dict) -> str | None:
    """First day the US holdings are trustworthy (the snapshot's), None if there's no snapshot."""
    return (settings.get("us_snapshot") or {}).get("as_of")


def first_order_day(conn: sqlite3.Connection) -> date | None:
    r = conn.execute("SELECT MIN(ts) FROM us_orders").fetchone()[0]
    return date.fromisoformat(r[:10]) if r else None
