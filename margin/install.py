"""`margin setup` and `margin update`.

setup does the steps of AGENTS.md "Setup" that need no one's hands, and is safe to run again: every step checks
first and only does what's missing. It stops at the steps only the person may do (the Google sign-in, iPhone text
forwarding and Full Disk Access, statement passwords) and says exactly what to do. In the person's own Terminal it
waits for them and checks again; run by Claude Code (no terminal) it lists them and exits with status 3.

update gets the latest Margin by fast-forward only, runs the tests on a temporary folder (and goes back if they
fail), rebuilds the Swift helpers and the app, and reloads the launchd agents. It never reads or changes the
person's data: money.db, settings.json, rules.json and statements stay exactly as they are.

Nothing here prints amounts, payees, names or numbers: only what was done and what's left.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import platform
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from . import config, health, paths

BIN_DIR = Path.home() / ".local" / "bin"
SYMBOL = {"ok": "✓", "done": "✓", "would": "·", "skipped": "·", "optional": "·", "failed": "✗", "todo": "→"}
EXIT_WAITING = 3

GMAIL_HOW = """Connect Gmail, read-only (about 10 minutes in your browser, signed in to Google Cloud with your personal Gmail):
  1. https://console.cloud.google.com/projectcreate : create a project named margin and select it.
  2. https://console.cloud.google.com/apis/library/gmail.googleapis.com : Enable.
  3. https://console.cloud.google.com/auth/overview > Get started: app name margin, support email your Gmail,
     Audience External, contact email your Gmail, agree > Create.
  4. Branding > App domain: any page you own (your GitHub profile is fine) as home page and privacy policy link;
     add its domain under Authorised domains > Save. (Needed before Publish; nobody else sees it.)
  5. Audience > Publish app > Confirm. (Left in Testing, Google expires the access every 7 days.)
  6. Clients > Create client > Desktop app, name margin-mac > Download JSON.
  7. margin setup-gmail ~/Downloads/client_secret_*.json
     Your browser opens: pick your Gmail, click Advanced > Go to margin (unsafe) (it's your own app), allow
     read-only access. Terminal says "Connected read-only to ...".
  8. Delete the downloaded client_secret file: its contents are in your Keychain now.
  Stuck? Publish greyed out: step 4 isn't finished. "Access blocked": step 5 was skipped."""

SMS_HOW = """Bank texts (iPhone; 2 minutes):
  1. iPhone: Settings > Apps > Messages > Text Message Forwarding > turn on this Mac.
  2. Mac: System Settings > Privacy & Security > Full Disk Access > + > press Cmd-Shift-G, paste
       {python}
     > Open > switch it on. (Only that Python gets it, so the nightly sync can read your Messages.)
  3. Optional, for older texts: Messages in iCloud, on the iPhone and on the Mac.
  No iPhone, or your bank texts don't reach this Mac: margin config set read_sms false
  The next sync checks it (margin sync, then margin doctor)."""

AI_HOW = """Choose the AI that sorts what Margin's rules can't and writes your reviews, in your own Terminal:
    margin setup-ai
  It lists the AIs on this Mac (Claude Code, Codex, Gemini CLI, Ollama) and API services (Grok, OpenAI, ...; the
  key is typed hidden), says where each sends what it's handed, asks one made-up question and checks it can't read
  your files before saving. Optional: without an AI, Margin's own rules and checks still run."""

STATEMENTS_HOW = """Statement passwords, only the ones that apply. Each goes into a hidden prompt in your own Terminal
(not into a chat) and is kept in your Keychain:
  - Indian stocks and mutual funds (CDSL statement, locked with your PAN): margin setup-pan
  - NPS (locked with your PRAN): margin setup-pran
  - SBI PPF statement: margin setup-ppf
  - EPF: download the year's passbook PDF from the EPFO member portal into
      {statements}
  - Other brokers: a holdings CSV export in that same folder is read each night."""

AFTER = """Then:
  - Open Margin from Spotlight once in each browser you use (that hands the browser its key), and bookmark
    http://margin.localhost/ . Touch ID asks on first open.
  - Sort out on the page: pick categories for the biggest payees and mark unknown senders (15 minutes; your
    choices).
  - margin doctor: every line should be ✓ after the first sync."""


class Step:
    def __init__(self, id_: str, who: str, status: str, text: str, how: str = "", notes: list | None = None):
        self.id, self.who, self.status, self.text, self.how = id_, who, status, text, how
        self.notes = notes or []

    def as_dict(self) -> dict:
        d = {"id": self.id, "who": self.who, "status": self.status, "text": self.text}
        if self.how:
            d["how"] = self.how
        if self.notes:
            d["notes"] = self.notes
        return d


def _run(*cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True, **kw)


def _quietly(fn: Callable[[], int]) -> tuple[int, list[str]]:
    """Run one of margin's own commands, keeping what it prints to show under the step."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = fn()
        except Exception as e:   # noqa: BLE001  one failed step mustn't stop the others
            print(f"{type(e).__name__}: {e}")
            rc = 1
    return rc, [line for line in buf.getvalue().splitlines() if line.strip()]


def _plist(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def _ours(plist: dict) -> bool:
    """Does a launchd agent run this copy of Margin with this Python?"""
    args = plist.get("ProgramArguments") or [""]
    return args[0] == health.python() and plist.get("WorkingDirectory") == str(paths.PROJECT_DIR)


# --- setup steps ---------------------------------------------------------------------------------------------
def step_mac() -> Step:
    found, missing = [], []
    ver = _run("sw_vers", "-productVersion").stdout.strip()
    found.append(f"macOS {ver or platform.mac_ver()[0]}")
    if _run("xcode-select", "-p").returncode == 0:
        found.append("command line tools")
    else:
        missing.append("the Xcode command line tools")
    if sys.version_info >= (3, 9):
        found.append(f"Python {platform.python_version()}")
    else:
        missing.append("Python 3.9 or newer")
    if os.path.exists("/usr/bin/swiftc") and _run("/usr/bin/swiftc", "--version").returncode == 0:
        found.append("Swift")
    else:
        missing.append("Swift (part of the command line tools)")
    notes = []
    if missing:
        return Step("mac", "you", "todo", "This Mac is missing " + " and ".join(missing),
                    "Run  xcode-select --install  in Terminal, click Install, wait for it to finish, then run "
                    "margin setup again.", notes)
    return Step("mac", "margin", "ok", "This Mac: " + ", ".join(found), notes=notes)


def _rc_file() -> Path:
    shell = os.environ.get("SHELL", "/bin/zsh")
    return Path.home() / (".bash_profile" if shell.endswith("bash") else ".zshrc")


def step_command(dry: bool) -> Step:
    """`margin` on the PATH: a link in ~/.local/bin to this copy's margin-cli (works in scripts too, unlike an
    alias), and ~/.local/bin on the PATH in the shell's startup file."""
    target = paths.PROJECT_DIR / "margin-cli"
    link = BIN_DIR / "margin"
    notes = []
    if link.is_symlink() and os.path.realpath(link) == str(target.resolve()):
        status, text = "ok", f"The margin command: {health.tilde(link)}"
    elif link.exists() and not link.is_symlink():
        return Step("command", "margin", "skipped", f"{health.tilde(link)} exists and isn't a link to Margin, so "
                    f"it was left alone. Use {health.tilde(target)} instead, or move that file away and run margin "
                    "setup again.")
    elif dry:
        status, text = "would", f"Would link {health.tilde(link)} to {health.tilde(target)}"
    else:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        status, text = "done", f"The margin command: {health.tilde(link)} (open a new Terminal window to use it)"
    rc = _rc_file()
    rc_text = rc.read_text() if rc.exists() else ""
    on_path = str(BIN_DIR) in os.environ.get("PATH", "").split(":") or ".local/bin" in rc_text
    if not on_path:
        if dry:
            notes.append(f"Would add ~/.local/bin to the PATH in {health.tilde(rc)}")
        else:
            with open(rc, "a") as f:
                f.write('\n# Margin: the margin command\nexport PATH="$HOME/.local/bin:$PATH"\n')
            notes.append(f"Added ~/.local/bin to the PATH in {health.tilde(rc)}")
    for line in rc_text.splitlines():
        if line.strip().startswith("alias margin=") and str(paths.PROJECT_DIR) not in line:
            notes.append(f"{health.tilde(rc)} has an older `alias margin=...` for another copy; it wins in Terminal until you "
                         "remove that line.")
    return Step("command", "margin", status, text, notes=notes)


