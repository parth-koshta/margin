---
name: margin-setup
description: Set Margin up on this Mac for the person, or finish a setup that stopped part way, following AGENTS.md "Setup" with `margin setup`, and verify every step. Use when they ask to install or set up Margin, connect Gmail, bank texts or statement passwords, or ask what's left to set up.
---

# Set Margin up

Read [AGENTS.md](../../../AGENTS.md) first, the Rules and Setup sections. The short version of the rules:

- **Never look at their money data.** Don't read, list, search or copy their Margin data folder (the data guard
  hook refuses it anyway, but it's an accident guard, not a sandbox), don't open http://margin.localhost/ with
  any browser tool, and don't repeat back names or numbers they tell you. You learn the state from
  `margin doctor --json` and `margin setup --json`: statuses and counts only.
- **Secrets only in hidden prompts in their own Terminal.** Never ask for a PAN, PRAN, password or token in chat.
- **Their steps are theirs:** the Google sign-in, Full Disk Access, phone settings, Touch ID, passwords. Tell
  them exactly what to click; then check.
- **Ask before changing their Mac.** `margin setup` changes nothing without `--yes`, and you pass `--yes` only
  after the person said yes to the list of changes.

Until step 1 is done, `margin` means `./margin-cli` in the repository folder.

## Steps

1. **See where things stand.** Offer the demo first if they haven't seen Margin: `./margin-cli demo` (US,
   dollars) or `./margin-cli demo --india`. Then run `./margin-cli setup --json --no-sync` (without `--yes` it
   changes nothing). Read `would_change`, `waiting_on_you` and `failed`.
   - `mac` waiting: they run `xcode-select --install` and click Install, then you run it again.

2. **Ask before changing anything.** Show the person `would_change` as a short list, in plain words. It's
   typically: a link `~/.local/bin/margin` to this folder's `margin-cli`; a line in `~/.zshrc` that puts
   `~/.local/bin` on the PATH; the private data folder with default settings and two small helper programs; two
   LaunchAgents (the nightly 00:00 sync and the page service); the Margin app in `~/Applications`. Ask whether to
   go ahead and **wait for a clear yes**. If they'd rather do it themselves: they run `margin setup` in their own
   Terminal, which asks them the same.

3. **Run it** (only after the yes): `./margin-cli setup --yes --json --no-sync`.
   - Anything in `failed`: show its `notes`, fix the cause (often a launchd or Swift problem), run it again.
   - Check: `command`, `data`, `helpers`, `schedule` and `app` are `ok` or `done`; `changed_nothing` is false.

4. **Their settings** (AGENTS.md step 2). Ask the questions in that table together, in one message, and set each
   answer with `margin config set <key> <value>` (for example `margin config set currency USD`,
   `margin config set self_names '["KAVYA MENON"]'`, `margin config set read_sms false`). Only the keys they
   need. `margin config get` shows what's set; personal values show as counts, and that's how it should stay.
   `claude_path` is theirs to set by hand if ever needed; `config set` refuses it.
   Check: each `config set` printed the key, with no refusal.

5. **Gmail** (the `gmail` step's `how`). Walk them through the Google Cloud steps, one or two at a time. When
   they've downloaded the JSON, ask for its exact file name (or list `~/Downloads/client_secret_*.json` and ask
   which one), then run `margin setup-gmail "<that exact path>"`: their browser opens; they sign in and allow
   read-only access. Then ask whether to delete that file (its contents are in the Keychain now) and, on a yes,
   delete **only that exact path**: never a wildcard, which could take other apps' client secrets with it.
   Alternatively they run `margin setup` in their own Terminal: it asks for the file, connects, and offers to
   delete it.
   Check: `margin doctor --json` has `gmail` and `gmail_token` ok.

6. **Bank texts** (the `sms` step's `how`; AGENTS.md step 4). Ask which phone they have.
   - iPhone: give them the Text Message Forwarding and Full Disk Access steps, with the Python path from
     `python_for_full_disk_access` in `margin doctor --json`.
   - Android: tell them first that the backup app puts all their texts in their own drive (Google Drive, Dropbox
     or OneDrive) and Margin reads only the bank texts from it. If they want it, walk them through SMS Backup &
     Restore and the drive's Mac app; then either they run `margin setup` in their Terminal and drag the folder
     in, or they tell you the folder and you run `margin config set read_sms false` and
     `margin config set sms_backup_folder "<that folder>"`. Never list, open or search the folder or a backup
     file to find or check it: it holds every text on their phone.
   - Neither: `margin config set read_sms false`.
   This is checked by the first sync, not now.

7. **Statement passwords** (the `statements` step; only what applies; India only). Ask them to open their own
   Terminal window (not this chat, not with `!`) and run `margin setup-pan`, `margin setup-pran` or
   `margin setup-ppf` there. Check: `secrets_saved` in `margin doctor --json`. EPF passbooks and other brokers'
   CSV exports go in the `statements` folder: tell them the path from the step's `how`; don't open it yourself.

8. **The AI** (the `ai` step; optional). It's their choice which AI Margin hands its masked data to, and where
   that goes ([README](../../../README.md#works-with-any-ai)). If the step isn't `ok`, tell them what its notes
   say and ask them to run `margin setup-ai` in their own Terminal: it lists what's installed, asks, types any API
   key hidden, and checks the AI before saving. If they tell you which one and it needs no key, you may run
   `margin setup-ai --provider <name>` (never with `--no-test`). `margin config set` refuses `ai`, `ai.path` and
   addresses. Check: `ai.ready` in `margin doctor --json`.

9. **First sync.** Once Gmail is connected, `margin setup --json` lists "start the first sync" under
   `would_change`; tell them it reads 12 months of mail and can take an hour, and on their yes run
   `margin setup --yes --json`. Watch with `margin doctor --json` (`sync.progress`), not by reading logs.

10. **Verify** (AGENTS.md "Verify"):
   - `margin setup --json`: `would_change`, `waiting_on_you` and `failed` empty.
   - `margin doctor --json` after the first sync: every check ok (the `sms` check passes once a sync has read
     texts; on Android, `texts_backup` names how many backups and the newest one's date); `counts.transactions`
     above 0.
   - Tell them to open **Margin** from Spotlight once in each browser they use and bookmark
     http://margin.localhost/ (Touch ID asks). You don't open it.

11. **Sort out** (AGENTS.md step 7) is theirs, on the page. You can explain how it works; the choices are theirs.

Finish with a short summary: what's set up, what's waiting on them, and `margin doctor` for next time.
