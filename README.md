# Margin

**Your money, on your Mac.** Margin shows where your money comes from and where it goes, what your investments are
doing, and your net worth. It reads your bank and card alerts (Gmail and the text messages your iPhone forwards to
your Mac), your investment statements, and anything you add by hand, then syncs every night at 00:00 by itself.
Everything is stored and shown on your Mac only, at <http://margin.localhost/>, behind Touch ID.

- **Spending**: money in and out by month or pay cycle, by category and payee, with rules you set once.
- **Investments**: stocks, funds, retirement accounts, deposits and more, valued daily, with stop-loss alerts
  (alerts only: Margin never trades).
- **Reviews**: daily, weekly and monthly summaries, plus checks for double charges, unusual amounts and new cards.
- **Sorted by Claude** (optional): Claude Code on your Mac sorts what fixed rules can't, and every change can be
  undone.
- **Any home currency**: amounts in other currencies are converted at the day's rate and keep their original
  (“£8.45 → $10.73”). Each currency is written its own way: ₹1,23,456 · $123,456 · ¥12,346.

Built for **macOS**. It reads **Indian banks'** alerts out of the box (HDFC, ICICI, SBI, Axis, Kotak and more),
plus CDSL, NPS, EPFO, PPF and fixed-deposit statements. With Claude sorting turned on, alerts in other formats
and other currencies are read too; see [Other countries](#other-countries-and-currencies).

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
touches real data or the internet.
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
- **The page** at <http://margin.localhost/> is served by a tiny server on your Mac only (127.0.0.1 and ::1, not
  reachable from other machines). `*.localhost` always means your own Mac, so nothing on a shared Wi-Fi can
  pretend to be it, and Chrome treats it as secure. A secret key is handed to your browser once and kept in a
  private cookie, so other websites can't read Margin. The page loads nothing from the internet.
- **Touch ID lock**: two minutes after you leave the page (another tab, another app, or sleep), Margin locks and
  asks for Touch ID (or your Mac password). While locked the server sends no data at all. The **Lock** button
  locks at once; `"lock_minutes"` in `settings.json` changes the time (0 turns it off).
- **Statement passwords** (PAN, PRAN, PPF statement password) are typed into hidden Terminal prompts and kept
  in the Keychain. PDFs are opened by macOS's own PDF reader and never saved unlocked.
- **Prices**: Indian stocks and funds come from whole-market files (NSE/BSE closes, AMFI NAVs), so nothing
  about your holdings is sent. US prices come from Yahoo Finance, which is sent only the tickers you hold.
  Exchange rates are the ECB's (frankfurter.dev): only currency codes and dates are sent.
- **Claude** (optional) runs as Claude Code on your Mac (`claude -p`) with no tools, no connectors, no hooks
  and no saved session. The text Margin hands it is processed by Anthropic under your Claude plan: payees,
  amounts, dates, categories and message text with long numbers masked. Turn it off with `"ai_sort": false`
  and `"ai_review": false`; the automatic checks still run.
- Logs record counts only. Notifications never show amounts or payees.

## Using it

| Command | What it does |
|---|---|
| <http://margin.localhost/>, the Margin app, `margin open` | Home, Spending, Investments, Sort out, Reviews, Sync now |
| `margin sync` | Sync now instead of waiting for midnight (or watch a running sync) |
| `margin doctor` | Check that everything works |
| `margin rule "swiggy" "Food & dining"` | Put matching payees in a category (past and future) |
| `margin trust "mybank.in"` | Count alerts from a sender Margin didn't know was a bank |
| `margin tidy` | Sort out in Terminal instead of the page |
| `margin demo [--india] [--lock] [--fresh]` | The made-up demo |
| `margin privacy-check` | Look for your personal details in the code (see [Leak guard](#leak-guard)) |
| `margin schedule status` | Is the nightly sync installed? |

- **Sort out** lists payees without a category (pick one and it becomes a rule), senders that aren't a known
  bank, and everything Claude sorted, each with Undo.
- **How counting works**: only alerts from banks and card issuers count (merchant receipts would double-count).
  An alert that arrives by email and by text counts once. **Transfer** (card bills, moves between your own
  accounts) and **Investments** aren't income or spending. Add your name as banks print it to `self_names` so
  transfers to yourself are recognised. **Pay cycle** months run from one salary to the next.
  **Miscellaneous** holds small known spends (under `misc_below`); *Other spending* means not sorted yet.
- **Net worth** = bank balances (from “available balance” in alerts) + investments + anything in `assets.csv`
  − card dues (from statement emails).

## Investments

| Type | Where it comes from |
|---|---|
| Indian stocks, ETFs and mutual funds | CDSL's monthly Consolidated Account Statement (locked with your PAN), valued at each night's NSE/BSE close or AMFI NAV |
| US stocks | A snapshot of your holdings (`us_snapshot`) plus INDmoney order emails after it, valued at each night's close |
| NPS | KFintech CRA's monthly statement (locked with your PRAN) |
| EPF | The EPFO passbook PDF in the statements folder, and EPFO's text after each contribution |
| PPF | SBI's PPF statement (`margin setup-ppf`), plus deposits texted since; or a balance in `settings.json` |
| Fixed deposits | HDFC's deposit advice emails, with interest accrued to today |
| Retirement accounts, savings, CDs, anything else | `other_investments` in `settings.json` (for example a 401(k), an IRA, a savings account) |

The page shows each holding's value, gain on cost and weight, value over time, your market's benchmark (the
Nifty 50 and S&P 500 with rupees at home, the S&P 500 otherwise), stop-losses (an alert the first time a close
is at or below your level; Margin can't trade and never will), and Claude's weekly and monthly analysis, which
measures and describes but never recommends.

## Other countries and currencies

Set your home currency once in `settings.json`, for example `"currency": "USD"`. Everything is totalled and shown in
it. Money in any other currency is converted at the European Central Bank rate for that day and keeps a note of
the original. The built-in alert reader knows Indian banks' formats; alerts it can't read (other countries'
banks, foreign-currency spends) go to Claude, who reads the amount, currency and payee, so turn on Claude sorting
if your bank isn't Indian. Add your bank's sender with `margin trust`. Retirement accounts and savings go in
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