def step_data(dry: bool) -> Step:
    if paths.SETTINGS_PATH.exists():
        if not dry:
            paths.ensure_data_dir()           # also puts the folder's permissions right (0700 / 0600)
        return Step("data", "margin", "ok", f"Data folder and settings: {health.tilde(paths.DATA_DIR)}")
    if dry:
        return Step("data", "margin", "would", f"Would create the private data folder {health.tilde(paths.DATA_DIR)} "
                    "(only you can open it) with the default settings")
    from . import sync
    paths.ensure_data_dir()
    sync.load_settings()                       # writes every setting with its default
    return Step("data", "margin", "done", f"Data folder and default settings: {health.tilde(paths.DATA_DIR)}",
                notes=["Settings to choose (margin config set <key> <value>): currency (now "
                       f"{config.read().get('currency')}), self_names (your name as banks print it), read_sms, "
                       "misc_below, ai_sort / ai_review. AGENTS.md step 2 lists them all."])


def _helpers() -> list[tuple[str, Path, Callable[[], Path]]]:
    from . import lock, pdf
    return [("PDF reader", pdf.SOURCE, pdf._binary), ("Touch ID helper", lock.SOURCE, lock._binary)]


def step_helpers(dry: bool) -> Step:
    """The Swift helpers are compiled into the data folder's bin/ (not the person's data) when their source is
    newer than the program."""
    stale = []
    bin_dir = paths.DATA_DIR / "bin"
    for name, source, _ in _helpers():
        out = bin_dir / ("pdftext" if "PDF" in name else "Margin")
        if not out.exists() or out.stat().st_mtime < source.stat().st_mtime:
            stale.append(name)
    if not stale:
        return Step("helpers", "margin", "ok", "PDF reader and Touch ID helper built")
    if dry:
        return Step("helpers", "margin", "would", "Would build helper programs into the data folder's bin/: "
                    + ", ".join(stale))
    failed = []
    for name, _, build in _helpers():
        if name in stale:
            try:
                build()
            except (RuntimeError, OSError) as e:
                failed.append(f"{name}: {e}")
    if failed:
        return Step("helpers", "margin", "failed", "Couldn't build the Swift helpers", notes=failed)
    return Step("helpers", "margin", "done", "Built: " + ", ".join(stale))


