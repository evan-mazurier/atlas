# Atlas — The Brain (the personal knowledge layer)

> The design of record for Atlas's **private model of the operator** — his taste, preferences, decision
> patterns and constraints — so the system advises and acts the way he actually would.
> Scoped **2026-07-26**. Companion to `agentic-structure.md` (agents & procedures) and
> `planner.md` (planned work).
> **Slices 1+2 built 2026-08-02; slice 3 (`brain-keeper`) built 2026-08-03; all MERGED to `main`
> 2026-08-04** (31 commits, 659 tests green, branch deleted). §14 is slice 3's design of record and
> carries one CORRECTED finding (§14.3).

---

## 0. Where this stands (2026-08-04)

**Slices 1, 2 and 3 are live on `main`. The corpus holds 18 notes. Nothing is left to build until
the soak says retrieval has earned slice 4.**

All three harvest paths were proven live on merge day, not just unit-tested:

| Path | Evidence |
| --- | --- |
| **Reader 1** (pulls CC memory files) | 5 of 5 post-watermark files correctly **rejected** on the subject test, each with its own reasoning. No false positives. |
| **Reader 2** (pushes control center turns) | a first-person statement to the receptionist judged live → a well-shaped `decision-pattern` proposal → accepted after an edit (17 → 18 notes). |
| **Disjointness** (§14.10) | the memory file Atlas's own receptionist turn wrote was **skipped on origin** (28 CLI `session_id`s recorded), so the same fact was never judged twice. |

**The merge did not wait on the soak, and should not have been expected to.** The soak gates
**graduation** (slice 4 — agents that act unwatched), not integration: on `main` the Brain still
feeds the receptionist alone, exactly as it did on the branch, so merging graduated nothing.
Holding the branch only meant running the daily control center off a feature branch.

