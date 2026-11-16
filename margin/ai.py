"""Claude sorting: the judgement calls rules get wrong, made by Claude Code on this Mac at each sync.

Jobs, each sent in batches to `claude -p` (no tools, no connectors, no hooks, no saved session):
  senders    unknown senders: hide shops/apps/ads; a sender that looks like a bank is only flagged
  unread     bank alerts the parser couldn't read: count real payments, drop non-payments
  doubles    the same payment reported twice in different words: count it once
  payees     payments with no payee: take the merchant/person from the message (must appear in it word for word)
  splits     money from friends that is their share of a bill you paid: reduces that bill's category
  categories payees and payee-less payments still in "Other": pick a category when confident
  audit      every counted transaction checked against its message: amount, direction, payee, category,
             not-a-payment, double count (each transaction once; nightly that means the new ones)
  merged     every pair Margin merged as one payment: un-merge pairs that are really two payments
Your own choices (categories you set, your rules) are never changed: Claude only suggests.

Every decision is stored in ai_decisions with Claude's reason, shown in Sort out, and can be undone.
An undone or "unsure" decision is left to you and not asked again (payees: again once they have new payments).
What Claude sees: sender names, message text with 9+ digit numbers masked to the last 4, payee names,
amounts, dates, your category list, your own rules and your names as banks print them.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import categorize, db, fx, money, parse, paths, review, sync, tidy

JOBS = ("senders", "unread", "doubles", "payees", "splits", "categories", "audit", "merged")
LIMITS = {"senders": 60, "unread": 60, "doubles": 60, "payees": 120, "nameless": 80, "batch": 60}

BASE = (
    "You sort records for a personal finance tracker of one person<<WHERE>>. Amounts are in <<CURRENCY>> unless "
    "a message says otherwise. A wrong decision makes their numbers wrong, so only decide when you are confident "
    "and answer 'unsure' (or confidence 'low') otherwise; the person decides those themselves. Reasons: plain "
    "English, under 15 words. Answer only through the JSON schema, with one decision for every item id.\n\n")

SENDERS_SYSTEM = BASE + (
    "Each item is a sender whose messages the tracker does NOT count, because it isn't on its list of banks. "
    "The tracker counts only alerts from the person's banks and card issuers; a merchant's receipt repeats a "
    "payment the bank already reported, so counting it would count the payment twice.\n"
    "verdict 'bank': the sender is a bank or card issuer (or its alert service) reporting money moving in the "
    "person's own account or card: debits, credits, card spends, refunds.\n"
    "verdict 'not_bank': shops, apps, wallets and payment apps (CRED, PhonePe, Paytm, Amazon Pay, Google Pay), "
    "payment gateways (Razorpay, Stripe, Paddle, PayU), brokers, fund houses, insurers, government bodies (EPFO), "
    "newsletters, offers and ads, OTP senders.\n"
    "verdict 'unsure': you can't tell from the name and examples.")

UNREAD_SYSTEM = BASE + (
    "Each item is a message from one of the person's banks that the tracker could not read.\n"
    "verdict 'payment': money actually moved in this message: a debit, credit, card spend, refund or reversal "
    "(a reversal or refund is direction 'in'). Give amount and currency exactly as written (e.g. 21.43 and "
    "'USD'; rupees are 'INR'), direction 'out' (money left) or 'in' (money came in), the payee or merchant name "
    "as written ('' if none), the last 4 digits of the card or account if shown ('' if not), and card=true when "
    "it is a credit/debit card.\n"
    "verdict 'not_payment': nothing was charged or credited in this message: OTPs, offers, statements, "
    "reminders, 'will be debited' notices, mandate or AutoPay set-ups that only register (no charge), failed or "
    "declined attempts, balance updates.\n"
    "verdict 'unsure': can't tell, or the amount is masked or missing.")

DOUBLES_SYSTEM = BASE + (
    "Each item is a pair of counted payments with the same amount and direction a few days apart.\n"
    "verdict 'same': both messages report ONE money movement, e.g. an email and a text about the same spend, or "
    "'refund processed' followed by 'refunded by' for the same refund, or 'reversed' then 'refunded'.\n"
    "verdict 'different': two separate payments, e.g. two orders, a repeat subscription, two transfers.\n"
    "verdict 'unsure': can't tell.")

CATEGORIES_SYSTEM = BASE + (
    "Each item is a payee (with all its payments) or a single payment without a payee name, still uncategorised. "
    "Pick one category from the lists given for its direction ('out' = paid by the person, 'in' = received).\n"
    "- Brands and merchants: their obvious category (Swiggy -> Food & dining, Blinkit -> Groceries, Uber -> "
    "Travel, Netflix -> Subscriptions).\n"
    "- 'in' with a spending category means a refund that reduces that spending.\n"
    "- Transfer: the person's own accounts, credit-card bill payments. Investments: SIPs, stocks, gold.\n"
    "- Individual people's names: confidence 'low' unless the person's own rules already show the pattern. Never "
    "guess Rent or Friends & family from a name alone.\n"
    "- Payment gateways alone (Razorpay, PayU, BillDesk, Cashfree): 'low'.\n"
    "confidence: 'high' = you'd bet on it, 'medium' = very likely, 'low' = a guess (not applied).\n"
    "Use the person's existing rules as hints about their life.")


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {"decisions": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, **props, "reason": {"type": "string"}},
        "required": ["id", *required, "reason"]}}}, "required": ["decisions"]}


SENDERS_SCHEMA = _schema({"verdict": {"type": "string", "enum": ["bank", "not_bank", "unsure"]}}, ["verdict"])
DOUBLES_SCHEMA = _schema({"verdict": {"type": "string", "enum": ["same", "different", "unsure"]}}, ["verdict"])
UNREAD_SCHEMA = _schema({
    "verdict": {"type": "string", "enum": ["payment", "not_payment", "unsure"]},
    "amount": {"type": "number"}, "currency": {"type": "string"},
    "direction": {"type": "string", "enum": ["in", "out", ""]}, "payee": {"type": "string"},
    "last4": {"type": "string"}, "card": {"type": "boolean"}}, ["verdict"])
CATEGORIES_SCHEMA = _schema({"category": {"type": "string"},
                             "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
                            ["category", "confidence"])


# --- helpers ------------------------------------------------------------------------------------
def _mask(text: str | None, n: int = 400) -> str:
    t = re.sub(r"<[^>]*>?", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return re.sub(r"\d{9,}", lambda m: "•" * (len(m.group()) - 4) + m.group()[-4:], t)[:n]


def _decided(conn, job: str) -> dict:
    return {r["key"]: r for r in conn.execute("SELECT * FROM ai_decisions WHERE job=?", (job,))}


def _record(conn, job, key, verdict, label, reason, detail=None, applied=False) -> None:
    conn.execute(
        """INSERT INTO ai_decisions(at,job,key,verdict,label,reason,detail,applied) VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(job,key) DO UPDATE SET at=excluded.at, verdict=excluded.verdict, label=excluded.label,
           reason=excluded.reason, detail=excluded.detail, applied=excluded.applied, undone=0""",
        (datetime.now().isoformat(timespec="seconds"), job, key, verdict, label, (reason or "")[:200],
         json.dumps(detail or {}, ensure_ascii=False), int(applied)))


def _context(settings: dict) -> dict:
    rules = json.loads(paths.RULES_PATH.read_text()) if paths.RULES_PATH.exists() else []
    return {
        "categories": {"out": tidy.SPEND + tidy.BOTH, "in": tidy.INCOME + tidy.BOTH + _paid_back()},
        "your_rules": [{"match": r["match"], "category": r["category"], "direction": r.get("direction", "any")}
                       for r in rules],
        "your_names_as_banks_print_them": settings.get("self_names", []),
        "about_the_person": settings.get("about_me", []),
    }


def _paid_back() -> list[str]:
    return [c for c in tidy.SPEND if c != "Other spending"]


def _ask(payload: dict, system: str, schema: dict, settings: dict) -> tuple[list, str]:
    home = money.home(settings)
    for token, value in (("<<WHERE>>", " in India" if home == "INR" else ""),
                         ("<<CURRENCY>>", "rupees (INR)" if home == "INR" else f"their home currency ({home})"),
                         ("<<SYM>>", money.symbol(home).strip())):
        system = system.replace(token, value)
    payload = {**payload, "home_currency": home}
    out, err = review.ask_claude(payload, "sort", settings, system=system, schema=schema, timeout=600)
    if out is None:
        return [], err
    return [d for d in out.get("decisions", []) if isinstance(d, dict) and "id" in d], ""


def _home() -> str:
    return money.home(sync.load_settings())


def _money(x: float) -> str:
    sym = money.symbol(_home())
    return f"{sym}{x:,.0f}" if x >= 100 else f"{sym}{x:,.2f}".rstrip("0").rstrip(".")


def _save_settings(settings: dict) -> None:
    paths.SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


# --- jobs ---------------------------------------------------------------------------------------
def job_senders(conn, settings) -> tuple[int, str]:
    done = _decided(conn, "senders")
    todo = [s for s in tidy.sender_list(conn, settings) if s["key"] not in done][:LIMITS["senders"]]
    if not todo:
        return 0, ""
    examples: dict[str, list[str]] = {}
    for r in conn.execute("SELECT sender, snippet FROM review WHERE reason LIKE 'sender isn%' ORDER BY ts DESC"):
        lst = examples.setdefault(parse.sender_key(r["sender"]), [])
        if len(lst) < 3 and r["snippet"] not in lst:
            lst.append(r["snippet"])
    items = [{"id": str(i), "sender": s["key"], "messages": s["n"],
              "examples": [_mask(x, 300) for x in examples.get(s["key"], [])]} for i, s in enumerate(todo)]
    decisions, err = _ask({"items": items}, SENDERS_SYSTEM, SENDERS_SCHEMA, settings)
    applied = 0
    for d in decisions:
        try:
            s = todo[int(d["id"])]
        except (ValueError, IndexError):
            continue
        v = d.get("verdict")
        if v == "not_bank":
            if s["key"] not in settings["ignored_senders"]:
                settings["ignored_senders"].append(s["key"])
            _record(conn, "senders", s["key"], v, s["key"], d.get("reason"), applied=True)
            applied += 1
        elif v in ("bank", "unsure"):
            _record(conn, "senders", s["key"], v, s["key"], d.get("reason"))
    _save_settings(settings)
    return applied, err


def job_unread(conn, settings) -> tuple[int, str]:
    done = _decided(conn, "unread")
    rows = [r for r in conn.execute("SELECT * FROM review WHERE reason NOT LIKE 'sender isn%' ORDER BY ts DESC")
            if r["uid"] not in done][:LIMITS["unread"]]
    if not rows:
        return 0, ""
    items = [{"id": str(i), "date": r["ts"][:16].replace("T", " "), "sender": r["sender"], "text": _mask(r["snippet"])}
             for i, r in enumerate(rows)]
    decisions, err = _ask({"items": items}, UNREAD_SYSTEM, UNREAD_SCHEMA, settings)
    applied = 0
    for d in decisions:
        try:
            r = rows[int(d["id"])]
        except (ValueError, IndexError):
            continue
        keep = dict(r)
        v, reason = d.get("verdict"), d.get("reason")
        if v == "not_payment":
            conn.execute("DELETE FROM review WHERE uid=?", (r["uid"],))
            _record(conn, "unread", r["uid"], v, f"{r['sender'][:40]} · {r['ts'][:10]}", reason,
                    {"review": keep}, applied=True)
            applied += 1
            continue
        home = money.home(settings)
        cur = (d.get("currency") or home).upper().replace("RS", "INR").replace("₹", "INR")
        amt, direction = float(d.get("amount") or 0), d.get("direction")
        bank = parse.bank_for_sender(r["sender"] or "", settings["trusted_senders"])
        rate, rate_src = fx.rate(conn, cur, home, r["ts"][:10], settings) if v == "payment" else (None, "")
        if v != "payment" or amt <= 0 or direction not in ("in", "out") or not bank or rate is None:
            why = reason if v != "payment" or rate is not None else rate_src
            _record(conn, "unread", r["uid"], "unsure", f"{r['sender'][:40]} · {r['ts'][:10]}", why, {"review": keep})
            continue
        inr = round(amt * rate, 2)
        payee = (d.get("payee") or "").strip()[:80] or None
        last4 = re.sub(r"\D", "", d.get("last4") or "")[-4:] or None
        kind = "card" if d.get("card") else "account"
        note = "" if cur == home else fx_note(cur, amt, inr, rate, rate_src, home)
        raw = f"[read by Claude]{note} {r['snippet']}"
        cat = sync._category(direction, payee or "", raw, inr, settings)
        cur_ = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (uid,source,sender,bank,ts,amount,direction,account,last4,counterparty,vpa,category,raw)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{r['uid']}:ai", r["source"], r["sender"], bank, r["ts"], inr, direction,
             sync._account_label(bank, kind, last4), last4, payee, None, cat, raw))
        if not cur_.rowcount:
            continue
        tid = cur_.lastrowid
        sync._mark_duplicate(conn, tid, r["source"], r["ts"], inr, direction, last4)
        conn.execute("DELETE FROM review WHERE uid=?", (r["uid"],))
        label = f"{'Paid' if direction == 'out' else 'Got'} {_money(inr)}" + (f" ({cur} {amt:g})" if cur != home else "") \
            + (f" · {payee}" if payee else "") + f" · {r['ts'][:10]}"
        _record(conn, "unread", r["uid"], "payment", label, reason, {"review": keep, "txn": tid}, applied=True)
        applied += 1
    return applied, err


def fx_note(cur: str, amt: float, conv: float, rate: float, src: str, home: str = "INR") -> str:
    """The note kept on a converted payment: "[USD 21.43 ≈ ₹1,810.84 at ₹84.5/USD, ...]" (or "≈ $..." etc.)."""
    sym = money.symbol(home).strip()
    whose = "rupee" if home == "INR" else home
    return (f" [{cur} {amt:g} ≈ {sym}{conv:,.2f} at {sym}{rate:g}/{cur}, {src}; the card statement has the exact {whose} "
            f"amount and any forex markup]")


FX_NOTE_RE = re.compile(r"^\[read by Claude\] \[([A-Z]{3}) ([0-9.]+) ≈ [^\d\s\]]*[0-9,.]+ at [^\d\s\]]*[0-9.]+/[A-Z]{3}[^\]]*\]")


def reconvert(conn, settings) -> int:
    """Re-convert foreign-currency payments Claude read, with the day's rate (fixes ones made at an old flat rate)."""
    n = 0
    for r in conn.execute("SELECT id, ts, amount, raw FROM transactions WHERE raw LIKE '[read by Claude] [%'").fetchall():
        m = FX_NOTE_RE.match(r["raw"] or "")
        if not m:
            continue
        cur, amt = m.group(1), float(m.group(2))
        home = money.home(settings)
        rate, src = fx.rate(conn, cur, home, r["ts"][:10], settings)
        if rate is None:
            continue
        inr = round(amt * rate, 2)
        raw = "[read by Claude]" + fx_note(cur, amt, inr, rate, src, home) + r["raw"][m.end():]
        if abs(inr - r["amount"]) >= 0.01 or raw != r["raw"]:
            conn.execute("UPDATE transactions SET amount=?, raw=? WHERE id=?", (inr, raw, r["id"]))
            n += 1
    for d in conn.execute("SELECT id, detail, label FROM ai_decisions WHERE job='unread' AND verdict='payment'").fetchall():
        t = conn.execute("SELECT amount FROM transactions WHERE id=?", (json.loads(d["detail"] or "{}").get("txn"),)).fetchone()
        if t and d["label"]:
            conn.execute("UPDATE ai_decisions SET label=? WHERE id=?",
                         (re.sub(r"^(Paid|Got) [^\d\s]*[0-9,.]+", lambda x: f"{x.group(1)} {_money(t['amount'])}", d["label"]), d["id"]))
    conn.commit()
    return n