def step_schedule(dry: bool, install_schedule: Callable[[], int]) -> Step:
    plist = _plist(paths.LAUNCHD_PLIST)
    if _ours(plist) and health._loaded(paths.LAUNCHD_LABEL):
        return Step("schedule", "margin", "ok", "Nightly sync scheduled (00:00)")
    if dry:
        return Step("schedule", "margin", "would", "Would add a LaunchAgent for the nightly 00:00 sync "
                    f"({health.tilde(paths.LAUNCHD_PLIST)})")
    if health.sync_running():
        return Step("schedule", "margin", "skipped", "A sync is running; run margin setup again when it's done")
    old_python = (plist.get("ProgramArguments") or [""])[0]
    rc, out = _quietly(install_schedule)
    notes = [line for line in out if not line.startswith(("Scheduled", "For bank SMS"))]
    if old_python and old_python != health.python():
        notes.append(f"The Python changed: give Full Disk Access to {health.tilde(health.python())} for bank texts.")
    if rc != 0:
        return Step("schedule", "margin", "failed", "Couldn't schedule the nightly sync", notes=notes)
    return Step("schedule", "margin", "done", "Nightly sync scheduled (00:00); a sleeping Mac runs it on waking",
                notes=notes)


def _app_ok(app_path: str) -> bool:
    from . import macapp
    if macapp.can_build() and macapp.stale(Path(app_path)):     # the browser stand-in, or older than its source
        return False
    return os.path.exists(app_path) and _ours(_plist(paths.WEB_PLIST)) and health._loaded(paths.WEB_LABEL)


