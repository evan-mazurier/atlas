# Atlas — Scheduling Layer (the time layer: availability, sessions, intra-day scheduling & the scheduling assistant)

> The locked design of record for how Atlas models **time** — the operator's availability and the
> intra-day placement of project work around it. Scoped with the operator 2026-07-16. **Build follows
> this document exactly.** Companion to `planner.md` (milestones, tasks, the planner agent) —
> this doc adds the *time* dimension the planning layer was missing. Read `planner.md` first.

---

## 1. The gap this closes — and the two-step vision

Atlas models **org** (`agents.yaml`), **state** (`status.yaml`), **jobs** (dispatch), and
**planned work** (`plan.yaml`: milestones → tasks). It has **no model of time** — no notion of
*when the operator is available* and *when each task is actually planned into the day*. So `plan.yaml`
knows a task is due Friday but not that the operator is away Wednesday–Thursday; the at-risk math flags a
date without knowing whether there's a single working hour before it.

Today that information sits in a **separate calendar** that knows nothing about the projects, while
Atlas's plan knows nothing about the calendar. This layer closes that split by giving Atlas a
native, sovereign model of time — so a single system knows **both** the work and the when, and can
schedule the work *around real availability*.

**The build is deliberately two steps:**

- **Part 1 (this doc, built now) — native scheduling.** A global, Atlas-owned model of the operator's
  availability + the intra-day placement of project tasks around it, a 6am–midnight day-view, the
  session/remaining model for partial work, and the planner's new **scheduling-assistant** capability.
  Desktop-first, fully native, fully sovereign.
- **Part 2 (parked, §12) — everywhere, beyond the work plane.** Atlas on a VPS + a phone surface, and
  non-work events entered as first-class items from anywhere — *replacing* Google Calendar
  entirely. **Not yet scoped.** Recorded as the final milestone; scoped after Part 1 ships.

### 1a. Why native-first (the decision, recorded)

We considered connecting the Google Calendar API instead. **Rejected — native-first.** The value
The operator wants is a *scheduling assistant* that reasons about his work, estimates effort, and advises
*where* to put a task around his availability — something Google structurally cannot do (it has no
model of the projects, the work, or the effort). Google-first would have propped up the weaker tool
en route to building the stronger one, and its one real benefit — capturing availability on the go
from a phone — is exactly what Part 2's phone surface supersedes. So we skip the throwaway adapter.
**Cheap fallback, if ever wanted before Part 2:** a *read-only* Google → availability import is a
small bolt-on, because the planner always reads Atlas's native model, never Google's API. We just
don't build it now.

This is the same north-star pattern as the model substrate: own the sovereign core, keep any
external source a removable adapter.

---

## 2. Guiding principle — plan.yaml owns the work; the schedule store owns the time

One clean split, and it is the heart of this design:

| Concern | Lives in | Owns |
| --- | --- | --- |
| **The work** (per project) | `plan.yaml` | task definition, **estimate**, **deadline** (due-by), state, milestone, type, sessions & remaining |
| **The time** (global, Atlas-owned) | **`schedule.yaml`** | the operator's **availability** + each task's **placement** (which day + which hour-slot) |

**Deadline ≠ scheduled day.** A task *due* Friday may be *worked* Tuesday. The deadline is a project
fact (stays in `plan.yaml`); the placement is a cross-project fitting decision (lives in the global
schedule). No per-project file ever holds intra-day placement — fitting a task into 2pm Thursday
depends on *every other project's tasks and the operator's availability*, so it can only be decided globally.

`schedule.yaml` holds **lightweight references** to project tasks, never copies — the task itself is
single-sourced in its project's `plan.yaml`.

---

## 3. The contract: `schedule.yaml` (Atlas-owned, global)

One file, in the Atlas repo root (not a project — this is control-plane state Atlas owns and writes).

