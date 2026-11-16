"""Bank, card and broker files downloaded into the statements folder (imports.py): US banks' CSV and QFX/OFX
activity, broker positions, retirement accounts, counting once with alerts, and valuing positions. Every file,
name, number and amount here is made up."""
import tests  # noqa: F401  (first: points margin at a temporary data folder, see tests/__init__.py)

import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from margin import imports, invest, networth, paths, sync
from tests.test_us import IN, US, mem


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="margin-files-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def file(self, name, text):
        p = self.dir / name
        p.write_text(text)
        return p

    def acts(self, name, text, home="USD"):
        p = self.file(name, text)
        self.assertEqual(imports.classify(p, home), "bank")
        return imports.read_ofx(p, home) if p.suffix.lower() in (".ofx", ".qfx") else imports.read_csv(p, home)

    def test_checking_with_running_balance(self):
        a, = self.acts("Chase1234_Activity_20261114.CSV",
                       "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
                       "DEBIT,11/14/2026,\"DEMO MARKET 1234 SEATTLE WA\",-45.00,DEBIT_CARD,2955.00,,\n"
                       "CREDIT,11/13/2026,\"ACME CORP PAYROLL PPD ID: 1234567890\",2000.00,ACH_CREDIT,3000.00,,\n"
                       "DEBIT,11/12/2026,\"ZELLE PAYMENT TO MARIA GARCIA JPM99ABCDEF\",-50.00,QUICKPAY_DEBIT,1000.00,,\n")
        self.assertEqual((a.bank, a.kind, a.last4, a.account), ("Chase", "account", "1234", "Chase a/c ••1234"))
        self.assertEqual([t["amount"] for t in a.txns], [-45.0, 2000.0, -50.0])
        self.assertEqual(a.balance, ("2026-11-14", 2955.0))
        self.assertEqual(imports.payee(a.txns[2]["desc"]), "MARIA GARCIA")
        self.assertEqual(imports.payee(a.txns[1]["desc"]), "ACME CORP PAYROLL")

    def test_card_signs(self):
        # Chase-style: purchases negative. Discover-style: purchases positive. Both are read as money out.
        a, = self.acts("chase-card-9876.csv", "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
                       "11/14/2026,11/15/2026,DEMO SHOP,Shopping,Sale,-45.00,\n"
                       "11/10/2026,11/10/2026,AUTOMATIC PAYMENT - THANK YOU,,Payment,500.00,\n")
        self.assertEqual((a.kind, [t["amount"] for t in a.txns]), ("card", [-45.0, 500.0]))
        a, = self.acts("Discover-Statement-1234.csv", "Trans. Date,Post Date,Description,Amount,Category\n"
                       "11/14/2026,11/15/2026,DEMO SHOP,45.00,Merchandise\n"
                       "11/10/2026,11/10/2026,INTERNET PAYMENT - THANK YOU,-500.00,Payments and Credits\n")
        self.assertEqual((a.bank, a.kind, a.last4, [t["amount"] for t in a.txns]), ("Discover", "card", "1234",
                                                                                     [-45.0, 500.0]))
        a, = self.acts("capital-one.csv", "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
                       "2026-11-14,2026-11-15,1111,DEMO SHOP,Merchandise,45.00,\n"
                       "2026-11-10,2026-11-10,1111,CAPITAL ONE AUTOPAY PYMT,Payment/Credit,,500.00\n")
        self.assertEqual((a.kind, a.last4, [t["amount"] for t in a.txns]), ("card", "1111", [-45.0, 500.0]))

    def test_summary_block_and_headerless(self):
        a, = self.acts("bofa-checking-5555.csv",
                       "Description,,Summary Amt.\nBeginning balance as of 11/01/2026,,\"1,000.00\"\n\n"
                       "Date,Description,Amount,Running Bal.\n11/01/2026,Beginning balance as of 11/01/2026,,\"1,000.00\"\n"
                       "11/02/2026,\"DEMO STORE 11/02 PURCHASE SEATTLE WA\",-20.00,980.00\n"
                       "11/03/2026,\"ACME CORP DES:PAYROLL ID:XXXXX INDN:JORDAN LEE CO ID:XXXXX PPD\",\"1,500.00\",\"2,480.00\"\n")
        self.assertEqual((a.bank, a.last4, len(a.txns), a.balance), ("Bank of America", "5555", 2, ("2026-11-03", 2480.0)))
        self.assertEqual(imports.payee(a.txns[1]["desc"]), "ACME CORP")
        a, = self.acts("wellsfargo-2222.csv", '"11/14/2026","-45.00","*","","PURCHASE AUTHORIZED ON 11/13 DEMO SHOP"\n'
                                               '"11/13/2026","2000.00","*","","ACME CORP PAYROLL"\n')
        self.assertEqual((a.bank, [t["amount"] for t in a.txns]), ("Wells Fargo", [-45.0, 2000.0]))

    def test_ofx(self):
        a, = self.acts("demo.qfx", "OFXHEADER:100\n<OFX><SIGNONMSGSRSV1><SONRS><FI><ORG>DEMOBANK</ORG></FI></SONRS>"
                       "</SIGNONMSGSRSV1><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>USD<CCACCTFROM>"
                       "<ACCTID>4000000000009876</CCACCTFROM><BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>"
                       "20261114120000<TRNAMT>-45.00<FITID>2026111401<NAME>DEMO SHOP</STMTTRN><STMTTRN><TRNTYPE>CREDIT"
                       "<DTPOSTED>20261110<TRNAMT>500.00<FITID>2026111002<NAME>PAYMENT THANK YOU</STMTTRN>"
                       "</BANKTRANLIST><LEDGERBAL><BALAMT>-1234.56<DTASOF>20261114</LEDGERBAL></CCSTMTRS>"
                       "</CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>")
        self.assertEqual((a.kind, a.last4, a.cur, len(a.txns), a.balance), ("card", "9876", "USD", 2,
                                                                            ("2026-11-14", -1234.56)))
        c = mem()
        imports.store_activity(c, a, "demo.qfx", US)
        self.assertEqual(tuple(c.execute("SELECT account, total_due FROM card_dues").fetchone()),
                         ("Demobank card ••9876", 1234.56))

    def test_counted_once_with_alerts_and_overlapping_files(self):
        c = mem()
        sync.ingest(c, {"id": "a1", "sender": "Chase <no.reply.alerts@chase.com>", "subject": "",
                        "ts_epoch": datetime(2026, 11, 12, 19, 5).timestamp(),
                        "text": "Chase: You made a $45.00 transaction with DEMO SHOP on card ending in 9876."},
                    "email", US)
        text = ("Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
                "11/14/2026,11/15/2026,DEMO SHOP,Shopping,Sale,-45.00,\n"
                "11/14/2026,11/15/2026,DEMO CAFE,Food & Drink,Sale,-4.50,\n"
                "11/14/2026,11/15/2026,DEMO CAFE,Food & Drink,Sale,-4.50,\n")
        for name in ("Chase9876_Activity_A.CSV", "Chase9876_Activity_B.CSV"):    # the same days downloaded twice
            for a in imports.read_csv(self.file(name, text), "USD"):
                imports.store_activity(c, a, name, US)
        counted = c.execute("SELECT amount FROM transactions WHERE duplicate_of IS NULL ORDER BY amount").fetchall()
        self.assertEqual([r[0] for r in counted], [4.5, 4.5, 45.0])   # two coffees, one shop (the alert's)
        self.assertEqual(c.execute("SELECT source FROM transactions WHERE amount=45 AND duplicate_of IS NULL")
                         .fetchone()[0], "email")

    def test_fidelity_positions(self):
        p = self.file("Portfolio_Positions_Nov-14-2026.csv",
                      "Account Number,Account Name,Symbol,Description,Quantity,Last Price,Last Price Change,"
                      "Current Value,Today's Gain/Loss Dollar,Today's Gain/Loss Percent,Total Gain/Loss Dollar,"
                      "Total Gain/Loss Percent,Percent Of Account,Cost Basis Total,Average Cost Basis,Type\n"
                      "Z00001234,Individual,SPAXX**,HELD IN MONEY MARKET,,,,$1234.56,,,,,10%,,,Cash,\n"
                      "Z00001234,Individual,VOO,DEMO S&P 500 ETF,10,$500.00,+$1.00,$5000.00,,,,,,$4000.00,$400.00,Cash,\n"
                      "X00005678,ROTH IRA,VTI,DEMO TOTAL MARKET ETF,5.5,$290.00,,$1595.00,,,,,,$1100.00,$200.00,Cash,\n"
                      "00009876,DEMO CORP 401(K) PLAN,12345X678,DEMO TARGET 2055 FUND,100,$20.00,,$2000.00,,,,,,,,,\n"
                      "Pending Activity,,,,,,,$-10.00,,,,,,,,\n\n"
                      "\"The data and information in this spreadsheet is provided to you solely for your use.\"\n"
                      "\"Date downloaded Nov-14-2026 9:00 p.m ET\"\n")
        self.assertEqual(imports.classify(p), "positions")
        accts = {a["account"]: a for a in imports.read_positions(p)}
        self.assertEqual(set(accts), {"Fidelity Individual ••1234", "Fidelity ROTH IRA ••5678",
                                      "Fidelity DEMO CORP 401(K) PLAN ••9876"})
        ind = accts["Fidelity Individual ••1234"]
        self.assertEqual((ind["as_of"], ind["retirement"]), ("2026-11-14", False))
        self.assertEqual([(x["ticker"], x["shares"], x["cost"]) for x in ind["positions"]],
                         [(None, 1, 1234.56), ("VOO", 10.0, 4000.0)])
        self.assertTrue(accts["Fidelity ROTH IRA ••5678"]["retirement"])
        k = accts["Fidelity DEMO CORP 401(K) PLAN ••9876"]
        self.assertTrue(k["retirement"])
        self.assertEqual((k["positions"][0]["ticker"], k["positions"][0]["value"]), (None, 2000.0))   # no ticker

    def test_schwab_vanguard_etrade_robinhood(self):
        s = self.file("Individual-Positions-2026-11-14-210000.csv",
                      '"Positions for account Individual ...123 as of 09:00 PM ET, 2026/11/14"\n\n'
                      '"Symbol","Description","Qty (Quantity)","Price","Mkt Val (Market Value)","Cost Basis",'
                      '"Security Type"\n"VOO","DEMO S&P 500 ETF","10","$500.00","$5,000.00","$4,000.00","ETFs"\n'
                      '"Cash & Cash Investments","--","--","--","$250.00","--","Cash and Money Market"\n'
                      '"Account Total","--","--","--","$5,250.00","--",""\n')
        a, = imports.read_positions(s)
        self.assertEqual((a["account"], a["as_of"]), ("Schwab Individual", "2026-11-14"))
        self.assertEqual([(x["ticker"], x["shares"], x["value"]) for x in a["positions"]],
                         [("VOO", 10.0, 5000.0), (None, 1, 250.0)])
        v = self.file("vanguard-roth-ira-OfxDownload.csv",
                      "Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,\n"
                      "00001111,DEMO TOTAL STOCK MARKET INDEX ADMIRAL,VTSAX,100.5,120.00,12060.00,\n\n\n"
                      "Account Number,Trade Date,Settlement Date,Transaction Type,Transaction Description,"
                      "Investment Name,Symbol,Shares,Share Price,Principal Amount,Commission Fees,Net Amount,\n"
                      "00001111,2026-11-01,2026-11-02,Buy,Buy,DEMO TOTAL STOCK,VTSAX,1.0,120.00,-120.00,0,-120.00,\n")
        a, = imports.read_positions(v)
        self.assertEqual((a["account"], a["retirement"], a["positions"][0]["ticker"]), ("Vanguard ••1111", True, "VTSAX"))
        e = self.file("PortfolioDownload.csv",
                      "Account Summary\nAccount,Net Account Value,Total Gain $\nIndividual Brokerage -1234,6234.56,1000\n\n"
                      "View Summary - All Positions\n\n"
                      "Symbol,Last Price $,Change $,Change %,Quantity,Price Paid $,Day's Gain $,Total Gain $,"
                      "Total Gain %,Value $\nVOO,500.00,1.00,0.2,10,400.00,10.00,1000.00,25.00,5000.00\n"
                      "CASH,,,,,,,,,1234.56\nTOTAL,,,,,,,,,6234.56\n")
        a, = imports.read_positions(e)
        self.assertEqual((a["account"], [(x["ticker"], x["cost"]) for x in a["positions"]]),
                         ("E*TRADE Individual Brokerage ••1234", [("VOO", 4000.0), (None, 1234.56)]))
        r = self.file("robinhood-activity.csv",
                      '"Activity Date","Process Date","Settle Date","Instrument","Description","Trans Code",'
                      '"Quantity","Price","Amount"\n'
                      '"11/10/2026","11/10/2026","11/12/2026","VOO","DEMO S&P 500 ETF\nCUSIP: 000000000","Sell","2",'
                      '"$500.00","$1,000.00"\n'
                      '"06/02/2026","06/02/2026","06/04/2026","VOO","DEMO S&P 500 ETF","Buy","4","$450.00","($1,800.00)"\n'
                      '"05/01/2026","05/01/2026","05/01/2026","","ACH Deposit","ACH","","","$2,000.00"\n')
        self.assertEqual(imports.classify(r), "positions")
        a, = imports.read_positions(r)
        self.assertEqual((a["account"], a["as_of"]), ("Robinhood", "2026-11-10"))
        self.assertEqual([(x["ticker"], x["shares"], x["cost"]) for x in a["positions"]], [("VOO", 2.0, 900.0)])

    def test_investment_ofx(self):
        p = self.file("fidelity-ira.qfx",
                      "<OFX><INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS><DTASOF>20261114<CURDEF>USD<INVACCTFROM>"
                      "<BROKERID>fidelity.com<ACCTID>X00005678</INVACCTFROM><INVPOSLIST><POSSTOCK><INVPOS><SECID>"
                      "<UNIQUEID>000000001<UNIQUEIDTYPE>CUSIP</SECID><HELDINACCT>CASH<POSTYPE>LONG<UNITS>10"
                      "<UNITPRICE>500.00<MKTVAL>5000.00<DTPRICEASOF>20261114</INVPOS></POSSTOCK></INVPOSLIST><INVBAL>"
                      "<AVAILCASH>100.00</INVBAL></INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1><SECLISTMSGSRSV1>"
                      "<SECLIST><STOCKINFO><SECINFO><SECID><UNIQUEID>000000001<UNIQUEIDTYPE>CUSIP</SECID>"
                      "<SECNAME>DEMO S&P 500 ETF<TICKER>VOO</SECINFO></STOCKINFO></SECLIST></SECLISTMSGSRSV1></OFX>")
        self.assertEqual(imports.classify(p), "positions")
        a, = imports.read_positions(p)
        self.assertEqual((a["account"], a["as_of"], a["retirement"]), ("Fidelity ••5678", "2026-11-14", True))
        self.assertEqual([(x["ticker"], x["shares"], x["value"]) for x in a["positions"]],
                         [("VOO", 10.0, 5000.0), (None, 1, 100.0)])

    def test_names_are_whole_words(self):
        self.assertIsNone(imports._bank_from("mf purchase history"))           # not "chase"
        self.assertIsNone(imports._bank_from("discovery fund units"))          # not "discover"
        self.assertEqual(imports._bank_from("Chase1234_Activity_20261114"), "Chase")
        self.assertEqual(imports._bank_from("discover-card-1234"), "Discover")

    def test_rupees_at_home_needs_a_us_name_in_the_file_name(self):
        # Made-up Indian files that say "Purchase" inside: they stay with networth.import_holdings.
        p = self.file("demo-holdings.csv", "Symbol,Quantity,Avg Purchase Price,Current Value\nDEMOIND,10,700,7426\n")
        self.assertIsNone(imports.classify(p, "INR"))
        q = self.file("sip-history.csv", "Date,Scheme,Transaction Type,Amount\n"
                                         "05/11/2026,DEMO FLEXI CAP FUND,SIP Purchase,5000\n")
        self.assertIsNone(imports.classify(q, "INR"))
        r = self.file("statement.csv", "Note: exported from Chase\nDetails,Posting Date,Description,Amount,Type,Balance\n"
                                       "DEBIT,11/14/2026,DEMO SHOP,-45.00,DEBIT_CARD,955.00\n")
        self.assertIsNone(imports.classify(r, "INR"))                         # the name must say it
        self.assertEqual(imports.classify(r, "USD"), "bank")

    def test_name_fields_never_stored(self):
        c = mem()
        a = imports.Activity("Bank of America", "account", "5555", "USD")
        a.txns = [{"date": "2026-11-03", "amount": 1500.0, "fitid": None,
                   "desc": "ACME CORP DES:PAYROLL ID:XXXXX INDN:JORDAN LEE CO ID:XXXXX PPD"},
                  {"date": "2026-11-04", "amount": 900.0, "fitid": None,
                   "desc": "ORIG CO NAME:ACME CORP ORIG ID:1234567890 DESC DATE:261104 CO ENTRY DESCR:PAYROLL "
                           "SEC:PPD TRACE#:000000000000001 EED:261104 IND ID:0000 IND NAME:JORDAN LEE TRN: 0000TC"}]
        imports.store_activity(c, a, "bofa-checking-5555.csv", US)
        for r in c.execute("SELECT raw, counterparty, category FROM transactions"):
            self.assertNotIn("JORDAN", r["raw"])
            self.assertNotIn("XXXXX", r["raw"])
            self.assertNotIn("1234567890", r["raw"])
            self.assertEqual((r["counterparty"], r["category"]), ("ACME CORP", "Salary"))

    def test_rupees_at_home_leaves_indian_csvs_alone(self):
        # A holdings export from an Indian app (made-up) stays with networth.import_holdings, read as before.
        p = self.file("groww-stocks.csv", "Stock Name,Symbol,Quantity,Average buy price,Current Value\n"
                                          "DEMO INDUSTRIES,DEMOIND,10,700,7426\n")
        self.assertIsNone(imports.classify(p, "INR"))
        self.assertEqual(imports.classify(p, "USD"), "positions")
        q = self.file("Chase1234_Activity.CSV", "Details,Posting Date,Description,Amount,Type,Balance\n"
                                                "DEBIT,11/14/2026,DEMO SHOP,-45.00,DEBIT_CARD,955.00\n")
        self.assertEqual(imports.classify(q, "INR"), "bank")       # a US bank's file: read, in dollars


