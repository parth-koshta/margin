"""The leak guard: stops your personal details from reaching a commit, a push or a pull request.

Two layers:
- Your own details, gathered fresh from YOUR private data each time it runs and kept only in memory (never written
  anywhere): account and card endings, full account numbers, names of people you've paid, their UPI IDs, salary and
  balance amounts, big transfers, EPF member IDs, PPF/FD accounts, the ISINs you hold, statement file names, your
  own rules' keywords, your names, your PAN / PRAN / statement passwords (from the Keychain), your Mac's name and
  home folder, your git email, and anything you list in private-words.txt in the data folder (employer, family).
- General patterns that work on any machine (and in CI): PAN-shaped codes, Indian mobile numbers, personal email
  addresses, /Users/<name> paths, API keys and private keys, and files that must never be committed (statements,
  PDFs, databases, settings.json, rules.json, server.key, client_secret*.json). Images and videos are allowed only
  in docs/media/ (captured from `margin demo`).

It reports file:line and WHAT kind of detail matched, never the value itself, so its output is safe to show anywhere.
A line can opt out of the general patterns (not of your own details) with the marker "privacy: ok" and a reason.

`margin privacy-check` scans the project; the git hooks in .githooks/ run it on every commit, commit message and
push (`margin privacy-check --install-hooks` turns them on).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import paths

ALLOW_MARK = "privacy: ok"
NEVER_FILES = re.compile(r"(^|/)(statements/|.*\.pdf$|.*\.db$|.*\.db-\w+$|.*\.sqlite3?$|money\.db|settings\.json$|"
                         r"rules\.json$|server\.key$|server\.json$|client_secret.*\.json$|.*\.p12$|.*\.pem$|"
                         r".*\.mbox$|.*\.eml$|chat\.db|assets\.csv$|private-words\.txt$)", re.I)
MEDIA = re.compile(r"\.(png|jpe?g|gif|webp|heic|mov|mp4|m4v|webm)$", re.I)
TEXT_LIMIT = 2_000_000
COMMON = {"payout", "payouts", "cashback", "autopay", "netbanking", "upi", "emi", "sip", "neft", "imps", "rtgs",
          "nach", "ecs", "atm", "pos", "ecom", "billpay", "recharge", "fastag", "forex", "petrol", "salary"}
FAKE_ENDINGS = {"0000", "1111", "1234", "2222", "4321", "5555", "9876"}
BUSINESS = re.compile(r"\b(LTD|LIMITED|PVT|PRIVATE|LLP|INC|CORP|STORES?|MART|BANK|PAY|SERVICES|ENTERPRISES?|"
                      r"TRADERS|FOODS?|CAFE|HOTEL|MEDICALS?|PHARMA\w*|TECH\w*|SOLUTIONS|INDIA|INTERNATIONAL|CLUB|"
                      r"AGENCY|CENTRE|CENTER|REFUND|SALARY|INTEREST|CHARGES|NEFT|IMPS|UPI|ATM|CASH)\b", re.I)
CONSUMER_MAIL = r"(?:gmail|googlemail|yahoo|ymail|outlook|hotmail|live|icloud|me|mac|proton|protonmail|rediffmail|aol)"
GENERAL = [
    ("a PAN-shaped code", re.compile(r"\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b")),
    ("an Indian mobile number", re.compile(r"(?<![\w.])(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?![\d])")),
    ("a personal email address", re.compile(rf"\b[\w.+-]+@{CONSUMER_MAIL}\.[a-z.]{{2,6}}\b", re.I)),
    ("a home folder path", re.compile(r"/Users/(?!Shared\b|you\b|<|\$|USER\b|username\b|me\b|name\b)[\w.-]+")),
    ("an API key or token", re.compile(r"\b(?:sk-ant-[\w-]{10,}|sk-[A-Za-z0-9]{20,}|AIza[\w-]{30,}|ghp_\w{30,}|"
                                       r"github_pat_\w{20,}|GOCSPX-[\w-]{10,}|xox[abp]-[\w-]{10,}|ya29\.[\w-]{20,}|"
                                       r"1//0[\w-]{20,})")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a 12-digit Aadhaar-shaped number", re.compile(r"\b[2-9]\d{3} \d{4} \d{4}\b")),
]


# --- your own details ------------------------------------------------------------------------------------------
class Personal:
    """Patterns for the owner's details, built in memory from the private data folder."""

    def __init__(self):
        self.literal: dict[str, str] = {}      # term (lowercase) -> what it is
        self.regex: list[tuple[str, re.Pattern]] = []
        self.found_data = paths.DB_PATH.exists() or paths.SETTINGS_PATH.exists()

    def add(self, term: str | None, what: str, min_len: int = 4) -> None:
        t = re.sub(r"\s+", " ", (term or "").strip()).lower()
        if len(t) >= min_len and t not in self.literal:
            self.literal[t] = what

    def ending(self, digits: str, what: str) -> None:
        if digits and digits not in FAKE_ENDINGS and len(digits) >= 3:
            self.regex.append((what, re.compile(
                r"(?i)(?:(?<![a-z0-9])[x*•…]{2,}\s?|\bending(?: with| in)?:?\s*|\ba/?c(?:count)?(?: no\.?| number)?\s*:?\s*|"
                r"\bcard no\.?\s*)"
                rf"[x*•…]*0*{digits}(?!\d)")))

    def amount(self, value: float, what: str) -> None:
        whole = f"{abs(value):.0f}"
        if len(whole) >= 5 and len(set(whole)) > 2 and not whole.endswith("000"):   # 10,000+, not round numbers
            flexible = ",?".join(whole)
            self.regex.append((what, re.compile(rf"(?<![\d,.]){flexible}(?:\.\d\d)?(?![\d,])")))

    def matches(self, line: str) -> list[str]:
        low = line.lower()
        hits = [what for term, what in self.literal.items() if term in low and _bounded(low, term)]
        hits += [what for what, rx in self.regex if rx.search(line)]
        return hits


