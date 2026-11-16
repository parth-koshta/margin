"""margin config, doctor --json, setup and update, on temporary folders with made-up data.

Nothing here touches launchd, the Keychain, Gmail or the real data folder: those are replaced with stand-ins.
"""
import tests  # noqa: F401  (temporary data folder first)

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from margin import config, db, health, install, paths


def _clean_data():
    for p in (paths.SETTINGS_PATH, paths.RULES_PATH, paths.DB_PATH):
        if p.exists():
            p.unlink()


def _out(fn, *args, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args, **kw)
    return rc, buf.getvalue()


NO_KEYCHAIN = mock.patch("margin.keychain.get_secret", return_value=None)
NOT_LOADED = mock.patch.object(health, "_loaded", return_value=False)
NOT_SYNCING = mock.patch.object(health, "sync_running", return_value=False)


class Config(unittest.TestCase):
    def setUp(self):
        _clean_data()

    def cfg(self, *argv):
        a = argparse.Namespace(action=argv[0], key=argv[1] if len(argv) > 1 else None,
                               value=argv[2] if len(argv) > 2 else None, show="--show" in argv)
        return _out(config.main, a)

    def test_set_and_get(self):
        self.assertEqual(self.cfg("set", "currency", "usd")[0], 0)
        self.assertEqual(self.cfg("set", "read_sms", "no")[0], 0)
        self.assertEqual(self.cfg("set", "misc_below", "3")[0], 0)
        self.assertEqual(self.cfg("set", "extra_categories", '{"out": ["Pets"], "in": []}')[0], 0)
        saved = json.loads(paths.SETTINGS_PATH.read_text())
        self.assertEqual((saved["currency"], saved["read_sms"], saved["misc_below"]), ("USD", False, 3))
        self.assertEqual(saved["extra_categories"]["out"], ["Pets"])
        self.assertIn("backfill_days", saved)                 # the other settings are written with their defaults
        self.assertEqual(paths.SETTINGS_PATH.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cfg("get", "currency")[1].strip(), '"USD"')

    def test_wrong_values_are_refused_and_nothing_changes(self):
        self.cfg("set", "currency", "EUR")
        before = paths.SETTINGS_PATH.read_text()
        for key, value in (("currency", "XYZ"), ("read_sms", "maybe"), ("lock_minutes", "-5"),
                           ("misc_below", "lots"), ("extra_categories", "[1]"), ("colour", "blue")):
            with self.subTest(key=key):
                self.assertEqual(self.cfg("set", key, value)[0], 1)
        self.assertEqual(paths.SETTINGS_PATH.read_text(), before)

    def test_secrets_are_refused_with_the_command_to_use(self):
        for key, command in (("pan", "setup-pan"), ("pran", "setup-pran"), ("ppf_statement_password", "setup-ppf"),
                             ("gmail_token", "setup-gmail")):
            with self.subTest(key=key):
                rc, out = self.cfg("set", key, "x")
                self.assertEqual(rc, 1)
                self.assertIn(command, out)
        rc, out = self.cfg("set", "about_me", '["My PAN is ABCPE1234F"]')   # privacy: ok (made-up PAN shape)
        self.assertEqual(rc, 1)
        self.assertIn("Keychain", out)
        self.assertFalse(paths.SETTINGS_PATH.exists())

    def test_no_real_setting_looks_like_a_secret(self):
        for key in config.known():
            with self.subTest(key=key):
                self.assertIsNone(config.SECRET_KEY.search(key))
                self.assertEqual(bool(config.PROGRAM_KEY.search(key)), key == "claude_path")

    def test_programs_are_set_by_hand_only(self):
        for key, value in (("claude_path", "/tmp/not-claude.sh"), ("fx_rates", '{"command": "x"}'),
                           ("extra_categories", '{"out": [], "in": [], "hook": "x"}')):
            with self.subTest(key=key):
                rc, out = self.cfg("set", key, value)
                self.assertEqual(rc, 1)
                self.assertIn("by hand", out)
        self.assertFalse(paths.SETTINGS_PATH.exists())

    def test_personal_settings_are_shown_only_as_counts(self):
        self.cfg("set", "self_names", "DEMO NAME")            # a plain string becomes a one-item list
        self.cfg("set", "about_me", '["Money from friends is usually a split bill", "Rent goes to Some One"]')
        rc, out = self.cfg("get")
        self.assertNotIn("DEMO NAME", out)
        self.assertNotIn("Some One", out)
        self.assertIn("about_me: set, 2 entries", out)
        self.cfg("set", "trusted_senders", '["mybank.example"]')
        self.cfg("set", "extra_categories", '{"out": ["Pets"], "in": ["Freelance", "Rent"]}')
        rc, out = self.cfg("get")
        self.assertNotIn("mybank.example", out)
        self.assertNotIn("Pets", out)
        self.assertIn("trusted_senders: set, 1 entry", out)
        self.assertIn("extra_categories: set, 3 entries", out)
        self.assertIn("DEMO NAME", self.cfg("get", "self_names", "--show")[1])

    def test_get_writes_nothing(self):
        self.cfg("get")
        self.assertFalse(paths.SETTINGS_PATH.exists())


