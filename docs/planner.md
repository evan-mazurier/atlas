# Atlas — Planning Layer (`plan.yaml`, milestones, progress, the planner agent & the panels)

> The locked design of record for how Atlas models **planned work, milestones and progress** across
> projects. Scoped with the operator 2026-06-30 → 2026-07-01; **planner-agent definition + architect alignment
> finalized 2026-07-01** (§6, §6a). **Build follows this document exactly.**
> Companion to `agentic-structure.md` (agents & procedures).
> **The *time* dimension — the operator's availability, task effort estimates, sessions, partial-completion
> and intra-day scheduling — lives in `scheduling.md` (scoped 2026-07-16), which extends the task
> model and the planner agent below. Read it alongside this doc.**

---

## 1. The gap this closes — and the vision behind it

Atlas models **org** (`agents.yaml`), **current state** (`status.yaml`), and **jobs** (dispatch sessions). It
has **no model of the planned work sequence or of progress**, so each session the operator re-reads `PLAN.md`,
re-derives what's next, and answers *"is this late? is this project at risk? what deserves priority?"* from
memory, with nothing to check the answer against.

**Why the whole layer exists:** the operator opens Atlas and asks *"what is on the plan, what is the priority,
is anything at risk?"* — and Atlas answers from a single source of truth: it reads the day's tasks, reads each
project's progress toward its current milestone, and names what deserves priority and what is blocked. The
planning layer exists to **move the sequencing load off the operator and into a file** — the operator still owns
scoping and dispatch; the system remembers the dates, rolls up progress, and spots the slippage. Everything
below is built to make that briefing possible and true.

---

## 2. Guiding principle — one primitive, two consumers

Same pattern Atlas already runs on, applied to planned work:

| | Current state | Planned work |
| --- | --- | --- |
| **Contract (per project)** | `status.yaml` | **`plan.yaml`** |
| **Who writes it (work plane)** | project sessions | **the planner agent** (+ deterministic hooks) |
| **Who reads it (control plane)** | the control center | **the planning panels + the assistant** |

The panels and the agent are the **read side** and **write side** of one new contract, `plan.yaml`. Independently
buildable; we build the read side (panels) first over simulated data, then the agent inherits the exact contract.

---

## 3. The contract: `plan.yaml` v2

One per project, in the project root next to `status.yaml` (work plane owns it; Atlas only reads it). Pointed to
by a `plan_file:` entry in `registry.yaml`, mirroring `status_file` / `procedures_file`.

```yaml
project: orchard-ops

# MILESTONES — the "bigger picture" targets. The unit that carries DATE, RISK and PROGRESS. A project
# has a SEQUENCE of them over its life; exactly ONE is `active` at a time (the current focus). `done`
# milestones are RETAINED forever (achievement history / the past timeline) — never deleted.
milestones:
  - id: pilot
    title: "Ship the EV-toolkit pilot as a working proof"
    status: active            # active | next | later | done   (exactly ONE active per project)
    target_date: 2026-07-15   # OPTIONAL — the "on time?" anchor; omit for "when it's ready"
    done_date:                # set when marked done (retained as history)

# SLICES — the granular tasks that roll UP to a milestone. The daily unit (Today panel, dispatch).
tasks:
  - id: nicegui-ui
    title: "NiceGUI UI task: input form → engine → results table"
    milestone: pilot          # OPTIONAL — rolls up to a milestone; OMIT = loose backlog (counts toward no bar)
    type: dispatch            # dispatch (worker can do it) | manual (the operator does it)
    state: next               # done | in_progress | next | todo
    priority: high            # high | medium | low
    target_date: 2026-07-02   # OPTIONAL — the DEADLINE (due-by). NOT the scheduled day (that lives
                              #   in schedule.yaml). Feeds future calendar column / overdue detection.
    estimate: 4h              # OPTIONAL (scheduling.md) — effort; agent-speed if dispatch, human-speed if manual
    remaining: 2h             # OPTIONAL (scheduling.md) — effort left, burns down across sessions (starts == estimate)
    sessions:                 # OPTIONAL (scheduling.md) — one entry per scheduled slot of work (the split mechanism)
      - { id: sess_a1b2, done: true, merged_at: 2026-07-16 }
    done_date:                # set when the TASK is marked done (the operator-gated: remaining == 0 — see scheduling.md §6)
    dispatch: job_...         # link to the Atlas job (dispatch tasks)
    notes: ""
```

