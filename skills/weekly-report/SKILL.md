---
name: weekly-report
description: Weekly report from the Atlas ledger. Runs the numbers, then writes three lessons, three concrete changes and 1-3 post ideas. Proposes only; the operator decides and writes any post.
argument-hint: "[YYYY-Www]"
---

You are the Chief. Produce the weekly report for the week in `$ARGUMENTS` (the current ISO week if empty).
This is a playbook (area atlas): no `: team-type` declaration is needed.

1. Run the numbers (read-only on the ledger):
   `py -3.12 <repo>\tools\weekly_report.py --week <YYYY-Www>` (omit `--week` for the current week).
   It prints the paths of `~/.atlas/reports/<week>.md` and `<week>.json`.
2. Read the markdown. Every number you quote comes from it; do not estimate or recompute from memory.
   If a figure looks wrong (for example a week with zero tasks), say so instead of explaining it away.
3. Fill the three placeholders in the SAME `.md` file (they are marked `<!-- FILL: ... -->`), editing only
   those lines. Never touch the block between `<!-- numbers:start -->` and `<!-- numbers:end -->`; re-running
   the script refreshes that block and keeps your text.
   - **Lessons: three observations.** Each one a plain sentence plus the number that shows it. Prefer the
     waste signals (Chief doing helper-sized work, undeclared tasks, reruns, cache reuse), first-pass READY,
     what the gates caught, and budget refusals (enforced vs would).
   - **Three changes.** Concrete and small: a prompt edit, a skill edit, or a routing change (which type or
     model takes which work). Each names the file or setting to change and the number it should move.
     These are proposals: do not apply any of them.
   - **Post ideas (1 to 3).** LinkedIn angles with the numbers behind them. Numbers only: never client
     names, company names, prices charged, or anything from a client's work. The operator writes the post.
4. Show the operator the finished report (the `.md` content), lead with the single most important finding, and ask
   which of the three changes the operator wants. Wait for the operator's answer.

Rules: it proposes, the operator decides. Never publish, post, send or schedule anything. Never write to the ledger.
No em-dashes in the post ideas.
