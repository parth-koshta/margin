"""Category rules. Defaults ship with the code; your own rules live in the private
data folder (rules.json) and are checked first, so you can override anything."""
from __future__ import annotations

import json
import re
from functools import lru_cache

from . import paths

# Categories that move money between your own pockets: excluded from income and spending.
NON_FLOW = {"Transfer", "Investments"}

PAYEE_ONLY = None   # 4th field: True = also search the message, False = payee (message only if there's no payee),
                    # PAYEE_ONLY = payee name only, never the message
# (keywords, category, direction or None, also_match_full_text)
DEFAULT_RULES: list[tuple[list[str], str, str | None, bool]] = [
    # Shopping on CRED (its store, and CRED Flash checkout) is spending; only card-bill payments through CRED are
    # own money moving. Refunds from the store come back as Shopping and reduce it.
    (["cred store", "credstor*", "cred flash"], "Shopping", None, False),
    # Own-money movements first, so they never count as spending or income.
    # Bank and card-issuer names: a payment TO them is a card bill. Payee only (PAYEE_ONLY): every bank's own
    # alert mentions its name ("... Axis Bank Ltd"), so searching the message would turn ordinary card spends
    # into transfers.
    (["cred club", "cred", "billdesk cc", "axis bank limited", "axis bank ltd", "hdfc bank limited", "hdfc bank ltd",
      "icici bank limited", "icici bank ltd", "sbi card", "sbi cards", "sbicard", "kotak mahindra bank",
      "idfc first bank", "indusind bank", "rbl bank", "au small finance bank", "yes bank limited",
      "american express", "onecard", "bobcard"], "Transfer", None, PAYEE_ONLY),
    # US card issuers as the payee of a payment from checking: a card bill. Payee only, for the same reason.
    (["chase credit crd", "chase card services", "chase card", "capital one", "discover e-payment",
      "discover payment", "discover card", "citi card", "citi autopay", "barclaycard", "synchrony bank",
      "amex epayment", "applecard", "apple card", "bank of america credit card", "bk of amer visa",
      "wells fargo card", "us bank card", "credit one bank"], "Transfer", None, PAYEE_ONLY),
    # Moving money between your own pockets in the US: card payments as the card issuer words them, transfers
    # between your own checking and savings, and money to or from your own Venmo / PayPal / Cash App balance.
    # (A Zelle or Venmo payment to another person is spending: it's left for your rules, or to Claude.)
    (["payment thank you", "payment - thank you", "payment-thank you", "pymt thank you", "received your payment",
      "autopay payment", "credit crd autopay", "crd epay", "crcardpmt", "online transfer to sav",
      "online transfer from sav", "online transfer to chk", "online transfer from chk", "transfer to savings",
      "transfer from savings", "transfer to checking", "transfer from checking", "venmo cashout", "venmo cash out",
      "venmo addfunds", "venmo add funds", "transfer from venmo", "transfer to venmo", "paypal transfer",
      "cash app cash out"], "Transfer", None, True),
    (["credit card bill", "card bill payment", "cc payment",
      "payment received towards your", "payment towards your credit card", "thank you for your payment",
      "credited to your card", "to your card ending", "received towards your credit card",
      "cc bill", "card payment",
      "si-tad", "si tad", "si-mad", "autopay for credit card", "been received towards your", "received on your card", "payment received on your card",
      "self transfer", "own account", "sweep in", "sweep out", "auto sweep"], "Transfer", None, True),
    (["zerodha", "groww", "upstox", "kuvera", "indmoney", "dhan", "smallcase", "angel one", "iccl",
      "indian clearing", "nse clearing", "bse ltd", "bse limited", "mutual fund", "sip", "cams", "kfintech",
      "nps", "ppf", "fixed deposit", "recurring deposit"], "Investments", None, False),
    # US brokers and robo-advisers as the payee (their own alerts name them, so payee only).
    (["fidelity", "vanguard", "schwab", "robinhood", "etrade", "e*trade", "wealthfront", "betterment", "acorns",
      "merrill edge", "coinbase"], "Investments", None, PAYEE_ONLY),

    # Income
    (["salary", "sal cr", "payroll"], "Salary", "in", True),
    (["direct dep*", "dir dep*"], "Salary", "in", True),
    (["interest", "int.pd", "int pd", "int credit", "dividend", "div"], "Interest & dividends", "in", True),
    (["refund", "reversal", "reversed", "cashback"], "Refunds & cashback", "in", True),

    # Spending
    (["rent", "nobroker", "nestaway", "housing.com", "maintenance", "society", "furlenco*", "nobroker*"], "Rent & housing", "out", False),
    (["swiggy", "zomato", "eatsure", "dominos", "domino's", "mcdonald", "kfc", "starbucks", "burger king",
      "pizza hut", "subway", "chaayos", "third wave", "blue tokai", "restaurant", "cafe", "bakery", "dineout*", "www dineout*", "bundl technologies*", "eatclub*", "restauran*", "biryani*",
      "doordash", "dd *doordash", "grubhub", "uber eats", "ubereats", "postmates", "chipotle", "taco bell", "wendy's",
      "wendys", "chick-fil-a", "panera", "dunkin", "panda express", "shake shack", "five guys", "popeyes", "wingstop",
      "papa john's", "little caesars", "jack in the box", "in-n-out", "sonic drive"],
     "Food & dining", "out", False),
    (["blinkit", "zepto", "bigbasket", "instamart", "swiggy instamart", "dmart", "jiomart", "more retail",
      "licious", "country delight", "milkbasket", "nature's basket", "blink commerce*", "blinkit*", "grofers*", "swiggyinstamart*", "instamart*", "relianceretail*", "jiomart*", "zepto*", "kiranakart*",
      "costco", "whole foods", "wholefds", "trader joe*", "kroger", "safeway", "instacart", "aldi", "publix",
      "wegmans", "sprouts farmers", "albertsons", "food lion", "meijer", "h-e-b", "stop & shop", "harris teeter",
      "giant eagle", "fred meyer", "ralphs", "vons", "sam's club", "sams club"], "Groceries", "out", False),
    (["uber", "ola", "olacabs", "rapido", "namma yatri", "metro", "dmrc", "delhi metro", "mmrcl", "mumbai metro",
      "bmrcl", "bmtc", "cmrl", "hmrl", "kmrl", "ksrtc", "msrtc", "tsrtc", "apsrtc", "lyft", "waymo", "mta*nyct",
      "nyct paygo", "clipper card"], "Transport", "out", False),
    (["fastag", "hpcl", "bpcl", "indian oil", "iocl", "petrol", "fuel", "shell", "service station", "car wash",
      "carwash", "parking", "car care", "automobile*", "automotive*", "motors", "tyre*", "tyres", "puncture",
      "go mechanic", "gomechanic", "chevron", "exxon*", "mobil", "speedway", "valero", "sunoco", "arco", "circle k",
      "marathon petro*", "wawa", "e-zpass", "ezpass", "sunpass", "fastrak", "jiffy lube", "autozone"], "Car", "out", False),
    (["irctc", "makemytrip", "goibibo", "indigo", "interglobe", "air india", "akasa", "spicejet", "cleartrip",
      "ixigo", "redbus", "oyo", "airbnb", "booking.com", "agoda", "yatra*", "ctfligh*", "cleartri*", "emt flight", "emtflig*", "easemytrip*", "airbnb*", "klook*", "thomas cook*", "forex*",
      "delta air*", "united airlines", "american airlines", "southwest air*", "jetblue", "alaska air*", "spirit airl*",
      "frontier airlines", "expedia", "hotels.com", "marriott", "hilton", "hyatt", "amtrak"], "Travel", "out", False),
    (["amazon", "flipkart", "myntra", "ajio", "nykaa", "meesho", "croma", "reliance digital", "decathlon",
      "ikea", "tata cliq", "uniqlo", "h&m", "zara", "lenskart", "jewellers", "jewellery", "jewelers", "tanishq*", "miniso*", "credstor*", "gyftr*", "igp*", "amazon*", "flipkart*",
      "walmart", "wal-mart", "wm supercenter", "target", "best buy", "home depot", "lowe's", "lowes", "macy's",
      "nordstrom", "tj maxx", "tjmaxx", "marshalls", "ross stores", "kohl's", "etsy", "ebay", "wayfair", "old navy",
      "dollar tree", "dollar general"], "Shopping", "out", False),
    (["netflix", "spotify", "youtube", "google play", "apple.com", "apple services", "itunes", "prime video",
      "hotstar", "jiohotstar", "sonyliv", "zee5", "openai", "chatgpt", "anthropic", "claude.ai", "notion",
      "github", "icloud", "google one", "linkedin", "medium", "audible", "apple media services", "appleservices*", "grok*", "xai", "paddle*", "openai*",
      "hulu", "disney plus", "disneyplus", "hbo max", "paramount+", "peacock", "nytimes", "adobe", "dropbox",
      "microsoft*"], "Subscriptions", "out", False),
    (["bescom", "bwssb", "msedcl", "mahadiscom", "tneb", "tangedco", "bses", "tata power", "adani electricity",
      "tsspdcl", "apspdcl", "cesc", "kseb", "uppcl", "djb", "mahanagar gas", "indraprastha gas", "electricity", "airtel", "jio", "vodafone", "vi ", "act fibernet", "hathway",
      "tata play", "broadband", "piped gas", "indane", "bharat gas", "recharge", "bbps", "postpaid", "jioinapp*", "viinapp*", "airtelpre*", "myjio*", "jiopay*", "airtel*",
      "comcast", "xfinity", "verizon", "at&t", "t-mobile", "tmobile", "spectrum", "cox comm*", "optimum", "google fi",
      "mint mobile", "pg&e", "con ed", "coned", "duke energy", "dominion energy", "national grid", "xcel energy",
      "southern california edison", "georgia power", "fpl"],
     "Bills & utilities", "out", False),
    (["urban company", "urbanclap", "laundry", "dry clean"], "Home services", "out", False),
    (["apollo", "pharmeasy", "1mg", "netmeds", "practo", "pharmacy", "medical", "hospital", "clinic",
      "diagnostic", "medical*", "pharma*", "health care", "healthcare",
      "dental", "doctor", "medplus", "chemist*", "pathology", "dawa*", "cvs", "walgreens", "rite aid", "kaiser",
      "quest diagnostics", "labcorp", "one medical"], "Medical", "out", False),
    (["cult.fit", "cultfit", "gym", "wellness*", "fitness", "yoga", "sports", "planet fitness", "equinox",
      "orangetheory", "peloton", "classpass"], "Health & fitness", "out", False),
    (["bookmyshow", "pvr", "inox", "district", "steam", "playstation", "distric*", "book my show*", "bookmyshow*"], "Entertainment", "out", False),
    (["udemy", "coursera", "unacademy", "byju"], "Education", "out", False),
    (["insurance", "lic", "acko", "policybazaar", "hdfc ergo", "star health", "icici lombard", "policy bazaar", "max life*", "maxlife*",
      "geico", "progressive ins*", "state farm", "allstate", "lemonade", "liberty mutual"], "Insurance", "out", False),
    (["emi", "loan", "bajaj finance", "bajaj finserv", "home credit", "mortgage", "sallie mae", "navient", "nelnet",
      "mohela"], "Loans & EMI", "out", True),
    (["atm", "cash withdrawal", "cash wdl", "nwd", "atw"], "Cash", "out", True),
]


