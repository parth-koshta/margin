"""Margin's demo: the real app on a made-up person's money, for trying Margin out, screenshots and videos.

`margin demo` builds a complete invented data folder and serves it at http://demo.margin.localhost:8765/:
- by default a US person in dollars (Jordan Lee, Seattle): paycheck, rent, card spends, a London trip paid in
  pounds, stocks & ETFs, a 401(k), an IRA, savings and a CD;
- with --india an Indian person in rupees (Kavya Menon, Kochi): bank texts, a USD subscription, Indian stocks, mutual
  funds, US stocks, NPS, EPF, PPF and a fixed deposit.
Each has 14 months of spending, two years of investment history, spending and investment reviews and Sort out items.
Everything in it is invented. It lives in ~/Library/Caches/Margin Demo/<us|india> and never reads or writes your
real data folder, never syncs, never contacts the internet and never runs Claude:
- Sync now plays a pretend sync,
- Claude's sorting and reviews are answered by a stand-in (demo_claude) from the same data the real Claude gets,
- with --lock it starts locked and Touch ID is simulated.
The data is rebuilt each day so it always ends today, and whenever Margin's code changes; --fresh rebuilds it now.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

DEMO_ROOT = Path(os.environ.get("MARGIN_DEMO_DATA") or Path.home() / "Library" / "Caches" / "Margin Demo")
REAL_DIR = Path.home() / "Library" / "Application Support" / "Margin"
HOST = "demo.margin.localhost"     # *.localhost is always this Mac; its cookie is separate from the real page's
KEY = "demo"
MARKER = ".margin-demo"            # only a folder with this file in it is ever deleted and rebuilt
_DELAY = 0.0                       # the Claude stand-in's thinking time (0 while building)


def main(a) -> int:
    """`margin demo`: switch to the demo folder (a fresh process, so nothing has the real folder open), build, serve."""
    persona = "india" if a.india else "us"
    folder = DEMO_ROOT / persona
    if REAL_DIR.resolve() in (DEMO_ROOT.resolve(), folder.resolve()) or REAL_DIR.resolve() in folder.resolve().parents:
        raise SystemExit("MARGIN_DEMO_DATA points at your real Margin folder; the demo needs its own.")
    if Path(os.environ.get("MARGIN_DATA", "/nonexistent")).resolve() != folder.resolve():
        if (DEMO_ROOT / MARKER).exists():                 # an older single-folder demo: start clean
            shutil.rmtree(DEMO_ROOT)
        folder.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, MARGIN_DATA=str(folder), PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        os.execve(sys.executable, [sys.executable, "-m", "margin", *sys.argv[1:]], env)
    _guard()
    stamp = folder / MARKER
    if a.fresh or not stamp.exists() or stamp.read_text().strip() != _version():
        print(f"Building the demo data ({'Kavya in Kochi, rupees' if a.india else 'Jordan in Seattle, dollars'})…",
              flush=True)
        print(build(persona))
    serve(a.port, open_browser=not a.no_open, locked=a.lock)
    return 0


def _version() -> str:
    """Today's date and a fingerprint of Margin's code: the demo is rebuilt when either changes, so a screenshot
    never shows data made by older code."""
    h = hashlib.sha1()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.read_bytes())
    return f"{date.today().isoformat()} {h.hexdigest()[:12]}"


def _guard() -> Path:
    from . import paths
    d = paths.DATA_DIR.resolve()
    if DEMO_ROOT.resolve() not in d.parents or d == REAL_DIR.resolve():
        raise SystemExit("The demo only ever runs on its own folder.")
    return d


def _in(x: float) -> str:
    """Indian digit grouping, as Indian banks write it: 1,42,000.00"""
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, parts = whole[:-3], [whole[-3:]]
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + parts)
    return f"{whole}.{frac}"


# --- building ----------------------------------------------------------------------------------------------------
def build(persona: str = "us", today: date | None = None) -> str:
    folder = _guard()
    if any(folder.iterdir()):
        if not (folder / MARKER).exists():
            raise SystemExit(f"{folder} isn't a Margin demo folder; not touching it.")
        shutil.rmtree(folder)
    from . import paths
    paths.ensure_data_dir()
    settings_in, rules = (US_SETTINGS, US_RULES) if persona == "us" else (INDIA_SETTINGS, INDIA_RULES)
    paths.SETTINGS_PATH.write_text(json.dumps(settings_in, indent=2, ensure_ascii=False) + "\n")
    paths.RULES_PATH.write_text(json.dumps(rules, indent=2) + "\n")
    from . import categorize, db, networth, sync
    categorize.reload_rules()
    _use_stand_ins()
    today = today or date.today()
    now = datetime.now().replace(second=0, microsecond=0)
    if now.date() != today:
        now = datetime.combine(today, datetime.min.time()).replace(hour=21)
    conn = db.connect()
    settings = sync.load_settings()
    rnd = random.Random(2026)
    (_us_money if persona == "us" else _india_money)(conn, settings, rnd, today, now)
    totals = _investment_history(conn, settings, rnd, today)
    _networth_history(conn, rnd, today, totals, cash_now=9500 if persona == "us" else 260000)
    _reviews(conn, settings, today, now)
    sync.refresh_dashboard(conn)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE duplicate_of IS NULL").fetchone()[0]
    nw = networth.snapshot(conn)
    conn.close()
    (folder / MARKER).write_text(_version() + "\n")
    from . import money
    cur = money.home(settings)
    return f"Demo data: {n:,} payments, net worth {money.fmt(nw['net'], cur)}."


class _Book:
    """Writes made-up payments the way Margin stores real ones (category from the person's rules)."""

    def __init__(self, conn, settings, now):
        self.conn, self.settings, self.now, self.n = conn, settings, now, 0

    def add(self, when: datetime, amount: float, direction: str, payee: str, account: str, raw: str,
            source: str = "email", sender: str = "alerts@bank.example", bank: str = "Demo Bank") -> None:
        from . import sync
        if when > self.now:
            return
        self.n += 1
        cat = sync._category(direction, payee, raw, amount, self.settings)
        self.conn.execute("""INSERT INTO transactions (uid,source,sender,bank,ts,amount,direction,account,last4,
                             counterparty,vpa,category,raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (f"demo:{self.n}", source, sender, bank, when.isoformat(timespec="seconds"), round(amount, 2),
                           direction, account, account[-4:], payee, None, cat, raw))


# --- Jordan in Seattle, dollars ------------------------------------------------------------------------------------
US_SETTINGS = {
    "currency": "USD", "lock_minutes": 0, "ai_review": True, "ai_sort": True, "read_sms": False, "misc_below": 3,
    "self_names": ["JORDAN LEE"], "about_me": ["Lives in Seattle and pays rent to Parkside Apartments on the 1st."],
    "us_snapshot": {"as_of": "2024-10-01", "holdings": {
        "VOO": {"shares": 42, "cost_usd": 15800, "name": "Vanguard S&P 500 ETF"},
        "VTI": {"shares": 55, "cost_usd": 12100, "name": "Vanguard Total Stock Market ETF"},
        "COST": {"shares": 6, "cost_usd": 3900, "name": "Costco Wholesale Corp"},
        "JPM": {"shares": 18, "cost_usd": 3100, "name": "JPMorgan Chase & Co"},
        "V": {"shares": 16, "cost_usd": 3600, "name": "Visa Inc"}}},
    "other_investments": [
        {"name": "Fidelity 401(k) · Target 2055", "kind": "retirement", "value": 84250, "cost": 61000},
        {"name": "Vanguard Roth IRA", "kind": "retirement", "value": 31400, "cost": 24000},
        {"name": "Ally high-yield savings", "kind": "cash", "value": 18600, "cost": 18600},
        {"name": "Marcus 12-month CD · 4.6%", "kind": "cash", "value": 10380, "cost": 10000}],
    "fx_rates": {"GBP": 1.27, "EUR": 1.08, "INR": 0.0119},
}
US_RULES = [
    {"match": ["parkside apartments"], "category": "Rent & housing", "direction": "out"},
    {"match": ["whole foods", "trader joe's", "safeway", "costco"], "category": "Groceries", "direction": "out"},
    {"match": ["uber eats", "doordash", "chipotle", "sweetgreen", "blue bottle", "pret a manger"],
     "category": "Food & dining", "direction": "out"},
    {"match": ["lyft", "tfl travel"], "category": "Transport", "direction": "out"},
    {"match": ["target", "rei", "apple store"], "category": "Shopping", "direction": "out"},
    {"match": ["puget sound energy", "xfinity", "t-mobile"], "category": "Bills & utilities", "direction": "out"},
    {"match": ["planet fitness"], "category": "Health & fitness", "direction": "out"},
    {"match": ["cvs pharmacy"], "category": "Medical", "direction": "out"},
    {"match": ["amc theatres", "british museum"], "category": "Entertainment", "direction": "out"},
    {"match": ["delta air lines", "marriott"], "category": "Travel", "direction": "out"},
    {"match": ["vanguard"], "category": "Investments", "direction": "out"},
]
US_EVERYDAY = [("WHOLE FOODS", 28, 145, 4), ("TRADER JOE'S", 22, 96, 4), ("STARBUCKS", 5.25, 9.8, 5),
               ("CHIPOTLE", 11, 16.5, 3), ("SWEETGREEN", 14, 18.5, 2), ("DOORDASH", 24, 48, 3), ("UBER", 12, 38, 3),
               ("LYFT", 10, 30, 2), ("AMAZON", 15, 120, 3), ("TARGET", 20, 140, 2), ("SHELL", 38, 62, 1),
               ("CVS PHARMACY", 8, 45, 1), ("AMC THEATRES", 15, 32, 1), ("BLUE BOTTLE COFFEE", 5.5, 11, 2)]
US_SORT_OUT = [("JOE'S PIZZA", 18, 42, "out"), ("GREENLEAF DRY CLEANERS", 16, 48, "out"),
               ("MARIA GARCIA", 40, 85, "out"), ("ALEX RIVERA", 25, 60, "in")]


def _us_money(conn, settings, rnd, today, now) -> None:
    from . import ai, db
    book = _Book(conn, settings, now)
    CHK, CARD = "Chase checking ••1234", "Chase Sapphire ••9876"

    def pay(when, amount, payee, account=CARD, direction="out", note=""):
        amount = round(amount, 2)
        verb = "Purchase" if account == CARD else ("Payment" if direction == "out" else "Deposit")
        book.add(when, amount, direction, payee, account,
                 f"{note}{verb} of ${amount:,.2f} {'at' if direction == 'out' else 'from'} {payee} on "
                 f"{when:%b %d, %Y}, {account}.", sender="no.reply.alerts@chase.example", bank="Chase")

    def abroad(when, gbp, payee):
        rate = 1.27
        note = ai.fx_note("GBP", gbp, round(gbp * rate, 2), rate, f"ECB rate for {when:%Y-%m-%d}", "USD").strip()
        pay(when, gbp * rate, payee, note=f"[read by Claude] {note} ")

    start = today - timedelta(days=425)
    london = today - timedelta(days=70)
    for m in range(24, 13, -1):                        # checking balances from before the payments shown here
        day = (today.replace(day=1) - timedelta(days=30 * m)).replace(day=28)
        conn.execute("INSERT OR REPLACE INTO balances VALUES (?,?,?,?)",
                     (CHK, f"{day}T06:10:00", round(4200 + 150 * (24 - m) + rnd.uniform(-600, 600), 2), "email"))
    bal = 6200.0
    d = start
    while d <= today:
        at = lambda h, m=0: datetime.combine(d, datetime.min.time()).replace(hour=h, minute=m)  # noqa: E731
        last_day = (d + timedelta(days=1)).day == 1
        if last_day:
            paycheck = 7850 if d < today - timedelta(days=200) else 8400              # a raise in the spring
            bal += paycheck - rnd.randint(6400, 7900)
            pay(at(6, 10), paycheck, "ACME CORP PAYROLL", CHK, "in")
            conn.execute("INSERT OR REPLACE INTO balances VALUES (?,?,?,?)",
                         (CHK, at(6, 10).isoformat(timespec="seconds"), round(bal, 2), "email"))
            pay(at(23, 0), rnd.uniform(64, 72), "ALLY BANK INTEREST", CHK, "in")
        if d.day == 1:
            pay(at(8, 40), 2350, "PARKSIDE APARTMENTS", CHK)
        if d.day == 2:
            pay(at(10, 5), 1000, "VANGUARD", CHK)
        if d.day == 3:
            pay(at(19, 15), rnd.uniform(1800, 3200), "CHASE CARD PAYMENT", CHK)
        if d.day == 5:
            pay(at(6, 2), 15.49, "NETFLIX")
            pay(at(6, 4), 11.99, "SPOTIFY")
            pay(at(6, 5), 2.99, "APPLE ICLOUD")
            pay(at(6, 7), 20.00, "OPENAI CHATGPT")
            pay(at(9, 30), 79.99, "XFINITY")
            pay(at(9, 35), 65.00, "T-MOBILE")
            pay(at(9, 40), 68 + 72 * max(0, math.cos((d.month - 1) / 12 * 2 * math.pi)), "PUGET SOUND ENERGY")
        if d.day == 10:
            pay(at(7, 0), 24.99, "PLANET FITNESS")
        if d.day in (14, 28) and rnd.random() < .5:
            pay(at(11, 0), rnd.uniform(85, 260), "COSTCO")
        if d.day == 18 and rnd.random() < .4:
            pay(at(12, 0), rnd.uniform(12, 90), "AMAZON REFUND", CARD, "in")
        if d == today - timedelta(days=150):
            pay(at(17, 45), 1299.00, "APPLE STORE")
        if d == today - timedelta(days=210):
            pay(at(13, 10), 412.60, "DELTA AIR LINES")
            pay(at(13, 25), 688.00, "AIRBNB")
        if d == london - timedelta(days=30):
            pay(at(20, 5), 1180.40, "DELTA AIR LINES")
        if london <= d < london + timedelta(days=5):                        # a week in London, paid in pounds
            if d == london:
                abroad(at(15, 30), 540.00, "MARRIOTT LONDON")
            abroad(at(9, 10), 8.45, "PRET A MANGER")
            abroad(at(10, 40), 12.80, "TFL TRAVEL")
            if d == london + timedelta(days=2):
                abroad(at(14, 5), 34.00, "BRITISH MUSEUM SHOP")
        for _ in range(rnd.choice([1, 2, 2, 3, 3, 4])):
            payee, lo, hi, _w = rnd.choices(US_EVERYDAY, [e[3] for e in US_EVERYDAY])[0]
            pay(at(rnd.randint(8, 22), rnd.randint(0, 59)), rnd.uniform(lo, hi), payee)
        if (today - d).days <= 45 and rnd.random() < .25:                     # new payees, still to sort
            payee, lo, hi, direction = rnd.choice(US_SORT_OUT)
            pay(at(rnd.randint(9, 20), rnd.randint(0, 59)), rnd.uniform(lo, hi), payee,
                CHK if direction == "in" or payee == "MARIA GARCIA" else CARD, direction)
        d += timedelta(days=1)
    for i, (sender, text) in enumerate([("venmo@venmo.example", "You paid Maria Garcia $42.00 for dinner"),
                                        ("receipts@doordash.example", "Your DoorDash order of $32.40 was charged")]):
        conn.execute("INSERT OR REPLACE INTO review VALUES (?,?,?,?,?,?)",
                     (f"demo-rev{i}", "email", sender, (now - timedelta(days=2 + i)).isoformat(timespec="seconds"),
                      "sender isn't a known bank (not counted)", text))
    conn.execute("INSERT OR REPLACE INTO card_dues VALUES (?,?,?,?)",
                 (CARD, (now - timedelta(days=6)).isoformat(timespec="seconds"), 2431.18,
                  (today + timedelta(days=9)).isoformat()))
    _prices(conn, rnd, today, {"VOO": 541.7, "VTI": 290.2, "COST": 912.6, "JPM": 248.3, "V": 331.8}, "USD")
    _benchmarks(conn, rnd, today)
    stamp = now.isoformat(timespec="seconds")
    for key, name, level in (("JPM", "JPMorgan Chase & Co", 238.0), ("V", "Visa Inc", 296.0)):
        conn.execute("INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?,?,?)", (key, "us_stock", name, level, stamp, None, None))
    db.set_state(conn, "last_sync", json.dumps({"at": now.replace(hour=0, minute=1).isoformat(timespec="seconds"),
                                                "ok": True, "problems": [], "counts": {"email": 1240}}))


# --- Kavya in Kochi, rupees -----------------------------------------------------------------------------------------
INDIA_SETTINGS = {
    "currency": "INR", "lock_minutes": 0, "ai_review": True, "ai_sort": True, "read_sms": True, "backfill_days": 365,
    "self_names": ["KAVYA MENON"], "about_me": ["Lives in Kochi and pays rent to the landlord on the 1st."],
    "ppf": [{"name": "SBI PPF ••1111", "balance": 612000, "as_of": "2026-03-31", "rate": 7.1, "invested": 480000}],
    "us_snapshot": {"as_of": "2026-06-01", "holdings": {
        "COST": {"shares": 2, "cost_usd": 1350, "name": "Costco Wholesale Corp"},
        "V": {"shares": 5, "cost_usd": 1400, "name": "Visa Inc"},
        "VOO": {"shares": 3, "cost_usd": 1480, "name": "Vanguard S&P 500 ETF"},
        "JPM": {"shares": 6, "cost_usd": 1150, "name": "JPMorgan Chase & Co"}}},
}
INDIA_RULES = [{"match": "meera kulkarni", "category": "Rent & housing", "direction": "out"}]

# payee, vpa, low, high, weight (how often on a random day), paid by card?
EVERYDAY = [("SWIGGY", "swiggy@icici", 180, 720, 6, True), ("ZOMATO", "zomato@hdfcbank", 220, 860, 4, False),
            ("BLINKIT", "blinkit@hdfcbank", 140, 1300, 5, False), ("ZEPTO", "zepto@ybl", 120, 900, 3, False),
            ("UBER INDIA", "uber@axisbank", 140, 620, 4, True), ("RAPIDO", "rapido@ybl", 50, 240, 4, False),
            ("STARBUCKS", "starbucks@hdfcbank", 290, 640, 2, True), ("CHAAYOS", "chaayos@ybl", 160, 420, 2, False),
            ("AMAZON PAY INDIA", "amazon@apl", 300, 4200, 2, True), ("MYNTRA", "myntra@icici", 800, 3600, 1, True),
            ("APOLLO PHARMACY", "apollo@axisbank", 120, 980, 1, False), ("BOOKMYSHOW", "bms@icici", 300, 1100, 1, True),
            ("HPCL FUEL", "hpcl@sbi", 1500, 3200, 1, False), ("NYKAA", "nykaa@icici", 400, 2100, 1, True)]
SORT_OUT = [("ANAND GENERAL STORES", "anandstores@okaxis", 180, 950), ("CARE MEDICALS", "caremedicals@paytm", 140, 760),
            ("VIKRAM BOSE", "vikram.bose@okicici", 600, 1500), ("KUMAR TAILORS", "kumartailors@ybl", 450, 1800)]


def _india_money(conn, settings, rnd, today, now) -> None:
    from . import ai, db, epf, fd, sync
    seq = [0]

    def text(body: str, when: datetime, sender: str = "JD-HDFCBK-S", source: str = "sms") -> None:
        if when > now:
            return
        seq[0] += 1
        sync.ingest(conn, {"id": f"demo{seq[0]}", "sender": sender, "subject": "", "ts_epoch": when.timestamp(),
                           "text": body}, source, settings)

    def upi(amount: float, payee: str, vpa: str, when: datetime) -> None:
        text(f"Dear Customer, Rs.{amount:.2f} has been debited from account **1234 to VPA {vpa} {payee} on "
             f"{when:%d-%m-%y}. Your UPI transaction reference number is {600000000000 + seq[0]}.", when)

    def card(amount: float, payee: str, when: datetime) -> None:
        text(f"INR {_in(amount)} spent on ICICI Bank Card XX9876 on {when:%d-%b-%y} at {payee}. "
             f"Avl Lmt: INR {_in(250000 - amount)}.", when, "VM-ICICIT-S")

    def credit(amount: float, by: str, when: datetime, bal: float | None = None) -> None:
        text(f"Your A/c XX1234 is credited with INR {_in(amount)} on {when:%d-%b-%y} by {by}."
             + (f" Avl Bal INR {_in(bal)}" if bal else ""), when)

    book = _Book(conn, settings, now)

    def in_dollars(when: datetime, usd: float, payee: str) -> None:     # read by Claude, converted at the day's rate
        rate = 88.9
        note = ai.fx_note("USD", usd, round(usd * rate, 2), rate, f"ECB rate for {when:%Y-%m-%d}", "INR").strip()
        book.add(when, usd * rate, "out", payee, "ICICI card ••9876",
                 f"[read by Claude] {note} Transaction of USD {usd:.2f} on ICICI Bank Card XX9876 at {payee}.",
                 source="sms", sender="VM-ICICIT-S", bank="ICICI")

    start = today - timedelta(days=425)
    trips = {today - timedelta(days=245): ("INDIGO", 11480, "MAKEMYTRIP", 8900),
             today - timedelta(days=95): ("AIR INDIA", 9640, "AIRBNB", 14200)}
    big = {today - timedelta(days=150): ("CROMA", 52490), today - timedelta(days=40): ("DECATHLON", 7480)}
    weights = [e[4] for e in EVERYDAY]
    bal = 210000.0
    d = start
    while d <= today:
        at = lambda h, m=0: datetime.combine(d, datetime.min.time()).replace(hour=h, minute=m)  # noqa: E731
        if (d + timedelta(days=1)).day == 1:          # paid on the last day of the month
            pay = 148500 if d < today - timedelta(days=200) else 162000           # a raise in the spring
            bal += pay - rnd.randint(95000, 130000)
            credit(pay, f"NEFT-ACME INDUSTRIES PVT LTD SALARY {d:%b}".upper(), at(9, 12), bal)
        if d.day == 1:
            upi(28000, "MEERA KULKARNI", "meera.k@okhdfcbank", at(8, 40))
        if d.day == 2:
            upi(15000, "GROWW", "groww.mf@axisbank", at(10, 5))
        if d.day == 7:
            upi(10000, "ZERODHA BROKING", "zerodha@hdfcbank", at(11, 30))
        if d.day == 3:
            upi(rnd.randint(16000, 31000), "CRED CLUB", "cred.club@axisb", at(19, 15))
        if d.day == 5:
            card(199, "NETFLIX", at(6, 2))
            card(119, "SPOTIFY", at(6, 4))
            in_dollars(at(6, 6), 20.00, "OPENAI CHATGPT")
            upi(799, "AIRTEL POSTPAID", "airtel@icici", at(9, 30))
            upi(1177, "JIO FIBER", "jiofiber@sbi", at(9, 35))
            upi(round(1100 + 1300 * max(0, math.sin((d.month - 2) / 12 * 2 * math.pi))), "KSEB ELECTRICITY",
                "kseb@billdesk", at(9, 40))
        if d.day == 10:
            card(1499, "CULTFIT", at(7, 0))
        if d.month in (3, 6, 9, 12) and (d + timedelta(days=1)).day == 1:
            credit(rnd.randint(1200, 2100), "INTEREST CREDIT", at(23, 0))
        if d in trips:
            fly, fare, stay, room = trips[d]
            card(fare, fly, at(13, 10))
            card(room, stay, at(13, 25))
        if d in big:
            card(big[d][1], big[d][0], at(17, 45))
        if d.day == 18 and rnd.random() < .4:
            credit(rnd.randint(400, 2400), "UPI-AMAZON REFUND", at(12, 0))
        if d.day in (12, 26) and rnd.random() < .5:
            credit(rnd.choice([650, 850, 1200]), "UPI-ARJUN MEHTA-arjun.m@okaxis", at(21, 10))
        for _ in range(rnd.choice([1, 2, 2, 3, 3, 4, 5])):
            payee, vpa, lo, hi, _w, by_card = rnd.choices(EVERYDAY, weights)[0]
            when = at(rnd.randint(8, 22), rnd.randint(0, 59))
            (card if by_card else lambda a, p, w: upi(a, p, vpa, w))(rnd.randint(lo, hi), payee, when)
        if (today - d).days <= 45 and rnd.random() < .3:                     # new payees, still to sort
            payee, vpa, lo, hi = rnd.choice(SORT_OUT)
            upi(rnd.randint(lo, hi), payee, vpa, at(rnd.randint(9, 20), rnd.randint(0, 59)))
        d += timedelta(days=1)
    for i, (sender, body) in enumerate([                                  # senders Margin doesn't count yet
            ("AD-PAYTMB", "Rs 450.00 paid to CARE MEDICALS from your Paytm Payments Bank a/c."),
            ("CP-JUPITR", "You spent Rs 2,340 at DECATHLON SPORTS using your Jupiter card."),
            ("VK-SWIGGY", "Your Swiggy order of Rs 389 is out for delivery. Paid via UPI.")]):
        text(body, now - timedelta(days=2 + i, hours=3), sender)
    text("Your ICICI Bank Credit Card XX9876 statement is ready. Total Amount Due: Rs. 24,310.00. Payment Due Date: "
         f"{(today + timedelta(days=9)):%d %b %Y}", now - timedelta(days=6), "credit_cards@icicibank.com", "email")
    first_salary = conn.execute("SELECT MIN(ts) FROM balances WHERE account='HDFC a/c ••1234'").fetchone()[0] or "9999"
    for m in range(24, -1, -1):                       # month-end balances back to the start of the charts
        month = today.replace(day=1) - timedelta(days=30 * m)
        conn.execute("INSERT OR REPLACE INTO balances VALUES (?,?,?,?)",
                     ("SBI a/c ••2222", f"{min(month.replace(day=20), today)}T10:00:00",
                      70000 + 1500 * (24 - m) + rnd.randint(-4000, 4000), "sms"))
        if month.replace(day=28).isoformat() < first_salary[:10]:
            conn.execute("INSERT OR REPLACE INTO balances VALUES (?,?,?,?)",
                         ("HDFC a/c ••1234", f"{month.replace(day=28)}T09:12:00",
                          150000 + 4000 * (24 - m) + rnd.randint(-12000, 12000), "sms"))

    asof = (today.replace(day=1) - timedelta(days=1)).isoformat()
    stamp = now.isoformat(timespec="seconds")
    for sid, kind in (("cas-demo", "cas"), ("nps-demo", "nps")):
        conn.execute("INSERT OR REPLACE INTO inv_statements VALUES (?,?,?,?,?,?)", (sid, kind, asof, stamp, 1, None))
    for kind, key, name, units, price, cost in [
            ("in_stock", "INE000D01011", "Nifty 50 ETF", 900, 284.6, 214000),
            ("in_stock", "INE000D01029", "Larsen & Toubro Ltd", 25, 3612.4, 78000),
            ("in_stock", "INE000D01037", "Asian Paints Ltd", 30, 2451.7, 69000),
            ("in_stock", "INE000D01045", "Titan Company Ltd", 20, 3388.5, 61000),
            ("in_stock", "INE000D01052", "Gold ETF", 1200, 78.4, 81000),
            ("mf", "INF000D01011", "Flexi Cap Fund - Direct Growth", 6120.4, 92.18, 430000),
            ("mf", "INF000D01029", "Nifty 50 Index Fund - Direct Growth", 9876.2, 41.06, 330000),
            ("mf", "INF000D01037", "Small Cap Fund - Direct Growth", 3410.9, 118.72, 300000),
            ("mf", "INF000D01045", "Liquid Fund - Direct Growth", 52.31, 3012.4, 150000),
            ("nps", "SM-E1", "Scheme E - Tier I", 4120.6, 58.31, 190000),
            ("nps", "SM-C1", "Scheme C - Tier I", 1980.2, 41.92, 74000),
            ("nps", "SM-G1", "Scheme G - Tier I", 2210.8, 33.75, 68000)]:
        conn.execute("INSERT INTO inv_positions VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("nps-demo" if kind == "nps" else "cas-demo", asof, kind, key, name, units, price, units * price,
                      cost, json.dumps({"broker": "Zerodha"} if kind == "in_stock" else {})))
        if kind in ("in_stock", "mf"):
            _prices(conn, rnd, today, {("NAV:" if kind == "mf" else "") + key: price}, "INR")
    _prices(conn, rnd, today, {"COST": 912.6, "V": 331.8, "VOO": 541.7, "JPM": 248.3}, "USD")
    from . import fx
    fx._cache(conn)
    for day, rate in (("2026-06-01", 84.6), ((today - timedelta(days=1)).isoformat(), 88.9), (today.isoformat(), 88.9)):
        conn.execute("INSERT OR REPLACE INTO fx_rates VALUES ('USD', ?, ?, ?)", (day, rate, day))
    _benchmarks(conn, rnd, today)
    for key, kind, name, level in (("INE000D01037", "in_stock", "Asian Paints Ltd", 2310.0),
                                   ("INE000D01045", "in_stock", "Titan Company Ltd", 3260.0)):
        conn.execute("INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?,?,?)", (key, kind, name, level, stamp, None, None))
    conn.execute(epf.SCHEMA)
    pf = 980000.0
    for m in range(24, 0, -1):
        day = (today.replace(day=1) - timedelta(days=30 * m)).replace(day=21)
        pf += 19800 + (pf * .0825 if day.month == 6 else 0)
        conn.execute("INSERT OR REPLACE INTO epf VALUES (?,?,?,?,?,?)",
                     (f"sms:epf{m}", f"{day}T11:00:00", "ZZDEM…0001", round(pf), 19800, f"{day:%b-%y}"))
    conn.execute(fd.SCHEMA)
    fd_start = today - timedelta(days=760)                     # a 3-year deposit, opened before the charts begin
    conn.execute("INSERT OR REPLACE INTO deposits VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("fd-demo", "HDFC", "••0000", 300000, fd_start.isoformat(), (fd_start + timedelta(days=1095)).isoformat(),
                  7.1, round(300000 * (1 + .071 / 4) ** 12), 1, 1, fd_start.isoformat()))
    db.set_state(conn, "last_sync", json.dumps({"at": now.replace(hour=0, minute=1).isoformat(timespec="seconds"),
                                                "ok": True, "problems": [], "counts": {"sms": 1840, "email": 96}}))


# --- shared ----------------------------------------------------------------------------------------------------
def _prices(conn, rnd, today, closes: dict, cur: str) -> None:
    for key, price in closes.items():
        for back in (3, 2, 1):
            close = price * (1 + rnd.uniform(-.012, .018) + .006 * (3 - back))
            conn.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?)",
                         (key, (today - timedelta(days=back)).isoformat(), round(close, 2),
                          round(close * (1 - rnd.uniform(-.015, .015)), 2), cur))


def _benchmarks(conn, rnd, today) -> None:
    for index, now_level, yearly in (("^NSEI", 24850, .11), ("^GSPC", 6020, .13)):    # calm, rising benchmarks
        for back in range(400, 0, -1):
            level = now_level * math.exp(-yearly * back / 365) * (1 + .018 * math.sin(back / 11)) \
                * (1 + rnd.gauss(0, .002))
            conn.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?,?)",
                         (index, (today - timedelta(days=back)).isoformat(), round(level, 2), None, "INR"))


def _investment_history(conn, settings, rnd, today) -> dict:
    """Week by week for two years, walking back from today's values: smooth, plausible, ending exactly today."""
    from . import invest
    rows, _ = invest.holdings(conn, settings)
    kinds = invest.by_kind(rows)
    walk = {"in_stock": (.12, .022, 2300), "mf": (.13, .016, 3500), "us_stock": (.18, .024, 0), "nps": (.10, .009, 1100),
            "epf": (.0825, 0, 5000), "ppf": (.071, 0, 0), "fd": (.071, 0, 0), "retirement": (.11, .012, 450),
            "cash": (.045, 0, 120), "other": (.06, .01, 0)}
    totals: dict[str, float] = {}
    for k, v in kinds.items():
        grow, swing, put_in = walk.get(k, (.08, .01, 0))
        val = v["value"]
        for w in range(105):
            day = (today - timedelta(days=7 * w)).isoformat()
            if w:
                conn.execute("INSERT OR REPLACE INTO inv_history VALUES (?,?,?,?)", (day, k, round(val), None))
            totals[day] = totals.get(day, 0) + val
            val = max((val - put_in) / (1 + grow / 52 + swing * rnd.gauss(0, 1)), v["value"] * .25)
    invest.record_today(conn, rows)
    return totals


def _networth_history(conn, rnd, today, totals: dict, cash_now: float) -> None:
    for day, inv_total in sorted(totals.items()):
        if date.fromisoformat(day).day > 7 and day != today.isoformat():
            continue                                  # one net-worth point a month
        months = (today - date.fromisoformat(day)).days / 30
        cash = cash_now * (1 - months / 60) + rnd.uniform(-.05, .05) * cash_now
        owed = rnd.uniform(.05, .12) * cash_now
        conn.execute("INSERT OR REPLACE INTO networth_history VALUES (?,?,?,?)",
                     (day, round(inv_total + cash), round(owed), round(inv_total + cash - owed)))


def _reviews(conn, settings, today, now) -> None:
    from . import ai, invreview, review
    for kind, step, n in (("daily", 1, 3), ("weekly", 7, 4), ("monthly", 31, 3)):
        for back in range(n):
            s, e = review.period(kind, today - timedelta(days=step * back))
            review.run(conn, kind, settings, start=s, end=e, force=True)
    for kind in ("weekly", "monthly"):
        invreview.run(conn, settings, kind, force=True)
    old = (today - timedelta(days=10)).isoformat()   # flags from before then were looked at long ago
    for (flags,) in conn.execute("SELECT flags FROM reviews WHERE end < ?", (old,)).fetchall():
        for fl in json.loads(flags or "[]"):
            review.set_flag(conn, fl["key"], "fine")
    ai.RUNNERS["audit"](conn, settings, None)        # older payments were checked long ago; Sort now stays quick


# --- the Claude stand-in ---------------------------------------------------------------------------------------
GUESSES = [("GENERAL STORE", "Groceries", "high", "A neighbourhood grocery store"),
           ("MEDICAL", "Medical", "high", "A pharmacy"), ("TAILOR", "Shopping", "medium", "A tailor: clothes"),
           ("PIZZA", "Food & dining", "high", "A pizza place"), ("CLEANERS", "Home services", "high", "Dry cleaning"),
           ("CHAI", "Food & dining", "high", "A tea stall")]


def demo_claude(payload: dict, kind: str, settings: dict, system: str | None = None, schema: dict | None = None,
                timeout: int = 300) -> tuple[dict | None, str]:
    """Answers like review.ask_claude, from the same payload, without running Claude."""
    from . import ai, invreview
    time.sleep(_DELAY)
    if schema is invreview.SCHEMA:
        return _investment_review(payload), ""
    if schema is None:
        return _spending_review(payload), ""
    decisions = []
    for it in payload.get("items", []):
        blob = json.dumps(it, ensure_ascii=False).upper()
        if schema is ai.SENDERS_SCHEMA:
            bank = any(k in blob for k in ("BANK", "PAYTMB", "JUPITR", "JUPITER"))
            decisions.append({"id": it["id"], "verdict": "bank" if bank else "not_bank",
                              "reason": "Reports money moving in an account" if bank else "Order updates, not bank alerts"})
        elif schema is ai.CATEGORIES_SCHEMA:
            cat, conf, why = next(((c, f, w) for k, c, f, w in GUESSES if k in blob),
                                  ("Miscellaneous", "low", "A person's name: yours to decide"))
            decisions.append({"id": it["id"], "category": cat, "confidence": conf, "reason": why})
        elif schema is ai.DOUBLES_SCHEMA:
            same = system is ai.MERGED_SYSTEM
            decisions.append({"id": it["id"], "verdict": "same" if same else "unsure",
                              "reason": "Same amount and time in both texts" if same else "Can't tell from the texts"})
        elif schema is ai.UNREAD_SCHEMA:
            decisions.append({"id": it["id"], "verdict": "unsure", "reason": "No amount in the text"})
        elif schema is ai.SPLITS_SCHEMA:
            decisions.append({"id": it["id"], "bill_id": "", "confidence": "low", "reason": "No matching bill"})
    return {"decisions": decisions}, ""


def _spending_review(p: dict) -> dict:
    from . import money
    from .categorize import NON_FLOW
    cur = p.get("home_currency", "INR")
    f = lambda x: money.fmt(x, cur, 0)   # noqa: E731
    t, prev, label = p["totals"], p["previous_period_totals"], p["period"]["label"]
    cats = sorted(((k, v) for k, v in t["by_category"].items() if k not in NON_FLOW and v > 0), key=lambda kv: -kv[1])
    out, before = t["out"], prev["out"]
    change = f"{abs(out - before) / before * 100:.0f}% {'less' if out < before else 'more'} than the period before" \
        if before else "the first period with data"
    head = f"{label}: spent {f(out)}, {change}" + (f"; {cats[0][0].lower()} led" if cats else "")
    bullets = [f"Spent {f(out)} (before: {f(before)})"
               + (f"; money in {f(t['in'])} (before: {f(prev['in'])})." if t["in"] or prev["in"] else ".")]
    if cats:
        bullets.append("Biggest categories: " + ", ".join(f"{k} {f(v)}" for k, v in cats[:3]) + ".")
    spends = [x for x in p["transactions"] if x["dir"] == "out" and x["category"] not in NON_FLOW]
    if spends:
        top = max(spends, key=lambda x: x["amount"])
        bullets.append(f"Largest payment: {f(top['amount'])} to {(top['payee'] or 'an unnamed payee').title()} "
                       f"({top['category']}).")
    subs = [x for x in spends if x["category"] == "Subscriptions"]
    if subs:
        bullets.append(f"Subscriptions came to {f(sum(x['amount'] for x in subs))} over {len(subs)} payments; "
                       "worth a glance if any go unused.")
    food = dict(cats).get("Food & dining")
    if food and out:
        bullets.append(f"Food & dining was {food / out * 100:.0f}% of spending.")
    flags = [{k: fl[k] for k in ("txn_ids", "severity", "title", "why")} for fl in p.get("local_flags", [])[:3]]
    return {"headline": head[:110], "summary": bullets[:6], "flags": flags}


def _investment_review(p: dict) -> dict:
    from . import money
    cur = p.get("home_currency", "INR")
    f = lambda x: money.fmt(x, cur, 0)   # noqa: E731
    tot, label = p["total"], p["period"]["label"]
    bench = {k: v for k, v in p["benchmarks_pct"].items() if v is not None}
    main = next(((k, v) for k, v in bench.items() if ("Nifty" in k) == (cur == "INR")), None)
    bullets, watch = [], []
    if tot["start_of_period"] and tot["end_of_period"]:
        ch = tot["end_of_period"] - tot["start_of_period"]
        pct = ch / tot["start_of_period"] * 100
        vs = "" if not main else f", {'ahead of' if pct >= main[1] else 'a little behind'} the {main[0]} ({main[1]:+.1f}%)"
        head = f"{label}: {'up' if ch >= 0 else 'down'} {f(abs(ch))} ({pct:+.1f}%){vs}"
        bullets.append(f"Investments went from {f(tot['start_of_period'])} to {f(tot['end_of_period'])}.")
    else:
        head = f"{label}: investments worth {f(tot['now'])}"
    moves = [(k, v["end_of_period"] - v["start_of_period"]) for k, v in p["by_type"].items()
             if v.get("start_of_period") and v.get("end_of_period")]
    if moves:
        k, v = max(moves, key=lambda kv: abs(kv[1]))
        bullets.append(f"{k} moved the most: {'+' if v >= 0 else '−'}{f(abs(v))}.")
    holdings = sorted(p["holdings"], key=lambda h: -h["weight_pct"])
    if holdings:
        h = holdings[0]
        bullets.append(f"Largest holding: {h['name']} at {h['weight_pct']}% of the total.")
    gains = [h for h in holdings if h.get("return_on_cost_pct") is not None]
    if gains:
        best = max(gains, key=lambda h: h["return_on_cost_pct"])
        bullets.append(f"{best['name']} is {best['return_on_cost_pct']:+.1f}% on cost, the best of the holdings.")
    for h in holdings:
        sl = h.get("stop_loss")
        if sl and sl["gap_pct"] <= 5:
            watch.append({"title": f"{h['name']} near its stop-loss",
                          "why": f"The close is {sl['gap_pct']}% above your stop at {money.fmt(sl['level'], h['currency'], 2)}."})
    for h in holdings[:3]:
        if h["weight_pct"] > 10:
            watch.append({"title": f"{h['name']} is {h['weight_pct']}% of the total", "why": "A large single holding."})
    return {"headline": head[:110], "summary": bullets[:6], "watch": watch[:4]}


# --- pretend sync and simulated Touch ID -------------------------------------------------------------------
PHASES = [("Reading bank texts", 18), ("Reading Gmail", 42), ("Reading statements", 3), ("Updating prices", 31)]
SYNC_SECONDS = 8.0
_SYNC = {"started": None}


def _start_sync() -> None:
    if _SYNC["started"] is None:
        _SYNC["started"] = time.time()


def _sync_status() -> tuple[bool, dict | None]:
    started = _SYNC["started"]
    if started is None:
        return False, None
    t = (time.time() - started) / SYNC_SECONDS
    if t >= 1:
        _SYNC["started"] = None
        from . import db
        conn = db.connect()
        db.set_state(conn, "last_sync", json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "ok": True,
                                                    "problems": [], "counts": {"sms": 18, "email": 42}}))
        conn.commit()
        conn.close()
        return False, None
    i = min(int(t * len(PHASES)), len(PHASES) - 1)
    phase, total = PHASES[i]
    done = round(total * (t * len(PHASES) - i))
    return True, {"phase": phase, "done": min(done, total), "total": total, "started": started}


