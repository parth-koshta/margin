"""margin <command>  (see README.md)"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from datetime import datetime

from . import categorize, db, gmail, paths, sms, sync, tidy


def _job_running(uid: int) -> bool:
    r = subprocess.run(["launchctl", "print", f"gui/{uid}/{paths.LAUNCHD_LABEL}"], capture_output=True, text=True)
    return r.returncode == 0 and "state = running" in r.stdout


_RATE: dict = {}


def _progress_line(cols: int = 100) -> str:
    try:
        p = json.loads(paths.PROGRESS_PATH.read_text())
    except (OSError, ValueError):
        return ""
    elapsed = time.time() - p.get("started", time.time())
    mins = f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s"
    done, total = p.get("done", 0), p.get("total", 0)
    if not total:
        return f"{p['phase']}...  {mins}"[:cols]
    frac = done / total
    # Speed is measured while watching (the initial listing would skew an average from the start).
    now, first = time.time(), _RATE.setdefault(p["phase"], (time.time(), done))
    eta = ""
    if done < total and now - first[0] >= 5 and done > first[1]:
        left = (total - done) * (now - first[0]) / (done - first[1])
        eta = f" ~{int(left // 60)}m left"
    tail = f" {frac:4.0%} {done:,}/{total:,}{eta}"
    width = max(8, min(30, cols - len(tail) - 3))
    bar = "█" * int(frac * width) + "░" * (width - int(frac * width))
    line = f"[{bar}]{tail}"
    if len(p["phase"]) + 1 + len(line) <= cols:
        line = f"{p['phase']} {line}"
    return line[:cols]


def cmd_sync(a) -> int:
    # macOS grants Full Disk Access (bank SMS) to the scheduled job, not to Terminal, so a manual sync
    # starts that same job and waits for it. --here runs in this process (that's what the job runs).
    uid = os.getuid()
    in_job = os.environ.get("XPC_SERVICE_NAME") == paths.LAUNCHD_LABEL
    if a.here or in_job or not paths.LAUNCHD_PLIST.exists():
        return sync.run(quiet_notify=a.quiet)
    print("Syncing (the first run reads back a year and can take several minutes)...", flush=True)
    if not _job_running(uid):
        r = subprocess.run(["launchctl", "kickstart", f"gui/{uid}/{paths.LAUNCHD_LABEL}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Couldn't start the sync job ({r.stderr.strip()}); running here instead, without SMS access.")
            return sync.run(quiet_notify=a.quiet)
        time.sleep(1)
    tty = sys.stdout.isatty()
    shown = ""
    while _job_running(uid):
        cols = shutil.get_terminal_size((100, 20)).columns - 1 if tty else 100
        line = _progress_line(cols)
        if line and line != shown:
            print(("\r\033[K" + line) if tty else line, end="" if tty else "\n", flush=True)
            shown = line
        time.sleep(1 if tty else 15)
    if tty and shown:
        print()
    log = paths.LOG_DIR / "sync.log"
    last = log.read_text().strip().splitlines()[-1] if log.exists() else ""
    print(last.split(" ", 2)[-1] if last else "Done.")
    print("Open the dashboard: margin open. Details: margin doctor.")
    return 0 if " sync ok " in f" {last} " else 1


def cmd_open(a) -> int:
    from . import server
    url = server.open_app(browser=not a.print_url)
    if a.print_url:
        print(url)
    return 0


APP_PATH = os.path.expanduser("~/Applications/Margin.app")


def _install_web_agent() -> bool:
    """launchd keeps 127.0.0.1:80 and [::1]:80 open and starts `margin serve` when a page is opened."""
    uid = os.getuid()
    sock = lambda node: {"SockNodeName": node, "SockServiceName": "80", "SockType": "stream"}
    plist = {
        "Label": paths.WEB_LABEL,
        "ProgramArguments": [_python(), "-m", "margin", "serve"],
        "WorkingDirectory": str(paths.PROJECT_DIR),
        "EnvironmentVariables": {"PYTHONPATH": str(paths.PROJECT_DIR)},
        "Sockets": {"Web": sock("127.0.0.1"), "Web6": sock("::1")},
        "StandardErrorPath": str(paths.LOG_DIR / "server.err.log"),
    }
    paths.WEB_PLIST.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{paths.WEB_LABEL}"], capture_output=True)
    with open(paths.WEB_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(paths.WEB_PLIST)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl refused the page service: {r.stderr.strip()}")
    return r.returncode == 0


LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/"
              "lsregister")


def _set_app_icon(app: str) -> None:
    """Give the app Margin's icon instead of the AppleScript one. osacompile's Info.plist names an icon in
    Assets.car (CFBundleIconName), which beats applet.icns, so both go. The bundle is then signed again, ad hoc,
    as osacompile signs it, and Finder / the Dock are told to look again."""
    from . import icon
    res = os.path.join(app, "Contents", "Resources")
    icon.write_icns(os.path.join(res, "applet.icns"))
    car = os.path.join(res, "Assets.car")
    if os.path.exists(car):
        os.remove(car)
    info = os.path.join(app, "Contents", "Info.plist")
    with open(info, "rb") as f:
        plist = plistlib.load(f)
    plist.pop("CFBundleIconName", None)
    plist["CFBundleIconFile"] = "applet"
    with open(info, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["codesign", "--force", "--sign", "-", app], check=True, capture_output=True)
    os.utime(app)
    subprocess.run([LSREGISTER, "-f", app], capture_output=True)


def _make_script_app() -> bool:
    """The fallback without the Command Line Tools: a small AppleScript app that opens the page in the browser."""
    cmd = (f"PYTHONPATH={json.dumps(str(paths.PROJECT_DIR))} {json.dumps(_python())} -m margin open "
           "> /dev/null 2>&1 &")
    script = f"do shell script {json.dumps(cmd)}"
    shutil.rmtree(APP_PATH, ignore_errors=True)          # osacompile would mix into an existing app
    r = subprocess.run(["osacompile", "-o", APP_PATH, "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Couldn't make the app: {r.stderr.strip()}")
        return False
    try:
        _set_app_icon(APP_PATH)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"(The app works but kept the plain AppleScript icon: {e})")
    return True


def cmd_app(a) -> int:
    """The Margin app in ~/Applications (Spotlight, Launchpad, Dock): the page in a window of its own, plus the
    always-on page service at http://margin.localhost/. Without the Command Line Tools (swiftc) the app is a small
    AppleScript one that opens the page in the browser instead."""
    from . import macapp, server
    if a.action == "remove":
        macapp.stop(APP_PATH)                            # quits it (locking Margin) and drops Open at Login
        subprocess.run(["rm", "-rf", APP_PATH])
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{paths.WEB_LABEL}"], capture_output=True)
        paths.WEB_PLIST.unlink(missing_ok=True)
        macapp.forget()
        print("Margin app and page service removed.")
        return 0
    paths.ensure_data_dir()
    if not _install_web_agent():
        return 1
    os.makedirs(os.path.dirname(APP_PATH), exist_ok=True)
    native = False
    if macapp.can_build():
        print("Building the Margin app…", flush=True)
        try:
            macapp.build(APP_PATH, server.address(), server.KEY_PATH)
            native = True
        except RuntimeError as e:
            print(f"Couldn't build the Mac app ({e}). Making the simple one that opens your browser instead.")
    else:
        print("No Command Line Tools (xcode-select --install), so the Margin app opens your browser for now. "
              "Run margin app install again once they're installed to get its own window.")
    if not native and not _make_script_app():
        return 1
    print(f"Margin app ready: {APP_PATH}. Open it from Spotlight (⌘ Space, type Margin) or drag it to the Dock.")
    if native:
        print("It shows Margin in a window of its own; nothing from it is kept in a browser.")
    if server.hostname_ok():
        print(f"In a browser: margin open (once per browser), then bookmark http://{server.HOSTNAME}/")
    else:
        print(f"Margin is at http://127.0.0.1/. For http://{server.HOSTNAME}/ add it to /etc/hosts once, then run "
              "margin app install again:")
        print(f"  printf '127.0.0.1\\t{server.HOSTNAME}\\n::1\\t{server.HOSTNAME}\\n' | sudo tee -a /etc/hosts")
    return 0


def cmd_privacy(a) -> int:
    from . import privacy
    return privacy.main(a)


def cmd_demo(a) -> int:
    from . import demo
    return demo.main(a)


def cmd_serve(a) -> int:
    from . import server
    server.serve()
    return 0


def cmd_setup_gmail(a) -> int:
    paths.ensure_data_dir()
    who = gmail.setup(a.client_secret)
    print(f"Connected read-only to {who}. The token is in your macOS Keychain (service 'margin').")
    print("You can delete the downloaded client_secret JSON now.")
    return 0


def _setup_secret(label: str, account: str, pattern: str, shape: str, test_q: str, test_name: str) -> int:
    """Ask for a statement password (hidden), keep it in the Keychain, and try it on the latest statement."""
    import getpass
    import re
    from . import keychain, pdf
    paths.ensure_data_dir()
    value = getpass.getpass(f"{label} (typing is hidden): ").strip().upper()
    if not re.fullmatch(pattern, value):
        print(f"That doesn't look like a {label} ({shape}). Nothing saved.")
        return 1
    keychain.set_secret(account, value)
    print(f"Saved in your macOS Keychain (service 'margin', account '{account}').")
    try:
        ids = gmail.search(test_q, limit=1)
        m = gmail.message(ids[0]) if ids else None
        name, ref = next((f for f in (m or {}).get("files", []) if f[0].lower().endswith(".pdf")), (None, None))
        if not name:
            print(f"No {test_name} email with a PDF found yet to test it on. It'll be used when one arrives.")
            return 0
        text = pdf.text(gmail.attachment(m["id"], ref), value)
        print(f"Checked: it opens your latest {test_name} ({name}, {text.count(chr(12)) + 1} pages).")
    except pdf.WrongPassword:
        print(f"Saved, but it didn't open your latest {test_name}. Check the {label} and run this again.")
        return 1
    except Exception as e:   # noqa: BLE001  (Gmail offline etc.: the value is still saved)
        print(f"Saved. Couldn't test it right now: {e}")
    return 0


def cmd_setup_pan(a) -> int:
    """Your PAN opens the CDSL CAS and INDmoney statement PDFs. It goes to the Keychain only."""
    from . import pdf
    return _setup_secret("PAN", pdf.PAN_ACCOUNT, r"[A-Z]{5}[0-9]{4}[A-Z]", "5 letters, 4 digits, 1 letter",
                         "from:cdslstatement.com has:attachment", "CDSL statement")


def cmd_setup_ppf(a) -> int:
    """SBI's PPF statement password: the last 5 digits of your mobile number, then your date of birth (DDMMYY)."""
    return _setup_secret("SBI PPF statement password", "ppf_statement", r"[0-9]{11}",
                         "the last 5 digits of your mobile number followed by your date of birth as DDMMYY",
                         'from:yonobysbi@alerts.sbi.bank.in subject:"PPF Account Statement" has:attachment',
                         "SBI PPF statement")