def step_app(dry: bool, app_path: str, install_app: Callable[[], int]) -> Step:
    if _app_ok(app_path):
        return Step("app", "margin", "ok", f"Margin app and page service: {health.tilde(app_path)}, "
                    "http://margin.localhost/")
    if dry:
        return Step("app", "margin", "would", f"Would install the Margin app ({health.tilde(app_path)}) and a "
                    f"LaunchAgent for the page service ({health.tilde(paths.WEB_PLIST)})")
    rc, out = _quietly(install_app)
    if rc != 0:
        return Step("app", "margin", "failed", "Couldn't install the Margin app", notes=out)
    return Step("app", "margin", "done", f"Margin app and page service installed: {health.tilde(app_path)}",
                notes=[line for line in out if not line.startswith(("Margin app ready", "Building the Margin app",
                                                                    "It shows Margin in a window"))])


def step_gmail() -> Step:
    from . import gmail
    if gmail.is_configured():
        return Step("gmail", "you", "ok", "Gmail connected (read-only)")
    return Step("gmail", "you", "todo", "Gmail isn't connected yet", GMAIL_HOW)


def step_sms() -> Step:
    settings = config.read()
    if settings.get("read_sms") is False:
        return Step("sms", "you", "skipped", "Bank texts off (read_sms is false)")
    if health.check_sms(settings, (health.last_sync() or {}).get("sms"))["ok"]:
        return Step("sms", "you", "ok", "Bank texts readable by the nightly sync")
    return Step("sms", "you", "todo", "Bank texts: forwarding and Full Disk Access",
                SMS_HOW.format(python=health.tilde(health.python())))


def step_statements() -> Step:
    saved = health.secrets_saved()
    have = [k.upper() if k != "ppf_statement" else "PPF statement password" for k, v in saved.items() if v]
    india = config.read().get("currency") == "INR"
    if not india and not have:
        return Step("statements", "you", "skipped", "Statement passwords: not needed outside India "
                    "(other investments go in other_investments; see AGENTS.md step 2)")
    text = ("Statement passwords saved: " + ", ".join(have)) if have else "Statement passwords: none saved yet"
    return Step("statements", "you", "optional", text,
                STATEMENTS_HOW.format(statements=health.tilde(paths.STATEMENTS_DIR)))


def step_ai() -> Step:
    from . import llm
    settings = config.read()
    if not settings.get("ai_sort", True) and not settings.get("ai_review", True):
        return Step("ai", "you", "skipped", "AI off: Margin's own rules and checks run (margin setup-ai turns it on)")
    why = llm.missing(settings)
    if not why:
        return Step("ai", "you", "ok", f"AI for sorting and reviews: {llm.name(settings)}")
    return Step("ai", "you", "optional", "AI for sorting and reviews: not set up yet", AI_HOW, notes=[why])