def _row(r) -> dict:
    return {"date": r["ts"][:16].replace("T", " "), "source": r["source"], "account": r["account"] or "",
            "amount": r["amount"], "direction": r["direction"], "payee": r["counterparty"] or r["vpa"] or "",
            "text": _mask(r["raw"], 300)}


def job_doubles(conn, settings) -> tuple[int, str]:
    done = _decided(conn, "doubles")
    pairs = [p for p in conn.execute(
        """SELECT a.id AS a, b.id AS b FROM transactions a JOIN transactions b
             ON a.id != b.id AND a.direction = b.direction AND ABS(a.amount - b.amount) < 0.01
            AND (a.ts < b.ts OR (a.ts = b.ts AND a.id < b.id))
            AND julianday(b.ts) - julianday(a.ts) <= 5
            AND SUBSTR(COALESCE(a.raw,''),1,40) != SUBSTR(COALESCE(b.raw,''),1,40)
            AND (a.last4 IS NULL OR b.last4 IS NULL OR a.last4 = b.last4)
           WHERE a.duplicate_of IS NULL AND b.duplicate_of IS NULL AND a.amount >= 100
           ORDER BY b.ts DESC""").fetchall() if f"{p['a']}-{p['b']}" not in done][:LIMITS["doubles"]]
    if not pairs:
        return 0, ""
    get = lambda i: conn.execute("SELECT * FROM transactions WHERE id=?", (i,)).fetchone()  # noqa: E731
    items = [{"id": str(i), "first": _row(get(p["a"])), "second": _row(get(p["b"]))} for i, p in enumerate(pairs)]
    decisions, err = _ask({"items": items}, DOUBLES_SYSTEM, DOUBLES_SCHEMA, settings)
    applied = 0
    for d in decisions:
        try:
            p = pairs[int(d["id"])]
        except (ValueError, IndexError):
            continue
        a, b, v = get(p["a"]), get(p["b"]), d.get("verdict")
        if not a or not b:
            continue
        label = f"{_money(a['amount'])} · {a['counterparty'] or a['account'] or ''} · {a['ts'][:10]} and {b['ts'][:10]}"
        key = f"{p['a']}-{p['b']}"
        if v == "same" and a["duplicate_of"] is None and b["duplicate_of"] is None:
            conn.execute("UPDATE transactions SET duplicate_of=? WHERE id=?", (a["id"], b["id"]))
            _record(conn, "doubles", key, v, label, d.get("reason"), {"kept": a["id"], "dropped": b["id"]}, True)
            applied += 1
        elif v in ("different", "unsure"):
            _record(conn, "doubles", key, v, label, d.get("reason"))
    return applied, err