Rules of the model (all locked):
- **Milestones:** a project has several; **exactly one `active`**, the rest parked `next` / `later`, plus retained
  `done`. `next`/`later` sequence by **their order in the file**. Dates are **optional**.
- **Task → milestone link is optional.** Set → the task counts toward that milestone's progress. Omitted →
  **loose backlog**: a real task that shows in the plan/Today but counts toward no bar, until the operator or the planner
  **files it** under a milestone.
- **`next_target`** (in `status.yaml`) is **derived** = the title of the top `state: next` task (single source of
  truth). *(Transition: hand-set until the merge-advance hook lands.)*
- **Bi-temporal:** `done_date` (tasks + milestones) feeds the **past** (progress trail / timeline); `target_date`
  feeds the **future** (calendar columns). Nothing done is ever deleted.

### 3a. Who owns which field — the two authors of `plan.yaml`

`plan.yaml` has **two authors**, and approving a planner proposal is a **whole-file overwrite**
(`dispatcher.approve_planner`). So a field only survives a planner run if something makes it survive.
The split is enforced in code (`dispatcher._ATLAS_TASK_FIELDS` + `_preserve_atlas_task_fields`), not
left to the planner's good behaviour:

| | fields | why |
| --- | --- | --- |
| **Planner-owned** | `id` `title` `milestone` `type` `state` `priority` `target_date` `estimate` `remaining` `notes`, and the ordering | the backlog itself — maintaining it *is* the planner's job. `state` included: the operator also hand-edits it, and the planner re-sequencing work is the whole point. `estimate`/`remaining` included: the charter tells it to estimate every task, and its re-estimate mode exists precisely to revise `remaining`. |
| **Atlas-owned** | `dispatch` `sessions_done` `review_pending` `done_date` | machine bookkeeping the planner has no authority over and no way to know. The charter already says it never marks tasks done — "a deterministic hook does that on merge". |

On approval, the Atlas-owned fields are re-asserted onto the proposal from the on-disk file, keyed by
task id — **disk is the sole source of truth** for them. Present on disk → written back; **absent on
disk → stripped from the proposal**, so a planner cannot invent a `dispatch:` backlink pointing at a
job it never launched (which would make a later merge advance the *wrong* task). It is text surgery,
so the planner's own formatting survives; only those four lines are rewritten. Same guarantee the
schedule branch gives pinned placements and life-events (`docs/scheduling.md` §7).

The one thing preservation cannot save is a task the planner **deletes** — there is nothing left to
write onto. That case is reported in the approval note rather than passing silently, since the job
that task linked to can no longer advance anything.

---

## 4. The deterministic progress model (the heart — it must be TRUE, never decorative)

Progress is measured **per active milestone**, from data, with zero guesswork:

> **progress = (its tasks with `state: done`) ÷ (its total tasks)**

Displayed as **all three together**: the **bar** filled to the %, the **raw count** (`4 / 7`), and the **%** (`57%`).

Why this is true and not a random fill:
- **Scope growth is a feature, shown honestly.** Discovering mid-milestone that you need 2 more tasks grows the
  denominator, so the % drops — correct: the milestone really *is* bigger than thought. The **raw count keeps it
  legible** — `4 / 7` → `4 / 9` reads as *"scope grew,"* not *"I went backwards."*
- **`done` is a concrete transition, no fuzziness:** a **dispatch** task → `done` when its session **merges** (the
  merge-advance hook sets `done` + `done_date` automatically); a **manual** task → `done` when the operator/the planner
  marks it. No "in-progress = 50%" guessing — a task is done or it isn't.