def step_first_sync(dry: bool, no_sync: bool) -> Step:
    from . import gmail
    last = health.last_sync()
    if health.sync_running():
        return Step("first_sync", "margin", "ok", "A sync is running (margin sync shows its progress)")
    if last:
        return Step("first_sync", "margin", "ok", f"Synced before (last {last.get('at', '?')})")
    if not gmail.is_configured():
        return Step("first_sync", "margin", "skipped", "First sync: after Gmail is connected")
    if no_sync or dry:
        return Step("first_sync", "margin", "would" if dry else "skipped",
                    "First sync not started yet: margin sync (it reads 12 months of mail; up to an hour)")
    if not paths.LAUNCHD_PLIST.exists():
        return Step("first_sync", "margin", "skipped", "First sync: after the nightly sync is scheduled")
    r = _run("launchctl", "kickstart", f"gui/{os.getuid()}/{paths.LAUNCHD_LABEL}")
    if r.returncode != 0:
        return Step("first_sync", "margin", "failed", "Couldn't start the first sync; run margin sync")
    return Step("first_sync", "margin", "done", "First sync started in the background: it reads 12 months of mail "
                "and can take an hour. margin sync shows its progress; closing Terminal is fine.")


# --- the person's steps, in their own Terminal ---------------------------------------------------------------
def _terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _yes(prompt: str, default: bool = False) -> bool:
    a = _ask(prompt + (" [Y/n] " if default else " [y/N] ")).lower()
    return default if not a else a.startswith("y")


def walk_gmail(step: Step) -> Step:
    from . import gmail
    print(f"\n→ {step.text}\n{step.how}\n")
    path = _ask("When you have the downloaded JSON file, drag it here (or type its path) and press Return.\n"
                "Press Return alone to do this later: ").strip("'\" ")
    if not path:
        return step
    path = os.path.expanduser(path)
    try:
        paths.ensure_data_dir()
        who = gmail.setup(path)
        print(f"Connected read-only to {who}. The token is in your macOS Keychain (service 'margin').")
    except (Exception, SystemExit) as e:   # noqa: BLE001  show what Google said and carry on
        print(f"That didn't work: {e}\nRun margin setup again to retry.")
        return step
    if os.path.exists(path) and _yes(f"Delete {path} now? Its contents are in your Keychain.", default=True):
        os.remove(path)
    return step_gmail()


def walk_sms(step: Step) -> Step:
    print(f"\n→ {step.text}\n{step.how}\n")
    a = _ask("Press Return when you've done both, or type n if your bank texts don't reach this Mac: ").lower()
    if a.startswith("n"):
        config.set_value("read_sms", "false")
        return Step("sms", "you", "skipped", "Bank texts off (read_sms is false)")
    return Step("sms", "you", "ok", "Bank texts: forwarding and Full Disk Access done (the next sync checks them)")


def walk_statements(step: Step, prompts: dict) -> Step:
    print(f"\n· {step.text}\n{step.how}\n")
    saved = health.secrets_saved()
    for account, question in (("pan", "Do you have Indian stocks or mutual funds (a CDSL statement)?"),
                              ("pran", "Do you have an NPS account?"),
                              ("ppf_statement", "Do you get SBI's PPF statement by email?")):
        if not saved.get(account) and account in prompts and _yes(question):
            prompts[account]()
    return step_statements()


def walk_ai(step: Step, prompts: dict) -> Step:
    print(f"\n· {step.text}\n{step.how}\n")
    if "ai" in prompts and _yes("Choose one now?", default=True):
        prompts["ai"]()
    return step_ai()


# --- setup ---------------------------------------------------------------------------------------------------
def _auto(dry: bool, app_path: str, install_app: Callable[[], int], install_schedule: Callable[[], int]) -> list:
    return [step_command(dry), step_data(dry), step_helpers(dry), step_schedule(dry, install_schedule),
            step_app(dry, app_path, install_app)]


def _changes(steps: list[Step]) -> list[str]:
    """What a dry run says setup would change on this Mac."""
    out = []
    for s in steps:
        if s.status == "would" and s.text.startswith("Would "):
            out.append(s.text[len("Would "):])
        out += [n[len("Would "):] for n in s.notes if n.startswith("Would ")]
    return out


