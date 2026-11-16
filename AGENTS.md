# AGENTS.md: setting up and working on Margin

You are a coding agent helping one person run Margin, a private money tracker that lives on their Mac. Read this
whole file before doing anything. It covers the rules, every setup step in order (with the check after each one),
how to adapt Margin to another bank or country, and how to work on the code without leaking anyone's data.

## Rules (always)

1. **Their money data never leaves their Mac through you.** It lives in `~/Library/Application Support/Margin/`
   (`MARGIN_DATA` overrides it). Don't print, copy, summarise or paste its contents (`money.db`, `settings.json`,
   `rules.json`, `statements/`) into chat, files in the repo, issues, commits or anything online. When you need to
   check something, query counts or yes/no answers, not values. If you must look at one record to debug, say so
   first and keep it to that record.
2. **Secrets go into hidden prompts, never chat.** Statement passwords, PAN and PRAN are typed by the person into
   `margin setup-pan`, `margin setup-pran` or `margin setup-ppf` in their own Terminal (input is hidden and goes
   to the Keychain). Never ask them to paste one to you, and never put one on a command line.
3. **Some steps are theirs.** You can't and mustn't do these for them: signing in to Google and approving access,
   granting Full Disk Access in System Settings, turning on Text Message Forwarding on their iPhone, approving
   Touch ID, typing passwords. Tell them exactly what to click, then check the result with `margin doctor`.
4. **Margin never trades.** Don't add anything that places, changes or cancels orders, or moves money. Stop-losses
   are alerts only. The investment analysis describes and measures; it never recommends.
