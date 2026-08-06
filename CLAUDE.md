# Atlas — what it is, and the rules it is built under

This file is the contract between the operator and any agent working in this repo. It is short
on purpose: a document nobody rereads is not a contract.

---

## What Atlas is

An **orchestration substrate**. It watches a set of projects, turns a request into a scoped
contract, dispatches the work to a coding agent in an isolated worktree, runs that project's
hard gate, has a reviewer judge the diff against the contract and the **real** test result, and
then stops and waits for a human to merge.

It is a **control plane**. The projects it dispatches to live in their own repos, referenced by
path from `registry.yaml` — Atlas never contains the work it orchestrates.

There are exactly **two human gates** in a normal job: confirming the contract before the work
starts, and merging after the gate passes. Everything between them is machinery.

---

## The north star

**Autonomy by verifiability.** A task earns unattended execution in proportion to how well
something *other than a model* can confirm the result. That is the whole design, and every rule
below is downstream of it.

The full framework is `docs/automation-patterns.md`; the live classification of what actually
runs, and at which rung, is `automation-map.yaml`.

1. **Never use an LLM for what code can do reliably.** Deterministic is cheaper, faster, and
   cannot lie. A capability that can be a lookup should be a lookup.
2. **Engineer the validator, not the automation.** The loop, the prompt, and the fan-out are
   delivery mechanisms. The validation layer is the object of design.
3. **Trust comes from independent, uncorrelated validators — at least one deterministic.** Two
   instances of the same model share the same blind spots, so "LLM checks LLM" buys much less
   than the head-count suggests.
4. **The human gates every irreversible or outward-facing step.** Merges, sends, publishes,
   payments. Automation may prepare them; it never executes them.
5. **Model-agnostic by design, locally sovereign where possible.** Changing an agent's model, or
   its whole engine, is a config change and never a code edit. See `docs/model-agnosticism.md`.
6. **Thin slice, used daily.** Build the narrowest version that can actually be used, then use
   it. Breadth that nobody runs is indistinguishable from breadth that does not work.
7. **Hardware and dependencies are bought at the point of need, not ahead of it.** A demonstrated
   ceiling is a reason; an anticipated one is not.

---

## Rules for working in this repo

- **Inputs vs outputs.** A field a human decides (a next target, a judgment call) is an INPUT and
  is never written by an automated hook. A field derived from what happened (progress, status
  rollups, completion) is an OUTPUT and is never hand-edited. Mixing them is how a file stops
  being trustworthy.
- **Be suspicious of config.** Before trusting any field in a YAML file, grep for a consumer.
  A setting that looks live and is read nowhere is worse than no setting, because it invites
  people to tune it and conclude the system is broken when nothing changes.
- **Comments carry the reasoning, not the mechanics.** The code says what it does. The comment
  says why this threshold, why not the obvious alternative, and what broke when it was tried the
  other way. Those comments are the project's memory.
- **A gate that only passes on one machine is not a gate.** Test commands run in a fresh worktree
  with no secrets and no network; anything requiring either is marked and skips by default.
- **Tests are the deliverable for anything that cannot be used yet.** If a feature ships before
  it can be exercised by hand, its tests are the only thing standing between it and rot.

---

## Layout

| Path | What it is |
|---|---|
| `dashboard/` | The engine — dispatcher, worker, gate, planner, brain, scheduler, substrate |
| `docs/` | Design records. `automation-patterns.md` is the governing one |
| `registry.yaml` | The projects Atlas watches, and each one's gate |
| `automation-map.yaml` | Atlas's self-model: every capability, its rung, its validator |
| `agents.yaml` | Brain-level agents + the templates each project's org is instantiated from |
| `procedures.yaml` | The model and effort each KIND of session runs at |
| `schedule.yaml` | Availability + the placements the scheduling assistant proposes |
| `umbrellas/` | Agents shared across the sub-projects of one umbrella |

The projects in `registry.yaml` are a **fictional worked example**. The real registry this repo
was extracted from is private.