def setup(a, app_path: str, install_app: Callable[[], int], install_schedule: Callable[[], int],
          secret_prompts: dict) -> int:
    """Changes nothing without a yes: --yes, or the person's answer in their own Terminal. Without one it reports
    what it would change and what's waiting, like --dry-run."""
    mac = step_mac()
    if mac.status != "ok":
        return _report(a, [mac], shown=0, interactive=False, dry=True, pending=[])
    plan = _auto(True, app_path, install_app, install_schedule)
    pending = _changes(plan)
    if not a.no_sync and step_first_sync(True, False).status == "would":
        pending.append("start the first sync in the background (it reads 12 months of mail; up to an hour)")
    go = getattr(a, "yes", False) or not pending
    if pending and not go and not a.dry_run and not a.json and _terminal():
        print("Margin setup will:")
        for c in pending:
            print(f"  - {c}")
        go = _yes("Go ahead?", default=True)
        print()
    dry = a.dry_run or not go
    interactive = not a.json and not dry and _terminal()
    steps = [mac] + (plan if dry else _auto(False, app_path, install_app, install_schedule))
    if interactive:
        _print(steps, header=True)
    person = [step_gmail(), step_sms(), step_statements(), step_ai()]
    if interactive:
        walked = []
        for s in person:
            if s.status == "todo" and s.id == "gmail":
                s = walk_gmail(s)
            elif s.status == "todo" and s.id == "sms":
                s = walk_sms(s)
            elif s.status == "optional" and s.id == "statements":
                s = walk_statements(s, secret_prompts)
            elif s.status == "optional" and s.id == "ai":
                s = walk_ai(s, secret_prompts)
            walked.append(s)
        person = walked
    steps += person + [step_first_sync(dry, a.no_sync)]
    shown = len(steps) - len(person) - 1 if interactive else 0
    return _report(a, steps, shown, interactive, dry, pending if dry else [])


def _report(a, steps: list[Step], shown: int, interactive: bool, dry: bool, pending: list[str]) -> int:
    waiting = [s.id for s in steps if s.status == "todo"]
    failed = [s.id for s in steps if s.status == "failed"]
    needs_yes = bool(pending) and not a.dry_run
    if a.json:
        print(json.dumps({"ok": not waiting and not failed and not pending, "changed_nothing": dry,
                          "needs_yes": needs_yes, "would_change": pending, "waiting_on_you": waiting,
                          "failed": failed, "steps": [s.as_dict() for s in steps]}, indent=2, ensure_ascii=False))
    else:
        _print(steps[shown:], header=shown == 0, how=not interactive)
        if needs_yes:
            print("\nNothing was changed. Setup would: " + "; ".join(pending) + ".\nOnce the person agrees: "
                  "margin setup --yes (or they run margin setup in their own Terminal, which asks).")
        elif a.dry_run and pending:
            print("\nDry run: nothing was changed.")
        if waiting:
            print(f"\nWaiting on you: {', '.join(waiting)}. Do the steps above, then run margin setup again.")
        elif not failed and not pending and not dry:
            print("\n" + AFTER)
    return 1 if failed else EXIT_WAITING if waiting or pending else 0


def _print(steps: list[Step], header: bool = False, how: bool = True) -> None:
    if header:
        print("Margin setup\n")
    for s in steps:
        who = " (you)" if s.who == "you" and s.status in ("todo", "optional") else ""
        print(f"{SYMBOL.get(s.status, '·')} {s.text}{who}")
        for n in s.notes:
            print(f"    {n}")
        if how and s.how and s.status in ("todo", "optional"):
            print("\n".join("    " + line for line in s.how.splitlines()) + "\n")


# --- update --------------------------------------------------------------------------------------------------
def _git(*args: str) -> subprocess.CompletedProcess:
    return _run("git", "-C", str(paths.PROJECT_DIR), *args)