def job_categories(conn, settings) -> tuple[int, str]:
    done = _decided(conn, "categories")
    allowed = {"out": set(tidy.SPEND + tidy.BOTH), "in": set(tidy.INCOME + tidy.BOTH + _paid_back())}
    payees = []
    for g in tidy._payee_groups(conn):
        key = f"payee:{g['direction']}:{g['k']}"
        old = done.get(key)
        if old and (old["undone"] or json.loads(old["detail"] or "{}").get("n") == g["n"]):
            continue      # undone by you, or already asked with no new payments since
        payees.append((key, g))
    payees = payees[:LIMITS["payees"]]
    nameless = [r for r in conn.execute(
        """SELECT * FROM transactions WHERE duplicate_of IS NULL AND category IN ('Other spending','Other income')
           AND COALESCE(NULLIF(counterparty,''), vpa, '') = '' AND category_locked = 0 ORDER BY ts DESC""")
        if f"txn:{r['id']}" not in done][:LIMITS["nameless"]]
    if not payees and not nameless:
        return 0, ""
    items = []
    for i, (key, g) in enumerate(payees):
        rows = conn.execute(
            """SELECT ts, amount, account, vpa FROM transactions WHERE duplicate_of IS NULL AND direction=?
               AND LOWER(COALESCE(NULLIF(counterparty,''), vpa))=? ORDER BY ts DESC LIMIT 6""",
            (g["direction"], g["k"])).fetchall()
        items.append({"id": f"p{i}", "kind": "payee", "payee": g["name"], "direction": g["direction"],
                      "payments": g["n"], "total": round(g["total"]), "upi_ids": sorted({r["vpa"] for r in rows if r["vpa"]}),
                      "recent": [{"date": r["ts"][:16].replace("T", " "), "amount": r["amount"], "account": r["account"]}
                                 for r in rows]})
    for i, r in enumerate(nameless):
        items.append({"id": f"t{i}", "kind": "payment", **_row(r)})
    decisions, err = _ask({**_context(settings), "items": items}, CATEGORIES_SYSTEM, CATEGORIES_SCHEMA, settings)
    applied = 0
    for d in decisions:
        sid, cat, conf = str(d["id"]), d.get("category", ""), d.get("confidence")
        try:
            idx = int(sid[1:])
        except ValueError:
            continue
        if sid.startswith("p") and idx < len(payees):
            key, g = payees[idx]
            ok = conf in ("high", "medium") and cat in allowed[g["direction"]]
            if ok:
                categorize.add_user_rule(g["name"], cat, g["direction"], match_text=False)
                rules = json.loads(paths.RULES_PATH.read_text())
                rules[0]["by"] = "claude"
                paths.RULES_PATH.write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n")
                categorize.reload_rules()
                applied += 1
            _record(conn, "categories", key, cat if ok else "unsure", f"{g['name']} → {cat or '?'}", d.get("reason"),
                    {"n": g["n"], "match": g["name"], "category": cat, "direction": g["direction"]}, ok)
        elif sid.startswith("t") and idx < len(nameless):
            r = nameless[idx]
            ok = conf in ("high", "medium") and cat in allowed[r["direction"]]
            if ok:
                conn.execute("UPDATE transactions SET category=?, category_locked=2 WHERE id=?", (cat, r["id"]))
                applied += 1
            _record(conn, "categories", f"txn:{r['id']}", cat if ok else "unsure",
                    f"{_money(r['amount'])} · {r['account'] or ''} · {r['ts'][:10]} → {cat or '?'}", d.get("reason"),
                    {"txn": r["id"]}, ok)
    return applied, err


