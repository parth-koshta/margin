#!/usr/bin/python3
"""Margin's data guard: a Claude Code PreToolUse hook that keeps the person's money data out of the conversation.

Claude Code runs this before every tool call in this project (see .claude/settings.json) and hands it the call as
JSON on stdin. It refuses (exit code 2; the reason on stderr goes back to Claude) any call that would read:
- the data folder, ~/Library/Application Support/Margin (money.db, settings.json, rules.json, statements/,
  server.key, private-words.txt, logs), and $MARGIN_DATA when it points somewhere other than a temporary folder;
- backups of it in ~/Library/Application Support/Margin Backups/ (the verification-scripts folder stays open);
- the Messages database, ~/Library/Messages (bank texts);
- Margin's Keychain secrets (`security find-generic-password -w ... margin`);
- the real page, http://margin.localhost/ (127.0.0.1 / localhost / [::1] on port 80), with curl, wget, a script
  or a browser tool;
- Python that imports Margin's code on the real folder (set MARGIN_DATA to a temporary folder, as the tests do);
- the few `margin` commands that print personal things or need the person's own hands: `margin tidy` (payees),
  `margin open --print-url` (the page's key), `margin config get --show`, and `margin setup-pan|pran|ppf`
  (hidden password prompts for the person's own Terminal).

Everything else, including every other `margin` command, the demo (~/Library/Caches/Margin Demo) and the tests,
goes through. The way in for Claude is the `margin` commands, which print counts and statuses only:
`margin doctor --json`, `margin config get|set`, `margin setup`, `margin update`.

This is a guardrail against reading the data by accident, not a sandbox: it reads commands, not intentions, so
code written to a file and run later isn't seen. AGENTS.md has the rules it backs up.
Standard library only; runs on the Mac's /usr/bin/python3 (3.9).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import sys
import tempfile

GLOB = re.compile(r"[*?\[]")
NETWORK = {"curl", "wget", "nc", "ncat", "http", "https", "xh", "aria2c", "lynx", "w3m"}
RECURSIVE = {"find", "rg", "ag", "ack", "fd", "du", "tree", "tar", "zip", "ditto", "rsync", "mdfind"}
PATH_KEYS = {"file_path", "path", "paths", "notebook_path", "directory", "dir", "cwd", "filename", "file", "root"}
URL_KEYS = {"url", "uri", "href"}
# The real page: margin.localhost (not demo.margin.localhost), or this Mac on port 80.
REAL_PAGE = re.compile(r"(?i)(?:https?://)?(?:(?<![\w.-])margin\.localhost|(?<![\w.-])127\.0\.0\.1|(?<![\w.-])localhost|"
                       r"\[::1\])(?::80)?(?=[/?#\"'\s)]|$)")
# The data folder spelled out in code or text: ".../Application Support/Margin" (not "Margin Backups", "Margin Demo")
DATA_TEXT = re.compile(r"(?i)Application(?:\\ | |%20)Support/Margin(?![\w.-]|(?:\\ | |%20)(?:Backups|Demo)\b)"
                       r"|Application Support[\"']\s*[,/]\s*[\"']Margin[\"']")
MESSAGES_TEXT = re.compile(r"(?i)Library/Messages(?![\w.-])")
PY_IMPORT = re.compile(r"\b(?:from\s+margin(?:\.\w+)*\s+import|import\s+margin\b)")

USE_INSTEAD = """Use a margin command instead; they print counts and statuses, never amounts, payees, names or numbers:
  margin doctor --json               health: checks, counts, last sync, what's set up
  margin config get [<key>]          settings (personal ones show only how many entries they have)
  margin config set <key> <value>    change a setting
  margin setup / margin update       set up or update Margin
