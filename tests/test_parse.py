"""Parser tests on made-up alerts in the shapes Indian banks use. No real data."""
import tests  # first: points margin at a temporary data folder (see tests/__init__.py)

import json
import unittest
from pathlib import Path

from margin import categorize, parse, paths


def one(text, subject=""):
    r = parse.parse_alert(text, subject)
    assert len(r.txns) == 1, (text, r.reason)
    return r.txns[0], r


class Senders(unittest.TestCase):
    def test_banks(self):
        self.assertEqual(parse.bank_for_sender("HDFC Bank InstaAlerts <alerts@hdfcbank.net>"), "HDFC")
        self.assertEqual(parse.bank_for_sender("JD-HDFCBK-S"), "HDFC")
        self.assertEqual(parse.bank_for_sender("VM-ICICIT"), "ICICI")
        self.assertEqual(parse.bank_for_sender("credit_cards@icicibank.com"), "ICICI")
        self.assertEqual(parse.bank_for_sender("AD-SBIUPI"), "SBI")
        self.assertEqual(parse.bank_for_sender("BZ-AXISBK"), "Axis")
        self.assertEqual(parse.bank_for_sender("Kotak <BankAlerts@kotak.com>"), "Kotak")

    def test_not_banks(self):
        self.assertIsNone(parse.bank_for_sender("Payments <no-reply@razorpay.com>"))
        self.assertIsNone(parse.bank_for_sender("Swiggy <noreply@swiggy.in>"))
        self.assertIsNone(parse.bank_for_sender("orders@relianceindustries.com"))
        self.assertIsNone(parse.bank_for_sender("+919812345678"))

    def test_trusted(self):
        self.assertEqual(parse.bank_for_sender("alerts@mynewbank.in", ["mynewbank"]), "mynewbank")


