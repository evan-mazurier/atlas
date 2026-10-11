# web2 lane notes (W1 Lane 2)

Decisions where docs/v2/contracts.md left something open, or could not be met as written.

1. **Colour check (contract §4.1, amended by the Chief).** `tools/check_look.py` ports the five checks of
   `tools/vendor/validate_palette.js` (dark mode, adjacent pairs, surface = look.css `--bg`) to Python with the
   validator's constants and math; `tools/test_check_look.py` runs the vendored JS with node and asserts identical
   pass/warn/fail per check. The Machado/CIEDE2000 rule and the 3.0 threshold were dropped. The six approved
   colours: worst CVD 8.4 (protan, #c98500 to #199e70), worst normal 19.8, all pass.
2. **`waiting` in teamlive** is an additive boolean field per session (`waiting`), next to `cwd`; `status` keeps its
   values `working` / `idle` because existing tests assert them. `shell_api` maps it to the attention status
   `waiting`. `/api/v2/team` passes `waiting` and `area` through.
3. **Usage window meter: REMOVED 2026-10-10.** The attention bar no longer shows a window meter and
   `/api/v2/attention` has no `window` key (it never matched Claude's own meter). Gone with it: `shell_api` refresher
   thread, `kit.segMeter`, `core/watcher.py`, `core/ledger.py`.
4. **Area lookup** uses `ledger2.areas.area_for`, falling back to an own longest-folder-prefix match on areas.yaml
   when Lane 1's function returns nothing. `ATLAS_AREAS_FILE` overrides the file (tests).
5. **Attention request cost.** The first `/api/v2/attention` after a server start takes about 1.5 s (teamlive reads
   transcripts for cost); later calls are incremental.
6. **Ledger sample mode:** `?sample=1` in the page query or the hash query reads `sample/ledger-{stats,tasks,task}.json`.
   Ledger task title = `title`, else `summary.goal`, else `session_title`, else first helper label, else id.
7. **Static server:** every stylesheet is linked statically in index.html (screens own their CSS file); hashing is
   cached by mtime. `ATLAS_WEB2_DIR` overrides the folder (tests).
8. `ctx` also carries `areaName(id)`, `query`, and `keepScroll(fn)` (saves and restores scroll of the screen and of
   elements marked `data-keep-scroll`).
9. Sonnet and Haiku model colours are close blues (from the approved kit); tooltips and legends carry the names.

## Plan screen (W2 Lane 3)
* **Deviation from contracts.md §8.2:** the Plan draws its own lanes (`screens/plan-time.js`) instead of calling
  `kit.timelineLanes`, because the kit renderer has no drag, resize or click-to-create. It reuses the kit's CSS classes
  (`.tl`, `.lane`, `.blk`, `.now`, `.pl-leg`), `onColor`, `bindTip` and `legend`, so the look is the charts-b one.
* Pure helpers live in `screens/plan-core.js` (tested by `cockpit/test_web2_plan.mjs`); `#/plan?sample=1` overlays
  sample agent runs and review items (tagged `sample: true`, never saved).
* Saves are serialized, optimistic, carry `base`; a 409 drops the queued saves behind it and forces a reload.