Show things on the made-up demo instead (margin demo). Ask the person if they want to look at their own data
themselves."""
WHAT = {
    "data": "Margin's private data folder",
    "backup": "a backup of Margin's private data",
    "messages": "the Messages database (bank texts)",
}


# --- places --------------------------------------------------------------------------------------------------
def _real(p: str) -> str:
    try:
        return os.path.realpath(p)
    except (OSError, ValueError):
        return os.path.normpath(p)


def _temporary(p: str, home: str) -> bool:
    tmp = {_real(tempfile.gettempdir()), "/tmp", "/private/tmp", "/var/folders", "/private/var/folders",
           _real(os.path.join(home, "Library", "Caches"))}
    return any(_inside(p, t) for t in tmp)


def protected(home: str, env: dict) -> list[tuple[str, str]]:
    """(folder, kind) pairs nothing may read. Backups are guarded one dated folder at a time, so the backup folder
    can still be listed and its verification-scripts folder used."""
    support = os.path.join(home, "Library", "Application Support")
    roots = [(_real(os.path.join(support, "Margin")), "data"), (_real(os.path.join(home, "Library", "Messages")),
                                                                 "messages")]
    custom = env.get("MARGIN_DATA")
    if custom:
        p = _real(_expand_user(custom, home))
        if not _temporary(p, home):
            roots.append((p, "data"))
    backups = os.path.join(support, "Margin Backups")
    try:
        for name in os.listdir(backups):
            if name != "verification-scripts":
                roots.append((_real(os.path.join(backups, name)), "backup"))
    except OSError:
        pass
    return roots


def _inside(p: str, root: str) -> bool:
    p, root = p.lower(), root.lower()            # macOS volumes ignore case: ~/library/... is the same folder
    return p == root or p.startswith(root.rstrip("/") + "/")


def _ancestor(p: str, root: str) -> bool:
    p, root = p.lower(), root.lower()
    return p != root and (p == "/" or root.startswith(p.rstrip("/") + "/"))


def _expand_user(t: str, home: str) -> str:
    if t == "~" or t.startswith("~/"):
        return home + t[1:]
    return os.path.expanduser(t) if t.startswith("~") else t


def expand(token: str, base: str, home: str, env: dict) -> str:
    """A shell word as the path it names: $HOME / ${HOME} / ~ expanded, relative to `base`, symlinks resolved."""
    def var(m):
        name = m.group(1) or m.group(2)
        if name == "HOME":
            return home
        return env.get(name, m.group(0))
    t = re.sub(r"\$\{(\w+)\}|\$(\w+)", var, token)
    t = _expand_user(t, home)
    if t.startswith("file://"):
        t = t[len("file://"):]
    if not os.path.isabs(t):
        t = os.path.join(base, t)
    return _real(os.path.normpath(t))


def _below(pattern: list[str], rel: list[str], recursive: bool) -> bool:
    """Does a glob (as path components) reach `rel` (the rest of a protected folder's path) or below it?"""
    if not rel:
        return True
    if not pattern:
        return recursive
    if pattern[0] == "**":
        return True
    return fnmatch.fnmatchcase(rel[0].lower(), pattern[0].lower()) and _below(pattern[1:], rel[1:], recursive)


def reaches(path: str, roots: list[tuple[str, str]], recursive: bool) -> str | None:
    """The kind of protected folder an (expanded, maybe glob) path reads, if any. `recursive`: the command walks
    down from the path (find, grep -r, the Grep tool...), so a folder above the data folder counts too."""
    parts = path.split("/")
    i = next((k for k, part in enumerate(parts) if GLOB.search(part)), len(parts))
    static = "/".join(parts[:i]) or "/"
    pattern = parts[i:]
    for root, kind in roots:
        if _inside(static, root):
            return kind
        if _ancestor(static, root):
            rel = root[len(static.rstrip("/")) + 1:].split("/")
            if pattern and _below(pattern, rel, recursive):
                return kind
            if not pattern and recursive:
                return kind
    return None


# --- tools ---------------------------------------------------------------------------------------------------
def _block(kind: str, detail: str = "") -> str:
    what = WHAT.get(kind, kind)
    return (f"Margin's data guard blocked this: it would read {what}{detail}. The person's money data never goes "
            f"into this conversation (AGENTS.md, rule 1).\n{USE_INSTEAD}")


def _words(cmd: str) -> list[str]:
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:              # unbalanced quotes (often a heredoc with an apostrophe): plain words will do
        return cmd.split()


def _pathish(tok: str) -> bool:
    return "/" in tok or tok.startswith(("~", "$HOME", "${HOME}", ".", "file:"))


def _recursive(words: list[str]) -> bool:
    for w in words:
        name = os.path.basename(w)
        if name in RECURSIVE or w == "--recursive" or re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", w):
            return True
    return False


def _margin_call(words: list[str]) -> list[str] | None:
    """The arguments after `margin`, `./margin-cli` or `python3 -m margin`, if the command runs Margin."""
    for i, w in enumerate(words):
        if os.path.basename(w) in ("margin", "margin-cli") and (i == 0 or words[i - 1] in
                                                                  {"&&", "||", ";", "|", "(", "then", "do", "exec",
                                                                   "time", "nohup", "env", "sudo"}
                                                                  or re.fullmatch(r"\w+=.*", words[i - 1])):
            return words[i + 1:]
        if w == "-m" and i + 1 < len(words) and words[i + 1] == "margin" \
                and any(os.path.basename(x).startswith("python") for x in words[:i]):
            return words[i + 2:]
    return None


def check_margin(args: list[str]) -> str | None:
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub in ("setup-pan", "setup-pran", "setup-ppf"):
        return (f"`margin {sub}` asks for a statement password in a hidden prompt, which only the person may type. "
                f"Ask them to open their own Terminal window (not this conversation, not with !) and run "
                f"`margin {sub}` there. Afterwards `margin doctor --json` shows whether it's saved.")
    if sub == "tidy":
        return ("`margin tidy` shows the person's payees and senders one by one: it's for them, in their own "
                "Terminal, or Sort out on the page. `margin doctor --json` has the counts of what's left to sort.")
    if sub == "open" and "--print-url" in args:
        return "`margin open --print-url` prints the page's private key. Use `margin open` to open it for the person."
    if sub == "config" and "--show" in args:
        return ("`margin config get --show` prints the person's own values. Without --show it says what's set "
                "without the values; the person can run --show in their own Terminal.")
    return None


def check_bash(cmd: str, cwd: str, home: str, env: dict) -> str | None:
    roots = protected(home, env)
    for root, kind in roots:
        if _inside(cwd, root):
            return _block(kind, " (the shell is inside it)")
    if DATA_TEXT.search(cmd):
        return _block("data", "") + ("\nIf you only meant the folder's name as text (a search pattern, a commit "
                                     "message), write it differently, e.g. \"the data folder\" or DATA_DIR.")
    custom = env.get("MARGIN_DATA")
    if custom and (_real(_expand_user(custom, home)), "data") in roots and custom in cmd:
        return _block("data")
    if MESSAGES_TEXT.search(cmd):
        return _block("messages")
    words = _words(cmd)
    recursive = _recursive(words)
    base = cwd
    for i, w in enumerate(words):
        candidates = [w]
        if "=" in w:
            candidates.append(w.split("=", 1)[1])
        for c in candidates:
            if not c or c.startswith("-") and "=" not in c:
                continue
            if _pathish(c) or any(_ancestor(base, r) for r, _ in roots):
                kind = reaches(expand(c, base, home, env), roots, recursive)
                if kind:
                    return _block(kind)
        if w == "cd" and i + 1 < len(words) and not words[i + 1].startswith("-"):
            base = expand(words[i + 1], base, home, env)
    margin = _margin_call(words)
    if margin is not None:
        why = check_margin(margin)
        if why:
            return "Margin's data guard: " + why
    names = {os.path.basename(w) for w in words}
    if any(n.startswith("python") for n in names) and PY_IMPORT.search(cmd) \
            and not re.search(r"MARGIN_DATA|MARGIN_DEMO_DATA|\bimport tests\b", cmd):
        return ("Margin's data guard blocked this: Python that imports Margin's code would open the person's real "
                "data folder. Run it on a temporary folder instead: prefix the command with "
                "MARGIN_DATA=\"$(mktemp -d)\" (as the tests do), or use the demo's folder.\n" + USE_INSTEAD)
    if "security" in names and re.search(r"\bfind-(?:generic|internet)-password\b", cmd) \
            and re.search(r"(?<!\S)-\w*[wg]\w*\b", cmd) and re.search(r"(?i)margin", cmd):
        return ("Margin's data guard blocked this: it would print one of Margin's Keychain secrets (Gmail token, "
                "PAN, PRAN or a statement password). `margin doctor --json` shows which ones are saved.")
    if "security" in names and "dump-keychain" in names:
        return "Margin's data guard blocked this: dumping the Keychain would print Margin's secrets."
    fetches = bool(names & NETWORK) or (any(n.startswith("python") for n in names)
                                        and re.search(r"urllib|requests|http\.client", cmd))
    if fetches and REAL_PAGE.search(cmd):
        return ("Margin's data guard blocked this: http://margin.localhost/ is the person's real page. Look at the "
                "demo instead (margin demo, http://demo.margin.localhost:8765/).")
    return None


def _strings(value, keys: set, key: str = ""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, keys, k)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v, keys, key)
    elif isinstance(value, str) and key.lower() in keys:
        yield value