class Doctor(unittest.TestCase):
    PAYEE, AMOUNT = "ACME CORNER STORE", 4321.5

    def setUp(self):
        _clean_data()
        conn = db.connect()
        conn.execute("INSERT INTO transactions (uid, source, ts, amount, direction, account, counterparty, category)"
                     " VALUES ('sms:demo:1', 'sms', datetime('now'), ?, 'out', 'DEMO card ••1234', ?, "
                     "'Other spending')", (self.AMOUNT, self.PAYEE))
        status = {"at": "2026-01-02T00:01:00", "ok": False, "counts": {},
                  "problems": [f"Statements: {self.PAYEE}.csv: no rows with a value", "Gmail (partial): 3 emails",
                               f"Investments, prices: No price yet for {self.PAYEE}"], "sms": "off"}
        db.set_state(conn, "last_sync", json.dumps(status))
        conn.commit()
        conn.close()
        paths.RULES_PATH.write_text(json.dumps([{"match": "acme", "category": "Shopping"}]))

    def report(self):
        with NO_KEYCHAIN, NOT_LOADED, NOT_SYNCING, mock.patch("margin.gmail.is_configured", return_value=False):
            return _out(health.main, True, "/nonexistent/Margin.app")

    def test_json_has_counts_and_statuses_only(self):
        rc, out = self.report()
        self.assertEqual(rc, 1)
        for private in (self.PAYEE, "ACME", "4321", "1234"):
            self.assertNotIn(private, out)
        r = json.loads(out)
        self.assertEqual(r["counts"]["transactions"], 1)
        self.assertEqual(r["counts"]["uncategorized_last_year"], 1)
        self.assertEqual(r["counts"]["rules"], 1)
        last = next(c for c in r["checks"] if c["id"] == "last_sync")
        self.assertEqual(last["problems"], 3)
        self.assertEqual(last["problem_areas"], {"Statements": 1, "Gmail": 1, "Investments": 1})
        self.assertEqual(r["secrets_saved"], {"gmail": False, "pan": False, "pran": False, "ppf_statement": False})
        self.assertTrue(all("text" not in c for c in r["checks"]))
        self.assertEqual(next(c for c in r["checks"] if c["id"] == "gmail")["ok"], False)

    def test_text_is_for_people(self):
        with NO_KEYCHAIN, NOT_LOADED, mock.patch("margin.gmail.is_configured", return_value=False), \
                mock.patch.object(health, "person_watching", return_value=True):
            rc, out = _out(health.main, False, "/nonexistent/Margin.app")
        self.assertEqual(rc, 1)
        self.assertTrue(all(line.startswith(("✓", "✗", "    ·")) for line in out.splitlines()))
        self.assertIn("✗ Gmail not connected", out)
        self.assertIn(self.PAYEE, out)                       # a person at their own terminal sees the details

    def test_text_for_an_agent_or_a_pipe_counts_problems(self):
        with NO_KEYCHAIN, NOT_LOADED, mock.patch("margin.gmail.is_configured", return_value=False):
            rc, out = _out(health.main, False, "/nonexistent/Margin.app")
        self.assertNotIn(self.PAYEE, out)
        self.assertIn("3 problems (Statements 1, Gmail 1, Investments 1)", out)
        line = f"2026-01-02 00:01:00 sync with problems in 9s: 3 emails | Statements: {self.PAYEE}.csv: no rows"
        self.assertNotIn(self.PAYEE, health.screen_line(line))
        self.assertIn("1 problem (margin doctor", health.screen_line(line))

    def test_json_shows_the_home_folder_as_a_tilde(self):
        rc, out = self.report()
        self.assertNotIn(str(Path.home()) + "/", out)

    def test_a_mac_without_a_data_folder(self):
        missing = Path(tempfile.mkdtemp()) / "not-yet"
        names = ("DATA_DIR", "DB_PATH", "SETTINGS_PATH", "RULES_PATH", "PROGRESS_PATH")
        with contextlib.ExitStack() as stack:
            for n in names:
                stack.enter_context(mock.patch.object(paths, n, missing / getattr(paths, n).name
                                                      if n != "DATA_DIR" else missing))
            rc, out = self.report()
        self.assertEqual(rc, 1)
        self.assertFalse(json.loads(out)["checks"][0]["ok"])
        self.assertFalse(missing.exists())                   # doctor never creates or writes anything


