"""Reviews' "things to check" and "Looks fine", on made-up payments. No real data: every payee, card and amount here
is invented, and the AI is a stand-in that returns whatever flags a test hands it."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import sqlite3
import unittest
from datetime import date
from unittest import mock

from margin import db, review

DAY = (date(2026, 11, 16), date(2026, 11, 17))
WEEK = (date(2026, 11, 16), date(2026, 11, 23))
SETTINGS = {"currency": "INR", "ai_review": True}


def mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def pay(conn, payee, amount, ts="2026-11-16T10:30:00", source="email", duplicate_of=None) -> int:
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    return conn.execute(
        "INSERT INTO transactions(uid, source, ts, amount, direction, account, counterparty, category, duplicate_of) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (f"demo:{n}", source, ts, amount, "out", "DEMO card ••0000", payee, "Subscriptions", duplicate_of)).lastrowid


def write_review(conn, kind, period, *groups, severity="medium") -> list[str]:
    """A review whose AI flags these groups of payment ids; returns their keys."""
    answer = {"headline": "A made-up day.", "summary": ["Made up."],
              "flags": [{"txn_ids": list(g), "severity": severity, "title": "Unfamiliar subscription",
                         "why": "No earlier payments to this payee."} for g in groups]}
    with mock.patch.object(review, "ask_ai", return_value=(answer, "")):
        review.run(conn, kind, SETTINGS, start=period[0], end=period[1], force=True)
    return [review.fkey(g) for g in groups]


def open_keys(conn) -> list[str]:
    return [f["key"] for f in review.open_flags(conn)]


class LooksFine(unittest.TestCase):
    def setUp(self):
        self.conn = mem()
        self.a = pay(self.conn, "DEMO STREAMING", 305.52)
        self.b = pay(self.conn, "DEMO CLOUD", 129.0, ts="2026-11-16T12:00:00")
        self.c = pay(self.conn, "DEMO NEWS", 99.0, ts="2026-11-16T18:00:00")

    def assert_same_in_page(self):
        """The page's list (from listing) and the badge count (from open_flags) agree."""
        page = {f["key"] for r in review.listing(self.conn) for f in r["flags"] if not f["fine"]}
        self.assertEqual(page, set(open_keys(self.conn)))

    def test_it_stays_fine_when_the_review_is_written_again(self):
        key, = write_review(self.conn, "daily", DAY, [self.a])
        review.set_flag(self.conn, key, "fine")
        write_review(self.conn, "daily", DAY, [self.a])                    # "Review yesterday now"
        self.assertEqual(open_keys(self.conn), [])
        self.assert_same_in_page()

    def test_it_stays_fine_when_a_later_review_flags_the_payment_alone(self):
        key, = write_review(self.conn, "daily", DAY, [self.a, self.b])      # flagged together...
        review.set_flag(self.conn, key, "fine")
        write_review(self.conn, "weekly", WEEK, [self.a])                  # ...then on its own
        write_review(self.conn, "daily", DAY, [self.b, self.a])            # ...or in another order
        self.assertEqual(open_keys(self.conn), [])
        self.assert_same_in_page()

    def test_it_follows_the_payment_when_margin_counts_its_other_copy(self):
        key, = write_review(self.conn, "daily", DAY, [self.a])
        review.set_flag(self.conn, key, "fine")
        # The same payment's text alert: Margin now counts that copy and the emailed one as its repeat.
        text = pay(self.conn, "DEMO STREAMING", 305.52, source="sms")
        self.conn.execute("UPDATE transactions SET duplicate_of=? WHERE id=?", (text, self.a))
        write_review(self.conn, "daily", DAY, [text])
        self.assertEqual(open_keys(self.conn), [])
        self.assert_same_in_page()

    def test_a_payment_you_havent_looked_at_keeps_the_flag_open(self):
        key, = write_review(self.conn, "daily", DAY, [self.a])
        review.set_flag(self.conn, key, "fine")
        new, = write_review(self.conn, "weekly", WEEK, [self.a, self.c])
        self.assertEqual(open_keys(self.conn), [new])
        self.assert_same_in_page()

    def test_two_fine_payments_can_still_be_a_double_charge(self):
        one, two = write_review(self.conn, "daily", DAY, [self.a], [self.b])
        review.set_flag(self.conn, one, "fine")
        review.set_flag(self.conn, two, "fine")
        both, = write_review(self.conn, "weekly", WEEK, [self.a, self.b], severity="high")
        self.assertEqual(open_keys(self.conn), [both])
        self.assert_same_in_page()

    def test_undo_brings_it_back_wherever_it_was_fine(self):
        key, = write_review(self.conn, "daily", DAY, [self.a, self.b])
        review.set_flag(self.conn, key, "fine")
        alone, = write_review(self.conn, "weekly", WEEK, [self.a])
        review.set_flag(self.conn, alone, "open")                          # "Undo fine" on the weekly one
        self.assertEqual(sorted(open_keys(self.conn)), sorted([alone, key]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM flag_state").fetchone()[0], 0)
        self.assert_same_in_page()

    def test_other_flags_are_left_alone(self):
        one, two = write_review(self.conn, "daily", DAY, [self.a], [self.b])
        review.set_flag(self.conn, one, "fine")
        self.assertEqual(open_keys(self.conn), [two])
        review.set_flag(self.conn, two, "open")                            # undo on a flag that was never fine
        self.assertEqual(open_keys(self.conn), [two])
        self.assert_same_in_page()


if __name__ == "__main__":
    unittest.main()
