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
                    "margin rule acme Shopping", 'margin trust "mybank.example"', "margin setup-gmail ~/Downloads/x.json",
                    "margin setup-ai --provider codex", "margin setup-ai --provider ollama --model demo-model",
                    "margin setup-ai", "margin config get ai"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd))

    def test_the_demo_and_temporary_folders(self):
        self.assertIsNone(bash("ls ~/Library/Caches/Margin\\ Demo/us"))
        self.assertIsNone(decide("Read", file_path=HOME + "/Library/Caches/Margin Demo/us/settings.json"))
        self.assertIsNone(decide("mcp__browser__navigate", url="http://demo.margin.localhost:8765/"))
        tmp = tempfile.gettempdir() + "/margin-tests-x"
        self.assertIsNone(decide("Read", file_path=tmp + "/money.db", env={"MARGIN_DATA": tmp}))

    def test_the_ai_start_folder(self):
        # Margin starts the AI CLIs in an empty folder in the temporary folder, outside the data folder.
        start = os.path.join(tempfile.gettempdir(), f"margin-ai-{os.getuid()}")
        self.assertIsNone(decide("Read", file_path=start + "/notes.txt"))
        self.assertIsNone(bash(f"ls '{start}'"))
        self.assertFalse(any(guard._inside(guard._real(start), root) for root, _ in guard.protected(HOME, {})))

    def test_file_tools_in_the_repo(self):
        self.assertIsNone(decide("Read", file_path=REPO + "/AGENTS.md"))
        self.assertIsNone(decide("Grep", pattern="DATA_DIR"))
        self.assertIsNone(decide("Glob", pattern="**/*.py"))
        # Docs, code and tests in this repository may name the folder in what they write.
        self.assertIsNone(decide("Edit", file_path=str(ROOT / "AGENTS.md"), old_string="x",
                                 new_string="Your data is in ~/Library/Application Support/Margin/."))
        self.assertIsNone(decide("MultiEdit", file_path=str(ROOT / "README.md"),
                                 edits=[{"old_string": "x", "new_string": "~/Library/Messages/chat.db"}]))


class EveryTool(unittest.TestCase):
    """Any tool's input, not just the known ones: paths, commands, URLs and the folder named in text."""

    def test_command_fields_of_other_tools(self):
        cat = "cat ~/Library/Application\\ Support/Margin/settings.json"
        self.assertTrue(decide("mcp__terminal__run_in_terminal", command=cat, cwd=REPO))
        self.assertTrue(decide("Monitor", command="tail -f ~/Library/Messages/chat.db"))
        self.assertTrue(decide("mcp__terminal__run_in_terminal", command="./margin-cli tidy"))
        self.assertTrue(decide("mcp__computer-use__type", text="sqlite3 ~/Library/Application\\ Support/Margin/money.db"))
        self.assertTrue(decide("mcp__browser__javascript_tool", action="javascript_exec",
                               text="await fetch('http://margin.localhost/').then(r => r.text())"))

    def test_paths_anywhere_in_the_input(self):
        self.assertTrue(decide("Artifact", file_path=REPO + "/page.html",
                               files={"data.json": {"from": DATA + "/settings.json"}}))
        self.assertTrue(decide("mcp__drive__upload", local_path=DATA + "/statements/x.csv"))
        self.assertTrue(decide("mcp__x__anything", options={"nested": [{"where": DATA + "/rules.json"}]}))

    def test_the_folder_named_in_any_text(self):
        self.assertTrue(decide("Agent", prompt="Count the rows in ~/Library/Application Support/Margin/money.db"))
        self.assertTrue(decide("Write", file_path="/tmp/peek.py",
                               content="import sqlite3\nsqlite3.connect('/Users/you/Library/Application Support/Margin/money.db')\n"))
        self.assertTrue(decide("Edit", file_path="/tmp/peek.sh", old_string="x", new_string="ls ~/Library/Messages/"))
        self.assertIn("as text", decide("Grep", pattern="Application Support/Margin") or "")

    def test_the_data_volume_spelling(self):
        env = {"MARGIN_DATA": "/Users/you/Money"}
        self.assertTrue(decide("Read", file_path="/System/Volumes/Data/Users/you/Money/money.db", env=env))
        self.assertTrue(bash("cat /System/Volumes/Data/Users/you/Money/rules.json", env=env))
        self.assertTrue(guard.reaches(guard.expand("/System/Volumes/Data" + DATA + "/x", REPO, HOME, {}),
                                      guard.protected(HOME, {}), False))

    def test_harmless_calls_from_other_tools(self):
        self.assertIsNone(decide("mcp__terminal__run_in_terminal", command="git status"))
        self.assertIsNone(decide("mcp__browser__navigate", url="http://demo.margin.localhost:8765/"))
        self.assertIsNone(decide("mcp__ccd_turn__update_plan", items=[{"content": "Run the tests", "status": "pending"}]))


