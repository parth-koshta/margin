"""`margin config get|set`: read and change settings.json without anyone opening the file.

Personal settings (names, balances, holdings, facts about you) are never printed by default: `get` says whether
they're set and how many entries they have. `--show` prints them, for the person in their own Terminal (Claude
Code's data guard refuses --show). Secrets are refused outright: they belong in the Keychain, through the hidden
prompts of `margin setup-pan`, `setup-pran`, `setup-ppf` and `setup-gmail`.
"""
from __future__ import annotations

import json
import os
import re

from . import paths

# Settings that aren't in sync.DEFAULT_SETTINGS (no value until the person adds one), with their type.
OPTIONAL = {"lock_minutes": (int, float), "other_investments": list, "us_snapshot": dict, "ppf": list}
# Their values describe the person (names, money, facts): shown only as "set, N entries" unless --show.
PERSONAL = {"self_names", "about_me", "other_investments", "us_snapshot", "ppf", "old_hostnames", "ignored_senders",
            "project_categories"}
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
    if key == "currency":
        from . import money
        value = value.strip().upper()
        if value not in money.CURRENCIES:
            raise Refused(f"Margin doesn't know {value!r} yet. Known: {', '.join(sorted(money.CURRENCIES))} "
                          "(add one in margin/money.py).")
    return value


def check_key(key: str) -> None:
    if SECRET_KEY.search(key):
        raise Refused(secret_hint(key))
    if key not in known():
        raise Refused(f"There's no setting called {key!r}. Settings: {', '.join(sorted(known()))}.")


def set_value(key: str, text: str) -> object:
    check_key(key)
    for what, rx in SECRET_VALUE:
        if rx.search(text):
            raise Refused(f"That value looks like {what}. Secrets go in the Keychain through margin's setup-* "
                          "commands (hidden prompts), never in settings.json.")
    value = parse_value(key, text)
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


def describe(key: str, value, show: bool = False) -> str:
    """One setting as text: the value, or only its shape for personal settings."""
    if show or key not in PERSONAL:
        return json.dumps(value, ensure_ascii=False)
    if value in (None, [], {}, ""):
        return "not set"
    if isinstance(value, list):
        return f"set, {len(value)} entr{'y' if len(value) == 1 else 'ies'} (values hidden; --show prints them)"
    if isinstance(value, dict):
        return f"set, {len(value)} key{'s' if len(value) != 1 else ''} (values hidden; --show prints them)"
    return "set (value hidden; --show prints it)"


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
