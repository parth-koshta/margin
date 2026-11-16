#!/usr/bin/python3
"""Margin's data guard: a Claude Code PreToolUse hook that keeps the person's money data out of the conversation.

It's an accident guard, not a sandbox. It reads each tool call before it runs and refuses the ones that would
read the data by mistake; it can't see what a program does once it runs (a script written to a file and run
later), and it doesn't stop anyone set on getting around it. The rules in AGENTS.md still apply in full.

Claude Code runs this before every tool call in this project (see .claude/settings.json) and hands it the call as
JSON on stdin. It refuses (exit code 2; the reason on stderr goes back to Claude) any call that would read:
- the data folder, ~/Library/Application Support/Margin (money.db, settings.json, rules.json, statements/,
  server.key, private-words.txt, logs), and $MARGIN_DATA when it points somewhere other than a temporary folder;
- backups of it in ~/Library/Application Support/Margin Backups/ (the verification-scripts folder stays open);
- the Messages database, ~/Library/Messages (bank texts);
- Margin's Keychain secrets (`security find-generic-password -w ... margin`);
- the real page, http://margin.localhost/ (127.0.0.1 / localhost / [::1] on port 80), with curl, wget, a script
  or a browser tool;
- Python that imports Margin's code (or runs `-m margin.<module>`) without MARGIN_DATA set to another folder:
  `MARGIN_DATA="$(mktemp -d)"`, as the tests do;
- the `margin` commands that print personal things or need the person's own hands, wherever they appear in a
  command: `margin tidy` (payees), `margin open --print-url` (the page's key), `margin config get --show`, and
  `margin setup-pan|pran|ppf` (hidden password prompts for the person's own Terminal).

Every string in every tool's input is checked, not just known tools: paths, URLs, the folder named in text, and
anything that looks like a command (Bash, a terminal, a monitor, a browser script) goes through the same command
checks. Text written into files inside this repository may name the folder (docs, code, tests); text written
anywhere else may not. Paths through the Data volume (/System/Volumes/Data/Users/<name>/...) count as the same paths.

Changes to the guard itself or to Claude Code's settings files (this repo's .claude/settings*.json and
.claude/hooks/, and ~/.claude/settings*.json) aren't refused but asked: Claude Code shows the person the change
and waits for their yes. If the guard itself fails, it refuses the call.

Everything else, including the other `margin` commands, the demo (~/Library/Caches/Margin Demo) and the tests,
goes through. The way in for Claude is the `margin` commands, which print counts and statuses only:
`margin doctor --json`, `margin config get|set`, `margin setup`, `margin update`.
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

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
GLOB = re.compile(r"[*?\[]")
NETWORK = {"curl", "wget", "nc", "ncat", "http", "https", "xh", "aria2c", "lynx", "w3m"}
RECURSIVE = {"find", "rg", "ag", "ack", "fd", "du", "tree", "tar", "zip", "ditto", "rsync", "mdfind"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
SEPARATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "(", ")", "{", "}", "then", "do", "else", "elif"}
# Fields that hold a command or script, in any tool: they get the same checks as a Bash command.
COMMAND_KEYS = {"command", "cmd", "commands", "script", "code", "text", "javascript", "js", "expression", "shell"}
URL_KEYS = {"url", "uri", "href"}
# Fields that hold text being written into a file (by Write, Edit, MultiEdit, NotebookEdit).
CONTENT_KEYS = {"content", "new_string", "old_string", "new_source", "edits"}
WRITERS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
WRITING_TOOL = re.compile(r"(?i)write|edit|move|rename|delete|remove|create|copy|upload|save|put|replace|patch")
WRITE_COMMANDS = {"rm", "mv", "cp", "tee", "truncate", "chmod", "chown", "ln", "touch", "dd", "install", "unlink",
                  "rsync", "ditto", "perl", "python", "python3", "ruby", "node", "awk", "ed", "ex", "patch"}
GIT_WRITES = {"checkout", "restore", "rm", "mv", "stash", "reset", "apply", "am", "cherry-pick", "revert", "clean"}
# The real page: margin.localhost (not demo.margin.localhost), or this Mac on port 80.
REAL_PAGE = re.compile(r"(?i)(?:https?://)?(?:(?<![\w.-])margin\.localhost|(?<![\w.-])127\.0\.0\.1|(?<![\w.-])localhost|"
                       r"\[::1\])(?::80)?(?=[/?#\"'\s)]|$)")
REAL_URL = re.compile(r"(?i)https?://(?:margin\.localhost|127\.0\.0\.1|localhost|\[::1\])(?::80)?(?=[/?#\"'\s)]|$)")
# The data folder spelled out in code or text: ".../Application Support/Margin" (not "Margin Backups", "Margin Demo")
DATA_TEXT = re.compile(r"(?i)Application(?:\\ | |%20)Support/Margin(?![\w.-]|(?:\\ | |%20)(?:Backups|Demo)\b)"
                       r"|Application Support[\"']\s*[,/]\s*[\"']Margin[\"']")
MESSAGES_TEXT = re.compile(r"(?i)Library/Messages(?![\w.-])")
PY_IMPORT = re.compile(r"\b(?:from\s+margin(?:\.\w+)*\s+import|import\s+(?:[\w.]+(?:\s+as\s+\w+)?\s*,\s*)*margin\b)"
                       r"|__import__\(\s*['\"]margin|import_module\(\s*['\"]margin")
PY_MODULE = re.compile(r"(?<!\S)-m\s*margin\.\w")
MARGIN_DATA_SET = re.compile(r"(?:^|[\s;&|(])(?:export\s+)?MARGIN_DATA=(\"[^\"]*\"|'[^']*'|\$\([^)]*\)|[^\s;&|)]+)")

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
AS_TEXT = ("\nIf you only meant the folder's name as text (a search pattern, a message, a commit message), write it "
           "differently, e.g. \"the data folder\" or DATA_DIR.")


# --- places --------------------------------------------------------------------------------------------------
def _real(p: str) -> str:
    try:
        r = os.path.realpath(p)
    except (OSError, ValueError):
        r = os.path.normpath(p)
    if r.lower().startswith("/system/volumes/data/"):     # the Data volume's firmlink: the same files
        r = r[len("/System/Volumes/Data"):]
    return r


def _temporary(p: str, home: str) -> bool:
    tmp = {_real(tempfile.gettempdir()), "/tmp", "/private/tmp", "/var/folders", "/private/var/folders",
           _real(os.path.join(home, "Library", "Caches"))}
    return any(_inside(p, t) for t in tmp)


def protected(home: str, env) -> list[tuple[str, str]]:
    """(folder, kind) pairs nothing may read. Backups are guarded one dated folder at a time, so the backup folder
    can still be listed and its verification-scripts folder used."""
    support = os.path.join(home, "Library", "Application Support")
    roots = [(_real(os.path.join(support, "Margin")), "data"),
             (_real(os.path.join(home, "Library", "Messages")), "messages")]
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


def guard_files(home: str) -> list[str]:
    """The guard and the settings files that switch it on or off: changing them needs the person's yes."""
    return [_real(os.path.join(REPO, ".claude", "hooks")),
            _real(os.path.join(REPO, ".claude", "settings.json")),
            _real(os.path.join(REPO, ".claude", "settings.local.json")),
            _real(os.path.join(home, ".claude", "settings.json")),
            _real(os.path.join(home, ".claude", "settings.local.json"))]


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


