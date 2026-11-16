"""Files you download from your bank, card issuer or broker into the statements folder, read at every sync.

Nothing here goes online: the files are read on this Mac, and only tickers are later priced (prices.py).

- Bank and card activity: a CSV or QFX/OFX download (every US bank and card issuer offers one). Each payment is
  stored like an alert's (source 'file'), sorted by the same rules, and counted once: a payment an alert already
  reported becomes that alert's duplicate. The file's balance (OFX's ledger balance, or the newest row of a CSV's
  running balance) becomes the account's balance, or for a card what's owed on it.
- Broker positions: a positions CSV export (Fidelity, Schwab, Vanguard, E*TRADE, or any CSV with a symbol and a
  quantity column), an investment QFX/OFX, or Robinhood's account activity report (shares rebuilt from its buys
  and sells). Shares per ticker and the cost basis when the file has it, valued each night at Yahoo's close like
  other US holdings (invest.py); positions without a ticker (some 401(k) funds) keep the file's own value.
  Accounts named IRA, Roth, 401(k), 403(b), 457, SEP, SIMPLE, TSP or retirement count as Retirement.

Which bank or broker, and the account's last 4 digits, come from the file (OFX, card-number and account columns)
or else its name: name a download like `chase-checking-1234.csv` when the bank doesn't. The newest file per
account wins for positions; payments from overlapping downloads are counted once. Files that are neither (a
holdings CSV with only a name and a value column) are left to networth.import_holdings, as before.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db, fx, money, parse, paths

BANK_EXT = (".csv", ".ofx", ".qfx")

# --- reading values -------------------------------------------------------------------------------------------
def _hdr(h: str) -> str:
    """'Trans. Date' -> 'trans date', 'Amount (USD)' -> 'amount usd', 'Qty (Quantity)' -> 'qty quantity'."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$%# ]", " ", (h or "").lower())).strip()


def _col(header: list[str], names: tuple[str, ...]) -> int | None:
    """Index of the first column whose name is one of these (tried in this order)."""
    norm = [_hdr(h) for h in header]
    for n in names:
        if n in norm:
            return norm.index(n)
    return None


def _money(s: str | None) -> float | None:
    """'$1,234.56' / '(1,234.56)' / '-$1.00' / '1,234.56 CR' -> float; '--', '' or 'n/a' -> None."""
    t = (s or "").strip()
    if not t:
        return None
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-") or "-$" in t or t.upper().endswith(" DR")
    t = re.sub(r"[^\d.]", "", t)
    if not t or t.count(".") > 1:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


_MON = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                    "dec"), 1)}


def _date(s: str, day_first: bool = False) -> str | None:
    """'11/14/2026', '11/14/26', '2026-11-14', 'Nov 14, 2026', '20261114120000' -> '2026-11-14'."""
    s = (s or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:\d{6})?(?:\.\d+)?(?:\[.*\])?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", s)
        if m:
            a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            y += 2000 if y < 100 else 0
            mo, d = (b, a) if day_first else (a, b)
        else:
            m = re.match(r"([A-Za-z]{3})[a-z]*\.?[\s-]+(\d{1,2}),?[\s-]+(\d{4})", s) or \
                re.match(r"(\d{1,2})[\s-]+([A-Za-z]{3})[a-z]*\.?[\s-]+(\d{4})", s)
            if not m:
                return None
            g = m.groups()
            mon, d = (g[0], g[1]) if g[0].isalpha() else (g[1], g[0])
            if mon.lower() not in _MON:
                return None
            y, mo, d = int(g[2]), _MON[mon.lower()], int(d)
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _day_first(values: list[str], cur: str) -> bool:
    """Whether a file writes 14/11/2026 (day first) or 11/14/2026: from any part above 12, else by currency."""
    for v in values:
        m = re.match(r"(\d{1,2})[/-](\d{1,2})[/-]\d{2,4}", (v or "").strip())
        if m and int(m.group(1)) > 12:
            return True
        if m and int(m.group(2)) > 12:
            return False
    return cur == "INR"


def _rows(path: Path) -> list[list[str]]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return [r for r in csv.reader(io.StringIO(text))]


# --- which bank or broker, which account -----------------------------------------------------------------------
# Bank and broker names in a file's name (or contents), as whole words: "chase" isn't in "purchase", nor
# "discover" in "Discovery". Digits may touch a name (Chase1234_Activity.csv).
FILE_BANKS = [(re.compile(rf"(?<![a-z])(?:{rx})(?![a-z])"), name) for rx, name in [
    (r"chase", "Chase"), (r"bank ?of ?america|bofa|boa", "Bank of America"), (r"wells ?fargo|wf", "Wells Fargo"),
    (r"capital ?one", "Capital One"), (r"discover", "Discover"), (r"citi(?:bank)?", "Citi"),
    (r"amex|american ?express", "Amex"), (r"u\.?s\.? ?bank", "U.S. Bank"), (r"pnc", "PNC"), (r"ally", "Ally"),
    (r"schwab", "Schwab"), (r"fidelity", "Fidelity"), (r"sofi", "SoFi"), (r"synchrony", "Synchrony"),
    (r"navy ?federal", "Navy Federal"), (r"usaa", "USAA"), (r"barclays?", "Barclays US"), (r"td ?bank", "TD Bank"),
    (r"apple ?card", "Apple Card"), (r"vanguard", "Vanguard"), (r"robinhood", "Robinhood"),
    (r"e ?\*? ?trade", "E*TRADE"), (r"merrill", "Merrill"), (r"marcus", "Marcus"), (r"chime", "Chime"),
]]
CARD_WORDS = re.compile(r"card|credit|visa|master ?card|amex|american ?express|discover|sapphire|freedom|venture|"
                        r"quicksilver|savor|double ?cash|custom ?cash|apple", re.I)
