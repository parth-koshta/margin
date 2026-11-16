"""`margin config get|set`: read and change settings.json without anyone opening the file.

Personal settings (names, balances, holdings, facts about you) are never printed by default: `get` says whether
they're set and how many entries they have. `--show` prints them, for the person in their own Terminal (Claude
Code's data guard refuses --show). Secrets are refused outright: they belong in the Keychain, through the hidden
prompts of `margin setup-pan`, `setup-pran`, `setup-ppf` and `setup-gmail`. So are settings that name a program
or an address (claude_path, anything with path, command, url...): the nightly sync would run that program, or send
prompts built from the person's money data to that address, so only the person sets those, by hand. Which AI
Margin asks (`ai`) is chosen with `margin setup-ai`, which checks the AI before saving it.
"""
from __future__ import annotations

import json
import os
import re

from . import paths

# Settings that aren't in sync.DEFAULT_SETTINGS (no value until the person adds one), with their type.
OPTIONAL = {"lock_minutes": (int, float), "other_investments": list, "us_snapshot": dict, "ppf": list,
            "indmoney": bool}
# Their values describe the person (names, money, facts): shown only as "set, N entries" unless --show.
PERSONAL = {"self_names", "about_me", "other_investments", "us_snapshot", "ppf", "old_hostnames", "ignored_senders",
            "project_categories", "trusted_senders", "extra_categories", "sms_backup_folder"}
# Settings naming a program the nightly sync runs (with prompts built from money data): set by hand only.
# Settings naming a program the nightly sync runs, or an address it sends to (prompts built from money data): set by
# hand only. (Not "host": old_hostnames only lists names the page answers to on this Mac.)
PROGRAM_KEY = re.compile(r"(?i)path|command|cmd|exe|binary|program|script|hook|shell|url|uri|endpoint|provider|"
                         r"server")
AI_KEYS = {"ai"}       # which AI is asked, and so where what it's handed goes: margin setup-ai
SECRET_KEY = re.compile(r"(?i)^(pan|pran)$|pan_|_pan$|pran|password|passwd|passcode|secret|token|ppf_statement|"
                        r"api_?key|private_?key|aadhaar|ssn")
SECRET_VALUE = [
    ("a PAN", re.compile(r"\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b")),
    ("an Aadhaar number", re.compile(r"\b[2-9]\d{3} ?\d{4} ?\d{4}\b")),
    ("an API key or token", re.compile(r"\b(?:sk-ant-[\w-]{10,}|sk-[A-Za-z0-9]{20,}|AIza[\w-]{30,}|ghp_\w{30,}|"
                                       r"GOCSPX-[\w-]{10,}|ya29\.[\w-]{20,}|1//0[\w-]{20,})")),
]
LIMITS = {"lock_minutes": (0, 24 * 60), "misc_below": (0, 10 ** 9), "backfill_days": (1, 3650)}


class Refused(Exception):
    pass


def defaults() -> dict:
    from .sync import DEFAULT_SETTINGS
    return dict(DEFAULT_SETTINGS)


def known() -> dict:
    """Every setting Margin reads -> its default (None for the optional ones)."""
    out = defaults()
    out.update({k: None for k in OPTIONAL})
    return out