- **Milestone completion is a DECISION, not automatic.** Hitting 100% (5/5) does **not** auto-close the milestone;
  it **prompts**: *"all listed tasks done — is this milestone achieved, or is there more?"* More → add tasks
  (→ 5/7, back below 100%). Truly done → the operator/planner sets `status: done` + `done_date`, and a parked milestone is
  promoted to `active`. So the bar is always true to the **current known scope**, and "done" means decided-done.
- **Unweighted** (a task is a task), by design — keeps it deterministic. Comparable size is enforced by the
  **planner agent's charter** (§6), so `count ≈ effort` without subjective weights. *(Effort-weighting parked, §12.)*
- **Empty milestone (0 tasks):** `0 / 0` → empty bar, labelled **"not scoped yet"** (no divide-by-zero).
- **Task-level progress is a SEPARATE, softer signal — it never touches this bar.** The scheduling
  layer adds per-task *estimate burn-down* (`(estimate − remaining) / estimate`, shown as an estimate;
  see `scheduling.md` §5). The **milestone bar stays strictly binary** (a task counts only when it is
  *done*), so a half-done big task moves the *task* bar but **not** the milestone bar — the hard signal
  can never be softened. A task is `done` only on the operator's gated confirmation that `remaining` is 0.

---

## 5. The at-risk rules (deterministic status color, per project)

Computed from data we already have (plan dates/states, `status.yaml` blockers, live jobs). Drives the color dot on
the left momentum strip:

- 🔴 **red** — the project has **blockers** (`status.yaml`), **OR** the active milestone's **`target_date` has
  passed** and it's not done, **OR** a task cannot fit its deadline in the free time that actually remains
  (`scheduling.md` §8, "capacity-aware red" below).
- 🟡 **amber** — a session is **live and waiting on the operator** (`needs_input`) — that's the normal contract-
  confirmation step of dispatch, not a real blocker, so it's amber and not red — **OR** a task in the active
  milestone is **overdue** (its `target_date` passed, not done), **OR** **stalled**: no task completed in the
  active milestone for **≥ 10 days**.
- 🟢 **green** — none of the above.

**No job STATUS colours a project red.** This dot reflects the **plan's** health — blockers, deadlines,
capacity, stalls — not a job's outcome. A `failed` or finished session is surfaced by the **Sessions panel**
and the **NEEDS YOU** rail, not by this dot.

The **10-day stall threshold** is the one tunable knob (`ATLAS_STALL_DAYS`). *(A smarter "projected to miss the
date" predictive signal is parked, §12 — it's less deterministic, so not v1.)*

**Implementation (2026-07-26; needs_input/stall-while-live rules revised 2026-08-04; dead red-on-failed rule
removed 2026-08-04).** `state._at_risk_detail()` in `the control center's state layer` is the **one** implementation of this
rule — `state.at_risk()` (colour only) and `server._project_at_risk()` (colour + spoken reason, used by the
voice briefing) both delegate to it; don't re-fork the logic a third time. Unit-tested in
`control center/test_at_risk.py`, including a table that runs the same fixtures through both delegating call sites to
guard against exactly the kind of drift that motivated this collapse (the two copies had drifted: the
on-screen dot had gained rules the voice briefing never got). Served as **`risk`** on every project in
`/api/state`. It previously lived in `app.js` as `momentumRisk`, which meant it coloured only the
momentum-strip dot, no other consumer (voice daemon, Streamlit, receptionist) could see it, and it sat outside
the test suite. Rules that extend the plain red/amber/green list above:

- **Capacity-aware red** (`scheduling.md` §8): a task whose `remaining` effort **cannot fit** in the free time
  actually left before its `target_date` — the date is unreachable given real availability.
- **Only a LIVE session counts** for the `needs_input` amber rule and for suppressing the stall clocks (below).
  A terminal job is history, not "at risk now" — and **there used to be a red rule for a live `failed`
  session, removed 2026-08-04: it was unreachable.** `failed` is a TERMINAL status, never in `ACTIVE_STATES`,
  so "live AND failed" can never actually happen — the rule was dead code from the day it was written. Don't
  re-add a red-on-job-status rule without first making sure the status you're keying on can genuinely be live.
- **Stall has two clocks.** Preferred: time since the last completed task in the active milestone. A milestone
  with **no completions yet cannot prove 10 days elapsed**, so that clock stays silent — and *only then* do we
  fall back to how long `status.yaml` has gone untouched. Without the fallback, a project with no plan (or a
  plan with nothing finished) could never go amber however long it sat. **Neither stall clock fires while the
  project has a live active session** — work in flight is not stalled.

**`health` in `status.yaml` is now legacy display text and colours nothing.** The colour is always computed.
The escape hatch is a separate, opt-in **`health_override: red|amber|green`** — deliberately NOT `health`,
because every sidecar already carries a hand-typed `health` and honouring that would let the stale typed value
win on every project, which is the exact problem this replaced. An override is rendered visibly **as** an
override (a `manual` chip + the computed value in the tooltip), never silently as if it were the data.

---

## 6. The planner agent — the operator's planning assistant

**What it is (the role, in one line):** the planner is Atlas's **planning brain** — an on-call assistant
that holds a complete, current understanding of one project's plan and progress, reasons about it, and
**takes the planning mental load off the operator.** It is the third global brain role (beside `receptionist` +
`architect`) with a **per-project subject**: pointed at one project, it proposes and maintains that
project's `plan.yaml`. On-demand, human-gated, exactly like the architect (propose → the operator approves →
Atlas writes).

