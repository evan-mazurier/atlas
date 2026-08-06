# Model-agnosticism — the substrate layer

> Design of record. Extends the north star in `CLAUDE.md` ("model-agnostic by design, locally
> sovereign by destination"). Written from a full read-only investigation of every agent's model
> wiring (2026-07-19). The goal: make changing **any** agent's model — or its whole engine — a
> config change, never a code edit, and keep the local-first option one flip away.

---

## 1. Current state (what the investigation found)

Atlas runs **11 distinct model-invoking agents**, in two groups:

**Conversational / advisory** (run inside the control center process):

| Agent | Substrate | Model | Set by |
| --- | --- | --- | --- |
| receptionist | Ollama (HTTP) | qwen2.5:3b-instruct | `ATLAS_MODEL` → agents.yaml → default |
| ask-Orchard | Ollama (HTTP) | qwen2.5:3b-instruct | shares the receptionist `MODEL` constant |
| ask-Almanac | Ollama (HTTP) | qwen2.5:3b-instruct | shares the receptionist `MODEL` constant |
| brainstormer | `claude` CLI | opus / high | `ATLAS_BRAIN_*` env |
| finance advisor | `claude` CLI | opus / medium | `ATLAS_ADVISOR_*` env |

**Dispatch / gate-engine / global** (run as dispatched `claude` CLI sessions):

| Agent | Model | Set by |
| --- | --- | --- |
| scoper | opus / high | hardcoded constant (`dispatcher.py`) |
| coder / worker | sonnet / medium (default) | `procedures.yaml` per session-type |
| reviewer / gatekeeper | sonnet / high | `ATLAS_REVIEW_*` / `procedures.yaml` |
| architect | opus / high | hardcoded constant |
| planner | opus / high | hardcoded constant |
| vault-keeper | opus / high | hardcoded constant |

### The problems this design fixes

1. **Four disjoint config mechanisms** — env vars, `procedures.yaml`, hardcoded constants in
   `dispatcher.py`, and the (mostly inert) `agents.yaml` fields. No single place to look or change.
2. **`agents.yaml` `substrate:`/`model:` is documentation, not wiring.** At runtime it is never read
   to select a model for a dispatched role. The only `substrate` field any code consults is the
   receptionist's, and only to decide whether to adopt its model value — never to pick the transport.
3. **No substrate abstraction.** The Ollama transport is hardcoded in `assistant.py`; the CLI binary
   is hardcoded in `worker.py`. The brainstormer/advisor have a one-branch seam (`ATLAS_*_SUBSTRATE`)
   but only `claude` is implemented. "Go local" is **not** a config change today.
4. **The receptionist's tool-calling** (Ollama function-calling for `focus`/`dispatch`) is the one
   genuine re-plumb blocker: no other engine offers that exact request/response contract through the
   subscription, so moving it needs a different tool mechanism (a parsed action-block).

---

## 2. The design — one seam: `dashboard/substrate.py`

A single module every conversational agent's model call goes through. (It lives in `dashboard/`
because `the control center's state layer` already puts that dir on `sys.path`, so the control center, the Streamlit app,
and tests can all import it.)

```
resolve(role) -> {substrate, model, effort}
    The single resolution point. Precedence: env override → central _DEFAULTS table → fallback.
    Every value is env-overridable as ATLAS_<ROLE>_SUBSTRATE / _MODEL / _EFFORT.

chat(role, messages, ...) -> {ok, text, error, substrate, model}
    Dispatches to the adapter for role's substrate. `messages` is OpenAI-style [{role, content}].
      _ollama_chat  — the existing HTTP path (native tools later).
      _claude_chat  — subprocess `claude -p`; prose today; tools via a parsed ```action block (Phase 2).
    Adapters share one normalized result shape, so the caller is substrate-blind.
