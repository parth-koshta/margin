"""Bank texts from an Android phone: SMS Backup & Restore files (margin/smsbackup.py), read by the nightly sync, checked
by the doctor and setup, kept out of commits and out of Claude's reach. Every text, sender, number, amount, folder and
date here is made up; the backups are written to temporary folders."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import quoteattr

from margin import config, db, health, install, paths, privacy, smsbackup, sync

IN = dict(sync.DEFAULT_SETTINGS, currency="INR")
US = dict(sync.DEFAULT_SETTINGS, currency="USD", misc_below=3)
DAY = 86400
NOW = time.time()

BANK = "Sent Rs.1500.00 From HDFC Bank A/C *4321 To Arjun Mehta On 06/11/26 Ref 612345678901"
BANK_2 = "Sent Rs.250.00 From HDFC Bank A/C *4321 To DEMO STORE On 07/11/26 Ref 612345678902"
FRIEND = "See you at 7? EMOJI"            # from a person: never read
EPF_OLD = ("Dear XXXXXXXX1111, your passbook balance against ZZDEM**************1234 is Rs. 6,54,321/-. "
           "Contribution of Rs. 12,345/- for due month Jul-24 has been received.")
CHASE = "Chase: A $8.50 debit card transaction to DEMO CAFE on 11/14/2026 exceeded your $0.00 set alert."
SMILE = "&#55357;&#56832;"                # 😀 as two UTF-16 halves, the way some phones back it up


def sms_el(address: str, body: str, when: float, kind: str = "1") -> str:
    return (f'  <sms protocol="0" address={quoteattr(address)} date="{int(when * 1000)}" type="{kind}" '
            f'subject="null" body={quoteattr(body)} toa="null" sc_toa="null" service_center="null" read="1" '
            f'status="-1" locked="0" date_sent="0" sub_id="1" readable_date="" contact_name="(Unknown)" />')


MMS = ('  <mms date="1762300000000" msg_box="1" address="+919999999999" m_type="132" text_only="0">\n'
       '    <parts><part seq="0" ct="image/jpeg" cl="photo.jpg" data="' + "QUJD" * 5000 + '" />\n'
       '    <part seq="1" ct="text/plain" text="Rs.999 debited, a picture" /></parts>\n'
       '    <addrs><addr address="+919999999999" type="137" charset="106" /></addrs>\n  </mms>')


def backup(*elements: str, root: str = "smses", prolog: str = "") -> bytes:
    body = "\n".join(elements)
    return (f"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n<!--File Created By a made-up test-->\n"
            f"{prolog}<{root} count=\"{len(elements)}\" backup_set=\"demo\" backup_date=\"1762300000000\">\n{body}\n"
            f"</{root}>\n").replace("EMOJI", SMILE).replace("CTRL", "&#1;").encode("utf-8")


def mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p


class Reading(Folder):
    def test_only_received_bank_texts(self):
        f = self.write("sms-20261116010203.xml", backup(
            sms_el("JD-HDFCBK-S", BANK, NOW - 2 * DAY),
            sms_el("+919999999999", FRIEND, NOW - DAY),               # a person
            sms_el("JD-HDFCBK-S", BANK_2, NOW - DAY, kind="2"),       # sent, not received
            MMS,
            sms_el("24273", CHASE, NOW - DAY)))                       # a short code: rupees at home, not trusted
        got = list(smsbackup.read(f, short_codes=False))
        self.assertEqual([(m["sender"], m["text"]) for m in got], [("JD-HDFCBK-S", BANK)])
        self.assertAlmostEqual(got[0]["ts_epoch"], int((NOW - 2 * DAY) * 1000) / 1000)
        self.assertEqual(list(smsbackup.read(f, short_codes=False))[0]["id"], got[0]["id"])   # the same each time
        self.assertEqual([m["sender"] for m in smsbackup.read(f, short_codes=True)], ["JD-HDFCBK-S", "24273"])

    def test_emoji_halves_and_control_characters(self):
        f = self.write("sms-1.xml", backup(sms_el("+919999999999", FRIEND, NOW),
                                           sms_el("VM-ICICIT-S", "Thanks EMOJI Rs.10 creditedCTRL", NOW)))
        got = list(smsbackup.read(f, short_codes=False))
        self.assertEqual(got[0]["text"], "Thanks \U0001F600 Rs.10 credited�")

    def test_fix_refs(self):
        self.assertEqual(smsbackup.fix_refs(b"a&#55357;&#56832;b"), b"a&#128512;b")
        self.assertEqual(smsbackup.fix_refs(b"&#xD83D;&#xDE00;"), b"&#128512;")
        self.assertEqual(smsbackup.fix_refs(b"x&#55357;y&#56832;"), b"x&#65533;y&#65533;")   # halves apart
        self.assertEqual(smsbackup.fix_refs(b"&#65;&#55357;&#56832;&#2;&#10;"), b"&#65;&#128512;&#65533;&#10;")
        self.assertEqual(smsbackup.fix_refs(b"&amp;&#8377;"), b"&amp;&#8377;")

    def test_small_chunks_never_split_a_pair(self):
        f = self.write("sms-2.xml", backup(*(sms_el("JD-HDFCBK-S", f"Rs.{i}0 credited EMOJI", NOW) for i in range(1, 30))))
        whole = [m["text"] for m in smsbackup.read(f)]
        for size in (1, 7, 13, 64):
            with mock.patch.object(smsbackup, "CHUNK", size):
                self.assertEqual([m["text"] for m in smsbackup.read(f)], whole)
        self.assertEqual(len(whole), 29)
        self.assertTrue(all("\U0001F600" in t for t in whole))

    def test_zip_and_password_zip(self):
        p = self.dir / "sms-3.xml.zip"
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("sms-3.xml", backup(sms_el("JD-HDFCBK-S", BANK, NOW)))
        self.assertEqual([m["text"] for m in smsbackup.read(p)], [BANK])
        data = bytearray(p.read_bytes())          # the same zip, marked as encrypted (the app's password option)
        for sig, at in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            i = data.find(sig)
            data[i + at] |= 1
        locked = self.write("sms-4.xml.zip", bytes(data))
        with self.assertRaises(smsbackup.Unreadable) as e:
            list(smsbackup.read(locked))
        self.assertIn("password", str(e.exception))
        with self.assertRaises(smsbackup.Unreadable):
            list(smsbackup.read(self.write("sms-5.xml.zip", b"not a zip")))

    def test_other_files_and_unsafe_ones(self):
        calls = self.write("calls-1.xml", backup('  <call number="9999999999" duration="5" date="1" type="1" />',
                                                 root="calls"))
        with self.assertRaises(smsbackup.NotABackup):
            list(smsbackup.read(calls))
        entities = self.write("sms-6.xml", backup(sms_el("JD-HDFCBK-S", "&lol;", NOW),
                                                  prolog='<!DOCTYPE smses [<!ENTITY lol "lol">]>\n'))
        with self.assertRaises(smsbackup.Unreadable):
            list(smsbackup.read(entities))
        broken = self.write("sms-7.xml", backup(sms_el("JD-HDFCBK-S", BANK, NOW))[:-30])
        with self.assertRaises(smsbackup.Unreadable) as e:
            list(smsbackup.read(broken))
        self.assertNotIn("4321", str(e.exception))           # the reason never quotes the file

    def test_finding_files(self):
        self.write("sms-1.xml", backup())
        self.write("sms-2.xml.zip", b"")
        self.write("notes.txt", b"")
        self.write(".sms-hidden.xml", b"")
        (self.dir / "inner").mkdir()
        self.write("inner/sms-9.xml", backup())
        files, notes = smsbackup.find({"sms_backup_folder": str(self.dir)})
        self.assertEqual(sorted(f.name for f in files), ["sms-1.xml", "sms-2.xml.zip"])
        self.assertEqual(notes, [])
        files, notes = smsbackup.find({"sms_backup_folder": str(self.dir / "gone")})
        self.assertEqual(files, [])
        self.assertEqual(len(notes), 1)
        self.assertEqual(smsbackup.find({"sms_backup_folder": ""}), ([], []))


class Syncing(Folder):
    def run_sync(self, conn, settings, seen=None):
        counts, problems = {"email": 0, "sms": 0, "txn": 0, "review": 0}, []
        seen = {r["uid"] for r in conn.execute("SELECT uid FROM seen")} if seen is None else seen
        status = sync.read_text_backups(conn, settings, seen, NOW - settings["backfill_days"] * DAY, counts, problems)
        return status, counts, problems

    def test_new_bank_texts_counted_once_old_ones_only_for_epf(self):
        conn = mem()
        settings = dict(IN, sms_backup_folder=str(self.dir), read_sms=False)
        f = self.write("sms-1.xml", backup(
            sms_el("JD-HDFCBK-S", BANK, NOW - 2 * DAY),
            sms_el("+919999999999", FRIEND, NOW - DAY),
            sms_el("JD-HDFCBK-S", BANK_2, NOW - 800 * DAY),            # before the backfill window
            sms_el("VM-EPFOHO", EPF_OLD, NOW - 800 * DAY)))
        status, counts, problems = self.run_sync(conn, settings)
        self.assertEqual(problems, [])
        self.assertEqual((counts["sms"], counts["txn"], counts.get("epf")), (1, 1, 1))
        self.assertEqual((status["state"], status["files"]), ("ok", 1))
        self.assertEqual(conn.execute("SELECT amount, source FROM transactions").fetchall()[0][:], (1500.0, "sms"))
        self.assertEqual(conn.execute("SELECT balance FROM epf").fetchone()[0], 654321.0)
        everything = json.dumps([list(r) for t in ("transactions", "review", "seen")
                                 for r in conn.execute(f"SELECT * FROM {t}")])
        self.assertNotIn("See you", everything)              # the friend's text went nowhere

        status, counts, _ = self.run_sync(conn, settings)    # unchanged file: not read again
        self.assertEqual(counts["sms"], 0)
        self.assertEqual(status["state"], "ok")

        f.write_bytes(backup(sms_el("JD-HDFCBK-S", BANK, NOW - 2 * DAY),        # an archive backup grew
                             sms_el("JD-HDFCBK-S", BANK_2, NOW - DAY)))
        os.utime(f, (NOW + 5, NOW + 5))
        _, counts, _ = self.run_sync(conn, settings)
        self.assertEqual((counts["sms"], counts["txn"]), (1, 1))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)

    def test_full_backups_overlap(self):
        conn = mem()
        settings = dict(IN, sms_backup_folder=str(self.dir))
        self.write("sms-1.xml", backup(sms_el("JD-HDFCBK-S", BANK, NOW - 2 * DAY)))
        self.write("sms-2.xml", backup(sms_el("JD-HDFCBK-S", BANK, NOW - 2 * DAY),
                                       sms_el("JD-HDFCBK-S", BANK_2, NOW - DAY)))
        _, counts, _ = self.run_sync(conn, settings)
        self.assertEqual((counts["sms"], counts["txn"]), (2, 2))

    def test_states(self):
        conn = mem()
        status, _, problems = self.run_sync(conn, dict(IN, sms_backup_folder=str(self.dir / "gone")))
        self.assertEqual(status["state"], "missing")
        self.assertTrue(problems[0].startswith("Text backups:"))
        self.assertEqual(self.run_sync(conn, dict(IN))[0]["state"], "off")
        self.write("calls-1.xml", backup('  <call number="9999999999" duration="5" date="1" type="1" />', root="calls"))
        self.assertEqual(self.run_sync(conn, dict(IN, sms_backup_folder=str(self.dir)))[0]["state"], "none")
        with mock.patch.object(smsbackup, "find", side_effect=PermissionError):
            status, _, problems = self.run_sync(conn, dict(IN, sms_backup_folder=str(self.dir)))
        self.assertEqual(status["state"], "no_access")
        self.assertIn("Full Disk Access", problems[0])
        self.write("sms-9.xml", b"<smses>" + sms_el("JD-HDFCBK-S", BANK, NOW).encode())   # cut short
        status, _, problems = self.run_sync(conn, dict(IN, sms_backup_folder=str(self.dir)))
        self.assertEqual(status["state"], "unreadable")
        self.assertIn("sms-9.xml", problems[0])
        self.assertNotIn("4321", " ".join(problems))

    def test_short_codes_outside_india(self):
        conn = mem()
        self.write("sms-1.xml", backup(sms_el("24273", CHASE, NOW - DAY)))
        _, counts, _ = self.run_sync(conn, dict(US, sms_backup_folder=str(self.dir)))
        self.assertEqual(counts["txn"], 1)


class DoctorAndSetup(unittest.TestCase):
    def test_doctor(self):
        self.assertIsNone(health.check_texts_backup(IN, None))
        self.assertIsNone(health.check_texts_backup(IN, {"state": "off", "files": 0, "newest": None}))
        folder = dict(IN, sms_backup_folder="~/Demo Drive/Backups")
        self.assertTrue(health.check_texts_backup(folder, None)["ok"])                  # not synced yet
        self.assertTrue(health.check_texts_backup(folder, {"state": "off", "files": 0, "newest": None})["ok"])
        today = time.strftime("%Y-%m-%d")
        ok = health.check_texts_backup(folder, {"state": "ok", "files": 2, "newest": today})
        self.assertTrue(ok["ok"])
        self.assertIn("2 files", ok["text"])
        for seen in ({"state": "no_access"}, {"state": "missing"}, {"state": "none", "files": 0},
                     {"state": "unreadable", "files": 1, "newest": today},
                     {"state": "ok", "files": 1, "newest": "2026-01-02"}):
            c = health.check_texts_backup(folder, seen)
            self.assertFalse(c["ok"], seen)
            self.assertTrue(c.get("fix"), seen)
        # Backups dropped into the statements folder, no folder set: still checked.
        self.assertTrue(health.check_texts_backup(IN, {"state": "ok", "files": 1, "newest": today})["ok"])

    def test_setup_step(self):
        with mock.patch.object(config, "read", return_value=dict(IN, read_sms=False, sms_backup_folder="~/Demo")), \
                mock.patch.object(health, "last_sync", return_value=None):
            s = install.step_sms()
        self.assertEqual(s.status, "ok")
        seen = {"texts_backup": {"state": "no_access", "files": 0, "newest": None}}
        with mock.patch.object(config, "read", return_value=dict(IN, read_sms=False, sms_backup_folder="~/Demo")), \
                mock.patch.object(health, "last_sync", return_value=seen):
            s = install.step_sms()
        self.assertEqual(s.status, "todo")
        self.assertIn("SMS Backup & Restore", s.how)
        self.assertIn(health.tilde(health.python()), s.how)
        self.assertIn("Android", install.SMS_HOW)

    def test_walking_through_android(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / "Demo Drive" / "Text Backups"
            folder.mkdir(parents=True)
            dragged = str(folder).replace(" ", "\\ ") + " "          # what dragging a folder into Terminal types
            with mock.patch.object(install, "_ask", side_effect=["a", dragged]), mock.patch("builtins.print"):
                s = install.walk_sms(install.Step("sms", "you", "todo", "Bank texts", "how"))
            saved = json.loads(paths.SETTINGS_PATH.read_text())
            self.assertEqual((s.status, saved["sms_backup_folder"], saved["read_sms"]), ("ok", str(folder), False))
            config.set_value("sms_backup_folder", '""')
            config.set_value("read_sms", "true")


class Settings(unittest.TestCase):
    def test_folder_is_personal_and_sane(self):
        self.assertIn("sms_backup_folder", config.PERSONAL)
        self.assertEqual(config.parse_value("sms_backup_folder", "~/Demo Drive/Backups"), "~/Demo Drive/Backups")
        for bad in ("/", "~", "~/", os.path.expanduser("~"), str(paths.PROJECT_DIR.parent)):
            with self.assertRaises(config.Refused, msg=bad):
                config.parse_value("sms_backup_folder", bad)

    def test_backups_never_committed(self):
        for name in ("sms-20261116010203.xml", "drive/sms-1.xml", "sms-1.xml.zip", "backup.xml.zip"):
            self.assertTrue(privacy.NEVER_FILES.search(name), name)
        self.assertFalse(privacy.NEVER_FILES.search("tests/test_android.py"))


_spec = importlib.util.spec_from_file_location("data_guard", paths.PROJECT_DIR / ".claude" / "hooks" / "data_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class DataGuard(unittest.TestCase):
    """The folder named in sms_backup_folder holds every text on the phone: Claude can't read it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self.tmp.name)
        self.data = Path(self.home, "Library", "Application Support", "Margin")
        self.data.mkdir(parents=True)
        self.drive = Path(self.home, "Demo Drive", "Text Backups")

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, tool, **tool_input):
        return guard.decide({"tool_name": tool, "tool_input": tool_input, "cwd": str(paths.PROJECT_DIR)},
                            home=self.home, env={})

    def settings(self, folder):
        (self.data / "settings.json").write_text(json.dumps({"sms_backup_folder": folder}))

    def test_backup_folder_blocked(self):
        f = str(self.drive / "sms-20261116010203.xml")
        self.assertIsNone(self.decide("Read", file_path=f))
        self.settings("~/Demo Drive/Text Backups")
        self.assertIn("text-message backups", self.decide("Read", file_path=f))
        self.assertTrue(self.decide("Bash", command="head ~/Demo\\ Drive/Text\\ Backups/sms-1.xml"))
        self.assertTrue(self.decide("Grep", pattern="HDFC", path=str(Path(self.home, "Demo Drive"))))
        self.assertIsNone(self.decide("Read", file_path=str(Path(self.home, "Demo Drive", "notes.txt"))))

    def test_folders_too_big_to_guard_are_ignored(self):
        for folder in ("~", "/", self.home, str(paths.PROJECT_DIR.parent)):
            self.settings(folder)
            self.assertIsNone(self.decide("Read", file_path=str(paths.PROJECT_DIR / "README.md")), folder)
        (self.data / "settings.json").write_text("not json")
        self.assertIsNone(self.decide("Read", file_path=str(paths.PROJECT_DIR / "README.md")))


if __name__ == "__main__":
    unittest.main()