RETIRE_RE = re.compile(r"\b(?:ira|roth|401\s*\(?\s*k\s*\)?|403\s*\(?\s*b\s*\)?|457\s*\(?\s*b?\s*\)?|sep|simple|tsp|"
                       r"keogh|retirement|pension|rollover)\b", re.I)


def _bank_from(text: str) -> str | None:
    t = (text or "").lower().replace("_", " ").replace("-", " ")
    for rx, name in FILE_BANKS:
        if rx.search(t):
            return name
    for tok, name in parse.BANK_TOKENS:          # an Indian bank's download
        if len(tok) >= 4 and re.search(rf"(?<![a-z]){re.escape(tok.lower())}(?![a-z])", t):
            return name
    return None


def _last4_from_name(stem: str) -> str | None:
    """The account's last 4 digits from a file name like 'Chase1234_Activity_20261114' or 'ally-savings-1234'."""
    found = [g for g in re.findall(r"(?<!\d)(\d{4})(?!\d)", stem) if not 1990 <= int(g) <= 2099]
    return found[0] if len(set(found)) == 1 else None


def _digits4(v: str | None) -> str | None:
    d = re.sub(r"\D", "", v or "")
    return d[-4:] if len(d) >= 4 else None


# --- classifying a file ---------------------------------------------------------------------------------------
DATE_COLS = ("transaction date", "trans date", "date", "posting date", "posted date", "post date", "activity date",
             "settlement date", "date posted", "transaction posted date")
DESC_COLS = ("description", "payee", "merchant", "transaction description", "name", "original description",
             "merchant name", "memo", "details")
AMOUNT_COLS = ("amount", "transaction amount", "amount usd", "amount $", "amount inr", "net amount")
DEBIT_COLS = ("debit", "debits", "withdrawal", "withdrawals", "debit amount", "money out", "outflow",
              "withdrawal amount", "withdrawal amt", "charges", "debit usd")
CREDIT_COLS = ("credit", "credits", "deposit", "deposits", "credit amount", "money in", "inflow", "deposit amount",
               "deposit amt", "credit usd")
BALANCE_COLS = ("balance", "running bal", "running balance", "current balance", "available balance", "runningbalance",
                "closing balance", "balance usd", "ledger balance")
TYPE_COLS = ("type", "transaction type", "details", "trans type", "transaction", "debit credit")
CARDNO_COLS = ("card no", "card number", "card", "account number", "card member", "account")
STATUS_COLS = ("status",)

SYMBOL_COLS = ("symbol", "ticker", "symbol cusip", "security symbol", "ticker symbol")
QTY_COLS = ("quantity", "qty", "qty quantity", "shares", "units", "share count", "quantity shares", "shares held")
PRICE_COLS = ("last price", "price", "share price", "last price $", "current price", "closing price", "last",
              "market price", "price $")
VALUE_COLS = ("current value", "mkt val market value", "market value", "total value", "value", "value $",
              "current market value", "equity", "mkt val", "market value $", "marketvalue")
COST_COLS = ("cost basis total", "cost basis", "total cost basis", "total cost", "cost basis $", "cost")
UNIT_COST_COLS = ("average cost basis", "average cost", "avg cost", "price paid $", "price paid", "cost share",
                  "unit cost", "average price", "avg price", "average cost per share")
NAME_COLS = ("description", "investment name", "name", "security description", "security name", "fund name",
             "security")
ACCOUNT_COLS = ("account name", "account number", "account", "account name number", "account type")


def _header_kind(row: list[str]) -> str | None:
    """'positions', 'robinhood', 'bank' or None for one CSV row."""
    norm = [_hdr(c) for c in row]
    if "trans code" in norm and "instrument" in norm and "quantity" in norm:
        return "robinhood"
    if _col(row, SYMBOL_COLS) is not None and _col(row, QTY_COLS) is not None:
        return "positions"
    if _col(row, DATE_COLS) is not None and (_col(row, AMOUNT_COLS) is not None or _col(row, DEBIT_COLS) is not None
                                             or _col(row, CREDIT_COLS) is not None):
        return "bank"
    return None


def _wells_fargo_shape(rows: list[list[str]]) -> bool:
    """Wells Fargo's CSV has no header: "date","amount","*","check no","description"."""
    body = [r for r in rows if any(c.strip() for c in r)][:20]
    return bool(body) and all(len(r) >= 5 and _date(r[0]) and _money(r[1]) is not None and r[2].strip() in ("*", "")
                              for r in body)


