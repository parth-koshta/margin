"""Names you give payees (payees.py), on made-up payments. No real data: every payee, UPI id, card and amount here is
invented."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import json
import sqlite3
import threading
import unittest
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from unittest import mock

from margin import categorize, dashboard, db, paths, payees, review, server, sync, tidy

SETTINGS = {"currency": "INR", "self_names": [], "misc_below": 0, "trusted_senders": [], "ai_review": True}
# A UPI payment made with a credit card: the bank's alert has only the masked end of the UPI id, and no name.
CARD_UPI = ("Dear Customer, Greetings from Demo Bank! We are sending this alert to help you check a recent UPI "
            "transaction made using your Demo Credit Card. Transaction Details: Rs.{amt:.2f} has been debited from "
            "your Demo Credit Card (ending 4321) to ********fruit9@okaxis Date: 03-11-26 UPI Transaction Reference "
            "Number: 123456789012")


def mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def pay(conn, payee, amount, vpa=None, ts="2026-11-16T10:30:00", direction="out", raw="", sender="") -> int:
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return conn.execute(
        "INSERT INTO transactions(uid, source, sender, ts, amount, direction, account, last4, counterparty, vpa, "
        "category, raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"demo:{n}", "email", sender, ts, amount, direction, "Demo card ••4321", "4321", payee, vpa,
         "Other spending", raw)).lastrowid


def row(conn, i) -> sqlite3.Row:
    return conn.execute("SELECT * FROM transactions WHERE id=?", (i,)).fetchone()


def shown(conn, i) -> str:
    r = row(conn, i)
    return r["counterparty"] or r["vpa"] or ""


class Naming(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        self.a = pay(self.conn, "fruit9@okaxis", 250, vpa="fruit9@okaxis")
        self.b = pay(self.conn, "fruit9@okaxis", 120, vpa="fruit9@okaxis", ts="2026-11-17T09:00:00")
        self.other = pay(self.conn, "DEMO BAKERY", 80, vpa="bakery@ybl")

    def test_every_payment_to_it_shows_the_name_and_keeps_the_handle(self):
        before = payees.set_name(self.conn, "fruit9@okaxis", "  Fresh   Fruit Stall ")
        self.assertEqual(before, {"fruit9@okaxis": ""})
        for i in (self.a, self.b):
            r = row(self.conn, i)
            self.assertEqual((r["counterparty"], r["vpa"], r["payee_was"]),
                             ("Fresh Fruit Stall", "fruit9@okaxis", "fruit9@okaxis"))
        self.assertEqual(shown(self.conn, self.other), "DEMO BAKERY")

    def test_new_payments_get_the_name_on_the_next_sync(self):
        payees.set_name(self.conn, "fruit9@okaxis", "Fresh Fruit Stall")
        new = pay(self.conn, "fruit9@okaxis", 60, vpa="fruit9@okaxis", ts="2026-11-18T08:00:00")
        self.assertEqual(payees.apply(self.conn), 1)
        self.assertEqual(shown(self.conn, new), "Fresh Fruit Stall")
        self.assertEqual(payees.apply(self.conn), 0)       # nothing left to do

    def test_taking_it_back_returns_the_alerts_own_wording(self):
        named = pay(self.conn, "DEMO BOOK CORNER", 300, vpa="books@ybl")
        payees.set_name(self.conn, "fruit9@okaxis", "Fresh Fruit Stall")
        payees.set_name(self.conn, "demo book corner", "Book shop")
        self.assertEqual(shown(self.conn, named), "Book shop")
        payees.set_name(self.conn, "fresh fruit stall", "")
        payees.set_name(self.conn, "book shop", "")
        self.assertEqual([(row(self.conn, i)["counterparty"], row(self.conn, i)["vpa"]) for i in (self.a, named)],
                         [("fruit9@okaxis", "fruit9@okaxis"), ("DEMO BOOK CORNER", "books@ybl")])
        self.assertIsNone(row(self.conn, named)["payee_was"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payee_names").fetchone()[0], 0)

    def test_a_handle_only_alert_without_a_payee_comes_back_the_same(self):
        bare = pay(self.conn, None, 45, vpa="tea7@ybl")
        payees.set_name(self.conn, "tea7@ybl", "Tea stall")
        self.assertEqual((row(self.conn, bare)["counterparty"], row(self.conn, bare)["payee_was"]), ("Tea stall", ""))
        payees.set_name(self.conn, "tea7@ybl", "")      # by its handle works too
        self.assertEqual((row(self.conn, bare)["counterparty"], shown(self.conn, bare)), (None, "tea7@ybl"))

    def test_two_handles_with_one_name_are_one_payee_and_rename_together(self):
        second = pay(self.conn, "fruit2@okaxis", 90, vpa="fruit2@okaxis")
        payees.set_name(self.conn, "fruit9@okaxis", "Fresh Fruit Stall")
        payees.set_name(self.conn, "fruit2@okaxis", "Fresh Fruit Stall")
        groups = [p for p in tidy.payee_list(self.conn) if p["key"] == "fresh fruit stall"]
        self.assertEqual([(g["n"], g["was"]) for g in groups], [(3, ["fruit2@okaxis", "fruit9@okaxis"])])
        before = payees.set_name(self.conn, "fresh fruit stall", "Ravi's fruit cart")
        self.assertEqual(before, {"fruit9@okaxis": "Fresh Fruit Stall", "fruit2@okaxis": "Fresh Fruit Stall"})
        self.assertEqual({shown(self.conn, i) for i in (self.a, second)}, {"Ravi's fruit cart"})
        payees.restore(self.conn, before)                 # Undo
        self.assertEqual({shown(self.conn, i) for i in (self.a, second)}, {"Fresh Fruit Stall"})

    def test_undo_of_a_first_name_takes_it_back(self):
        before = payees.set_name(self.conn, "fruit9@okaxis", "Fresh Fruit Stall")
        payees.restore(self.conn, before)
        self.assertEqual(shown(self.conn, self.a), "fruit9@okaxis")

    def test_a_name_with_nothing_to_name_does_nothing(self):
        self.assertEqual(payees.set_name(self.conn, "nobody@ybl", "Ghost"), {})
        self.assertEqual(payees.set_name(self.conn, "  ", "Ghost"), {})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM payee_names").fetchone()[0], 0)

    def test_names_are_tidied(self):
        payees.set_name(self.conn, "fruit9@okaxis", "Fruit\x00 stall\n" + "x" * 80)
        name = shown(self.conn, self.a)
        self.assertTrue(name.startswith("Fruit stall x"))
        self.assertLessEqual(len(name), payees.MAX_NAME)


class Rules(unittest.TestCase):
    """Rules see the name and the alert's own wording."""

    def setUp(self):
        self.conn = mem()
        self.a = pay(self.conn, "fruit9@okaxis", 250, vpa="fruit9@okaxis")
        rules = paths.RULES_PATH
        self.addCleanup(lambda: (rules.unlink(missing_ok=True), categorize.reload_rules()))

    def category(self):
        sync.recategorize(self.conn, SETTINGS)
        return row(self.conn, self.a)["category"]

    def test_a_rule_on_the_name_sorts_it(self):
        payees.set_name(self.conn, "fruit9@okaxis", "Fresh Fruit Stall")
        categorize.add_user_rule("Fresh Fruit Stall", "Groceries", "out", match_text=False)
        self.assertEqual(self.category(), "Groceries")

    def test_a_rule_on_the_handle_still_sorts_it_after_naming(self):
        categorize.add_user_rule("fruit9@okaxis", "Groceries", "out", match_text=False)
        payees.set_name(self.conn, "fruit9@okaxis", "Fresh Fruit Stall")
        self.assertEqual(self.category(), "Groceries")

    def test_a_rule_on_a_card_merchants_old_name_still_sorts_it(self):
        card = pay(self.conn, "DEMO GADGET HUB", 999)
        categorize.add_user_rule("DEMO GADGET HUB", "Shopping", "out", match_text=False)
        payees.set_name(self.conn, "demo gadget hub", "Phone repair shop")
        sync.recategorize(self.conn, SETTINGS)
        self.assertEqual(row(self.conn, card)["category"], "Shopping")


