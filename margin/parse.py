"""Turn bank / card alert text (email or SMS) into transactions: Indian banks' alerts in rupees, US banks' in dollars.

Rule-based on purpose: it runs unattended at midnight, needs no network and no AI,
and behaves the same every night. Anything it can't read confidently goes to the
review list instead of being guessed.

Each alert is read in one currency, its bank's (currency_for): Indian banks in rupees with the Indian patterns,
US banks in dollars with the US ones. The two sets are separate, so teaching one never changes how the other reads.
"""
from __future__ import annotations

import re

from . import money
from dataclasses import dataclass, field

# --- who counts as a bank ---------------------------------------------------------
# Tokens matched against an email's domain or an SMS sender ID (e.g. "JD-HDFCBK-S").
BANK_TOKENS = [
    ("HDFC", "HDFC"), ("ICICI", "ICICI"), ("SBICARD", "SBI Card"), ("SBI", "SBI"),
    ("AXIS", "Axis"), ("KOTAK", "Kotak"), ("YESB", "Yes Bank"), ("YESBANK", "Yes Bank"),
    ("INDUSIND", "IndusInd"), ("INDUSB", "IndusInd"), ("IDFC", "IDFC First"), ("AUBANK", "AU"), ("AUSFB", "AU"),
    ("RBL", "RBL"), ("FEDBNK", "Federal"), ("FEDERAL", "Federal"), ("FEDFIB", "Federal"),
    ("BOBTXN", "Bank of Baroda"), ("BANKOFBARODA", "Bank of Baroda"), ("BARODA", "Bank of Baroda"),
    ("PNB", "PNB"), ("CANBNK", "Canara"), ("CANARA", "Canara"), ("UNIONB", "Union Bank"),
    ("HSBC", "HSBC"), ("SCBANK", "Standard Chartered"), ("STANCHART", "Standard Chartered"),
    ("CITI", "Citi"), ("AMEX", "Amex"), ("AMERICANEXPRESS", "Amex"), ("AEXPIN", "Amex"),
    ("ONECRD", "OneCard"), ("ONECARD", "OneCard"), ("GETONECARD", "OneCard"),
    ("SLICE", "slice"), ("JUPITER", "Jupiter"), ("FIMONY", "Fi"), ("FI.MONEY", "Fi"),
    ("DBS", "DBS"), ("EQUITAS", "Equitas"), ("UJJIVAN", "Ujjivan"), ("BANDHAN", "Bandhan"),
    ("IOB", "IOB"), ("INDBNK", "Indian Bank"), ("INDIANBANK", "Indian Bank"),
    ("CENTRALBANK", "Central Bank"), ("CBOI", "Central Bank"), ("PAYTMB", "Paytm Bank"),
    ("AIRTELPAYMENTSBANK", "Airtel Payments Bank"), ("AIRBNK", "Airtel Payments Bank"),
]


def sender_key(sender: str) -> str:
    """A stable short name for a sender: the email domain, or the SMS header without its prefix/suffix."""
    s = (sender or "").strip()
    m = re.search(r"@([\w.-]+)", s)
    if m:
        return m.group(1).lower()
    s = re.sub(r"^[A-Z]{2}-", "", s.upper())
    return re.sub(r"-[SPTG]$", "", s)


# US banks, by the email domain their alerts come from (that domain or a subdomain of it, e.g.
# alerts.chase.com). Checked by whole domain, so short names can't match inside other words.
US_DOMAINS = [
    ("chase.com", "Chase"), ("bankofamerica.com", "Bank of America"), ("bofa.com", "Bank of America"),
    ("wellsfargo.com", "Wells Fargo"), ("capitalone.com", "Capital One"), ("discover.com", "Discover"),
    ("usbank.com", "U.S. Bank"), ("pnc.com", "PNC"), ("ally.com", "Ally"), ("schwab.com", "Schwab"),
    ("fidelity.com", "Fidelity"), ("sofi.com", "SoFi"), ("synchrony.com", "Synchrony"), ("syf.com", "Synchrony"),
    ("navyfederal.org", "Navy Federal"), ("usaa.com", "USAA"), ("barclaysus.com", "Barclays US"),
    ("tdbank.com", "TD Bank"), ("truist.com", "Truist"), ("citizensbank.com", "Citizens"), ("53.com", "Fifth Third"),
    ("key.com", "KeyBank"), ("regions.com", "Regions"), ("huntington.com", "Huntington"), ("chime.com", "Chime"),
    ("marcus.com", "Marcus"), ("citi.com", "Citi"), ("citibank.com", "Citi"), ("americanexpress.com", "Amex"),
]
# Brokers: their mail is about money in a brokerage account (and, for Indian RSU or ESPP holders, a broker
# abroad), so they count as banks only when the home currency isn't the rupee.
US_BROKER_DOMAINS = {"schwab.com", "fidelity.com"}
# US banks text from short codes. Only codes that are the bank's own; any other short code counts when its
# text starts with a US bank's name ("Chase: ...", "BofA: ...") or once you trust it (margin trust 12345).
US_SHORT_CODES = {"24273": "Chase", "73981": "Bank of America", "93557": "Wells Fargo"}
_US_NAMES = {"chase": "Chase", "bofa": "Bank of America", "bank of america": "Bank of America",
             "wells fargo": "Wells Fargo", "capital one": "Capital One", "discover": "Discover", "citi": "Citi",
             "citibank": "Citi", "u.s. bank": "U.S. Bank", "us bank": "U.S. Bank", "pnc": "PNC", "ally": "Ally",
             "ally bank": "Ally", "amex": "Amex", "american express": "Amex", "navy federal": "Navy Federal",
             "usaa": "USAA", "sofi": "SoFi", "synchrony": "Synchrony", "schwab": "Schwab", "fidelity": "Fidelity",
             "td bank": "TD Bank", "truist": "Truist", "citizens": "Citizens", "fifth third": "Fifth Third",
             "keybank": "KeyBank", "regions": "Regions", "huntington": "Huntington", "chime": "Chime"}
