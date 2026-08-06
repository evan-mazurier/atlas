# Atlas — Agentic Structure (procedures & agents)

> The locked design for how Atlas defines **agents** and **procedures** per project.
> Scoped with the operator 2026-06-25. This is the design of record; build follows it.

---

## Guiding principle

**Autonomy scales with verifiability.** Push autonomy as far as the output can be
checked — objectively (`tests`/lint), by a separate judgment agent, or by you — and
keep a human gate only where verification is hard or stakes are high. Reliability comes
from the **verification topology**, not from agent personas.

---

## Roles (fixed, project-agnostic vocabulary)

| Role | What it does |
| --- | --- |
| **receptionist** | Routes/translates *you* across all projects. Never does work. |
| **architect** | Defines & refines each project's org (writes `agents.yaml`/`procedures.yaml`). |
| **orchestrator** | Sequences a session's agents per a pre-approved plan. **Deterministic — no LLM.** |
| **scoper** | Turns a novel request into a confirmable **contract** (brief + acceptance criteria + test_cmd + plan). |
| **worker** | Executes — code, tests, reports, etc. The specialists. |
| **verifier** (gatekeeper) | Judges a worker's output against its contract. Read-only. |

`tests` (the project's `test_cmd`/lint) is an **objective verifier** — config, not an agent.
**You** are the human gate: approve the contract (#1) and merge (#2).

---

## Global vs project-specific

**Criterion:** an agent is **global** only if it operates *across* or *on* projects by nature.
An agent that operates *within a single project's work* belongs to that project.

- **Global (the brain):** `receptionist`, `architect`, **+ default templates** for
  `orchestrator`/`scoper`/`gatekeeper`.
- **Project's own org (`agents.yaml`):** its `orchestrator`, `scoper`, `gatekeeper`
  (instantiated from the global templates, override-able) **+ its `worker`s + the `tests` verifier.**

The architect, when wiring a project, mostly just decides **what workers it needs and how
each is verified** — everything else is inherited from the brain's templates, so no project
starts from a blank page and standards don't drift.

**When each global agent runs:**
- **receptionist** — *constantly*. The front door: every typed/spoken request to Atlas goes
  through it first (translate → route: navigate / open / dispatch). Lives in the **brain core's
  Ask tab** (no longer a footer input). Never does project work.
- **architect** — *rarely, on demand*. Invoked (1) once when a project is first wired, to propose
  a minimal org, and (2) whenever the operator asks to refine an existing org (add / re-scope / retire
  agents or procedures). Writes `agents.yaml` / `procedures.yaml`; **the operator approves** (`verified_by:
  human`). It is the only writer of org files.

---

## Cardinality (per project)

| Role | Count | Note |
| --- | --- | --- |
| receptionist | **1** (global) | one front door |
| architect | **1** (global) | one org-builder |
| orchestrator | **1** | single coordinator; >1 = ambiguous control |
| scoper | **1** | single front gate for novel work |
| gatekeeper | **1** default (0–3) | +specialized verifiers if a project needs them |
| worker | **1** default (**soft cap 5**) | the only elastic slot; >5 ⇒ over-decomposition, architect must justify |

A project's org is typically **~4–10 agents**.

---

## Models

**Local Ollama is allowed ONLY for the receptionist.** Anything that *does* or *judges*
real work runs on Claude.

| Role | Substrate | Model · effort |
| --- | --- | --- |
| receptionist | Local Ollama | qwen2.5-coder:7b |
| architect | Claude | **Opus · high** |
| scoper | Claude | **Opus · high** (Sonnet·high for simple projects) |
| orchestrator | **Deterministic engine** | none (Sonnet only if a project ever needs dynamic routing) |
| worker | Claude | **Sonnet · high** default · **Haiku** for bounded/mechanical · Opus rare |
| gatekeeper | Claude | **Sonnet · high** (Opus·high for high-stakes) |
| tests | — | none (runs `test_cmd`) |

Reflection tier (architect, scoper) = Opus/high · doing tier (workers) = Sonnet/Haiku ·
judging tier (gatekeeper) = Sonnet/high · coordination = deterministic · receptionist = local.

---

## Procedures

A **procedure** is a **repeatable, never-changing** operation. Work needing reflection is
**not** a procedure → it goes to the scoper.

Two faces:
- `name` + `summary` — the **simple** label shown in the control center.
- `instructions` — the **strict, exhaustive** step-by-step brief the agent runs.

Rules:
- **One owning agent per procedure** (clear accountability). An agent owns many procedures.
- Lives in the project's **`procedures.yaml`**; references its owning agent by id.
- Model/effort come from the **agent**, not the procedure.

---

## Verification

Every agent declares `verified_by`, one of three kinds:
- `verified_by: <agent-id>` — a separate read-only agent judges it (e.g. `gatekeeper`).
- `verified_by: tests` — objective gate (the project's `test_cmd`/lint). Strongest; prefer it.
- `verified_by: human` — you. Fallback for high-stakes or hard-to-verify output.

**Every write-capable agent has a verifier. Verifiers are read-only** (so they carry no
write-risk and don't need their own verifier; `tests` + your merge are the backstops).

---

## Dispatch flow

```
You ask for something
   ├─ matches a procedure?  → YES → confirm & run it           (no scoper; one-click)
   └─ no match (novel)      → SCOPER: asks you questions → drafts CONTRACT (+plan)
                              → YOU approve/edit (gate #1)
                              → ORCHESTRATOR runs the plan (deterministic):
                                   for each plan step: launch its worker
                                   then run verifiers (tests / gatekeeper)
                                   apply gate rules:
                                     pass + commit-ready → commit on atlas/<id> → READY → you merge (gate #2)
                                     needs-changes / test-fail → REWORK (budget N) → re-verify
                                     escalate / budget out → NEEDS_INPUT (you)
                                     infra error → FAILED
```

**The orchestrator needs no LLM:** the scoper produces the plan (you approve it), and the
orchestrator only *sequences* it and applies fixed gate rules. This is exactly what
`worker.py run_gate` already does for the single-worker case — multi-worker runs the plan's
steps through that same gate, in parallel where the plan allows. An LLM orchestrator (Sonnet)
is a per-project exception, only if a project genuinely needs on-the-fly re-planning.

---

## Multi-worker dispatch (BUILT 2026-07-20)

The scoper may declare a **plan** in its contract: an ordered list of steps, each with a
self-contained `brief`, the `paths` it will touch, the step ids it `depends_on`, and optionally
the `agent` that owns it. **Fan-out is always declared, never inferred** — no plan, or a
single-step plan, runs exactly as it always did.

With a multi-step plan, each step becomes a **lane**: its own worktree on `atlas/<job>-<step>`,
its own coding session, its own test gate, its own reviewer, its own rework budget.

The orchestrator scheduling those lanes is **rung 0 — pure code, no model**:

| decision | the rule |
| --- | --- |
| what may start now | every dependency is `ready` (ready-set over the plan DAG) |
| what may run together | declared `paths` don't intersect; overlap becomes an ordering edge, not a race |
| how many at once | `ATLAS_MAX_LANES` (default 3), machine-wide |
| a lane goes red | its own gate loop — tests, reviewer, rework budget — unchanged from serial |
| a lane parks or dies | stop launching NEW lanes; in-flight lanes finish and keep their commits; the session parks as a whole |
| the operator answers | only the lane that asked resumes, in the same claude session; the rest are untouched |
| all lanes green | **JOIN** — merge the lane branches onto one `atlas/<job>` and **re-run the full test gate on the integrated tree** |

**Why the join re-gates.** Each lane was written and tested against the *original* base. Two
lanes can merge without a textual conflict and still be semantically incompatible — B was
built for a world where A's change didn't exist. Only tests on the combined tree catch that.
A clean merge is not evidence.

**Path declarations are load-bearing.** The overlap check is conservative on purpose: a step
that declares no paths, or a repo-wide glob, has unknown blast radius and is serialized against
everything. An *incomplete* `paths` list is the one way to get two workers racing, which is why
the scoping directive is explicit about it.

**Project requirement:** anything dispatched with fan-out must **gitignore its build artifacts**.
The worker commits with `git add -A`, so whatever the test command leaves behind is committed
too. Serially that's noise; under fan-out two lanes on disjoint source can write the same
artifact path and manufacture a merge conflict unrelated to their work.

---

## Concurrency — several sessions on one project (locked 2026-06-26)

Multiple sessions can run on one project, but **only for independent work**. The model:

- **Agents are roles, not singletons.** An `agents.yaml` entry is a *definition* (template). Each
  session instantiates the roles it needs as **independent Claude subprocesses in its own
  `atlas/<id>` worktree**. Two concurrent sessions in `coding` = two separate `builder` instances
  sharing one definition, **no shared runtime state**. Concurrency is safe by construction; it's
  bounded only by a **global resource cap** (max concurrent sessions / token budget, machine-wide —
  config, `ATLAS_*`, *not* an agent).

- **Conflict management is a per-project, DETERMINISTIC orchestrator responsibility — not a new
  agent.** Conflict only ever arises *within* one project (two sessions on the same repo), so by the
  global-vs-project criterion it's per-project. The decision is rule-based, so it stays in the
  deterministic orchestrator (no LLM, same as single-session sequencing), elevated from "sequence one
  session's plan" to "sequence the project's session set":
  - **Admission:** the **scoper declares the paths each session will touch** (+ any dependency on
    in-flight work) in its contract. The orchestrator does a **set-intersection** → disjoint =
    **parallel**, overlap = **queue** (don't parallelize overlapping work — conflict cost > parallel
    benefit).
  - **Merge ordering:** sessions commit on their own branch and **never auto-merge** (gate #2 is
    The operator's). After a merge, any remaining branch is **rebased onto the new base and re-gated
    (tests + quick reviewer)** before its `ready` stands — this catches *semantic* staleness (B was
    coded/tested against the pre-A base), which a textual no-conflict can hide.
  - **LLM escape hatch** (same as the orchestrator's): if a project ever has semantic coupling that
    file-paths can't capture, the **scoper** flags the dependency; only a rare project would need an
    LLM coordinator.

This is the **one place with a cross-session view**, and it's the orchestrator reading the job queue
(which the dispatcher already has) — a rule, not a new brain.

**Status (2026-07-24): both halves are built.** The within-session fan-out (above) and now the
CROSS-session half — two jobs on one project set-intersect their declared paths (disjoint = parallel,
overlap = queue), and a leftover branch is rebased + re-gated after a sibling merges. It's part of
the multi-worker global engine below.

---

## Multi-worker global engine (Part 1 + Part 2 BUILT + VALIDATED 2026-07-24)

**Status: live on main (`e7a889a`); the always-on worker runs it.** Validated by 174 offline tests
+ a live campaign (9/9) + a full-saturation run (9/9, every cap demonstrably binding — 18 claude
sessions through 4 permits, 12 suites through 2, light bypassing the heavy permit at 3). The only
thing that can't be tested without a real `claude` session (real scoper plans/paths, real RAM under
load) is left to a real dispatch — the engine's handling of all of it is proven.

The problem this solved: `worker.py` *was* a **single process that ran one job at a time, serially**.
`main()` looped `drain_once()`, which iterated the queue and called `process(job)` **inline and
blocking**. The only concurrency that existed was `MAX_LANES` *inside* one dispatch's
fan-out. So while a heavy dispatch was coding, a `planner` / `architect` / `vault-keeper` job just
sat `queued` behind it — which defeated Atlas's core premise: running work on several projects at
once. This section is the design that fixed it.

### Vocabulary (the word "worker" is overloaded — pin it down)

| Term | What it is | How many |
| --- | --- | --- |
| **Worker *process*** | the one OS process `worker.py` that drains the queue; singleton-guarded | **1**, unchanged |
| **Job** | one queue unit. Kinds: `dispatch` (heavy), `architect`/`planner`/`vault-keeper` (light) | many queued |
| **Lane** (swimlane "worker#k") | a plan step of a fanned dispatch job — own worktree + coding + gate + reviewer | ≤ `MAX_LANES` per job |
| **claude session** | the actual `claude` CLI subprocess (`run_claude`), on the operator's subscription | one per lane *at a time* |
| **Conversational agent** | `receptionist`, `ask-orchard`, `ask-almanac`, the brainstormer | **not jobs** — see below |

**Conversational agents are out of scope.** The receptionist / ask-* / brainstormer run **inside
the dashboard server process** via `substrate.py` (`chat()`), synchronously answering the control center
chat. They never touch `worker.py` or the job queue, so dispatch concurrency does not affect them.
This engine governs only the **worker's** jobs (heavy dispatch + the light control-plane kinds).

### The shape: still one process, now concurrent

The worker process stays a **single, singleton-guarded process** — one heartbeat, unchanged. What
changes is that `drain_once()` stops calling `process()` inline; the loop admits queued jobs and
submits each to a pool, so the loop never blocks on a job. The heavy compute (the model) runs
**remotely** on the subscription and locally in **subprocesses** (`claude`, `pytest`, `npm`), so
**threads** are the right primitive — no Python-GIL problem. This extends patterns already in
`worker.py` (`run_lanes` already uses a `ThreadPoolExecutor` + ready-set scheduler + locks).

### Two tracks, not two processes

The light/heavy split falls out as **two semaphores in one process**, not two processes:

- **Heavy track** — `dispatch` jobs and their lanes: sandboxed worktree, test suite, reviewer,
  rework loop. The resource-hungry work.
- **Light track** — `planner` / `architect` / `vault-keeper`: **one claude call, no worktree, no
  test suite, no rework loop** — draft → propose → the operator gates the write. Their own small pool so
  "ask the planner" is **always instant, even when the heavy pool is saturated**. That guarantee
  is the entire daily win, and it costs ~0.6 GB and zero CPU (MAX_LIGHT=3 — all three control-plane
  agents can run at once; each added light job is one ~0.2 GB network-bound CLI with no test suite).

### The resource model: two ceilings under Model A's knobs

The operator's intuitive knobs (`MAX_JOBS` dispatches, each up to `MAX_LANES` lanes) stay on top; two
**global semaphores** underneath cap the two real resources so the *product* can't spike. The key
asymmetry: a **claude session is cheap locally** (~210 MB, network-bound — the model runs
remotely) but **subscription-bound**; a **test suite is the local hog** (CPU-bound, 0.5–2 GB —
`pytest` 881 tests / `npm run build` Astro). So the two resources get **separate** caps.

| Knob | Value (reference machine) | Governs | Bound by |
| --- | --- | --- | --- |
| `ATLAS_MAX_JOBS` | 3 | how many dispatch projects are in flight (queue-level control) | disk (worktrees × node_modules) |
| `ATLAS_MAX_LANES` | 3 | lanes a single job may fan into (unchanged) | structural |
| `ATLAS_MAX_WORKERS` | 4 | **global** ceiling on concurrent **claude sessions** | subscription rate limit |
| `ATLAS_MAX_SUITES` | 2 | **global** ceiling on concurrent **test suites** | CPU + RAM |
| `ATLAS_MAX_LIGHT` | 3 | light-track jobs (planner/architect/vault-keeper — all three at once) | negligible |

Because the semaphores are **module-global**, they enforce the ceiling **across all jobs and all
their per-job lane pools at once** — 3 concurrent jobs each with their own pool still share the same
4 claude / 2 test permits. `MAX_LANES`/`MAX_JOBS` are what you *declare*; `MAX_WORKERS`/`MAX_SUITES`
are what actually *runs*.

**Permits are held per step, not per lane.** A lane bounces between the two pools over its life:

```
acquire[claude] → CODE → release[claude]
  → acquire[test] → TEST → release[test]
    → acquire[claude] → REVIEW → release[claude]
      → (if rework) acquire[claude] → FIX → release[claude] → acquire[test] → RE-TEST → …
```

While a lane is testing or parked on `needs_input`, it holds **no** claude permit, so another lane
can code. The pool self-balances.

**Scheduling walkthrough (3 jobs × 3 lanes = 9 lanes wanting to run):**

```
t0: only 4 claude permits exist → 4 lanes CODE (split across jobs is uncontrolled and irrelevant);
    5 lanes WAIT.
t1: a lane finishes coding → releases its claude permit → a waiting lane grabs it. The finished
    lane now wants a TEST permit (≤2); if both busy it waits a beat, then tests.
…  permits recycle until every lane is green …
JOIN: a job integrates only when ITS lanes are all green → full re-gate on the combined tree → ready.
```

### Correctness under staggering

It is **fine that lanes don't finish coding or hit their suite at the same time** — completion
order affects wall-clock only, never correctness:

1. **Lanes are independent** — separate worktrees; declared `paths` are disjoint (overlap is
   *serialized*, never raced).
2. **Jobs are independent** — different projects = different repos; same project = the
   cross-session path check (below) queues overlaps.
3. **Each lane's own gate** validates that lane against the base whenever it runs.
4. **The JOIN re-runs the full suite on the combined tree** after all of a job's lanes are green —
   so the *combination* is validated regardless of the order the parts finished.

Staggering is in fact *beneficial*: coding is long and network-bound, testing is short and bursty,
so `MAX_SUITES`=2 costs almost nothing — lanes rarely want to test at the same instant.

### Cross-session same-project safety (wire the already-designed rule)

Lift the §Concurrency set-intersection from *within-plan* to *across-jobs* — still rung 0,
deterministic, the orchestrator reading the job queue the dispatcher already holds:

- **Admission:** a candidate **heavy** job is admissible only if its declared `paths` don't
  intersect any **in-flight job on the same project**. Overlap → stays `queued`. Disjoint → admit.
  Different projects never conflict → always parallel, bounded only by the ceilings.
- **Rebase-on-merge:** when a job merges, a sibling branch on the same project reaching `ready`
  against a moved base is **rebased onto the new base and re-gated** (tests + quick reviewer)
  before its `ready` stands — catching semantic staleness a textual no-conflict hides.

### Singleton + hot-reload

The singleton guard stays (one process). The **stale-worker-after-merge** gotcha gets worse with an
always-on concurrent worker (a merge to `worker.py`/`dispatcher.py` is not live until the scheduled
task restarts). Add a **code-version rollover**: on a change, drain in-flight jobs → exit → the
scheduler relaunches on new code. This automates the current manual "restart AtlasWorker after
merge" ritual.

### Feasibility on the reference machine (6 cores / 12 threads, 16 GB)

Why the caps are what they are. Peak concurrent RAM under them:

| Load | Count | Each | Subtotal |
| --- | --- | --- | --- |
| claude coding sessions | 4 | ~210 MB | ~0.8 GB |
| test suites (worst = npm build) | 2 | ~1.5–2 GB | ~3–4 GB |
| light claude | 3 | ~210 MB | ~0.6 GB |
| **Dispatch peak** | | | **~4.4–5.4 GB** |

Fits 16 GB with headroom for OS + control center + worker + apps. The earlier "9 sessions / 9 suites"
worst case is **not** survivable here — 9 concurrent suites = 5–13 GB + 9 cores wanted on 6 → hard
swap; RAM is the wall. So `MAX_SUITES`=2 is the true hardware guardrail; `MAX_WORKERS`=4 is
RAM-trivial but subscription-bound (tune down on 429s). On a future rig these two numbers rise and
the machine scales — **config only, no code change** (the "hardware at the point of need" north
star).

### Build split — both parts BUILT (2026-07-24)

- **Part 1 — light control-plane track ✅.** `drain_once()` submits to a heavy + a light
  `ThreadPoolExecutor` instead of blocking; planner/architect/vault-keeper (own pool, `MAX_LIGHT`=3)
  run concurrently with, and never blocked by, a heavy dispatch. Stop signal made per-job (thread-local).
- **Part 2 — parallel heavy dispatch across projects ✅.** Heavy lifted to `MAX_JOBS`; the two global
  permits (`CLAUDE_SEM`=`MAX_WORKERS` in `run_claude(heavy=True)`, `TEST_SEM`=`MAX_SUITES` in
  `run_tests`) span cross-job *and* within-job lanes — light claude calls pass `heavy=False` so the
  light track is never starved. The cross-session same-project overlap check (`job_paths` set-
  intersected via `paths_conflict`, read-only scoping exempt) gates admission; a sibling merge flags
  ready jobs for a `regate_job` rebase-onto-new-base + test re-gate (tests-only — content was already
  reviewed); a `worker.py`/`dispatcher.py` mtime change triggers a drain-then-exit **hot-reload
  rollover** so the supervisor relaunches on new code automatically.

---

## State → agent lighting map (org view mirrors the state machine)

| lifecycle state | lit in the org |
| --- | --- |
| queued | orchestrator (idle) |
| scoping | scoper |
| coding | the worker in the active plan step |
| testing | `tests` verifier |
| review | gatekeeper |
| reworking | that worker again |
| needs_input | you (human) |
| ready | gatekeeper ✓ → arrow to you |
| merged | you |
| failed | the failed step (red) |

---

## File layout

```
D:\work\atlas\agents.yaml                 ← brain: globals (receptionist, architect) + templates
<project>\agents.yaml                   ← project org: workers + instantiated orch/scoper/gatekeeper
<project>\procedures.yaml               ← project's repeatable procedures (reference agent ids)
D:\work\atlas\examples\demo-app\          ← the fake example project (design preview)
```

**`agents.yaml` agent fields:** `id · role · charter · model · effort ·
permissions{write,git} · verified_by · procedures[] · escalates_to`.

---

## Build status

- **Design preview (this session):** brain `agents.yaml` + a minimal example project
  (1 worker + 1 gatekeeper + inherited orchestrator/scoper) + the **live org overview**
  + an **agents demo** that lights the org and the state machine in sync. **No wiring** —
  nothing actually runs yet.
- **Later (wiring pass):** read `agents.yaml`/`procedures.yaml` in `state.py`; the architect
  agent; the orchestrator extension to multi-step plans; dispatch from the control center.