def _compile(keywords: list[str]) -> re.Pattern:
    """Whole-word match; a trailing * matches as a prefix, for merchant codes like CTFLIGH00001234."""
    alts = "|".join(re.escape(k.strip()[:-1]) if k.strip().endswith("*") else re.escape(k.strip()) + r"(?![a-z0-9])"
                    for k in keywords if k.strip())
    return re.compile(rf"(?<![a-z0-9])(?:{alts})", re.I)


@lru_cache(maxsize=1)
def _rules() -> list[tuple[re.Pattern, str, str | None, bool]]:
    rules = []
    if paths.RULES_PATH.exists():
        try:
            for r in json.loads(paths.RULES_PATH.read_text()):
                kws = r["match"] if isinstance(r["match"], list) else [r["match"]]
                rules.append((_compile(kws), r["category"], r.get("direction"), bool(r.get("text", True))))
        except (ValueError, KeyError) as e:
            raise SystemExit(f"rules.json is not valid: {e}")
    for kws, cat, direction, text in DEFAULT_RULES:
        rules.append((_compile(kws), cat, direction, text))
    return rules


def reload_rules() -> None:
    _rules.cache_clear()


# Penalties the bank takes ("ECS/NACH dishonored ... debited as return charges"). Checked before any rule,
# because the alert also names what bounced (e.g. NACH), which a rule may send to Investments or EMI.
BANK_CHARGES_RE = _compile(["return charges", "return charge", "bounce charges", "dishonour charges",
                            "dishonor charges", "min balance charges", "minimum balance charges",
                            "non maintenance charges", "annual maintenance charges", "annual maintenance charge"])