```yaml
# ATLAS GLOBAL SCHEDULE — availability + the intra-day placement of work around it.
# Atlas owns and writes this (unlike plan.yaml, which the work plane owns). Placements
# REFERENCE project tasks/sessions; the task itself is single-sourced in its plan.yaml.

# AVAILABILITY — when the operator is NOT available. Everything else on the 6am–midnight lane is free.
# No "type" (appointment / free time / errand) in Part 1 — just "not available". Types arrive in Part 2.
availability:
  blocks:                          # hour-level unavailability within a day
    - { day: 2026-07-16, start: "14:00", end: "16:00" }
    - { day: 2026-07-17, start: "09:30", end: "11:00" }
  full_days:                       # whole-day unavailability
    - 2026-07-20

# PLACEMENTS — each is one work-session of a task, dropped into a concrete day + hour-slot.
# A task may have SEVERAL placements (it is splittable across slots — see planner.md task model).
placements:
  - session: sess_a1b2             # the session id (also lives on the task in plan.yaml)
    project: orchard-ops         # which project's plan.yaml the task belongs to
    task: nicegui-ui               # the task id within that plan.yaml
    day: 2026-07-16
    start: "10:00"
    end: "12:00"                   # 2h of the task's estimate booked into this slot
```

Rules of the model (locked):
- **Availability is a denylist.** The 6am–midnight lane is free by default; `blocks` (hour-level) and
  `full_days` carve out the unavailable time. No type/reason in Part 1 — just unavailable.
- **A placement = one session in one slot.** Splitting a task across slots = the task having multiple
  placements (multiple sessions). The scheduler never has to fit a whole task in one contiguous block.
- **Placements reference, never copy.** `(project, task, session)` points into a `plan.yaml`; the task
  definition/estimate/state stay there. If a referenced task disappears, the placement is dropped.
- **No task is ever scheduled onto unavailable time** — a hard constraint on the scheduler (§7).

---

## 4. The task model extensions (mirrored in `planner.md` §3)

`plan.yaml` tasks gain three fields to carry effort and partial-completion. Full authority for the
task/milestone model stays in `planner.md`; these are the additions this layer depends on:

```yaml
tasks:
  - id: nicegui-ui
    title: "NiceGUI UI task: input form → engine → results table"
    milestone: pilot
    type: dispatch                 # dispatch (agent-speed estimate) | manual (human-speed estimate)
    state: in_progress             # done | in_progress | next | todo
    target_date: 2026-07-18        # DEADLINE (due-by) — NOT the scheduled day
    estimate: 4h                   # NEW — expected effort (see §4a for the estimate rule)
    remaining: 2h                  # NEW — effort left; burns down across sessions (starts == estimate)
    sessions:                      # NEW — one entry per scheduled slot of work (the split mechanism)
      - { id: sess_a1b2, done: true,  merged_at: 2026-07-16 }   # a merged (finished) session
      - { id: sess_c3d4, done: false }                          # a planned/upcoming session
    dispatch: job_...
    notes: ""
```

### 4a. The estimate rule (locked) — estimate by *who does the work*

The estimate is **not** a human wall-clock guess. It depends on the task `type`:
- **`dispatch`** → estimate the **agent-execution** time. When the operator works this task he works *with the
  agents*, which compress a lot of work into little time. Estimate accordingly (short).
- **`manual`** → estimate the **human** time, because the operator does it himself.

The planner produces the estimate when it plans/decomposes the task, and re-estimates the **remaining**
on each session review (§6). Estimates are always shown *as estimates*, never as false-precise fact.

---

## 5. Progress — two signals, two honesties (extends `planner.md` §4)

- **Milestone bar stays binary and unchanged** — `tasks with state:done ÷ total tasks`. It cannot lie.
  This is the hard signal. `planner.md` §4 is untouched.
- **Task progress is a separate, softer signal: estimate burn-down** — `(estimate − remaining) / estimate`,
  shown e.g. "≈ 3h of ~4h", **always labelled as an estimate**, never a fake percent. If `remaining`
  keeps growing across sessions, that is shown honestly (the task really was bigger) — the same
  scope-growth-is-a-feature spirit as the milestone bar, at task level.

So the milestone bar is the hard truth; the task bar is the honest estimate. They never contaminate
each other.

---

## 6. Sessions, partial completion & the auto-triggered remainder review (locked)

The lifecycle of a task worked across sessions:

1. The scheduler books a **session** (a placement in a slot), up to available capacity — possibly a
   *slice* of the task, not the whole thing.
2. When that session's dispatch **merges**, the deterministic merge-advance hook marks the **session**
   `done` (not the task).
3. **Auto-triggered remainder review (the key behaviour the operator asked for):** on that merge, the hook
   **automatically opens the planner** in a *"how much of this task got done — what's left?"* review.
   The planner **advises** a revised `remaining`; **the operator approves or revises it with the planner.** This
   is auto-*triggered* but still human-gated — it does not decide the remainder autonomously.