**What "waiting" now means, concretely:** the operator uses the receptionist normally and presses *wrong
note* / *should have known* when retrieval misses. At roughly 30 real questions (§8's criterion)
there is enough material to judge, and slice 4 begins with **one** agent.

**The readout lives in the Brain panel header, beside the note count** — `soak · 12/30 questions ·
9 with notes · 2 wrong · 1 missed`, turning accent-coloured once the question count passes the
reference figure. Every receptionist answer is counted, including those retrieval stayed silent on,
because a question it should have known about is exactly a *should have known* and excluding those
would flatter the rate. Only note names are stored, never the question text: the denominator needs
a count, and a judgement record already captures the question when something went wrong. The
threshold is a reference point the code never acts on — graduation stays a human call, and a number
that promoted itself would be the kind of silent decision this design exists to prevent.

**One quality lesson already worth carrying into slice 4** (found by the operator, not by a test): the
keeper wrote a rule about *his* gate as though it were the *agent's* — "report such work complete
only after it has been manually exercised". Injected as a hard rule, which is how `decision-pattern`
notes arrive (§7), that would stall every dispatch waiting on him. **Read a proposal's `Applies to:`
line as an instruction an agent will obey literally, and edit before accepting.**

---

## 1. The gap this closes

Atlas models **org** (`agents.yaml`), **state** (`status.yaml`), **planned work** (`plan.yaml`),
**time** (`schedule.yaml`) and **decisions** (the Orchard vault). Every one of those is about
**the work**. None of them is about **the operator**.

So every agent in Atlas starts cold on the question that matters most: *how does this operator
decide?* Taste, standing constraints and the reasoning behind a hundred past calls are nowhere the
system can read — they sit in the operator's head or in scattered agent memory files. Each new
session re-derives them, imperfectly, and arrives at a slightly different answer.

**The Brain closes that gap:** a curated, private, local corpus of durable facts *about the operator*,
queryable by any agent. It is the sovereignty rule applied to the most sensitive data the system
touches, which is why the whole layer is built around what a model is NOT allowed to do to it.

**What it buys:** an answer shaped by what Atlas knows about the operator rather than only about the
projects, and later a dispatched contract that honours a standing preference without anyone having to
restate it.

---

## 2. What belongs in the Brain — the subject test

The Brain is **not** a general knowledge store, a log, or a second vault. One test decides
membership:

> **If every project were cancelled tomorrow, would this still be true?**

| | Goes in | Example |
| --- | --- | --- |
| **Yes → the Brain** | facts about **the operator** | "Never commits generated build artifacts." "Prefers a rollback path over a forward fix." "Decides by shipping the smallest change first." |
| **No → existing stores** | facts about **the work** | "The multi-worker engine is live." → `status.yaml`. "Orchard reorg decision." → the Orchard vault. "Next slice is the phone PWA." → `plan.yaml`. |

Durability is *not* the test — a lot of project state is durable too. **Subject** is the test.

### Boundary with the existing stores

| Store | Subject | Written by | Shape |
| --- | --- | --- | --- |
| **The Brain** | **the operator** | the operator (+ gated proposals) | atomic facts, ~1 KB |
| the Orchard vault / Almanac vaults | company & project decisions | `vault-keeper`, Obsidian | documents, ~16 KB avg |
| `status.yaml` / `plan.yaml` / `schedule.yaml` | current state, planned work, time | work plane / planner | structured records |
| Claude Code session memory | per-project working context | the CC session | scratchpad, per project dir |

Session memory stays the **per-project scratchpad**. The Brain is the **operator model**. No
overlap — the subject test assigns every fact to exactly one home.

---

## 3. Guiding principles (the rules that make it safe)

1. **Curated, never a log.** Hundreds-to-low-thousands of distilled facts the operator would recognize if
   he read them — not transcripts mined later. A log gives you a search engine over your own
   noise; a curated corpus gives you something that can actually steer an agent.
2. **Files are the source of truth. Any index is derived, and deletable.** Same rule as the rest
   of Atlas.
3. **The write path is deterministic. A model never writes a file.** Models may *propose*; only
   code commits. This is structural, not behavioural — see §6.
4. **Atomic.** One fact per file, hard-capped (~150 words / ~1 KB). Retrieval injects whole notes;
   a long note is a bad retrieval unit. If it wants to be longer, it is two notes — or it belongs
   in a vault.
5. **Trust is earned per consumer.** Consumers the operator reads in real time come first; consumers that
   act unwatched are graduated later, one at a time (§8).
6. **Forget is first-class.** Deleting a note deletes it, everywhere, and the index rebuilds. This
   is personal data.
7. **Fully local, always.** No external service ever holds the corpus. No cloud fallback, no
   "just for embeddings" exception.

---

## 4. Where it lives

**`D:\work\brain`** — its own **private local git repo**, sibling of Atlas.

- **Outside the Atlas repo** so personal data never enters the codebase's git history.
- **Obsidian opens it directly** — markdown + frontmatter + `[[wikilinks]]`, same substrate as the
  existing vaults, so manual editing works on day one with no tooling.
- **Git gives history and undo** for free.
- **Not a project.** It has no `status.yaml`, no work, no gate, and must not render as a project
  card. It gets a **new top-level `brain:` key in `registry.yaml`**, *not* an entry under
  `projects:`.

---

## 5. The contract: a Brain note

The precedent is the **Claude Code memory-file format**, not the the Orchard vault document format — one
fact, one file, a one-line `description` used for relevance. Three fields are added.

```markdown
---
name: no-build-artifacts          # kebab-case slug; the filename
description: Never commit generated build artifacts to a shared branch
type: taste                          # taste | preference | decision-pattern | constraint | person | fact
scope: global                        # global | atlas | orchard | orchard-ops | ...
confidence: high                     # high | medium | low  — how sure we are this still holds
source: "session 2026-07-05"         # where it came from (session, dispatch, conversation)
created: 2026-07-05
reviewed: 2026-07-19                 # last re-confirmed; drives staleness
---

Native `<select>`, checkboxes and pickers render OS-white and can't be themed.

**Why:** they break the control center's visual system.
**Applies to:** any new UI surface. Related: [[jarvis-theme-decision]]
```

- `description` is the **retrieval key** in v1 (§7) — it must stand alone and be specific.
- `type` and `scope` are filters, not folders. **Flat directory**; the frontmatter is the index.
- `[[wikilinks]]` are a cheap relatedness signal — a retrieval hit may expand to its neighbours.
  They are *not* the primary access path (unlike the vaults, where the graph is the interface).
- **A validator enforces the schema and the size cap** on every write. Markdown's one real
  weakness is frontmatter drift; this is the fix, and it ships in slice 1.

---

## 6. Ingestion — deterministic writes, gated proposals

The single most important rule in this document:

> **Writing to the Brain is always deterministic code. A model never writes a file.**

Given `{name, description, body, type, scope}`, a plain function validates, enforces the cap,
writes the `.md`, and updates the index. There is no model in that path — ever.

Models appear in exactly two places, both producing **drafts the operator approves**:

| Path | Who decides the content | Who writes the file | Gate |
| --- | --- | --- | --- |
| **Direct capture** — the operator types it, or says *"Atlas, remember that…"* (existing STT) | the operator | deterministic writer | none needed — he authored it |
| **Harvest** — `brain-keeper` reads a merged dispatch / session and *proposes* a candidate fact | LLM drafts | **the same deterministic writer**, after acceptance | accept / edit / reject, per note |
| **Distillation help** — the operator dumps a messy paragraph, a model compresses it to atomic form | LLM suggests | **the same deterministic writer**, after acceptance | approve before write |

Consequence: the Brain **cannot silently fill with model-invented facts**. That property comes from
the architecture, not from the model behaving well.

**Slice 1 contains no model at all** — a capture form and voice capture, straight to disk.

---

## 7. Retrieval — description-matching first, vectors on a trigger

**v1 (ships with slice 2): description-matching, no embeddings.**
Load every note's one-line `description` (2,000 notes ≈ 200 KB), hand that index to the consuming
agent, and let it choose which notes to open in full. This is exactly how Claude Code's memory
system works today, it needs no embedding model, no vector store, and no RAM budget — which
matters on a 16 GB desktop with a known instability under load.

Important property: the **candidate set is deterministic** (every note is always visible to the
index pass); only the *selection* is model-driven. The Brain therefore cannot fail by inventing a
memory — worst case it surfaces the wrong real one, in front of the operator.

Two deterministic guards sit around that model-driven selection (`assistant.brain_evidence`), and
they are what keep the property above true in practice:

1. **An invented name is dropped.** The selection is returned as note *names*, and each is checked
   against the known set before anything is opened. A hallucinated name resolves to nothing rather
   than to a fabricated memory.
2. **A failed selection degrades to matching, not to nothing.** If the call errors, or returns
   `NONE`, or returns only names that do not exist, retrieval falls back to `brain.search()` —
   literal description-matching, no model. This is the only part of retrieval that is plain
   matching, and it exists because a receptionist with no Brain is worse than one with an
   approximate one. Note the consequence for the soak: a `wrong note` report may be scoring the
   fallback rather than the selection, so read the cited chips before concluding the selection pass
   is at fault.

### Retrieved notes inject BY TYPE, and that is the difference between cited and obeyed

Found during slice 2, and load-bearing enough to be part of the design rather than an
implementation detail. A retrieved note is **not** handed to the consuming agent as neutral
background:

| Type | Injected as | Because |
| --- | --- | --- |
| `taste`, `preference`, `constraint`, `decision-pattern` | **HARD RULES** that override the model's own habits | they exist precisely to change what it would otherwise do |
| `fact`, `person` (and unclassified) | context | they inform an answer; they do not direct it |

Fed as background, a style rule was **cited in the same reply that violated it**. The split fixed
it. Two consequences worth holding on to: a wrongly-scoped rule now produces a *false compliance
failure* that reads exactly like the model ignoring you (§0), and a proposal's `Applies to:` line
will be obeyed literally by an agent, so it must describe the agent's behaviour and not the operator's.

**v2 (deferred, and it needs a trigger, not a date): a local embedding index.**
Built only when retrieval visibly starts missing things — the corpus outgrowing the description
pass. Small local model (bge-small class, ~130 MB), index ~3 MB for 2,000 notes, **derived and
rebuildable from the files**. The `brain.search()` interface is identical either way, so nothing
built in v1 is wasted.

### On disk cost (measured, not estimated)

| Corpus | Files | Size | Avg/note |
| --- | --- | --- | --- |
| Claude Code session memory (today) | 31 | 216 KB | **7 KB** |
| Orchard vault (today) | 60 | 930 KB | **16 KB** |
| **Brain projected** | ~2,000 | **~2 MB** | **~1 KB** |

A mature Brain plus its vector index is **under 10 MB** — less than one phone photo. Disk is a
non-issue by orders of magnitude.

The measurement did surface a real constraint, and it is why the cap in §5 exists: the existing
memory notes average **7 KB**, far too fat to be retrieval units. Injecting five of those is 35 KB
of context for perhaps two relevant sentences.

---

## 8. Consumers and the trust ladder

The axis that matters is **not** "project work vs not" — that line is tool-shaped and dissolves as
The operator moves his interaction into Atlas. The durable line:

| | Consumers | Failure mode | Status |
| --- | --- | --- | --- |
| **Visible** — the operator reads the output in real time | the receptionist; the operator browsing the Brain | bad retrieval is **seen immediately**, nothing happened | **first, and alone during the soak** |
| **Autonomous** — acts while the operator is not watching | scoper, reviewer, planner, dispatched workers | bad retrieval becomes **a contract or a commit** | **graduated later, one agent at a time** |

**The soak.** After slice 2, the Brain feeds the receptionist and **nothing else**. The operator uses it
daily, asks it real questions, and watches what it surfaces. This is autonomy-by-verifiability
applied to the Brain itself: the receptionist is not the payoff, it is the **test harness for the
corpus**.

**Graduation criterion** (stated so "a while" doesn't drift into never): *over roughly 30 real
questions, retrieval surfaces the right notes, and the operator cannot recall the last time it pulled junk
or missed something obvious.* Not scientific — but checkable, so promotion is a decision rather
than a mood. Then autonomous consumers are enabled **one at a time**, each with the same standard.

**The soak gates this table's second row and nothing else.** It is not a gate on merging, shipping
or growing the corpus — all of which happened during it (§0). Confusing "we are soaking" with "hold
everything" cost the branch a week it did not need to spend. Before gating any work on the soak,
check that the soak actually changes that work's risk; here, only graduation qualifies.

**Two observations logged so far, neither yet acted on:**
- **Retrieval sees only the current message**, never the conversation history — so "ok, write it
  now" after five turns of discussion retrieves nothing. Deliberately not fixed mid-soak; the fix,
  if it bites in real use, is to fold the previous user turn into the selection query.
- **Selection is generous.** Five chips fired on a statement that asked nothing. Harmless in a
  visible consumer, much less so in an autonomous one — so if this repeats on real questions, it is
  a precondition for slice 4, not a cosmetic issue.

---

## 9. Forgetting, staleness, privacy

- **Delete = gone.** Removing a note removes it from the corpus and the derived index. No tombstone,
  no archive-by-default. Git history is the only recoverability, and the repo is local.
- **Staleness.** `reviewed` + `confidence` drive a periodic re-confirm pass: old, low-confidence
  beliefs get surfaced to the operator rather than quietly steering agents forever. People change; a
  personal model that only accumulates is wrong by construction.
- **Never leaves the machine.** No cloud sync, no telemetry, no external embedding API. When the
  phone slice (`scheduling-part2.md`) lands, the Brain travels over the Tailscale mesh as files —
  the same transport as everything else, still on the operator's own hardware.
- **Informs suggestions, never ungated irreversible actions** — the standing Atlas guardrail.

---

## 10. Build order

| Slice | What | Model involved? | Touches work plane? | Status |
| --- | --- | --- | --- | --- |
| **1** | `D:\work\brain` repo · note schema · **validator** · deterministic writer · capture form + voice capture · control center **Brain** surface (list, search-by-description, open, edit, delete) | **None** | No | ✅ 2026-08-02 |
| **2** | `brain.search()` (description-matching) → wired into **the receptionist only** — shows which notes it used | Yes, read-only | No | ✅ 2026-08-02 |
| **—** | **SOAK** — daily use; judge retrieval quality against §8's criterion | — | — | ▶ **running** |
| **3** | **`brain-keeper`** light-track agent (mirrors `vault-keeper`): proposes candidate notes from CONVERSATION (never work output), the operator accepts/edits/rejects each. **Design of record: §14** | Yes, proposals only | No | ✅ 2026-08-03 |
| **4** | **Graduate**: inject retrieval into scoper / reviewer / planner — **one at a time**, each earning it separately | Yes | **Yes** | ⏳ waits on the soak |

**Slice 3 sits inside the soak deliberately.** The soak is only meaningful if the corpus is
growing, and hand-typing every note is the single most likely reason this stalls.

**Slice 1 is usable on its own**, with zero AI. That is the thin-vertical-slice test: if the operator is
not adding memories in week one, the design is wrong and that gets fixed before slice 2.

### Seeding

Harvest the qualifying notes from the 31 existing Claude Code memory files — those that pass the
§2 subject test (estimated 6–8), split down to the ~1 KB atomic form. One-time, gated, done by
hand or with distillation help. The rest stay as session memory.

---

## 11. UI — how it surfaces

Scoped with the operator **2026-07-26**, immediately after the rest of this document. It invents no new
surface *type*: the control center already has three, and the locked rules assign each Brain function to
one of them.

| Existing surface | Role | Brain function |
| --- | --- | --- |
| **Pulse rail** (right, always on) | the **notifier** | `brain-keeper` proposals waiting |
| **Control center** (docked) | the **room** — where the operator acts | reviewing those proposals · the receptionist thread and its citations |
| **Panel** (`gplan`, `ladder`, `finance`…) | a **library** — go look at a thing | the corpus: browse, classify, edit, delete |

### The door: the bottom-left bar, not the core

A **`Brain` button in `.bb-left`** (`index.html`, beside Home / Plan / Ladder / Finance), an
`#brainPanel` sibling of `#ladderPanel` / `#financePanel`, opened through `closePanelsExcept('brain')`,
plus the **`b`** single-key shortcut (verified free — `app.js` binds `k c f l p s h`).

**Explicitly NOT the brain core.** The core opens the control center and that stays. The bottom-left
bar is the *library* cluster (Plan, Ladder, Finance are all things you go look at); the core is what
you *talk to*. Hanging the corpus off the core would blur exactly the distinction the CC/panel split
exists to protect.

### Capture is the make-or-break, and it is not the panel

If capture means "open a panel → New → fill a form", the corpus never grows and the soak (§8) is
meaningless. So the primary path is:

**`⌘K` → `remember <fact>` → Enter.** The palette is already the fastest input in the control center, already
does subsequence matching, and already falls through to the receptionist — `remember` is a natural
sibling of `disp` / `plan` / `arch`. Under two seconds, from anywhere.

**Capture asks for nothing but the text.** `description` comes from what was typed; `type` and `scope`
are left unset. This is safe rather than sloppy: retrieval v1 matches on `description` (§7), so **an
unclassified note is fully retrievable the moment it is written.** `type`/`scope` serve browsing, and
later the narrowing of what a specific agent gets injected — never the note's basic usefulness. The
panel carries a **"needs classifying"** filter for a cheap batch pass. *An unclassified note beats an
uncaptured one.*

**Voice is the second path:** *"Atlas, remember that…"* → existing STT → the same deterministic writer.
Confirmation goes through `voiceSay()` (the voice path, animates the waveform), **not** `brainSay()` —
the file already splits those by intent.

Validator rejections (over cap, missing description) surface **inline where the text was typed**, never
in a panel that has to be gone and found.

### The panel is a library, not a graph

A grid of note cards — the established convention (`repeat(auto-fill, minmax(...))`, `align-items:
stretch`, as used by the roster, procedures and pending grids). Per card: `description` as the title,
`type` tag, `scope`, staleness marker. Click to open, edit, delete. Above it: search over descriptions
plus **themed segmented filters** on `type` / `scope` (`ccSelect()` and the segmented toggle already
exist → zero native controls by construction).

**Do not build a graph view.** The vaults have one because they are document-shaped and navigational;
the Brain is retrieval-shaped, and a hairball of 2,000 atomic facts would look impressive, be useless,
and quietly encourage browsing over capturing. Wikilinks appear as related-note chips inside an open
note — that is all they earn.

**The empty state matters more than usual**: on day one there are ~8 seeded notes, and the panel must
read as *capture something*, not as a broken library.

### Citations — the soak's actual instrument

§8 does not work without this. When the receptionist answers using the Brain, the reply carries a row
of **cited note chips**; clicking one opens that note. Retrieval is visible on every single answer.

Two controls on that row turn the soak from passive observation into a feedback loop:

- **wrong note** — marks a misfire, so §8's graduation criterion is *counted* rather than remembered
  vaguely;
- **should have known** — opens capture pre-filled from the question. The most valuable moment to add
  a memory is the instant a missing one is noticed.

### Proposals reuse the pending pattern exactly

`brain-keeper` proposals are just another thing that needs the operator: the **Pulse rail** lists them, a click
routes into the **CC stage** (the same path as a job decision), which shows an accept / edit / reject
card. The rail gets no second Brain entry; the CC never re-lists what the rail lists. Review is
**batch-friendly** — a grid of proposal cards, not one modal at a time.

### Staleness never touches the Pulse rail

The `reviewed` / `confidence` re-confirm pass (§9) is a low-priority queue **inside the panel** — *"N
notes not confirmed in 6 months"*. The rail is for what needs the operator *now*; a six-month-old belief does
not, and putting it there would train him to ignore the rail — the one surface that must stay
trustworthy. Same rule for the unclassified backlog: visible in the panel, never in the rail.

### Not built

No graph view. **No per-project Brain panel** — the per-project PLAN panel already overlaps neighbouring
canvas cards (known, deferred), and a second one compounds an unfixed bug; `scope` filtering inside the
one global panel covers the need. No stats dashboard — note count is not a metric worth a chart.

### UI per slice

| Slice | UI shipped |
| --- | --- |
| **1** | `Brain` button + `b` · `⌘K remember` · voice capture · the panel (grid, search, filters, classify, edit, delete) · inline validator errors |
| **2** | citation chips + *wrong note* / *should have known* in the receptionist thread |
| **3** | the unharvested-source count in the PANEL; proposals in the Pulse rail → review grid in the CC stage, incl. the conflict card (§14.4, §14.7) |
| **4** | nothing new — graduation is invisible by design |

### Blast radius

`index.html` `.bb-left` + a new `#brainPanel` · `closePanelsExcept()` · the single-key handler
(`app.js` ~9493) · `palActions()` for the `remember` verb · `threadView()` for citations ·
`renderPulse()` / `ccGoJob()` for proposals · `ccSelect()` and the segmented toggle for filters.

---

## 12. Explicitly deferred / out of scope

- **MCP server.** Originally scoped as slice 3 on the OB1 rationale ("any AI tool shares one
  memory"). **Demoted 2026-07-26**: that argument optimizes for Claude Code as a client, and
  The operator's end state is interacting **inside Atlas**, not through external tools. MCP's real, durable
  value is as a **seam** — a query interface owned by no single client, so roles migrating to local
  models (llama.cpp / Ollama) read the same Brain without a rewrite. Worth building **later**, for
  that reason, not this one.
- **Log-style / episodic memory** (transcripts ingested wholesale, mined later). Rejected — see
  §3.1. If this is ever wanted it is a *separate* store with its own substrate decision, not a
  loosening of the Brain's curation rule.
- **Postgres + pgvector** (OB1's actual stack). Rejected: it buys nothing at 2 MB, costs a service
  to run and back up on constrained hardware, and breaks the plain-files convention the phone slice
  already depends on.
- **Cloud anything.** Permanently.

---

## 13. Open questions (resolve at build time)

*(Control center surface placement — the original question 1 — was resolved 2026-07-26; see §11.)*

1. **`scope` vocabulary** — RESOLVED 2026-08-02: constrained to registry project ids + `global` +
   `atlas`. `atlas` is listed explicitly because Atlas is the control plane and has no `projects:`
   entry, yet a large share of the corpus is taste about how Atlas itself gets built — deriving the
   vocabulary from the registry alone left those facts unscopeable.
2. **Voice capture phrasing** — RESOLVED 2026-08-02: `"Atlas, remember that…"` and a bare
   `"remember …"`. **`"remember TO …"` is excluded**: that phrasing is a todo, and filing a task as a
   durable fact would corrupt the corpus. Capture yields to a focused or named waiting session so a
   spoken answer is never swallowed into the Brain; an explicit `"Atlas,"` lead still overrides.
3. **Harvest trigger** — RESOLVED 2026-08-03; see §14.4.
5. **Conflict handling** — RESOLVED 2026-08-03; see §14.7.

---

## 14. `brain-keeper` (slice 3) — the design of record

> Scoped with the operator **2026-08-03**, the day after slices 1+2 shipped. This section supersedes the
> one-line description in §10 and answers §13.3 and §13.5.

### 14.1 Why it exists: growth is the bottleneck

Measured on day one of the soak: **15 notes came from the seed, 1 from manual capture, 0 overnight.**
§10 already predicted this ("hand-typing every note is the single most likely reason this stalls"),
and the prediction held within 24 hours. `brain-keeper` is that prediction's countermeasure.

It is nothing more exotic than **the CP3 seeding session, automated**: read a source, apply the
subject test, propose what passes, let the operator accept, edit or reject each one. The write path is
unchanged — the same deterministic writer commits, so §6's rule survives intact.

### 14.2 It reads CONVERSATION, never work output

The original wording ("merged dispatches and sessions") lumped two sources with very different
yields. Pulled apart:

- **Merged dispatches** are about *the work*. The subject test rejects nearly all of it — "the
  engine is live" belongs in `status.yaml`. Low hit rate, and every miss costs a rejection click.
- **Conversation** is where the operator reveals himself. One day of scoping produced five real
  facts — a standing style constraint, a language level, a location, a target, a weekly rhythm.
  **None could have come from a dispatch.** And the 16 seeded notes came from agent memory files,
  which are distilled conversation.

> **The operator reveals himself when he is talking, not when the machine is working.**

Note this is *not* a global-vs-project split. `scope` exists, so a `almanac`-scoped note is still a
Brain note. The line is **subject**, exactly as in §2: "Almanac gates every strategy before real
money" is the work; "he trades only what a deterministic edge supports" is him.

### 14.3 Two readers, with disjoint territories

> **CORRECTED 2026-08-03, and the correction matters.** This section first claimed that Atlas's own
> agents run `claude -p` and therefore write **no** memory files, which made a memory-file reader a
> bridge with an expiry date and reader 2 the durable engine. **That was wrong.** Two test prompts
> to the receptionist produced two memory files within seconds, timestamped to the minute of each
> call. The original evidence (a dozen `D--work-atlas-worktrees-job-*` dirs, none with a `memory/`)
> was real but did not generalise: dispatched work contains no facts about the operator, so nothing gets
> saved, and the same is true of the architect and planner (`D--AI-orchard-api` and
> `D--AI-orchard-site` have no `memory/` either).

**Every path invokes the CLI identically** (`claude -p --output-format json`). So whether a memory
file is written turns on **content, not on which agent asked**: the memory system saves when the
model judges that a durable fact appeared. "Which agents write memory" is the wrong question, and
there is no configuration that makes it deterministic, because the decision belongs to a model
Atlas neither controls nor can inspect.

Which is why there are two readers, and why their territories are made **disjoint** rather than one
being switched off:

| Reader | Territory | Shape | Cost |
| --- | --- | --- | --- |
| **1 — memory files** | the operator's terminal Claude Code sessions, and the backlog | **pull**: files persist, a scan rediscovers them | one call per new *file* |
| **2 — in-flight** | what the operator says to a control center agent | **push**: a turn is gone a second later | one call per *qualifying turn* |
| *(3 — escalations)* | the operator's answers to `needs_input` jobs | not built | — |

**The overlap, and how it is closed.** A turn the operator types in the control center can arrive twice: pushed by
reader 2 as he types it, and found by reader 1 in the memory file that same turn produced. Two
source ids for one fact, which dedup cannot see through.

The CLI returns a `session_id` on every response; a memory file records the `originSessionId` that
wrote it. So Atlas records the ids of its own calls (`dashboard/atlas_sessions.py`, written to by
substrate, the brainstormer and the worker), and **reader 1 skips any file whose origin session was
Atlas's own**. Every turn is judged exactly once, by a reader that can be named in advance.

Losing that ledger degrades to the old overlapping behaviour rather than to an error, and a memory
file with no recorded session stays reader 1's, so older files never silently vanish from the queue.

**Reader 2's trigger, stated so it is predictable:** the message the operator typed, 30+ characters, containing
a first-person word (`I`, `my`, `me`) or a standing-rule word (`never`, `always`, `prefer`, `don't`,
`from now on`), and not a question (ends in `?` *and* opens with a question word). Passing that
filter only buys one model call; the charter still applies the subject test, and most still fail it.

### 14.4 Harvesting distillations, and why that is not log-mining

§3.1 rejects "transcripts mined later". Both readers stay clear of it **structurally**, not by
discipline:

- **Reader 1 never reads a conversation.** A Claude Code memory file is already one distilled fact
  with a description and a *Why* — one layer of curation has happened, by another process, at the
  moment the fact was learned. `brain-keeper` only applies the subject test and splits anything over
  the cap.
- **Reader 2 proposes in the moment, so no transcript is ever stored.** Control center threads live in the
  browser and vanish; harvesting them later would require Atlas to *start* keeping transcripts, which
  is the log the design refuses. Proposing in-flight is also exactly how Claude Code's own memory
  works, which is the mechanism that produced the 107 files in the first place.

**Trigger (answers §13.3): detection is free and automatic, harvesting is on demand.**

1. A deterministic scan hashes every source file and compares it against what has been accepted or
   rejected. No model, no tokens, milliseconds. Rides the control center's existing poll.
2. The **Brain panel** shows the count ("7 new memory files to review"). Never the Pulse rail:
   unharvested files do not need the operator *now*, and §11's rule is that the rail stays trustworthy.
3. The operator clicks; `brain-keeper` runs on exactly those files.
4. **The resulting proposals go to the Pulse rail**, because now a decision is waiting on him.

A fixed schedule was rejected on measured data: growth is bursty (50 files in 30 days, 2 in the last
7), so any interval is either mostly wasted or too slow. On-demand *alone* was rejected too, because
without the count the operator would never know whether a run was worth it.

### 14.5 Dedup: one file, one fact, one hash

A Claude Code memory file holds one fact, the same shape as a Brain note, so a **file content hash
is a fact-level hash**. No parsing, no fuzzy matching.

| State | Where it lives | Why there |
| --- | --- | --- |
| **Accepted** | the note's own `source:` field, e.g. `cc-memory:D--work-orchard-vault/feedback_no-wildcard-imports@a1b2c3d4` | Derivable from the corpus, so principle 2 holds: files are truth, the index is derived and deletable. |
| **Rejected** | a small gitignored ledger at the Atlas root | Nothing was written, so there is no other record. Deleting it costs a re-ask, never a wrong result. |
| **Changed** | hash differs → re-offered **as an update** to the linked note | The underlying fact was revised, so the note may need revising. This is most of §14.7 for free. |

**Watermark at 2026-08-02.** The 107 existing files were already reviewed together during CP3;
without this, run one would re-litigate the ~90 that did not qualify.

### 14.6 Charter: precision over recall, deliberately

The asymmetry decides it. **A missed fact is recoverable** — it stays in the source and is re-offered
when that source changes, or the operator captures it by hand. **A bad proposal is not**: it spends his
attention permanently, and enough of them and he stops opening the review grid. His own
[[check-the-work-before-he-sees-it]] says the same thing about his review time.

Three filters, in order:

1. **The subject test, verbatim** (§2): *if every project were cancelled tomorrow, would this still
   be true?*
2. **"So what should be done differently?"** If a note changes nothing an agent would do, it is not
   a note. This is the filter that catches the plausible-but-useless: *"prefers a rollback path used
   daily"* is a real pattern of the operator's, and nothing acts on it.
3. **Would the operator recognize it?** (§3.1). He did not recognize two of his own early captures, which is
   how the bar was calibrated.

Plus an explicit reject list drawn from what is actually in the memory files: build status, branch
state, test counts, file paths, environment setup, migration numbers.

**Calibration:** CP3 qualified 16 of 107 files (~15%), and the operator accepted essentially all of what was
shown, correcting three. That is the target behaviour; the charter is the by-hand pass written down.

**Volume: 10 proposals per run**, and it reports what it held back ("10 shown, 6 more qualified").
The cap is about reviewability, not about the agent: ten cards is one sitting, thirty is a chore that
gets abandoned. The operator controls the rest already, because the panel tells him how many sources wait.

### 14.7 Conflicts (answers §13.5): show both, and allow EDIT

The proposing agent receives the **full corpus index** (every description, ~2 KB today) before it
proposes anything. Three outcomes:

- **Already covered** → propose nothing. Silent, costs the operator nothing.
- **Contradicts an existing note** → propose it *as a conflict*, naming the note it clashes with,
  both shown side by side.
- **New** → propose normally.

A conflict offers three actions: **keep the existing · replace it · EDIT the existing.**

The third is the one that matters. The `no-wildcard-imports` correction was formally a
contradiction ("applies to chat replies" vs "not chat replies") but was really a **scope narrowing**,
and the right resolution was neither keep nor replace. A keep-or-replace UI loses the *Why* and the
accumulated nuance every time it is used.

### 14.8 Build order inside slice 3

1. **Deterministic layer first** — the source scanner, hashes, the accept/reject ledger, the
   watermark, the panel count. Zero AI, independently testable, and it is what makes runs idempotent.
2. **Reader 1 + the charter** — the risky, unproven part, aimed at material the operator has already watched
   being handled by hand, so the output can be judged against a known-good pass.
3. **The review grid** — Pulse rail entry → CC stage, batch-friendly, accept / edit / reject, plus
   the conflict card.
4. **Reader 2** — mostly a prompt and a hook once the charter has earned trust; the UI already exists.

### 14.9 Still open

- **Duplication with the source.** When a fact is promoted, it stays in the Claude Code memory file
  too, so it lives in two stores. Removing it would mean `brain-keeper` mutating files another system
  owns. Left alone for now; revisit if it causes real confusion.
- ~~**Reader 2's cost shape.**~~ **Resolved in the build.** The deterministic pre-filter was built
  rather than deferred: a turn is only sent to the model if it is 30+ characters, carries a
  first-person or standing-rule marker, and is not a question. Roughly 3 turns in 8 survive it, so
  the model cost tracks assertions rather than conversation volume. What remains genuinely open is
  whether that filter is *too* tight — it is deliberately blind to a fact stated in the second half
  of a long question.

### 14.10 One turn, one reader

The rule, stated so it can be checked rather than trusted:

| What happened | Who judges it | When |
| --- | --- | --- |
| the operator types in a Claude Code terminal session, and a memory file is written | **reader 1** | next time he runs a harvest |
| the operator types to the receptionist or brainstormer | **reader 2** | within seconds, in the background |
| …and that turn also writes a memory file | **nobody, again** | reader 1 skips it by origin session |
| A dispatch, architect or planner session runs | nobody | they write no memory files |

`dashboard/atlas_sessions.py` holds the ids, capped and gitignored. It is written to from the three
places Atlas invokes the CLI: `substrate._claude_chat` (receptionist, ask-orchard, ask-almanac,
brain-keeper itself), `brainstorm._claude_answer`, and `worker.parse_result` (dispatch, architect,
planner — insurance, since those have never written a memory file).

**Failure is chosen to be dull.** No ledger means the readers overlap again, which costs a duplicate
proposal to reject. A memory file with no recorded session belongs to reader 1, so nothing written
before this existed disappears from the queue. Neither case errors, and neither loses a fact.