def cmd_setup_pran(a) -> int:
    """Your PRAN opens the monthly NPS statement PDF. It goes to the Keychain only."""
    from . import pdf
    return _setup_secret("PRAN", pdf.PRAN_ACCOUNT, r"[0-9]{12}", "12 digits",
                         'from:KCRA@kfintech.com subject:"Monthly Transaction Statement" has:attachment', "NPS statement")


def cmd_rule(a) -> int:
    categorize.add_user_rule(a.keyword, a.category, a.direction)
    conn = db.connect()
    n = sync.recategorize(conn, sync.load_settings())
    conn.commit()
    print(f"Rule saved. Re-checked {n} transactions; the dashboard updates on the next sync (or margin sync now).")
    return 0


def cmd_trust(a) -> int:
    n = tidy.trust_sender(db.connect(), sync.load_settings(), a.sender)
    print(f"'{a.sender}' is now treated as a bank. {n} of its messages get read on the next sync (margin sync).")
    return 0


def cmd_tidy(a) -> int:
    return tidy.run()


def _python() -> str:
    return os.path.realpath(sys.executable)


def cmd_schedule(a) -> int:
    uid = os.getuid()
    if a.action == "install":
        paths.ensure_data_dir()
        plist = {
            "Label": paths.LAUNCHD_LABEL,
            "ProgramArguments": [_python(), "-m", "margin", "sync", "--here"],
            "WorkingDirectory": str(paths.PROJECT_DIR),
            "EnvironmentVariables": {"PYTHONPATH": str(paths.PROJECT_DIR)},
            "StartCalendarInterval": {"Hour": 0, "Minute": 0},
            "StandardOutPath": str(paths.LOG_DIR / "launchd.out.log"),
            "StandardErrorPath": str(paths.LOG_DIR / "launchd.err.log"),
            "ProcessType": "Background",
            "LowPriorityIO": True,
        }
        paths.LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(paths.LAUNCHD_PLIST)], capture_output=True)
        with open(paths.LAUNCHD_PLIST, "wb") as f:
            plistlib.dump(plist, f)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(paths.LAUNCHD_PLIST)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"launchctl refused: {r.stderr.strip()}")
            return 1
        print("Scheduled: every night at 00:00. If the Mac is asleep then, it runs as soon as it wakes.")
        print(f"For bank SMS, give Full Disk Access to: {_python()}")
        return 0
    if a.action == "remove":
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(paths.LAUNCHD_PLIST)], capture_output=True)
        paths.LAUNCHD_PLIST.unlink(missing_ok=True)
        print("Nightly sync removed.")
        return 0
    r = subprocess.run(["launchctl", "print", f"gui/{uid}/{paths.LAUNCHD_LABEL}"], capture_output=True, text=True)
    print("Nightly sync is scheduled (00:00)." if r.returncode == 0 else "Nightly sync is NOT scheduled.")
    return 0 if r.returncode == 0 else 1