_DICT: set[str] = set()


def _plain_words(phrase: str) -> bool:
    """True when every word is an ordinary lowercase English word (macOS's word list), i.e. not a name."""
    if not _DICT:
        try:
            _DICT.update(w.strip() for w in open("/usr/share/dict/words") if w[:1].islower())
        except OSError:
            _DICT.add("")
    words = re.findall(r"[a-z]+", phrase)
    return bool(words) and all(w in _DICT or w.rstrip("s") in _DICT or w[:-2] in _DICT for w in words)


def _bounded(low: str, term: str) -> bool:
    for m in re.finditer(re.escape(term), low):
        a, b = m.start(), m.end()
        if (a == 0 or not low[a - 1].isalnum()) and (b == len(low) or not low[b].isalnum()):
            return True
    return False


def personal() -> Personal:
    p = Personal()
    home = Path.home()
    p.add(str(home), "your home folder path")
    p.add(home.name, "your Mac user name", 4)
    for key in ("ComputerName", "LocalHostName"):
        r = subprocess.run(["scutil", "--get", key], capture_output=True, text=True)
        if r.returncode == 0:
            p.add(r.stdout.strip(), "your Mac's name")
    for scope in ("--global", "--local"):
        r = subprocess.run(["git", "config", scope, "user.email"], capture_output=True, text=True)
        email = r.stdout.strip()
        if email and not email.endswith("users.noreply.github.com"):
            p.add(email, "your git email address")
    settings = {}
    if paths.SETTINGS_PATH.exists():
        try:
            settings = json.loads(paths.SETTINGS_PATH.read_text())
        except ValueError:
            pass
    for name in settings.get("self_names", []):
        p.add(name, "your name as banks print it")
        for part in name.split():
            p.add(part, "your name", 4)
    for host in settings.get("old_hostnames", []):
        p.add(host, "an old address of your Margin page")
    for acct in settings.get("ppf", []):
        for field in ("account", "name"):
            for d in re.findall(r"\d{4,}", str(acct.get(field, ""))):
                p.ending(d[-4:], "your PPF account")
    for acct in settings.get("ppf", []):
        for field in ("balance", "invested"):
            p.amount(float(acct.get(field) or 0), "your PPF balance")
    for h in (settings.get("us_snapshot") or {}).get("holdings", {}).values():
        _shares(p, h.get("shares"))
    words = paths.DATA_DIR / "private-words.txt"
    if words.exists():
        for line in words.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                p.add(line, "a word in your private-words.txt", 3)
    if paths.RULES_PATH.exists():
        from .categorize import DEFAULT_RULES
        defaults = {k.lower() for kws, *_ in DEFAULT_RULES for k in kws}
        try:
            for r in json.loads(paths.RULES_PATH.read_text()):
                for k in (r["match"] if isinstance(r["match"], list) else [r["match"]]):
                    k = k.lower().rstrip("*").strip()
                    if k not in defaults and k not in COMMON and not _plain_words(k):   # a name or place
                        p.add(k, "one of your own rules", 5)
        except (ValueError, KeyError, TypeError):
            pass
    if paths.STATEMENTS_DIR.exists():
        for f in paths.STATEMENTS_DIR.rglob("*"):
            for tok in re.findall(r"[A-Za-z0-9]{10,}", f.stem):
                if re.search(r"\d", tok):
                    p.add(tok, "an ID from your statement files")
    from . import keychain
    for account, what in (("pan", "your PAN"), ("pran", "your PRAN"), ("ppf_statement", "your PPF statement password")):
        try:
            secret = keychain.get_secret(account)
        except OSError:
            secret = None
        if secret:
            p.add(secret, what, 5)
    if paths.DB_PATH.exists():
        _from_db(p)
    return p


