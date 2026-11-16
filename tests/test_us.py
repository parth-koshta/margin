"""Living in the US: bank texts from short codes, US alert shapes in dollars, balances and card dues,
US brands, balances the AI reads. Downloaded files are in tests/test_files.py. Every message, name, number and
amount here is made up."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import sqlite3
import unittest
from datetime import datetime, timedelta

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
        b = lambda s, text="", trusted=None, home="USD": parse.bank_for_sender(s, trusted, text, home)  # noqa: E731
        self.assertEqual(b("24273"), "Chase")
        self.assertEqual(b("93557"), "Wells Fargo")
        self.assertEqual(b("555123", "Capital One: A purchase of $45.00 was made"), "Capital One")
        self.assertEqual(b("555123", "BofA: Your available balance is $1,000.00"), "Bank of America")
        self.assertIsNone(b("555123", "Your DEMO PIZZA order of $24.00 is on its way"))
        self.assertIsNone(b("2065550100", "Chase: A $45.00 transaction"))       # a person
        self.assertEqual(b("555123", trusted=["555123"]), "555123")             # trusted by you

    def test_rupees_at_home_short_codes_only_once_trusted(self):
        self.assertIsNone(parse.bank_for_sender("24273", None, "Chase: A $45.00 transaction", "INR"))
        self.assertEqual(parse.bank_for_sender("24273", ["24273"], "", "INR"), "24273")
        self.assertIs(sms.short_codes_for(US), True)
        self.assertEqual(sms.short_codes_for(dict(IN, trusted_senders=["55512", "VM-MYBANK", "demo.in"])), {"55512"})
        self.assertFalse(sms.is_service_sender("24273", set()))
        self.assertTrue(sms.is_service_sender("55512", {"55512"}))
        self.assertTrue(sms.is_service_sender("JD-HDFCBK-S", set()))            # sender IDs always


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
            self.assertEqual(parse.bank_for_sender(sender, home="USD"), bank, sender)

    def test_brokers_are_banks_only_outside_india(self):
        # An Indian RSU / ESPP holder's broker mail ("shares deposited", "proceeds credited") isn't their bank.
        for sender in ("alerts@schwab.com", "Fidelity <alerts@fidelity.com>"):
            self.assertIsNone(parse.bank_for_sender(sender), sender)
            self.assertIsNone(parse.bank_for_sender(sender, home="INR"), sender)
            self.assertIsNotNone(parse.bank_for_sender(sender, home="USD"), sender)
        self.assertEqual(parse.bank_for_sender("no.reply.alerts@chase.com", home="INR"), "Chase")

    def test_not_inside_other_names(self):
        self.assertIsNone(parse.bank_for_sender("deals@monkey.com", home="USD"))          # not KeyBank's key.com
        self.assertIsNone(parse.bank_for_sender("orders@purchase.example", home="USD"))   # "chase" inside a word

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
        # The Indian reader still leaves a dollar spend from an Indian bank to the AI (tests/test_parse.py).
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
        text = "Chase: A $8.50 debit card transaction to DEMO CAFE on 11/14/2026 exceeded your $0.00 set alert."
        self.assertEqual(sync.ingest(c, self.msg(text, "24273"), "sms", US), "txn")

    def test_rupees_at_home_broker_mail_is_not_income(self):
        c = mem()
        kind = sync.ingest(c, self.msg("Your RSU shares were deposited and $1,000.00 credited to your account ending "
                                       "in 1234.", "alerts@schwab.com"), "email", IN)
        self.assertNotEqual(kind, "txn")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM balances").fetchone()[0], 0)

    def test_one_payment_from_three_sources(self):
        # The same spend by email, by text and in a downloaded statement counts once, in any order.
        from margin import imports
        email = "Chase: You made a $45.00 transaction with DEMO SHOP on card ending in 9876."
        text = "Chase: A $45.00 debit card transaction to DEMO SHOP on card ending in 9876."
        file = imports.Activity("Chase", "card", "9876", "USD")
        file.txns = [{"date": "2026-11-14", "amount": -45.0, "desc": "DEMO SHOP", "fitid": "F1"}]
        when = datetime(2026, 11, 13, 19, 5)
        orders = [("email", "sms", "file"), ("file", "email", "sms"), ("sms", "file", "email"), ("email", "file", "sms")]
        for order in orders:
            c = mem()
            for i, src in enumerate(order):
                if src == "file":
                    imports.store_activity(c, file, "chase-9876.csv", US)
                else:
                    sync.ingest(c, {"id": f"x{i}", "sender": "24273" if src == "sms" else "no.reply.alerts@chase.com",
                                    "subject": "", "ts_epoch": (when + timedelta(minutes=i)).timestamp(),
                                    "text": text if src == "sms" else email}, src, US)
            n = c.execute("SELECT COUNT(*), COUNT(duplicate_of) FROM transactions").fetchone()
            self.assertEqual((n[0], n[0] - n[1]), (3, 1), order)
            roots = c.execute("SELECT COUNT(DISTINCT duplicate_of) FROM transactions WHERE duplicate_of IS NOT NULL")
            self.assertEqual(roots.fetchone()[0], 1, order)                   # one kept row, two copies of it

    def test_without_files_one_copy_per_kept_payment(self):
        # Rupee alerts, email and text only: unchanged, a kept payment takes one copy.
        c = mem()
        msgs = [("sms", "JD-HDFCBK-S"), ("email", "alerts@hdfcbank.net"), ("sms", "JD-HDFCBK-S")]
        for i, (src, sender) in enumerate(msgs):
            sync.ingest(c, {"id": f"r{i}", "sender": sender, "subject": "", "ts_epoch": datetime(
                2026, 11, 13, 9, i).timestamp(), "text": "Rs.450.00 debited from a/c **1234 to VPA demo@ybl DEMO"},
                src, IN)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM transactions WHERE duplicate_of IS NULL").fetchone()[0], 2)

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


class AIBalances(unittest.TestCase):
    def run_job(self, c, answers, settings):
        real = ai._ask
        seen = {}

        def fake(payload, system, schema, s):
            seen["system"], seen["schema"] = system, schema
            return answers, ""
        ai._ask = fake
        try:
            return ai.job_unread(c, settings)[0], seen
        finally:
            ai._ask = real

    def review(self, c, *snippets):
        for i, t in enumerate(snippets):
            c.execute("INSERT INTO review VALUES (?,?,?,?,?,?)", (f"email:b{i}", "email", "alerts@demo-cu.example",
                                                                 "2026-11-14T08:00:00", "no debit/credit wording", t))

    def test_balance_and_card_due_recorded_and_undone(self):
        c = mem()
        self.review(c, "Demo CU: Checking ••2222 end-of-day funds $1,500.00",
                    "Demo CU Visa ••4321: you owe $300.00, pay by Dec 5")
        answers = [{"id": "0", "verdict": "balance", "amount": 1500, "currency": "USD", "last4": "2222",
                    "account_type": "checking", "card": False, "reason": "end-of-day balance"},
                   {"id": "1", "verdict": "balance", "amount": 300, "currency": "USD", "last4": "4321",
                    "account_type": "credit_card", "card": True, "due_date": "2026-12-05", "reason": "card balance"}]
        settings = dict(US, trusted_senders=["demo-cu.example"])
        applied, seen = self.run_job(c, answers, settings)
        self.assertEqual(applied, 2)
        self.assertIs(seen["system"], ai.UNREAD_SYSTEM)
        self.assertEqual(tuple(c.execute("SELECT account, balance FROM balances").fetchone()),
                         ("demo-cu.example a/c ••2222", 1500.0))
        self.assertEqual(tuple(c.execute("SELECT account, total_due, due_date FROM card_dues").fetchone()),
                         ("demo-cu.example card ••4321", 300.0, "2026-12-05"))
        self.assertEqual(c.execute("SELECT COUNT(*) FROM review").fetchone()[0], 0)
        d = c.execute("SELECT id FROM ai_decisions WHERE key='email:b0'").fetchone()[0]
        ai.undo(c, settings, d)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM balances").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM review").fetchone()[0], 1)

    def test_loans_and_outstanding_amounts_are_never_assets(self):
        c = mem()
        self.review(c, "Demo CU: your auto loan ••5555 outstanding balance is $12,000.00",
                    "Demo CU: account ••2222 outstanding balance $800.00",
                    "Demo CU: account ••3333 balance $50.00")
        answers = [{"id": "0", "verdict": "balance", "amount": 12000, "currency": "USD", "last4": "5555",
                    "account_type": "loan", "card": False, "reason": "loan balance"},
                   {"id": "1", "verdict": "balance", "amount": 800, "currency": "USD", "last4": "2222",
                    "account_type": "checking", "card": False, "reason": "outstanding balance"},
                   {"id": "2", "verdict": "balance", "amount": 50, "currency": "USD", "last4": "3333",
                    "account_type": "other", "card": False, "reason": "a balance"}]
        applied, _ = self.run_job(c, answers, dict(US, trusted_senders=["demo-cu.example"]))
        self.assertEqual(applied, 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM balances").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM card_dues").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM review").fetchone()[0], 3)        # left to the person

    def test_rupees_at_home_reader_unchanged(self):
        c = mem()
        self.review(c, "Demo Bank: A/c XX2222 balance Rs 1,500.00")
        answers = [{"id": "0", "verdict": "balance", "amount": 1500, "currency": "INR", "last4": "2222",
                    "account_type": "savings", "card": False, "reason": "balance"}]
        applied, seen = self.run_job(c, answers, dict(IN, trusted_senders=["demo-cu.example"]))
        self.assertIs(seen["system"], ai.UNREAD_SYSTEM_INR)
        self.assertIs(seen["schema"], ai.UNREAD_SCHEMA_INR)
        self.assertNotIn("balance", ai.UNREAD_SCHEMA_INR["properties"]["decisions"]["items"]["properties"]
                         ["verdict"]["enum"])
        self.assertEqual((applied, c.execute("SELECT COUNT(*) FROM balances").fetchone()[0]), (0, 0))

if __name__ == "__main__":
    unittest.main()