4. Two honest outcomes of the review (the escape hatch, `planner.md` spirit):
   - **Re-estimate the remainder** — same task, set the new `remaining`; the scheduler books more
     sessions until it reaches 0.
   - **Split into subtasks** — when `remaining` keeps ballooning, the task was really several tasks;
     the planner splits it, which also makes the milestone bar finer-grained and more truthful.
5. **Task `done` is the operator-gated.** A merge closes a *session*; the **task** is done only when the operator and the
   planner **agree `remaining` is 0**. The planner advises; **the operator is the gatekeeper** who calls it
   finally done. Only *that* transition moves the milestone bar (`planner.md` §4).

This changes the merge-advance hook from *"merge → task done"* to *"merge → session done + auto-open
remainder review; task done only on the operator's gated confirmation that remaining is 0."*

---

## 7. The scheduling assistant — a new capability on the planner (NOT a new agent)

The planner gains one new job: **fit ready work into available time.** It is the same agent, same
propose→approve loop (`planner.md` §6a), with one important difference in *scope*.

**7a. It is GLOBAL-scope (the one call confirmed with the operator 2026-07-16).** The planner today is a
per-project subject; scheduling is inherently *cross-project* — it lays out **all** projects' ready
tasks into the operator's **one** shared day. So the scheduling assistant is invoked at **global scope**: it
reads every project's `plan.yaml` (ready tasks, estimates, deadlines, remaining) + `schedule.yaml`
(availability + existing placements) and proposes placements across the whole day/week.

**7b. Hard constraints (non-negotiable):**
- **Never schedule onto unavailable time** (§3 availability is a hard denylist).
- **Fill available capacity with slices — never force a whole task into one block.** A 4h task with a
  2h opening → propose a **2h session** and leave 2h `remaining` for another slot. Splitting across
  slots/days is the normal case, not the exception.
- **Respect deadlines & priority** — prefer placements that land ready, high-priority, and
  soon-due tasks into the earliest suitable slots; surface anything that *cannot* fit before its
  deadline (feeds the capacity-aware at-risk signal, §8).