def _classify(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in (".ofx", ".qfx"):
        head = path.read_text(errors="replace")[:200000].upper()
        if "<INVPOSLIST>" in head or "<INVSTMTRS>" in head:
            return "positions"
        return "bank" if "<STMTTRN>" in head or "<LEDGERBAL>" in head else None
    if ext != ".csv":
        return None
    try:
        rows = _rows(path)
    except OSError:
        return None
    for r in rows[:60]:
        k = _header_kind(r)
        if k:
            return "positions" if k == "robinhood" else k
    return "bank" if _wells_fargo_shape(rows) else None


US_FILE_SOURCES = (parse.US_BANKS | {"Vanguard", "Robinhood", "E*TRADE", "Merrill", "Apple Card"}) - {"Citi", "Amex"}


def classify(path: Path, home: str = "USD") -> str | None:
    """'bank' (activity), 'positions' (a broker's holdings) or None (not one of ours) for a statements file.
    With rupees at home, only files whose NAME says a US bank or broker (chase-1234.csv, fidelity-positions.csv)
    are ours: every other CSV, like the holdings exports of Indian apps, stays with networth.import_holdings, read
    as before."""
    kind = _classify(path)
    if kind and home == "INR" and _bank_from(path.stem) not in US_FILE_SOURCES:
        return None
    return kind


# --- bank and card activity -----------------------------------------------------------------------------------
class Activity:
    """One downloaded account: its payments, and its balance (or, for a card, what's owed) if the file has one."""

    def __init__(self, bank, kind, last4, cur, name=""):
        self.bank, self.kind, self.last4, self.cur = bank, kind, last4, cur
        # what identifies the account across downloads (the kind can read differently from month to month)
        self.key = f"{bank}:{last4}" if last4 else f"{bank}:{re.sub(r'[^a-z]', '', name.lower())}"
        self.txns: list[dict] = []          # {date, amount (signed: + in, - out), desc, fitid}
        self.balance: tuple[str, float] | None = None   # (date, amount): + money you have, - owed on a card

    @property
    def account(self) -> str:
        if not self.last4:
            return f"{self.bank} {'card' if self.kind == 'card' else 'a/c'}"
        return f"{self.bank} {'card' if self.kind == 'card' else 'a/c'} ••{self.last4}"


PAYMENT_TO_CARD = re.compile(r"payment|thank you|autopay|pymt|\bpaid\b", re.I)


def _ofx_tags(block: str) -> dict:
    """Leaf values of an OFX block, SGML (<TAG>value) or XML (<TAG>value</TAG>)."""
    return {k.upper(): v.strip() for k, v in re.findall(r"<([A-Za-z0-9.]+)>([^<\r\n]*)", block) if v.strip()}


def read_ofx(path: Path, home: str) -> list[Activity]:
    text = path.read_text(errors="replace")
    out = []
    for stmt in re.findall(r"<(?:STMTRS|CCSTMTRS)>(.*?)</(?:STMTRS|CCSTMTRS)>", text, re.S | re.I) or [text]:
        card = bool(re.search(r"<CCACCTFROM>", stmt, re.I))
        acct = re.search(r"<(?:BANKACCTFROM|CCACCTFROM)>(.*?)(?:</(?:BANKACCTFROM|CCACCTFROM)>|<BANKTRANLIST>)",
                         stmt, re.S | re.I)
        tags = _ofx_tags(acct.group(1)) if acct else {}
        org = _ofx_tags(text[:3000]).get("ORG", "")
        bank = _bank_from(org) or _bank_from(path.stem) or (org.title() if org else "Bank")
        cur = (_ofx_tags(stmt).get("CURDEF") or parse.currency_for(bank, home)).upper()
        a = Activity(bank, "card" if card or tags.get("ACCTTYPE") == "CREDITLINE" else "account",
                     _digits4(tags.get("ACCTID")) or _last4_from_name(path.stem), cur, path.stem)
        for t in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|</BANKTRANLIST>)", stmt, re.S | re.I):
            f = _ofx_tags(t)
            amt, day = _money(f.get("TRNAMT")), _date(f.get("DTPOSTED") or f.get("DTUSER") or "")
            if amt is None or not day or amt == 0:
                continue
            name, memo = f.get("NAME") or "", f.get("MEMO") or ""
            desc = name if not memo or memo == name else f"{name} {memo}".strip()
            a.txns.append({"date": day, "amount": amt, "desc": re.sub(r"\s+", " ", desc).strip(), "fitid": f.get("FITID")})
        bal = re.search(r"<LEDGERBAL>(.*?)(?:</LEDGERBAL>|<AVAILBAL>|</STMTRS>|</CCSTMTRS>|$)", stmt, re.S | re.I)
        if bal:
            b = _ofx_tags(bal.group(1))
            amt, day = _money(b.get("BALAMT")), _date(b.get("DTASOF") or "")
            if amt is not None and day:
                a.balance = (day, amt)
        out.append(a)
    return out