def _use_stand_ins() -> None:
    from . import lock, review
    review.ask_claude = demo_claude
    review.claude_path = lambda settings: "demo"

    def unlock(settings):
        time.sleep(1.2)                     # about as long as a real fingerprint
        lock._STATE["until"] = time.time() + lock.minutes(settings) * 60
        return True, ""
    lock.unlock = unlock


class _V6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve(port: int = 8765, open_browser: bool = True, locked: bool = False) -> None:
    global _DELAY
    _guard()
    from . import lock, paths, server
    s = json.loads(paths.SETTINGS_PATH.read_text())
    s["lock_minutes"] = 2 if locked else 0
    paths.SETTINGS_PATH.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n")
    _use_stand_ins()
    _DELAY = 0.4
    lock.lock_now()
    server.HOSTS = (*server.HOSTS, HOST)
    server.DEMO = {"start": _start_sync, "status": _sync_status}
    httpds = []
    for cls, addr in ((ThreadingHTTPServer, "127.0.0.1"), (_V6, "::1")):
        try:
            h = cls((addr, port), server.Handler)
        except OSError:
            if not httpds:
                raise SystemExit(f"Port {port} is in use. Try: margin demo --port {port + 1}")
            continue
        h.key, h.touched, h.daemon_threads = KEY, time.time(), True
        httpds.append(h)
    url = f"http://{HOST}:{port}/"
    for h in httpds[1:]:
        threading.Thread(target=h.serve_forever, daemon=True).start()
    print(f"Margin demo (made-up data) at {url}  (first visit: {url}?k={KEY})\n"
          f"Data folder: {paths.DATA_DIR}. Your real Margin isn't touched. Ctrl-C stops it.", flush=True)
    if open_browser:
        subprocess.run(["open", f"{url}?k={KEY}"])
    try:
        httpds[0].serve_forever()
    except KeyboardInterrupt:
        pass