def run_tests() -> tuple[bool, str]:
    """The test suite on a throwaway data folder and demo folder, never the person's."""
    with tempfile.TemporaryDirectory(prefix="margin-update-") as tmp:
        env = dict(os.environ, MARGIN_DATA=os.path.join(tmp, "data"), MARGIN_DEMO_DATA=os.path.join(tmp, "demo"),
                   PYTHONPATH=str(paths.PROJECT_DIR))
        env.pop(FINISH_ENV, None)
        r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                           cwd=str(paths.PROJECT_DIR), env=env, capture_output=True, text=True, timeout=1800)
    lines = (r.stderr or r.stdout).strip().splitlines()
    return r.returncode == 0, "\n".join(lines[-15:])


def _other_agents() -> list[str]:
    """Margin's other launchd agents (anything com.<you>.margin.* besides the nightly sync and the page)."""
    folder = paths.LAUNCHD_PLIST.parent
    found = sorted(folder.glob(f"{paths.LAUNCHD_LABEL}.*.plist")) if folder.exists() else []
    return [p.name[:-len(".plist")] for p in found if p.name[:-len(".plist")] != paths.WEB_LABEL]


FINISH_ENV = "MARGIN_UPDATE_FINISH"     # set only by update itself, for the half that runs on the new code


def _incoming(target: str) -> list[str]:
    out = _git("log", "--reverse", "--format=%h %s", f"HEAD..{target}").stdout
    return [line for line in out.splitlines() if line.strip()]


def update(a, app_path: str, install_app: Callable[[], int], install_schedule: Callable[[], int]) -> int:
    """Fetch, show what's coming, and install exactly that only with a yes: `--to <commit>` (after the person saw
    the list from --check) or their answer in their own Terminal."""
    if _git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        print(f"{health.tilde(paths.PROJECT_DIR)} isn't a git checkout, so Margin can't update itself. Get it with "
              "git clone (README) and run margin setup there.")
        return 1
    finishing = os.environ.pop(FINISH_ENV, None)
    if finishing is not None:
        return _finish(finishing, app_path, install_app, install_schedule)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").stdout.strip()
    if not upstream:
        print(f"This copy's branch ({branch}) doesn't follow a branch on GitHub, so there's nothing to update from.")
        return 1
    if not a.check and health.sync_running():
        print("A sync is running (margin sync shows it). Run margin update again when it's done.")
        return 1
    if _git("fetch", "--quiet").returncode != 0:
        print("Couldn't reach GitHub, so nothing changed.")
        return 1
    head = _git("rev-parse", "HEAD").stdout.strip()
    target = _git("rev-parse", upstream).stdout.strip()
    if a.to:
        target = _git("rev-parse", "--verify", "--quiet", f"{a.to}^{{commit}}").stdout.strip()
        if not target or _git("merge-base", "--is-ancestor", target, upstream).returncode != 0:
            print(f"{a.to} isn't a commit on {upstream}, so nothing changed. margin update --check lists what's new.")
            return 1
    if _git("merge-base", "--is-ancestor", head, target).returncode != 0:
        print(f"Can't update by fast-forward, so nothing changed: this copy has commits that aren't on {upstream}. "
              "Keep them on a branch, or ask for help. (Margin never merges.)")
        return 1
    commits = _incoming(target)
    if not commits:
        if a.force and not a.check:
            return _after(None, head, app_path, install_app, install_schedule)
        print(f"Margin is up to date ({head[:7]}).")
        return 0
    print(f"{len(commits)} new change{'s' if len(commits) != 1 else ''} on {upstream} ({head[:7]} → {target[:7]}):")
    for line in commits[:40]:
        print(f"  {line}")
    if len(commits) > 40:
        print(f"  … and {len(commits) - 40} more")
    if a.check:
        print(f"To install exactly these once the person agrees: margin update --to {target[:12]}")
        return 0
    if not a.to and not (_terminal() and _yes("Install them?")):
        print(f"Nothing changed. To install exactly these once the person agrees: margin update --to {target[:12]}")
        return EXIT_WAITING
    merge = _git("merge", "--ff-only", "--quiet", target)
    if merge.returncode != 0:
        why = (merge.stderr or merge.stdout).strip().splitlines()
        print(f"Couldn't fast-forward, so nothing changed.\n  git: {why[-1] if why else 'failed'}")
        return 1
    print(f"Got {len(commits)} change{'s' if len(commits) != 1 else ''}: {head[:7]} → {target[:7]}.", flush=True)
    # This process still runs the old code: carry on in a fresh one, so the new code tests, builds and installs
    # itself. It checks it's really the second half of this update (see _finish).
    env = dict(os.environ, PYTHONPATH=str(paths.PROJECT_DIR), **{FINISH_ENV: head})
    os.chdir(paths.PROJECT_DIR)     # `python -m` looks in the current folder first
    os.execve(sys.executable, [sys.executable, "-m", "margin", "update"], env)
    return 0                        # not reached


