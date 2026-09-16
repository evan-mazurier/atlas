# The ledger

> Design of record for `dashboard/ledger.py` and `dashboard/ledger_report.py`. Written
> 2026-08-25, before the first line of the module; the module has followed it since. This is §5
> of a larger design (the monitoring layer) that is still being measured and is not in this
> snapshot — the ledger is the part that stands alone.

## 1. Why

Three separate needs, one artefact:

1. **Safety net for unattended work.** Once the monitor merges without the operator, a *feeling*
   that it is going well is not good enough. Revert rate, rework counts and reviewer
   rejection rate are the instruments that say when to widen or narrow autonomy.
2. **The token question.** "Atlas burns a lot of tokens" is currently untestable. Made
   measurable, every efficiency change becomes a before/after instead of a hope.
3. **The publishable artefact.** This whole system is the author's applied experiment in agentic
   engineering. *"I ran N agentic coding jobs on real client and product work; here is what
   the review loop actually caught, what it cost, and how often the human was needed"* is
   the only credible proof of work for this kind of system. It cannot be written retroactively.

## 2. The storage rule — events only, statistics derived

One append-only file, `ledger/events.jsonl`. One line per thing that happened. Never
edited, never pruned.

**Statistics are computed on read and never stored.** This is `CLAUDE.md`'s inputs-vs-outputs
rule: a stored summary is a second source of truth that drifts. If a chart is slow, cache
the *result* — a cache is disposable and rebuildable, so it is not a second truth.

The practical payoff is twofold. A metric nobody thought of today is answerable from July's
data in December. And a formula found to be wrong is fixed once, correcting every chart
retroactively.

## 3. What is recorded

| Event | Carries |
| --- | --- |
| `started` | project, task, plan task, session type, model, effort, **`gate_contract` / `gate_merge`**, lane |
| `queued` / `dequeued` | capacity pressure — waiting on a lane is neither work nor waiting on the operator |
| `state` | scoping → coding → testing → review → reworking → ready → merged / **failed / stopped / discarded** |
| `test` | pass/fail, which attempt, duration |
| `review` | verdict, which pass, the reviewer's notes |
| `escalation` | **the question text**, what triggered it, tags (e.g. `environmental`, `manufactured`) |
| `escalation_answered` | **by `monitor` or the operator**, the answer, how long it waited |
| `gate` | which gate, shown / approved / redirected, by whom, how long it waited |
| `merged` | commit, diffstat |
| `revert` | the job undone, automatic or deliberate, **and a one-line reason** |
| `phase_tokens` | **on every phase**: fresh input, output, cache read, cache write |
| `finished` | totals, wall clock |

**Every event carries its lane.** A fanned job runs several lanes with their own tests,
reviewers and rework; without a lane identifier it collapses into one blurred row.

Two fields are new information Atlas does not keep today — **who answered an escalation**
and **how long it waited**. They measure the thing the monitoring layer exists to change.

Everything else already exists and is simply not written down: the worker counts tokens
after every call (`_add_tokens`), records `created`/`started` (queue time), and records
terminal states. The ledger mostly stops throwing this away.

### Worked example — one job

A content pass of 2026-08-25 on a knowledge-base site, abbreviated. The *shape* is the real
record; the project is aliased and the text fields are paraphrased, which is exactly what §9
requires of anything that leaves the ledger:

```jsonl
{"t":"09:24:49","job":"0649","ev":"started","project":"orchard-site","task":"copy-pass","type":"content","model":"sonnet","effort":"high","gate_contract":true,"gate_merge":true,"lane":1}
{"t":"09:26:10","job":"0649","ev":"gate","which":"contract","action":"approved","by":"operator","waited_s":78}
{"t":"09:27:03","job":"0649","ev":"escalation","q":"npm ci fails: EPERM unlink esbuild.exe. Is this my change?","trigger":"test-failure"}
{"t":"09:29:41","job":"0649","ev":"escalation_answered","by":"monitor","waited_s":158,"tags":["environmental","manufactured"]}
{"t":"09:33:20","job":"0649","ev":"test","result":"pass","attempt":1,"duration_s":125}
{"t":"09:35:02","job":"0649","ev":"review","verdict":"needs-changes","pass":1,"notes":"3 headings at 3.17:1; two are regressions from 5.50:1"}
{"t":"09:54:11","job":"0649","ev":"review","verdict":"commit-ready","pass":2}
{"t":"10:12:44","job":"0649","ev":"gate","which":"merge","action":"approved","by":"operator","waited_s":1064}
{"t":"10:12:50","job":"0649","ev":"merged","commit":"a1b2c3d","files":5,"insertions":312,"deletions":88}
```

Facts only. Everything below falls out of them without being stored: 48 minutes total, of
which **19 were the machine waiting on the operator** (40% of the job); one escalation, handled by
the monitor; the reviewer **rejected on the first pass**, with its reason; one rework; one
escalation tagged `manufactured`.

## 4. The statistics

Grouped by the question each answers.

**"Am I still the bottleneck?"** — the headline arc
- Human interventions per week *(should fall; this is the story of the rework)*
- Share of escalations answered by the monitor vs by the operator
- Share of jobs that ran dispatch → merge with zero human touch
- **Hours per week the machine spent blocked waiting on the operator** — invisible today, probably large

**"Is the reviewer real, or does it always say yes?"**
- First-pass rejection rate
- Rework distribution — jobs needing 0, 1, 2, 3+ passes
- Test-gate first-pass failure rate
- **Manufactured-failure rate** — escalations the system caused itself (a broken sandbox, a stale
  dependency) rather than the task; should trend to zero
- **Revert rate, split by gate** (§5)

**"Where are the tokens going?"** (§6)
- Burn against the rolling 5-hour cap; weekly allowance used across twelve windows
- Tokens per hour of work block
- Waste ratios: rework share, manufactured share, no-output-phase share
- Cache read ratio (higher is better)
- Tokens per merged task, **bucketed by estimate, segmented by session type**

**"How fast?"**
- Wall clock split three ways: machine time · blocked on the operator · queued on capacity

## 5. Reverts

**What it is for.** Once the monitor merges work the operator never saw, "are the merges any good?"
needs an answer that is *external to the system judging itself*. Tests passing does not
answer it — tests pass on the wrong feature. The reviewer approving does not either; it is
part of the same machine. The honest signal is whether merged work later had to be undone.

**Caught two ways.** Automatically, by noticing a revert commit or code removed again
shortly after, linked back to the job that produced it. And **deliberately**, when the operator or
the monitor undoes something on purpose — the more valuable of the two, because it captures
*built correctly, but the wrong thing*, which automatic detection cannot see. Every
deliberate revert carries a one-line reason.

**Tracked on every job, but read split by gate:**

| | |
| --- | --- |
| Revert rate on work **the operator approved** | the human baseline |
| Revert rate on **ungated** work | the number that licenses more autonomy |

The comparison is the point. If ungated work reverts no more often than work the operator
personally approved, **that approval was not adding safety** — it was costing time for
nothing, and the ungated category widens on evidence rather than nerve. If ungated is
clearly worse, the classification is too loose.

**Two causes, distinguishable, different fixes:**

- Work that **should have been gated and was not** → the **classification** is wrong — the task was ungated by rule and should not have been
- Work **rightly ungated that still came out wrong** → the **contract** was too vague; the
  monitor's briefing is wrong

**Rare and slow.** No meaningful number for weeks. Shown as a running count with its sample
size beside it, never as a percentage of a handful of jobs. If reverts practically never
happen, that is the single strongest number in the ledger — and the most credible line in
anything published.

## 6. Tokens — the real objective

**Money is not the metric.** The models run on a flat subscription with usage ceilings, not on
API billing. `cost_usd` is a footnote; it measures nothing actionable.