def expand(token: str, base: str, home: str, env) -> str:
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


# --- commands ------------------------------------------------------------------------------------------------
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


def _segments(words: list[str]) -> list[list[str]]:
    """Simple commands, split at ;, &&, ||, |, & and parentheses."""
    out, cur = [], []
    for w in words:
        if w in SEPARATORS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        out.append(cur)
    return out


def _pathish(tok: str) -> bool:
    return "/" in tok or tok.startswith(("~", "$HOME", "${HOME}", ".", "file:"))


def _recursive(words: list[str]) -> bool:
    for w in words:
        name = os.path.basename(w)
        if name in RECURSIVE or w == "--recursive" or re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", w):
            return True
    return False


def _pythons(words) -> bool:
    return any(os.path.basename(w).startswith("python") for w in words)


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


def _margin_data_safe(cmd: str, base: str, home: str, env, roots) -> bool:
    """Is MARGIN_DATA set, in this command, to a folder that isn't the person's data (a mktemp folder, the demo)?"""
    values = MARGIN_DATA_SET.findall(cmd)
    if not values:
        return False
    for v in values:
        v = v.strip("\"'")
        if v.startswith("$(mktemp"):
            continue
        if not v or "$(" in v or "`" in v:
            return False
        p = expand(v, base, home, env)
        if "$" in p or reaches(p, roots, recursive=False):
            return False
    return True