**6.1 Boundaries (hard, non-negotiable):**
- It **never does project work** — it plans, it does not code/write/analyze the project. Plane separation holds.
- It **never marks tasks done or advances progress** — that is the deterministic merge-advance hook (below), not
  the agent.
- It **never reads a project's prose** — no `PLAN.md`, no master doc, no README. It reads only **structured
  state** (`plan.yaml`, `status.yaml`, job/merge history). Anything it doesn't know, **it asks the operator.** *(This
  upholds Atlas's core rule — "Atlas never parses a project's prose" — and makes the planner work on a
  brand-new project that has no documents at all.)*

**6.2 Inputs (structured only):** the current `plan.yaml` (empty on a new project), the project's `status.yaml`
(blockers, phase), recent **job/merge history**, and **the operator, interactively** (it interrogates for anything
ambiguous — the vision comes from the operator, not a file).

**6.3 What it produces:** a proposed `plan.yaml` — the milestone → task hierarchy:
- **Milestones** — define / date (optional) / prioritize; **exactly one `active`**, rest parked `next`/`later`,
  `done` retained. On completion, propose promoting the next.
- **Tasks** — decomposed, assigned to a milestone, typed `dispatch|manual`, prioritized, ordered; **files
  loose-backlog tasks** under milestones.
- **Charter rule — decompose into roughly comparable-sized, individually-dispatchable tasks**, so the unweighted
  `count` is a fair proxy for effort. This is what keeps progress honest *without* effort-weighting.
- **Reasons about risk & sequencing** — surfaces what's at risk and how to order the work, in plain language, in
  its summary. *(The at-risk **colors** are computed deterministically by code, §5; the planner **explains** them
  and suggests what to do.)*

**6.4 The charter (goes verbatim-ish into `agents.yaml`; this is the role scoping):**
> *You are Atlas's planner — the operator's planning assistant for one project. You hold a complete, current
> understanding of the project's plan and progress and your job is to take the planning mental load off the operator.
> Given where the project is now, propose and maintain its milestone→task plan: define, date (optionally) and
> prioritize milestones (exactly ONE active); decompose work into roughly comparable-sized, individually
> dispatchable tasks; file loose backlog tasks under the right milestone; set the active milestone and park the
> rest. Reason about what is at risk and how to sequence the work, and surface it clearly and briefly. You read
> ONLY structured state — `plan.yaml`, `status.yaml`, job/merge history — NEVER a project's prose (no `PLAN.md`,
> no README). Anything you do not know, ASK the operator. You NEVER do project work and you NEVER mark tasks done (a
> deterministic hook does that on merge). You propose; the operator approves or gives feedback; Atlas writes `plan.yaml`.*