PAYEES_SYSTEM = BASE + (
    "Each item is a counted payment whose payee the tracker couldn't find. From its bank message ('text'), give the "
    "merchant or person the money went to (direction 'out') or came from ('in'), copied exactly as written in the "
    "text, e.g. 'RAZ*Swiggy', 'ZOMATO LIMITED', 'KAVYA MENON K'. Not the bank itself, not the person's own "
    "name unless it really is a transfer to themselves, not a greeting or a word like 'merchant'. If the text "
    "names no payee (e.g. 'NACH debit', 'ATM withdrawal', 'charges'), payee ''.")
PAYEES_SCHEMA = _schema({"payee": {"type": "string"}}, ["payee"])


def _in_text(name: str, text: str) -> bool:
    norm = lambda x: re.sub(r"\s+", " ", x or "").strip().lower()  # noqa: E731
    return bool(name.strip()) and norm(name) in norm(text)


def job_payees(conn, settings, progress=None) -> tuple[int, str]:
    done = _decided(conn, "payees")
    rows = [r for r in conn.execute(
        """SELECT * FROM transactions WHERE duplicate_of IS NULL AND COALESCE(counterparty,'')='' AND COALESCE(vpa,'')=''
           ORDER BY ts DESC""") if f"txn:{r['id']}" not in done]
    if not rows:
        return 0, ""
    batches = _batches(rows, LIMITS["batch"])
    results = _parallel([[{"id": str(r["id"]), **_row(r)} for r in b] for b in batches],
                        lambda b: _ask({"items": b}, PAYEES_SYSTEM, PAYEES_SCHEMA, settings), progress,
                        "Claude is finding payees")
    applied, errs = 0, []
    for b, (decisions, err) in zip(batches, results):
        if err:
            errs.append(err)
            continue
        got = {str(d["id"]): (d.get("payee") or "").strip() for d in decisions}
        for r in b:
            name = got.get(str(r["id"]), "")[:80]
            if name and _in_text(name, r["raw"]) and not parse.is_junk_party(name):
                conn.execute("UPDATE transactions SET counterparty=? WHERE id=? AND COALESCE(counterparty,'')=''",
                             (name, r["id"]))
                _record(conn, "payees", f"txn:{r['id']}", "fixed", f"{_txn_label(r)}: payee → {name}", "",
                        {"txn": r["id"], "fix": {"counterparty": name}, "old": {"counterparty": None}}, applied=True)
                applied += 1
            else:
                _record(conn, "payees", f"txn:{r['id']}", "none", "", name and "not found in the message")
        conn.commit()
    return applied, "; ".join(dict.fromkeys(errs))