def _imports_margin(cmd: str, words: list[str], base: str, home: str, env, roots) -> str | None:
    """Python that loads Margin's code opens the data folder unless MARGIN_DATA points elsewhere."""
    if not _pythons(words):
        return None
    module = PY_MODULE.search(cmd)
    imp = PY_IMPORT.search(cmd)
    if not module and not imp:
        return None
    if _margin_data_safe(cmd, base, home, env, roots):
        return None
    tests_first = re.search(r"\bimport\s+tests\b", cmd)
    if imp and not module and tests_first and tests_first.start() <= imp.start():
        return None                  # tests/__init__.py points MARGIN_DATA at a temporary folder before margin loads
    return ("Margin's data guard blocked this: Python that loads Margin's code would open the person's real data "
            "folder. Run it on a temporary folder: prefix the command with MARGIN_DATA=\"$(mktemp -d)\" (as the "
            "tests do), or point MARGIN_DATA at the demo's folder.\n" + USE_INSTEAD)


def check_bash(cmd: str, cwd: str, home: str, env, depth: int = 0) -> str | None:
    roots = protected(home, env)
    for root, kind in roots:
        if _inside(cwd, root):
            return _block(kind, " (the shell is inside it)")
    if DATA_TEXT.search(cmd):
        return _block("data") + AS_TEXT
    custom = env.get("MARGIN_DATA")
    if custom and (_real(_expand_user(custom, home)), "data") in roots and custom in cmd:
        return _block("data")
    if MESSAGES_TEXT.search(cmd):
        return _block("messages") + AS_TEXT
    words = _words(cmd)
    base = cwd
    for seg in _segments(words):
        if seg[0] == "cd":
            if len(seg) > 1 and not seg[1].startswith("-"):
                base = expand(seg[1], base, home, env)
            continue
        recursive = _recursive(seg)
        above = any(_ancestor(base, r) for r, _ in roots)
        narrower = False
        for w in seg:
            candidates = [w] + ([w.split("=", 1)[1]] if "=" in w else [])
            for c in candidates:
                if not c or c.startswith("-") and "=" not in c:
                    continue
                if _pathish(c) or above:
                    p = expand(c, base, home, env)
                    kind = reaches(p, roots, recursive)
                    if kind:
                        return _block(kind)
                    if above and os.path.exists(p) and not any(_ancestor(p, r) for r, _ in roots):
                        narrower = True
        if recursive and above and not narrower:
            return _block("data", " (a search through the folders above it, from here)")
        for i, w in enumerate(seg):
            name = os.path.basename(w)
            if name in SHELLS and i + 2 < len(seg) and re.fullmatch(r"-[a-z]*c[a-z]*", seg[i + 1]) and depth < 3:
                why = check_bash(seg[i + 2], base, home, env, depth + 1)
                if why:
                    return why
            if name == "eval" and depth < 3:
                why = check_bash(" ".join(seg[i + 1:]), base, home, env, depth + 1)
                if why:
                    return why
            if name in ("margin", "margin-cli") or (w == "-m" and i + 1 < len(seg) and seg[i + 1] == "margin"
                                                    and _pythons(seg[:i])):
                why = check_margin(seg[i + (2 if w == "-m" else 1):])
                if why:
                    return "Margin's data guard: " + why
    why = _imports_margin(cmd, words, cwd, home, env, roots)
    if why:
        return why
    names = {os.path.basename(w) for w in words}
    if "security" in names and re.search(r"\bfind-(?:generic|internet)-password\b", cmd) \
            and re.search(r"(?<!\S)-\w*[wg]\w*\b", cmd) and re.search(r"(?i)margin", cmd):
        return ("Margin's data guard blocked this: it would print one of Margin's Keychain secrets (Gmail token, "
                "PAN, PRAN or a statement password). `margin doctor --json` shows which ones are saved.")
    if "security" in names and "dump-keychain" in names:
        return "Margin's data guard blocked this: dumping the Keychain would print Margin's secrets."
    fetches = bool(names & NETWORK) or (_pythons(words) and re.search(r"urllib|requests|http\.client", cmd))
    if fetches and REAL_PAGE.search(cmd):
        return _real_page()
    return None


def _real_page() -> str:
    return ("Margin's data guard blocked this: http://margin.localhost/ is the person's real page. Look at the "
            "demo instead (margin demo, http://demo.margin.localhost:8765/).")


