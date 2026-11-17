"""Margin's versions. A release is a git tag vX.Y.Z on GitHub's main, made by the maintainer (AGENTS.md "Releasing").

`margin update` installs the newest release, not whatever was pushed last (install.py), so a change reaches people
only once it's released. The nightly sync checks whether a newer release is out (check), and the page and
`margin doctor` say so. The check is `git fetch` from GitHub, the same as `margin update` does: GitHub sees this
Mac's IP address and nothing about the person or their money. `margin config set check_updates false` turns it off.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import time

from . import db, paths

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
STATE_KEY = "update_available"      # in the state table: {"version", "changes", "from", "checked"} or null


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")          # never wait for a password prompt
    try:
        return subprocess.run(["git", "-C", str(paths.PROJECT_DIR), *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def is_checkout() -> bool:
    return _git("rev-parse", "--is-inside-work-tree").stdout.strip() == "true"


def upstream() -> str:
    """The GitHub branch this copy follows (origin/main), or '' if none."""
    return _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip()


def head() -> str:
    return _git("rev-parse", "HEAD").stdout.strip()


def fetch() -> bool:
    """GitHub's latest commits and release tags. A tag moved on GitHub is taken as GitHub has it."""
    return _git("fetch", "--quiet", "--tags", "--force").returncode == 0


def latest(up: str) -> tuple[str, str] | None:
    """The newest release on `up`'s history, as (tag, commit), or None when it has no release."""
    best = None
    for tag in _git("tag", "--list", "v*", "--merged", up).stdout.split():
        m = TAG.match(tag)
        if m and (best is None or tuple(map(int, m.groups())) > best[0]):
            best = (tuple(map(int, m.groups())), tag)
    if not best:
        return None
    commit = _git("rev-parse", "--verify", "--quiet", f"{best[1]}^{{commit}}").stdout.strip()
    return (best[1], commit) if commit else None


def version() -> str:
    """This copy's version: 'v0.2.0', 'v0.2.0 + 3 changes' after it, or the commit when it has no release."""
    found = _git("describe", "--tags", "--long", "--match", "v[0-9]*.[0-9]*.[0-9]*", "HEAD").stdout.strip()
    m = re.match(r"^(v\d+\.\d+\.\d+)-(\d+)-g[0-9a-f]+$", found)
    if m:
        tag, n = m.group(1), int(m.group(2))
        return tag if n == 0 else f"{tag} + {n} change{'s' if n != 1 else ''}"
    return _git("rev-parse", "--short", "HEAD").stdout.strip() or "unknown"


def newer() -> dict | None:
    """The newest release when this copy can update to it (it's ahead, by fast-forward): {"version", "changes"}.
    Uses what was last fetched; nothing goes to GitHub."""
    up = upstream()
    found = latest(up) if up else None
    if not found:
        return None
    tag, commit = found
    here = head()
    if not here or commit == here or _git("merge-base", "--is-ancestor", here, commit).returncode != 0:
        return None          # already there, ahead of it (a developer's copy), or with changes of its own
    n = _git("rev-list", "--count", f"{here}..{commit}").stdout.strip()
    return {"version": tag, "changes": int(n) if n.isdigit() else 0}


def check(conn, settings: dict) -> dict | None:
    """The nightly check (sync.py): fetch, then remember whether a newer release is out. Offline, it keeps what it
    knew. Turned off, it forgets it."""
    if not settings.get("check_updates", True) or not is_checkout():
        db.set_state(conn, STATE_KEY, "null")
        return None
    if not fetch():
        return saved(conn)
    found = newer()
    db.set_state(conn, STATE_KEY, json.dumps(
        {**found, "from": head(), "checked": time.strftime("%Y-%m-%dT%H:%M:%S")} if found else None))
    return found


def saved(conn) -> dict | None:
    try:
        found = json.loads(db.get_state(conn, STATE_KEY) or "null")
    except ValueError:
        return None
    return found if isinstance(found, dict) and found.get("version") else None


@functools.lru_cache(maxsize=1)
def _running() -> str:
    """The commit this process's code came from. The page service is restarted by every update."""
    return head()


def notice(conn) -> dict | None:
    """What the page shows: the newer release found by the last check, while this copy is still where it was then
    (after `margin update` it isn't, until the next check)."""
    found = saved(conn)
    if not found or found.get("from") != _running():
        return None
    return {"version": found["version"], "changes": found.get("changes", 0)}