SPLITS_SYSTEM = BASE + (
    "Each item is money the person RECEIVED from someone, still uncategorised, with the bills the person paid "
    "around then ('bills'). Read 'about_the_person' first. If the sender is an individual and this is their share "
    "of one of those bills (a shared meal, trip, stay, cab, groceries), give that bill's id: the money then reduces "
    "that bill's category. Good signs: a bill a few days before, several people sending similar amounts, the share "
    "being a sensible fraction of the bill. Businesses, salary, refunds from shops: bill_id ''. If it's a split but "
    "no listed bill fits, bill_id '' and say so. confidence 'high' = clear match, 'medium' = most likely.")
SPLITS_SCHEMA = _schema({"bill_id": {"type": "string"},
                         "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}, ["bill_id", "confidence"])


def job_splits(conn, settings, progress=None) -> tuple[int, str]:
    done = _decided(conn, "splits")
    rows = [r for r in conn.execute(
        """SELECT * FROM transactions WHERE duplicate_of IS NULL AND direction='in' AND category='Other income'
           AND category_locked=0 AND COALESCE(NULLIF(counterparty,''), vpa, '') != '' ORDER BY ts""")
        if f"txn:{r['id']}" not in done]
    if not rows:
        return 0, ""
    paid_back = set(_paid_back())
    items, bills_by = [], {}
    never = ["Transfer", "Investments", "Rent & housing", *settings.get("project_categories", [])]   # not split bills
    for r in rows:
        bills = conn.execute(
            f"""SELECT * FROM transactions WHERE duplicate_of IS NULL AND direction='out' AND amount >= ?
               AND category NOT IN ({','.join('?' * len(never))})
               AND julianday(?) - julianday(ts) BETWEEN -2 AND 10 ORDER BY amount DESC LIMIT 8""",
            (r["amount"] * 1.2, *never, r["ts"])).fetchall()
        bills_by[r["id"]] = {b["id"]: b for b in bills}
        items.append({"id": str(r["id"]), "date": r["ts"][:16].replace("T", " "), "amount": r["amount"],
                      "from": r["counterparty"] or r["vpa"],
                      "bills": [{"id": str(b["id"]), "date": b["ts"][:16].replace("T", " "), "amount": b["amount"],
                                 "payee": b["counterparty"] or b["vpa"] or "", "category": b["category"]} for b in bills]})
    decisions, err = _ask({**_context(settings), "items": items}, SPLITS_SYSTEM, SPLITS_SCHEMA, settings)
    if err:
        return 0, err
    applied = 0
    by_id = {r["id"]: r for r in rows}
    for d in decisions:
        try:
            r = by_id[int(d["id"])]
        except (ValueError, KeyError):
            continue
        bill = bills_by[r["id"]].get(int(d["bill_id"])) if str(d.get("bill_id") or "").isdigit() else None
        who = r["counterparty"] or r["vpa"]
        if bill and d.get("confidence") in ("high", "medium") and bill["category"] in paid_back:
            conn.execute("UPDATE transactions SET category=?, category_locked=2 WHERE id=?", (bill["category"], r["id"]))
            _record(conn, "splits", f"txn:{r['id']}", "fixed",
                    f"{_money(r['amount'])} from {who} · {r['ts'][:10]}: share of {bill['counterparty'] or bill['vpa'] or 'a bill'} "
                    f"{_money(bill['amount'])} ({bill['ts'][:10]}), reduces {bill['category']}", d.get("reason"),
                    {"txn": r["id"], "fix": {"category": bill["category"], "category_locked": 2},
                     "old": {"category": r["category"], "category_locked": 0}}, applied=True)
            applied += 1
        else:
            _record(conn, "splits", f"txn:{r['id']}", "unsure", f"{_money(r['amount'])} from {who} · {r['ts'][:10]}",
                    d.get("reason"))
    conn.commit()
    return applied, ""


AUDIT_SYSTEM = BASE + (
    "Each item is one transaction the tracker counted, with the bank message it was read from ('text'). Check "
    "each against its text and the person's context. Return a decision ONLY for items with a problem (an empty "
    "list is fine):\n"
    "- not_a_payment: the message doesn't report money that actually moved: reminders, dues or overdue notices, "
    "'will be debited' notices, mandate/AutoPay set-ups, OTPs, offers, failed or declined attempts, statements.\n"
    "- wrong_amount: the amount doesn't match the text. fix_amount in <<CURRENCY>>. Text marked '[read by Claude] [USD … ≈ "
    "<<SYM>>…]' was converted on purpose: don't report that.\n"
    "- wrong_direction: 'out' = money left the person, 'in' = money came in. fix_direction.\n"
    "- duplicate: the same single money movement as another item in this batch (e.g. an email and a text). "
    "duplicate_of = the other item's id.\n"
    "- wrong_category: clearly wrong for this payment. fix_category from the lists for its direction. Transfer = "
    "the person's own accounts and credit-card bill payments; Investments = SIPs, stocks, gold; 'in' with a "
    "spending category = a refund. set_by 'you' or 'your rule' is the person's own choice: report it only if "
    "the text clearly contradicts it, never for taste.\n"
    "- wrong_payee: the payee isn't the merchant or person in the text (a bank name, a fragment, a word like "
    "'was successful'). fix_payee as written in the text.\n"
    "confidence 'high' only when the text makes it certain.")

MERGED_SYSTEM = BASE + (
    "Each item is a pair the tracker merged as ONE money movement (it counts only 'kept'). Check with the texts.\n"
    "verdict 'same': both report the same single money movement (e.g. email and text, refund processed / refunded "
    "by, debit alert and forex alert of one purchase).\n"
    "verdict 'different': two separate money movements, so both should count.\n"
    "verdict 'unsure': can't tell.")

AUDIT_SCHEMA = _schema({
    "problem": {"type": "string", "enum": ["not_a_payment", "wrong_amount", "wrong_direction", "duplicate",
                                           "wrong_category", "wrong_payee"]},
    "fix_amount": {"type": "number"}, "fix_direction": {"type": "string", "enum": ["in", "out", ""]},
    "fix_category": {"type": "string"}, "fix_payee": {"type": "string"}, "duplicate_of": {"type": "string"},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}, ["problem", "confidence"])
MERGED_SCHEMA = DOUBLES_SCHEMA

NOT_COUNTED = 0   # duplicate_of = 0: not a payment (no row has id 0), so every "counted" query skips it


def _set_by(conn, r, settings: dict) -> str:
    if r["category_locked"] == 1:
        return "you"
    if r["category_locked"] == 2:
        return "Claude"
    party = " ".join(x for x in (r["counterparty"], r["vpa"]) if x)
    if sync._is_self(party, r["raw"] or "", settings["self_names"]):
        return "your names"
    i = categorize.rule_index(r["direction"], party, r["raw"] or "")
    user = json.loads(paths.RULES_PATH.read_text()) if paths.RULES_PATH.exists() else []
    if i is not None and i < len(user):
        return "Claude" if user[i].get("by") == "claude" else "your rule"
    return "default rule" if i is not None else "none"


def _batches(items: list, n: int) -> list[list]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def _parallel(batches: list, fn, progress=None, phase="") -> list:
    """Run fn over batches, 3 at a time (separate claude processes). Results in order."""
    out, done = [None] * len(batches), 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fn, b): i for i, b in enumerate(batches)}
        for f in futs:
            out[futs[f]] = f.result()
            done += 1
            if progress:
                progress(phase, done, len(batches), force=True)
    return out


