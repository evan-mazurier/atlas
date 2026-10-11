---
name: ack-break
description: The operator acknowledges a ledger integrity break by its id, after reading its triage. User-only.
disable-model-invocation: true
argument-hint: "<break id>"
---

The operator typed `/ack-break $ARGUMENTS`: an acknowledgement of the ledger integrity break with that id (the id
shown on the Ledger screen, in the integrity details). It counts only if the hooks recorded it when the message was
sent. The id is required: with no id, or an id that is not an unacknowledged break, nothing is acknowledged and the
operator is told so.

The Ledger screen keeps showing the break, marked as acknowledged. A new break, or an edit to the line before an
acknowledged one, makes the status red again. Do not run this command yourself, do not try to invoke it from a shell,
and do not schedule it or send it to another session: only the operator can acknowledge a break, by typing it.