- `when:` → *"Your planning brain for a project — proposes / re-plans its milestones + tasks on your call. Never
  does project work."*
- `substrate: claude · model: opus · effort: high · permissions: {write: true (plan.yaml only), git: false} ·
  verified_by: human` — same profile as the architect.

**6.5 Invocation model — on the operator's call (plus ONE auto-triggered, still-gated review).** Symmetric with the architect. It is
**one job, state-adaptive** (not several modes): *"propose an updated milestone→task plan for where this project
is now."* The trigger only changes what "now" is:

| the operator calls it when… | It proposes |
| --- | --- |
| He **just wired a project** (empty `plan.yaml`) | the initial milestone→task plan, from scratch (heavy interrogation) |
| A milestone hit **100%** (completion prompt fired) | the next milestone + its tasks |
| **Reality drifted** — scope grew, priorities shifted, new work found | a re-plan: adjust milestones, file loose tasks, re-order |
| A dispatch session **merged** (auto-triggered; see `scheduling.md` §6) | a **remainder review** — how much of the task got done, the revised `remaining`, and re-scheduling the rest |

The **remainder review is auto-*triggered*** by the merge hook (6.6), but it is still **human-gated** —
the planner *advises* the remaining effort and the operator approves or revises it with the planner; the hook
never decides the remainder or marks the task done itself. Everything else in 6.6 is pure deterministic
code (not the agent).

**6.6 Deterministic hooks (NOT the LLM — plain code, no gate):**
- **Merge-advance:** when a dispatch job **merges**, its linked **session** → `done` (NOT the task — see
  `scheduling.md` §6), and it **auto-opens the planner's remainder review** (6.5). The **task** goes `done`
  + `done_date` only when the operator gates it (the review agrees `remaining` is 0). When a *task* is thus marked
  done, if that brings its milestone to 100%, raise the **completion prompt**; recompute the derived
  `next_target`; pre-fill the next dispatch from the new top `next` task.
- **Progress + at-risk** (§4, §5) are pure functions over `plan.yaml` + `status.yaml` + jobs — computed live in the
  control center snapshot, not by the agent.

So: the **agent** does the reflective re-planning (judgment); the **hooks** do the mechanical advancement (deterministic).

---

## 6a. The shared interaction model (planner ≡ architect ≡ dispatch — one feel)

Atlas is easiest to use when its brain agents behave identically. All three now share **one** loop:

> **interrogate (if needed) → a short readable SUMMARY the operator skims → then either APPROVE, hand-EDIT the file, or
> type FEEDBACK to have the agent REVISE (same session) → Atlas writes the file.**

- **Dispatch** writes the run · **Architect** writes `agents.yaml` (+ `procedures.yaml`) · **Planner** writes `plan.yaml`.
- **The confirm view (new standard, applies to architect + planner):** a **synthesized plain-language summary on
  top** (what's proposed, in prose — e.g. *"3 milestones. Pilot active, 5 tasks, due Jul 18…"*) that the operator reads and
  approves, with the **full editable YAML in a collapsible section below** for inspection / hand-edit. The existing
  "request changes" feedback box + approve/discard buttons stay.

**Architect alignment (build this alongside the planner, before finishing):** the architect today (a) still reads
`PLAN.md` and (b) dumps raw YAML with no summary. Both change to match the above — **stop reading `PLAN.md`
(interrogate only)** and **add the summary-on-top + collapsible full YAML**. After this, architect and planner are
the same shape; only the file they write differs. *(This is why the build touches the architect, not just new
planner code.)*

---

## 7. The front-end — exactly what gets built, per surface

Bi-temporal, and consistent with the existing control center (see §8). Four surfaces:

**A. Left frame — per-project "momentum strips" (the headline).** Stacked **under the existing Today panel**,
always visible. One strip per project:
- project name + the **at-risk color dot** (§5),
- the **active milestone** title,
- a **progress bar** filled to the %, plus the **count `4 / 7`** and the **%**,
- **"due in N days"** when the active milestone has a `target_date`.
- **UX:** clicking a strip opens that project's per-project plan panel (same action as the card's `🗓 plan`
  button) — consistent with the existing open behavior. `0/0` milestones show "not scoped yet."