_US_NAME_AT_START = re.compile(
    r"^\W*(?:\[[^\]]*\]\s*)?(" + "|".join(re.escape(n) for n in sorted(_US_NAMES, key=len, reverse=True)) +
    r")(?:\s+alert)?\s*[:\-]", re.I)
_SHORT_CODE = re.compile(r"\d{5,6}")
# Banks that are only in the US (their alerts are in dollars); banks in both countries (Citi, Amex, HSBC...)
# and senders you trusted yourself are read in your home currency's way (currency_for).
US_BANKS = {n for _, n in US_DOMAINS} | set(US_SHORT_CODES.values()) | set(_US_NAMES.values())
US_BANKS -= {"Citi", "Amex"}


def _us_domain(domain: str, home: str) -> str | None:
    for d, name in US_DOMAINS:
        if (domain == d or domain.endswith("." + d)) and (home != "INR" or d not in US_BROKER_DOMAINS):
            return name
    return None


def bank_for_sender(sender: str, extra_trusted: list[str] | None = None, text: str = "",
                    home: str = "INR") -> str | None:
    """Return a bank name if this email/SMS sender is a bank, else None. `text` (the message) is only looked at
    for SMS short codes: a US bank's text starts with its name. `home` is the home currency: with rupees at home,
    US brokers aren't banks and a short code counts only once trusted."""
    home = (home or "INR").upper()
    s = (sender or "").upper()
    domain = ""
    if "@" in s:
        s = s.split("@", 1)[1]           # judge emails by domain only
        domain = re.sub(r"[^a-z0-9.\-].*$", "", s.lower())
    s_compact = re.sub(r"[^A-Z0-9.]", "", s)
    for tok in extra_trusted or []:
        if tok.upper() in s_compact or tok.upper() in s:
            return tok
    if domain and _us_domain(domain, home):
        return _us_domain(domain, home)
    if NOT_A_BANK.search(s_compact):
        return None          # a fund house, insurer or broker that shares a bank's name (SBIMF, Axis Max Life...)
    for tok, name in BANK_TOKENS:
        if tok in s_compact:
            return name
    code = (sender or "").strip()
    if _SHORT_CODE.fullmatch(code) and home != "INR":
        if code in US_SHORT_CODES:
            return US_SHORT_CODES[code]
        m = _US_NAME_AT_START.match(text or "")
        if m:
            return _US_NAMES[m.group(1).lower()]
    return None


def currency_for(bank: str | None, home: str) -> str:
    """The currency a bank's alerts are read in: US banks dollars, Indian banks rupees. A bank in both countries
    (Citi, Amex, HSBC...) or a sender you trusted yourself: dollars if you live in the US, else rupees."""
    if bank in US_BANKS:
        return "USD"
    if bank in INDIAN_BANKS:
        return "INR"
    return "USD" if (home or "").upper() == "USD" else "INR"


# Senders whose name contains a bank's but which aren't bank accounts: their "receipts" and "redemption
# processed" notices aren't money moving in your accounts (the bank's own alert covers that).
NOT_A_BANK = re.compile(r"MF\b|MF\.|MUTUAL|AMC|MAXLIFE|LIFE|ERGO|LOMBARD|PRU|SECURITIES|HDFCSEC|ICICISEC|KOTAKSEC|"
                        r"AXISSEC|SBISEC|INSURANCE|GENERALI|CAMS|KFIN|NSDL|CDSL", re.I)
INDIAN_BANKS = {name for _, name in BANK_TOKENS} - {"Citi", "Amex", "HSBC", "DBS", "Standard Chartered"}