class Setup(unittest.TestCase):
    def setUp(self):
        _clean_data()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.calls = []

    def args(self, **kw):
        return argparse.Namespace(**{"json": True, "dry_run": False, "no_sync": True, "yes": True, **kw})

    def run_setup(self, a=None, mac_ok=True, terminal=False):
        mac = install.Step("mac", "margin", "ok", "This Mac") if mac_ok else \
            install.Step("mac", "you", "todo", "This Mac is missing the Xcode command line tools", "xcode-select")
        app = lambda: self.calls.append("app") or 0          # noqa: E731
        schedule = lambda: self.calls.append("schedule") or 0     # noqa: E731
        with NO_KEYCHAIN, NOT_LOADED, NOT_SYNCING, \
                mock.patch.object(install, "step_mac", return_value=mac), \
                mock.patch.object(install, "_terminal", return_value=terminal), \
                mock.patch.object(install, "BIN_DIR", self.tmp / "bin"), \
                mock.patch.object(install, "_rc_file", return_value=self.tmp / ".zshrc"), \
                mock.patch.object(install, "_helpers", return_value=[]), \
                mock.patch("margin.gmail.is_configured", return_value=False):
            return _out(install.setup, a or self.args(), app_path=str(self.tmp / "Margin.app"), install_app=app,
                        install_schedule=schedule, secret_prompts={})

    def assert_nothing_changed(self):
        self.assertEqual(self.calls, [])
        self.assertFalse(paths.SETTINGS_PATH.exists())
        self.assertFalse((self.tmp / "bin").exists())
        self.assertFalse((self.tmp / ".zshrc").exists())

    def test_does_its_part_and_waits_for_the_persons(self):
        rc, out = self.run_setup()
        r = json.loads(out)
        self.assertEqual(rc, install.EXIT_WAITING)
        self.assertEqual(r["waiting_on_you"], ["gmail", "sms"])
        self.assertFalse(r["changed_nothing"])
        status = {s["id"]: s["status"] for s in r["steps"]}
        self.assertEqual((status["command"], status["data"], status["schedule"], status["app"]),
                         ("done", "done", "done", "done"))
        self.assertEqual(self.calls, ["schedule", "app"])
        self.assertTrue(paths.SETTINGS_PATH.exists())
        self.assertEqual(os.path.realpath(self.tmp / "bin" / "margin"), str(paths.PROJECT_DIR / "margin-cli"))
        self.assertIn("Cmd-Shift-G", next(s for s in r["steps"] if s["id"] == "sms")["how"])

    def test_without_a_yes_it_only_reports(self):
        rc, out = self.run_setup(self.args(yes=False))
        r = json.loads(out)
        self.assertEqual(rc, install.EXIT_WAITING)
        self.assertTrue(r["needs_yes"] and r["changed_nothing"])
        changes = " ".join(r["would_change"])
        for what in ("link", "PATH", "data folder", "LaunchAgent", "Margin app"):
            self.assertIn(what, changes)
        self.assert_nothing_changed()

    def test_safe_to_run_again(self):
        self.run_setup()
        rc, out = self.run_setup(self.args(yes=False))
        r = json.loads(out)
        status = {s["id"]: s["status"] for s in r["steps"]}
        self.assertEqual((status["command"], status["data"]), ("ok", "ok"))
        self.assertEqual((self.tmp / ".zshrc").read_text().count(".local/bin"), 1)

    def test_texts_off_means_only_gmail_waits(self):
        config.set_value("read_sms", "false")
        config.set_value("currency", "USD")
        r = json.loads(self.run_setup()[1])
        self.assertEqual(r["waiting_on_you"], ["gmail"])
        self.assertEqual(next(s for s in r["steps"] if s["id"] == "statements")["status"], "skipped")

    def test_dry_run_changes_nothing(self):
        rc, out = self.run_setup(self.args(json=False, dry_run=True, no_sync=False, yes=True))
        self.assertIn("Would", out)
        self.assertIn("Dry run: nothing was changed.", out)
        self.assert_nothing_changed()

    def test_in_their_own_terminal_it_asks_then_waits_for_them(self):
        answers = iter(["", "", "n"])       # go ahead: yes; Gmail: later; bank texts: they don't reach this Mac
        with mock.patch.object(install, "_ask", side_effect=lambda prompt: next(answers)):
            rc, out = self.run_setup(self.args(json=False, yes=False), terminal=True)
        self.assertIn("Margin setup will:", out)
        self.assertEqual(rc, install.EXIT_WAITING)
        self.assertIn("Waiting on you: gmail.", out)
        self.assertIs(json.loads(paths.SETTINGS_PATH.read_text())["read_sms"], False)

    def test_in_their_own_terminal_no_means_no(self):
        with mock.patch.object(install, "_ask", return_value="n"):
            rc, out = self.run_setup(self.args(json=False, yes=False), terminal=True)
        self.assertIn("Nothing was changed", out)
        self.assert_nothing_changed()

    def test_stops_when_the_mac_needs_the_command_line_tools(self):
        rc, out = self.run_setup(mac_ok=False)
        r = json.loads(out)
        self.assertEqual((rc, r["waiting_on_you"], len(r["steps"])), (install.EXIT_WAITING, ["mac"], 1))
        self.assertEqual(self.calls, [])

    def test_leaves_a_stranger_file_alone(self):
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "margin").write_text("#!/bin/sh\n")
        status = {s["id"]: s["status"] for s in json.loads(self.run_setup()[1])["steps"]}
        self.assertEqual(status["command"], "skipped")
        self.assertEqual((self.tmp / "bin" / "margin").read_text(), "#!/bin/sh\n")

    def test_shows_the_home_folder_as_a_tilde(self):
        self.assertNotIn(str(Path.home()) + "/", self.run_setup(self.args(yes=False))[1])


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=Demo Name", "-c", "user.email=demo@example.com",
                        *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return r.stdout.strip()