def _shares(p: Personal, shares) -> None:
    """Fractional share counts (like 1.234567) are as good as a fingerprint."""
    try:
        txt = f"{float(shares):.6f}".rstrip("0")
    except (TypeError, ValueError):
        return
    if len(txt.split(".")[-1]) >= 3:
        p.add(txt, "one of your US share counts", 5)


def _from_db(p: Personal) -> None:
    db = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    from .categorize import DEFAULT_RULES
    defaults = {k.lower().rstrip("*") for kws, *_ in DEFAULT_RULES for k in kws}
    labels = {r[0] for r in db.execute("SELECT DISTINCT account FROM transactions WHERE account IS NOT NULL")}
    labels |= {r[0] for r in db.execute("SELECT DISTINCT account FROM balances")} if "balances" in tables else set()
    labels |= {r[0] for r in db.execute("SELECT DISTINCT last4 FROM transactions WHERE last4 IS NOT NULL")}
    if "deposits" in tables:
        labels |= {r[0] for r in db.execute("SELECT DISTINCT account FROM deposits")}
    for label in labels:
        for d in re.findall(r"\d{3,}", str(label)):
            p.ending(d[-4:], "one of your account or card endings")
    for (raw,) in db.execute("SELECT raw FROM transactions WHERE raw LIKE '%a/c%' OR raw LIKE '%account%'"):
        for d in re.findall(r"(?<!\d)\d{9,18}(?!\d)", raw or ""):
            if not re.search(r"0{5}", d) and not d.startswith(("1800", "1860")):   # not toll-free numbers
                p.add(d, "a full account or reference number from your bank texts", 9)
    brands = {k for k in defaults if len(k) >= 4}           # "DECATHLON SPORTS" is a shop, not a person
    q = """SELECT DISTINCT counterparty, vpa FROM transactions WHERE counterparty IS NOT NULL
           AND category NOT IN ('Salary', 'Investments', 'Interest & dividends')"""
    for party, vpa in db.execute(q):          # people (family, friends, landlord) and small local places
        words = party.split()
        if 2 <= len(words) <= 4 and all(re.fullmatch(r"[A-Za-z.]{2,}", w) for w in words) \
                and not BUSINESS.search(party) and party.lower() not in defaults \
                and not _plain_words(party.lower()) and not any(_bounded(party.lower(), k) for k in brands):
            p.add(party, "the name of someone you paid or were paid by")
            if vpa:
                p.add(vpa, "a UPI ID of someone you paid", 6)
    for (vpa,) in db.execute("SELECT DISTINCT vpa FROM transactions WHERE vpa IS NOT NULL"):
        if re.search(r"\d{8,}", vpa.split("@")[0]):
            p.add(vpa, "a phone-number UPI ID from your texts", 6)
    for (amt,) in db.execute("SELECT amount FROM transactions WHERE category='Salary'"):
        p.amount(amt, "one of your salary amounts")
    for (party,) in db.execute("SELECT DISTINCT counterparty FROM transactions WHERE category='Salary'"):
        name = re.sub(r"(?i)\b(salary|sal|neft|cr|for|the|month|\d{4}\w*|[A-Z]{4}0\w{6})\b", " ", party or "")
        name = " ".join(w for w in name.split() if len(w) > 2)[:30]
        if len(name) >= 6:
            p.add(" ".join(name.split()[:2]), "your employer's name")
    for (amt,) in db.execute("SELECT amount FROM transactions WHERE amount >= 50000 AND duplicate_of IS NULL"):
        p.amount(amt, "one of your large payments")
    if "balances" in tables:
        for (bal,) in db.execute("SELECT balance FROM balances"):
            p.amount(bal or 0, "one of your bank balances")
    if "epf" in tables:
        for member, bal, contrib in db.execute("SELECT member, balance, contribution FROM epf"):
            for part in re.findall(r"[A-Z]{5}|\d{4,}", member or ""):
                if part.isalpha():
                    p.add(part, "your EPF office code", 5)
                else:
                    p.ending(part[-4:], "your EPF member ID")
            p.amount(bal or 0, "one of your EPF balances")
            p.amount(contrib or 0, "one of your EPF contributions")
    if "inv_positions" in tables:
        for key, extra in db.execute("SELECT DISTINCT key, extra FROM inv_positions"):
            isin = (key or "").replace("NAV:", "")
            if re.fullmatch(r"IN[EF][0-9A-Z]{9}", isin):
                p.add(isin, "the ISIN of something you hold", 12)
            for folio in re.findall(r"\d{6,}/\d+|\d{8,}", extra or ""):
                p.add(folio, "one of your folio numbers", 6)
        for (cost,) in db.execute("SELECT DISTINCT cost FROM inv_positions WHERE cost >= 10000"):
            p.amount(cost, "the cost of one of your holdings")
    if "us_orders" in tables:
        cols = {r[1] for r in db.execute("PRAGMA table_info(us_orders)")}
        if "shares" in cols:
            for (shares,) in db.execute("SELECT shares FROM us_orders"):
                _shares(p, shares)
    if "deposits" in tables:
        for principal, maturity in db.execute("SELECT principal, maturity_amount FROM deposits"):
            p.amount(principal or 0, "your deposit amount")
            p.amount(maturity or 0, "your deposit's maturity amount")
    db.close()


