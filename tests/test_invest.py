"""Tests for the investment readers, on made-up statements and emails in the shapes the real ones use. No real data."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import sqlite3
import unittest

from margin import cas, db, epf, fd, invest, nps, ppf, prices, usstocks


def mem() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    conn.execute("CREATE TABLE fx_rates (cur TEXT NOT NULL, day TEXT NOT NULL, rate REAL NOT NULL, rate_day TEXT, "
                 "PRIMARY KEY (cur, day))")
    return conn


class OrderEmails(unittest.TestCase):
    def test_buy(self):
        o = usstocks.parse_order("BUY order of Visa Inc. for $150 is successful",
                                 "Hi, Your BUY order for Visa Inc. is successful. Your order details are as follows: "
                                 "Ticker: Visa Inc. Amount: $150 Price: $212.4 Shares: 0.706215 Order Type: Market")
        self.assertEqual((o["side"], o["name"], o["status"], o["shares"], o["price"], o["amount"]),
                         ("BUY", "Visa Inc.", "successfu", 0.706215, 212.4, 150.0))

    def test_old_spelling_and_cancelled_ticker(self):
        o = usstocks.parse_order("", "Your BUY order for Visa, Inc. is successfull. Ticker: Visa, Inc. Amount: "
                                     "$95.50 Price: $142.25 Shares: 0.67135325 Order Type: Market")
        self.assertEqual(o["status"], "successfu")
        c = usstocks.parse_order("", "Your Sell order for Costco Wholesale Corporation is cancelled. Your order details are as "
                                     "follows: Ticker: COST Order Type: stop_limit US a/c no. : XX000")
        self.assertEqual((c["side"], c["status"], c["ticker"], c["shares"]), ("SELL", "cancelled", "COST", None))

    def test_same_company_spelled_differently(self):
        self.assertEqual(usstocks.norm("Visa, Inc."), usstocks.norm("Visa Inc."))
        self.assertEqual(usstocks.norm("Goldman Sachs Group Inc"), usstocks.norm("Goldman Sachs Group Inc."))
        self.assertEqual(usstocks.norm("ICICI Bank Ltd."), usstocks.norm("Icici Bank Ltd."))


class PriceFiles(unittest.TestCase):
    def test_bhavcopy(self):
        rows = prices.parse_bhavcopy(
            "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,ClsPric,PrvsClsgPric\n"
            "2026-11-13,2026-11-13,CM,NSE,STK,1234,INE0DEMO1018,DEMOIND,EQ,742.6,735.15\n"
            "2026-11-13,2026-11-13,CM,NSE,ETF,1,INF000000001,SOMEETF,EQ,10,9\n")
        self.assertEqual(rows, [("INE0DEMO1018", "2026-11-13", 742.6, 735.15, "INR")])

    def test_amfi(self):
        rows = prices.parse_amfi("Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;"
                                 "Net Asset Value;Date\n\nDemo Mutual Fund\n\n"
                                 "100001;INF0DEMO7CF8;-;Demo Equity Fund - Direct Growth;27.4512;13-Nov-2026\n")
        self.assertEqual(rows, [("NAV:INF0DEMO7CF8", "2026-11-13", 27.4512, None, "INR")])


class USHoldings(unittest.TestCase):
    def setUp(self):
        self.c = mem()
        self.c.execute("INSERT INTO us_tickers VALUES ('costco wholesale', 'COST', 'test')")
        for d in ("2024-05-14", "2026-11-20"):
            self.c.execute("INSERT INTO fx_rates VALUES ('USD', ?, 83.0, ?)", (d, d))
        self.s = {"us_snapshot": {"as_of": "2026-11-14", "holdings": {"COST": {"shares": 2.0, "cost_usd": 380}}}}

    def order(self, mid, ts, side, shares, amount):
        self.c.execute("INSERT INTO us_orders VALUES (?,?,?,?,?,?,?)",
                       (mid, ts, side, "Costco Wholesale Corporation", shares, amount / shares, amount))

    def test_split_applies_to_earlier_buys(self):
        self.order("a", "2024-05-14T10:00:00", "BUY", 0.4, 360)
        self.c.execute("INSERT INTO splits VALUES ('COST', '2024-06-10', 10)")
        pos, notes = usstocks.positions(self.c, {}, "2024-07-01", lookup=False)
        self.assertAlmostEqual(pos["COST"]["shares"], 4.0)
        self.assertAlmostEqual(pos["COST"]["cost_inr"], 360 * 83.0)

    def test_snapshot_plus_later_orders(self):
        self.order("old", "2024-05-14T10:00:00", "BUY", 9.0, 900)        # before the snapshot: ignored
        self.order("new", "2026-11-20T10:00:00", "BUY", 0.5, 100)
        pos, _ = usstocks.positions(self.c, self.s, "2026-11-21", lookup=False)
        self.assertAlmostEqual(pos["COST"]["shares"], 2.5)
        self.assertAlmostEqual(pos["COST"]["cost_usd"], 480)

    def test_selling_everything_clears_it(self):
        self.order("s", "2026-11-20T10:00:00", "SELL", 2.0, 350)
        pos, notes = usstocks.positions(self.c, self.s, "2026-11-21", lookup=False)
        self.assertNotIn("COST", pos)
        self.assertEqual(notes, [])


class StopLoss(unittest.TestCase):
    def test_alerts_once_then_rearms(self):
        c, sent = mem(), []
        invest.set_stop(c, "INE1", "in_stock", "Demo", 100.0)
        row = lambda p: {"key": "INE1", "name": "Demo", "price": p, "day": "2026-11-13", "cur": "INR",  # noqa: E731
                         "stop": dict(c.execute("SELECT * FROM stops WHERE key='INE1'").fetchone())}
        self.assertEqual(len(invest.check_stops(c, [row(99.0)], lambda t, m: sent.append(m))), 1)
        self.assertEqual(invest.check_stops(c, [row(98.0)], lambda t, m: sent.append(m)), [])
        invest.check_stops(c, [row(101.0)], lambda t, m: sent.append(m))       # closes above: re-armed
        self.assertEqual(len(invest.check_stops(c, [row(97.0)], lambda t, m: sent.append(m))), 1)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all("Nothing was sold" in m for m in sent))


class CASParser(unittest.TestCase):
    # Made-up statement in the layout CDSL uses: one table row per line (as pdf.text(layout=True) gives it). Every
    # name, ISIN, folio and figure is invented; the layout quirks are the real ones: a transactions table that also
    # has ISINs, a ".71" with no leading zero, an ISIN split across two lines ("...6C" then "W0"), a folio wrapped
    # across columns ("12345678   90/0"), a "0 .47" split number and "#EQUITY" in names.
    LAYOUT = """Statement for the period from 01-Oct-2026 to 31-Oct-2026
