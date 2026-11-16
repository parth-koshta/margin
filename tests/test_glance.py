"""The menu bar panel's figures (/api/glance), on made-up payments. No real data."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import io
import json
import sqlite3
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from unittest import mock

from margin import db, glance, lock, server

TODAY = date(2026, 11, 16)


def mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def pay(conn, ts, amount, direction="out", category="Food & dining"):
    conn.execute("INSERT INTO transactions(uid, ts, amount, direction, category, source) VALUES(?,?,?,?,?,?)",
                 (f"t{conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]}", ts, amount, direction,
                  category, "demo"))


class Figures(unittest.TestCase):
    def summary(self, conn, settings=None, inv=None, balances=(40000.0,)):
        settings = {"currency": "USD", "ignored_senders": [], "trusted_senders": [], **(settings or {})}
        nw = {"net": 50000.0, "balances": [{"value": v} for v in balances]}
        inv = inv or {"total": 10000.0, "networth": []}
        with mock.patch("margin.glance.networth.snapshot", return_value=nw), \
                mock.patch("margin.glance.invest.snapshot", return_value=inv):
            return glance.summary(conn, settings, {"at": "2026-11-16T00:01:00", "ok": True, "problems": []},
                                  False, today=TODAY)

    def test_today_and_month_follow_the_pages_rules(self):
        conn = mem()
        pay(conn, "2026-11-16T09:00:00", 12.5)                              # today
        pay(conn, "2026-11-16T10:00:00", 500, category="Transfer")         # own money: not spending
        pay(conn, "2026-11-16T11:00:00", 300, category="Investments")      # not spending
        pay(conn, "2026-11-16T12:00:00", 2.5, direction="in")              # a friend paid back: reduces spending
        pay(conn, "2026-11-16T13:00:00", 40, direction="in", category="Refunds & cashback")   # income, not spending
        pay(conn, "2026-11-03T08:00:00", 90)                                # earlier this month
        g = self.summary(conn)
        self.assertEqual(g["today"]["spent"], "$10.00")
        self.assertEqual(g["month"]["spent"], "$100")
        self.assertEqual(g["month"]["label"], "Spent this month")          # no salaries yet: calendar months
        self.assertEqual(g["month"]["vs"], "Nov 2026")                     # nothing last month to compare with

    def test_month_is_compared_with_the_same_days_of_last_month(self):
        conn = mem()
        pay(conn, "2026-11-05T09:00:00", 80)
        pay(conn, "2026-10-05T09:00:00", 100)          # by the 16th of October: 100
        pay(conn, "2026-10-25T09:00:00", 900)          # later in October: not counted against today
        g = self.summary(conn)
        self.assertEqual(g["month"]["vs"], "20% less than Oct by now")
        self.assertEqual(g["month"]["trend"], "down")

    def test_pay_cycles_once_two_salaries_are_on_record(self):
        conn = mem()
        for d in ("2026-08-25", "2026-09-25", "2026-10-26"):
            pay(conn, d + "T09:00:00", 5000, direction="in", category="Salary")
        pay(conn, "2026-10-27T09:00:00", 200)          # after the October payday: counts for "Nov"
        pay(conn, "2026-11-10T09:00:00", 100)
        pay(conn, "2026-10-20T09:00:00", 999)          # before it: last cycle
        pay(conn, "2026-09-27T09:00:00", 150)          # last cycle, within the same number of days
        g = self.summary(conn)
        self.assertEqual(g["month"]["label"], "Spent this cycle")
        self.assertEqual(g["month"]["spent"], "$300")
        self.assertEqual(g["month"]["vs"], "100% more than Oct by now")

    def test_months_estimate_a_missing_payday(self):
        m = glance.Months(["2026-07-25", "2026-08-25", "2026-10-25"], TODAY)
        self.assertIn(date(2026, 9, 25), m.starts)          # September's text never came: estimated
        self.assertEqual(m.key(date(2026, 10, 30)), "2026-11")
        self.assertEqual(m.start("2026-11"), date(2026, 10, 25))

    def test_net_worth_change_is_like_for_like_since_month_end(self):
        inv = {"total": 10000.0, "networth": [{"date": "2026-09-30", "bank": 30000.0, "invest": 9000.0},
                                              {"date": "2026-10-31", "bank": None, "invest": 9500.0},
                                              {"date": "2026-10-31", "bank": 38000.0, "invest": 9500.0}]}
        g = self.summary(mem(), inv=inv)
        self.assertEqual(g["net"]["value"], "$50,000")
        self.assertEqual(g["net"]["change"], {"amount": "+$2,500 · 5.3%", "since": "this month", "trend": "up"})

    def test_no_change_without_a_month_end_balance(self):
        g = self.summary(mem(), inv={"total": 1.0, "networth": [{"date": "2026-10-31", "bank": None, "invest": 1.0}]})
        self.assertIsNone(g["net"]["change"])

    def test_sync_and_lock_flags(self):
        g = self.summary(mem(), {"lock_minutes": 0})
        self.assertFalse(g["lock_on"])
        self.assertEqual(g["sync"], {"at": "2026-11-16T00:01:00", "ok": True, "problems": 0, "running": False})
        self.assertTrue(self.summary(mem(), {"lock_minutes": 2})["lock_on"])
        json.dumps(g)


class Endpoint(unittest.TestCase):
    """/api/glance on the real handler: the key is needed, and while locked it sends no figures."""

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.key, self.httpd.touched = "test-key-0000", 0
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def get(self, key):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/glance",
                                     headers={"X-Margin-Key": key} if key else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_needs_the_key(self):
        code, body = self.get(None)
        self.assertEqual(code, 403)
        self.assertNotIn(b"spent", body)

    def test_locked_sends_nothing(self):
        lock.lock_now()
        with mock.patch("margin.server.sync.load_settings", return_value={"lock_minutes": 2}):
            code, body = self.get("test-key-0000")
        self.assertEqual(code, 423)
        self.assertEqual(set(json.loads(body)), {"error", "locked"})

    def test_unlocked_answers(self):
        fake = {"today": {"spent": "$1"}}
        with mock.patch("margin.server.sync.load_settings", return_value={"lock_minutes": 0}), \
                mock.patch("margin.glance.summary", return_value=fake), \
                mock.patch("margin.server._job_running", return_value=False):
            code, body = self.get("test-key-0000")
        self.assertEqual((code, json.loads(body)), (200, fake))


if __name__ == "__main__":
    unittest.main()
