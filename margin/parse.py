"""Turn Indian bank / card alert text (email or SMS) into transactions.

Rule-based on purpose: it runs unattended at midnight, needs no network and no AI,
and behaves the same every night. Anything it can't read confidently goes to the
review list instead of being guessed.
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


def bank_for_sender(sender: str, extra_trusted: list[str] | None = None) -> str | None:
    """Return a bank name if this email/SMS sender is a bank, else None."""
    s = (sender or "").upper()
    if "@" in s:
        s = s.split("@", 1)[1]           # judge emails by domain only
    s_compact = re.sub(r"[^A-Z0-9.]", "", s)
    for tok in extra_trusted or []:
        if tok.upper() in s_compact or tok.upper() in s:
            return tok
    if NOT_A_BANK.search(s_compact):
        return None          # a fund house, insurer or broker that shares a bank's name (SBIMF, Axis Max Life...)
    for tok, name in BANK_TOKENS:
        if tok in s_compact:
            return name
    return None


# Senders whose name contains a bank's but which aren't bank accounts: their "receipts" and "redemption
# processed" notices aren't money moving in your accounts (the bank's own alert covers that).
NOT_A_BANK = re.compile(r"MF\b|MF\.|MUTUAL|AMC|MAXLIFE|LIFE|ERGO|LOMBARD|PRU|SECURITIES|HDFCSEC|ICICISEC|KOTAKSEC|"
                        r"AXISSEC|SBISEC|INSURANCE|GENERALI|CAMS|KFIN|NSDL|CDSL", re.I)


# Bump when payee extraction improves: the next sync re-reads stored alerts that have no payee.
PARSER_VERSION = 15

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


def _party_from(window: str) -> str | None:
    for rx in PARTY_RES:
        for m in rx.finditer(window):   # the first "to ..." can be template wording; try the next one
            p = _clean_party(m.group(1))
            if p:
                return p
    return None


def parse_alert(text: str, subject: str = "") -> ParseResult:
    """Parse one bank alert. Only call this for messages from a bank sender."""
    full = normalize(f"{subject} . {text}" if subject else text)
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


def looks_financial(text: str) -> bool:
    """For non-bank senders: does this look like a money movement worth a human look?"""
    t = normalize(text)
    return bool((AMOUNT_RE.search(t) or money.AMOUNT_ANY.search(t)) and DIR_RE.search(t) and not SKIP_RE.search(t))
