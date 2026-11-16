import tests  # noqa: F401  (a temporary data folder, never the real one)

import sqlite3
import unittest
from datetime import date, datetime

from margin import ai, db, fx, invest, money, parse, sync


class Formatting(unittest.TestCase):
    def test_each_currency_its_own_way(self):
        self.assertEqual(money.fmt(123456, "INR"), "₹1,23,456")
        self.assertEqual(money.fmt(12345678, "INR"), "₹1,23,45,678")
        self.assertEqual(money.fmt(123456, "USD"), "$123,456")
        self.assertEqual(money.fmt(12.5, "USD"), "$12.50")
        self.assertEqual(money.fmt(12345.6, "JPY"), "¥12,346")        # no decimals in yen
        self.assertEqual(money.fmt(-2500, "EUR"), "−€2,500")
        self.assertEqual(money.fmt(1999.999, "KWD", 3), "KWD 1,999.999")

    def test_short_forms(self):
        self.assertEqual(money.short(123456, "INR"), "₹1.2L")
        self.assertEqual(money.short(34500000, "INR"), "₹3.5Cr")
        self.assertEqual(money.short(1234567, "USD"), "$1.2M")
        self.assertEqual(money.short(45200, "USD"), "$45K")

    def test_home_currency(self):
        self.assertEqual(money.home({}), "INR")
        self.assertEqual(money.home({"currency": "usd"}), "USD")
        self.assertEqual(money.home({"currency": "XXX"}), "INR")       # unknown: the default, not a crash
        self.assertEqual(money.info("USD")["locale"], "en-US")


class Detection(unittest.TestCase):
    def test_finds_every_currency_in_a_text(self):
        self.assertEqual(money.detect("Spent USD 12.99 at DEMO SHOP (INR 1,085.20). Avl Lmt Rs.50,000"),
                         [("USD", 12.99), ("INR", 1085.2), ("INR", 50000.0)])
        self.assertEqual(money.detect("£8.45 at a cafe, €3 tip, S$4.50 taxi"),
                         [("GBP", 8.45), ("EUR", 3.0), ("SGD", 4.5)])

    def test_a_foreign_only_text_is_no_longer_skipped(self):
        self.assertTrue(parse.looks_financial("Your card was charged USD 17.40 at DEMO AI on 03-11-26."))
        self.assertTrue(parse.looks_financial("Rs.450.00 debited from your account"))

    def test_conversion_notes_in_any_home_currency(self):
        rupees = ai.fx_note("USD", 21.43, 1810.84, 84.5, "ECB rate for 2026-11-02")
        self.assertIn("≈ ₹1,810.84 at ₹84.5/USD", rupees)                 # unchanged for rupees
        dollars = ai.fx_note("GBP", 8.45, 10.73, 1.27, "ECB rate", "USD")
        self.assertIn("≈ $10.73 at $1.27/GBP", dollars)
        for note in (rupees, dollars):
            self.assertTrue(ai.FX_NOTE_RE.match("[read by Claude]" + note + " text"))
            self.assertEqual(money.original("[read by Claude]" + note)[0], note.split()[0][1:])
        self.assertIsNone(money.original("Rs.450.00 debited from your account"))


def mem() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    return c


class HomeCurrency(unittest.TestCase):
    def test_rates_into_any_currency_are_cached_separately(self):
        c = mem()
        fx._cache(c)
        c.execute("INSERT INTO fx_rates VALUES ('INR>USD', '2026-11-02', 0.0119, '2026-11-02')")
        c.execute("INSERT INTO fx_rates VALUES ('USD', '2026-11-02', 84.0, '2026-11-02')")
        self.assertEqual(fx.rate(c, "INR", "USD", "2026-11-02")[0], 0.0119)
        self.assertEqual(fx.rate(c, "USD", "INR", "2026-11-02")[0], 84.0)
        self.assertEqual(fx.rate(c, "EUR", "EUR", "2026-11-02")[0], 1.0)
        self.assertEqual(fx.cached(c, "INR", "USD"), 0.0119)

    def test_investments_in_a_dollar_home(self):
        c = mem()
        fx._cache(c)
        c.execute("INSERT INTO prices VALUES ('VOO', ?, 500.0, 495.0, 'USD')", (date.today().isoformat(),))
        settings = {"currency": "USD",
                    "us_snapshot": {"as_of": "2026-01-02", "holdings": {"VOO": {"shares": 2, "cost_usd": 800}}},
                    "other_investments": [{"name": "Demo 401(k)", "kind": "retirement", "value": 50000, "cost": 40000},
                                          {"name": "Demo savings", "kind": "cash", "value": 9000}]}
        rows, _ = invest.holdings(c, settings)
        by = {h["key"]: h for h in rows}
        self.assertEqual(by["VOO"]["value"], 1000.0)                 # dollars at home: no conversion
        self.assertEqual(by["VOO"]["cost"], 800)
        self.assertEqual(by["VOO"]["label"], "Stocks & ETFs")
        self.assertEqual(by["OTHER:Demo 401(k)"]["value"], 50000)
        self.assertEqual({k["label"] for k in invest.by_kind(rows).values()},
                         {"Stocks & ETFs", "Retirement", "Savings & CDs"})

    def test_rupee_bank_texts_convert_for_a_dollar_home(self):
        c = mem()
        fx._cache(c)
        c.execute("INSERT INTO fx_rates VALUES ('INR>USD', '2026-11-03', 0.012, '2026-11-03')")
        settings = dict(sync.DEFAULT_SETTINGS, currency="USD")
        msg = {"id": "t1", "sender": "JD-HDFCBK-S", "subject": "", "ts_epoch": datetime(2026, 11, 3, 10).timestamp(),
               "text": "Dear Customer, Rs.1000.00 has been debited from account **1234 to VPA demo@ybl DEMO SHOP on "
                       "03-11-26. Your UPI transaction reference number is 600000000001."}
        self.assertEqual(sync.ingest(c, msg, "sms", settings), "txn")
        t = c.execute("SELECT amount, raw FROM transactions").fetchone()
        self.assertEqual(t["amount"], 12.0)
        self.assertEqual(money.original(t["raw"]), ("INR", 1000.0))


if __name__ == "__main__":
    unittest.main()