def read_csv(path: Path, home: str) -> list[Activity]:
    rows = _rows(path)
    hi = next((i for i, r in enumerate(rows[:60]) if _header_kind(r) == "bank"), None)
    if hi is None:
        if not _wells_fargo_shape(rows):
            return []
        hdr, body = ["date", "amount", "x", "check", "description"], [r for r in rows if any(c.strip() for c in r)]
    else:
        hdr, body = rows[hi], rows[hi + 1:]
    c = {k: _col(hdr, v) for k, v in (("date", DATE_COLS), ("desc", DESC_COLS), ("amount", AMOUNT_COLS),
                                       ("debit", DEBIT_COLS), ("credit", CREDIT_COLS), ("balance", BALANCE_COLS),
                                       ("type", TYPE_COLS), ("cardno", CARDNO_COLS), ("status", STATUS_COLS))}
    if c["desc"] is not None and c["desc"] == c["type"]:      # Chase calls its DEBIT/CREDIT column "Details"
        c["desc"] = _col([h if i != c["type"] else "" for i, h in enumerate(hdr)], DESC_COLS)
    get = lambda r, k: r[c[k]].strip() if c[k] is not None and c[k] < len(r) else ""   # noqa: E731
    bank = _bank_from(path.stem) or _bank_from(" ".join(hdr)) or "Bank"
    cur = parse.currency_for(bank, home)
    day_first = _day_first([get(r, "date") for r in body], cur)
    cards = {_digits4(get(r, "cardno")) for r in body if _digits4(get(r, "cardno"))}
    last4 = next(iter(cards)) if len(cards) == 1 else _last4_from_name(path.stem)
    raw = []
    for r in body:
        day = _date(get(r, "date"), day_first)
        if not day or re.search(r"pending", get(r, "status"), re.I):
            continue
        desc = get(r, "desc")
        if re.match(r"(?i)beginning balance|ending balance|opening balance|closing balance", desc):
            continue
        if c["amount"] is not None and _money(get(r, "amount")) is not None:
            amt = _money(get(r, "amount"))
        else:
            dr, cr = _money(get(r, "debit")), _money(get(r, "credit"))
            if dr is None and cr is None:
                continue
            amt = (abs(cr) if cr else 0.0) - (abs(dr) if dr else 0.0)
        if not amt:
            continue
        typ = get(r, "type").lower()
        raw.append({"date": day, "amount": amt, "desc": desc, "type": typ, "balance": _money(get(r, "balance"))})
    if not raw:
        return []
    is_card = c["balance"] is None and (
        bool(CARD_WORDS.search(re.sub(r"\d", " ", path.stem)))
        or (c["cardno"] is not None and "card" in _hdr(hdr[c["cardno"]]))
        or _col(hdr, ("category",)) is not None                        # card issuers' files sort spends by category
        or any(re.match(r"(?:sale|return|purchase)$", x["type"]) for x in raw)
        or any(PAYMENT_TO_CARD.search(x["desc"]) and "thank" in x["desc"].lower() for x in raw))
    a = Activity(bank, "card" if is_card else "account", last4, cur, path.stem)
    sign = _in_sign(raw, is_card, c["amount"] is not None, bank)
    for x in raw:
        a.txns.append({"date": x["date"], "amount": x["amount"] * sign, "desc": x["desc"], "fitid": None})
    with_bal = [x for x in raw if x["balance"] is not None]
    if with_bal and not is_card:
        a.balance = _newest_balance(with_bal, sign)
    return [a]


def _in_sign(raw: list[dict], is_card: bool, signed: bool, bank: str) -> int:
    """+1 if positive amounts are money in (most banks), -1 if positive means a charge (Discover, Amex, Apple
    Card). Read from the file itself where it can be: a running balance, the type column, card payments."""
    if not signed:
        return 1                                    # separate debit / credit columns: already signed above
    votes = 0
    for x in raw:                                   # the type column, where it says
        t = x["type"]
        if re.search(r"^(?:debit|sale|purchase|withdrawal|fee|charge)", t):
            votes += 1 if x["amount"] < 0 else -1
        elif re.search(r"^(?:credit|deposit|return|refund)", t):
            votes += 1 if x["amount"] > 0 else -1
    with_bal = [x for x in raw if x["balance"] is not None]
    for p, q in zip(with_bal, with_bal[1:]):        # balances step by the amount: + in, - out
        for older, newer in ((p, q), (q, p)):
            if abs(older["balance"] + newer["amount"] - newer["balance"]) < 0.01:
                votes += 1
            elif abs(older["balance"] - newer["amount"] - newer["balance"]) < 0.01:
                votes -= 1
    if is_card:                                     # a card's "payment, thank you" is money in
        for x in raw:
            if PAYMENT_TO_CARD.search(x["desc"]) and re.search(r"thank|autopay|payment", x["desc"], re.I):
                votes += 2 if x["amount"] > 0 else -2
    if votes:
        return 1 if votes > 0 else -1
    if is_card and re.search(r"discover|amex|apple", bank, re.I):
        return -1
    return 1


def _newest_balance(rows: list[dict], sign: int) -> tuple[str, float]:
    """The balance after the newest payment (files list newest first or oldest first)."""
    newest_first = rows[0]["date"] > rows[-1]["date"] or (
        rows[0]["date"] == rows[-1]["date"] and len(rows) > 1 and
        abs(rows[1]["balance"] + rows[0]["amount"] * sign - rows[0]["balance"]) < 0.01)
    r = rows[0] if newest_first else rows[-1]
    return r["date"], r["balance"]


_DESC_CUT = re.compile(r"\s+(?:PPD ID|WEB ID|CCD ID|DES:|ID:|INDN:|CO ID|ORIG CO NAME|TRACE)\b.*$|\s+(?:PPD|CCD|WEB)\s*$",
                       re.I)
_DESC_PREFIX = re.compile(
    r"^(?:POS (?:DEBIT|PURCHASE)|DEBIT CARD PURCHASE|PURCHASE AUTHORIZED ON \d\d/\d\d|RECURRING PAYMENT AUTHORIZED ON "
    r"\d\d/\d\d|CHECKCARD \d{4}|ACH (?:DEBIT|CREDIT|PMT|WITHDRAWAL|DEPOSIT)|DEBIT CARD|CARD PURCHASE(?: WITH PIN)?|"
    r"PURCHASE|SQ \*|SQ\*|TST\* ?|PP\*|POS)\s*[-:]?\s*", re.I)


