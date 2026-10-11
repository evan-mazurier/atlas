# Plan engine: decisions where the contract left room (W1 Lane 3)

Files: `store.py` (paths, locks, atomic write, section splice), `versions.py` (plan-version.json),
`model.py` (read model), `api.py` (routes). Tests: `test_store.py`, `test_api.py` (temp copies only).

## Extra env overrides (tests and the sandbox; defaults are the repo files)
* `ATLAS_AREAS_FILE` (default repo `areas.yaml`). When set, `model.load_areas()` reads it directly;
  when unset it tries `ledger2.areas.load_areas()` first and falls back to the file if that is empty.
* `ATLAS_REGISTRY_FILE` (default repo `registry.yaml`): where the `plan_file` paths come from.

## Reads
* Task rows read from a project's `plan.yaml` get id `<project>/<task id>` (unique next to tasks.yaml
  ids); `done` ones are left out; `type: manual` -> owner `me`, anything else -> `agents`; estimate
  strings (`2h`, `90m`, `2h30`) -> `estimate_min`; only projects that an area lists under `projects`.
* Event area: explicit, else `match.title_prefix` (all areas, file order), else `match.keywords` (whole word anywhere,
  case and accents ignored, file order). The plan engine does the title matching itself (`model.event_area`); `ledger2.areas` only loads the file.
* Events also carry `busy`, `review_of`, `from_series`, `location`, `remind` when present (additive).
* Placements also carry `pinned`/`done` when set (additive). Range filter applies to events,
  placements, blocks, availability and agents; tasks and the tray are not range-filtered.
* Agents `actual`: a run still open today ends "now"; one that crosses midnight is cut at 23:59.
  Lanes are assigned per day, greedy by (day, start).
* Review tray: runs with `status == "done"` and `verdict == "READY"` from the last 30 days
  (`model.TRAY_DAYS`), minus `reviews:`. `review_min` is the run's value, else 20. `scheduled` is
  `null` or `{event, day, start, end}` of the event whose `review_of` is the task id.
* `review_tray_count()` is cached for 5 s and returns 0 on any error.

## Writes
* Every write: process RLock -> OS lock on `<schedule>.lock` and `<tasks>.lock` (if the files exist)
  -> OS lock on `<version file>.lock` (its folder is created when missing) -> read, detect outside
  changes, mutate, splice, atomic write, bump, save version. A GET takes the same locks (same order),
  so it never reads a state a writer is half way through.
* Outside-change detection scans the namespaces whose file exists. Tray-derived `rv:` keys are scanned
  by GET and by the review endpoints only; a new `rv:` key is stamped with the current version
  without a bump (it comes from the ledger, not from a hand edit).
* First run (no version file): everything is baselined at version 1 with no bump.
* The v1 collision guard is kept (same reason text) but only runs when an event's `day/start/end/busy`
  is new or changed, so a notes-only edit on a legacy overlapping event is not refused.
* Unknown ids -> 404 (v1 delete was a silent no-op). Event type outside `appointment|errand|personal|free`
  becomes `personal` (v1 rule). `start` without `end` (or the reverse) -> 400 (v1 silently dropped both).
* Cross-day move of one occurrence: answer `changed` = the NEW event; the updated series is in the
  top-level `series` key. Both objects get the same new version.
* Moving a whole series (no `occurrence`, or the event is not recurring) shifts its anchor day (v1).
* `block/delete` does NOT release tasks assigned to the block (v1 wrote project plan.yaml files; v2
  may not). `block` accepts the fields flat (as v1) or under `block`.
* `task/state` accepts any of the five states (not only the next one along). `task` upsert is a merge
  like events: `null`/"" removes a field, unknown fields are kept.
* `review/schedule` on an already-scheduled review moves the same event. `review/done` is idempotent.
* A write never creates `schedule.yaml` / `tasks.yaml` (503 `no plan file`).

## For the Chief
* The OS lock creates `schedule.yaml.lock`, `tasks.yaml.lock` (next to the data files) and
  `plan-version.json.lock`. `*.lock` is not in `.gitignore` (not my file): add it before the repo's own
  schedule.yaml/tasks.yaml are ever written by v2.

## Review fixes
* The `type: personal` default applies only to a NEW event; editing a stored event never adds fields.
* A plan-version.json that exists but does not parse (or has the wrong shape) is refused with 500
  (GET and writes), never silently reset to version 1. Delete the file by hand to start over.
* Comments inside a section that is being rewritten are NOT preserved (the section is re-serialised);
  all other sections, and the comments around them, stay byte-identical.

## Screen review changes (2026-10-09)
* Window 07:00-22:00 (`model.DAY_START_MIN/DAY_END_MIN`) and the all-day rule live in `api._event_guard`; `schedmath.collision`
  (06:00-23:00, dashboard code) still does the busy-overlap check after them. A timed busy event retimed on a day that has an
  all-day event is still refused by `schedmath` ("fully unavailable"): known edge, nothing is changed for it.
* `task/place` is the only route that writes both files (write mode `"both"`: each file is written only when its sections are
  dirty; schedule first, then tasks; a failure between the two would leave the event without `planned`, which the GET does not
  depend on for "me" tasks because the event is the truth).
* The agents' lane cap counts planned slots of `tasks.yaml` tasks and `auto_runs` of that day (`MAX_AGENT_LANES` = 3).
