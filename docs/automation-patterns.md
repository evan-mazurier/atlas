# Atlas — Automation Patterns (the pattern ladder & how to choose)

> **STATUS: LOCKED — signed off by the operator 2026-07-05 (all §8 calls resolved).** Scoped with the operator 2026-07-03.
> The **governing framework** for how Atlas automates anything. Every capability Atlas takes on MUST be
> placed on the ladder below, choosing the **cheapest rung that clears its trust bar**. This is how Atlas
> compounds over time *without* losing trust or burning tokens. Companion to
> `agentic-structure.md` (agents & procedures) and the core principle *autonomy-by-verifiability*.

---

## 1. Why this document exists

Atlas is built by **compounding** — every new thing (a newly wired project, a finance dashboard, an
email agent that reads and answers your mail) is, structurally, **a new automation**. Without a shared rule
for *"how much machinery does this actually need,"* we fail one of two ways:

- **Over-engineer** — wrap a simple task in a self-prompting loop → token burn, and *false* trust (a loop
  with no real validator looks rigorous but converges on nothing).
- **Under-engineer** — fire a one-shot LLM at something that needed verification → silent, unnoticed errors.

This document is the **classifier that prevents both.** Before building any capability, you place it on the
ladder; the choice is forced and explicit. It is the operational expression of Atlas's core principle:
**automate aggressively where the system can verify itself; keep the human in the loop only where it can't
— and only then.**

---

## 2. The two questions that decide everything

Every automation decision reduces to two questions, asked in order:

1. **Does it need an LLM at all?** If a computation, lookup, or rule can do it → it is **rung 0
   (deterministic)**. *Non-negotiable: never use an LLM for what code can do reliably.*
2. **Given it needs an LLM — what is its validation metric, and how trustworthy & cheap is that metric?**
   This single question picks the rung. **The clearer and more deterministic the validator, the cheaper the
   rung you can safely use.**

> **Central thesis: you do not engineer the automation — you engineer its _validator_.** The loop, the
> prompt, the fan-out are just delivery mechanisms. The **validation layer** is the object of design;
> everything else follows from it.

---

## 3. The validation layer (the spine — read before the ladder)

Every automation above rung 0 **must declare a validation metric**: the concrete signal that says *"this
output is acceptable."* There are two kinds, and the whole game is to get as close to the first as possible.

- **Deterministic oracle** — code decides pass/fail: tests pass, a returned number equals the expected, a
  schema validates, a diff applies, output is within a numeric tolerance. **Never lies.** The gold standard.
- **LLM judge** — a model decides acceptability where no deterministic check exists (*is this well-written?
  is this design sound?*). **Fallible.** Use only when forced, and shore it up (see the trust rule).

**Design goal for every task: push the validation metric as far toward _deterministic_ as possible.** A task
that looks un-verifiable often has a deterministic proxy hiding in it — it compiles, it passes N golden
tests, it matches a reference output, it stays within tolerance. **Finding that proxy _is_ the engineering.**
When you genuinely can't find one, that is itself the signal that the task belongs closer to the human.

