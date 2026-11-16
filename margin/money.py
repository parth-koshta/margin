"""Currencies: your home currency, and how each currency is written.

Margin keeps every amount in ONE home currency ("currency" in settings.json, INR unless you set another). Money
in any other currency (a card spend abroad, a US stock, an Indian account when you live elsewhere) is converted
at the day's ECB rate (fx.py) and keeps a note of the original amount, so the page can show "$12.99 ≈ ₹1,085".

Each currency is written its own way: INR with Indian grouping and lakh/crore short forms (₹1,23,456 · ₹1.2L ·
₹3.4Cr), the rest with thousands grouping and K/M/B (US$: $123,456 · $1.2M), and with the currency's own decimals
(¥ and ₩ have none). The page uses the same rules through each currency's locale (Intl.NumberFormat).
"""
from __future__ import annotations

import json
import re

# code: (symbol, decimals, locale for the page)
CURRENCIES = {
    "INR": ("₹", 2, "en-IN"), "USD": ("$", 2, "en-US"), "EUR": ("€", 2, "en-IE"), "GBP": ("£", 2, "en-GB"),
    "JPY": ("¥", 0, "ja-JP"), "CNY": ("CN¥", 2, "zh-CN"), "AUD": ("A$", 2, "en-AU"), "CAD": ("C$", 2, "en-CA"),
    "SGD": ("S$", 2, "en-SG"), "HKD": ("HK$", 2, "en-HK"), "NZD": ("NZ$", 2, "en-NZ"), "CHF": ("CHF ", 2, "de-CH"),
    "AED": ("AED ", 2, "en-AE"), "SAR": ("SAR ", 2, "en-SA"), "QAR": ("QAR ", 2, "en-QA"), "KWD": ("KWD ", 3, "en-KW"),
    "THB": ("฿", 2, "th-TH"), "MYR": ("RM", 2, "en-MY"), "IDR": ("Rp", 0, "id-ID"), "PHP": ("₱", 2, "en-PH"),
    "KRW": ("₩", 0, "ko-KR"), "LKR": ("Rs ", 2, "en-LK"), "NPR": ("Rs ", 2, "en-NP"), "BDT": ("৳", 2, "en-BD"),
    "PKR": ("Rs ", 2, "en-PK"), "ZAR": ("R ", 2, "en-ZA"), "BRL": ("R$", 2, "pt-BR"), "MXN": ("MX$", 2, "es-MX"),
    "SEK": ("SEK ", 2, "sv-SE"), "NOK": ("NOK ", 2, "nb-NO"), "DKK": ("DKK ", 2, "da-DK"), "PLN": ("zł ", 2, "pl-PL"),
}
DEFAULT = "INR"

# How amounts are labelled in bank texts and emails -> currency code (checked longest first)
_TOKENS = {"₹": "INR", "RS.": "INR", "RS": "INR", "INR": "INR", "US$": "USD", "USD": "USD", "$": "USD",
           "€": "EUR", "EUR": "EUR", "£": "GBP", "GBP": "GBP", "¥": "JPY", "JPY": "JPY", "A$": "AUD", "AUD": "AUD",
           "C$": "CAD", "CAD": "CAD", "S$": "SGD", "SGD": "SGD", "HK$": "HKD", "HKD": "HKD", "AED": "AED",
           "DHS": "AED", "SAR": "SAR", "CHF": "CHF", "THB": "THB", "฿": "THB", "MYR": "MYR", "RM": "MYR",
           "IDR": "IDR", "PHP": "PHP", "₱": "PHP", "KRW": "KRW", "₩": "KRW", "LKR": "LKR", "NPR": "NPR",
           "BDT": "BDT", "৳": "BDT", "ZAR": "ZAR", "NZD": "NZD", "CNY": "CNY", "RMB": "CNY", "QAR": "QAR",
           "KWD": "KWD", "SEK": "SEK", "NOK": "NOK", "DKK": "DKK", "PLN": "PLN", "BRL": "BRL", "MXN": "MXN"}
_TOKEN_RE = "|".join(re.escape(t) for t in sorted(_TOKENS, key=len, reverse=True))
AMOUNT_ANY = re.compile(rf"(?<![A-Za-z])({_TOKEN_RE})\s?([0-9][0-9,]*(?:\.[0-9]{{1,3}})?)", re.I)