# --- every tool ----------------------------------------------------------------------------------------------
def _strings(value, key: str = ""):
    """Every string in a tool's input, with the name of the field it's in (list items keep the list's name)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, str(k))
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v, key)
    elif isinstance(value, str):
        yield key, value


def _target(args: dict, cwd: str, home: str, env) -> str | None:
    p = args.get("file_path") or args.get("notebook_path")
    return expand(p, cwd, home, env) if isinstance(p, str) and p else None


def decide(event: dict, home: str | None = None, env=None) -> str | None:
    """Why this tool call must not run, or None to let it through."""
    home = home or os.path.expanduser("~")
    env = os.environ if env is None else env
    tool = str(event.get("tool_name") or "")
    args = event.get("tool_input") or {}
    cwd = str(event.get("cwd") or os.getcwd())
    roots = protected(home, env)
    for root, kind in roots:
        if _inside(_real(cwd), root):
            return _block(kind, " (the working folder is inside it)")
    if tool == "Bash" and isinstance(args, dict):
        why = check_bash(str(args.get("command") or ""), cwd, home, env)
        if why:
            return why
    if tool == "Grep" and isinstance(args, dict):     # searches inside every file below its folder
        kind = reaches(expand(str(args.get("path") or cwd), cwd, home, env), roots, recursive=True)
        if kind:
            return _block(kind)
    if tool == "Glob" and isinstance(args, dict):
        base = expand(str(args.get("path") or cwd), cwd, home, env)
        kind = reaches(expand(str(args.get("pattern") or "*"), base, home, env), roots, recursive=False)
        if kind:
            return _block(kind)
    target = _target(args, cwd, home, env) if isinstance(args, dict) else None
    in_repo = bool(target) and _inside(target, _real(REPO))
    for key, s in _strings(args):
        k = key.lower()
        if k in COMMAND_KEYS and not (tool == "Bash" and k == "command"):
            why = check_bash(s, cwd, home, env)
            if why:
                return why
            if REAL_URL.search(s):
                return _real_page()
        if tool in WRITERS and k in CONTENT_KEYS and in_repo:
            continue                 # text written into the repository's docs, code and tests
        if DATA_TEXT.search(s):
            return _block("data") + AS_TEXT
        if MESSAGES_TEXT.search(s):
            return _block("messages") + AS_TEXT
        one_line = s.strip()
        if one_line and "\n" not in one_line and (_pathish(one_line) or k in ("path", "file_path", "local_path")):
            kind = reaches(expand(one_line, cwd, home, env), roots, recursive=False)
            if kind:
                return _block(kind)
        if k in URL_KEYS and REAL_PAGE.match(one_line):
            return _real_page()
    return None


def _writes(words: list[str]) -> bool:
    for i, w in enumerate(words):
        name = os.path.basename(w)
        if w in (">", ">>", ">|") or name in WRITE_COMMANDS or name.startswith("python"):
            return True
        if name == "sed" and any(re.fullmatch(r"-\w*i\w*", x) or x.startswith("--in-place") for x in words[i + 1:]):
            return True
        if name == "git" and any(x in GIT_WRITES for x in words[i + 1:]):
            return True
    return False


def approval(event: dict, home: str | None = None, env=None) -> str | None:
    """Why the person should approve this call first (it changes the guard or what switches it on), or None."""
    home = home or os.path.expanduser("~")
    env = os.environ if env is None else env
    tool = str(event.get("tool_name") or "")
    args = event.get("tool_input") or {}
    cwd = str(event.get("cwd") or os.getcwd())
    guarded = guard_files(home)
    reason = ("This changes Margin's data guard or the Claude Code settings that turn it on "
              "(.claude/hooks/, .claude/settings*.json, ~/.claude/settings*.json). The person approves it.")

    def touches(p: str) -> bool:
        return any(_inside(p, g) for g in guarded)

    commands = []
    if tool == "Bash" and isinstance(args, dict):
        commands.append(str(args.get("command") or ""))
    for key, s in _strings(args):
        if key.lower() in COMMAND_KEYS and not (tool == "Bash" and key == "command"):
            commands.append(s)
        elif (tool in WRITERS or (tool != "Bash" and WRITING_TOOL.search(tool))) and "\n" not in s.strip() \
                and (_pathish(s.strip()) or key.lower() in ("file_path", "path", "notebook_path")) \
                and touches(expand(s.strip(), cwd, home, env)):
            return reason
    for cmd in commands:
        words = _words(cmd)
        if not _writes(words):
            continue
        if any(touches(expand(w, cwd, home, env)) for w in words if _pathish(w)) \
                or re.search(r"\.claude/(?:settings[\w.]*\.json|hooks\b)|data_guard\.py", cmd):
            return reason
    return None


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw or "{}")
        why = decide(event)
        ask = None if why else approval(event)
    except Exception as e:   # noqa: BLE001  a guard that breaks must not let calls through
        print(f"Margin's data guard couldn't check this call ({type(e).__name__}), so it refused it to be safe. "
              "Tell the person; the guard is .claude/hooks/data_guard.py.", file=sys.stderr)
        return 2
    if why:
        print(why, file=sys.stderr)
        return 2
    if ask:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                 "permissionDecisionReason": ask}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