class Valuing(unittest.TestCase):
    def setUp(self):
        self.saved = paths.SETTINGS_PATH.read_text() if paths.SETTINGS_PATH.exists() else None
        paths.STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for f in paths.STATEMENTS_DIR.glob("*"):
            f.unlink()
        if self.saved is None:
            paths.SETTINGS_PATH.unlink(missing_ok=True)
        else:
            paths.SETTINGS_PATH.write_text(self.saved)

    def test_positions_valued_at_the_latest_close_and_split(self):
        c = mem()
        imports.store_positions(c, [{"account": "Demo Broker Roth IRA ••5678", "as_of": "2026-11-01",
                                     "retirement": True,
                                     "positions": [{"ticker": "VOO", "name": "DEMO S&P 500 ETF", "shares": 10,
                                                    "price": 500.0, "value": 5000.0, "cost": 4000.0},
                                                   {"ticker": None, "name": "DEMO TARGET 2055", "shares": 100,
                                                    "price": 20.0, "value": 2000.0, "cost": None}]}], "x.csv")
        c.execute("INSERT INTO prices VALUES ('VOO', '2026-11-13', 260.0, 255.0, 'USD')")
        c.execute("INSERT INTO splits VALUES ('VOO', '2026-11-05', 2.0)")
        rows, _ = invest.holdings(c, US)
        voo = next(h for h in rows if h["name"].startswith("DEMO S&P"))
        self.assertEqual((voo["kind"], voo["label"], voo["units"], voo["value"], voo["cost"]),
                         ("retirement", "Retirement", 20.0, 5200.0, 4000.0))
        self.assertIn("Roth IRA", voo["name"])
        fund = next(h for h in rows if h["name"].startswith("DEMO TARGET"))
        self.assertEqual(fund["value"], 2000.0)                  # no ticker: the file's own value
        self.assertEqual(imports.tickers(c), ["VOO"])

    def test_newest_file_per_account(self):
        c = mem()
        for day, shares in (("2026-10-01", 5), ("2026-11-01", 7)):
            imports.store_positions(c, [{"account": "Demo Broker ••1234", "as_of": day, "retirement": False,
                                         "positions": [{"ticker": "VTI", "name": "VTI", "shares": shares,
                                                        "price": 1.0, "value": shares, "cost": None}]}], day)
        self.assertEqual([p["units"] for p in imports.broker_positions(c)], [7])

    def test_fixed_value_csvs_still_read_and_ours_skipped(self):
        paths.SETTINGS_PATH.write_text(json.dumps({"currency": "USD"}))
        (paths.STATEMENTS_DIR / "kuvera-funds.csv").write_text("Scheme Name,Units,Current Value\nDEMO FUND,10,1500\n")
        (paths.STATEMENTS_DIR / "Portfolio_Positions_Nov-14-2026.csv").write_text(
            "Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value\n"
            "Z00001234,Individual,VOO,DEMO S&P 500 ETF,10,$500.00,$5000.00\n")
        c = mem()
        self.assertEqual(networth.import_holdings(c), [])
        self.assertEqual([(r["name"], r["value"]) for r in c.execute("SELECT name, value FROM holdings")],
                         [("DEMO FUND", 1500.0)])
        self.assertEqual(imports.read(c, US), [])
        self.assertEqual(len(imports.broker_positions(c)), 1)
        self.assertEqual(imports.read(c, US), [])               # unchanged files aren't read again

    def test_nothing_indian_no_indian_fetches(self):
        c = mem()
        self.assertFalse(invest.holds_indian_market(c))
        self.assertFalse(invest.uses_indmoney(c, US))
        self.assertTrue(invest.uses_indmoney(c, IN))
        self.assertTrue(invest.uses_indmoney(c, dict(US, indmoney=True)))
        c.execute("INSERT INTO inv_statements VALUES ('cas1', 'cas', '2026-10-31', '', 1, '')")
        c.execute("INSERT INTO inv_positions(stmt, as_of, kind, key, name, units) VALUES "
                  "('cas1', '2026-10-31', 'mf', 'INF000D01011', 'Demo Fund', 1)")
        self.assertTrue(invest.holds_indian_market(c))


if __name__ == "__main__":
    unittest.main()