def home(settings: dict | None = None) -> str:
    code = str((settings or {}).get("currency") or DEFAULT).upper()
    return code if code in CURRENCIES else DEFAULT


def symbol(code: str) -> str:
    return CURRENCIES.get(code.upper(), (code.upper() + " ", 2, "en-US"))[0]


def decimals(code: str) -> int:
    return CURRENCIES.get(code.upper(), ("", 2, ""))[1]


def info(code: str) -> dict:
    """What the page needs to format this currency."""
    sym, dec, loc = CURRENCIES.get(code, (code + " ", 2, "en-US"))
    return {"code": code, "symbol": sym.strip(), "decimals": dec, "locale": loc,
            "grouping": "indian" if code == "INR" else "western"}


def _group(whole: str, indian: bool) -> str:
    if len(whole) <= 3:
        return whole
    if not indian:
        return f"{int(whole):,}"
    head, parts = whole[:-3], [whole[-3:]]
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(([head] if head else []) + parts)


def fmt(x: float, code: str = DEFAULT, places: int | None = None, sign: bool = False) -> str:
    """₹1,23,456 / $123,456 / ¥12,346. places=None: whole units (cents shown only below 100)."""
    code = code.upper()
    if places is None:
        places = decimals(code) if abs(x) < 100 else 0
    places = min(places, decimals(code))
    num = f"{abs(x):.{places}f}"
    whole, _, frac = num.partition(".")
    body = _group(whole, code == "INR") + (f".{frac}" if frac else "")
    neg = "−" if x < 0 else ("+" if sign and x > 0 else "")
    return f"{neg}{symbol(code)}{body}"


def short(x: float, code: str = DEFAULT) -> str:
    """₹1.2L, ₹3.4Cr / $1.2M, $45K."""
    code, a = code.upper(), abs(x)
    s = "−" if x < 0 else ""
    steps = [(1e7, "Cr"), (1e5, "L"), (1e3, "k")] if code == "INR" else [(1e9, "B"), (1e6, "M"), (1e3, "K")]
    for size, unit in steps:
        if a >= size:
            v = a / size
            return f"{s}{symbol(code)}{v:.{0 if v >= 10 or unit in ('k', 'K') else 1}f}{unit}"
    return f"{s}{symbol(code)}{a:.0f}"


def detect(text: str) -> list[tuple[str, float]]:
    """Every (currency, amount) written in a text, e.g. 'Spent USD 12.99 (INR 1,085.20)' -> [('USD', 12.99),
    ('INR', 1085.2)]. Shared tokens ('$', 'Rs') are read as the most common currency that uses them."""
    out = []
    for m in AMOUNT_ANY.finditer(text or ""):
        tok = m.group(1).upper()
        code = _TOKENS.get(tok) or _TOKENS.get(m.group(1))
        try:
            out.append((code, float(m.group(2).replace(",", ""))))
        except ValueError:
            pass
    return [(c, a) for c, a in out if c]


def configured() -> str:
    """The home currency from settings.json (for code that has no settings at hand)."""
    from . import paths
    try:
        return home(json.loads(paths.SETTINGS_PATH.read_text()))
    except (OSError, ValueError):
        return DEFAULT


def prompt_bits(code: str) -> dict:
    """How the AI's instructions describe the money (unchanged wording for rupees)."""
    if code == "INR":
        return {"where": "They live in India; amounts are INR.", "sym": "₹",
                "where_inv": "They live in India;\nvalues are in rupees (INR) unless marked USD.",
                "style": "Use ₹ with Indian digit grouping (₹1,23,456)."}
    sym = symbol(code).strip()
    return {"where": f"Amounts are in their home currency, {code}.", "sym": sym,
            "where_inv": f"Values are in their home currency ({code}) unless marked otherwise.",
            "style": f"Use {sym} with the usual digit grouping ({fmt(123456, code)})."}


ORIGINAL_RE = re.compile(r"^(?:\[read by (?:Claude|AI)\])?\s*\[([A-Z]{3}) ([0-9.]+) ≈ ")   # old notes say Claude


def original(raw: str | None) -> tuple[str, float] | None:
    """(currency, amount) a converted payment was made in, from its note; None if it was in the home currency."""
    m = ORIGINAL_RE.match(raw or "")
    return (m.group(1), float(m.group(2))) if m else None
