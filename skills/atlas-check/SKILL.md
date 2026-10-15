---
name: atlas-check
description: End-of-day Atlas check, typed by the operator only. Shows the day's tasks (active time, cost, result) and Atlas health. Facts only, no suggestions.
disable-model-invocation: true
argument-hint: "[YYYY-MM-DD]"
---

You are the orchestrator. Show the end-of-day check for the day in `$ARGUMENTS` (today if empty).
This is a playbook (area atlas): no `: team-type` declaration is needed.

1. Run `py -3.12 <repo>\tools\daily_check.py` (add `--day <YYYY-MM-DD>` if a day was given).
2. Show its output as it is: the day table, the totals line, then Atlas health. Every number comes from
   the script; never estimate or recompute.
3. If every health line is green, end with one line: "All green." If a line is amber or red, add one plain
   sentence per line saying what it means, and stop. The operator asks if more detail is wanted.

Rules: facts only. No lessons, no suggested changes, no post ideas (that is /weekly-report).
Never write to the ledger. No helpers needed.
