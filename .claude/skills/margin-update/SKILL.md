---
name: margin-update
description: Update the person's Margin to the latest version with `margin update` (fast-forward git pull, tests on a temporary folder, rebuild, restart) and verify it still works, without touching their data. Use when they ask to update or upgrade Margin, get the latest version, or after they hear about a fix.
---

# Update Margin

Read [AGENTS.md](../../../AGENTS.md) first (Rules). `margin update` never reads or changes their data
(`money.db`, `settings.json`, `rules.json`, statements); neither do you. Learn the state from
`margin doctor --json` only.

## Steps

1. **Is there anything new?** `margin update --check`. If it says up to date, say so and stop.
   If it says the branch doesn't follow GitHub, or this isn't a git checkout, explain and stop: they got Margin
   another way, and updating means cloning it again (README) and running `margin setup` there.

2. **Before.** `margin doctor --json`: note which checks were ok, so you can compare afterwards.

3. **Update.** `margin update`. What it does, in order:
   - waits if a sync is running (run it again when `margin doctor --json` shows `sync.running` false);
   - `git pull --ff-only`. It never merges. If it refuses, their copy has local commits or edits:
     `git -C <repo> status` and `git log @{u}..` show what (code only, safe to look at). Ask them what to do;
     don't reset or stash their work without a yes;
   - runs the tests on a temporary data folder; if they fail it goes back to the previous version by itself and
     says so. Report the failing test names, nothing else to fix on their Mac;
   - rebuilds the Swift helpers and the app, reloads the nightly sync and restarts the page service.

4. **Verify.**
   - `margin doctor --json`: the same checks are ok as before.
   - `margin setup --json`: a new version can bring a new step; `waiting_on_you` and `failed` should be empty
     (or the same as before).
   - If update said "The Python changed", Full Disk Access has to be given again to the new path
     (`python_for_full_disk_access`): walk them through it (AGENTS.md step 4).

5. **Tell them** in two or three lines: from which version to which (`margin update` prints both), that the tests
   passed, and anything they need to do. Don't open the page yourself; they can.