The left rail becomes **Today (what's on now) + Momentum strips (how each project is moving / what's at risk)**.

**B. Per-project plan panel — the project's ARC** (opens from the card's unused direction). **Revised
2026-08-04; supersedes the original day-column timeline described here.** It answers one question at a glance:
*where does this project stand?* — never *when is it worked*.
- A **summary strip**: `milestones 2 / 5` · `tasks 11 / 31`, over the **arc rail** — one segment per milestone,
  weighted by its task count and filled by its progress. Clicking a segment opens + scrolls to that milestone.
- Then **one section per milestone, in plan order** (the arc): its status tag, title, `4 / 9`, a progress bar and
  its date — `due in Nd` / `overdue` while open, `done <date>` once closed. The bar is §4-binary; effort never
  enters it. The section's **task list collapses**; the `active` one is expanded by default.
- **Task rows** are one line: a state glyph (`▸ next` / `◐ in progress` / `• todo` / `✓ done`, struck through
  when done, spelled out by a legend under the summary), the title, and only the meta that earns its place —
  `high` priority, `dispatch` type, and the remaining/estimated effort. Rows are held in **`plan.yaml` order,
  never re-sorted**: that sequence *is* the plan (tasks reason about each other as a chain), so a task finished
  out of order stays where it was declared rather than sinking to the bottom. The glyph carries what is live.
- Clicking a row opens the full detail card **inline, under that row**, so the arc above it never moves. When
  the task's `remaining` reaches 0 while it is still open, the effort block **asks** whether to complete it —
  the §6 gate stays the operator's, but it is no longer possible to strand a task by answering the remainder review
  (which used to be the only thing carrying the "mark task complete" button).
- **Bounded by construction** — fixed width + a max-height scroll body — so the panel can never grow over its
  neighbours on the canvas.

*Why it changed:* the panel used to be a day-lane keyed on `target_date`, which Stage 6.5 redefined as a
**deadline** (the scheduled day lives in `schedule.yaml`). So scheduled work never appeared on a day, every task
fell into an unbounded "backlog" grid, and the panel spread over the neighbouring project cards. It was also a
**second calendar** competing with the merged Plan surface. The split is now clean: **the Plan surface owns
time, the per-project panel owns advancement.**

**C. Global plan drawer** (bottom, all projects): **light touch** — the task **detail card** also shows the
task's **milestone**. Everything else stays as-is (the cross-project *progress* view is the left strips; the drawer
stays the cross-project *timeline*).

**D. Project cards on the canvas:** **unchanged** for now (they already show `next_target`, derived from the top
`next` task) — avoids clutter.

Timeline interaction (the **cross-project** surfaces — the global timeline and the day-view; the per-project
panel dropped its lane in B above): a **horizontally-draggable lane of day-columns** (wheel + scrollbar, **no**
left-drag), today highlighted, scroll position preserved across polls, `today` button re-centers, past days
greyed + click-to-light (exclusive), whole-day-click → full uncropped detail card, undated → backlog rail.

---

## 8. Consistency mandate (UI **and** UX — non-negotiable)

Every new element MUST match the existing control center exactly. Reuse, don't reinvent:
- **CSS variables + classes:** reuse `--red/--green/--amber/--blue/--txt/--muted/--dim/--line/--line-hot`, and the
  existing `.plan-task`, `.plan-day`, `.plan-detail`, `.ps-tag`, panel/card idioms. New elements are variants of
  these, not new visual languages.
- **Fonts:** Orbitron (headers), Rajdhani (body/labels), JetBrains Mono (small mono tags) — as everywhere.
- **Color discipline:** red is the primary accent; **blue only for `today` / `next` / active accents**; type-color
  stays blue = dispatch / amber = manual. **No Jarvis-blue recolor yet** (parked, whole-control center pass later).
- **Sizes/spacing/rounding/glow:** match the current panels (14px card radius, the `--line-hot` border glow, chip
  paddings, tag sizes).