class Elsewhere(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        self.a = pay(self.conn, "fruit9@okaxis", 250, vpa="fruit9@okaxis")

    def test_the_page_gets_the_name_and_what_the_alert_said(self):
        payees.set_name(self.conn, "fruit9@okaxis", "Fresh Fruit Stall")
        t, = [t for t in dashboard.data(self.conn, {}, {})["txns"] if t["id"] == self.a]
        self.assertEqual((t["p"], t["w"]), ("Fresh Fruit Stall", "fruit9@okaxis"))
        p, = tidy.payee_list(self.conn)
        self.assertEqual((p["name"], p["key"], p["was"]), ("Fresh Fruit Stall", "fresh fruit stall", ["fruit9@okaxis"]))

    def test_unnamed_payees_have_no_extra_fields(self):
        t, = dashboard.data(self.conn, {}, {})["txns"]
        p, = tidy.payee_list(self.conn)
        self.assertNotIn("w", t)
        self.assertNotIn("was", p)

    def test_a_review_written_before_shows_the_new_name(self):
        answer = {"headline": "A made-up day.", "summary": ["Made up."],
                  "flags": [{"txn_ids": [self.a], "severity": "medium", "title": "Coded UPI handle",
                             "why": "No named merchant."}]}
        with mock.patch.object(review, "ask_ai", return_value=(answer, "")):
            review.run(self.conn, "daily", SETTINGS, start=date(2026, 11, 16),
                       end=date(2026, 11, 17), force=True)
        payees.set_name(self.conn, "fruit9@okaxis", "Fresh Fruit Stall")
        flag, = review.listing(self.conn)[0]["flags"]
        self.assertEqual(flag["payee"], "Fresh Fruit Stall")

    def test_re_reading_alerts_leaves_a_name_alone(self):
        conn = mem()
        i = pay(conn, "fruit9@okaxis", 250, vpa="fruit9@okaxis", raw=CARD_UPI.format(amt=250), sender="VM-HDFCBK")
        payees.set_name(conn, "fruit9@okaxis", "Call taxi stand")      # reads like template wording to the parser
        sync.reparse_parties(conn, {**SETTINGS, "currency": "INR"})
        self.assertEqual((row(conn, i)["counterparty"], row(conn, i)["vpa"]), ("Call taxi stand", "fruit9@okaxis"))

    def test_an_older_database_gets_the_new_column(self):
        path = paths.DATA_DIR / "older-money.db"      # its own file: other tests leave money.db behind
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        p = mock.patch.object(paths, "DB_PATH", path)
        p.start()
        self.addCleanup(p.stop)
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, uid TEXT UNIQUE NOT NULL, source TEXT NOT NULL, "
                    "sender TEXT, bank TEXT, ts TEXT NOT NULL, amount REAL NOT NULL, direction TEXT NOT NULL, "
                    "account TEXT, last4 TEXT, counterparty TEXT, vpa TEXT, category TEXT, "
                    "category_locked INTEGER DEFAULT 0, duplicate_of INTEGER, raw TEXT)")
        old.commit()
        old.close()
        conn = db.connect()
        self.addCleanup(conn.close)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        self.assertIn("payee_was", cols)
        db.connect().close()                                # and again: no error once it's there