# ACH descriptions carry fields that can hold the account holder's name or ids (Bank of America's "INDN:", "ID:",
# "CO ID:", Chase's "IND NAME:", "IND ID:", "TRACE#:"). They're dropped before a row is stored, so they never
# reach the page's text or Claude. "DES:" and "CO ENTRY DESCR:" stay: they say what the payment is (PAYROLL).
_LABELS = (r"DES|INDN|IND ?NAME|IND ?ID|INDIVIDUAL ID|CO ID|ID|CO ENTRY DESCR|ORIG CO NAME|ORIG ID|ORIG CO ID|DESC DATE|"
           r"SEC|TRACE ?#?|TRN|EED|PPD ID|WEB ID|CCD ID|CO NAME|RECEIVER|RECIPIENT")
_PRIVATE = re.compile(
    r"\s*\b(?:INDN|IND ?NAME|IND ?ID|INDIVIDUAL ID|CO ID|ORIG CO ID|ORIG ID|ID|TRACE ?#?|TRN|EED|PPD ID|WEB ID|"
    rf"CCD ID|RECEIVER|RECIPIENT)\s*[:#]\s*.*?(?=\s+(?:{_LABELS})\s*[:#]|\s+(?:PPD|CCD|WEB)\s*$|$)", re.I)


def scrub(desc: str) -> str:
    """A bank's description without the fields that can name or identify the account holder:
    'ACME CORP DES:PAYROLL ID:XXXXX INDN:JORDAN LEE CO ID:XXXXX PPD' -> 'ACME CORP DES:PAYROLL PPD'."""
    return re.sub(r"\s+", " ", _PRIVATE.sub("", desc or "")).strip()


def payee(desc: str) -> str | None:
    """A payee name from a bank's description: 'ZELLE PAYMENT TO MARIA GARCIA JPM99ABC' -> 'MARIA GARCIA',
    'PURCHASE AUTHORIZED ON 11/14 DEMO SHOP SEATTLE WA S300000000' -> 'DEMO SHOP SEATTLE WA'."""
    d = re.sub(r"\s+", " ", desc or "").strip()
    o = re.search(rf"ORIG CO NAME\s*:\s*(.+?)(?=\s+(?:{_LABELS})\s*[:#]|$)", d, re.I)   # Chase's long ACH form
    if o:
        d = o.group(1)
    z = re.match(r"(?i)zelle (?:payment |transfer )?(?:to|from)\s+(.+?)(?:\s+(?:on|conf#|jpm\w*|ref\b|#).*)?$", d)
    if z:
        d = z.group(1)
    d = _DESC_CUT.sub("", d)
    for _ in range(2):
        d = _DESC_PREFIX.sub("", d)
    d = re.sub(r"\s+\d{2}/\d{2}(?:/\d{2,4})?\b.*$", "", d)          # a date, then location and references
    d = re.sub(r"\s+(?:[A-Z]*\d[A-Z\d]{7,}|#\s*\d+|\d{5,})\b", "", d)   # reference and card numbers
    d = re.sub(r"\s+", " ", d).strip(" -*#.,")
    return d[:48] or None


def _uid(account: str, t: dict, n: int) -> str:
    if t.get("fitid"):
        return f"file:{account}:{t['fitid']}"
    key = f"{t['date']}|{t['amount']:.2f}|{re.sub(r'[^a-z0-9]', '', (t['desc'] or '').lower())}|{n}"
    return f"file:{account}:{hashlib.sha1(key.encode()).hexdigest()[:16]}"


def store_activity(conn: sqlite3.Connection, a: Activity, source_file: str, settings: dict) -> int:
    """Payments from one downloaded account into transactions, its balance into balances or card_dues.
    Returns the payments added."""
    from . import sync   # noqa: PLC0415
    home = money.home(settings)
    seen: dict[tuple, int] = {}
    added = 0
    for t in a.txns:
        k = (t["date"], round(t["amount"], 2), t["desc"])
        n = seen[k] = seen.get(k, -1) + 1             # two identical coffees on one day are two payments
        uid = _uid(a.key, t, n)
        if conn.execute("SELECT 1 FROM transactions WHERE uid=?", (uid,)).fetchone():
            continue
        amt, direction = round(abs(t["amount"]), 2), "in" if t["amount"] > 0 else "out"
        note = ""
        if a.cur != home:
            rate, src = fx.rate(conn, a.cur, home, t["date"], settings)
            if rate is None:
                continue                                # tried again next sync
            sym = money.symbol(home).strip()
            note = f"[{a.cur} {amt:g} ≈ {sym}{amt * rate:,.2f} at {sym}{rate:g}/{a.cur}, {src}] "
            amt = round(amt * rate, 2)
        party = payee(scrub(t["desc"]))
        raw = f"{note}{scrub(t['desc'])}"     # the file's name is in sender, so rules never match on it
        cat = sync._category(direction, party or "", raw, amt, settings)
        cur = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (uid,source,sender,bank,ts,amount,direction,account,last4,counterparty,vpa,category,raw)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uid, "file", f"file:{source_file}"[:80], a.bank, f"{t['date']}T12:00:00", amt, direction, a.account,
             a.last4, party, None, cat, raw[:1000]))
        if cur.rowcount:
            added += 1
            _mark_alert_duplicate(conn, cur.lastrowid, t["date"], amt, direction, a.last4)
    if a.balance:
        day, amt = a.balance
        rate = 1.0 if a.cur == home else fx.rate(conn, a.cur, home, day, settings)[0]
        if rate is not None:
            if a.kind == "card":
                if amt < 0:                              # OFX: a card's balance is negative while you owe
                    conn.execute("INSERT OR REPLACE INTO card_dues VALUES(?,?,?,?)",
                                 (a.account, f"{day}T23:59:00", round(-amt * rate, 2), None))
            else:
                conn.execute("INSERT OR REPLACE INTO balances VALUES(?,?,?,?)",
                             (a.account, f"{day}T23:59:00", round(amt * rate, 2), "file"))
    return added