**7c. The loop (identical feel to the architect/planner):** interrogate if needed → a short readable
**summary** of the proposed day/week ("Tue: 2h nicegui-ui 10–12, …; nothing fits before Fri deadline
on X") → the operator **approves / hand-edits / gives feedback to revise** → Atlas writes `schedule.yaml`.

**7d. Charter addition (goes into `agents.yaml` alongside the planner's existing charter):**
> *You also schedule. Given every project's ready tasks (with estimates, deadlines, remaining) and
> The operator's availability, propose where each task is worked across his 6am–midnight days. NEVER place work
> on unavailable time. Fill the available time with SLICES — book a session up to the free capacity and
> leave the rest as `remaining` for another slot; never force a whole task into one block. Respect
> deadlines and priority, and clearly surface anything that cannot fit before its deadline. Propose a
> readable day/week summary; the operator approves, edits, or revises with you; Atlas writes `schedule.yaml`.*

**7e. Invocation.** On the operator's call (like all planner work) **plus** the auto-triggered remainder review
(§6) which, after re-estimating, naturally flows into re-scheduling the remaining sessions.

---

## 8. At-risk becomes capacity-aware (extends `planner.md` §5)

The deterministic at-risk rules gain the availability signal they were missing:
- 🔴 **red** (added case) — a task's **deadline** has no available slot before it (it *cannot* be done in
  time given the operator's availability), or an active-milestone deadline has passed. (Existing red cases —
  blockers, failed/`needs_input` sessions — unchanged.)
- 🟡 **amber** — existing cases (overdue task, ≥10-day stall) unchanged; capacity-aware overdue means a
  scheduled session's day has passed with the task not advanced.

Still pure functions over data we already hold (`plan.yaml` + `schedule.yaml` + `status.yaml` + jobs) —
computed live in the control center snapshot, not by the agent.

---

## 9. The UI — the 6am–midnight day-view (the headline surface)

The global plan view gains a clear **intra-day, hour-resolution day-view**: for each day, a **6am →
midnight lane** showing, unmistakably:
- **Unavailable time** — `blocks` and `full_days` rendered as clearly-marked "blocked" regions.
- **Available time** — free lane, with the **placed task-sessions** dropped into their slots as chips
  (project tag + task title + the slot's hours), each linking back to its task.

**Design intent (the operator gives broad freedom, within these rails):** clear, well-defined, beautiful, and
**on-theme with the current control center**. It must reuse the existing control center visual language
(`planner.md` §8 consistency mandate): the CSS variables, fonts (Orbitron / Rajdhani / JetBrains Mono),
chip/card idioms, rounding, glow, and the existing timeline interactions (wheel + scrollbar scroll,
today highlighted/re-centered, click-a-thing→open-its-panel).

**Color-code note (a real constraint):** "blocked = red" would collide with Atlas's **at-risk red**. So
blocked/unavailable time gets a treatment that reads instantly as *unavailable* **without** clashing
with the danger/at-risk red — a distinct on-theme device (e.g. a muted hatched/greyed region or a
dimmed non-danger tone), decided in the build. Danger red stays reserved for at-risk.

---

## 10. Build order — Part 1 only (three checkpoints, stop-and-verify between each)

Build the read/data side first, verify each checkpoint with the operator before the next:

- **Checkpoint 1 — data + day-view (a + b together).** `schedule.yaml` contract + availability model +
  loader into the control center snapshot **and** the 6am–midnight day-view UI. (a and b verify *together* —
  the day-view is how you see the availability store.) Verify: availability renders correctly on the lane.
- **Checkpoint 2 — task extensions (c).** `estimate` / `remaining` / `sessions` on `plan.yaml` tasks +
  the estimate-burn-down task progress + the planner.md model deltas + capacity-aware at-risk (§8).
  Verify: a task shows estimate + remaining + a burn-down bar; milestone bar still binary.
- **Checkpoint 3 — the scheduling assistant + completion gate (d).** The planner's global-scope
  scheduling capability (§7), the session/merge → **auto-triggered remainder review** (§6), and the
  The operator-gated task-done transition. Verify: the assistant proposes a day layout that never touches
  unavailable time and splits a too-big task into slices; a merge auto-opens the remainder review.

*(Part 2 is not built now — see §12. It is scoped only after Part 1 ships and is used daily.)*

---

## 11. Consistency mandate

Same as `planner.md` §8, verbatim in spirit: every new element MUST match the existing control center
(CSS vars, fonts, chip/card idioms, sizes, glow, and identical scroll / click-to-open UX). New
surfaces are variants of the existing visual language, not a new one. A user should feel the day-view
was always part of the control center.

---

## 12. Part 2 — the final milestone (✅ SCOPED 2026-07-19 → `scheduling-part2.md`)

**Design of record: `docs/scheduling-part2.md`** (scoped with the operator 2026-07-19). The one-line vision:

> **Atlas everywhere, beyond the work plane.** Reach the **desktop** (the sovereign compute core, asleep by
> default + woken via Wake-on-LAN) from a **laptop** (full remote-desktop, borrowing its power) and a
> **phone** (an installable PWA — read + capture life-events), all over a **Tailscale mesh** — and model
> **personal / life events as first-class typed items** (appointments, errands, protected free time,
> with recurrence), superseding Part 1's type-less blocks. **Clean cutover replaces Google Calendar
> entirely.**

The reframe that scoping produced: it is **not** "run Atlas on a VPS" (rejected) — it is **remote
access to the single always-on-capable desktop instance** over a mesh VPN, which is maximally
north-star (no third party ever runs the workload). One decision — the always-on WoL **relay** — is
deferred to on-site testing at the destination; everything else is location-independent. See the doc for the
full model, the `events:` schema, and the staged build order.

## 13. Parked (recorded so nothing is lost)

- **Event types on availability** (appointment / free / errand) — Part 2; Part 1 is type-less.
- **Read-only Google → availability import** — the cheap bolt-on fallback if manual availability entry
  proves annoying before Part 2's phone surface exists (§1a). Not built now.
- **Two-way sync** (pushing Atlas work-blocks back into an external calendar) — the genuinely hard
  part (conflict/echo loops); deferred, likely obsolete once Part 2 replaces Google outright.
- **Effort-weighted milestone progress** — still parked (see `planner.md` §12); the task-level
  estimate here is a *display* burn-down, and does **not** feed the binary milestone bar.