**The two ceilings** are a **5-hour rolling window** and a **weekly total**. A working week
is a fixed number of windows — twelve, at the author's cadence — and that many full windows
should sum to the weekly cap.

> **The goal, stated precisely because it is counterintuitive:**
> spend **more** tokens per week — fill the allowance — and **fewer** tokens per merged
> task. Efficiency is not spending less; it is getting more work out of a fixed ceiling.

**Which figures.** Atlas already made the right split and the ledger must not invent a
second accounting (`_add_tokens`, `dashboard/usage.py`):

- **fresh input + output** — the subscription-relevant headline
- **cache read + cache write** — tracked separately. Re-sent context dwarfs the real number
  10–80×; mixing them makes every chart meaningless. A **high cache-read ratio is good** —
  context reused rather than re-sent.

All four are recorded **per phase**, which is what turns "we burn a lot of tokens" into
levers. The worker already counts them at every call; it currently sums them away.

**Two clocks, never summed.** Burn is measured against both, because they answer different
questions and neither describes the other:

- The **rolling 5-hour window** is a *constraint* — "am I about to be cut off?" It runs on
  Claude's clock and ignores the schedule entirely. Read as: peak usage against the cap, and
  when it was reached.
- The **work block** (`schedule.yaml`) is *productivity* — "what did this morning cost per
  hour?" It runs on the operator's clock and blocks vary in length, so it is reported **per hour of
  block**, never per block.

A window trailing from the morning into the afternoon block is expected and harmless — the
two figures are never added together.

So the chart is a **continuous timeline** of burn with the cap line drawn on it and the work
blocks marked underneath. No bucketing into either clock, and the mismatch disappears.

It shows three distinct failure modes: hitting the cap early (work stalls for hours — spread
it or spend less per job), finishing windows well under the cap (unused capacity — dispatch
more in parallel), and the weekly cap binding before twelve windows are used (daily spend too
high to sustain the week).

**Comparability — why raw tokens-per-task misleads.** A two-hour refactor and a ten-minute
bug fix share no meaningful denominator. Three rules:

1. **Measure waste, not productivity.** Waste is a *ratio*, and ratios compare across any
   task size: rework share, manufactured-failure share, no-output-phase share, cache-read
   ratio (`cache_read ÷ (cache_read + fresh_input)`). Both a refactor and a bug fix can be
   15% wasted or 60% wasted, and that comparison is fair. **These are the levers**, and a
   ratio cut on small tasks translates into a large absolute saving when the same principle
   holds on large ones.

   **But transfer is verified, never assumed.** Large tasks have a different waste profile —
   more rework passes, more context re-reading. So a change is read per bucket:

   | | Before | After |
   | --- | --- | --- |
   | Small — waste % | 22% | 14% |
   | Large — waste % | 31% | **30%** |

   That is a failed transfer, visible only because of the split. Needs roughly ten jobs per
   bucket to believe — about a week at current volume.

2. **For absolute numbers, bucket by size — derived from the estimate, never a label.**
   `plan.yaml` already carries an hour estimate on every task; the bucket is computed from
   it: **small ≤ 1h · medium 1–3h · large > 3h**. The operator sets an estimate, as they already do,
   and never sets a bucket — so if the thresholds prove wrong, the thresholds change and no
   data needs re-labelling.

   The typical token cost per bucket is **learned from the data**, not declared, and becomes
   the baseline. *"Medium tasks used to cost 200k, now 140k"* is a fair claim; *"average
   tokens per task fell"* is not.

   **Side effect worth having:** once a baseline exists, the monitor can flag a *running* job
   at 3× its bucket's norm — an early warning that it is going in circles, while it is still
   cheap to stop.

3. **Never average across session types. Across projects, combine — then check the split.**
   Content, engineering and research have structurally different costs; always segment by
   session type. Across *projects* within one type, combine by default (per-project-per-type
   buckets are too small to say anything at current volume) and split once a project has the
   volume.

   **The mix trap.** Tokens per implementation task falling from 180k to 140k looks like a
   22% improvement — until the split shows week 2 was 15 cheap small-site jobs and 3
   expensive big-repo ones, against 5 and 10 the week before. Nothing got more efficient;
   the *mix* changed. **A combined number can suggest a conclusion; only the split can
   confirm it.** Any claimed improvement is checked against the split before it is believed.