def rule_index(direction: str, counterparty: str | None, text: str) -> int | None:
    """Index into _rules() of the rule that decides this payment (user rules come first), None if none."""
    party = counterparty or ""
    for i, (rx, cat, rdir, use_text) in enumerate(_rules()):
        if rdir and rdir != direction:
            continue
        if (party and rx.search(party)) or (use_text is not None and (use_text or not party) and rx.search(text or "")):
            return i
    return None


def categorize(direction: str, counterparty: str | None, text: str) -> str:
    party = counterparty or ""
    if direction == "out" and BANK_CHARGES_RE.search(text or ""):
        return "Bank charges"
    for rx, cat, rdir, use_text in _rules():
        if rdir and rdir != direction:
            continue
        if party and rx.search(party):
            return cat
        if use_text is not None and (use_text or not party) and rx.search(text or ""):
            return cat
    return "Other income" if direction == "in" else "Other spending"


def add_user_rule(keyword: str, category: str, direction: str | None = None, match_text: bool = True) -> None:
    rules = json.loads(paths.RULES_PATH.read_text()) if paths.RULES_PATH.exists() else []
    entry = {"match": keyword, "category": category}
    if direction:
        entry["direction"] = direction
    if not match_text:
        entry["text"] = False   # payee name only, not the whole alert
    rules = [r for r in rules if {k: v for k, v in r.items() if k != "by"} != entry]   # same rule again: move it up
    rules.insert(0, entry)
    paths.RULES_PATH.write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n")
    reload_rules()