5. **Made-up data only** in tests, examples, docs, comments, the demo and screenshots (see
   [Made-up data](#made-up-data)). Never copy a real message, statement or amount into the repo.
6. **The leak guard is not optional.** Run `./margin-cli privacy-check` before every commit; never use
   `git commit --no-verify` or `git push --no-verify`. If it reports something, replace it with made-up data.
7. **Show Margin in public only as the demo** (`margin demo`): screenshots, screen recordings, bug reports.

## Setup

Run these in order. `margin` below means `~/projects/margin/margin-cli` (or wherever the repo is) until step 1.

### 0. Check the Mac

```bash
sw_vers -productVersion                 # macOS
xcode-select -p                         # command line tools; if missing, the person runs: xcode-select --install
/usr/bin/python3 --version              # 3.9 or newer (comes with the command line tools)
/usr/bin/swiftc --version               # for the PDF reader and the Touch ID prompt
command -v claude                       # optional: Claude Code, for sorting and reviews
```

Try the demo first so they see what they're setting up: `./margin-cli demo` (US person, dollars) or
`./margin-cli demo --india` (Indian person, rupees). Stop it with Ctrl-C.

### 1. The `margin` command

```bash
echo 'alias margin="$HOME/projects/margin/margin-cli"' >> ~/.zshrc     # adjust the path to the repo
```

They open a new Terminal window. Check: `margin doctor` runs (it will show what's missing).

### 2. Their settings

Ask, then write `~/Library/Application Support/Margin/settings.json` (create the folder with `margin doctor` or
`margin sync` first; keep the file 0600). Only the keys they need; everything else has a default.

| Key | Ask them | Example |
|---|---|---|
| `currency` | Home currency (ISO code). Everything is totalled in it. `"USD"` turns on the US wording in the Gmail search and reads banks in both countries (Citi, Amex) in dollars | `"INR"` (default), `"USD"` |
| `self_names` | Their name as banks print it, for transfers to themselves | `["KAVYA MENON", "MENON KAVYA"]` |
| `read_sms` | Do their bank texts reach this Mac (iPhone + Text Message Forwarding)? | `true` / `false` |
| `misc_below` | Small unsorted spends below this go to Miscellaneous. 100 suits rupees; use ~3 for dollars/euros | `100` |
| `about_me` | Facts that help Claude sort | `["Money from friends is usually a split bill"]` |
| `ai_sort`, `ai_review` | Use Claude Code for sorting and reviews? | `true` |
| `extra_categories` | Their own categories | `{"out": ["Pets"], "in": ["Freelance"]}` |
| `project_categories` | Big one-off projects, shown apart on Spent | `["Home renovation"]` |
| `other_investments` | Savings, CDs, retirement accounts with no positions download, anything without a statement Margin reads | `[{"name": "401(k)", "kind": "retirement", "value": 52000, "cost": 40000}]` (`kind`: `retirement`, `cash` or `other`; optional `currency`, `as_of`) |
| `us_snapshot` | US shares held (INDmoney users): shares and dollars invested per ticker, on a date | `{"as_of": "2026-01-02", "holdings": {"VOO": {"shares": 3, "cost_usd": 1500}}}` |
| `ppf` | PPF balance if they have no SBI statement | `[{"name": "PPF", "balance": 100000, "as_of": "2026-03-31"}]` |
| `lock_minutes` | Minutes away before Touch ID is needed (0 = off) | `2` |
| `trusted_senders` | Extra bank senders (email domains, SMS IDs or US short codes), e.g. a credit union | `["mybank.in", "VM-MYBANK", "democu.org", "55512"]` |

Others (rarely needed): `backfill_days` (365), `ai_model` ("sonnet"), `claude_path`, `fx_rates` (fallback rates,
home currency per unit), `old_hostnames`, `indmoney` (`true` to read INDmoney order emails when the home currency
isn't INR; with rupees at home they're always read).

**Living in the US**: `"currency": "USD"`, `"misc_below": 3`, their name as US banks print it in `self_names`.
Then ask them to turn on alerts in each bank's and card's app: email (and text, if they forward texts) for every
transaction, deposits, the daily available balance, and the statement. That's what Margin reads each night.

### 3. Gmail, read-only (they do it; about 10 minutes)

Walk them through it; they must be signed in to Google Cloud with **their personal Gmail**.

1. <https://console.cloud.google.com/projectcreate>: create a project named `margin` and select it.
2. <https://console.cloud.google.com/apis/library/gmail.googleapis.com>: **Enable**.
3. <https://console.cloud.google.com/auth/overview> → **Get started**: app name `margin`, support email their
   Gmail, **Audience: External**, contact email their Gmail, agree → **Create**.
4. **Branding** → App domain: any page they own (their GitHub profile is fine) as home page and privacy policy
   link; add its domain under Authorised domains → **Save**. (Needed before Publish; nobody else sees it.)
5. **Audience** → **Publish app** → Confirm. (Left in "Testing", Google expires access every 7 days.)
6. **Clients** → **Create client** → **Desktop app**, name `margin-mac` → **Download JSON**.
7. You run: `margin setup-gmail ~/Downloads/client_secret_*.json`. Their browser opens; they pick their Gmail,
   click **Advanced → Go to margin (unsafe)** (it's their own app), allow read-only access.
   Check: Terminal says `Connected read-only to …`.
8. You run: `rm ~/Downloads/client_secret_*.json` (its contents are now in the Keychain).

Problems: Publish greyed out → step 4 incomplete. "Access blocked" → step 5 skipped. "No long-lived token" → they
remove margin at <https://myaccount.google.com/permissions>, then redo step 7.

### 4. Bank texts (iPhone users; they do it; 2 minutes)

1. iPhone: **Settings → Apps → Messages → Text Message Forwarding** → turn on this Mac.
2. Mac: **System Settings → Privacy & Security → Full Disk Access** → `+` → ⌘⇧G → paste the Python path that
   `margin doctor` prints → Open → switch on. (Full Disk Access is for that Python only, so it can read
   `~/Library/Messages/chat.db`.)
3. Optional, for older texts: **Messages in iCloud** on the iPhone and the Mac.

No iPhone or texts don't reach the Mac: `"read_sms": false`.

Margin reads texts from service senders only: Indian bank IDs (`JD-HDFCBK-S`) and US short codes (5 or 6 digits).
Texts from phone numbers (people) are never read. Chase (24273), Bank of America (73981) and Wells Fargo (93557)
are known; any other short code counts when its text starts with a US bank's name ("Capital One: ..."), or once
they trust it (`margin trust 55512`). With rupees at home, short codes are read only once trusted, and US brokers
(Schwab, Fidelity) aren't treated as banks, so RSU or ESPP mail never counts as income.

### 5. Statements (they type the passwords)

Only what applies to them. Files go in `~/Library/Application Support/Margin/statements/` (the "statements
folder"); every file there is read each night on the Mac, and a changed file is read again.

In India:
- Indian stocks and funds: `margin setup-pan` (CDSL statements are locked with the PAN).
- NPS: `margin setup-pran`.
- SBI PPF statement: `margin setup-ppf` (the statement's password).
- EPF: they download the year's passbook PDF from the EPFO member portal into the statements folder.
- Other brokers: a holdings CSV export (a name and a current-value column) in the statements folder.

Each `setup-*` asks in a hidden prompt, saves to the Keychain and checks it opens their latest statement.

In the US (no passwords; they download, you check):
- **Bank and card activity**: on each bank's or card's website, *Download account activity* (or *Export*), as CSV
  or QFX/Quicken. A year back is a good first download, then a fresh one whenever they like (overlaps count once).
  If the file name doesn't say the bank and the account's last 4 digits, rename it, e.g. `chase-checking-1234.csv`
  or `amex-card-1001.csv`. Payments are counted once with the alerts (same amount and account within a few days);
  balances and card balances count in net worth.
- **Brokerage and retirement positions**: Fidelity (Positions → Download), Schwab (Positions → Export), Vanguard
  (Download center, CSV), E*TRADE (Portfolios → Download) or any broker's QFX. Robinhood has no positions export:
  its account activity report (Reports and statements → Reports, from the account's start) is rebuilt into
  holdings. Accounts named IRA, Roth, 401(k), 403(b), 457, SEP, SIMPLE or TSP count as Retirement; if the file
  doesn't say (Vanguard), put it in the file name (`vanguard-roth-ira.csv`). The newest file per account wins.
- A 401(k) plan with no download: its value in `other_investments` (kind `retirement`), updated now and then.
- With rupees at home (someone in India with a US account), a US file is read only when its file name says the
  bank or broker (`chase-checking-1234.csv`, `fidelity-positions.csv`); every other CSV is read as before.

Rows from bank files are stored without the fields that can carry the account holder's name or ids (`INDN:`,
`ID:`, `CO ID:`, `IND NAME:`, `TRACE#:`...), so those never reach the page's text or Claude.

Check after the next `margin sync`: `margin doctor`, then the page's Net worth and Investments. Notes about files
that couldn't be read show in the sync's problems ("Statements: ...").

### 6. Schedule, app, first sync

```bash
margin schedule install     # the nightly 00:00 sync (launchd)
margin app install          # the Margin app (Spotlight) and the always-on page at http://margin.localhost/
margin sync                 # first run reads 12 months of mail; can take an hour; safe to close Terminal
margin doctor               # every line should be ✓
```

They open **Margin** from Spotlight once in each browser they use (that hands the browser its key), then bookmark
<http://margin.localhost/>. Touch ID asks on first open.

### 7. Sort out (they decide; 15 minutes)

In the page's **Sort out**: pick categories for the biggest payees (each becomes a rule), mark unknown senders as
*My bank* or *Not a bank*, and check what Claude sorted. You can help by explaining, but the choices are theirs.

### Verify

`margin doctor` all ✓; the page opens and locks after two minutes away; `margin schedule status` installed; the
next morning, "Updated … 12:0x am" at the top of the page.

## Adding a bank or a country

- **A sender Margin doesn't count**: `margin trust "<domain, SMS ID or short code>"`, or *My bank* in Sort out.
  A credit union or small US bank is usually just this: once trusted, its alerts are read like any US bank's.
- **A format the reader can't parse**: with `ai_sort` on, Claude reads it at the next sync (amount, currency,
  payee) and converts other currencies to the home currency. That's often enough.
- **Teach the built-in reader** (`margin/parse.py`): add the sender to the bank list at the top (Indian banks:
  `BANK_TOKENS`; US banks: `US_DOMAINS` by email domain, `US_SHORT_CODES` and `_US_NAMES` for texts), then a
  pattern for the new wording, and a test with a **made-up** message in the same shape (fake ending, fake amounts,
  fake names): `tests/test_parse.py` for Indian alerts, `tests/test_us.py` for US ones. The person can show you
  the shape of one of their messages; rewrite every value before it goes anywhere. Run the tests and the leak
  guard.
- **Two readers, one per currency**: each bank's alerts are read in its currency (`parse.currency_for`): Indian
  banks with the rupee patterns (`AMOUNT_RE`, `BAL_RE`, `SKIP_RE`...), US banks with the dollar ones (`US_*`,
  `_parse_us`). Banks in both countries (Citi, Amex, HSBC) and trusted senders follow the home currency. Change
  one reader and the other can't move: `tests/test_parse.py` must pass unchanged. After a change that should
  re-read stored alerts, bump `PARSER_VERSION` (the next sync re-reads payees and endings and retries US
  messages still on the review list); check first on the person's data that rupee results don't change,
  comparing counts and totals only.
- **A bank or broker download Margin can't read** (`margin/imports.py`): the column names it knows are listed near
  the top (`DATE_COLS`, `AMOUNT_COLS`, `SYMBOL_COLS`...); add the bank's, with a made-up file in
  `tests/test_files.py`. Ask the person for the header line only, never the rows.
- **Categories for local merchants** belong in the person's own `rules.json` (via Sort out or `margin rule`), not
  in `margin/categorize.py`, which holds only well-known national brands.
- **Currencies**: `margin/money.py` lists symbols, decimals and locales; add one there if it's missing.
- **Another country**: set its currency, trust its banks; Claude reads their alerts in any currency. A built-in
  reader for that country's wording follows the US one: its own patterns and `_parse_<country>`, picked by
  `currency_for`, so the Indian and US readers stay exactly as they are.

## Working on the code

```bash
python3 -m unittest discover -s tests -t .     # all tests; they use a temporary data folder, never the real one
./margin-cli demo --fresh                      # see changes on made-up data
./margin-cli privacy-check                     # before every commit
```

The page is `margin/dashboard.py` (one HTML template, built on request); after changing it, restart the page
service (`launchctl kickstart -k gui/$(id -u)/com.$(whoami).margin.web`) or the demo.

| File | What it does |
|---|---|
| `sync.py` | The nightly pipeline: Gmail + texts → payments, balances, reviews, investments |
| `parse.py`, `categorize.py` | Reading bank alerts; built-in category rules (the person's own are in `rules.json`) |
| `gmail.py`, `sms.py` | Read-only Gmail; the Messages database |
| `money.py`, `fx.py` | Home currency, formatting and detection; exchange rates (ECB) |
| `ai.py`, `review.py`, `invreview.py` | Claude sorting; spending reviews and checks; investment analysis |
| `invest.py`, `cas.py`, `nps.py`, `usstocks.py`, `epf.py`, `fd.py`, `ppf.py`, `prices.py` | Investments |
| `imports.py` | Bank, card and broker files downloaded into the statements folder (CSV, QFX/OFX) |
| `networth.py`, `tidy.py` | Net worth; Sort out logic |
| `server.py`, `dashboard.py`, `icon.py`, `lock.py`, `touchid.swift` | The local page, its server and the Touch ID lock |
| `pdf.py`, `pdftext.swift`, `keychain.py` | Locked PDFs via PDFKit; Keychain secrets |
| `demo.py` | The made-up demo people and the stand-ins for Claude, sync and Touch ID |
| `privacy.py`, `.githooks/` | The leak guard |

### Made-up data

Everything in tests, docs, comments and the demo is invented:
- Account and card endings only from 0000, 1111, 1234, 2222, 4321, 5555, 9876; full numbers obviously fake
  (`50100000009876`); reference numbers like `600000000000`.
- People: Kavya Menon, Arjun Mehta, Vikram Bose, Jordan Lee, Maria Garcia, Alex Rivera, Some One, Demo Name.
  Companies: ACME …, DEMO …. Phone numbers: 9999999999.
- IDs: EPF `ZZDEM…1234`, folios `1234567890/0`, ISINs `INE000D01011`-style.
- Amounts and dates: invented, round-ish, never someone's real figures.

### Publishing (only when the person asks)

1. `git init`, then `./margin-cli privacy-check --install-hooks`.
2. Set a GitHub noreply author email for this repo:
   `git config user.email "<id>+<username>@users.noreply.github.com"` (the push hook refuses others, because
   commit emails are public).
3. `./margin-cli privacy-check` must say clean; then commit.
4. Create the GitHub repo **private** first, push, let the person look it over, and only they make it public.
   Suggest they turn on GitHub secret scanning and push protection in the repo settings.