def _txn_label(r) -> str:
    return f"{_money(r['amount'])} · {r['counterparty'] or r['vpa'] or r['account'] or ''} · {r['ts'][:10]}"


PROBLEM_TEXT = {"not_a_payment": "not a payment, no longer counted", "wrong_amount": "amount corrected",
                "wrong_direction": "direction corrected", "duplicate": "same payment counted twice, now once",
                "wrong_category": "category corrected", "wrong_payee": "payee corrected"}


def job_audit(conn, settings, progress=None) -> tuple[int, str]:
    done = _decided(conn, "audit")
    rows = [r for r in conn.execute("SELECT * FROM transactions WHERE duplicate_of IS NULL ORDER BY ts")
            if f"txn:{r['id']}" not in done]
    if not rows:
        return 0, ""
    ctx = _context(settings)
    by_id = {r["id"]: r for r in rows}

    def item(r):
        return {"id": str(r["id"]), **_row(r), "category": r["category"], "set_by": _set_by(conn, r, settings)}

    batches = [[item(r) for r in b] for b in _batches(rows, LIMITS["batch"])]
    results = _parallel(batches, lambda b: _ask({**ctx, "items": b}, AUDIT_SYSTEM, AUDIT_SCHEMA, settings),
                        progress, "Claude is checking every transaction")
    applied, errs = 0, []
    allowed = {"out": set(tidy.SPEND + tidy.BOTH), "in": set(tidy.INCOME + tidy.BOTH + _paid_back())}
    for batch, (decisions, err) in zip(batches, results):
        if err:
            errs.append(err)
            continue          # not recorded: checked again next time
        flagged = set()
        for d in decisions:
            try:
                tid = int(d["id"])
            except ValueError:
                continue
            r = conn.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
            if tid not in by_id or not r or r["duplicate_of"] is not None:
                continue
            prob, conf, reason = d.get("problem"), d.get("confidence"), d.get("reason")
            set_by = _set_by(conn, r, settings)
            label = f"{_txn_label(r)}: {PROBLEM_TEXT.get(prob, prob)}"
            old = {k: r[k] for k in ("amount", "direction", "category", "category_locked", "counterparty", "duplicate_of")}
            fix: dict = {}
            if prob == "not_a_payment" and conf == "high":
                fix = {"duplicate_of": NOT_COUNTED}
            elif prob == "wrong_amount" and conf == "high" and float(d.get("fix_amount") or 0) > 0:
                fix = {"amount": round(float(d["fix_amount"]), 2)}
                label = f"{_txn_label(r)}: amount {_money(r['amount'])} → {_money(fix['amount'])}"
            elif prob == "wrong_direction" and conf == "high" and d.get("fix_direction") in ("in", "out") \
                    and d["fix_direction"] != r["direction"]:
                fix = {"direction": d["fix_direction"]}
            elif prob == "duplicate" and conf == "high":
                try:
                    other = conn.execute("SELECT * FROM transactions WHERE id=?", (int(d.get("duplicate_of") or 0),)).fetchone()
                except ValueError:
                    other = None
                if other and other["id"] != tid and other["duplicate_of"] is None and abs(other["amount"] - r["amount"]) < 0.01:
                    keep, drop = (other, r) if other["ts"] <= r["ts"] else (r, other)
                    old = {"duplicate_of": None}
                    tid, fix = drop["id"], {"duplicate_of": keep["id"]}
            elif prob == "wrong_category" and conf in ("high", "medium") and d.get("fix_category") in allowed[r["direction"]] \
                    and d["fix_category"] != r["category"]:
                fix = {"category": d["fix_category"], "category_locked": 2}
                label = f"{_txn_label(r)}: {r['category']} → {d['fix_category']}"
                if set_by in ("you", "your rule"):
                    _record(conn, "audit", f"txn:{r['id']}", "suggest", label, reason,
                            {"txn": r["id"], "fix": fix, "old": old})
                    flagged.add(r["id"])
                    continue
            elif prob == "wrong_payee" and _in_text(d.get("fix_payee") or "", r["raw"] or "") and (
                    conf == "high" or (conf == "medium" and (not (r["counterparty"] or r["vpa"])
                                                             or parse.is_junk_party(r["counterparty"])))):
                # a blank or junk payee can only get better, so "very likely" is enough there
                fix = {"counterparty": d["fix_payee"].strip()[:80]}
                label = f"{_txn_label(r)}: payee → {fix['counterparty']}"
            if not fix:
                _record(conn, "audit", f"txn:{r['id']}", "unsure", label, reason, {"txn": r["id"], "problem": prob})
                flagged.add(r["id"])
                continue
            conn.execute(f"UPDATE transactions SET {', '.join(k + '=?' for k in fix)} WHERE id=?", (*fix.values(), tid))
            _record(conn, "audit", f"txn:{r['id']}", "fixed", label, reason,
                    {"txn": tid, "fix": fix, "old": {k: old.get(k) for k in fix}}, applied=True)
            flagged.add(r["id"])
            applied += 1
        for it in batch:                       # everything else in the batch was checked and is fine
            if int(it["id"]) not in flagged:
                _record(conn, "audit", f"txn:{it['id']}", "ok", "", "")
        conn.commit()
    return applied, "; ".join(dict.fromkeys(errs))


