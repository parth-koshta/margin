"""The Claude Code data guard (.claude/hooks/data_guard.py): what it blocks and what it lets through.

All paths are made up: the home folder is /Users/you, and the backup tests build a temporary one.
"""
import tests  # noqa: F401  (temporary data folder first)

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "data_guard.py"
_spec = importlib.util.spec_from_file_location("data_guard", HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

HOME = "/Users/you"
REPO = HOME + "/projects/margin"
DATA = HOME + "/Library/Application Support/Margin"


def decide(tool, home=HOME, env=None, cwd=REPO, **tool_input):
    return guard.decide({"tool_name": tool, "tool_input": tool_input, "cwd": cwd}, home=home, env=env or {})


def bash(command, **kw):
    return decide("Bash", command=command, **kw)


class Blocks(unittest.TestCase):
    def test_reading_the_data_folder_with_shell_commands(self):
        for cmd in (
            "cat ~/Library/Application\\ Support/Margin/settings.json",
            'cat "$HOME/Library/Application Support/Margin/rules.json"',
            "cat ${HOME}/Library/Application\\ Support/Margin/server.key",
            f"sqlite3 '{DATA}/money.db' 'select count(*) from transactions'",
            "ls ~/Library/Application\\ Support/Margin/statements",
            "head ~/Library/Application\\ Support/Margin/private-words.txt",
            "cp -r ~/Library/Application\\ Support/Margin /tmp/copy",
            "cat ~/library/application\\ support/margin/rules.json",            # the disk ignores case
            "ls ~/Library/Application\\ Support/Mar*",                          # a glob that reaches it
            "cd ~/Library/Application\\ Support && cat Margin/settings.json",
            "cd ~/Library/Application\\ Support && ls Margin",
            "tail -n 5 ~/Library/Application\\ Support/Margin/logs/sync.log",
        ):
            with self.subTest(cmd=cmd):
                self.assertIn("margin doctor --json", bash(cmd) or "")

    def test_code_that_names_the_folder(self):
        heredoc = ("python3 - <<'EOF'\nimport sqlite3, pathlib\n"
                   "p = pathlib.Path.home() / 'Library' / 'Application Support' / 'Margin' / 'money.db'\n"
                   "print(\"it's\", sqlite3.connect(p))\nEOF")
        self.assertTrue(bash(heredoc))
        self.assertTrue(bash("python3 -c \"open('/Users/you/Library/Application Support/Margin/settings.json')\""))

    def test_searching_from_a_folder_above_it(self):
        for cmd in ("find ~ -name money.db", "grep -rn ACME ~/Library", "rg ACME ~/Library/Application\\ Support",
                    "cd ~ && grep -r ACME ."):
            with self.subTest(cmd=cmd):
                self.assertTrue(bash(cmd))
        self.assertTrue(bash("grep -rn ACME .", cwd=HOME))

    def test_a_shell_already_inside_the_folder(self):
        self.assertTrue(bash("ls", cwd=DATA + "/statements"))

    def test_messages_keychain_and_the_real_page(self):
        self.assertTrue(bash("cp ~/Library/Messages/chat.db /tmp/x"))
        self.assertTrue(bash("security find-generic-password -s margin -a pan -w"))
        self.assertTrue(bash("security dump-keychain -d"))
        self.assertTrue(bash("curl -s http://margin.localhost/"))
        self.assertTrue(bash("curl -s 'http://127.0.0.1/?k=x'"))
        self.assertTrue(bash("python3 -c \"import urllib.request; urllib.request.urlopen('http://localhost/')\""))

    def test_python_on_margins_code_needs_a_temporary_folder(self):
        why = bash("python3 -c 'from margin import db; print(db.connect())'")
        self.assertIn("MARGIN_DATA", why or "")
        self.assertTrue(bash("PYTHONPATH=. /usr/bin/python3 - <<'EOF'\nimport margin.sync\nEOF"))

    def test_margin_commands_that_print_personal_things(self):
        for cmd, hint in (("./margin-cli tidy", "own Terminal"), ("margin setup-pan", "own Terminal"),
                          ("margin setup-pran", "hidden prompt"), ("cd ~/projects/margin && ./margin-cli setup-ppf",
                                                                    "hidden prompt"),
                          ("margin open --print-url", "key"), ("margin config get self_names --show", "--show"),
                          ("/usr/bin/python3 -I -m margin tidy", "own Terminal")):
            with self.subTest(cmd=cmd):
                self.assertIn(hint, bash(cmd) or "")

    def test_file_tools(self):
        self.assertTrue(decide("Read", file_path=DATA + "/server.key"))
        self.assertTrue(decide("Read", file_path="~/Library/Application Support/Margin/settings.json"))
        self.assertTrue(decide("Edit", file_path=DATA + "/settings.json", old_string="a", new_string="b"))
        self.assertTrue(decide("Write", file_path=DATA + "/rules.json", content="[]"))
        self.assertTrue(decide("Grep", pattern="ACME", path=HOME + "/Library"))
        self.assertTrue(decide("Grep", pattern="ACME", cwd=HOME))
        self.assertTrue(decide("Glob", pattern="**/*.db", path=HOME))
        self.assertTrue(decide("Glob", pattern=DATA + "/statements/*.pdf"))
        self.assertTrue(decide("NotebookEdit", notebook_path=DATA + "/x.ipynb", new_source=""))

    def test_other_tools_by_their_paths_and_urls(self):
        self.assertTrue(decide("mcp__files__read", path=DATA + "/money.db"))
        self.assertTrue(decide("mcp__browser__navigate", url="http://margin.localhost/"))
        self.assertTrue(decide("WebFetch", url="http://127.0.0.1/", prompt="x"))
        self.assertTrue(decide("mcp__browser__navigate", url="file://" + DATA + "/settings.json"))

    def test_margin_data_elsewhere(self):
        env = {"MARGIN_DATA": "/Volumes/Safe/Margin"}
        self.assertTrue(decide("Read", file_path="/Volumes/Safe/Margin/money.db", env=env))
        self.assertTrue(bash("sqlite3 /Volumes/Safe/Margin/money.db .tables", env=env))


class Allows(unittest.TestCase):
    def test_everyday_work_on_the_code(self):
        for cmd in ("/usr/bin/python3 -m unittest discover -s tests -t .", "./margin-cli privacy-check",
                    "git status && git diff", "grep -rn parse_alert margin/", "ls ~/Library/Application\\ Support/",
                    "find . -name '*.py'", "MARGIN_DATA=$(mktemp -d) python3 -c 'from margin import db; db.connect()'",
                    "python3 -c 'import tests, margin.parse'", "curl -s http://demo.margin.localhost:8765/",
                    "cat ~/Library/LaunchAgents/com.you.margin.plist", "rm -rf /tmp/margin-scratch"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd))

    def test_margin_commands(self):
        for cmd in ("margin doctor --json", "./margin-cli doctor", "margin setup --json", "margin update --check",
                    "margin config get", "margin config get currency", "margin config set currency USD",
                    "margin sync", "margin demo --india --no-open", "margin schedule status", "margin open",
                    "margin rule acme Shopping", 'margin trust "mybank.example"', "margin setup-gmail ~/Downloads/x.json"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd))

    def test_the_demo_and_temporary_folders(self):
        self.assertIsNone(bash("ls ~/Library/Caches/Margin\\ Demo/us"))
        self.assertIsNone(decide("Read", file_path=HOME + "/Library/Caches/Margin Demo/us/settings.json"))
        self.assertIsNone(decide("mcp__browser__navigate", url="http://demo.margin.localhost:8765/"))
        tmp = tempfile.gettempdir() + "/margin-tests-x"
        self.assertIsNone(decide("Read", file_path=tmp + "/money.db", env={"MARGIN_DATA": tmp}))

    def test_file_tools_in_the_repo(self):
        self.assertIsNone(decide("Read", file_path=REPO + "/AGENTS.md"))
        self.assertIsNone(decide("Grep", pattern="Application Support/Margin"))
        self.assertIsNone(decide("Glob", pattern="**/*.py"))
        # Docs may name the folder: only the file being written counts for Edit and Write.
        self.assertIsNone(decide("Edit", file_path=REPO + "/AGENTS.md", old_string="x",
                                 new_string="Your data is in ~/Library/Application Support/Margin/."))


