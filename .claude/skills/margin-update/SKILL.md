---
name: margin-update
description: Update the person's Margin to the newest release with `margin update` (shows what's in it, then with their yes fast-forwards to exactly that, tests on a temporary folder, rebuilds, restarts) and verify it still works, without touching their data. Use when they ask to update or upgrade Margin, get the latest version, see "Update" on Margin's page, or after they hear about a fix.
---

# Update Margin

Read [AGENTS.md](../../../AGENTS.md) first (Rules). `margin update` never reads or changes their data
(`money.db`, `settings.json`, `rules.json`, statements); neither do you. Learn the state from
`margin doctor --json` only.

## Steps

1. **What's new?** `margin update --check`. It fetches and lists the changes in the newest release (its version,
   how many changes, and each one's subject) and changes nothing. If it says up to date, say so and stop. If it says the branch doesn't follow
   GitHub, or this isn't a git checkout, explain and stop: they got Margin another way, and updating means
   cloning it again (README) and running `margin setup` there.

2. **Ask.** Show the person the list in plain words (you can summarise the subjects) and ask whether to install
   them. **Wait for a clear yes.** No new code runs before they've seen the list and agreed.

3. **Before.** `margin doctor --json`: note which checks were ok, so you can compare afterwards.

4. **Update** to exactly what they saw: `margin update --to <version>`, with the version `--check` printed
   (such as `v0.2.0`). Anything released after the check isn't included. What it does, in order:
   - refuses while a sync runs (run it again when `margin doctor --json` shows `sync.running` false);
   - fast-forwards to that commit. It never merges. If it refuses, their copy has local commits:
     `git -C <repo> status` and `git log @{u}..` show what (code only, safe to look at). Ask them what to do;
     don't reset or stash their work without a yes;
   - on the new code, runs the tests on a temporary data folder; if they fail it goes back to the previous
     version by itself and says so. Report the failing test names;
   - rebuilds the Swift helpers and the app, reloads the nightly sync and restarts the page service.

5. **Verify.**
   - `margin doctor --json`: the same checks are ok as before.
   - `margin setup --json`: a new version can bring a new step; `would_change`, `waiting_on_you` and `failed`
     should be empty (or the same as before). Anything new in `would_change` needs the person's yes first
     (margin-setup skill).
   - If update said "The Python changed", Full Disk Access has to be given again to the new path
     (`python_for_full_disk_access`): walk them through it (AGENTS.md step 4).

6. **Tell them** in two or three lines: from which version to which (`margin update` prints both; `margin
   --version` shows it), that the tests
   passed, and anything they need to do. Don't open the page yourself; they can.

In their own Terminal they can simply run `margin update`: it shows the list and asks them.