def _mark_alert_duplicate(conn: sqlite3.Connection, rowid: int, day: str, amount: float, direction: str,
                          last4: str | None) -> None:
    """A payment the bank's alert already reported: keep the alert (it has the payee and time), count this once.
    Files carry the posting date, a few days after the alert; same amount, same card or account. The kept alert
    may already have its text or email twin as a duplicate (sync._mark_duplicate): one file row joins them."""
    d = date.fromisoformat(day)
    row = conn.execute(
        """SELECT id FROM transactions k WHERE id != ? AND source != 'file' AND duplicate_of IS NULL AND direction = ?
           AND ABS(amount - ?) < 0.01 AND ts BETWEEN ? AND ? AND (last4 IS NULL OR ? IS NULL OR last4 = ?)
           AND NOT EXISTS (SELECT 1 FROM transactions d WHERE d.duplicate_of = k.id AND d.source = 'file')
           ORDER BY ABS(julianday(substr(ts, 1, 10)) - julianday(?)) LIMIT 1""",
        (rowid, direction, amount, (d - timedelta(days=5)).isoformat(), (d + timedelta(days=2)).isoformat() + "T99",
         last4, last4, day)).fetchone()
    if row:
        conn.execute("UPDATE transactions SET duplicate_of=? WHERE id=?", (row["id"], rowid))


# --- broker positions -----------------------------------------------------------------------------------------
_NOT_A_HOLDING = re.compile(r"^(?:account total|total|totals|grand total|pending activity|--|n/a|"
                            r"cash & cash investments total|positions total)\b", re.I)
_CASH = re.compile(r"cash|money market|core position|sweep|spaxx|fdrxx|vmfxx|swvxx|fcash", re.I)
_TICKER = re.compile(r"[A-Z]{1,5}(?:[.\-/][A-Z]{1,2})?")
_ACCOUNT_LINE = re.compile(r"\.{3}\s?\d|[-#]\s?\d{3,}|\b(?:individual|joint|brokerage|trust|custodial|account)\b|"
                           + RETIRE_RE.pattern, re.I)


def _ticker(sym: str) -> str | None:
    s = (sym or "").strip().upper().rstrip("*").strip()
    if not _TICKER.fullmatch(s) or s in ("CASH", "TOTAL", "N/A"):
        return None
    return re.sub(r"[./]", "-", s)            # BRK.B / BRK/B -> Yahoo's BRK-B


BROKER_SHAPES = [   # a broker's name, or the shape of its positions export
    (r"fidelity|portfolio_positions|last price change|cost basis total", "Fidelity"),
    (r"schwab|positions for account|mkt val \(market value\)|qty \(quantity\)", "Schwab"),
    (r"vanguard|investment name,symbol,shares,share price", "Vanguard"),
    (r"e\*?trade|portfoliodownload|price paid \$", "E*TRADE"),
    (r"robinhood|trans code", "Robinhood"), (r"merrill", "Merrill"), (r"\bsofi\b", "SoFi"), (r"\bally\b", "Ally"),
]


def _broker(text: str, stem: str) -> str:
    t = f"{stem}\n{text[:5000]}".lower()
    for rx, name in BROKER_SHAPES:
        if re.search(rx, t):
            return name
    return "Brokerage"