# Bump when payee extraction improves: the next sync re-reads stored alerts that have no payee, and US banks'
# messages still on the review list (sync.reparse_parties, retry_review). 16: the US reader. A re-read changes
# nothing for rupee alerts (checked on a real database: same payments, payees, categories, balances and dues).
PARSER_VERSION = 16

# --- the alert itself -------------------------------------------------------------
AMOUNT = r"(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
AMOUNT_RE = re.compile(AMOUNT, re.I)
BAL_RE = re.compile(
    r"(?:avl\.?|avail(?:able)?|avbl\.?|available)\s*(?:bal(?:ance)?|limit|lmt)?\.?\s*(?:is|:|-)?\s*(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.I)
CLEAR_BAL_RE = re.compile(
    r"(?:avl\.?|avail(?:able)?|avbl\.?)\s*bal(?:ance)?\.?\s*(?:is|:|-)?\s*(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)

OUT_WORDS = (r"debited|debit(?!\s*card)|spent|withdrawn|paid|sent|deducted|purchase|txn of|transaction of|"
             r"used (?:at|for)|for using|has been used|charged")
IN_WORDS = r"credited|received|deposited|refund(?:ed)?|reversed|reversal|cashback|added to"
DIR_RE = re.compile(rf"\b(?P<out>{OUT_WORDS})\b|\b(?P<inn>{IN_WORDS})\b", re.I)

SKIP_RE = re.compile(
    r"\bOTP\b|one[- ]time password|verification code|"
    r"\b(?:failed|declined|unsuccessful|not been processed)\b|"
    r"will be (?:debited|deducted|auto[- ]?debited)|to be (?:debited|deducted|auto[- ]?debited)|is due|due (?:on|by|date)|minimum amount due|"
    r"has an overdue|pay the min(?:imum)? due|"
    r"pre-?approved|offer|congratulations|eligible for|apply now|"
    r"has requested (?:money|rs|inr|₹)|collect request|requested you|"
    r"mandate (?:created|registered|setup)|e-?mandate|autopay (?:set|registered)|"
    r"standing instruction|"
    r"convert (?:your|this|the) (?:transaction|purchase|spends?|outstanding)|into (?:easy )?(?:monthly )?(?:emis?|instal+ments)|"
    r"convert (?:it )?now|flexi ?emi|easy emi|emi offer|"
    r"credited to the beneficiary account|redemption proceeds will be|premium payment receipt",
    re.I)

ACCT_RE = re.compile(
    r"(?P<kind>a/?c|acct|account|card|credit card|debit card)\b"
    r"(?:(?!\b(?:rs|inr)\b|₹|\bdear\b)[^0-9x*]){0,25}?(?:no\.?\s*)?"
    r"(?:ending(?:\s*with)?|ending in)?\s*[:\-]?\s*(?:[x*•.]+|xx)?\s*(?P<last4>[0-9]{3,6})\b",
    re.I)

# "to VPA x@y NAME", "towards VPA x@y (NAME)", and the bare/masked "to ****x@y" of card-on-UPI alerts.
VPA_RE = re.compile(
    r"\b(?:to|towards|from|by)\s+(?:VPA\s+)?\**([\w.\-]*\w@[\w.\-]*[A-Za-z])"
    r"(?:\s*\(([^)]{1,40})\)|\s+([A-Za-z][A-Za-z0-9 .&'\-]{1,40}?)"
    r"(?=\s+\d|\s+on\b|\s+ref|\s*\.\s|\s*\(|,|$))?", re.I)
DATE = r"\d{1,2}[/-](?:\d{1,2}|[A-Za-z]{3})[/-]\d{2,4}"
TO_ACCT_RE = re.compile(r"\bTo\s+A/?c(?:count)?\s*(?:no\.?\s*)?[xX*•]*(\d{3,})\b"
                        r"|\band a/?c\s*(?:no\.?\s*)?[xX*•]*(\d{3,}) credited\b", re.I)   # SBI IMPS
PARTY_RES = [
    # HDFC deposits: "INR 1,23,450.00 deposited in HDFC Bank A/c XX1111 on 26-AUG-22 for ACME PVT LTD.Avl bal ..."
    re.compile(r"\bdeposited in .{0,40}?\bon \S+ for\s+(.{2,150}?)(?=\.\s*Avl\b|\.\s|$)", re.I),
    # HDFC refund emails: "...from the merchant Dear Customer, ... From Merchant: AMAZON Date Time: ..."
    re.compile(r"\bFrom Merchant\s*:\s*(.{2,40}?)(?=\s+Date\b|\s+on\b|\s+Please\b|\.\s|,|$)", re.I),
    re.compile(r"\btransfer to payee\s+([A-Za-z][A-Za-z .]{1,40}?)(?=\s+using\b|\s+on\b|\.|,|$)", re.I),
    re.compile(r"\bPaid To:\s*([A-Za-z0-9][A-Za-z0-9 .&*'\-_]{1,40}?)(?=\s+Important\b|\s+Ref|\.|,|$)", re.I),
    re.compile(r"\b(?:by|via|through)\s+(NACH|ECS|ACH|NACH-DR)\b", re.I),
    re.compile(r"UPI/(?:P2[AM]|CR|DR)/\d+/([^/\n]+)", re.I),
    re.compile(r"\bMerchant Name\s*:\s*(.{2,40}?)(?=\s+(?:[A-Za-z]+ Bank|Credit Card|Card No|Date|Transaction|Axis)\b|$)",
               re.I),
    re.compile(r"\bIST\s+([A-Za-z0-9][A-Za-z0-9 .&*'\-_/]{1,40}?)\s+Avl\.?\s*(?:Limit|Bal)", re.I),
    re.compile(r"\bInfo:?\s*([A-Za-z0-9 .&*\-/@]{3,40})", re.I),
    re.compile(r"\bat\s+([A-Za-z][A-Za-z0-9 .&*'\-_/]{1,40}?)(?=\s+on\b|\s+dated\b|\s+(?:was|is|has)\b|\.\s|\s*\(|,|$)", re.I),
    re.compile(r"\b(?:to|towards|for)\s+([A-Za-z0-9][A-Za-z0-9 .&'\-_@]{1,40}?)"
               r"(?=\s+on\b|\s+ref\b|\s+via\b|\.\s|,|\s+UPI|\s+" + DATE + r"|$)", re.I),
    re.compile(r"\b(?:from|by)\s+([A-Za-z0-9][A-Za-z0-9 .&'\-_@]{1,40}?)"
               r"(?=\s+on\b|\s+ref\b|\s+via\b|\.\s|,|\s+UPI|\s+" + DATE + r"|$)", re.I),
]
PARTY_JUNK = re.compile(
    r"^(?:your|you|a/?c|acct|account|card|the|bank|rs|inr|upi|imps|neft|rtgs|vpa|xx+\d*|\d+|"
    r"your a/?c.*|your account.*|your card.*|sms block.*|block.*|call.*|dispute.*|clearing)$",
    re.I)

STATEMENT_DUE_RE = re.compile(
    r"total\s+(?:amount\s+)?(?:due|outstanding)\s*(?:is|:|-|of)?\s*(?:rs\.?|inr|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.I)
DUE_DATE_RE = re.compile(r"(?:payment\s+)?due\s+date\s*(?:is|:|-)?\s*([0-9]{1,2}[\s\-/][A-Za-z0-9]{2,9}[\s\-/,]*[0-9]{2,4})",
                         re.I)

# --- US alerts, in dollars ----------------------------------------------------------
# The shapes US banks use (every example in the tests is made up): "Chase: You made a $45.00 transaction with
# DEMO SHOP", "A $45.00 transaction was made at DEMO SHOP on card ending in 1234", "Merchant: DEMO SHOP Amount:
# $45.00", "Your available balance is $1,234.56", "Statement balance: $1,234.56 ... Payment due date: 12/05/2026".
USD = r"(?:(?<![A-Za-z])US\s?\$|(?<![A-Za-z])USD\s?\$?|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
US_AMOUNT_RE = re.compile(USD, re.I)
# "available balance ... is $X", "balance of $X", "Avail Bal: $X". Not "balance is below $100" (a threshold).
US_BAL_RE = re.compile(
    r"\b(?P<which>avail(?:able)?\.?|avl\.?|current|ledger|ending|account|acct\.?|statement|new|end[- ]of[- ]day)?\s*"
    r"bal(?:ance)?\b(?:(?!\$|\bbal(?:ance)?\b|\b(?:below|above|under|over|less than|more than|exceeds?|threshold|"
    r"reached|limit)\b).){0,60}?" + USD, re.I)
US_IN = (r"payment (?:of \$?[0-9,]+(?:\.[0-9]{2})? )?(?:has been |was |is )?(?:received|posted|applied|credited)|"
         r"received your payment|thank you for (?:your|the) payment|direct deposit|deposit(?:ed)?|credited|received|"
         r"sent you|paid you|refund(?:ed)?|reversal|reversed|returned|credit of|cash ?back|incoming")
US_OUT = (r"withdrawal|withdrawn|debited|debit(?!\s*card)|purchases?|purchased|charged?|spent|paid|sent|"
          r"payment (?:to|of|from)|transactions?|transfer(?:red)? (?:to|of)|was used|used (?:at|for)|bill ?pay")
US_DIR_RE = re.compile(rf"\b(?P<inn>{US_IN})\b|\b(?P<out>{US_OUT})\b", re.I)
US_SKIP_RE = re.compile(
    r"\b(?:one[- ]time (?:pass)?code|verification code|security code|passcode|OTP)\b|"
    r"\b(?:declined|not approved|wasn't approved|unsuccessful|could not be (?:processed|completed)|failed)\b|"
    r"\bdid you (?:make|attempt|authorize)\b|\bsuspicious\b|\breply (?:yes|y|no|n)\b|"
    r"\brequested \$|\bsent you a (?:payment )?request\b|\brequest(?:ed)? (?:money|a payment)\b|"
    r"\b(?:pre-?approved|apply now|congratulations|you(?:'re| are) eligible)\b|"
    r"\b(?:fell|dropped|is) below\b|\bbalance is (?:above|over|under)\b", re.I)
# Reminders and schedules: nothing moved yet. A completed payment that also mentions the next due date counts.
US_LATER_RE = re.compile(
    r"\b(?:will be (?:debited|withdrawn|charged|deducted|paid|sent|processed)|is scheduled|scheduled (?:payment|"
    r"autopay|transfer)|upcoming (?:payment|autopay|transfer|bill)|reminder|is due|due (?:on|by|date|soon)|"
    r"minimum payment)\b", re.I)
US_DONE_RE = re.compile(r"\b(?:received your payment|payment (?:of \$?[0-9,.]+ )?(?:has been |was |is )?"
                        r"(?:received|posted|applied)|thank you for (?:your|the) payment)\b", re.I)
US_ACCT_RE = re.compile(
    r"(?P<kind>credit card|debit card|card|account|acct|checking|savings)\b"
    r"(?:(?!\$|\b(?:card|account|acct|checking|savings)\b).){0,40}?"     # the kind word nearest the ending
    r"(?:\bending(?:\s+in|\s+with)?|\.{2,}|(?<![A-Za-z])[xX*]+|#|\bno\.?)\s*[:\-]?\s*(?P<last4>[0-9]{4,5})\b"
    r"|\((?:\.{2,}|[xX*]+)(?P<last4b>[0-9]{4,5})\)|(?<![\w.])\.{3}(?P<last4c>[0-9]{4})\b", re.I)
US_STATEMENT_DUE_RE = re.compile(
    r"(?:(?:statement|new|total|current|closing)\s+balance|total\s+(?:amount\s+)?due)(?:\s+(?:is|of|was|due))*"
    r"\s*[:\-]?\s*" + USD, re.I)
US_DUE_DATE_RE = re.compile(
    r"\bdue(?:\s+date)?\s*(?:is|:|-|on|by)?\s*(?:on\s+|by\s+)?"
    r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4})", re.I)
# Thresholds you set ("exceeded your $0.00 set alert", "over $500.00") are not the payment.
US_NOT_AMOUNT_AFTER = re.compile(r"^\s*(?:set alert|alert|or more|or higher|or greater|threshold|limit)\b", re.I)
US_NOT_AMOUNT_BEFORE = re.compile(r"(?:exceed(?:ed|s)? (?:your|the)?|above|over|more than|greater than|below|under|"
                                  r"less than|limit(?: of)?|threshold(?: of)?)\s*$", re.I)
US_PARTY_RES = [
    re.compile(r"\bMerchant(?:\s+(?:name|details?))?\s*:?\s+(?!name\b|details?\b)([A-Za-z0-9][A-Za-z0-9 .&*'#\-/]{1,40}?)"
               r"(?=\s+(?:Amount|Date|Card|Account|Time|Transaction|View|on|in|for|was|has)\b|\s*\.(?:\s|$)|,|;|$)", re.I),
    re.compile(r"\bWhere\s*:\s*([A-Za-z0-9][A-Za-z0-9 .&*'#\-/]{1,40}?)"
               r"(?=\s+(?:Amount|Date|Card|Account|Time|View|Transaction)\b|\s*\.(?:\s|$)|,|;|$)", re.I),
    re.compile(r"\b(?:transaction|purchase|charge|payment) (?:with|at|to)\s+([A-Za-z0-9][A-Za-z0-9 .&*'#\-/]{1,40}?)"
               r"(?=\s+(?:on|at \d|exceeded|was|has|is|using|from|for)\b|\s*\.(?:\s|$)|,|;|$)", re.I),
    re.compile(r"\b(?:at|with)\s+([A-Za-z][A-Za-z0-9 .&*'#\-/]{1,40}?)"
               r"(?=\s+(?:on|was|has|is|using|for|exceeded|with your|from your|in the amount)\b|\s*\.(?:\s|$)|,|;|$)",
               re.I),
    re.compile(r"\b(?:to|from)\s+([A-Za-z][A-Za-z0-9 .&'\-]{1,40}?)"
               r"(?=\s+(?:on|with Zelle|via|using|was|has|is|for|posted)\b|\s*\.(?:\s|$)|,|;|$)", re.I),
]
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}


def _us_date(s: str) -> str:
    """12/05/2026, 12/05/26, Dec 5, 2026 or 2026-12-05 -> 2026-12-05 (as written if it can't be read)."""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        mo, d = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)
        if not m or m.group(1).lower() not in _MONTHS:
            return s
        y, mo, d = int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return s
    return f"{y:04d}-{mo:02d}-{d:02d}"