class Alerts(unittest.TestCase):
    def test_card_limit_is_not_the_amount(self):
        # Made-up alerts in an older Axis card format: "Avl Lmt" is the card's spare limit, not the spend.
        r = parse.parse_alert("Spent Card no. XX1234 INR 62.40 21-02-24 02:14:37 IST SOME SHOP. Avl Lmt INR 100000.00 "
                              "SMS BLOCK 1234 to 910000000000, if not you.")
        self.assertEqual(r.txns[0].amount, 62.40)
        self.assertIsNone(r.balance)
        # A foreign-currency spend has no rupee amount to read: left for Claude, who converts it at the day's rate.
        r = parse.parse_alert("Spent Card no. XX1234 USD 8.75 14-12-22 10:42:18 SOME SITE Avl Lmt INR 86420.5 "
                              "SMS BLOCK 1234 to 910000000000, if not you - Axis Bank")
        self.assertEqual(r.txns, [])

    def test_vpa_without_at(self):
        r = parse.parse_alert("HDFC Bank: Rs 75.00 debited from a/c **1111 on 19-05-23 to VPA someshop.storesaxisbank"
                              "(UPI Ref No 300000000000). Not you? Call on 18000000000 to report")
        self.assertEqual((r.txns[0].counterparty, r.txns[0].vpa), ("someshop.stores@axisbank", "someshop.stores@axisbank"))
        r = parse.parse_alert("HDFC Bank: Rs 50.00 debited from a/c **1111 on 31-05-23 to VPA Q123456789ybl"
                              "(UPI Ref No 300000000001). Not you? Call on 18000000000 to report")
        self.assertEqual(r.txns[0].vpa, "Q123456789@ybl")
        r = parse.parse_alert("HDFC Bank: Rs. 1800.00 credited to a/c XXXXXX1111 on 21-07-23 by a/c linked to VPA "
                              "someoneybl (UPI Ref No 300000000002).")
        self.assertEqual(r.txns[0].counterparty, "someone@ybl")

    def test_hdfc_deposit_payer(self):
        r = parse.parse_alert("Update! INR 1,23,450.00 deposited in HDFC Bank A/c XX1111 on 26-AUG-22 for ACME INDUSTRIES "
                              "PVT LTD.Avl bal INR 2,10,987.65. Cheque deposits in A/C are subject to clearing")
        self.assertEqual(r.txns[0].counterparty, "ACME INDUSTRIES PVT LTD")
        r = parse.parse_alert("Update! INR 1,11,111.00 deposited in HDFC Bank A/c XX1111 on 31-AUG-25 for NEFT "
                              "Cr-DEMO0000001-Salary 2025AUG ACME .Avl bal INR 3,12,345.00. Cheque deposits in A/C are "
                              "subject to clearing")
        self.assertEqual(r.txns[0].counterparty, "Salary 2025AUG ACME")
        r = parse.parse_alert("Update! INR 1,11,111.00 deposited in HDFC Bank A/c XX1111 on 30-APR-25 for NEFT "
                              "Cr-DEMO0000001-Salary 2025APR ACME INDUSTRI-SOME ONE-INXXXXXXXXXX0000.Avl bal INR "
                              "3,33,333.30. Cheque deposits in A/C are subject to clearing")
        self.assertEqual(r.txns[0].counterparty, "Salary 2025APR ACME INDUSTRI")
        r = parse.parse_alert("Update! INR 2,222.00 deposited in HDFC Bank A/c XX1111 on 05-OCT-25 for UPI-ARJUN MEHTA"
                              "-arjun@ybl-DEMO0000002-600000000000-Trip share.Avl bal INR 98,765.43. Cheque "
                              "deposits in A/C are subject to clearing")
        self.assertEqual((r.txns[0].counterparty, r.txns[0].vpa), ("ARJUN MEHTA", "arjun@ybl"))

    def test_report_is_not_a_payee(self):
        r = parse.parse_alert("HDFC Bank: Rs. 2700.00 debited from a/c **1111 on 07-09-22 to a/c **4321 "
                              "(UPI Ref No. 200000000000). Not you? Call on 18000000000 to report")
        self.assertEqual(r.txns[0].counterparty, "A/c ••4321")

    def test_sbi_imps_to_account(self):
        # The payee is the credited account (masked to its last 3 digits here), never the amount
        # ("debited for Rs.123456.00 on ...").
        t, _ = one("Dear Customer, Your a/c no. XXXXXXXX1111 is debited for Rs.123456.00 on 14-02-26 and "
                   "a/c XXXXXXX876 credited (IMPS Ref no 600000000000). -SBI")
        self.assertEqual((t.amount, t.direction, t.counterparty), (123456.0, "out", "A/c ••876"))
        self.assertTrue(parse.is_junk_party("Rs.123456.00"))

    def test_upi_debit(self):
        t, r = one("Dear Customer, Rs.450.00 has been debited from account **4321 to VPA swiggy@icici "
                   "SWIGGY on 09-11-26. Your UPI transaction reference number is 412345678901.")
        self.assertEqual((t.amount, t.direction, t.last4), (450.0, "out", "4321"))
        self.assertEqual((t.counterparty, t.vpa), ("SWIGGY", "swiggy@icici"))

    def test_card_spend(self):
        t, _ = one("Thank you for using your HDFC Bank Credit Card ending 9876 for Rs 2,499.00 at AMAZON PAY "
                   "INDIA on 2026-11-08:14:22:10. Authorization code:- 123456")
        self.assertEqual((t.amount, t.direction, t.kind), (2499.0, "out", "card"))
        self.assertIn("AMAZON", t.counterparty)

    def test_icici_card(self):
        t, _ = one("INR 1,250.00 spent on ICICI Bank Card XX5555 on 07-Nov-26 at ZOMATO. Avl Lmt: INR 1,48,750.00. "
                   "If not you, call 1800 2662/SMS BLOCK 5555 to 9999999999")
        self.assertEqual((t.amount, t.direction, t.last4, t.counterparty), (1250.0, "out", "5555", "ZOMATO"))
        self.assertIsNone(t.balance)   # a card limit is not a bank balance

    def test_salary_credit_with_balance(self):
        t, r = one("Your A/c XX1234 is credited with INR 85,000.00 on 01-Nov-26 by NEFT-ACME INDUSTRIES PVT "
                   "LTD SALARY OCT. Avl Bal INR 1,23,456.78")
        self.assertEqual((t.amount, t.direction, t.last4), (85000.0, "in", "1234"))
        self.assertEqual(r.balance, 123456.78)
        self.assertEqual(categorize.categorize("in", t.counterparty, "SALARY OCT"), "Salary")

    def test_sbi_style(self):
        t, _ = one("Dear UPI user A/C X9876 debited by 120.0 on date 05Nov26 trf to RAPIDO Refno 412398765432. "
                   "If not u? call 1800111109. -SBI".replace("debited by 120.0", "debited by Rs.120.0"))
        self.assertEqual((t.amount, t.direction, t.last4), (120.0, "out", "9876"))

    def test_amount_before_verb(self):
        t, _ = one("Sent Rs.1500.00 From HDFC Bank A/C *4321 To Arjun Mehta On 06/11/26 Ref 612345678901")
        self.assertEqual((t.amount, t.direction), (1500.0, "out"))
        self.assertEqual(t.counterparty, "Arjun Mehta")

    def test_refund(self):
        t, _ = one("Refund of Rs 899.00 from MYNTRA has been credited to your card ending 9876 on 04-Nov-26.")
        self.assertEqual((t.amount, t.direction), (899.0, "in"))

    def test_ignores_otp_and_failed_and_requests(self):
        for text in [
            "OTP for txn of Rs 2,000.00 at AMAZON on card 9876 is 123456. Do not share.",
            "Your transaction of Rs.202.00 to URBAN COMPANY has failed. Any amount debited will be refunded.",
            "arjun@okaxis has requested money Rs 500 from you on UPI. Approve in your app.",
            "Rs 1,111 will be debited on 05-Nov towards your SIP mandate.",
            "Congratulations! You are eligible for a pre-approved loan of Rs 5,00,000.",
        ]:
            self.assertEqual(parse.parse_alert(text).txns, [], text)

    def test_statement(self):
        r = parse.parse_alert("Your HDFC Bank Credit Card statement for card ending 9876 is ready. "
                              "Total Amount Due: Rs. 23,456.00. Minimum Amount Due: Rs. 1,180.00. "
                              "Payment Due Date: 25 Nov 2026", "Credit Card Statement")
        self.assertEqual((r.statement_due, r.last4), (23456.0, "9876"))
        self.assertEqual(r.txns, [])

    def test_balance_ignored_as_amount(self):
        t, r = one("Avl Bal Rs 50,000.00. Rs 300.00 debited from A/c XX1234 on 10-Nov-26 to DEMO POWER.")
        self.assertEqual(t.amount, 300.0)
        self.assertEqual(r.balance, 50000.0)

    def test_amount_not_read_as_card_ending(self):
        t, _ = one("We're sharing this alert about a recent UPI transaction made using your RuPay Credit Card. "
                   "Transaction Details: Rs.180.00 has been debited from your RuPay Credit Card (ending 1111) "
                   "Paid to ******1111@ybl Date: 10-11-26")
        self.assertEqual((t.amount, t.last4, t.kind), (180.0, "1111", "card"))
        t, _ = one("View: Account update for your HDFC Bank A/c Dear Customer, Rs. 1500.00 is successfully "
                   "credited to your account **2222 by VPA someone@okaxis SOME ONE on 12-04-26.")
        self.assertEqual((t.amount, t.direction, t.last4), (1500.0, "in", "2222"))


