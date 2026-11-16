---
name: margin-add-bank
description: Make Margin count a bank, card or country it misses, following AGENTS.md "Adding a bank or a country": trust a sender, check Claude sorting, teach the built-in reader a new alert format with a made-up test, add a currency, or add category rules. Use when the person says a bank, card, account or currency isn't showing up or isn't read right.
---

# Add a bank or a country

Read [AGENTS.md](../../../AGENTS.md) first: Rules, "Adding a bank or a country", "Working on the code" and
"Made-up data". The person's messages and database are never your source: you don't search their mail, texts or
data folder for examples (the data guard refuses it). You work from what they choose to tell you, rewritten.

## 1. Find out which case it is

Ask the person (no figures needed): which bank or card, email or text, and what they see (not counted at all,
or counted with the wrong amount or payee). Then `margin doctor --json`: `counts.unknown_senders` above 0 means
Sort out has senders waiting for them to mark.

## 2. Fix it, simplest first

- **A sender Margin doesn't count.** Ask for the sender's email domain or SMS ID (like `mybank.example` or
  `VM-MYBANK`; that's not money data) and run `margin trust "<sender>"`. Or they mark it *My bank* in Sort out.
  Check: the command says how many of its messages get read at the next sync.
- **A format the reader can't parse.** If `margin config get ai_sort` is true, Claude reads it at the next sync
  (amount, currency, payee). Often that's enough: suggest `margin sync`, then check
  `margin doctor --json` (`counts.transactions` went up).
- **Teach the built-in reader** (`margin/parse.py`), when they want it read without Claude:
  1. Ask them for the *shape* of one alert with every value replaced: amount, account ending, payee, reference,
     date. If they paste a real one, don't repeat it back; rewrite every value before it goes into a file.
     Use only the made-up values from AGENTS.md "Made-up data" (endings 0000, 1111, 1234, ...; names like
     Kavya Menon, ACME ...).
  2. Add the sender to the bank list at the top of `margin/parse.py`, then a pattern for the new wording.
  3. Add a test in `tests/test_parse.py` with the made-up message.
  4. `/usr/bin/python3 -m unittest discover -s tests -t .` and `./margin-cli privacy-check` must both pass.
  5. Commit only if they ask; never with `--no-verify`. If they'd like it in Margin for everyone, they open the
     pull request themselves after looking it over.
- **Their own categories for local shops**: `margin rule "<keyword>" "<category>"`, which goes in their own
  `rules.json`. Don't add local merchants to `margin/categorize.py` (national brands only).
- **Another currency**: if `margin config set currency <CODE>` says Margin doesn't know it, add it to
  `CURRENCIES` in `margin/money.py` (symbol, decimals, locale), with a test, then set it.

## 3. Verify

- Code changes: tests and leak guard clean.
- After the next sync (`margin sync`, or tonight's): `margin doctor --json`. `last_sync` ok,
  `counts.transactions` up, `counts.unknown_senders` down. Ask them to look at the page to confirm the bank
  shows up; you don't open it.