class Loopholes(unittest.TestCase):
    def test_python_needs_margin_data_itself_set_elsewhere(self):
        for cmd in ("MARGIN_DEMO_DATA=/tmp/x python3 -c 'from margin import db; db.connect()'",
                    "python3 -c 'from margin import db  # MARGIN_DATA'",
                    "python3 -c 'from margin import db; import tests'",
                    "python3 -c 'import os, margin.db'",
                    "python3 -c '__import__(\"margin.db\")'",
                    "python3 -m margin.sync",
                    "PYTHONPATH=. /usr/bin/python3 -I -m margin.tidy",
                    "MARGIN_DATA=$UNKNOWN python3 -m margin.sync",
                    'MARGIN_DATA="$HOME/Library/Application Support/Margin" python3 -m margin.sync'):
            with self.subTest(cmd=cmd):
                self.assertTrue(bash(cmd))
        for cmd in ("MARGIN_DATA=$(mktemp -d) python3 -m margin.sync",
                    'MARGIN_DATA="$(mktemp -d)" python3 -c "from margin import db"',
                    "MARGIN_DATA=/tmp/margin-try python3 -c 'from margin import parse'",
                    'MARGIN_DATA="$HOME/Library/Caches/Margin Demo/us" python3 -c "from margin import db"',
                    "python3 -c 'import tests; from margin import parse'"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(bash(cmd))

    def test_margin_commands_anywhere_in_a_command(self):
        for cmd in ("sh margin-cli tidy", "bash ./margin-cli open --print-url", "sh -c 'margin tidy'",
                    "bash -lc \"cd ~/projects/margin && ./margin-cli setup-pan\"", "eval margin config get x --show",
                    "true; /usr/bin/env margin tidy"):
            with self.subTest(cmd=cmd):
                self.assertTrue(bash(cmd))

    def test_a_recursive_search_from_above_the_folder_without_a_path(self):
        for cmd in ("cd ~ && grep -r ACME", "cd ~/Library && rg ACME", "cd ~ && find -name '*.db'"):
            with self.subTest(cmd=cmd):
                self.assertTrue(bash(cmd))
        self.assertTrue(bash("rg ACME", cwd=HOME))
        self.assertIsNone(bash("cd ~/projects/margin && grep -r ACME"))
        with tempfile.TemporaryDirectory() as home:
            home = os.path.realpath(home)
            os.makedirs(os.path.join(home, "projects"))
            self.assertIsNone(bash("cd ~ && grep -r ACME projects", home=home))


class AsksFirst(unittest.TestCase):
    """Changing the guard, or the settings that switch it on, waits for the person's yes."""

    def ask(self, tool, **tool_input):
        return guard.approval({"tool_name": tool, "tool_input": tool_input, "cwd": str(ROOT)}, home=HOME, env={})

    def test_edits_to_the_guard_and_settings(self):
        self.assertTrue(self.ask("Edit", file_path=str(HOOK), old_string="a", new_string="b"))
        self.assertTrue(self.ask("Write", file_path=str(ROOT / ".claude" / "settings.json"), content="{}"))
        self.assertTrue(self.ask("Write", file_path=str(ROOT / ".claude" / "settings.local.json"),
                                 content='{"disableAllHooks": true}'))
        self.assertTrue(self.ask("Write", file_path=HOME + "/.claude/settings.json", content="{}"))
        self.assertTrue(self.ask("Bash", command="sed -i '' 's/2/0/' .claude/hooks/data_guard.py"))
        self.assertTrue(self.ask("Bash", command="rm .claude/settings.json"))
        self.assertTrue(self.ask("Bash", command="echo '{}' > .claude/settings.local.json"))
        self.assertTrue(self.ask("Bash", command="git checkout -- .claude/hooks"))

    def test_reading_them_is_fine(self):
        self.assertIsNone(self.ask("Read", file_path=str(HOOK)))
        self.assertIsNone(self.ask("Bash", command="cat .claude/settings.json && git diff .claude/hooks"))
        self.assertIsNone(self.ask("Bash", command="python3 -m unittest tests.test_data_guard"))
        self.assertIsNone(self.ask("Edit", file_path=str(ROOT / "README.md"), old_string="a", new_string="b"))


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

    def test_a_broken_event_is_refused(self):
        r = self.run_hook('{"tool_name": "Read", "tool_input": {"file_path": "' + REPO + '/README.md"')
        self.assertEqual(r.returncode, 2)
        self.assertIn("refused it to be safe", r.stderr)

    def test_asks_through_claude_code(self):
        r = self.run_hook(json.dumps({"tool_name": "Edit", "cwd": str(ROOT),
                                      "tool_input": {"file_path": str(HOOK), "old_string": "a", "new_string": "b"}}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_registered_for_every_tool(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
        entries = settings["hooks"]["PreToolUse"]
        self.assertTrue(any(e["matcher"] == "*" and any("data_guard.py" in h["command"] for h in e["hooks"])
                            for e in entries))


if __name__ == "__main__":
    unittest.main()