class Endpoint(unittest.TestCase):
    """/api/payee/name and its Undo on the real handler, with the key."""

    def setUp(self):
        self.addCleanup(lambda: paths.DB_PATH.unlink(missing_ok=True))
        conn = db.connect()
        self.a = pay(conn, "fruit9@okaxis", 250, vpa="fruit9@okaxis")
        conn.commit()
        conn.close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.httpd.key, self.httpd.touched = "test-key-0000", 0
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        for target, fake in (("margin.server._state", lambda conn: {}),
                             ("margin.server._after_change", lambda conn: conn.commit())):   # it saves, as the real one
            p = mock.patch(target, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("margin.server.sync.load_settings", return_value={"lock_minutes": 0})
        p.start()
        self.addCleanup(p.stop)

    def post(self, path, body, key="test-key-0000"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.httpd.server_address[1]}{path}",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Margin-Key": key})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def payee(self):
        conn = db.connect()
        try:
            return shown(conn, self.a)
        finally:
            conn.close()

    def test_name_then_undo(self):
        code, st = self.post("/api/payee/name", {"key": "fruit9@okaxis", "name": "Fresh Fruit Stall"})
        self.assertEqual((code, st["before"]), (200, {"fruit9@okaxis": ""}))
        self.assertEqual(self.payee(), "Fresh Fruit Stall")
        code, _ = self.post("/api/payee/names", {"names": st["before"]})
        self.assertEqual((code, self.payee()), (200, "fruit9@okaxis"))

    def test_bad_requests_and_the_key(self):
        self.assertEqual(self.post("/api/payee/name", {"key": "", "name": "x"})[0], 400)
        self.assertEqual(self.post("/api/payee/name", {"key": 5, "name": "x"})[0], 400)
        self.assertEqual(self.post("/api/payee/names", {"names": ["x"]})[0], 400)
        self.assertEqual(self.post("/api/payee/name", {"key": "fruit9@okaxis", "name": "x"}, key="wrong")[0], 403)
        self.assertEqual(self.payee(), "fruit9@okaxis")


if __name__ == "__main__":
    unittest.main()