def _file_day(path: Path, text: str) -> str:
    """The positions' date: from the file ('as of ... 2026/11/14', 'Date downloaded Nov-14-2026', the name), or
    else when it was saved."""
    for rx in (r"as of[^,\n\"]*?,?\s*(\d{4}/\d{2}/\d{2})", r"as of\s+(\d{1,2}/\d{1,2}/\d{4})",
               r"downloaded\s+([A-Za-z]{3}-\d{1,2}-\d{4})", r"as of\s+([A-Za-z]{3}[a-z]* \d{1,2},? \d{4})"):
        m = re.search(rx, text, re.I)
        if m:
            d = _date(m.group(1).replace("/", "-") if re.match(r"\d{4}/", m.group(1)) else m.group(1))
            if d:
                return d
    m = re.search(r"([A-Za-z]{3})-(\d{1,2})-(\d{4})", path.stem)
    d = _date(f"{m.group(1)} {m.group(2)}, {m.group(3)}") if m else None
    m = re.search(r"\d{4}-\d{2}-\d{2}", path.stem)
    d = d or (_date(m.group(0)) if m else None)
    if d:
        return d
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def read_positions(path: Path) -> list[dict]:
    """[{account, retirement, as_of, positions: [{ticker, name, shares, price, value, cost}]}] from one file."""
    if path.suffix.lower() in (".ofx", ".qfx"):
        return _positions_ofx(path)
    rows = _rows(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    broker, as_of = _broker(text, path.stem), _file_day(path, text)
    if any(_header_kind(r) == "robinhood" for r in rows[:5]):
        return _positions_robinhood(rows, broker, path)
    accounts: dict[str, dict] = {}
    section, cols, summary_col = None, None, None
    for r in rows:
        cells = [x.strip() for x in r]
        filled = [x for x in cells if x]
        if not filled:
            continue
        kind = _header_kind(r)
        if kind == "positions":
            cols = {k: _col(r, v) for k, v in (("sym", SYMBOL_COLS), ("qty", QTY_COLS), ("price", PRICE_COLS),
                                               ("value", VALUE_COLS), ("cost", COST_COLS), ("unit", UNIT_COST_COLS),
                                               ("name", NAME_COLS), ("acct", ACCOUNT_COLS), ("acctname", ("account name",)),
                                               ("acctno", ("account number",)))}
            continue
        if kind:                                    # Vanguard: the transactions after the positions
            cols = None
            continue
        if len(filled) == 1 and _ACCOUNT_LINE.search(filled[0]) and len(filled[0]) <= 90:
            # "Positions for account Individual ...123 as of 09:00 PM ET, 2026/11/14", Schwab's "Roth IRA ...456"
            section = re.sub(r"(?i)^positions for account\s+|\s+as of\b.*$", "", filled[0]).strip()
            continue
        if cols is None:
            if summary_col is not None and summary_col < len(cells) and cells[summary_col]:
                section, summary_col = cells[summary_col], None     # E*TRADE: the account under "Account"
            elif "account" in [_hdr(x) for x in cells]:
                summary_col = [_hdr(x) for x in cells].index("account")
            continue
        g = lambda k: cells[cols[k]] if cols[k] is not None and cols[k] < len(cells) else ""   # noqa: E731
        sym, name = g("sym"), g("name")
        if not sym and not name or _NOT_A_HOLDING.match(sym or name):
            continue
        acct_name, acct_no = g("acctname"), g("acctno")
        label_bits = [broker] + [x for x in (acct_name or (g("acct") if not acct_no else ""), section) if x]
        last4 = _digits4(acct_no) or _digits4(g("acct")) or _digits4(re.sub(r"\D", "", section or "")) or None
        label = " ".join(dict.fromkeys(b for b in label_bits if b and not re.fullmatch(r"[\d.\-X*]+", b)))
        label = re.sub(r"\s*(?:\.{2,}|[X*]+|-|#)?\s*\d{3,}\s*$", "", label).strip() + (f" ••{last4}" if last4 else "")
        acct = accounts.setdefault(label, {"account": label, "as_of": as_of, "positions": [],
                                           "retirement": bool(RETIRE_RE.search(f"{label} {path.stem}"))})
        qty, price, value = _money(g("qty")), _money(g("price")), _money(g("value"))
        cost = _money(g("cost"))
        if cost is None and _money(g("unit")) is not None and qty:
            cost = round(_money(g("unit")) * qty, 2)
        ticker = _ticker(sym)
        if ticker and _CASH.search(f"{sym} {name}") and (qty is None or price in (None, 1.0)):
            ticker = None                           # a money market sweep: its dollar value is the holding
        if qty is None or qty == 0:
            if value is None or not _CASH.search(f"{sym} {name}"):
                continue
            acct["positions"].append({"ticker": None, "name": name or sym or "Cash", "shares": 1, "price": value,
                                      "value": value, "cost": value})
            continue
        if value is None and price is not None:
            value = price * qty
        if price is None and value is not None:
            price = value / qty
        acct["positions"].append({"ticker": ticker, "name": name or sym, "shares": qty, "price": price,
                                  "value": value, "cost": cost, "symbol": sym.rstrip("*")})
    return [a for a in accounts.values() if a["positions"]]


def _positions_robinhood(rows: list[list[str]], broker: str, path: Path) -> list[dict]:
    """Robinhood's account activity report: shares rebuilt from buys, sells and splits (export it from the
    account's start for complete holdings)."""
    hdr = next(r for r in rows if _header_kind(r) == "robinhood")
    c = {k: _col(hdr, (k,)) for k in ("activity date", "instrument", "trans code", "quantity", "amount", "description")}
    events = []
    for r in rows[rows.index(hdr) + 1:]:
        g = lambda k: r[c[k]].strip() if c[k] is not None and c[k] < len(r) else ""   # noqa: E731
        day, code, sym = _date(g("activity date")), g("trans code").upper(), _ticker(g("instrument"))
        if not day or not sym or code not in ("BUY", "SELL", "SPL", "REC", "SXCH"):
            continue
        events.append((day, code, sym, _money(g("quantity")) or 0.0, abs(_money(g("amount")) or 0.0),
                       g("description").split("\n")[0].strip()))
    held: dict[str, dict] = {}
    for day, code, sym, qty, amt, desc in sorted(events, key=lambda e: e[0]):
        h = held.setdefault(sym, {"ticker": sym, "name": desc or sym, "shares": 0.0, "cost": 0.0, "price": None,
                                  "value": None, "symbol": sym})
        if code == "BUY":
            h["shares"] += qty
            h["cost"] += amt
        elif code == "SELL" and h["shares"]:
            frac = min(1.0, qty / h["shares"])
            h["shares"], h["cost"] = max(0.0, h["shares"] - qty), h["cost"] * (1 - frac)
        elif code in ("SPL", "REC"):
            h["shares"] += qty
    as_of = max((e[0] for e in events), default=datetime.fromtimestamp(path.stat().st_mtime).date().isoformat())
    pos = [h for h in held.values() if h["shares"] > 1e-6]
    return [{"account": broker, "as_of": as_of, "retirement": bool(RETIRE_RE.search(path.stem)), "positions": pos}] if pos else []


def _block(tag: str, text: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S | re.I)
    return m.group(1) if m else ""


def _positions_ofx(path: Path) -> list[dict]:
    """An investment QFX/OFX: positions (units, price, value) with tickers from its securities list."""
    text = path.read_text(errors="replace")
    secs = {}
    for info in re.findall(r"<SECINFO>(.*?)</SECINFO>", text, re.S | re.I):
        t = _ofx_tags(info)
        secs[t.get("UNIQUEID")] = (t.get("TICKER"), t.get("SECNAME"))
    acct = _ofx_tags(_block("INVACCTFROM", text))
    broker = _bank_from(acct.get("BROKERID", "")) or _broker(text[:3000], path.stem)
    last4 = _digits4(acct.get("ACCTID"))
    label = broker + (f" ••{last4}" if last4 else "")
    head = re.search(r"<INVSTMTRS>(.{0,600})", text, re.S | re.I)
    as_of = _date(_ofx_tags(head.group(1)).get("DTASOF", "") if head else "") or \
        datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    pos = []
    for p in re.findall(r"<INVPOS>(.*?)</INVPOS>", text, re.S | re.I):
        t = _ofx_tags(p)
        units, price, value = _money(t.get("UNITS")), _money(t.get("UNITPRICE")), _money(t.get("MKTVAL"))
        if not units:
            continue
        tick, name = secs.get(t.get("UNIQUEID"), (None, None))
        pos.append({"ticker": _ticker(tick or ""), "name": name or tick or t.get("UNIQUEID"), "shares": units,
                    "price": price, "value": value, "cost": None, "symbol": tick})
    cash = _money(_ofx_tags(_block("INVBAL", text)).get("AVAILCASH"))
    if cash:
        pos.append({"ticker": None, "name": "Cash", "shares": 1, "price": cash, "value": cash, "cost": cash})
    return [{"account": label, "as_of": as_of, "retirement": bool(RETIRE_RE.search(f"{label} {path.stem}")),
             "positions": pos}] if pos else []


def store_positions(conn: sqlite3.Connection, accounts: list[dict], source_file: str) -> int:
    """One statement per account and date (kind 'broker'), its positions as 'us_stock' or 'retirement'."""
    n = 0
    for a in accounts:
        key = re.sub(r"\s+", " ", a["account"]).strip()
        sid = f"broker:{key}:{a['as_of']}"
        conn.execute("DELETE FROM inv_positions WHERE stmt=?", (sid,))
        conn.execute("INSERT OR REPLACE INTO inv_statements(id, kind, as_of, read_at, ok, note) VALUES (?,?,?,?,?,?)",
                     (sid, "broker", a["as_of"], datetime.now().isoformat(timespec="seconds"), 1,
                      json.dumps({"account": key, "file": source_file})))
        kind = "retirement" if a["retirement"] else "us_stock"
        for p in a["positions"]:
            pkey = f"FILE:{key}:{p['ticker'] or p.get('symbol') or p['name']}"
            conn.execute("""INSERT INTO inv_positions(stmt, as_of, kind, key, name, units, price, value, cost, extra)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (sid, a["as_of"], kind, pkey, p["name"], p["shares"], p["price"], p["value"], p["cost"],
                          json.dumps({"ticker": p["ticker"], "account": key, "file": source_file})))
            n += 1
    return n


def broker_positions(conn: sqlite3.Connection) -> list[dict]:
    """The newest positions of each account read from files: [{kind, key, name, ticker, account, units, price,
    value, cost, as_of}] (prices and values as the file had them, in dollars)."""
    latest: dict[str, sqlite3.Row] = {}
    for st in conn.execute("SELECT id, as_of, note FROM inv_statements WHERE kind='broker' AND ok=1 ORDER BY as_of"):
        latest[json.loads(st["note"] or "{}").get("account", st["id"])] = st
    out = []
    for acct, st in latest.items():
        for p in conn.execute("SELECT * FROM inv_positions WHERE stmt=?", (st["id"],)):
            x = json.loads(p["extra"] or "{}")
            out.append({"kind": p["kind"], "key": p["key"], "name": p["name"], "ticker": x.get("ticker"),
                        "account": acct, "units": p["units"], "price": p["price"], "value": p["value"],
                        "cost": p["cost"], "as_of": st["as_of"]})
    return out


def tickers(conn: sqlite3.Connection) -> list[str]:
    return sorted({p["ticker"] for p in broker_positions(conn) if p["ticker"]})


# --- the nightly read -----------------------------------------------------------------------------------------
def read(conn: sqlite3.Connection, settings: dict) -> list[str]:
    """Read new or changed downloads in the statements folder. Returns notes (counts and file names only)."""
    notes = []
    home = money.home(settings)
    if not paths.STATEMENTS_DIR.exists():
        return notes
    for f in sorted(paths.STATEMENTS_DIR.iterdir()):
        if f.suffix.lower() not in BANK_EXT or not f.is_file():
            continue
        st = f.stat()
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
        if db.get_state(conn, f"file:{f.name}") == stamp:
            continue
        try:
            kind = classify(f, home)
            if kind == "bank":
                acts = read_ofx(f, home) if f.suffix.lower() in (".ofx", ".qfx") else read_csv(f, home)
                if not acts:
                    notes.append(f"{f.name}: no payments found")
                for a in acts:
                    store_activity(conn, a, f.name, settings)
            elif kind == "positions":
                accts = read_positions(f)
                if not accts:
                    notes.append(f"{f.name}: no positions found")
                store_positions(conn, accts, f.name)
            else:
                continue
        except (OSError, ValueError, KeyError, IndexError, csv.Error) as e:
            notes.append(f"{f.name}: couldn't read it ({type(e).__name__})")
            continue
        db.set_state(conn, f"file:{f.name}", stamp)
    return notes