def cmd_doctor(a) -> int:
    ok = True

    def line(good: bool, msg: str):
        nonlocal ok
        ok &= good
        print(("✓ " if good else "✗ ") + msg)

    line(paths.DATA_DIR.exists() and (paths.DATA_DIR.stat().st_mode & 0o077) == 0,
         f"Private data folder (only you can open it): {paths.DATA_DIR}")
    line(gmail.is_configured(), "Gmail connected (read-only, token in Keychain)" if gmail.is_configured()
         else "Gmail not connected: margin setup-gmail <client_secret.json>")
    if gmail.is_configured():
        try:
            gmail.access_token()
            line(True, "Gmail token still valid")
        except gmail.AuthError as e:
            line(False, f"Gmail token problem: {e}")
    # Terminal-run checks inherit Terminal's permissions, so judge SMS by what the scheduled job saw.
    last_sms = None
    if paths.DB_PATH.exists():
        last = db.get_state(db.connect(), "last_sync")
        last_sms = json.loads(last).get("sms") if last else None
    if sync.load_settings()["read_sms"] is False:
        line(True, "Bank SMS off (read_sms is false in settings.json)")
    elif last_sms == "ok" or sms.available()[0]:
        line(True, "Bank SMS readable by the sync job")
    else:
        line(False, "Bank SMS: the sync job couldn't read Messages yet. Give Full Disk Access to "
             f"{_python()} (System Settings > Privacy & Security > Full Disk Access), then run margin sync.")
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{paths.LAUNCHD_LABEL}"], capture_output=True)
    line(r.returncode == 0, "Nightly 00:00 sync scheduled" if r.returncode == 0 else
         "Nightly sync not scheduled: margin schedule install")
    if paths.DB_PATH.exists():
        conn = db.connect()
        last = db.get_state(conn, "last_sync")
        if last:
            s = json.loads(last)
            age_h = (datetime.now() - datetime.fromisoformat(s["at"])).total_seconds() / 3600
            line(s["ok"] and age_h < 30, f"Last sync {s['at']} ({'ok' if s['ok'] else 'had problems'})"
                 + ("" if age_h < 30 else " — more than a day ago"))
            for p in s["problems"]:
                print(f"    · {p}")
        else:
            line(False, "Never synced: margin sync")
    return 0 if ok else 1