def _finish(old: str, app_path: str, install_app: Callable[[], int], install_schedule: Callable[[], int]) -> int:
    """The second half of an update, on the new code. Only right after update's own fast-forward: the commit it
    came from must be HEAD's previous position in the reflog, and an ancestor of HEAD."""
    new = _git("rev-parse", "HEAD").stdout.strip()
    before = _git("rev-parse", "--verify", "--quiet", "HEAD@{1}").stdout.strip()
    if not old or old == new or before != old or _git("merge-base", "--is-ancestor", old, new).returncode != 0:
        print("This only runs as the second half of margin update, right after it fast-forwarded. Nothing changed; "
              "run margin update.")
        return 1
    return _after(old, new, app_path, install_app, install_schedule)


def _after(old: str | None, new: str, app_path: str, install_app: Callable[[], int],
           install_schedule: Callable[[], int]) -> int:
    """Test the code now checked out (going back to `old` if that fails), rebuild, reinstall, restart."""
    print("Running the tests (on a temporary folder)...", flush=True)
    ok, tail = run_tests()
    if not ok:
        print(tail)
        if old:
            back = _git("reset", "--keep", old)
            print(f"The tests failed, so Margin went back to {old[:7]}." if back.returncode == 0 else
                  f"The tests failed and going back to {old[:7]} didn't work: {back.stderr.strip()}")
        return 1
    print("✓ Tests passed")
    if paths.DATA_DIR.exists():
        built = step_helpers(dry=False)
        print(f"{SYMBOL[built.status]} {built.text}")
        for n in built.notes:
            print(f"    {n}")
    restarted = []
    if os.path.exists(app_path) or paths.WEB_PLIST.exists():
        rc, out = _quietly(install_app)
        print("✓ App and page service reinstalled" if rc == 0 else "✗ Couldn't reinstall the app:\n    "
              + "\n    ".join(out))
    if paths.LAUNCHD_PLIST.exists():
        old_python = (_plist(paths.LAUNCHD_PLIST).get("ProgramArguments") or [""])[0]
        rc, out = _quietly(install_schedule)
        print("✓ Nightly sync reloaded" if rc == 0 else "✗ Couldn't reload the nightly sync:\n    " + "\n    ".join(out))
        if old_python and old_python != health.python():
            print(f"    The Python changed: give Full Disk Access to {health.tilde(health.python())} for bank texts.")
    for label in _other_agents():
        if health._loaded(label):
            _run("launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}")
            restarted.append(label)
    if restarted:
        print(f"✓ Restarted {len(restarted)} more Margin service{'s' if len(restarted) != 1 else ''}")
    print(f"Margin is up to date ({new[:7]}). Your data wasn't touched.")
    return 0