STATEMENT OF TRANSACTIONS FOR THE PERIOD FROM 01-10-2026 TO 31-10-2026
INE0DEMO1018   DEMO LTD   TX:123456   16-10-2026   4.000   --   4.000   0
HOLDING STATEMENT AS ON 31-10-2026
INE0DEMO1018   12.000   --   --   --   12.000   845.6000   10,147.20
INE0DEMO2026   DEMO BANK LIMITED#EQUITY   25.000   --   --   --   25.000   612.3500   15,308.75
INF0DEMO3GB5   LTD#DEMO MF-DEMO NIFTY ETF  40.000   --   --   --   40.000   98.7600   3,950.40
Portfolio Value ` 29,406.35 as on 31-10-2026
MUTUAL FUND UNITS HELD AS ON 31-10-2026
Bluechip Fund -   INF0DEMO4AA1   1234567890/0   4210.527   58.42   2,10,000.00   2,45,978.99   35,978.99   17.13
Small Cap Fund -   INF0DEMO5BB2   1000000001/0   .71   46.812   30.00   33.24   3.24   10.80
VALUE 30   INF0DEMO6C
INDEX FUND -   W0 500000000000/0   1830.412   27.4815   45,000.00   50,302.47   5,302.47   11.78
Large Cap Fund -   INF000000001   12345678   90/0 DIRECT   100.000   10.00   900.00   1,000.00   0 .47   0 100.00   11.1
Grand Total   2,55,930.00   2,97,314.70
"""
    PLAIN = """INE0DEMO1018
DEMO INDUSTRIES LIMITED#EQUITY SHARES
12.000 -- -- -- 12.000 845.6000 10,147.20
INE0DEMO2026
DEMO BANK LIMITED#EQUITY SHARES 25.000
INF0DEMO3GB5
DEMO AM LTD#DEMO MF-DEMO NIFTY ETF
40.000
Scheme Name : Demo Bluechip Fund - Direct Growth Folio No : 1234567890/0 Mode of Holding : Single
ISIN : INF0DEMO4AA1 UCC : RTA : KFIN
Scheme Name : Demo Small Cap Fund - Direct Plan Growth Folio No : 1000000001/0 Mode
ISIN : INF0DEMO5BB2 UCC : RTA : KFIN
ABCD - DEMO NIFTY VALUE 30 INDEX FUND - DIRECT GROWTH PLAN Folio No : 500000000000/0 Mode
ISIN : INF0DEMO6CW0 UCC :
EFGH - Demo Large Cap Fund - Direct Growth Folio No : 1234567890/0 Mode
ISIN : INF000000001 UCC :
"""

    def test_reads_every_row_and_matches_totals(self):
        st = cas.parse(self.LAYOUT, self.PLAIN)
        self.assertEqual(st["as_of"], "2026-10-31")
        by = {p["key"]: p for p in st["positions"]}
        self.assertEqual(set(by), {"INE0DEMO1018", "INE0DEMO2026", "INF0DEMO3GB5", "INF0DEMO4AA1", "INF0DEMO5BB2",
                                   "INF0DEMO6CW0", "INF000000001"})
        self.assertEqual((by["INE0DEMO1018"]["units"], by["INE0DEMO1018"]["price"]), (12.0, 845.60))
        self.assertEqual(by["INE0DEMO1018"]["name"], "DEMO INDUSTRIES LIMITED")
        self.assertEqual((by["INF0DEMO3GB5"]["kind"], by["INF0DEMO3GB5"]["name"]), ("in_stock", "DEMO NIFTY ETF"))
        self.assertEqual(by["INF0DEMO5BB2"]["units"], 0.71)                         # ".71"
        w0 = by["INF0DEMO6CW0"]                                                     # ISIN split across lines
        self.assertEqual((w0["kind"], w0["units"], w0["value"], w0["cost"]), ("mf", 1830.412, 50302.47, 45000.0))
        self.assertTrue(w0["name"].startswith("DEMO NIFTY VALUE 30"))
        self.assertEqual(by["INF000000001"]["units"], 100.0)                        # folio wrapped, extra columns
        self.assertEqual(st["checks"]["demat_read"], st["checks"]["demat_printed"])  # transactions row ignored
        self.assertEqual(st["checks"]["funds_read"], st["checks"]["funds_printed"])

    def test_misread_row_is_dropped(self):
        st = cas.parse("HOLDING STATEMENT AS ON 31-10-2026\nINE0DEMO1018   12.000   845.60   99,999.00\n")
        self.assertEqual(st["positions"], [])


class EPFTexts(unittest.TestCase):
    def test_balance_text(self):
        # Member IDs: an EPFO office code (made up here: ZZDEM, ZZOLD) and digits, masked in the texts.
        e = epf.parse_text("Dear XXXXXXXX1111, your passbook balance against ZZDEM**************1234 is Rs. 6,54,321/-. "
                           "Contribution of Rs. 12,345/- for due month Jul-26 has been received.")
        self.assertEqual(e, {"member": "ZZDEM…1234", "balance": 654321.0, "contribution": 12345.0, "due_month": "Jul-26"})
        e = epf.parse_text("Dear XXXXXXXX1111,your passbook balance against ZZOLD•••••••••••••4321 is Rs. 84,250/-. "
                           "Contribution of Rs. 4,200/- for due month Aug-23 has been received.")
        self.assertEqual((e["member"], e["balance"]), ("ZZOLD…4321", 84250.0))
        self.assertIsNone(epf.parse_text("Your claim has been settled."))
        self.assertTrue(epf.is_epfo("VM-EPFOHO(smsft_fi)"))
        self.assertFalse(epf.is_epfo("HDFCBK-S"))

    # Opening 4,61,250 + 4,38,910 = 9,00,160; two months of 12% of wages from each side (18,288 x 2, 19,050 x 2).
    PASSBOOK = """Member ID/Name   ZZDEM00000000000001234 / DEMO NAME
EPF Passbook [ Financial Year - 2026-2027 ]
OB Int. Updated upto 31/03/2026   4,61,250   4,38,910 0
Mar-2026   01-04-2026   CR   Cont. for the Due-Month   202604   1,52,400   0 18,288   18,288 0
Apr-2026   01-05-2026   CR   Cont. for the Due-Month   202605   1,58,750   0 19,050   19,050 0
Int. Updated upto 31/03/2027   0 0 0
Closing Balance as on 31/03/2027   4,98,588   4,76,248 0
Printed On : 12-11-2026 10:20:15
"""

    def test_passbook(self):
        pb = epf.parse_passbook(self.PASSBOOK)
        self.assertEqual((pb["member"], pb["fy"], pb["opening"], pb["printed"]),
                         ("ZZDEM…1234", "2026-2027", ("2026-03-31", 900160.0), "2026-11-12"))
        self.assertEqual([(r["date"], r["due_month"], r["amount"]) for r in pb["rows"]],
                         [("2026-04-01", "Apr-26", 36576.0), ("2026-05-01", "May-26", 38100.0)])
        self.assertEqual(pb["checks"], {"read": 974836.0, "printed": 974836.0})

    def test_passbook_replaces_texts_for_its_months(self):
        c = mem()
        epf.store(c, "sms:1", "2026-02-27T10:00:00", "your passbook balance against ZZDEM***1234 is Rs. 8,24,310/-.")
        epf.store(c, "sms:2", "2026-04-20T10:00:00", "your passbook balance against ZZDEM***1234 is Rs. 9,36,736/-.")
        epf.store_passbook(c, epf.parse_passbook(self.PASSBOOK))
        kept = [r["uid"] for r in epf.balances(c)]
        self.assertIn("sms:1", kept)                    # before the passbook's year
        self.assertNotIn("sms:2", kept)                 # inside it: the passbook (with interest) wins
        self.assertEqual((epf.latest(c)["balance"], epf.latest(c)["source"]), (974836.0, "passbook"))

    def test_latest_counts_only_the_current_account(self):
        c = mem()
        epf.store(c, "a", "2024-03-12T10:00:00", "your passbook balance against ZZOLD•••4321 is Rs. 1,12,480/-.")
        epf.store(c, "b", "2026-07-10T10:00:00", "your passbook balance against ZZDEM***1234 is Rs. 6,54,321/-.")
        e = epf.latest(c)
        self.assertEqual((e["member"], e["balance"], e["transferred"]), ("ZZDEM…1234", 654321.0, ["ZZOLD…4321"]))


class NPSStatements(unittest.TestCase):
    # Made-up figures: units x NAV = value on every row, the printed totals are their sums, and the gain is the
    # valuation less the contributions.
    NEW = """Investment Details as on 31-10-2026
No of Contributions   Total Contribution   Total Withdrawal   Deductions due to   Current Valuation   Notional Gain/Loss
5 240000.00   0.00   412.36   277361.08   37361.08
Scheme Name   Total Units   Latest NAV   Value at NAV   XIRR
NPS TRUST- A/C SBI PENSION FUND
MANAGEMENT LIMITED SCHEME E - TIER I   4102.5517   48.7311   199921.86   8.1%
NPS TRUST- A/C SBI PENSION FUND
MANAGEMENT LIMITED SCHEME C - TIER I   1105.2290   29.8104   32947.32
NPS TRUST- A/C SBI PENSION FUND
MANAGEMENT LIMITED SCHEME G - TIER I   1640.8836   27.1146   44491.90
Total   277361.08
"""
    OLD = """Total Contribution (₹)   40000.00
Total Value (₹)   42942.76
Account Status as of Dec 31, 2021
40000.00   1 0.00   42942.76   2942.76   72.66
PFM/Scheme Name   Total Net   Total Units   Latest NAV   Value at NAV   Unrealized   Return on
SBI Pension Management   29945.98   965.5612   33.9120   32744.11   2798.13
Company Limited Scheme E - Tier I
31-Dec-21
SBI Pension Management   3992.57   182.4410   22.4115   4088.78   96.21
Company Limited Scheme C - Tier I   8.37%
SBI Pension Management   5988.79   279.1934   21.8840   6109.87   121.08
Company Limited Scheme G - Tier I
Total   39927.34   42942.76   3015.42
Total   40000.00
"""

    def test_current_layout(self):
        st = nps.parse(self.NEW)
        self.assertEqual(st["as_of"], "2026-10-31")
        self.assertEqual([p["key"] for p in st["positions"]], ["NPS:SBI:E:I", "NPS:SBI:C:I", "NPS:SBI:G:I"])
        self.assertEqual(st["checks"], {"printed": 277361.08, "read": 277361.08})
        self.assertAlmostEqual(sum(p["cost"] for p in st["positions"]), 240000.0)

    def test_2021_layout(self):
        st = nps.parse(self.OLD)
        self.assertEqual(st["as_of"], "2021-12-31")
        self.assertEqual([p["key"] for p in st["positions"]], ["NPS:SBI:E:I", "NPS:SBI:C:I", "NPS:SBI:G:I"])
        self.assertEqual(st["positions"][0]["name"], "SBI Pension Fund, Scheme E (Tier I)")
        self.assertEqual(st["checks"]["printed"], st["checks"]["read"])
        self.assertAlmostEqual(sum(p["cost"] for p in st["positions"]), 40000.0)


class FixedDeposits(unittest.TestCase):
    # Made up: 3,00,000 at 6.8% compounded quarterly for a year is 3,20,926, renewed for another year to 3,43,312.
    ADVICE = """DEPOSIT CONFIRMATION/ RENEWAL ADVICE
KAVYA MENON   Deposit Account   50100000009876
Deposit Type   REINVEST. DEPOSIT
Amount   Start Date   of Deposit   Interest (%p.a)   Date   Amount (in Rs.)
3,20,926.00 14-JUN-2026 12 Month(s) 6.8000 14-JUN-2027 3,43,312.00
Interest Payment Frequency   ON MATURITY
Maturity Instruction   RENEW PRINCIPAL AND INTEREST
"""

    def test_advice(self):
        d = fd.parse(self.ADVICE)
        self.assertEqual(d, {"account": "••9876", "principal": 320926.0, "start": "2026-06-14", "rate": 6.8,
                             "maturity": "2027-06-14", "maturity_amount": 343312.0, "reinvest": True, "renews": True})
        self.assertEqual(fd.parse("3,00,000.00 14 Jun 2025 12 Month(s) 6.8000 14 Jun 2026 3,20,926.00 "
                                  "Deposit Account 50100000009876")["start"], "2025-06-14")

    def test_value_grows_to_the_maturity_amount_and_keeps_the_first_cost(self):
        c = mem()
        c.execute(fd.SCHEMA)
        for i, (p, s0, e, m) in enumerate([(300000, "2025-06-14", "2026-06-14", 320926),
                                           (320926, "2026-06-14", "2027-06-14", 343312)]):
            c.execute("INSERT INTO deposits VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (str(i), "HDFC", "••9876", p, s0, e, 6.8, m, 1, 1, s0))
        self.assertAlmostEqual(fd.deposits(c, "2025-06-14")[0]["value"], 300000)
        self.assertAlmostEqual(fd.deposits(c, "2027-06-14")[0]["value"], 343312)
        mid = fd.deposits(c, "2027-01-10")[0]
        self.assertTrue(320926 < mid["value"] < 343312)
        self.assertEqual(mid["cost"], 300000)


class PPFInterest(unittest.TestCase):
    def test_deposit_by_the_fifth_earns_the_whole_year(self):
        c = mem()
        a = {"balance": 100000, "as_of": "2025-03-31", "deposits": [{"date": "2025-04-04", "amount": 150000}]}
        v = ppf.value_on(c, a, "2026-03-31")
        self.assertEqual((v["balance"], v["accrued"]), (100000 + 150000 + 17750, 0))     # 7.1% of 2.5L, credited 31 Mar
        late = dict(a, deposits=[{"date": "2025-04-06", "amount": 150000}])   # after the 5th: earns from May
        both = 100000 + 150000
        self.assertAlmostEqual(ppf.value_on(c, late, "2026-03-31")["balance"],
                               both + round(100000 * .071 / 12 + both * .071 * 11 / 12), places=0)

    def test_interest_accrues_until_31_march(self):
        v = ppf.value_on(mem(), {"balance": 120000, "as_of": "2026-03-31"}, "2026-09-30")
        self.assertEqual(v["balance"], 120000)
        self.assertAlmostEqual(v["accrued"], 120000 * .071 / 2)

    # A made-up account history, rows out of order as the statement prints them.
    STATEMENT = """Date of Statement :   12-11-2026
Clear Balance :   1,86,632.00CR
Account Number :   10000000001234
Interest Rate :   7.10 % p.a.
Account Open Date :   03-09-2021
04/04/2022   DEP TFR   - -   60,000.00   1,74,260.00
31/03/2022   INTEREST CREDIT - WITHIN   - -   4,260.00   1,14,260.00
20/01/2022   DEP TFR   - -   10,000.00   1,10,000.00
05/09/2021   DEP TFR   - -   75,000.00   1,00,000.00
03/09/2021   CASH DEPOSIT SELF AT   - -   25,000.00   25,000.00
31/03/2023   INTEREST CREDIT - WITHIN   - -   12,372.00   1,86,632.00
"""

    def test_sbi_statement(self):
        st = ppf.parse_statement(self.STATEMENT)
        self.assertEqual((st["account"], st["as_of"], st["balance"], st["rate"], st["opened"]),
                         ("1234", "2026-11-12", 186632.0, 7.1, "2021-09-03"))
        self.assertEqual([(t["date"], t["kind"]) for t in st["txns"]],
                         [("2021-09-03", "deposit"), ("2021-09-05", "deposit"), ("2022-01-20", "deposit"),
                          ("2022-03-31", "interest"), ("2022-04-04", "deposit"), ("2023-03-31", "interest")])
        # The scheme's rules give the statement's first-year credit: 1L in by the 5th of September earns 7 months
        # (Sep-Mar), the 10,000 paid on 20 January (after the 5th) earns from February, 2 months, at 7.1%:
        # 1,00,000 x 7.1% x 7/12 + 10,000 x 7.1% x 2/12 = 4,141.67 + 118.33 = 4,260, credited 31 March.
        acct = {"balance": 0.0, "as_of": "2021-09-02", "deposits": [{"date": "2021-09-03", "amount": 25000},
                                                                     {"date": "2021-09-05", "amount": 75000},
                                                                     {"date": "2022-01-20", "amount": 10000}]}
        self.assertEqual(ppf.value_on(mem(), acct, "2022-03-31")["balance"], 114260)

    def test_bank_alert_deposits_but_not_ppfas(self):
        c = mem()
        c.execute("INSERT INTO transactions(uid, source, ts, amount, direction, raw) VALUES "
                  "('a','sms','2026-04-03T10:00:00',50000,'out','Rs.50000 debited for transfer to PPF A/c XX1234'),"
                  "('b','sms','2026-04-07T10:00:00',9999.5,'out','Purchase in Parag Parikh Flexi Cap PPFAS')")
        self.assertEqual([d["amount"] for d in ppf.bank_deposits(c, "2026-03-31")], [50000])


if __name__ == "__main__":
    unittest.main()