- **UX behaviors identical:** the same scrolling (wheel + scrollbar, no left-drag), the same click-to-open-detail,
  the same exclusive selection, the same fly-to/zoom framing, the same "click a thing → open its panel." A user
  should feel the progress features were always part of the control center.

---

## 9. Simulated data & cleanup protocol (so nothing residual survives, and no real task is deleted)

To shape + verify the UI before the agent exists, we seed **lots of simulated milestones + tasks** across
**orchard-ops** and **retired-tool**. Marked so cleanup is deterministic:

- **`retired-tool` (`D:\work\retired-tool\plan.yaml`) = 100% THROWAWAY.** Its header already says so.
  **Cleanup = delete the whole file + remove the `plan_file:` line from that project's `registry.yaml` entry.**
- **`orchard-ops` (`D:\work\orchard-ops\plan.yaml`) = mixed.** The **two REAL, completed tasks — `org-design`
  (done 2026-06-26) and `engine-v1` (done 2026-06-28) — are REAL history; NEVER delete them.** Every simulated
  item (all demo milestones + all demo tasks added for verification) carries an explicit **`sim: true`** field and
  sits under a clearly-commented `# --- SIMULATED (remove before the planner agent) ---` block.
  **Cleanup = remove only the `sim: true` items** (and the comment block), leaving the two real tasks (which may be
  grouped under a real `pilot` milestone that is itself the only real milestone — kept).
- `sim: true` is an inert marker (the loader ignores unknown fields); it exists purely for safe cleanup.
- **Cleanup happens right before the planner agent runs** (Slice-4), which then regenerates real plans. Documented
  here so it's deterministic and residue-free.

---

## 10. Build order (build the read side first; each step independently verifiable)

1. **Data:** `plan.yaml` v2 with **simulated milestones + tasks** (per §9) on orchard-ops + retired-tool; `state.py`
   loads `milestones` + computes per-milestone **progress** and the **at-risk** status into the snapshot.
2. **Left momentum strips** (the headline) — under Today. Verify first.
3. **Per-project plan panel:** milestone header + task milestone-tags + milestone history.
4. **Global drawer:** task-detail shows its milestone.
5. **(Then Slice 4 — the write side)** run the §9 cleanup first, then build, in one pass:
   (a) the **planner agent** (charter §6.4, mirroring the architect's dispatch/gate/write path) + its
   `plan.yaml` proposal → summary-on-top confirm (§6a) → approve/edit/revise → write;
   (b) the **deterministic merge-advance hook** (task→done, `next_target`, 100%→completion prompt);
   (c) the **architect alignment** (§6a): stop it reading `PLAN.md`, add the same summary-on-top + collapsible
   confirm view. After this, architect and planner are the same shape.

*(the operator will launch the build to do steps 1–4 in one pass, then verify with guided checks.)*

---

## 11. Layout note — the framed control center (context)

The left frame (Today + momentum strips) is the first populated piece of the **framed control center** (canvas confined to
a center box, surrounded by fixed frames). That confinement needs the camera-math refactor (window-based →
bounded-rect) and is its own later task; tracked in the private handoff. The strips overlay the canvas edge until then.

---

## 12. Parked (recorded so nothing is lost)

- **Parallel milestones** (more than one `active` at once) — for **Orchard**, the multi-project umbrella. Simple
  successive-milestones (one active) now; revisit when Orchard is wired.
- **Effort-weighting** of tasks — the evidence-triggered fallback if comparable-sized tasks ever proves
  insufficient. One optional field + sum-instead-of-count; cheap to add later.
- **Predictive lateness** — a "projected to miss the `target_date`" signal (date-near + progress-low). Less
  deterministic than §5, so deferred.
- **Voice commands for the plans / the morning briefing** — "what's on today / what's my priority / anything at
  risk"; the assistant reads milestones + progress + at-risk to offload the mental load. Deferred sub-phase after
  the panels + agent (same Vosk-grammar pattern as the other voice commands).
- **Global strategist agent** — cross-project prioritization; added only if the panel/receptionist prove
  insufficient.
