"""One-tap category suggestions for Sort out.

For each payee still waiting for a category: up to three likely categories, each with the reason it was picked,
from (in this order of trust):
1. Claude's own guess, when the nightly sort looked at this payee but wasn't sure enough to apply it;
2. payees you've already sorted that share a word of the name (another "... General Stores");
3. a word in the name itself ("... Medicals" is Medical, "... Tailors" a home service);
4. the same amount about every month (a subscription);
5. a name that looks like a person's (Friends & family);
6. the categories you use most, to fill up the row.
Worked out on this Mac from your own data. Nothing is applied until you tap one.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date
from statistics import median

from . import tidy

MAX = 3

# Words in a shop's name that say what it sells. Generic, in English as shops in most places write them; a payee
# your rules already match never reaches Sort out, so these only need to cover what's left.
NAME_WORDS = {
    "Groceries": ["grocery", "groceries", "grocer", "kirana", "supermarket", "supermarkets", "provisions", "mart",
                  "dairy", "vegetables", "veggies", "fruits", "general stores", "general store", "market"],
    "Food & dining": ["cafe", "coffee", "restaurant", "restaurants", "kitchen", "kitchens", "foods", "food",
                      "biryani", "pizza", "pizzeria", "dhaba", "canteen", "juice", "juices", "tea", "chai", "sweets",
                      "bakery", "bakers", "bar", "grill", "diner", "eatery", "bistro", "burger", "burgers", "tiffin",
                      "mess", "catering", "caterers", "taco", "sushi", "noodles", "deli"],
    "Medical": ["medical", "medicals", "pharmacy", "pharmacies", "pharma", "chemist", "chemists", "drugs", "clinic",
                "clinics", "hospital", "hospitals", "dental", "dentist", "diagnostic", "diagnostics", "pathology",
                "labs", "health care", "healthcare", "optical", "opticals", "eye care", "physio", "physiotherapy"],
    "Transport": ["travels", "cab", "cabs", "taxi", "taxis", "parking", "toll", "tolls", "metro", "transit", "rides"],
    "Car": ["fuel", "fuels", "petrol", "diesel", "filling station", "service station", "motors", "automobiles",
            "automotive", "garage", "tyres", "tires", "car wash", "car care", "auto parts"],
    "Home services": ["tailor", "tailors", "laundry", "laundromat", "dry cleaners", "dry cleaning", "cleaners",
                      "plumber", "plumbing", "electrician", "electricals", "repairs", "repair", "carpenter", "pest",
                      "housekeeping", "maid", "salon", "salons", "barber", "barbers", "parlour", "parlor"],
    "Health & fitness": ["gym", "gyms", "fitness", "yoga", "pilates", "crossfit", "sports", "athletic", "spa"],
    "Shopping": ["fashion", "fashions", "boutique", "garments", "textiles", "clothing", "apparel", "footwear",
                 "shoes", "electronics", "mobiles", "jewellers", "jewelers", "gifts", "books", "bookstore",
                 "stationery", "hardware", "furniture", "furnishings", "toys", "cosmetics", "outlet"],
    "Bills & utilities": ["electricity", "power", "water", "gas", "broadband", "internet", "fiber", "fibre",
                          "telecom", "wireless", "utilities", "utility", "cable"],
    "Rent & housing": ["rent", "rentals", "society", "apartment", "apartments", "housing", "realty", "properties",
                       "estates", "residency", "residences", "hoa", "landlord"],
    "Education": ["school", "schools", "college", "academy", "tuition", "tutorial", "tutorials", "classes",
                  "institute", "university", "coaching", "learning"],
    "Entertainment": ["cinema", "cinemas", "movies", "theatre", "theater", "gaming", "games", "concert", "tickets"],
    "Travel": ["travel", "tours", "holidays", "resort", "resorts", "hotel", "hotels", "airlines", "airways",
               "stays", "hostel", "inn", "lodge"],
    "Insurance": ["insurance", "assurance"],
    # money in
    "Salary": ["salary", "payroll"],
    "Interest & dividends": ["interest", "dividend", "dividends"],
    "Refunds & cashback": ["refund", "refunds", "cashback", "reversal"],
}
_WORD_RES = {cat: re.compile(r"(?<![a-z])(" + "|".join(re.escape(w) for w in sorted(ws, key=len, reverse=True))
                             + r")(?![a-z])") for cat, ws in NAME_WORDS.items()}

# Words that say nothing about what a payee is: company forms, honorifics, payment apps and UPI handles.
STOP = {"the", "and", "pvt", "ltd", "limited", "private", "india", "indian", "llp", "llc", "inc", "corp", "corporation",
        "company", "services", "service", "enterprises", "enterprise", "traders", "trading", "international",
        "solutions", "group", "online", "new", "sri", "shri", "shree", "sree", "payment", "payments", "pay", "upi",
        "paytm", "phonepe", "gpay", "bank", "ybl", "axl", "ibl", "apl", "okaxis", "okicici", "oksbi", "okhdfcbank",
        "store", "shop", "shops", "center", "centre", "and", "for", "www", "com"}
SHOP_WORDS = {part for ws in NAME_WORDS.values() for w in ws for part in w.split()}
BUSINESS = STOP | SHOP_WORDS


def _tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", (name or "").lower()) if t not in STOP}


def _looks_like_person(name: str) -> bool:
    """Two or three plain words, no digits, no handle, no shop word: "Vikram Bose", not "Kumar Tailors"."""
    if not name or re.search(r"[\d@&/]", name):
        return False
    words = re.findall(r"[A-Za-z]+", name)
    return (2 <= len(words) <= 3 and all(2 <= len(w) <= 14 for w in words)
            and not any(w.lower() in BUSINESS for w in words))


def _monthly(rows: list[sqlite3.Row]) -> bool:
    """Three or more payments of about the same amount, about a month apart."""
    if len(rows) < 3:
        return False
    amounts = [r["amount"] for r in rows]
    mid = median(amounts)
    if not mid or any(abs(a - mid) > 0.03 * mid for a in amounts):
        return False
    days = sorted(date.fromisoformat(r["ts"][:10]) for r in rows)
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    return 25 <= median(gaps) <= 35


def _allowed(direction: str) -> list[str]:
    cats = tidy.SPEND if direction == "out" else tidy.INCOME
    return [c for c in cats if c not in ("Other spending", "Other income")]


def add(conn: sqlite3.Connection, payees: list[dict]) -> list[dict]:
    """Give each payee (from tidy.payee_list) "sugs": [{cat, src, ...}], best first."""
    if not payees:
        return payees
    # 1. Claude's low-confidence guesses from the nightly sort
    guesses = {}
    for r in conn.execute("""SELECT key, reason, detail FROM ai_decisions WHERE job='categories' AND verdict='unsure'
                             AND undone=0 AND key LIKE 'payee:%'"""):
        d = json.loads(r["detail"] or "{}")
        if d.get("category"):
            guesses[r["key"]] = (d["category"], r["reason"] or "")
    # 2. what you've already sorted, by payee name
    sorted_payees = conn.execute(
        """SELECT MAX(COALESCE(NULLIF(counterparty,''), vpa)) AS name, direction, category, COUNT(*) AS n
           FROM transactions WHERE duplicate_of IS NULL AND COALESCE(NULLIF(counterparty,''), vpa, '') != ''
             AND category NOT IN ('Other spending','Other income','Transfer','Investments')
           GROUP BY LOWER(COALESCE(NULLIF(counterparty,''), vpa)), direction, category""").fetchall()
    by_token: dict[tuple[str, str], list] = defaultdict(list)
    for r in sorted_payees:
        for t in _tokens(r["name"]):
            by_token[(r["direction"], t)].append(r)
    # 6. the categories you use most (last half year), per direction
    often = {d: [c for c, _ in Counter(r["category"] for r in conn.execute(
        """SELECT category FROM transactions WHERE duplicate_of IS NULL AND direction=? AND ts >= date('now','-6 months')
           AND category NOT IN ('Other spending','Other income','Transfer','Investments')""", (d,))).most_common(8)]
        for d in ("out", "in")}

    for p in payees:
        d, allowed, sugs = p["dir"], _allowed(p["dir"]), []

        def offer(cat, src, **why):
            if cat in allowed and cat not in [s["cat"] for s in sugs] and len(sugs) < MAX:
                sugs.append({"cat": cat, "src": src, **why})

        g = guesses.get(f"payee:{d}:{p['key']}")
        if g:
            offer(g[0], "claude", reason=g[1][:140])
        # 2. similar names: a shared word counts more the fewer of your payees have it. A single shared word only
        # counts if it's a shop word ("General Stores"); "Joe" alone doesn't make "Joe's Pizza" a grocer.
        mine, score, like = _tokens(p["name"]), Counter(), {}
        seen = {id(r): r for t in mine for r in by_token.get((d, t), [])}
        for r in seen.values():
            shared = mine & _tokens(r["name"])
            if len(shared) < 2 and not shared & SHOP_WORDS:
                continue
            score[r["category"]] += sum(1 / len(by_token[(d, t)]) for t in shared if len(by_token[(d, t)]) <= 12)
            if r["n"] > like.get(r["category"], (0, ""))[0]:
                like[r["category"]] = (r["n"], r["name"])
        for cat, s in score.most_common(2):
            if s >= 0.25:
                offer(cat, "similar", like=like[cat][1])
        # 3. a word in the name
        low = (p["name"] or "").lower()
        for cat, rx in _WORD_RES.items():
            m = rx.search(low)
            if m:
                offer(cat, "name", word=m.group(1))
        # 4. a fixed monthly amount
        if d == "out":
            rows = conn.execute("""SELECT ts, amount FROM transactions WHERE duplicate_of IS NULL AND direction='out'
                                   AND LOWER(COALESCE(NULLIF(counterparty,''), vpa))=? ORDER BY ts""",
                                (p["key"],)).fetchall()
            if _monthly(rows):
                offer("Subscriptions", "monthly")
        # 5. a person
        if _looks_like_person(p["name"]):
            offer("Friends & family", "person")
        # 6. fill up with what you use most
        for cat in often[d]:
            offer(cat, "often")
        p["sugs"] = sugs
    return payees
