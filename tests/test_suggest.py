"""Sort out's one-tap suggestions, on made-up payees. No real data."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import json
import sqlite3
import unittest
from datetime import date, timedelta

from margin import db, suggest

N = 0


def mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    return conn


def txn(conn, payee, category, amount=100.0, direction="out", days_ago=10):
    global N
    N += 1
    ts = (date.today() - timedelta(days=days_ago)).isoformat() + "T10:00:00"
    conn.execute("INSERT INTO transactions(uid, source, ts, amount, direction, counterparty, category) "
                 "VALUES (?,?,?,?,?,?,?)", (f"t{N}", "sms", ts, amount, direction, payee, category))


def payee(name, direction="out"):
    return {"key": name.lower(), "dir": direction, "name": name, "n": 1, "total": 100.0, "last": "2026-11-01"}


def sugs(conn, name, direction="out"):
    return suggest.add(conn, [payee(name, direction)])[0]["sugs"]


class Suggestions(unittest.TestCase):
    def test_a_payee_like_one_you_sorted(self):
        c = mem()
        txn(c, "ANAND GENERAL STORES", "Groceries")
        s = sugs(c, "ACME GENERAL STORES")
        self.assertEqual(s[0]["cat"], "Groceries")
        self.assertEqual(s[0]["src"], "similar")
        self.assertEqual(s[0]["like"], "ANAND GENERAL STORES")

    def test_one_shared_first_name_is_not_enough(self):
        c = mem()
        txn(c, "KAVYA MENON GROCERY", "Groceries")
        s = sugs(c, "KAVYA PIZZA")
        self.assertEqual(s[0], {"cat": "Food & dining", "src": "name", "word": "pizza"})
        self.assertNotIn("similar", [x["src"] for x in s])

    def test_a_word_in_the_name(self):
        c = mem()
        self.assertEqual(sugs(c, "CARE MEDICALS")[0], {"cat": "Medical", "src": "name", "word": "medicals"})
        self.assertEqual(sugs(c, "KUMAR TAILORS")[0]["cat"], "Home services")

    def test_a_person(self):
        c = mem()
        self.assertEqual(sugs(c, "VIKRAM BOSE")[0], {"cat": "Friends & family", "src": "person"})
        self.assertEqual(sugs(c, "Meera Kulkarni", "in")[0]["cat"], "Friends & family")
        self.assertNotIn("person", [x["src"] for x in sugs(c, "KUMAR TAILORS")])     # a shop word: not a person
        self.assertNotIn("person", [x["src"] for x in sugs(c, "arjun@ybl")])          # a UPI handle

    def test_the_same_amount_every_month(self):
        c = mem()
        for m in range(4):
            txn(c, "ACME", "Other spending", 799, days_ago=5 + 30 * m)
        self.assertEqual(sugs(c, "ACME")[0], {"cat": "Subscriptions", "src": "monthly"})

    def test_claudes_unsure_guess_comes_first(self):
        c = mem()
        c.execute("INSERT INTO ai_decisions(at, job, key, verdict, reason, detail) VALUES (?,?,?,?,?,?)",
                  ("2026-11-16T00:00:00", "categories", "payee:out:jordan lee", "unsure", "Splits dinner bills",
                   json.dumps({"category": "Food & dining", "n": 3})))
        s = sugs(c, "JORDAN LEE")
        self.assertEqual(s[0], {"cat": "Food & dining", "src": "claude", "reason": "Splits dinner bills"})
        self.assertEqual(s[1]["cat"], "Friends & family")

    def test_fills_up_with_what_you_use_most_and_never_repeats(self):
        c = mem()
        for _ in range(3):
            txn(c, "ANAND GENERAL STORES", "Groceries")
        txn(c, "CARE MEDICALS", "Medical")
        s = sugs(c, "ACME")
        self.assertEqual([x["cat"] for x in s], ["Groceries", "Medical"])
        self.assertTrue(all(x["src"] == "often" for x in s))
        s = sugs(c, "ACME GENERAL STORES")
        self.assertEqual([x["cat"] for x in s], ["Groceries", "Medical"])   # Groceries once, though two reasons

    def test_income_only_gets_income_categories(self):
        c = mem()
        txn(c, "ANAND GENERAL STORES", "Groceries")
        self.assertEqual(sugs(c, "ACME REFUND", "in")[0]["cat"], "Refunds & cashback")
        self.assertNotIn("Groceries", [x["cat"] for x in sugs(c, "ACME GENERAL STORES", "in")])

    def test_at_most_three(self):
        c = mem()
        for cat in ("Groceries", "Medical", "Shopping", "Transport", "Car"):
            txn(c, f"ACME {cat.upper()}", cat)
        self.assertLessEqual(len(sugs(c, "ALEX RIVERA")), 3)


if __name__ == "__main__":
    unittest.main()