class Backups(unittest.TestCase):
    """Backups of the data are guarded too; the verification scripts next to them aren't."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.backups = os.path.join(self.home, "Library", "Application Support", "Margin Backups")
        os.makedirs(os.path.join(self.backups, "2026-01-01 before a change"))
        os.makedirs(os.path.join(self.backups, "verification-scripts"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_copies_are_blocked(self):
        copy = os.path.join(self.backups, "2026-01-01 before a change", "money.db")
        self.assertTrue(decide("Read", home=self.home, file_path=copy))
        self.assertTrue(decide("Bash", home=self.home, command=f"sqlite3 '{copy}' .tables"))
        self.assertTrue(decide("Bash", home=self.home, command=f"find '{self.backups}'"))

    def test_verification_scripts_and_the_listing_work(self):
        script = os.path.join(self.backups, "verification-scripts", "final_check.sh")
        self.assertIsNone(decide("Bash", home=self.home, command=f"sh '{script}'"))
        self.assertIsNone(decide("Bash", home=self.home, command='sh "$HOME/Library/Application Support/'
                                                                  'Margin Backups/verification-scripts/final_check.sh"'))
        self.assertIsNone(decide("Read", home=self.home, file_path=script))
        self.assertIsNone(decide("Bash", home=self.home, command=f"ls '{self.backups}'"))


class AsAHook(unittest.TestCase):
    """Run the way Claude Code runs it: JSON on stdin, exit code 2 and a reason on stderr to refuse."""

    def run_hook(self, event_text):
        env = dict(os.environ, HOME=HOME)
        env.pop("MARGIN_DATA", None)
        return subprocess.run([sys.executable, str(HOOK)], input=event_text, capture_output=True, text=True,
                              env=env, cwd=str(ROOT))

    def test_refuses_with_a_reason(self):
        r = self.run_hook(json.dumps({"tool_name": "Read", "tool_input": {"file_path": DATA + "/money.db"},
                                      "cwd": REPO}))
        self.assertEqual(r.returncode, 2)
        self.assertIn("margin doctor --json", r.stderr)

    def test_lets_others_through_quietly(self):
        r = self.run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": REPO}))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_a_broken_event_that_names_the_folder_is_refused(self):
        r = self.run_hook('{"tool_name": "Read", "tool_input": {"file_path": "' + DATA + '/money.db"')
        self.assertEqual(r.returncode, 2)

    def test_registered_for_every_tool(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        entries = settings["hooks"]["PreToolUse"]
        self.assertTrue(any(e["matcher"] == "*" and any("data_guard.py" in h["command"] for h in e["hooks"])
                            for e in entries))


if __name__ == "__main__":
    unittest.main()