@dataclass
class Parsed:
    amount: float
    direction: str                 # 'in' | 'out'
    last4: str | None = None
    kind: str | None = None        # 'card' | 'account'
    counterparty: str | None = None
    vpa: str | None = None
    balance: float | None = None


@dataclass
class ParseResult:
    txns: list[Parsed] = field(default_factory=list)
    balance: float | None = None
    last4: str | None = None
    statement_due: float | None = None
    due_date: str | None = None
    reason: str | None = None      # why nothing was counted (for the review list)
    currency: str = "INR"          # what the amounts are in (the bank's currency, currency_for)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def normalize(text: str) -> str:
    text = text.replace(" ", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _clean_party(p: str | None) -> str | None:
    if not p:
        return None
    p = re.sub(r"\s+", " ", p).strip(" .:-/*,")
    neft = re.match(r"(?:NEFT|RTGS|IMPS)?\s*Cr-[A-Z]{4}0[A-Z0-9]{6}-([^-]+)", p)   # "NEFT Cr-DEMO0000001-ACME LTD-YOU-REF"
    if neft:
        p = neft.group(1).strip()
    p = re.sub(r"^(?:VPA|UPI|IMPS|NEFT|RTGS)(?:\s*(?:from|to|-|/))?\s+", "", p, flags=re.I)
    p = re.sub(r"\s+(?:on|ref|via|dated|at)$", "", p, flags=re.I)
    p = re.sub(r"\s+(?:USD|EUR|GBP|AED|SGD|AUD|CAD|JPY|INR|Rs\.?)\s*[\d.,]+$", "", p, flags=re.I)
    if len(p) < 2 or is_junk_party(p):
        return None
    return p[:48]


# Template wording that the "to/from <name>" patterns can pick up instead of a payee.
_JUNK_ANY = re.compile(
    r"\b(?:you that|has been|have been|is debited|is credited|was debited|was credited|account ending|"
    r"card ending|ending with|ending in|from the merchant|your (?:\w+ )?(?:bank|a/?c|account|card|credit card)|"
    r"confirmation from|auto[- ]?debit|available (?:balance|limit|credit)|know your|check your|"
    r"outstanding amount|recent transactions|registered mobile|net ?banking|online banking|mobile banking|"
    r"customer care|call us|sms block|insufficient funds|return charges?|the merchant|dear \w+|"
    r"greetings|was successful|is successful|has been initiated|avl\.? bal)\b|^your beneficiary\b|^report\b|^transfer of\b|^of (?:rs|inr)\b|^using\b|^(?:our|we|this|more|inform|informing|please|kindly)\b|"
    r"^\d+\s*-\s*\w+ bank$|^(?:rs\.?|inr|₹)\s*[\d,.]+$", re.I)


def is_junk_party(p: str | None) -> bool:
    return bool(p) and bool(PARTY_JUNK.match(p) or _JUNK_ANY.search(p))


def _counterparty(text: str, start: int) -> tuple[str | None, str | None]:
    """Returns (display name, UPI VPA)."""
    window = text[max(0, start - 40): start + 260]
    for v in VPA_RE.finditer(window):
        handle = v.group(1)
        # A bare "to x@y.com" is an email address (e.g. "write to support@bank.com"), not a UPI handle.
        if "." in handle.split("@", 1)[1] and not re.search(r"\bVPA\b", v.group(0), re.I):
            continue
        return _clean_party(v.group(2) or v.group(3)) or handle, handle
    # HDFC deposits by UPI: "deposited in ... for UPI-ARJUN MEHTA-arjun@ybl-DEMO0000002-123456-Payment from Phone"
    m = DEPOSIT_UPI_RE.search(window)
    if m:
        return _clean_party(m.group(1)) or m.group(2), m.group(2)
    # HDFC's 2023 texts dropped the @ ("to VPA swiggy.storesaxisbank(UPI Ref ...", "by a/c linked to VPA
    # someoneybl (UPI Ref ..."): put it back.
    m = OLD_VPA_RE.search(window)
    if m:
        handle = _restore_at(m.group(1))
        return handle, handle
    # Transfers to a bank account with no name ("To A/c xxxxxx9876"): use the account as the payee,
    # so a rule can sort all transfers to it.
    m = TO_ACCT_RE.search(window)
    named = _party_from(window)
    if m and (not named or re.match(r"(?i)a/?c\b", named)):
        return f"A/c ••{(m.group(1) or m.group(2))[-4:]}", None
    return named, None


DEPOSIT_UPI_RE = re.compile(r"\bfor\s+UPI-([^-]{2,40})-([\w.\-]+@[\w.]+)-", re.I)
OLD_VPA_RE = re.compile(r"\b(?:to|linked to) VPA\s+([\w.\-]{3,60}?)\s*\(", re.I)
# UPI app handles (the part after the @), longest first so "oksbi" wins over "sbi".
PSP_HANDLES = sorted("""ybl ibl axl paytm ptys ptyes ptaxis pthdfc ptsbi icici okicici hdfcbank okhdfcbank axisbank axisb
    okaxis sbi oksbi yesbankltd yespay yesbank upi apl kotak kmbl idfcfirst idfcbank freecharge jupiteraxis fbl federal
    waaxis waicici wahdfcbank wasbi ikwik airtel jio rbl indus aubank dbs equitas barodampay pnb unionbank""".split(),
                     key=len, reverse=True)


def _restore_at(handle: str) -> str:
    low = handle.lower()
    for psp in PSP_HANDLES:
        if low.endswith(psp) and len(handle) > len(psp):
            return f"{handle[:-len(psp)]}@{handle[-len(psp):]}"
    return handle


def _party_from(window: str, patterns: list | None = None) -> str | None:
    for rx in patterns or PARTY_RES:
        for m in rx.finditer(window):   # the first "to ..." can be template wording; try the next one
            p = _clean_party(m.group(1))
            if p:
                return p
    return None


def parse_alert(text: str, subject: str = "", currency: str = "INR") -> ParseResult:
    """Parse one bank alert, written in `currency` (currency_for its bank: "INR" or "USD"). Only call this for
    messages from a bank sender."""
    full = normalize(f"{subject} . {text}" if subject else text)
    if currency == "USD":
        return _parse_us(full)
    res = ParseResult()

    bal = CLEAR_BAL_RE.search(full) or BAL_RE.search(full)
    if bal and not re.search(r"limit|lmt", bal.group(0), re.I):   # a card's spare limit isn't a balance
        res.balance = _num(bal.group(1))
    acct = ACCT_RE.search(full)
    if acct:
        res.last4 = acct.group("last4")[-4:]

    # Credit-card statements: record what's owed, they are not transactions.
    if re.search(r"\bstatement\b", full, re.I):
        due = STATEMENT_DUE_RE.search(full)
        if due:
            res.statement_due = _num(due.group(1))
            dd = DUE_DATE_RE.search(full)
            res.due_date = dd.group(1).strip() if dd else None
            res.reason = "statement"
            return res

    if SKIP_RE.search(full):
        res.reason = "not a completed transaction (OTP, failed, due, offer or request)"
        return res

    dm = DIR_RE.search(full)
    if not dm:
        res.reason = "no debit/credit wording"
        return res
    direction = "out" if dm.group("out") else "in"

    # Amount closest to the debit/credit verb, ignoring balance figures.
    bal_spans = [m.span() for m in BAL_RE.finditer(full)]
    best = None
    for m in AMOUNT_RE.finditer(full):
        if any(a <= m.start() < b for a, b in bal_spans):
            continue
        if re.search(r"(?:bal(?:ance)?|limit|lmt)\W{0,6}$", full[max(0, m.start() - 14):m.start()], re.I):
            continue
        dist = abs(m.start() - dm.start())
        if best is None or dist < best[0]:
            best = (dist, m)
    if best is None or best[0] > 220:
        res.reason = "no amount near the debit/credit wording"
        return res
    amount = _num(best[1].group(1))
    if amount <= 0:
        res.reason = "zero amount"
        return res

    kind = None
    if acct:
        kind = "card" if "card" in acct.group("kind").lower() else "account"
    if re.search(r"credit card", full, re.I) and kind != "account":
        kind = "card"

    party, vpa = _counterparty(full, dm.start())
    if res.last4 and party == f"A/c ••{res.last4}":   # "credited to a/c xx1111" names your own account
        party = None
    res.txns.append(Parsed(
        amount=amount, direction=direction, last4=res.last4, kind=kind,
        counterparty=party, vpa=vpa, balance=res.balance,
    ))
    return res


US_WEAK_OUT_RE = re.compile(r"\btransactions?\b", re.I)    # "You made a $45.00 transaction": out, unless a clearer word
_ZELLE_FROM_RE = re.compile(r"(?:^|[.:]\s*)([A-Z][A-Za-z .'\-]{1,40}?) (?:sent you|paid you)\b")


def _us_amount_ok(full: str, m: re.Match, skip_spans: list[tuple[int, int]]) -> bool:
    """Is this dollar figure a payment, not a balance, a credit limit or an alert threshold you set?"""
    if any(a <= m.start() < b for a, b in skip_spans):
        return False
    before, after = full[max(0, m.start() - 30):m.start()], full[m.end():m.end() + 20]
    if re.search(r"(?:bal(?:ance)?|limit|lmt|credit)\W{0,6}$", before, re.I):
        return False
    return not (US_NOT_AMOUNT_BEFORE.search(before) or US_NOT_AMOUNT_AFTER.search(after))


def _us_kind(full: str, acct: re.Match | None) -> str:
    """'card' or 'account': from the word just before the ending ("card ending in", "account ending in",
    "Demo Rewards Card (...9876)"), else from the message."""
    word = (acct.group("kind") or "").lower() if acct else ""
    if word:
        return "card" if "card" in word else "account"
    card = re.search(r"\bcard\b", full, re.I) and not re.search(r"\b(?:checking|savings)\b", full, re.I)
    return "card" if card else "account"


def _parse_us(full: str) -> ParseResult:
    """A US bank's alert, in dollars. Same rules as the Indian reader: only completed money movements count;
    balances, card statements and card balances are recorded; anything unclear goes to the review list."""
    res = ParseResult(currency="USD")
    bal_spans = []
    for b in US_BAL_RE.finditer(full):
        bal_spans.append(b.span())
        which = (b.group("which") or "").lower()
        if res.balance is None and which not in ("statement", "new"):
            res.balance = _num(b.group(b.re.groups))     # the amount is the last group
    acct = US_ACCT_RE.search(full)
    if acct:
        res.last4 = (acct.group("last4") or acct.group("last4b") or acct.group("last4c"))[-4:]
    kind = _us_kind(full, acct)

    # Card statements: what's owed and when, not a transaction.
    if re.search(r"\bstatement\b", full, re.I) and re.search(r"\b(?:card|minimum payment|credit)\b", full, re.I):
        due = US_STATEMENT_DUE_RE.search(full)
        if due:
            res.statement_due, res.balance = _num(due.group(1)), None
            dd = US_DUE_DATE_RE.search(full)
            res.due_date = _us_date(dd.group(1)) if dd else None
            res.reason = "statement"
            return res

    if US_SKIP_RE.search(full) or (US_LATER_RE.search(full) and not US_DONE_RE.search(full)):
        res.reason = "not a completed transaction (code, declined, reminder, request or offer)"
        return res

    skip = bal_spans + [d.span() for d in US_STATEMENT_DUE_RE.finditer(full)]
    amounts = [m for m in US_AMOUNT_RE.finditer(full) if _us_amount_ok(full, m, skip)]
    words = [(m.start(), "in" if m.group("inn") else "out", 0) for m in US_DIR_RE.finditer(full)]
    words += [(m.start(), "out", 30) for m in US_WEAK_OUT_RE.finditer(full)]   # "transaction" alone: a weak hint
    if not words:
        if kind == "card" and res.balance is not None:     # "your card balance is $523.45": what you owe on it
            res.statement_due, res.balance, res.reason = res.balance, None, "card balance"
        else:
            res.reason = "no debit/credit wording"
        return res
    if not amounts:
        res.reason = "no amount near the debit/credit wording"
        return res
    # The money word nearest a payment figure decides the direction ("received" in a footer doesn't).
    pos, direction, _ = min(words, key=lambda w: (min(abs(a.start() - w[0]) for a in amounts) + w[2], w[0]))
    m = min(amounts, key=lambda a: abs(a.start() - pos))
    if abs(m.start() - pos) > 220:
        res.reason = "no amount near the debit/credit wording"
        return res
    amount = _num(m.group(1))
    if amount <= 0:
        res.reason = "zero amount"
        return res

    party = None
    z = _ZELLE_FROM_RE.search(full)
    if direction == "in" and z:
        party = _clean_party(z.group(1))
    window = full[max(0, pos - 120): pos + 260]
    for rx, where in zip(US_PARTY_RES, (full, full, full, window, window)):
        if party:
            break
        for pm in rx.finditer(where):
            party = _clean_party(pm.group(1))
            if party:
                break
    res.txns.append(Parsed(amount=amount, direction=direction, last4=res.last4, kind=kind, counterparty=party,
                           balance=res.balance))
    return res


def is_not_payment(text: str, currency: str = "INR") -> bool:
    """Wording that says nothing moved yet (OTPs, reminders, declined, offers), in that currency's reader."""
    if currency == "USD":
        return bool(US_SKIP_RE.search(text) or (US_LATER_RE.search(text) and not US_DONE_RE.search(text)))
    return bool(SKIP_RE.search(text))


def looks_financial(text: str, home: str = "INR") -> bool:
    """For non-bank senders: does this look like a money movement worth a human look? With dollars at home, US
    wording ("You made a $45.00 transaction with ...") counts too."""
    t = normalize(text)
    if bool((AMOUNT_RE.search(t) or money.AMOUNT_ANY.search(t)) and DIR_RE.search(t) and not SKIP_RE.search(t)):
        return True
    return home == "USD" and bool(US_AMOUNT_RE.search(t) and (US_DIR_RE.search(t) or US_WEAK_OUT_RE.search(t))
                                  and not is_not_payment(t, "USD"))