# --- scanning -------------------------------------------------------------------------------------------------
def _git(*args: str, input: str | None = None) -> str:
    r = subprocess.run(["git", "-C", str(paths.PROJECT_DIR), *args], capture_output=True, text=True, input=input)
    return r.stdout if r.returncode == 0 else ""


def _files_worktree() -> list[tuple[str, bytes]]:
    root = paths.PROJECT_DIR
    if _git("rev-parse", "--is-inside-work-tree").strip() == "true":
        names = [n for n in _git("ls-files", "-co", "--exclude-standard", "-z").split("\0") if n]
    else:
        names = [str(f.relative_to(root)) for f in root.rglob("*")
                 if f.is_file() and not {"__pycache__", ".git"} & set(f.relative_to(root).parts)]
    out = []
    for n in names:
        try:
            out.append((n, (root / n).read_bytes()))
        except OSError:
            pass
    return out


def _files_staged() -> list[tuple[str, bytes]]:
    names = [n for n in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if n]
    out = []
    for n in names:
        r = subprocess.run(["git", "-C", str(paths.PROJECT_DIR), "show", f":{n}"], capture_output=True)
        out.append((n, r.stdout))
    return out


def _files_at(commit: str) -> list[tuple[str, bytes]]:
    names = [n for n in _git("ls-tree", "-r", "--name-only", "-z", commit).split("\0") if n]
    out = []
    for n in names:
        r = subprocess.run(["git", "-C", str(paths.PROJECT_DIR), "show", f"{commit}:{n}"], capture_output=True)
        out.append((n, r.stdout))
    return out