```

The interface is deliberately shaped so `_anthropic_api` (native tool-use, metered) and `_llamacpp`
(the future local rig) slot in as new adapters with **no** change to callers — but those are **not**
built until point-of-need (north-star rule).

`_DEFAULTS` is the **single central table** where a conversational agent's substrate/model/effort
lives — replacing the scattered constants. Wiring `resolve()` to additionally read the distributed
`agents.yaml` files (so those fields stop being inert) is a clean follow-on, but the central table +
full env-overridability already delivers "one place, all swappable."

---

## 3. Rollout — additive, per-agent, gated, reversible

The prime directive is **don't break what works.** Each phase ships independently; defaults preserve
today's behavior; every agent can be flipped back with a config value; the Ollama adapter stays wired
(so local-first is always one flip away).

- **Phase 0 — the seam, zero behavior change.** Introduce `substrate.py`; route the vault agents
  (`ask_vault`) through it with `resolve → ollama/qwen` defaults. Pure refactor — same transport,
  same model, tests green. Proves the seam on the lowest-risk agents (no tool-calling). *The
  receptionist's tool-calling path is left untouched here — it moves in Phase 2.*
- **Phase 1 — flip the easy two.** `ask-Orchard` + `ask-Almanac` defaults → `claude`/`haiku`. They now
  answer through the CLI adapter. Immediate quality lift on vault recall, and they finally have their
  own model knob (no longer chained to the receptionist). Gate: golden before/after on a question set
  + a live check.
- **Phase 2 — the receptionist (the only real work).** Implement the action-block tool path in
  `_claude_chat` (prose + a fenced `action` block parsed back into `focus`/`clear`/`dispatch`), route
  the receptionist through it, flip → `haiku`. Gate: verify actions still fire; live check. Its own
  slice.
- **Phase 3 — optional, light touch.** Promote the hardcoded dispatch constants (`SCOPE/ARCH/
  PLANNER_MODEL`) to env-overridable. One line each. **The Stage-4 gate pipeline itself is not
  restructured** — only its constants are exposed.

### Reversibility

`ATLAS_ASK_ORCHARD_SUBSTRATE=ollama` (etc.) instantly returns any agent to local. When better hardware
lands, the whole control plane migrates back to a local model role-by-role via config — exactly the
north-star endgame, with zero code change.

---

## 4. The model choice — Haiku for the ex-qwen agents

The three qwen agents move to **Claude Haiku 4.5** on the subscription (near-zero cost). Haiku is the
right *tier* for fast front-door / grounded-recall work, and — unlike qwen2.5:3b — it reliably follows
format and reasoning instructions (the qwen ceiling that produced the "no next target" slip and the
vault hedging). Deeper synthesis remains the brainstormer's job (Opus). Tradeoff: a CLI subprocess
adds ~2–4 s/call vs instant Ollama — acceptable for these, including voice. All reversible via config.

---

## 5. Non-goals / out of scope

- **No restructuring of the Stage-4 worker/dispatcher.** It is proven and load-bearing; expose its
  constants (Phase 3), never rip out the mechanism.
- **No speculative adapters.** `_anthropic_api` and `_llamacpp` are designed-for, not built, until a
  concrete need (a metered-API decision, or the local rig).
- **No new config file.** The central `_DEFAULTS` table + env overrides is the source of truth;
  reading the existing `agents.yaml` fields into `resolve()` is a documented later enhancement.

---

## 6. Status

- **Phase 0 — built** (`atlas/model-agnostic`): `substrate.py` seam + `ask_vault` routed through it,
  behavior-preserving (ollama defaults). Live-verified.
- **Phase 1 — built**: `ask-Orchard`/`ask-Almanac` → Haiku via config. Live-verified (grounded recall +
  correct refusal on absent evidence).
- **Phase 2 — built**: the receptionist → Haiku, with tool-calling carried across substrates (native
  on Ollama, a parsed `action` block on the CLI). Live-verified: the old "no next target" slip is gone,
  KPI answers are accurate, and both `focus` and free-form `dispatch` actions fire (after strengthening
  the action-protocol so the model emits the block rather than narrating it). `_flatten_for_cli` made
  argv-safe as part of this. 21/21 substrate tests green. **All three conversational front-door agents
  now run on Haiku, each one env-flip from local.**
- **Phase 3 — built**: the scope/architect/planner constants are now env-overridable
  (`ATLAS_SCOPE_MODEL`/`_EFFORT`, `ATLAS_ARCH_*`, `ATLAS_PLANNER_*`), matching the reviewer. **Every
  single-model agent's model is now a config value, not a code edit.** (Worker model stays
  per-session-type in `procedures.yaml`, by design — a worker has no single fixed model.)
- **Display**: the roster overlays the live-resolved model for the seam agents *and* every fixed-role
  dispatch agent (scoper/architect/planner/vault-keeper/gatekeeper → their dispatcher constant), so those
  cards always show what actually runs, even after an env override. Only the **workers** stay nominal —
  their model is per-session-type (`procedures.yaml`), so no single card number is truthful, by design.
- **Reviewer → Opus** (2026-07-19): the gate reviewer is a judgment role (catches what tests can't), so
  it now runs Opus/high like the scoper/architect, not Sonnet.
- **Brainstormer + advisor** — already env-configurable; fold them onto the shared `chat()` seam
  opportunistically once `atlas/brainstormer` merges (trivial — they already match the CLI adapter).

## 7. Final state — how to change any agent's model

| Agent(s) | Change by | Effort |
| --- | --- | --- |
| receptionist, ask-Orchard, ask-Almanac | `substrate.py` `_DEFAULTS`, or `ATLAS_<ROLE>_SUBSTRATE/_MODEL/_EFFORT` | config |
| brainstormer, finance advisor | `ATLAS_BRAIN_*` / `ATLAS_ADVISOR_*` | config |
| scoper, architect, planner, reviewer | `ATLAS_SCOPE_*` / `ATLAS_ARCH_*` / `ATLAS_PLANNER_*` / `ATLAS_REVIEW_*` (or `procedures.yaml` reviewer key) | config |
| coder / worker (per session-type) | `procedures.yaml` | config |

Going local later: for any conversational agent, `ATLAS_<ROLE>_SUBSTRATE=ollama ATLAS_<ROLE>_MODEL=<local>`
— both adapters are wired, so it is a genuine flip with no code change.

## 8. Current model assignments (2026-07-19)

**Philosophy:** Opus *thinks* (scope) and *checks* (review) and does *unverifiable-judgment* work; Sonnet
*builds*; Haiku does *chores*.

**Fixed-role agents:** receptionist / ask-Orchard / ask-Almanac = **Haiku**; brainstormer = **Opus**/high;
finance advisor = **Opus**/medium; scoper / architect / planner / vault-keeper = **Opus**/high; gate
reviewer = **Opus**/high.

**Worker, by session-type** (`procedures.yaml`, the one knob you own):

| Category | Model | Effort |
| --- | --- | --- |
| default (unlisted) | Sonnet | medium |
| orchard-api · implementation | Sonnet | high |
| orchard-site · content (science) | Opus | high |
| almanac · research | Opus | high |
| orchard-journal · content | Sonnet | high |
| orchard-site · engineering | Sonnet | medium |
| almanac · ops | Sonnet | medium |
| almanac · ingest | Haiku | low |

Free-form dispatch with "scope it first" → the build inherits **Opus**; without it, an unknown type → the
Sonnet default.