def job_merged(conn, settings, progress=None) -> tuple[int, str]:
    done = _decided(conn, "merged")
    pairs = [p for p in conn.execute(
        """SELECT d.id AS d, d.duplicate_of AS k FROM transactions d JOIN transactions k ON k.id = d.duplicate_of
           WHERE d.duplicate_of > 0 ORDER BY d.ts""") if f"{p['d']}-{p['k']}" not in done]
    if not pairs:
        return 0, ""
    get = lambda i: conn.execute("SELECT * FROM transactions WHERE id=?", (i,)).fetchone()  # noqa: E731
    batches = _batches([{"id": f"{p['d']}-{p['k']}", "kept": _row(get(p["k"])), "merged_into_it": _row(get(p["d"]))}
                        for p in pairs], LIMITS["batch"])
    results = _parallel(batches, lambda b: _ask({"items": b}, MERGED_SYSTEM, MERGED_SCHEMA, settings),
                        progress, "Claude is checking merged payments")
    applied, errs = 0, []
    for batch, (decisions, err) in zip(batches, results):
        if err:
            errs.append(err)
            continue
        got = {}
        for d in decisions:
            got[str(d["id"])] = d
        for it in batch:
            d = got.get(it["id"], {"verdict": "same", "reason": ""})
            dup, kept = (int(x) for x in it["id"].split("-"))
            r = get(dup)
            if d.get("verdict") == "different" and r and r["duplicate_of"] == kept:
                conn.execute("UPDATE transactions SET duplicate_of=NULL WHERE id=?", (dup,))
                _record(conn, "merged", it["id"], "different", f"{_txn_label(r)}: two payments, both counted now",
                        d.get("reason"), {"dropped": dup, "kept": kept}, applied=True)
                applied += 1
            else:
                _record(conn, "merged", it["id"], d.get("verdict") or "same", "", d.get("reason"))
        conn.commit()
    return applied, "; ".join(dict.fromkeys(errs))


RUNNERS = {"senders": job_senders, "unread": job_unread, "doubles": job_doubles, "payees": job_payees,
           "splits": job_splits, "categories": job_categories, "audit": job_audit, "merged": job_merged}