def read() -> dict:
    """The settings as Margin sees them (defaults + settings.json), without writing anything."""
    s = defaults()
    try:
        s.update(json.loads(paths.SETTINGS_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return s


def secret_hint(key: str) -> str:
    k = key.lower()
    if "pran" in k:
        cmd = "margin setup-pran"
    elif "ppf" in k or "statement" in k:
        cmd = "margin setup-ppf"
    elif "pan" in k:
        cmd = "margin setup-pan"
    elif any(w in k for w in ("gmail", "google", "token", "client")):
        return ("Gmail's access is kept in the Keychain by `margin setup-gmail <client_secret.json>` (the person "
                "signs in to Google in their browser).")
    else:
        return "Secrets never go in settings.json; Margin keeps them in the macOS Keychain."
    return (f"That's a secret: it goes in the Keychain, never in settings.json. The person types it into the hidden "
            f"prompt of `{cmd}` in their own Terminal.")


def parse_value(key: str, text: str):
    """The command-line text as a setting value: JSON when it parses, a plain string otherwise; checked against
    the type Margin expects."""
    default = known().get(key)
    want = OPTIONAL.get(key) or type(default)
    if want is bool:
        low = text.strip().lower()
        if low in ("true", "yes", "on", "1"):
            return True
        if low in ("false", "no", "off", "0"):
            return False
        raise Refused(f"{key} is true or false.")
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    if want in (int, float, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Refused(f"{key} is a number.")
        lo, hi = LIMITS.get(key, (float("-inf"), float("inf")))
        if not lo <= value <= hi:
            raise Refused(f"{key} goes from {lo} to {hi}.")
        return int(value) if want is int and float(value).is_integer() else value
    if want is list:
        if isinstance(value, str):
            value = [value]                 # margin config set self_names "KAVYA MENON"
        if not isinstance(value, list):
            raise Refused(f'{key} is a list, e.g. \'["first", "second"]\'.')
        return value
    if want is dict:
        if not isinstance(value, dict):
            raise Refused(f"{key} is a JSON object, e.g. '{json.dumps(default or {})}'.")
        return value
    if not isinstance(value, str):
        value = text
    if key == "sms_backup_folder":
        value = value.strip()
        where = os.path.realpath(os.path.expanduser(value)) if value else ""
        if where and (where == "/" or where == os.path.realpath(os.path.expanduser("~"))
                      or os.path.realpath(paths.PROJECT_DIR).startswith(where.rstrip("/") + "/")):
            raise Refused("sms_backup_folder is the folder the backup app's files land in (e.g. a folder in Google "
                          "Drive), not a folder that holds everything else.")
    if key == "currency":
        from . import money
        value = value.strip().upper()
        if value not in money.CURRENCIES:
            raise Refused(f"Margin doesn't know {value!r} yet. Known: {', '.join(sorted(money.CURRENCIES))} "
                          "(add one in margin/money.py).")
    return value


def _program_keys(value) -> bool:
    if isinstance(value, dict):
        return any(PROGRAM_KEY.search(str(k)) or _program_keys(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_program_keys(v) for v in value)
    return False


PROGRAM_HINT = ("That setting names a program the nightly sync runs, with prompts built from the person's money "
                "data, so it can't be set from here. The person sets it by hand in settings.json if they need to; "
                "left empty, Margin finds Claude Code by itself.")


def check_key(key: str) -> None:
    if SECRET_KEY.search(key):
        raise Refused(secret_hint(key))
    if key not in known():
        raise Refused(f"There's no setting called {key!r}. Settings: {', '.join(sorted(known()))}.")


AI_HINT = ("Which AI Margin asks decides where what it's handed goes, so it's chosen with margin setup-ai, which "
           "checks the AI answers and can't read files before saving it: the person runs it in their own Terminal "
           "(or, with their choice, margin setup-ai --provider <name>). An API key goes into its hidden prompt.")


def set_value(key: str, text: str) -> object:
    if SECRET_KEY.search(key):
        raise Refused(secret_hint(key))
    if re.split(r"[.\[]", key)[0] in AI_KEYS:           # ai, ai.path, ai.base_url, ai[provider]...
        raise Refused(AI_HINT)
    if PROGRAM_KEY.search(key):
        raise Refused(PROGRAM_HINT)
    check_key(key)
    for what, rx in SECRET_VALUE:
        if rx.search(text):
            raise Refused(f"That value looks like {what}. Secrets go in the Keychain through margin's setup-* "
                          "commands (hidden prompts), never in settings.json.")
    value = parse_value(key, text)
    if _program_keys(value):
        raise Refused(PROGRAM_HINT)
    paths.ensure_data_dir()
    try:
        saved = json.loads(paths.SETTINGS_PATH.read_text())
    except OSError:
        saved = defaults()
    except ValueError:
        raise Refused("settings.json isn't valid JSON, so it wasn't changed. The person can fix it by hand or move "
                      "it aside (Margin writes a fresh one with the defaults).")
    saved[key] = value
    tmp = paths.SETTINGS_PATH.with_name(".settings.json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(saved, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, paths.SETTINGS_PATH)
    return value


def size(value) -> int:
    """How many entries a setting has: list items; for {"out": [...], "in": [...]} or {"holdings": {...}}, what's
    inside."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        inner = [v for v in value.values() if isinstance(v, (list, dict))]
        return sum(len(v) for v in inner) if inner else len(value)
    return 0 if value in (None, "") else 1


def describe(key: str, value, show: bool = False) -> str:
    """One setting as text: the value, or only how many entries it has for personal settings."""
    if show or key not in PERSONAL:
        return json.dumps(value, ensure_ascii=False)
    n = size(value)
    if not n:
        return "not set"
    return f"set, {n} entr{'y' if n == 1 else 'ies'} (values hidden; --show prints them)"


def main(a) -> int:
    try:
        if a.action == "set":
            if a.value is None:
                raise Refused("Usage: margin config set <key> <value>")
            value = set_value(a.key, a.value)
            print(f"{a.key} = {describe(a.key, value)}. It applies from the next sync (margin sync) and page load.")
            return 0
        settings = read()
        if a.key:
            check_key(a.key)
            print(describe(a.key, settings.get(a.key), a.show))
            return 0
        for key in sorted(known()):
            print(f"{key}: {describe(key, settings.get(key), a.show)}")
        return 0
    except Refused as e:
        print(e)
        return 1
