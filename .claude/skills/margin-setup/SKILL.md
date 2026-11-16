---
name: margin-setup
description: Set Margin up on this Mac for the person, or finish a setup that stopped part way, following AGENTS.md "Setup" with `margin setup`, and verify every step. Use when they ask to install or set up Margin, connect Gmail, bank texts or statement passwords, or ask what's left to set up.
---

# Set Margin up

Read [AGENTS.md](../../../AGENTS.md) first, the Rules and Setup sections. The short version of the rules:

- **Never look at their money data.** Don't read, list, search or copy `~/Library/Application Support/Margin/`
  (the data guard hook refuses it anyway), don't open http://margin.localhost/ with any browser tool, and don't
  repeat back names or numbers they tell you. You learn the state from `margin doctor --json` and
  `margin setup --json`: statuses and counts only.
- **Secrets only in hidden prompts in their own Terminal.** Never ask for a PAN, PRAN, password or token in chat.
- **Their steps are theirs:** the Google sign-in, Full Disk Access, iPhone settings, Touch ID, passwords. Tell
  them exactly what to click; then check.

Until step 1 is done, `margin` means `./margin-cli` in the repository folder.

## Steps

1. **See where things stand.** Run `./margin-cli setup --dry-run`. Tell the person in a few lines what it will
   do (link the `margin` command, create the private data folder with default settings, build two small helper
   programs, schedule the nightly sync, install the Margin app) and what will be left for them. Offer the demo
   first if they haven't seen Margin: `./margin-cli demo` (US, dollars) or `./margin-cli demo --india`.

2. **Run it.** `./margin-cli setup --json --no-sync`. Read `steps`, `waiting_on_you` and `failed`.
   - `mac` waiting: they run `xcode-select --install` and click Install, then you run setup again.
   - Anything in `failed`: show its `notes`, fix the cause (often a launchd or Swift problem), run setup again.
   - Check: `command`, `data`, `helpers`, `schedule` and `app` are `ok` or `done`.

3. **Their settings** (AGENTS.md step 2). Ask the questions in that table together, in one message, and set each
   answer with `margin config set <key> <value>` (for example `margin config set currency USD`,
   `margin config set self_names '["KAVYA MENON"]'`, `margin config set read_sms false`). Only the keys they
   need. `margin config get` shows what's set; personal values show as counts, and that's how it should stay.
   Check: each `config set` printed the key, with no "refused" message.

4. **Gmail** (the `gmail` step's `how`). Walk them through the Google Cloud steps, one or two at a time. When
   they have the downloaded JSON, run `margin setup-gmail ~/Downloads/client_secret_*.json` (their browser
   opens; they sign in and allow read-only access), then `rm ~/Downloads/client_secret_*.json`.
   Alternatively they run `margin setup` in their own Terminal: it waits for the file and does the same.
   Check: `margin doctor --json` has `gmail` and `gmail_token` ok.

5. **Bank texts** (the `sms` step's `how`; skip if they set `read_sms` to false). Give them the iPhone and Full
   Disk Access steps with the Python path from `python_for_full_disk_access` in `margin doctor --json`. This
   is checked by the first sync, not now.

6. **Statement passwords** (the `statements` step; only what applies; India only). Ask them to open their own
   Terminal window (not this chat, not with `!`) and run `margin setup-pan`, `margin setup-pran` or
   `margin setup-ppf` there. Check: `secrets_saved` in `margin doctor --json`. EPF passbooks and other brokers'
   CSV exports go in the `statements` folder: tell them the path from the step's `how`; don't open it yourself.

7. **First sync.** `margin setup --json` starts it in the background once Gmail is connected (it reads 12 months
   of mail; up to an hour). Watch with `margin doctor --json` (`sync.progress`), not by reading logs.

8. **Verify** (AGENTS.md "Verify"):
   - `margin setup --json`: `waiting_on_you` empty, `failed` empty.
   - `margin doctor --json` after the first sync: every check ok (the `sms` check passes once a sync has read
     texts); `counts.transactions` above 0.
   - Tell them to open **Margin** from Spotlight once in each browser they use and bookmark
     http://margin.localhost/ (Touch ID asks). You don't open it.

9. **Sort out** (AGENTS.md step 7) is theirs, on the page. You can explain how it works; the choices are theirs.

Finish with a short summary: what's set up, what's waiting on them, and `margin doctor` for next time.
