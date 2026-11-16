"""The nightly job: pull new email + SMS, parse, dedupe, categorize, update investments, snapshot net worth."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta

from . import categorize, db, epf, fx, gmail, money, networth, parse, paths, review, sms

DEFAULT_SETTINGS = {
    "backfill_days": 365,
    "read_sms": True,        # set to false if your bank texts don't reach this Mac (e.g. Android phone)
    "trusted_senders": [],   # extra email domains / SMS IDs to treat as banks, e.g. "kotak811", "VM-MYBANK"
    "ignored_senders": [],   # senders you marked "not a bank" (hidden from Needs a look / Sort out)
    "self_names": [],        # your own name(s) as banks print them, to spot transfers to yourself
    "currency": "INR",       # your home currency (money.py): everything is shown and totalled in it
    "misc_below": 100,       # uncategorised spends below this much (home currency) go to Miscellaneous (0 = off)
    "ai_review": True,       # daily/weekly/monthly review written by Claude Code on this Mac (false = local checks only)
    "ai_model": "sonnet",    # model alias for the review
    "claude_path": "",       # leave empty to find the claude command automatically
    "about_me": [],          # facts about you that help Claude sort, e.g. "Money from friends is usually a split bill"
    "ai_sort": True,         # Claude sorts new senders, payees, unreadable alerts and double counts at each sync
    "fx_rates": {"USD": 94.0, "EUR": 108.0, "GBP": 124.0},   # home currency per unit, ONLY if the rate service is down
    "extra_categories": {"out": [], "in": []},   # your own categories, e.g. {"out": ["Pets"], "in": ["Freelance"]}
    "project_categories": [],  # big one-off projects (e.g. "Home renovation"): spending, shown apart on Spent
    "old_hostnames": [],       # earlier addresses of the page that should forward to http://margin.localhost/
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    saved = json.loads(paths.SETTINGS_PATH.read_text()) if paths.SETTINGS_PATH.exists() else {}
    s.update(saved)
    if set(s) != set(saved):          # first run, or a new setting was added: write all keys out
        paths.SETTINGS_PATH.write_text(json.dumps(s, indent=2) + "\n")
    return s


def _log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with open(paths.LOG_DIR / "sync.log", "a") as f:
        f.write(line + "\n")


class _Progress:
    """Writes {phase, done, total, started} to progress.json (counts only) so `margin sync` can draw a bar."""

    def __init__(self):
        self.started, self._last = time.time(), 0.0

    def __call__(self, phase: str, done: int = 0, total: int = 0, force: bool = False):
        now = time.time()
        if not force and now - self._last < 0.5 and done != total:
            return
        self._last = now
        tmp = paths.PROGRESS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"phase": phase, "done": done, "total": total, "started": self.started}))
        tmp.chmod(0o600)
        tmp.replace(paths.PROGRESS_PATH)

    def clear(self):
        paths.PROGRESS_PATH.unlink(missing_ok=True)


def _notify(title: str, msg: str) -> None:
    subprocess.run(["/usr/bin/osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                   capture_output=True)


def _account_label(bank: str, kind: str | None, last4: str | None) -> str:
    if not last4:
        return bank
    return f"{bank} {'card' if kind == 'card' else 'a/c'} ••{last4}"


def _is_self(counterparty: str | None, text: str, self_names: list[str]) -> bool:
    """Money to/from yourself. Judged by the payee only: your name also appears in greetings and in
    salary credits ("...-Salary ... YOUR NAME"), which aren't transfers."""
    party = re.sub(r"[^a-z ]", " ", (counterparty or "").lower())
    party = " " + re.sub(r"\s+", " ", party).strip() + " "
    return any(n.strip() and " " + re.sub(r"\s+", " ", n.lower().strip()) + " " in party for n in self_names)


def _snippet(text: str) -> str:
    """Review-list preview: account, card, phone and reference numbers (9+ digits) masked to the last 4.
    Shorter numbers stay, so amounts like 150000 remain readable."""
    t = parse.normalize(re.sub(r"<[^>]*>?", " ", text))[:400]
    return re.sub(r"\d{9,}", lambda m: "•" * (len(m.group()) - 4) + m.group()[-4:], t)


