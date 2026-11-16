"""Living in the US: bank texts from short codes, US alert shapes in dollars, balances and card dues,
US brands, balances Claude reads. Downloaded files are in tests/test_files.py. Every message, name, number and
amount here is made up."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import sqlite3
import unittest
from datetime import datetime

from margin import ai, categorize, db, gmail, parse, sms, sync

US = dict(sync.DEFAULT_SETTINGS, currency="USD", self_names=["JORDAN LEE"], misc_below=3)
IN = dict(sync.DEFAULT_SETTINGS, currency="INR")


def mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("CREATE TABLE fx_rates (cur TEXT NOT NULL, day TEXT NOT NULL, rate REAL NOT NULL, rate_day TEXT, "
                 "PRIMARY KEY (cur, day))")
    return conn


def us(text, subject=""):
    return parse.parse_alert(text, subject, "USD")


def one(text, subject=""):
    r = us(text, subject)
    assert len(r.txns) == 1, (text, r.reason)
    return r.txns[0], r


class Texts(unittest.TestCase):
    def test_short_codes_are_read_people_never(self):
        for s in ("24273", "227898", "JD-HDFCBK-S", "VM-ICICIT"):
            self.assertTrue(sms.is_service_sender(s), s)
        for s in ("+12065550100", "2065550100", "+919999999999", "9999999999", "someone@example.com", "", "1234",
                  "+24273", "1234567"):
            self.assertFalse(sms.is_service_sender(s), s)

    def test_short_code_banks(self):
        self.assertEqual(parse.bank_for_sender("24273"), "Chase")
        self.assertEqual(parse.bank_for_sender("93557"), "Wells Fargo")
        self.assertEqual(parse.bank_for_sender("555123", text="Capital One: A purchase of $45.00 was made"),
                         "Capital One")
        self.assertEqual(parse.bank_for_sender("555123", text="BofA: Your available balance is $1,000.00"),
                         "Bank of America")
        self.assertIsNone(parse.bank_for_sender("555123", text="Your DEMO PIZZA order of $24.00 is on its way"))
        self.assertIsNone(parse.bank_for_sender("2065550100", text="Chase: A $45.00 transaction"))   # a person
        self.assertEqual(parse.bank_for_sender("555123", ["555123"]), "555123")                  # trusted by you


class Senders(unittest.TestCase):
    def test_us_bank_domains(self):
        for sender, bank in [("Chase <no.reply.alerts@chase.com>", "Chase"),
                             ("Bank of America <onlinebanking@ealerts.bankofamerica.com>", "Bank of America"),
                             ("Wells Fargo Online <alerts@notify.wellsfargo.com>", "Wells Fargo"),
                             ("Capital One <capitalone@notification.capitalone.com>", "Capital One"),
                             ("Discover Card <discover@services.discover.com>", "Discover"),
                             ("alerts@usbank.com", "U.S. Bank"), ("alerts@pnc.com", "PNC"), ("alerts@ally.com", "Ally"),
                             ("alerts@navyfederal.org", "Navy Federal"), ("alerts@usaa.com", "USAA"),
                             ("alerts@barclaysus.com", "Barclays US"), ("alerts@sofi.com", "SoFi"),
                             ("alerts@synchrony.com", "Synchrony"), ("alerts@schwab.com", "Schwab"),
                             ("alerts@fidelity.com", "Fidelity"),
                             ("American Express <AmericanExpress@welcome.americanexpress.com>", "Amex")]:
            self.assertEqual(parse.bank_for_sender(sender), bank, sender)

    def test_not_inside_other_names(self):
        self.assertIsNone(parse.bank_for_sender("deals@monkey.com"))          # not KeyBank's key.com
        self.assertIsNone(parse.bank_for_sender("orders@purchase.example"))   # "chase" inside a word

    def test_indian_senders_unchanged(self):
        self.assertEqual(parse.bank_for_sender("HDFC Bank InstaAlerts <alerts@hdfcbank.net>"), "HDFC")
        self.assertEqual(parse.bank_for_sender("BZ-AXISBK"), "Axis")
        self.assertIsNone(parse.bank_for_sender("+919812345678"))

    def test_currency_for(self):
        self.assertEqual(parse.currency_for("Chase", "INR"), "USD")
        self.assertEqual(parse.currency_for("HDFC", "USD"), "INR")
        self.assertEqual(parse.currency_for("Citi", "USD"), "USD")        # in both countries: where you live
        self.assertEqual(parse.currency_for("Citi", "INR"), "INR")
        self.assertEqual(parse.currency_for("mycu.org", "USD"), "USD")    # a sender you trusted
        self.assertEqual(parse.currency_for("mybank.in", "INR"), "INR")


class Alerts(unittest.TestCase):
    def test_card_spends(self):
        cases = [
            ("Chase: A $45.00 debit card transaction to DEMO SHOP on 11/14/2026 2:15:00 PM ET exceeded your $0.00 "
             "set alert. Reply STOP to cancel.", None),
            ("You made a $45.00 transaction with DEMO SHOP . Account Demo Rewards Card (...9876) Date Nov 14, 2026 "
             "at 2:15 PM ET Merchant DEMO SHOP Amount $45.00 You are receiving this alert because you chose card "
             "alerts. If you received this in error, call us.", "9876"),
            ("Credit card transaction exceeds alert limit you set Amount: $45.00 Card ending in: 1234 Where: DEMO "
             "SHOP View details", "1234"),
            ("Purchase amount $45.00 Merchant detail DEMO SHOP Card ending 1234", "1234"),
            ("Discover: A transaction of $45.00 at DEMO SHOP on November 14, 2026 was made with your card ending in "
             "1234.", "1234"),
            ("Citi Alert: A $45.00 transaction was made at DEMO SHOP on card ending in 1234.", "1234"),
            ("U.S. Bank: Your debit card ending in 1234 was used for a $45.00 purchase at DEMO SHOP.", "1234"),
            ("As requested, we're notifying you that on November 14, 2026, at DEMO SHOP, a pending authorization or "
             "purchase in the amount of $45.00 was placed or charged on your Demo card ending in 1234.", "1234"),
        ]
        for text, last4 in cases:
            t, _ = one(text)
            self.assertEqual((t.amount, t.direction, t.last4, t.kind, t.counterparty),
                             (45.0, "out", last4, "card", "DEMO SHOP"), text)

    def test_bank_account(self):
        t, r = one("Ally Bank: A withdrawal of $100.00 was made from your account ending in 1234. Available "
                   "balance: $1,234.56.")
        self.assertEqual((t.amount, t.direction, t.kind, r.balance), (100.0, "out", "account", 1234.56))
        t, r = one("Chase: Your direct deposit of $2,000.00 from ACME CORP posted to your account (...1234). "
                   "Available balance $3,234.56")
        self.assertEqual((t.amount, t.direction, t.counterparty, t.balance), (2000.0, "in", "ACME CORP", 3234.56))
        t, _ = one("Chase: Your payment of $500.00 to CHASE CARD PAYMENT was sent from your account ending in 1234.")
        self.assertEqual((t.direction, t.kind, t.counterparty), ("out", "account", "CHASE CARD PAYMENT"))

    def test_zelle(self):
        t, _ = one("Zelle: MARIA GARCIA sent you $50.00. It's in your account ending in 1234.")
        self.assertEqual((t.direction, t.counterparty), ("in", "MARIA GARCIA"))
        t, _ = one("Zelle: You sent $50.00 to MARIA GARCIA. Account ending in 1234.")
        self.assertEqual((t.direction, t.counterparty), ("out", "MARIA GARCIA"))

    def test_card_payment_and_refund_are_money_in(self):
        t, _ = one("Capital One: We received your payment of $500.00 for your card ending in 1234. Thank you.")
        self.assertEqual((t.amount, t.direction, t.kind), (500.0, "in", "card"))
        t, _ = one("A credit of $12.00 from DEMO SHOP was posted to your card ending in 1234.")
        self.assertEqual((t.amount, t.direction, t.counterparty), (12.0, "in", "DEMO SHOP"))

    def test_balance_only(self):
        r = us("BofA: Your available balance for account ending in 1234 is $1,234.56 as of 11/14/2026.")
        self.assertEqual((r.txns, r.balance, r.last4), ([], 1234.56, "1234"))
        r = us("Chase: Your Demo card (...1234) balance is $523.45.")       # what you owe on a card
        self.assertEqual((r.txns, r.statement_due, r.balance), ([], 523.45, None))

    def test_statement(self):
        r = us("Your Demo card statement is ready for the card ending in 1234. New Balance: $1,234.56. Minimum "
               "Payment Due: $35.00. Payment Due Date: December 5, 2026.")
        self.assertEqual((r.statement_due, r.due_date, r.last4, r.txns), (1234.56, "2026-12-05", "1234", []))
        r = us("Your statement is available. Statement balance: $2,431.18 Minimum payment due: $40.00 Payment due "
               "date: 12/05/2026. Credit card ending in 9876.")
        self.assertEqual((r.statement_due, r.due_date), (2431.18, "2026-12-05"))

    def test_not_payments(self):
        for text in ["Your verification code is 123456. Don't share it.",
                     "Your card ending in 1234 was declined for $45.00 at DEMO SHOP.",
                     "MARIA GARCIA requested $25.00 from you with Zelle.",
                     "Reminder: your payment of $35.00 is due on Dec 5, 2026 for card ending 1234.",
                     "Your available balance is below $100.00 for account ending in 1234.",
                     "Did you make this purchase? $45.00 at DEMO SHOP. Reply YES or NO.",
                     "Your scheduled payment of $500.00 will be withdrawn on 12/01/2026."]:
            self.assertEqual(us(text).txns, [], text)

    def test_threshold_is_not_the_amount(self):
        t, _ = one("Chase: A $12.34 debit card transaction to DEMO CAFE exceeded your $10.00 set alert.")
        self.assertEqual(t.amount, 12.34)

    def test_rupee_reader_is_untouched(self):
        # The Indian reader still leaves a dollar spend from an Indian bank to Claude (tests/test_parse.py).
        self.assertEqual(parse.parse_alert("Spent Card no. XX1234 USD 8.75 14-12-22 10:42:18 SOME SITE Avl Lmt "
                                           "INR 86420.5").txns, [])
        self.assertEqual(parse.parse_alert("Rs.450.00 debited from your account").txns[0].amount, 450.0)
        self.assertFalse(parse.looks_financial("You made a $45.00 transaction with DEMO SHOP"))
        self.assertTrue(parse.looks_financial("You made a $45.00 transaction with DEMO SHOP", "USD"))


class Ingest(unittest.TestCase):
    def msg(self, text, sender="Chase <no.reply.alerts@chase.com>", when=datetime(2026, 11, 14, 14, 15), n=[0]):
        n[0] += 1
        return {"id": f"demo{n[0]}", "sender": sender, "subject": "", "ts_epoch": when.timestamp(), "text": text}

    def test_us_alerts_in_dollars(self):
        c = mem()
        self.assertEqual(sync.ingest(c, self.msg("Chase: You made a $45.00 transaction with WHOLE FOODS MARKET on "
                                                 "card ending in 9876."), "email", US), "txn")
        t = c.execute("SELECT * FROM transactions").fetchone()
        self.assertEqual((t["amount"], t["bank"], t["account"], t["category"]),
                         (45.0, "Chase", "Chase card ••9876", "Groceries"))
        self.assertFalse(t["raw"].startswith("["))                     # no conversion note: dollars at home
        sync.ingest(c, self.msg("BofA: Your available balance for account ending in 1234 is $3,210.00.",
                                "onlinebanking@ealerts.bankofamerica.com"), "email", US)
        sync.ingest(c, self.msg("Your statement is ready. Statement balance: $2,431.18. Payment due date: "
                                "12/05/2026. Credit card ending in 9876."), "email", US)
        self.assertEqual(dict(c.execute("SELECT account, balance FROM balances").fetchone()),
                         {"account": "Bank of America a/c ••1234", "balance": 3210.0})
        self.assertEqual(tuple(c.execute("SELECT account, total_due, due_date FROM card_dues").fetchone()),
                         ("Chase card ••9876", 2431.18, "2026-12-05"))

    def test_short_code_text(self):
        c = mem()
        kind = sync.ingest(c, self.msg("Chase: A $8.50 debit card transaction to DEMO CAFE on 11/14/2026 exceeded "
                                       "your $0.00 set alert.", "24273"), "sms", US)
        self.assertEqual(kind, "txn")

    def test_us_bank_with_rupees_at_home(self):
        c = mem()
        c.execute("INSERT INTO fx_rates VALUES ('USD', '2026-11-14', 84.0, '2026-11-14')")
        sync.ingest(c, self.msg("Chase: You made a $10.00 transaction with DEMO SHOP on card ending in 9876."),
                    "email", IN)
        t = c.execute("SELECT amount, raw FROM transactions").fetchone()
        self.assertEqual(t["amount"], 840.0)
        self.assertTrue(t["raw"].startswith("[USD 10 ≈ ₹840.00 at ₹84/USD"))

    def test_unknown_us_sender_goes_to_sort_out(self):
        c = mem()
        self.assertEqual(sync.ingest(c, self.msg("You made a $45.00 transaction with DEMO SHOP.",
                                                 "alerts@demo-credit-union.example"), "email", US), "review")
        self.assertEqual(sync.ingest(c, self.msg("You made a $45.00 transaction with DEMO SHOP.",
                                                 "alerts@demo-credit-union.example"), "email", IN), "skip")

    def test_review_retried_with_the_us_reader(self):
        c = mem()
        c.execute("INSERT INTO review VALUES (?,?,?,?,?,?)",
                  ("email:old1", "email", "Chase <no.reply.alerts@chase.com>", "2026-11-10T09:00:00",
                   "sender isn't a known bank (not counted)", "Chase: You made a $20.00 transaction with DEMO DELI."))
        c.execute("INSERT INTO review VALUES (?,?,?,?,?,?)",
                  ("sms:old2", "sms", "VM-HDFCBK", "2026-11-10T09:00:00", "no debit/credit wording", "Rs 10 something"))
        self.assertEqual(sync.retry_review(c, US), 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
        self.assertEqual([r[0] for r in c.execute("SELECT uid FROM review")], ["sms:old2"])   # rupees: untouched


class Search(unittest.TestCase):
    def test_india_unchanged(self):
        self.assertEqual(gmail.search_query(IN), gmail.SEARCH)

    def test_us_wording(self):
        q = gmail.search_query(dict(US, trusted_senders=["demo-cu.example", "24273"]))
        self.assertIn('"available balance"', q)
        self.assertIn("from:chase.com", q)
        self.assertIn("from:demo-cu.example", q)
        self.assertNotIn("from:24273", q)
        self.assertTrue(q.endswith("-category:promotions -category:social -in:sent -in:chats"))


class Brands(unittest.TestCase):
    def test_us_brands(self):
        c = categorize.categorize
        for payee, cat in [("WALMART SUPERCENTER", "Shopping"), ("TARGET T-1234", "Shopping"),
                           ("COSTCO WHSE #123", "Groceries"), ("TRADER JOE'S #456", "Groceries"),
                           ("WHOLEFDS MKT", "Groceries"), ("KROGER", "Groceries"), ("DOORDASH*DEMO", "Food & dining"),
                           ("UBER EATS", "Food & dining"), ("LYFT RIDE", "Transport"), ("CHEVRON 0123", "Car"),
                           ("CVS/PHARMACY #789", "Medical"), ("COMCAST CABLE", "Bills & utilities"),
                           ("AT&T BILL PAYMENT", "Bills & utilities"), ("T-MOBILE", "Bills & utilities"),
                           ("GEICO AUTO", "Insurance"), ("VANGUARD BUY", "Investments")]:
            self.assertEqual(c("out", payee, ""), cat, payee)

    def test_own_money_moving(self):
        c = categorize.categorize
        self.assertEqual(c("in", "", "AUTOMATIC PAYMENT - THANK YOU"), "Transfer")
        self.assertEqual(c("out", "CHASE CREDIT CRD AUTOPAY", ""), "Transfer")
        self.assertEqual(c("in", "", "VENMO CASHOUT"), "Transfer")
        self.assertEqual(c("out", "MARIA GARCIA", "Zelle payment to MARIA GARCIA"), "Other spending")   # a person
        self.assertEqual(c("in", "ACME CORP", "ACME CORP DIRECT DEP"), "Salary")


class ClaudeBalances(unittest.TestCase):
    def test_balance_and_card_due_recorded_and_undone(self):
        c = mem()
        c.execute("INSERT INTO review VALUES (?,?,?,?,?,?)",
                  ("email:b1", "email", "alerts@demo-cu.example", "2026-11-14T08:00:00", "no debit/credit wording",
                   "Demo CU: Checking ••2222 end-of-day funds $1,500.00"))
        c.execute("INSERT INTO review VALUES (?,?,?,?,?,?)",
                  ("email:b2", "email", "alerts@demo-cu.example", "2026-11-14T08:00:00", "no debit/credit wording",
                   "Demo CU Visa ••4321: you owe $300.00, pay by Dec 5"))
        answers = [{"id": "0", "verdict": "balance", "amount": 1500, "currency": "USD", "last4": "2222",
                    "card": False, "reason": "end-of-day balance"},
                   {"id": "1", "verdict": "balance", "amount": 300, "currency": "USD", "last4": "4321",
                    "card": True, "due_date": "2026-12-05", "reason": "card balance owed"}]
        settings = dict(US, trusted_senders=["demo-cu.example"])
        real = ai._ask
        ai._ask = lambda payload, system, schema, s: (answers, "")
        try:
            applied, _ = ai.job_unread(c, settings)
        finally:
            ai._ask = real
        self.assertEqual(applied, 2)
        self.assertEqual(tuple(c.execute("SELECT account, balance FROM balances").fetchone()),
                         ("demo-cu.example a/c ••2222", 1500.0))
        self.assertEqual(tuple(c.execute("SELECT account, total_due, due_date FROM card_dues").fetchone()),
                         ("demo-cu.example card ••4321", 300.0, "2026-12-05"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM review").fetchone()[0], 0)
        d = c.execute("SELECT id FROM ai_decisions WHERE key='email:b1'").fetchone()[0]
        ai.undo(c, settings, d)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM balances").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM review").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