> **Guardrail — efficiency is never read alone.** Tokens-per-task can always be driven down
> by doing worse work: a cheaper model, lower effort, a shallower reviewer. So every
> efficiency chart sits beside rework rate, reviewer rejection rate and revert rate. A
> change that cuts tokens 30% and raises rework 50% has *cost* tokens, not saved them — the
> rework spends them straight back, for worse output. This pairing is also what makes the
> north-star substrate swap (`docs/model-agnosticism.md`) testable on evidence.

## 7. The panel

Six charts, no more, plus a plain table of recent jobs.

1. Human interventions per week — *the arc*
2. Escalations: monitor vs the operator, over time
3. Reviewer verdicts: approved first pass vs rejected
4. Rework distribution
5. Token burn timeline against the rolling cap, work blocks marked underneath
6. Wall clock split: machine · blocked on the operator · queued

The statistics and the panel are the same thing — the statistics are the numbers, the panel
is where they are looked at. They are designed separately only because *what to measure* is
the real work and *how to draw it* is presentation.

## 7b. The ledger reader — a monitor capability, not an agent

The ledger will hold more data than is readable by eye, and the operator is the one engineering
Atlas: they need the data turned into decisions, not charts to interpret alone.

That is a **capability of the monitor**, not a new agent. It is judgment applied to data the monitor already owns, and adding a "ledger analyst"
agent would recreate exactly the sprawl the monitoring layer was built to remove.

It produces a **weekly report card**, plus on demand: what changed, what it cost, what
improved, what got worse, and **one** recommendation. Same card shape as every other decision card, same register.

Two rules are properties of the report template, not habits:

- **No efficiency number without the quality numbers beside it.** Rework rate, reviewer
  rejection rate and revert rate accompany every claim about tokens. This is what prevents
  optimising tokens into worse output without noticing.
- **No claimed improvement without the split checked** (§6 rule 3). A change in project
  mix must never be reported as an efficiency gain.

## 8. Backfill

The worker's log holds 81 jobs and 118 escalations back to 2026-07-01. Imported as a
clearly-marked partial period, because the honesty matters more than the coverage:

| Period | What exists |
| --- | --- |
| **Jul 1 – Aug 25** | Timings, state transitions, rework counts, escalation *counts*. **No question text, no tokens, no verdicts** — deleted by `ATLAS_KEEP=10`. |
| **Last ~10 jobs** | The above, plus verdicts, tokens, cost |
| **From the ledger onward** | Everything |

Every chart draws a **visible switch line** at the ledger's start date, and a series that
does not exist before it **begins there** rather than being drawn as zero. A chart implying
"no escalations in July" because none were recorded would be worse than no chart.

> Both gaps exist because `ATLAS_KEEP=10` deletes all but the ten newest finished records,
> and because the `question` field is overwritten by the answer. The monitoring layer's design had
> to be reconstructed from five recoverable cases out of 118. **Atlas has been destroying exactly
> the evidence it now needs.**

## 9. Publication

The export is a generated report — charts, numbers, and a short written method,
regenerated from the ledger so it is never stale.

**The ledger contains client material**: task text, escalation questions, reviewer notes.
One surviving escalation names a client's production database. That cannot be published.

So the public export is a **separate, redacted view**: projects aliased, and **numbers
only — never text of any kind**. No task descriptions, no questions, no reviewer notes, no
project or client names.

This is a **hard property of the exporter, not a filter to remember**: the public export
path can only emit counts, rates, costs and timings. It has no code path that can emit a
string from the ledger.

The statistics are the interesting part regardless. *"It merged 200 changes without me and
2 were undone"* is more credible than any brief.

---