**Trust rule (why a good loop can beat a human, and where it can't):** trust comes from **independent,
uncorrelated validators, at least one of them deterministic.** Two instances of the *same* model fail the
*same way* — correlated blind spots — so stacking "LLM checks LLM" buys far less than it appears. The reason
Atlas's gate engine is trustworthy is that the **deterministic test gate is independent of the model**; the
LLM reviewer only judges the residue tests can't capture. Independence is the source of trust, not head-count.

**Human-legibility rule ("stay the engineer"):** even a fully automated loop must keep its reasoning and
decision points **inspectable**, and keep any **irreversible or outward-facing action gated to the operator** (merge,
send, pay). Verifiability is not only for the machine to converge — it is for the **human to spot-audit**. The
more reliable a loop becomes, the greater the temptation to stop reading its escalations; that is exactly when
a correlated error slips through. Build the loop like someone who intends to stay the engineer.

---

## 4. The ladder — choose the leftmost rung that clears the trust bar

| Rung | Pattern | Definition | Validation layer | Choose when | Token cost |
| --- | --- | --- | --- | --- | --- |
| **0** | **Deterministic** | Code, no LLM. | The code's own correctness / unit tests. | No judgment or natural language is required — it's a calculation, lookup, or rule. | ~0 (no LLM) |
| **1** | **One-shot prompt** | A single LLM call, output used directly. | None / implicit (output is self-evident or the cost of a mistake is trivial). | One pass is reliable **and** a wrong answer is cheap and recoverable. | 1 generation |
| **1.5** | **One-shot + verify** | A single call, then a **deterministic check** accepts it or **hard-escalates** to the human. **No iteration.** | A deterministic oracle, applied once. | One pass usually works, but a wrong answer is **costly** — you want a safety net without paying to iterate. | 1 gen + 1 check |
| **2** | **Loop (iterate-against-validator)** | The model iterates, re-checked each round, until the validator passes or an **iteration cap** is hit → then escalate. | A validator good enough to **score progress and define "done"** — deterministic preferred; an LLM judge only with its trust shored up. | One pass genuinely can't do it **and** a validator exists that can close the loop. | N generations (bounded by the validator + the cap) |
| **2.5** | **Fan-out + select** | N **independent** attempts generated in parallel; a validator/comparator picks the best. | A comparator or judge that can **rank** candidates. | The solution space is **wide** and any single trajectory is unreliable (design, scoping, naming). | N in parallel (breadth, not depth) |
| **—** | **Never-automate** *(boundary, off-ladder)* | The human does the action. Automation may **prepare** (draft, compute, summarize) but never **execute**. | The human. | The action is **irreversible / outward-facing / financial**: money movement, client comms, trades. | n/a |

**The rule in one line:** *pick the leftmost rung that clears the trust bar for this task; declare its
validation metric explicitly; prefer deterministic validators; keep the human legible-in and gating the
irreversible step.*

**Rung 2.5 vs rung 2 (they're easy to confuse):** rung 2 refines *one* trajectory over several rounds
(**depth**); rung 2.5 generates *N independent* trajectories in parallel and keeps the best (**breadth**).
Reach for 2.5 only when the solution space is **wide** and any single attempt is likely mediocre — design,
architecture, scoping, naming. *Example:* scoping a genuinely new capability (e.g. the finance dashboard) by
fanning out 3–5 independent scopes from different angles (read-only-first, feed-first, risk-first), then a
judge ranks them and you synthesize from the winner. It is the **most token-expensive rung** — you pay N
generations up-front with no iteration to amortize them — so reserve it for wide, high-value decisions where
one pass won't do and refining one direction can't save you.

---

## 5. The decision procedure (run this for any new capability)

1. **Can code do it alone?** → **rung 0.** Stop. (Never spend an LLM on it.)
2. **Does one pass reliably do it, and is a wrong answer cheap?** → **rung 1.**
3. **Is a wrong answer costly, but one pass usually right?** → **rung 1.5** (one call + a deterministic check
   that escalates on fail).
4. **Does it need iteration / exploration?** Then ask *what closes the loop:*
   - **Deterministic validator exists** → **rung 2**; cap iterations tight; escalate on cap.
   - **Only an LLM judge** → **rung 2**, but shore up the judge (independent lens, explicit rubric, human
     spot-audit) **or** reconsider whether a deterministic proxy exists after all. Keep the human gate close.
   - **No validator at all** → you **cannot** safely automate acceptance. Find a validator, or keep it human.
5. **Wide solution space, want the best of many?** → **rung 2.5** (fan-out + select).
6. **Irreversible / outward-facing / financial?** → **boundary.** The human executes, regardless of which
   rung was used to *prepare* the work.

---

## 6. Atlas today, classified (dogfooding the framework)

> **Live version:** this classification is maintained as data in **`automation-map.yaml`** (repo root) and
> rendered in the control center's right-sidebar ladder — Atlas's self-model. **The yaml is the single source of
> truth**: it carries the full **per-project profiles** (Orchard, Almanac, Finance) plus a reflexive *"this view =
> rung 0"* entry. The table below is a **curated teaching excerpt** of the core `atlas-core` capabilities —
> *not* an exhaustive mirror. When you add a capability, record it in `automation-map.yaml`; extend the table
> below only if it's a new *core* teaching case.

| Capability | Rung | Validation layer |
| --- | --- | --- |
| Progress %, at-risk color (§5), merge-advance hook, status/cost aggregation | **0** | Pure functions over data — no LLM by design |
| Voice command parse + the morning-briefing answers | **0** | Deterministic regex parse + deterministic read of the snapshot (the briefing does **not** use an LLM — this is the principle in action) |
| Receptionist Q&A over live status | **1** | Implicit; a wrong answer is cheap and the operator is right there |
| **Stage-4 gate engine** (scope → code → tests → reviewer → rework → auto-commit → merge) | **2** | **Deterministic test gate (floor)** + LLM reviewer (residue); iteration-capped; **human gates the merge** — the canonical rung-2 |
| Architect / scoper / planner proposals | **1.5-ish** | One proposal → **the operator approves/edits/revises** (human as the check); no autonomous iteration |
| *(none yet)* fan-out design/scoping exploration | **2.5** | Candidate for later — cheap to name now so future design work reaches for it |
| Merge approval, client comms, money, trades | **boundary** | Human executes; automation only prepares |

**Worked example — a live mis-rung and its cascade (2026-07-03).** The voice command *"launch the next
task"* was first built on the **receptionist LLM** (rung 1) to decide *which* task to launch. But
"the next task" is a **plan lookup** — pure rung 0. That single mis-rung caused three separate failures at
once: it launched the **wrong project** (the LLM guessed an empty one), it **dropped the structured dispatch
action** (so the form didn't pre-fill), and it **double-spoke** (the LLM path and the command path both
voiced it). Re-building it as a deterministic lookup (`_top_next_task` over `plan.yaml`) fixed **all three**
in one change. The lesson this doc exists to teach: **a mis-rung doesn't fail cleanly — it cascades, and the
fix is almost always "find the deterministic core and drop a rung."**

---

## 7. How this governs new capabilities (the compounding contract)

- **Every new capability's design doc states two things: which rung, and its validation metric.** No capability
  is scoped without answering "what's the validator." This is the discipline that lets Atlas compound while
  staying trustworthy.
- **Feature ideas are classified through this ladder as step 1 of scoping them** — the ladder is where a raw
  idea ("read and answer my emails") becomes a buildable plan (which parts are rung 0, where the validator is,
  what stays behind the never-automate boundary).
- **The architect agent owns this classification step.** Running §5 is the first move of scoping any new work,
  which is already the architect's job — so **no new agent is created for it.** The architect's charter is
  **widened to accept "a new Atlas capability" as a valid subject** (not only a downstream project being
  wired): building *inside* Atlas is Atlas wiring itself. The §5 procedure is baked into the architect's
  prompt / scoping procedure **as a link to this doc**, so there is one source of truth rather than a copy.
- **The visual (built next) renders this ladder as Atlas's self-model** — so the operator can always see, at a glance,
  how any part of Atlas is automated and *why* it sits where it does.

---

## 8. Sign-off — resolved (the operator, 2026-07-05)

All five judgment calls are **decided and locked**:

1. **Doc name** → **`automation-patterns`.** (Loop is only *one* rung; naming the whole framework after it
   would misframe it.)
2. **Rung 1.5 (one-shot + verify)** → **distinct rung.** Its cost and safety profile differ materially from
   rung 1 — it has a real deterministic gate; rung 1 doesn't.
3. **Rung 2.5 (fan-out)** → **kept**, though Atlas doesn't use it yet. Naming it is cheap and it steers future
   design/scoping work toward the right pattern (see the §4 clarification).
4. **Never-automate boundary + human-legibility rule** → **summarized here, owned by
   `autonomy-by-verifiability`** — cross-linked, not duplicated, so there is one source of truth.
5. **§6 classifications** → **confirmed as written** (incl. architect/planner as "1.5-ish").