def ingest(conn: sqlite3.Connection, msg: dict, source: str, settings: dict) -> str:
    """Process one message. Returns 'txn', 'review', 'info' or 'skip'."""
    uid = f"{source}:{msg['id']}"
    conn.execute("INSERT OR IGNORE INTO seen(uid) VALUES(?)", (uid,))
    ts = sms.iso_local(msg["ts_epoch"])
    text, subject = msg["text"], msg.get("subject", "")
    bank = parse.bank_for_sender(msg["sender"], settings["trusted_senders"], text)
    full_text = f"{subject} {text}"
    home = money.home(settings)

    if not bank:
        if source == "sms" and epf.is_epfo(msg["sender"]):   # EPF balance texts, even when EPFO's sender is hidden
            return "info" if epf.store(conn, uid, ts, text) else "skip"
        if parse.sender_key(msg["sender"]) in settings["ignored_senders"]:
            return "skip"
        if parse.looks_financial(full_text, home):
            conn.execute("INSERT OR REPLACE INTO review VALUES(?,?,?,?,?,?)",
                         (uid, source, msg["sender"][:80], ts, "sender isn't a known bank (not counted)",
                          _snippet(full_text)))
            return "review"
        return "skip"

    # Each bank's alerts are read in its own currency (Indian banks rupees, US banks dollars). In another home
    # currency they're converted at the day's rate and keep the original.
    cur = parse.currency_for(bank, home)
    res = parse.parse_alert(text, subject, cur)
    rate, rate_src = (1.0, "") if home == cur else fx.rate(conn, cur, home, ts[:10], settings)
    if rate is None and (res.txns or res.balance is not None or res.statement_due is not None):
        conn.execute("INSERT OR REPLACE INTO review VALUES(?,?,?,?,?,?)",
                     (uid, source, msg["sender"][:80], ts, f"no {cur}->{home} rate yet", _snippet(full_text)))
        return "review"
    conv = (lambda x: x) if home == cur else (lambda x: None if x is None else round(x * rate, 2))
    res.statement_due, res.balance = conv(res.statement_due), conv(res.balance)
    if res.statement_due is not None and res.last4:
        conn.execute("INSERT OR REPLACE INTO card_dues VALUES(?,?,?,?)",
                     (_account_label(bank, "card", res.last4), ts, res.statement_due, res.due_date))
        return "info"
    if not res.txns:
        if res.balance is not None and res.last4 and not re.search(r"card", full_text, re.I):
            conn.execute("INSERT OR REPLACE INTO balances VALUES(?,?,?,?)",
                         (_account_label(bank, "account", res.last4), ts, res.balance, source))
            return "info"
        if parse.looks_financial(full_text, home):
            conn.execute("INSERT OR REPLACE INTO review VALUES(?,?,?,?,?,?)",
                         (uid, source, msg["sender"][:80], ts, res.reason or "couldn't read", _snippet(full_text)))
            return "review"
        return "skip"

    for n, t in enumerate(res.txns):
        note = ""
        if home != cur:
            sym = money.symbol(home).strip()
            note = f"[{cur} {t.amount:g} ≈ {sym}{t.amount * rate:,.2f} at {sym}{rate:g}/{cur}, {rate_src}] "
            t.amount, t.balance = round(t.amount * rate, 2), conv(t.balance)
        party = " ".join(x for x in (t.counterparty, t.vpa) if x)
        cat = _category(t.direction, party, full_text, t.amount, settings)
        account = _account_label(bank, t.kind, t.last4)
        cur = conn.execute(
            """INSERT OR IGNORE INTO transactions
               (uid,source,sender,bank,ts,amount,direction,account,last4,counterparty,vpa,category,raw)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{uid}:{n}", source, msg["sender"][:80], bank, ts, t.amount, t.direction, account, t.last4,
             t.counterparty, t.vpa, cat, (note + parse.normalize(full_text))[:1000]))
        if cur.rowcount:
            _mark_duplicate(conn, cur.lastrowid, source, ts, t.amount, t.direction, t.last4)
        if t.balance is not None and t.kind != "card" and t.last4:
            conn.execute("INSERT OR REPLACE INTO balances VALUES(?,?,?,?)", (account, ts, t.balance, source))
    return "txn"


REFUND_RE = re.compile(r"refund|revers", re.I)


def _mark_duplicate(conn, rowid, source, ts, amount, direction, last4) -> None:
    """The same spend often arrives as both an email and an SMS: keep the first, flag the second."""
    t = datetime.fromisoformat(ts)
    lo, hi = (t - timedelta(hours=36)).isoformat(), (t + timedelta(hours=36)).isoformat()
    # A downloaded statement (source 'file') has the posting date, up to a few days after the alert.
    flo, fhi = (t - timedelta(days=2)).date().isoformat(), (t + timedelta(days=5)).date().isoformat() + "T99"
    row = conn.execute(
        """SELECT id FROM transactions WHERE id != ? AND source != ? AND duplicate_of IS NULL
           AND direction = ? AND ABS(amount - ?) < 0.01
           AND (ts BETWEEN ? AND ? OR (source = 'file' AND ts BETWEEN ? AND ?))
           AND (last4 IS NULL OR ? IS NULL OR last4 = ?)
           AND id NOT IN (SELECT duplicate_of FROM transactions WHERE duplicate_of IS NOT NULL)
           ORDER BY ABS(julianday(ts) - julianday(?)) LIMIT 1""",
        (rowid, source, direction, amount, lo, hi, flo, fhi, last4, last4, ts)).fetchone()
    if not row and amount >= 5000 and last4:
        # The same bank sometimes sends two different alerts for one large credit/debit (e.g. SBI's
        # "credit for ... REFUND" and "An IT Refund amount ... has been credited"). Same account, same
        # amount, within 3 hours, different wording = one transaction. Identical wording stays separate.
        lo3, hi3 = (t - timedelta(hours=3)).isoformat(), (t + timedelta(hours=3)).isoformat()
        me = conn.execute("SELECT raw FROM transactions WHERE id=?", (rowid,)).fetchone()["raw"] or ""
        for cand in conn.execute(
                """SELECT id, raw FROM transactions WHERE id != ? AND duplicate_of IS NULL AND direction = ?
                   AND ABS(amount - ?) < 0.01 AND last4 = ? AND ts BETWEEN ? AND ? AND ts <= ?""",
                (rowid, direction, amount, last4, lo3, hi3, ts)).fetchall():
            if (cand["raw"] or "")[:40] != me[:40]:
                row = cand
                break
    if not row and direction == "in" and last4:
        # Card refunds are often reported twice, a day or two apart and worded differently (HDFC: "A refund was
        # processed…" / "Transaction Reversed!…" and later "Rs. X refunded by…"). Same card, same amount,
        # within 4 days, both about a refund/reversal = one refund; attach to the earlier one.
        me = conn.execute("SELECT raw FROM transactions WHERE id=?", (rowid,)).fetchone()["raw"] or ""
        if REFUND_RE.search(me):
            lo4 = (t - timedelta(days=4)).isoformat()
            for cand in conn.execute(
                    """SELECT id, raw FROM transactions WHERE id != ? AND duplicate_of IS NULL AND direction = 'in'
                       AND ABS(amount - ?) < 0.01 AND last4 = ? AND ts BETWEEN ? AND ? ORDER BY ts""",
                    (rowid, amount, last4, lo4, ts)).fetchall():
                if REFUND_RE.search(cand["raw"] or "") and (cand["raw"] or "")[:40] != me[:40]:
                    row = cand
                    break
    if row:
        conn.execute("UPDATE transactions SET duplicate_of=? WHERE id=?", (row["id"], rowid))


def _category(direction: str, party: str, text: str, amount: float, settings: dict) -> str:
    """Transfer to yourself, else your rules and the defaults; small spends no rule knows go to Miscellaneous."""
    if _is_self(party, text, settings["self_names"]):
        return "Transfer"
    cat = categorize.categorize(direction, party, text)
    if cat == "Other spending" and amount < float(settings.get("misc_below") or 0):
        return "Miscellaneous"
    return cat


def recategorize(conn: sqlite3.Connection, settings: dict) -> int:
    categorize.reload_rules()
    n = 0
    for r in conn.execute(
            "SELECT id, direction, amount, counterparty, vpa, raw FROM transactions WHERE category_locked=0").fetchall():
        party = " ".join(x for x in (r["counterparty"], r["vpa"]) if x)
        cat = _category(r["direction"], party, r["raw"] or "", r["amount"], settings)
        conn.execute("UPDATE transactions SET category=? WHERE id=?", (cat, r["id"]))
        n += 1
    return n


def reparse_parties(conn: sqlite3.Connection, settings: dict) -> int:
    """Re-read payees of stored transactions that have none or a junk one, with the current parser. Each alert is
    read in its bank's currency (parse.currency_for). Payments read from statement files (source 'file') aren't
    alerts and are left alone."""
    fixed = 0
    home = money.home(settings)
    cur = lambda r: parse.currency_for(r["bank"], home)   # noqa: E731
    rows = [r for r in conn.execute("SELECT id, raw, bank, amount, direction, counterparty, vpa, last4 FROM transactions "
                                    "WHERE source != 'file'")
            if not (r["counterparty"] or r["vpa"]) or (r["counterparty"] and parse.is_junk_party(r["counterparty"]))
            or (r["last4"] and r["counterparty"] == f"A/c ••{r['last4']}")]   # your own account isn't the payee
    # Alerts the parser now recognises as not-a-payment (e.g. "to be debited by" notices), and ones from
    # senders it no longer treats as a bank (fund houses, insurers), are removed.
    for r in conn.execute("SELECT id, raw, sender, bank FROM transactions WHERE source != 'file'").fetchall():
        raw = r["raw"] or ""
        us_by_claude = cur(r) == "USD" and raw.startswith("[read by Claude]")   # Claude already judged it a payment
        if ((not us_by_claude and parse.is_not_payment(parse.normalize(raw), cur(r)))
                or not parse.bank_for_sender(r["sender"] or "", settings["trusted_senders"], raw)):
            conn.execute("UPDATE transactions SET duplicate_of=NULL WHERE duplicate_of=?", (r["id"],))
            conn.execute("DELETE FROM transactions WHERE id=?", (r["id"],))
            fixed += 1
    for r in rows:
        if not conn.execute("SELECT 1 FROM transactions WHERE id=?", (r["id"],)).fetchone():
            continue
        res = parse.parse_alert(r["raw"] or "", "", cur(r))
        match = [t for t in res.txns if t.direction == r["direction"] and abs(t.amount - r["amount"]) < 0.01]
        new = (match[0].counterparty, match[0].vpa) if match else (None, None)
        if new != (r["counterparty"], r["vpa"]):
            conn.execute("UPDATE transactions SET counterparty=?, vpa=? WHERE id=?", (*new, r["id"]))
            fixed += 1
    # Card/account endings, with the current parser (an older one sometimes read the amount as the ending).
    for r in conn.execute("SELECT id, raw, bank, amount, direction, last4, account FROM transactions "
                          "WHERE source != 'file'").fetchall():
        match = [t for t in parse.parse_alert(r["raw"] or "", "", cur(r)).txns
                 if t.direction == r["direction"] and abs(t.amount - r["amount"]) < 0.01]
        if match:
            acct = _account_label(r["bank"], match[0].kind, match[0].last4)
            if (match[0].last4, acct) != (r["last4"], r["account"]):
                conn.execute("UPDATE transactions SET last4=?, account=?, duplicate_of=NULL WHERE id=?",
                             (match[0].last4, acct, r["id"]))
                conn.execute("UPDATE transactions SET duplicate_of=NULL WHERE duplicate_of=?", (r["id"],))
                fixed += 1
    for r in conn.execute("SELECT id, source, ts, amount, direction, last4 FROM transactions "
                          "WHERE duplicate_of IS NULL ORDER BY ts").fetchall():
        if conn.execute("SELECT 1 FROM transactions WHERE duplicate_of=?", (r["id"],)).fetchone():
            continue   # already the kept copy of something
        _mark_duplicate(conn, r["id"], r["source"], r["ts"], r["amount"], r["direction"], r["last4"])
    for r in conn.execute("SELECT id, counterparty FROM transactions WHERE COALESCE(counterparty,'')!='' "
                          "AND source != 'file'").fetchall():
        cleaned = parse._clean_party(r["counterparty"])
        if cleaned != r["counterparty"]:
            conn.execute("UPDATE transactions SET counterparty=? WHERE id=?", (cleaned, r["id"]))
            fixed += 1
    fixed += retry_review(conn, settings)
    recategorize(conn, settings)
    db.set_state(conn, "parser_version", str(parse.PARSER_VERSION))
    return fixed


def retry_review(conn: sqlite3.Connection, settings: dict) -> int:
    """Messages from banks read in dollars that are still on the review list (the reader couldn't read them, or
    didn't know the bank yet) are read again with the current reader. Returns how many it reads now. Rupee
    alerts are left alone: that reader's patterns didn't change."""
    home, n = money.home(settings), 0
    for r in conn.execute("SELECT * FROM review").fetchall():
        bank = parse.bank_for_sender(r["sender"] or "", settings["trusted_senders"], r["snippet"] or "")
        if not bank or parse.currency_for(bank, home) != "USD" or ":" not in r["uid"]:
            continue
        msg = {"id": r["uid"].split(":", 1)[1], "sender": r["sender"] or "", "subject": "", "text": r["snippet"] or "",
               "ts_epoch": datetime.fromisoformat(r["ts"]).timestamp()}
        conn.execute("DELETE FROM review WHERE uid=?", (r["uid"],))
        kind = ingest(conn, msg, r["source"], settings)
        if kind in ("txn", "info"):
            n += 1
        elif kind == "skip":           # not even worth a look now: keep it where it was
            conn.execute("INSERT OR REPLACE INTO review VALUES(?,?,?,?,?,?)", tuple(r))
    return n


def refresh_dashboard(conn: sqlite3.Connection) -> None:
    """After a change: record today's net worth (the page itself is built fresh on every request)."""
    networth.snapshot(conn)


def run(quiet_notify: bool = False) -> int:
    started = time.time()
    conn = db.connect()
    settings = load_settings()
    seen = {r["uid"] for r in conn.execute("SELECT uid FROM seen")}
    backfill_epoch = time.time() - settings["backfill_days"] * 86400
    counts = {"email": 0, "sms": 0, "txn": 0, "review": 0}
    problems = []
    progress = _Progress()
    progress("Finding emails", force=True)

    # Email
    if gmail.is_configured():
        last = float(db.get_state(conn, "email_after_epoch") or backfill_epoch)
        newest = last
        try:
            # Emails from senders you trusted in margin tidy, re-read by id (the search may not list them).
            refetch = json.loads(db.get_state(conn, "refetch_email_ids") or "[]")
            if refetch:
                rstats: dict = {}
                for m in gmail.fetch_ids(refetch, progress=lambda d, t: progress("Re-reading trusted", d, t),
                                         stats=rstats):
                    kind = ingest(conn, m, "email", settings)
                    counts["email"] += 1
                    counts[kind] = counts.get(kind, 0) + 1
                db.set_state(conn, "refetch_email_ids", json.dumps(rstats["skipped_ids"]))
                conn.commit()
                seen = {r["uid"] for r in conn.execute("SELECT uid FROM seen")}
            gstats: dict = {}
            for m in gmail.fetch_since(last - 86400, seen,   # 1 day overlap; 'seen' prevents repeats
                                       progress=lambda d, t: progress("Reading emails", d, t), stats=gstats,
                                       query=gmail.search_query(settings)):
                kind = ingest(conn, m, "email", settings)
                counts["email"] += 1
                counts[kind] = counts.get(kind, 0) + 1
                newest = max(newest, m["ts_epoch"])
                if counts["email"] % 100 == 0:
                    conn.commit()
            if gstats.get("skipped"):   # keep the cursor so the skipped ones are listed again next time
                problems.append(f"Gmail (partial): {gstats['skipped']} emails were rate-limited by Google; "
                                "the next sync fetches them.")
            else:
                db.set_state(conn, "email_after_epoch", str(newest))
        except gmail.AuthError as e:
            problems.append(f"Gmail: {e}")
    else:
        problems.append("Gmail not connected yet (margin setup-gmail)")

    # SMS
    progress("Reading bank texts", force=True)
    ok, why = sms.available() if settings["read_sms"] else (False, "")
    if not settings["read_sms"]:
        pass
    elif ok:
        cursor = db.get_state(conn, "sms_after_rowid")
        if not db.get_state(conn, "sms_short_codes"):
            # Once: texts from 5-6 digit short codes (how US banks text) used to be passed over. Outside India,
            # look through the backfill window again for them ('seen' skips everything already read).
            if money.home(settings) != "INR":
                cursor = None
            db.set_state(conn, "sms_short_codes", "1")
        after = int(cursor) if cursor is not None else sms.max_rowid_before(backfill_epoch)
        newest = after
        try:
            for m in sms.fetch_since(after):
                newest = max(newest, m["rowid"])
                if f"sms:{m['id']}" in seen:
                    continue
                kind = ingest(conn, m, "sms", settings)
                counts["sms"] += 1
                counts[kind] = counts.get(kind, 0) + 1
            db.set_state(conn, "sms_after_rowid", str(newest))
            if not db.get_state(conn, "epf_backfill"):   # once: every EPFO text already in Messages
                progress("Reading EPF texts", force=True)
                counts["epf"] = epf.backfill(conn, sms.fetch_since(0))
                db.set_state(conn, "epf_backfill", datetime.now().isoformat(timespec="seconds"))
        except sms.NoAccess as e:
            problems.append(f"SMS: {e}")
    else:
        problems.append(f"SMS: {why} Grant Full Disk Access (see margin doctor) or set read_sms to false in settings.json.")

    if db.get_state(conn, "parser_version") != str(parse.PARSER_VERSION):
        reparse_parties(conn, settings)
    try:   # Claude sorting must never break the sync
        from . import ai
        sorted_ = ai.run(conn, settings, progress)
        problems += [f"Claude sorting: {n}" for n in sorted_["notes"]]
    except Exception as e:   # noqa: BLE001
        problems.append(f"Claude sorting: {e}")
    try:   # investments must never break the sync either
        from . import invest
        problems += invest.update(conn, settings, progress)
    except Exception as e:   # noqa: BLE001
        problems.append(f"Investments: {e}")
    progress("Building dashboard", force=True)
    notes = networth.import_holdings(conn)
    problems += [f"Statements: {n}" for n in notes]
    networth.snapshot(conn)   # today's net worth
    status = {"at": datetime.now().isoformat(timespec="seconds"), "ok": not any(
        p.startswith(("Gmail:", "SMS: No permission")) for p in problems), "problems": problems, "counts": counts,
        "sms": "off" if not settings["read_sms"] else ("ok" if ok and not any(
            p.startswith("SMS:") for p in problems) else "no_access")}
    db.set_state(conn, "last_sync", json.dumps(status))
    conn.commit()
    reviews_written = []
    try:   # a review problem must never break the sync
        progress("Writing reviews", force=True)
        reviews_written = review.run_due(conn, settings)
    except Exception as e:   # noqa: BLE001
        problems.append(f"Review: {e}")
    conn.close()
    progress.clear()
    flagged = sum(1 for r in reviews_written for f in r["flags"] if f["severity"] in ("high", "medium"))
    if flagged and not quiet_notify:
        # no amounts or payees in notifications: they can show on the lock screen
        _notify("Margin", f"Your review found {flagged} thing{'s' if flagged != 1 else ''} to check. Open Margin.")

    # Counts only: never amounts or merchants in the log.
    _log(f"sync {'ok' if status['ok'] else 'with problems'} in {time.time() - started:.0f}s: "
         f"{counts['email']} emails, {counts['sms']} texts, {counts['txn']} new transactions, "
         f"{counts['review']} to review" + (f" | {'; '.join(problems)}" if problems else ""))
    if not status["ok"] and not quiet_notify:
        _notify("Margin", "Last night's sync had a problem. Run margin doctor.")
    return 0 if status["ok"] else 1