class Payees(unittest.TestCase):
    def test_notices_are_not_payments(self):
        for msg in ("INR 149.00 for Google Play will be auto debited via Axis Bank Credit Card no. XX1111 by 27-04-26.",
                    "Your Axis Bank Credit Card no. XX1111 has an overdue. Pay the min due of INR 650.00 at https://x.y ."):
            self.assertEqual(parse.parse_alert(msg).txns, [], msg)

    def test_refund_merchant_not_greeting(self):
        t, _ = one("A refund was processed to your Credit Card from the merchant Dear Customer, Greetings from "
                   "HDFC Bank! A transaction reversal of Rs. 245.00 has been initiated to your HDFC Bank Credit Card "
                   "ending 1111 From Merchant: SHOPCO Date Time: 04 Oct, 2026 at 16:20:05 Please allow 48 hours")
        self.assertEqual((t.amount, t.direction, t.counterparty), (245.0, "in", "SHOPCO"))

    def test_reversal_merchant_without_was_successful(self):
        t, _ = one("Txn reversal of INR 86.43 at HOSTCO was successful. Card no. XX1111 18-07-26 10:12:44 IST "
                   "Avl Limit: INR 50000.0 Axis Bank")
        self.assertEqual((t.amount, t.direction, t.counterparty), (86.43, "in", "HOSTCO"))

    """Formats seen in real alerts (shapes only; every name, number and handle here is made up)."""

    def party(self, text):
        r = parse.parse_alert(text)
        self.assertEqual(len(r.txns), 1, r.reason)
        return r.txns[0].counterparty, r.txns[0].vpa

    def test_card_on_upi_masked_handle(self):
        p, v = self.party("Dear Customer, Greetings from Demo Bank! We are sending this alert to help you check a "
                          "recent UPI transaction made using your Demo Credit Card. Transaction Details: Rs.250.00 "
                          "has been debited from your Demo Credit Card (ending 4321) to ********fruit9@okaxis "
                          "Date: 03-11-26 UPI Transaction Reference Number: 123456789012")
        self.assertEqual(v, "fruit9@okaxis")

    def test_towards_vpa_with_name_in_brackets(self):
        p, v = self.party("Dear Customer, Greetings from Demo Bank! Rs.120.00 is debited from your account ending "
                          "4321 towards VPA idlyshop@ybl (SRI IDLY CORNER) on 03-11-26. UPI transaction "
                          "reference no.: 123456789012.")
        self.assertEqual((p, v), ("SRI IDLY CORNER", "idlyshop@ybl"))

    def test_merchant_name_field(self):
        p, _ = self.party("INR 640.00 spent on credit card no. XX4321 03-11-26 Dear Customer, here's the summary "
                          "of your Demo Bank Credit Card Transaction: Transaction Amount: INR 640.00 Merchant "
                          "Name: BLUE BEAN CAFE Demo Bank Credit Card No. XX4321 Date & Time: 03-11-26")
        self.assertEqual(p, "BLUE BEAN CAFE")

    def test_mandate_to_name_then_date(self):
        p, _ = self.party("UPI Mandate: Sent Rs.199.00 from Demo Bank A/c 4321 To STREAMCO SERVICES 03/11/26 "
                          "Ref 123456789 Not You? Call 1800000000/SMS BLOCK UPI to 9999999999")
        self.assertEqual(p, "STREAMCO SERVICES")

    def test_card_sms_merchant_before_avl_limit(self):
        p, _ = self.party("Spent INR 899 Demo Bank Card no. XX4321 03-11-26 14:22:10 IST GADGET HUB Avl Limit: "
                          "INR 50000 Not you? SMS BLOCK 4321 to 9999999999")
        self.assertEqual(p, "GADGET HUB")

    def test_card_to_handle_then_name(self):
        p, v = self.party("Dear Customer, Rs.75.00 has been debited from your Demo Bank RuPay Credit Card XX4321 "
                          "to chai.point@ybl CHAI POINT on 03-11-26. Your UPI transaction reference number is 1234.")
        self.assertEqual((p, v), ("CHAI POINT", "chai.point@ybl"))

    def test_email_address_is_not_a_payee(self):
        r = parse.parse_alert("Rs.500.00 debited from your A/c XX4321 on 03-11-26. Queries? Write to "
                              "support@demobank.com")
        self.assertNotEqual(r.txns[0].vpa, "support@demobank.com")

    def test_upi_prefix_stripped(self):
        p, _ = self.party("Your A/c XX4321 is credited with INR 5,000.00 on 03-Nov-26 by UPI from KAVYA MENON.")
        self.assertEqual(p, "KAVYA MENON")

    def test_template_wording_is_not_a_payee(self):
        for text in [
            "Dear Customer, we wish to inform you that Rs.123.45 has been debited from your account XX4321 on 03-11-26.",
            "Rs.500.00 has been debited from your Demo Bank account ending in 4321 on 03-11-26.",
            "INR 900 spent on YOUR CREDIT CARD ENDING WITH 4321 on 03-11-26.",
        ]:
            r = parse.parse_alert(text)
            self.assertTrue(r.txns, text)
            self.assertFalse(parse.is_junk_party(r.txns[0].counterparty), r.txns[0].counterparty)
            self.assertNotRegex(r.txns[0].counterparty or "", r"(?i)been|ending|your")
        self.assertTrue(parse.is_junk_party("know your available balance"))
        self.assertFalse(parse.is_junk_party("THE BEER CAFE"))
        self.assertFalse(parse.is_junk_party("AXIS BANK LIMITED"))

    def test_alerts_without_a_payee_name(self):
        # Wording banks use when they don't name the payee must not become a payee.
        for text in [
            "INR 250.00 sent from your account XXXXXXXX4321 Sent to your beneficiary on May 03, 2026. "
            "If this transaction wasn't done by you, call 1800-0000000-Demo Bank",
            "Dear Customer, Your A/C XXXXX654321 has a debit by transfer of Rs 400.00 on 03/05/26. Avl Bal Rs 1,00,000.00.-SBI",
            "Dear Customer, ECS/NACH dishonored in Acc XXXXX654321 due to insufficient funds. "
            "Rs.590.00 debited to account as return charges.-SBI",
        ]:
            r = parse.parse_alert(text)
            self.assertTrue(r.txns, text)
            self.assertIsNone(r.txns[0].counterparty, text)
        for p in ["your beneficiary", "transfer of", "insufficient funds"]:
            self.assertTrue(parse.is_junk_party(p), p)

    def test_bounce_charges_are_bank_charges(self):
        from margin import categorize
        text = ("Dear Customer, ECS/NACH dishonored in Acc XXXXX654321 due to insufficient funds. "
                "Rs.590.00 debited to account as return charges.-SBI")
        self.assertEqual(categorize.categorize("out", None, text), "Bank charges")
        text = ("Dear Customer, Your A/C ending with 1111 has been debited for INR 354.0 on 18-02-26 towards annual "
                "maintenance charges for your SBI Debit Card ending with 2222")   # not Rent & housing (society maintenance)
        self.assertEqual(categorize.categorize("out", None, text), "Bank charges")

    def test_towards_merchant_after_template_to(self):
        p, _ = self.party("Greetings from Demo Bank. We would like to inform you that Rs. 450.00 has been debited "
                          "from your Demo Bank Credit Card ending 4321 towards QUICK MART PRIVATE on 03 Nov, 2026 "
                          "at 14:05:10 . To check your available balance, you may use the app.")
        self.assertEqual(p, "QUICK MART PRIVATE")

    def test_advance_autodebit_notice_is_not_a_payment(self):
        r = parse.parse_alert("INR 129 Merchant Name: STREAMCO To be debited by: 05-11-26 Max Limit: INR 500 Demo "
                              "Bank Credit card No. XX4321 Please keep sufficient limit on the card for the auto debit.")
        self.assertEqual(r.txns, [])

    def test_foreign_amount_not_in_payee(self):
        p, _ = self.party("Rs.4,210.00 spent on your Demo Bank Credit Card XX4321 at CLOUDHOST USD 49.99 on 09-11-26.")
        self.assertEqual(p, "CLOUDHOST")

    def test_emi_conversion_offer_is_not_a_payment(self):
        r = parse.parse_alert("Convert your transaction of INR 1,08,640 into easy monthly instalments with tenure up "
                              "to 48 months with Flexi EMI on your Demo Bank Credit Card. Convert now. *T&C.")
        self.assertEqual(r.txns, [])

    def test_nach_payee(self):
        p, _ = self.party("Dear Customer, Your A/C XXXXX1234 has a debit by NACH of Rs 43,750.00 on 09/10/26. Avl Bal Rs 1,000.")
        self.assertEqual(p, "NACH")

    def test_fund_houses_and_insurers_are_not_banks(self):
        for sender in ("SBIMF <noreply@sbimf.com>", "Axis Max Life <service@axismaxlife.com>",
                       "alerts@hdfcsec.com", "ICICIPRULIFE"):
            self.assertIsNone(parse.bank_for_sender(sender), sender)
        self.assertEqual(parse.bank_for_sender("alerts@hdfcbank.net"), "HDFC")
        self.assertEqual(parse.bank_for_sender("VM-SBIINB"), "SBI")

    def test_rtgs_to_payee(self):
        p, _ = self.party("You have successfully initiated RTGS transaction of Rs. 222222 from your Demo Bank A/c "
                          "XX5555 for a transfer to payee Kavya Menon using Demo Bank Online Banking. Not you? Call.")
        self.assertEqual(p, "Kavya Menon")

    def test_beneficiary_credit_notice_skipped(self):
        r = parse.parse_alert("RTGS Transaction with reference number DEMOR1234 for INR 2,22,222.00 has been credited "
                              "to the beneficiary account number XX2222 on 17-02-2025.")
        self.assertEqual(r.txns, [])

    def test_transfer_to_account_number(self):
        p, _ = self.party("IMPS INR 1,11,111.00 sent from Demo Bank A/c XX5555 on 11-08-25 To A/c xxxxxxxxxx9876 "
                          "Ref-123456 Not you?Call 1800/SMS BLOCK OB to 9999")
        self.assertEqual(p, "A/c ••9876")

    def test_time_is_not_a_merchant(self):
        r = parse.parse_alert("Spent INR 320 at 14:05 on Nov 3 2026. If it wasn't done by you, ping us. -Demo Bank")
        self.assertIsNone(r.txns[0].counterparty)