def decide(event: dict, home: str | None = None, env: dict | None = None) -> str | None:
    """Why this tool call must not run, or None to let it through."""
    home = home or os.path.expanduser("~")
    env = os.environ if env is None else env
    tool = event.get("tool_name") or ""
    args = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()
    if tool == "Bash":
        return check_bash(str(args.get("command") or ""), cwd, home, env)
    roots = protected(home, env)
    if tool == "Grep":                 # searches inside every file below its folder
        target = args.get("path") or cwd
        kind = reaches(expand(target, cwd, home, env), roots, recursive=True)
        if kind:
            return _block(kind)
        return None
    if tool == "Glob":
        base = expand(args.get("path") or cwd, cwd, home, env)
        pattern = str(args.get("pattern") or "*")
        kind = reaches(expand(pattern, base, home, env), roots, recursive=False)
        return _block(kind) if kind else None
    for p in _strings(args, PATH_KEYS):
        kind = reaches(expand(p, cwd, home, env), roots, recursive=False)
        if kind:
            return _block(kind)
    for u in _strings(args, URL_KEYS):
        if u.startswith("file:"):
            kind = reaches(expand(u, cwd, home, env), roots, recursive=False)
            if kind:
                return _block(kind)
        elif REAL_PAGE.match(u.strip()):
            return ("Margin's data guard blocked this: http://margin.localhost/ is the person's real page. Look at "
                    "the demo instead (margin demo, http://demo.margin.localhost:8765/).")
    return None


def main() -> int:
    raw = sys.stdin.read()
    try:
        why = decide(json.loads(raw or "{}"))
    except Exception:   # noqa: BLE001  never let a bug here open the data: refuse if the call mentions it
        why = _block("data") if DATA_TEXT.search(raw) or MESSAGES_TEXT.search(raw) else None
    if why:
        print(why, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