def run(conn, settings: dict, progress=None) -> dict:
    """Run all jobs. Never raises: problems come back in 'notes'."""
    result = {"at": datetime.now().isoformat(timespec="seconds"), "applied": 0, "notes": []}
    if not settings.get("ai_sort", True):
        return result
    if not review.claude_path(settings):
        result["notes"].append("Claude Code isn't installed where Margin can find it (claude_path in settings.json).")
    else:
        for i, job in enumerate(JOBS):
            if progress:
                progress("Claude is sorting", i, len(JOBS), force=True)
            try:
                fn = RUNNERS[job]
                n, err = fn(conn, settings, progress) if job in ("payees", "audit", "merged") else fn(conn, settings)
                result["applied"] += n
                if err:
                    result["notes"].append(f"{job}: {err}")
            except Exception as e:   # noqa: BLE001 - one job failing must not stop the others or the sync
                result["notes"].append(f"{job}: {e}")
            conn.commit()
        sync.recategorize(conn, settings)
    db.set_state(conn, "ai_last", json.dumps(result))
    conn.commit()
    return result


# --- show and undo ------------------------------------------------------------------------------
def listing(conn, per_job: int = 300) -> list[dict]:
    """Newest first, up to per_job of each job, so one busy job never pushes the others off the page."""
    out = []
    for job in RUNNERS:
        out += [{"id": r["id"], "at": r["at"], "job": r["job"], "verdict": r["verdict"], "label": r["label"],
                 "reason": r["reason"], "applied": bool(r["applied"]), "undone": bool(r["undone"])}
                for r in conn.execute("""SELECT * FROM ai_decisions WHERE job=? AND (applied=1 OR verdict IN ('bank','suggest'))
                                         ORDER BY at DESC, id DESC LIMIT ?""", (job, per_job))]
    return out


def bank_hints(conn) -> dict:
    return {r["key"]: r["reason"] for r in conn.execute(
        "SELECT key, reason FROM ai_decisions WHERE job='senders' AND verdict='bank' AND undone=0")}


def undo(conn, settings: dict, decision_id: int) -> bool:
    d = conn.execute("SELECT * FROM ai_decisions WHERE id=?", (decision_id,)).fetchone()
    if not d or d["undone"] or not d["applied"]:
        return False
    det = json.loads(d["detail"] or "{}")
    if d["job"] == "senders":
        settings["ignored_senders"] = [k for k in settings["ignored_senders"] if k != d["key"]]
        _save_settings(settings)
    elif d["job"] == "categories" and "match" in det:
        rules = json.loads(paths.RULES_PATH.read_text()) if paths.RULES_PATH.exists() else []
        for i, r in enumerate(rules):
            if r.get("by") == "claude" and r.get("match") == det["match"] and r.get("category") == det["category"]:
                rules.pop(i)
                break
        paths.RULES_PATH.write_text(json.dumps(rules, indent=2, ensure_ascii=False) + "\n")
        categorize.reload_rules()
    elif d["job"] == "categories" and "txn" in det:
        conn.execute("UPDATE transactions SET category_locked=0 WHERE id=? AND category_locked=2", (det["txn"],))
    elif d["job"] == "unread":
        if det.get("txn"):
            conn.execute("UPDATE transactions SET duplicate_of=NULL WHERE duplicate_of=?", (det["txn"],))
            conn.execute("DELETE FROM transactions WHERE id=?", (det["txn"],))
        rv = det.get("review")
        if rv:
            conn.execute("INSERT OR REPLACE INTO review(uid,source,sender,ts,reason,snippet) VALUES(?,?,?,?,?,?)",
                         (rv["uid"], rv["source"], rv["sender"], rv["ts"], rv["reason"], rv["snippet"]))
    elif d["job"] == "payees":
        conn.execute("UPDATE transactions SET counterparty=NULL WHERE id=?", (det["txn"],))
    elif d["job"] in ("audit", "splits"):
        old = det.get("old") or {}
        if old:
            conn.execute(f"UPDATE transactions SET {', '.join(k + '=?' for k in old)} WHERE id=?",
                         (*old.values(), det["txn"]))
    elif d["job"] == "merged":
        conn.execute("UPDATE transactions SET duplicate_of=? WHERE id=? AND duplicate_of IS NULL",
                     (det["kept"], det["dropped"]))
    elif d["job"] == "doubles":
        conn.execute("UPDATE transactions SET duplicate_of=NULL WHERE id=? AND duplicate_of=?",
                     (det["dropped"], det["kept"]))
    conn.execute("UPDATE ai_decisions SET undone=1 WHERE id=?", (decision_id,))
    sync.recategorize(conn, settings)
    conn.commit()
    return True


def accept(conn, settings: dict, decision_id: int) -> bool:
    """Apply a suggestion Claude left for you (it touched one of your own choices)."""
    d = conn.execute("SELECT * FROM ai_decisions WHERE id=?", (decision_id,)).fetchone()
    if not d or d["verdict"] != "suggest" or d["applied"]:
        return False
    det = json.loads(d["detail"] or "{}")
    fix = det.get("fix") or {}
    if fix:
        conn.execute(f"UPDATE transactions SET {', '.join(k + '=?' for k in fix)} WHERE id=?", (*fix.values(), det["txn"]))
    conn.execute("UPDATE ai_decisions SET applied=1, verdict='fixed' WHERE id=?", (decision_id,))
    conn.commit()
    return True


def dismiss(conn, decision_id: int) -> None:
    conn.execute("UPDATE ai_decisions SET verdict='dismissed' WHERE id=? AND verdict='suggest'", (decision_id,))
    conn.commit()