def main() -> int:
    os.umask(0o077)     # everything Margin writes (database, rules, statements, PDF reader) is yours alone
    p = argparse.ArgumentParser(prog="margin", description="Margin: private money tracker")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="pull new email/SMS and rebuild the dashboard")
    s.add_argument("--quiet", action="store_true", help="no macOS notification on problems")
    s.add_argument("--here", action="store_true",
                   help="run in this process instead of through the scheduled job (no SMS from Terminal)")
    s.set_defaults(fn=cmd_sync)
    s = sub.add_parser("open", help="open the Margin page (dashboard + Sort out) in your browser")
    s.add_argument("--print-url", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_open)
    sub.add_parser("serve", help=argparse.SUPPRESS).set_defaults(fn=cmd_serve)
    s = sub.add_parser("app", help="install | remove the Margin app in ~/Applications")
    s.add_argument("action", choices=["install", "remove"])
    s.set_defaults(fn=cmd_app)
    s = sub.add_parser("setup-gmail", help="connect Gmail read-only with your own Google sign-in")
    s.add_argument("client_secret")
    s.set_defaults(fn=cmd_setup_gmail)
    sub.add_parser("setup-pan", help="save your PAN in the Keychain so statement PDFs can be opened"
                   ).set_defaults(fn=cmd_setup_pan)
    sub.add_parser("setup-ppf", help="save your SBI PPF statement password in the Keychain"
                   ).set_defaults(fn=cmd_setup_ppf)
    sub.add_parser("setup-pran", help="save your NPS PRAN in the Keychain so the NPS statement can be opened"
                   ).set_defaults(fn=cmd_setup_pran)
    s = sub.add_parser("rule", help='categorize: margin rule "swiggy" "Food & dining"')
    s.add_argument("keyword")
    s.add_argument("category")
    s.add_argument("--direction", choices=["in", "out"])
    s.set_defaults(fn=cmd_rule)
    s = sub.add_parser("trust", help="treat an email domain / SMS sender as a bank")
    s.add_argument("sender")
    s.set_defaults(fn=cmd_trust)
    s = sub.add_parser("schedule", help="install | remove | status of the nightly 00:00 sync")
    s.add_argument("action", choices=["install", "remove", "status"])
    s.set_defaults(fn=cmd_schedule)
    sub.add_parser("tidy", help="sort uncategorized payees and unknown senders, one question at a time"
                   ).set_defaults(fn=cmd_tidy)
    sub.add_parser("doctor", help="check that everything works").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("privacy-check", help="look for your personal details in the code before it's shared")
    s.add_argument("--staged", action="store_true", help="check what's staged for commit (git pre-commit hook)")
    s.add_argument("--commit-msg", metavar="FILE", help="check a commit message (git commit-msg hook)")
    s.add_argument("--push", action="store_true", help="check commits being pushed, refs on stdin (pre-push hook)")
    s.add_argument("--install-hooks", action="store_true", help="turn on the git hooks in .githooks/")
    s.set_defaults(fn=cmd_privacy)
    s = sub.add_parser("demo", help="try Margin on a made-up person's money (never touches your data)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--fresh", action="store_true", help="rebuild the made-up data now")
    s.add_argument("--lock", action="store_true", help="start locked, with Touch ID simulated")
    s.add_argument("--india", action="store_true", help="the Indian person in rupees instead of the US person in dollars")
    s.add_argument("--no-open", action="store_true", help="don't open the browser")
    s.add_argument("--app", action="store_true", help="open it in the Margin app's window instead of the browser")
    s.set_defaults(fn=cmd_demo)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
