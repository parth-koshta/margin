# Margin

**Your money, on your Mac.** Margin shows where your money comes from and where it goes, what your investments are
doing, and your net worth. It reads your bank and card alerts (Gmail and the text messages your iPhone forwards to
your Mac), your investment statements, and anything you add by hand, then syncs every night at 00:00 by itself.
Everything is stored and shown on your Mac only, in the Margin app's own window (or at <http://margin.localhost/>
in a browser), behind Touch ID.

- **Spending**: money in and out by month or pay cycle, by category and payee, with rules you set once.
- **Investments**: stocks, funds, retirement accounts, deposits and more, valued daily, with stop-loss alerts
  (alerts only: Margin never trades).
- **Reviews**: daily, weekly and monthly summaries, plus checks for double charges, unusual amounts and new cards.
- **Sorted by Claude** (optional): Claude Code on your Mac sorts what fixed rules can't, and every change can be
  undone.
- **Any home currency**: amounts in other currencies are converted at the day's rate and keep their original
  (“£8.45 → $10.73”). Each currency is written its own way: ₹1,23,456 · $123,456 · ¥12,346.

Built for **macOS**. It reads **Indian banks'** alerts out of the box (HDFC, ICICI, SBI, Axis, Kotak and more)
and **US banks'** (Chase, Bank of America, Wells Fargo, Capital One, Discover, Citi, Amex and more), plus CDSL, NPS,
EPFO, PPF and fixed-deposit statements, and the bank, card and broker files you download. With Claude sorting
turned on, alerts in other formats and other currencies are read too; see
[Other countries](#other-countries-and-currencies).

## Try it first: the demo

```bash
git clone https://github.com/<you>/margin ~/projects/margin   # wherever you like
cd ~/projects/margin
./margin-cli demo
```

This opens <http://demo.margin.localhost:8765/> on a made-up person's money: Jordan in Seattle, in dollars.
`./margin-cli demo --india` shows Kavya in Kochi, in rupees, with EPF, PPF and NPS. Everything works: Sync now
plays a pretend sync, “Ask Claude to sort now” and the reviews are answered by a stand-in, and `--lock` starts
at the lock screen with a simulated Touch ID. The demo lives in `~/Library/Caches/Margin Demo/`, rebuilds
itself each day so it always ends today, and whenever Margin's code changes (`--fresh` rebuilds now). It never
touches real data or the internet. `--app` opens it in the Margin app's own window, with Margin in the menu bar,
instead of the browser (quitting the app stops the demo).
Use it for screenshots and videos: it's the only Margin that should ever be on screen in public.

## Set it up

**With a coding agent** (Claude Code or similar): open this folder and ask it to set Margin up. It follows
[AGENTS.md](AGENTS.md), which covers every step, the checks after each one, and the steps only you can do
(signing in to Google, granting Full Disk Access, typing statement passwords into hidden prompts).

**By hand**: follow the same steps in [AGENTS.md](AGENTS.md#setup), in order. You need macOS with the Xcode
command line tools (`xcode-select --install`, which also provides Python 3.9+ and Swift), and optionally
[Claude Code](https://claude.com/claude-code) for sorting and reviews.

## Privacy

- **Your data stays on your Mac**, in `~/Library/Application Support/Margin/`, readable only by your Mac user
  (folders 0700, files 0600). The project folder holds code only, and the [leak guard](#leak-guard) keeps it
  that way.
- **Gmail is read-only**, through your own Google sign-in. The token is in your macOS Keychain (service
  `margin`). Margin can't send, delete or change mail.
- **The Margin app** shows the page in a window of its own: a small program built on your Mac from
  `margin/macapp.swift` (AppKit and WebKit, no Xcode project, no code from anyone else). Nothing from the page is
  kept on disk (no history, cache, cookies or autofill), browser extensions can't see it, and links to other sites
  open in your browser. It reads the page's key from your data folder and hands it over in a private cookie, never
  in an address. Without the Xcode command line tools, the Margin app opens the page in your browser instead.
- **The menu bar** shows only Margin's m, never a figure (anyone near your screen, or a screen recording, sees the
  menu bar). Click it for a quick glance: spent today, this month against last, net worth, what's waiting in Sort
  out and the last sync, with Sync now, Lock and Open Margin. The figures are fetched while it's open and dropped
  when it closes; while Margin is locked it shows none, only "Unlock with Touch ID". Closing the window keeps
  Margin in the menu bar; Quit ends it. "Open at Login" in its ⋯ menu is off until you turn it on.
- **The page** at <http://margin.localhost/> is served by a tiny server on your Mac only (127.0.0.1 and ::1, not
  reachable from other machines). `*.localhost` always means your own Mac, so nothing on a shared Wi-Fi can
  pretend to be it, and Chrome treats it as secure. A secret key is handed to your browser once and kept in a
  private cookie, so other websites can't read Margin. The page loads nothing from the internet.
- **Touch ID lock**: two minutes after you leave the page (another tab, another app, or sleep), Margin locks and
  asks for Touch ID (or your Mac password). While locked the server sends no data at all. The **Lock** button
  locks at once, and so does hiding or quitting the Margin app, or your Mac locking or sleeping.
  `"lock_minutes"` in `settings.json` changes the time (0 turns it off).
- **Statement passwords** (PAN, PRAN, PPF statement password) are typed into hidden Terminal prompts and kept
  in the Keychain. PDFs are opened by macOS's own PDF reader and never saved unlocked.
- **Prices**: Indian stocks and funds come from whole-market files (NSE/BSE closes, AMFI NAVs), fetched only
  while you hold some, so nothing about your holdings is sent. US prices come from Yahoo Finance, which is sent
  only the tickers you hold.
- **Downloaded statements** (bank, card and broker files you put in the statements folder) are read on your
  Mac. No Plaid, no aggregator, no bank login: Margin never connects to your bank.
  Exchange rates are the ECB's (frankfurter.dev): only currency codes and dates are sent.
- **Claude** (optional) runs as Claude Code on your Mac (`claude -p`) with no tools, no connectors, no hooks
  and no saved session. The text Margin hands it is processed by Anthropic under your Claude plan: payees,
  amounts, dates, categories and message text with long numbers masked. Turn it off with `"ai_sort": false`
  and `"ai_review": false`; the automatic checks still run.
- Logs record counts only. Notifications never show amounts or payees.

## Using it

| Command | What it does |
|---|---|
| The Margin app (Spotlight), its m in the menu bar; in a browser `margin open`, then <http://margin.localhost/> | Home, Spending, Investments, Sort out, Reviews, Sync now; a quick glance from the menu bar |
| `margin sync` | Sync now instead of waiting for midnight (or watch a running sync) |
| `margin doctor` | Check that everything works |
| `margin rule "swiggy" "Food & dining"` | Put matching payees in a category (past and future) |
| `margin trust "mybank.in"` | Count alerts from a sender Margin didn't know was a bank |
| `margin tidy` | Sort out in Terminal instead of the page |
| `margin demo [--india] [--lock] [--fresh] [--app]` | The made-up demo (`--app`: in the Margin app's window) |
| `margin privacy-check` | Look for your personal details in the code (see [Leak guard](#leak-guard)) |
| `margin schedule status` | Is the nightly sync installed? |

- **Sort out** lists payees without a category (pick one and it becomes a rule), senders that aren't a known
  bank, and everything Claude sorted, each with Undo.
- **How counting works**: only alerts from banks and card issuers count (merchant receipts would double-count).
  An alert that arrives by email and by text counts once. **Transfer** (card bills, moves between your own
  accounts) and **Investments** aren't income or spending. Add your name as banks print it to `self_names` so
  transfers to yourself are recognised. **Pay cycle** months run from one salary to the next.
  **Miscellaneous** holds small known spends (under `misc_below`); *Other spending* means not sorted yet.
- **Net worth** = bank balances (from “available balance” in alerts and the balance in downloaded statements) +
  investments + anything in `assets.csv` − card dues (from statement emails, card balance alerts and downloaded
  card statements).

## Investments

| Type | Where it comes from |
|---|---|
| Indian stocks, ETFs and mutual funds | CDSL's monthly Consolidated Account Statement (locked with your PAN), valued at each night's NSE/BSE close or AMFI NAV |
| US stocks, ETFs and funds | A positions export from your broker in the statements folder: Fidelity, Schwab, Vanguard or E*TRADE (CSV or QFX), or Robinhood's account activity report. Shares and cost basis per ticker, valued at each night's close |
| US stocks through INDmoney | A snapshot of your holdings (`us_snapshot`) plus INDmoney order emails after it, valued at each night's close |
| 401(k), IRA and other retirement accounts | The same positions export, when your broker or plan offers one: counted as Retirement, each fund valued at its close (a fund without a ticker keeps the file's value). Otherwise a value in `other_investments` |
| NPS | KFintech CRA's monthly statement (locked with your PRAN) |
| EPF | The EPFO passbook PDF in the statements folder, and EPFO's text after each contribution |
| PPF | SBI's PPF statement (`margin setup-ppf`), plus deposits texted since; or a balance in `settings.json` |
| Fixed deposits | HDFC's deposit advice emails, with interest accrued to today |
| Savings, CDs, retirement accounts without an export, anything else | `other_investments` in `settings.json` (for example a CD, a pension, a 401(k) plan with no download) |

The page shows each holding's value, gain on cost and weight, value over time, your market's benchmark (the
Nifty 50 and S&P 500 with rupees at home, the S&P 500 otherwise), stop-losses (an alert the first time a close
is at or below your level; Margin can't trade and never will), and Claude's weekly and monthly analysis, which
measures and describes but never recommends.

## Other countries and currencies

Set your home currency once in `settings.json`, for example `"currency": "USD"`. Everything is totalled and shown in
it. Money in any other currency is converted at the European Central Bank rate for that day and keeps a note of
the original.

**In the US**, with `"currency": "USD"`, Margin works out of the box:
- **Bank and card alerts by email** from Chase, Bank of America, Wells Fargo, Capital One, Discover, Citi, Amex,
  U.S. Bank, PNC, Ally, Schwab, Fidelity, SoFi, Synchrony, Navy Federal, USAA, Barclays and more are read in
  dollars: purchases, deposits, withdrawals, Zelle, refunds, card payments, available balances, card balances and
  card statements (what's owed and when it's due). Turn on the alerts you want in each bank's app; "every
  transaction" alerts give the most complete picture.
- **Bank texts** from US banks' short codes (Chase's 24273, Wells Fargo's 93557, and any short code whose text
  starts with a US bank's name) are read like emails. Texts from people's phone numbers are never read.
- **Downloaded statements**: drop the CSV or QFX/OFX files your banks and card issuers let you download into
  `~/Library/Application Support/Margin/statements/`. Every payment in them is counted once, even when an alert
  already reported it (same amount and card, within a few days) or two downloads overlap, and their balances
  count in net worth. A restaurant tip added after the alert makes the amounts differ, so check those in Sort out. Name a file after the
  bank and the account's last 4 digits if the bank doesn't (`ally-savings-1234.csv`).
- **Investments**: drop your broker's positions export there too (see [Investments](#investments)). 401(k) and
  IRA accounts are kept apart as Retirement.
- **Shops**: national brands (Walmart, Target, Costco, Whole Foods, Trader Joe's, DoorDash, Lyft, Comcast,
  Verizon, CVS, Chevron and the like) are sorted by default; card payments, transfers between your own accounts
  and Venmo cash-outs count as Transfer.

What you still type or click: `margin trust "<domain or short code>"` (or *My bank* in Sort out) for a credit union
or a bank Margin doesn't know; categories for local shops and for Zelle or Venmo payments to people (each choice
becomes a rule); retirement accounts with no download, CDs and the like in `other_investments`.

**Elsewhere**, the built-in reader knows Indian and US banks' formats; alerts it can't read (other countries'
banks, foreign-currency spends) go to Claude, who reads the amount, currency and payee, so turn on Claude sorting
if your bank is elsewhere. Add your bank's sender with `margin trust`. Retirement accounts and savings go in
`other_investments`. To teach the built-in reader a new bank format, see
[AGENTS.md](AGENTS.md#adding-a-bank-or-a-country).

## Leak guard

Margin is developed next to real financial data, so the repository guards itself. `margin privacy-check` scans
the code for your details: account endings and numbers, names of people you've paid, UPI IDs, salary and balance
amounts, folio numbers, the ISINs you hold, EPF and PPF IDs, your PAN and PRAN, your Mac's name and your home
folder. It builds that list in memory from your own data folder and Keychain each time and never writes it
anywhere. It also checks general patterns (PAN-shaped codes, phone numbers, personal emails, API keys, statements,
databases). It prints where and what kind, never the value. The git hooks in `.githooks/` run it on every
commit, commit message and push (turn them on with `margin privacy-check --install-hooks` after `git init`), and
the push hook also refuses commits whose author email isn't a GitHub noreply address. Words only you know about
(an employer, family names) go in `private-words.txt` in your data folder, one per line.

## Contributing

```bash
python3 -m unittest discover -s tests -t .
./margin-cli privacy-check
```

Tests use made-up data only and run on a temporary data folder, never yours. Read [AGENTS.md](AGENTS.md) first:
it has the rules for made-up test data and the map of the code.

## Licence

MIT: see [LICENSE](LICENSE). Margin is not financial advice and never trades; you use it at your own risk.
