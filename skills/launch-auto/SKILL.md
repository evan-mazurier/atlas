---
name: launch-auto
description: The orchestrator's procedure to start one automatic Claude Code session (a scheduled-task run) for a piece of work, with the boundary, crew and rework budget baked in. Parks the result for the operator's review.
argument-hint: "<area> <type> <what to build>"
---

You are the orchestrator. The operator asked for work to run on its own, or a playbook decided it. One piece of work =
one automatic session. Only reversible work qualifies (branches, drafts, unpublished pages, research notes).
Refuse and tell the operator if the work needs a publish, send, payment, signature, merge to main or contact with
anyone, or if its type is `legal` or `quick-question`: those are never run alone. The launcher also refuses the types `undeclared`
and `playbook`, and any brief with a line starting `/grant-rework`.

Tool names below are generic. Find the real tool for each in your tool list. If one is unavailable, do the
rest, append one line to the build log (`<repo>\docs\v2\build-log.md`, create it if missing) saying
which step could not be done, and say it in your final message to the operator.

1. **Write the brief file** to a temp path (for example in your scratchpad), self-contained, since the new
   session starts cold: goal, exact files or folders, definition of done, where to park the result (a branch
   name `atlas/<short-name>` or a file path). No line may start with `/grant-rework`.
2. **Prepare the launch:**
   `py -3.12 <repo>\tools\launch_session.py --area <id> --type <type> --model <model id> --title "<short title>" --brief <file> --review-min <N>`
   Pass the model you want (Sonnet for builds and drafts, Opus only for judgment work) so the marker records
   the intent. Be aware that a routine run starts on the app's default model: its model cannot be set before
   its first turn. Optionally switch it after the first turn with the session-model tool. The ledger cost
   shows the model that really ran. `--review-min` is your honest estimate of the operator's review time. Add
   `--dry-run` first if unsure.
   Exit code 2 = refused (unknown area or type, refused type, bad brief): fix the cause, do not work around it.
   It prints ONE JSON object `{marker, area, group, model, title, prompt}` and writes
   `ledger\auto\<marker>.json`.
3. **Create the scheduled task** with the scheduled-task tool: name = the title, prompt = the printed
   `prompt` verbatim, model = the printed `model`, no schedule (manual only). Then **run it once** now.
   The run appears in the operator's sidebar (under its routine) and on the Team panel. A routine cannot be started
   from inside a routine run: if you are one, tell the operator to click Run now on the routine.
4. **Remote Control on:** with the session Remote Control tool, switch Remote Control on for the new session
   so it shows on the operator's phone.
5. **Sidebar group (non-routine sessions only):** routine runs cannot be filed in sidebar groups, they stay
   under their routine. Only if the session is not a routine run, move it into the group named by `group`
   in the JSON with the sidebar group tool (create the group if missing). Otherwise skip this step.
6. **Record the session id:** run
   `py -3.12 <repo>\tools\launch_session.py --record-session <marker> <session_id>`
   (it merges `session_id` into `ledger\auto\<marker>.json` and refuses an unknown marker). The ledger
   folder is closed to Edit/Write by design, so never edit the marker by hand.
7. **Wait for the result.** When the session reports its result parked (branch or file named, and its
   `: team-summary` carrying `review_min`), send ONE phone notification with the push notification tool:
   `<title>: ready for review, ~<N> min`. One per session, never a stream. The operator reviews it in the session
   itself (the Plan's review tray is parked since 2026-10-15). If the session fails or stalls, notify once with the reason
   instead, and do not relaunch without telling the operator.
8. **After the operator's review:** try the session archive tool to archive the session. If it is blocked or
   missing, record that in the build log; the app archives sessions idle for 7 days anyway. The ledger
   keeps the record either way.

Never: publish, send, pay, sign, push, merge to main, contact anyone, grant yourself rework rounds, or start a
nested `claude`. Logins, payments and submits are the operator's. Chores the reviewer passes close themselves and are
only recorded; everything else waits for the operator.