class Update(unittest.TestCase):
    """A made-up Margin repository and its 'GitHub' (a bare repository in a temporary folder)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin, self.copy, self.other = self.tmp / "origin.git", self.tmp / "copy", self.tmp / "other"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.other)], check=True, capture_output=True)
        self.commit(self.other, "first")
        _git(self.other, "push", "-q", "origin", "HEAD:main")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.copy)], check=True, capture_output=True)
        self.calls = []

    def commit(self, repo, name, push=False):
        (repo / f"{name}.txt").write_text(name + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", name)
        if push:
            _git(repo, "push", "-q", "origin", "HEAD:main")
        return _git(repo, "rev-parse", "HEAD")

    def head(self):
        return _git(self.copy, "rev-parse", "HEAD")

    def update(self, tests_pass=True, finishing=None, terminal=False, answer="", **kw):
        a = argparse.Namespace(**{"check": False, "force": False, "to": None, **kw})
        env = {install.FINISH_ENV: finishing} if finishing else {}
        with mock.patch.object(paths, "PROJECT_DIR", self.copy), NOT_SYNCING, NOT_LOADED, \
                mock.patch.object(paths, "LAUNCHD_PLIST", self.tmp / "none.plist"), \
                mock.patch.object(paths, "WEB_PLIST", self.tmp / "none.web.plist"), \
                mock.patch.object(install, "run_tests", return_value=(tests_pass, "FAILED (failures=1)")), \
                mock.patch.object(install, "step_helpers", return_value=install.Step("helpers", "margin", "ok", "ok")), \
                mock.patch.object(install, "_terminal", return_value=terminal), \
                mock.patch.object(install, "_ask", return_value=answer), \
                mock.patch.dict(os.environ, env), mock.patch("os.chdir"), \
                mock.patch("os.execve", side_effect=lambda exe, argv, env: self.calls.append((argv, env))):
            return _out(install.update, a, app_path=str(self.tmp / "none.app"),
                        install_app=lambda: self.calls.append("app") or 0,
                        install_schedule=lambda: self.calls.append("schedule") or 0)

    def test_up_to_date(self):
        rc, out = self.update()
        self.assertEqual(rc, 0)
        self.assertIn("up to date", out)

    def test_check_lists_whats_coming_and_changes_nothing(self):
        old = self.head()
        self.commit(self.other, "second")
        new = self.commit(self.other, "third", push=True)
        rc, out = self.update(check=True)
        self.assertEqual(rc, 0)
        self.assertIn("2 new changes", out)
        self.assertIn(" second", out)
        self.assertIn(" third", out)
        self.assertIn(f"margin update --to {new[:12]}", out)
        self.assertEqual(self.head(), old)

    def test_without_a_go_ahead_nothing_changes(self):
        old = self.head()
        self.commit(self.other, "second", push=True)
        rc, out = self.update()
        self.assertEqual(rc, install.EXIT_WAITING)
        self.assertIn("Nothing changed", out)
        self.assertEqual(self.head(), old)
        rc, out = self.update(terminal=True, answer="n")      # the person says no in their own Terminal
        self.assertEqual(self.head(), old)
        self.assertEqual(self.calls, [])

    def test_installs_exactly_what_was_shown_then_finishes_on_the_new_code(self):
        old = self.head()
        shown = self.commit(self.other, "second", push=True)
        self.commit(self.other, "arrived later", push=True)  # after the person saw the list
        rc, out = self.update(to=shown[:12])
        self.assertEqual(self.head(), shown)
        argv, env = self.calls[-1]
        self.assertEqual(argv[-3:], ["-m", "margin", "update"])
        self.assertEqual(env[install.FINISH_ENV], old)
        rc, out = self.update(finishing=old)                   # what the fresh process does
        self.assertEqual(rc, 0)
        self.assertIn("Tests passed", out)
        self.assertIn("Your data wasn't touched", out)

    def test_a_yes_in_their_own_terminal(self):
        new = self.commit(self.other, "second", push=True)
        self.update(terminal=True, answer="y")
        self.assertEqual(self.head(), new)

    def test_the_second_half_only_right_after_the_first(self):
        old = self.head()
        self.commit(self.other, "second", push=True)
        rc, out = self.update(finishing=old)                   # nothing was fast-forwarded by this run
        self.assertEqual(rc, 1)
        self.assertIn("only runs as the second half", out)
        self.update(to="origin/main")
        rc, out = self.update(finishing="0" * 40)              # not where HEAD came from
        self.assertEqual(rc, 1)
        self.assertNotIn("app", self.calls)

    def test_failing_tests_go_back(self):
        old = self.head()
        self.commit(self.other, "broken", push=True)
        self.update(to="origin/main")
        rc, out = self.update(tests_pass=False, finishing=old)
        self.assertEqual(rc, 1)
        self.assertIn("went back", out)
        self.assertEqual(self.head(), old)
        self.assertNotIn("app", self.calls)

    def test_only_commits_from_upstream(self):
        old = self.head()
        stray = self.commit(self.other, "never pushed")
        rc, out = self.update(to=stray[:12])
        self.assertEqual(rc, 1)
        self.assertIn("isn't a commit on", out)
        self.assertEqual(self.head(), old)

    def test_never_merges(self):
        mine = self.commit(self.copy, "local change")
        self.commit(self.other, "upstream change", push=True)
        rc, out = self.update(to="origin/main")
        self.assertEqual(rc, 1)
        self.assertIn("fast-forward", out)
        self.assertEqual(self.head(), mine)

    def test_waits_for_a_running_sync(self):
        with mock.patch.object(health, "sync_running", return_value=True):
            a = argparse.Namespace(check=False, force=False, to=None)
            with mock.patch.object(paths, "PROJECT_DIR", self.copy):
                rc, out = _out(install.update, a, app_path="", install_app=None, install_schedule=None)
        self.assertEqual(rc, 1)
        self.assertIn("sync is running", out)

    def test_tests_never_see_the_real_folder(self):
        seen = {}

        def fake_run(cmd, cwd, env, **kw):
            seen.update(env)
            return subprocess.CompletedProcess(cmd, 0, "", "OK")
        with mock.patch("subprocess.run", side_effect=fake_run), mock.patch.dict(os.environ, {install.FINISH_ENV: "x"}):
            ok, _ = install.run_tests()
        self.assertTrue(ok)
        self.assertNotEqual(seen["MARGIN_DATA"], str(paths.DATA_DIR))
        self.assertTrue(seen["MARGIN_DATA"].startswith(os.path.realpath(tempfile.gettempdir()))
                        or seen["MARGIN_DATA"].startswith(tempfile.gettempdir()))
        self.assertNotIn(install.FINISH_ENV, seen)


if __name__ == "__main__":
    unittest.main()