def scan(files: list[tuple[str, bytes]], mine: Personal | None) -> list[str]:
    problems = []
    for name, blob in files:
        if NEVER_FILES.search(name):
            problems.append(f"{name}: this kind of file must never be committed (private data)")
            continue
        if MEDIA.search(name):
            if not name.startswith("docs/media/"):
                problems.append(f"{name}: images and videos go in docs/media/ and must come from `margin demo`")
            continue
        if b"\0" in blob[:4096] or len(blob) > TEXT_LIMIT:
            problems.append(f"{name}: binary or very large file; check it by hand and keep data out of the repo")
            continue
        problems += scan_text(name, blob.decode("utf-8", "replace"), mine)
    return problems


def scan_text(name: str, text: str, mine: Personal | None) -> list[str]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        kinds = []
        if mine:
            kinds += mine.matches(line)
        if ALLOW_MARK not in line:
            kinds += [what for what, rx in GENERAL if any(not _obviously_fake(m.group()) for m in rx.finditer(line))]
        for what in dict.fromkeys(kinds):
            out.append(f"{name}:{i}: {what}")
    return out


def _obviously_fake(s: str) -> bool:
    """9999999999, 9000000000, 9812345678-style numbers are clearly made up; real ones aren't flagged away."""
    d = re.sub(r"\D", "", s)
    return bool(d) and (len(set(d)) <= 2 or "00000" in d or "12345" in d or "54321" in d)


def check_commits(rev_range: str, mine: Personal | None) -> list[str]:
    out = []
    for line in _git("log", "--format=%H%x00%ae%x00%ce%x00%B%x01", rev_range).split("\x01"):
        if not line.strip():
            continue
        sha, author, committer, body = (line.strip("\n").split("\0") + ["", "", "", ""])[:4]
        for who, email in (("author", author), ("committer", committer)):
            if email and not email.endswith("users.noreply.github.com"):
                out.append(f"commit {sha[:9]}: {who} email isn't a GitHub noreply address (it would be public)")
        out += [p.replace("commit message", f"commit {sha[:9]} message", 1)
                for p in scan_text("commit message", body, mine)]
    return out


def main(a) -> int:
    if a.install_hooks:
        if _git("rev-parse", "--is-inside-work-tree").strip() != "true":
            print("Not a git repository yet: run `git init` first, then this again.")
            return 1
        _git("config", "core.hooksPath", ".githooks")
        print("Leak guard on: every commit, commit message and push in this repository is checked.")
        return 0
    mine = personal()
    note = "" if mine.found_data else " (no private Margin data on this machine: general patterns only)"
    if a.commit_msg:
        problems = scan_text("commit message", Path(a.commit_msg).read_text(), mine)
    elif a.push:
        problems = []
        for line in sys.stdin.read().splitlines():
            parts = line.split()
            if len(parts) != 4 or set(parts[1]) == {"0"}:
                continue                                         # a branch being deleted
            local, remote = parts[1], parts[3]
            rng = local if set(remote) == {"0"} else f"{remote}..{local}"
            problems += [f"[{parts[0]}] {p}" for p in scan(_files_at(local), mine) + check_commits(rng, mine)]
    elif a.staged:
        problems = scan(_files_staged(), mine)
    else:
        problems = scan(_files_worktree(), mine)
    if problems:
        print(f"Leak guard: {len(problems)} problem(s){note}. Values are not shown, only where and what:")
        for p in problems:
            print("  " + p)
        print("Replace each with made-up data (see AGENTS.md, 'Made-up data'). Never bypass this with --no-verify.")
        return 1
    print(f"Leak guard: clean{note}.")
    return 0