class Categories(unittest.TestCase):
    def test_defaults(self):
        c = categorize.categorize
        self.assertEqual(c("out", "swiggy@icici", ""), "Food & dining")
        self.assertEqual(c("out", "ZOMATO", ""), "Food & dining")
        self.assertEqual(c("out", "RAPIDO", ""), "Transport")
        self.assertEqual(c("out", "AMAZON PAY INDIA", ""), "Shopping")
        self.assertEqual(c("out", "ZERODHA BROKING", ""), "Investments")
        self.assertEqual(c("out", "CRED CLUB", ""), "Transfer")
        self.assertEqual(c("in", "", "payment received towards your credit card"), "Transfer")
        self.assertEqual(c("in", "", "Payment of Rs 5000 was credited to your card ending 4321"), "Transfer")
        self.assertEqual(c("in", "", "PAYMENT OF RS. 5000 RECEIVED TOWARDS YOUR CREDIT CARD ENDING WITH 4321"), "Transfer")
        self.assertEqual(c("out", "Arjun Mehta", ""), "Other spending")
        self.assertEqual(c("out", "AXIS BANK LIMITED", ""), "Transfer")

    def test_tests_use_a_temporary_data_folder(self):
        self.assertEqual(paths.DATA_DIR, Path(tests.DATA_DIR))
        self.assertNotEqual(paths.DATA_DIR, Path.home() / "Library" / "Application Support" / "Margin")

    def test_own_rules_are_checked_before_the_defaults(self):
        # Your rules live in rules.json in the data folder (a temporary one here, never the real one).
        self.assertEqual(paths.RULES_PATH.parent, Path(tests.DATA_DIR))
        c = categorize.categorize
        self.assertEqual(c("in", "DEMO REWARDS", "payout"), "Other income")
        self.assertEqual(c("out", "SWIGGY", ""), "Food & dining")
        before = paths.RULES_PATH.read_text() if paths.RULES_PATH.exists() else None
        paths.RULES_PATH.write_text(json.dumps([
            {"match": "demo rewards", "category": "Side income", "direction": "in"},
            {"match": "swiggy", "category": "Friends & family", "direction": "out", "text": False}]))
        try:
            categorize.reload_rules()
            self.assertEqual(c("in", "DEMO REWARDS", "payout"), "Side income")      # a category of your own
            self.assertEqual(c("out", "SWIGGY", ""), "Friends & family")            # wins over the default rule
            self.assertEqual(c("out", "ZOMATO", ""), "Food & dining")               # the defaults still apply after
        finally:
            if before is None:
                paths.RULES_PATH.unlink()
            else:
                paths.RULES_PATH.write_text(before)
            categorize.reload_rules()
        self.assertEqual(c("in", "DEMO REWARDS", "payout"), "Other income")

    def test_no_substring_false_positives(self):
        c = categorize.categorize
        self.assertEqual(c("in", "", "your account is credited"), "Other income")   # 'cred' != 'credited'
        self.assertEqual(c("out", "CURRENT ELECTRONICS", ""), "Other spending")     # 'rent' != 'current'


if __name__ == "__main__":
    unittest.main()
