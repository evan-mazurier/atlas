# Lane 1 notes: choices the contract left open

Simplest option taken each time; none changes a shape another lane depends on.

* **Folder match** (`area_for`): plain case-insensitive string prefix of the normalised path, longest wins
  (no path-boundary check), so a worktree `<repo>-v2-record` falls under `<repo>-v2`.
* **YAML**: `areas.py` carries `mini_yaml` (block maps/lists, flow lists/maps, quotes, comments), checked
  equal to PyYAML on the real `areas.yaml` and `workflows.yaml`. The hook uses it for both files.
* **Implicit task**: a non-exempt edit with no open task writes a normal `work_type` event with
  `type: undeclared` and `implicit: true`, so `derive` needs no special case.
* **Extra event fields** (additive, derive ignores them): `task` on `helper_start` / `helper_paired` /
  `helper_end` events, `carried: true` on a `work_type` that inherited a spent budget,
  `implicit: true`, `entry` on `rework_granted`. `derive` recomputes the carry rule itself.
* **Carry rule**: needs at least one NEEDS_CHANGES version (so budget 0 with no failures does not poison
  the next type) and `needs_changes >= budget`, or `escalated`. The new budget is `min(old, new)`.
  The carry logs `refusal{guard: rework}` on the NEW task and does not deny the switch.
* **Cost window** (`derive`): cost events belong to the task opened last in the session (also after its
  `task_done`, until the next opening), by `task` position, not by an id. Cost before the first task is not
  attributed to any task but still counts in the stats (usage per day, per helper).
* **"checked"**: `last READY version >= final version`, or an `ok` check at the final version when every
  non-check-file edit is already covered by a READY (`other_v <= last_verified`). A task with no edits and
  no verdict is `checked: false` (and never flagged, since the flag needs edits).
* **Stats**: "closed" = status other than `open` with an `ended` time, counted by the local date of
  `ended`. `rework_refused` counts only `guard: rework` refusals; `rounds_hist["refused"]` counts tasks with
  one, and those tasks are NOT also counted in 0/1/2. `unverified` = flagged tasks closed in the last
  4 weeks. `weighted_usage` uses `cost` events by their own date (not task end). v1 jobs are included in
  the KPIs.
* **Report endpoint**: 404 `{ok: false, error}` when the week's file is absent (override dir:
  `ATLAS_REPORTS_DIR`, default `~/.atlas/reports`).
* **Grant entry check**: `noninteractive_entries` in `atlas_config.json` is empty by default (contract:
  "until confirmed, record both and grant"); the entry value is stored on `rework_granted.entry`.
  Fill the list after the record day, e.g. `["sdk-cli"]`.
* **custom-title** lines: read as `{"type": "custom-title", "customTitle": ...}` (also `title` / `text`).
  The real field name is unverified.
* **Dry run** (installer): runs a throwaway copy of the staged hook forced to record mode against a temp
  ledger, so enforce-mode blocks never read as failures and the real ledger is never touched.
* **Tamper check** reads `ledger/state/last_mode.json` (written by `hooks_mode.py` and the installer,
  rebuilt from the last `mode` event if absent), logs once per found value.
* **Helper transcripts** are found at `<transcript dir>/<sid>/subagents/agent-<id>.jsonl`; the helper type
  comes from the sibling `.meta.json` (`agentType`, unverified key) or the pairing map.

## Rework round 1 (senior review)

* Verdict versions come from a per-agent map (`agents`) that `helper_end` never empties; an unknown launch
  version is recorded as `null` and never satisfies the done-lock (an unknown NEEDS_CHANGES still burns a round).
* Carried rounds live in `carried_nc`, added to the distinct-version count (hook state and `derive` alike).
* `interactive_entries` replaces `noninteractive_entries` (empty = record day). Before enforce set it to
  `["claude-desktop", "cli"]`.
* Deny rule is now: a command that invokes `claude` (by name or by `...\claude.exe` path) followed by
  `-p/--print/--resume`. `/grant-rework` text alone (git messages, greps) passes.
* Pairing drops unclaimed launches older than 30 min or from another task (pruned on every Txn); exact
  pairing through `agent-<id>.meta.json` `toolUseId` when the payload gives a transcript path (key unverified).
* `query.all_tasks` derives inline once, then only serves the cache and refreshes in a background thread
  when a ledger file changed or the cache is older than 60 s (`query.invalidate()` for tests).
* Installer stages in a temp dir, dry-runs there (exit 0, empty stdout, <= 3 s, no hook-errors.log, the
  expected events logged), and only then copies into the live hooks folder.
