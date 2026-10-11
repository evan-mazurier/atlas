---
name: grant-rework
description: The operator grants more rework rounds on the current task after the budget was spent. User-only.
disable-model-invocation: true
argument-hint: "[n]"
---

The operator typed `/grant-rework` and has granted $ARGUMENTS more rework round(s) on the current task (one
round if no number was given), if the grant was recorded: the hooks log it when the operator's message is sent,
and the rework guard only reopens once that record exists.

Continue the task: the rework budget is open again, so builders may be launched for the remaining
findings. Do not run this command yourself and do not try to invoke it from a shell: only the operator can
grant a round.
