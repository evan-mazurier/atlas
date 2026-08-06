"""
Atlas — `brain-keeper`: the deterministic half (docs/brain.md §14).

This module decides WHAT is worth looking at. It never decides what is worth keeping: no model runs
here, nothing is written to the corpus, and no source file is ever modified. The model half (the
charter that applies the subject test) lands on top of this and can only ever *propose*, per §6.

Everything here exists to make a harvest run **idempotent and cheap**, so the count of unreviewed
sources can be computed on every control center poll for free (§14.4): a hash scan, no tokens, milliseconds.

The three states a source can be in, and where each is recorded (§14.5):

  accepted  -> the NOTE's own `source:` field, e.g. cc-memory:D--work-orchard-vault/foo@a1b2c3d4.
               Derived from the corpus, so principle 2 holds: files are truth, indexes are derived
               and deletable. Nothing here duplicates it.
  rejected  -> a small ledger, because nothing was written and there is no other record. Gitignored.
               Deleting it costs a re-ask, never a wrong result.
  changed   -> the source id matches a note but the hash does not, so the fact was revised and the
               note may need revising: offered as an UPDATE, not as a new note.

Two readers, with DISJOINT territories (§14.10):

  reader 1 (pull)  -> Claude Code memory files, from anywhere: the operator's terminal sessions and the
                      existing backlog. Cheap, and its input was distilled by another process
                      before we saw it.
  reader 2 (push)  -> what the operator says to a control center agent, judged as he says it, because a
                      conversation turn is gone a second later.

They do not overlap, and that took work. Atlas's own agents DO write memory files (an earlier
version of this file claimed otherwise, on the strength of dispatch worktrees having none: they
run `claude -p`, but so does everything else, and whether a memory file gets written turns on
CONTENT rather than on which agent asked). So a turn the operator types in the control center can arrive twice,
once pushed by reader 2 and once found by reader 1 in the memory file that same turn produced.
Reader 1 therefore skips any file whose originSessionId belongs to an Atlas call, which the CLI
hands us on every response. Every turn is judged once, by a reader you can name in advance.
"""
from __future__ import annotations

import os
import json
import hashlib
import datetime as dt
from pathlib import Path

import brain

# Sources that already existed when the operator and Atlas reviewed the whole corpus by hand (CP3). Without
# this, run one would re-litigate the ~90 files that did not qualify that day.
WATERMARK = dt.date.fromisoformat(os.environ.get("ATLAS_HARVEST_WATERMARK", "2026-08-02"))

# The reject ledger sits beside the soak ledger: Atlas-local operational state, not corpus content.
REJECT_LEDGER = brain._ATLAS_ROOT / "brain_harvest_rejected.json"

# A CACHE, not a decision. The charter's own "no" must never be filed as the operator's judgement (§14.6),
# but without recording it somewhere the same source stays pending forever and every run pays for
# the same answer. This records only "we already spent a call on this exact hash and it did not
# qualify", which is a fact about work already done rather than a claim about what the operator wants. It
# expires the moment the source changes, and deleting it costs one re-run.
JUDGED_LEDGER = brain._ATLAS_ROOT / "brain_harvest_judged.json"

MAX_PROPOSALS = 10          # §14.6 — a review grid of ten is one sitting; thirty is abandoned


def cc_memory_root() -> Path:
    """Where Claude Code keeps its per-project memory. ATLAS_CC_MEMORY overrides (tests, WSL)."""
    raw = os.environ.get("ATLAS_CC_MEMORY")
    return Path(raw) if raw else Path.home() / ".claude" / "projects"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]


# ---- readers ----------------------------------------------------------------------------
def _read_cc_memory() -> list[dict]:
    """Reader 1: Claude Code memory files.

    One file holds one fact, the same shape as a Brain note, so a file's content hash IS a
    fact-level hash (§14.5) and no parsing is needed to tell two facts apart. MEMORY.md is the
    per-project index, not a fact, so it is skipped.

    This reader never sees a conversation: it reads what another process already distilled, which
    is what keeps it clear of §3.1's rejection of transcript mining.
    """
    root = cc_memory_root()
    if not root.exists():
        return []
    out = []
    for mem in sorted(root.glob("*/memory")):
        if not mem.is_dir():
            continue
        for f in sorted(mem.glob("*.md")):
            if f.name.lower() == "memory.md":
                continue
            try:
                text = f.read_text(encoding="utf-8")
                mtime = dt.date.fromtimestamp(f.stat().st_mtime)
            except Exception:  # noqa: BLE001 — one unreadable file must never break a scan
                continue
            out.append({
                "id": f"cc-memory:{mem.parent.name}/{f.stem}",
                "reader": "cc-memory",
                "origin": mem.parent.name,
                "title": f.stem,
                "path": str(f),
                "hash": _hash(text),
                "modified": mtime.isoformat(),
                # Which CLI session wrote it. Atlas's own conversations write memory files too
                # (§14.10), and reader 2 has already judged those turns; without this the same
                # fact arrives twice under two source ids and dedup cannot see they are one.
                "origin_session": (_ORIGIN_SESSION.search(text).group(1)
                                   if _ORIGIN_SESSION.search(text) else ""),
                "text": text,
            })
    return out


_ORIGIN_SESSION = __import__("re").compile(
    r"originSessionId:\s*([0-9a-fA-F-]{16,})")


READERS = {"cc-memory": _read_cc_memory}

# ---- reader 2: Atlas's own conversations (§14.3) -----------------------------------------
# Structurally different from reader 1, and it has to be. Reader 1 PULLS: files sit on disk and a
# scan rediscovers them whenever we like. A conversation turn exists for an instant and is gone,
# because control center threads live in the browser and Atlas stores none of them. So reader 2 PUSHES:
# it judges at the moment the turn happens and queues a proposal, or lets it go.
#
# That is what keeps it clear of §3.1's rejection of transcript mining. Harvesting threads later
# would mean Atlas keeping transcripts to harvest, which is exactly the log the design refuses.
# Judging in the moment stores nothing except the proposal the operator accepts.
_TURN_MIN_CHARS = 30
# Deterministic pre-filter, so most turns never reach a model at all. It looks for a first-person
# assertion, because a fact about the operator is something he says about himself: questions, task
# instructions and passing remarks all fail it for free.
_TURN_MARKERS = __import__("re").compile(
    r"\b(i|i'm|im|i've|my|me|myself)\b|\bnever\b|\balways\b|\bprefer\b|\bdon'?t\b|"
    r"\bmake sure\b|\bfrom now on\b|\bi want\b|\bi like\b|\bi hate\b|\bremember\b", __import__("re").I)
_QUESTION_LEAD = __import__("re").compile(
    r"^\s*(should|shall|can|could|would|will|do|does|did|is|are|was|were|have|has|"
    r"what|how|why|when|where|which|who|any)\b", __import__("re").I)


def turn_is_worth_judging(text: str) -> bool:
    """Cheap, deterministic, and deliberately blunt. Wrong in the safe direction: a turn it lets
    through costs one model call that probably rejects, a turn it drops costs nothing, and the operator can
    always capture the fact himself with Ctrl+K."""
    t = (text or "").strip()
    if len(t) < _TURN_MIN_CHARS:
        return False
    # A question is asking, not telling. "Should I click Run brain-keeper?" contains "I" and reveals
    # nothing, and this is the single most common shape of turn in a working session. Rejected
    # before the model, not by it.
    if t.endswith("?") and _QUESTION_LEAD.match(t):
        return False
    return bool(_TURN_MARKERS.search(t))


def observe_turn(agent: str, text: str) -> dict | None:
    """Judge one thing the operator just said. Returns the queued proposal, or None.

    Never raises: this runs off the back of a conversation, and a failure here must not touch the
    reply he is reading. Identity is the hash of what he said, so saying the same thing twice is
    the same source and the existing dedup applies with no special case.
    """
    try:
        t = (text or "").strip()
        if not turn_is_worth_judging(t):
            return None
        if len(load_proposals()) >= MAX_PROPOSALS:
            return None                      # the review grid is already full; do not pile up
        src = {"id": f"atlas-chat:{agent}/{_hash(t)}", "reader": "atlas-chat",
               "origin": agent, "title": "conversation", "hash": _hash(t),
               "modified": dt.date.today().isoformat(), "text": t, "state": "new"}
        if src["id"] in harvested() or src["id"] in judged() or src["id"] in rejected():
            return None                      # already settled, said again
        v = judge(src, kind="turn")
        if v["verdict"] == "accept":
            queue([v])
            return v
        if v["verdict"] == "reject":
            mark_judged(src["id"], src["hash"], v.get("reason", ""))
        return None
    except Exception:  # noqa: BLE001 — an enrichment that breaks a conversation is worse than none
        return None


def sources(reader: str | None = None) -> list[dict]:
    """Every candidate source unit, from every reader (or one named reader)."""
    out = []
    for name, fn in READERS.items():
        if reader and name != reader:
            continue
        try:
            out.extend(fn())
        except Exception:  # noqa: BLE001 — a broken reader degrades to no sources, never a crash
            continue
    return out


# ---- state ------------------------------------------------------------------------------
def source_ref(source_id: str, hash_: str) -> str:
    """The string written into an accepted note's `source:` field."""
    return f"{source_id}@{hash_}"


def parse_source_ref(ref: str):
    """`cc-memory:dir/file@a1b2c3d4` -> ("cc-memory:dir/file", "a1b2c3d4"). None if not a ref."""
    ref = (ref or "").strip()
    if "@" not in ref or ":" not in ref.split("@")[0]:
        return None
    sid, _, h = ref.rpartition("@")
    return (sid, h) if sid and h else None


def harvested() -> dict:
    """source_id -> {"note", "hash"} for everything already promoted. Read from the CORPUS, so this
    stays true even if every ledger on disk is deleted."""
    out = {}
    for note in brain.list_notes():
        parsed = parse_source_ref(note.get("source") or "")
        if parsed:
            out[parsed[0]] = {"note": note["name"], "hash": parsed[1]}
    return out


def rejected() -> dict:
    """source_id -> {"hash", "at", "reason"}. Absent/corrupt ledger reads as empty: the cost is
    being asked again about facts already declined, which is annoying and never wrong."""
    try:
        data = json.loads(REJECT_LEDGER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def mark_rejected(source_id: str, hash_: str, reason: str = "") -> dict:
    """The operator declined this fact. Recorded against the HASH, so a later revision of the same source
    is offered again rather than silently suppressed forever."""
    led = rejected()
    led[source_id] = {"hash": hash_, "at": dt.datetime.now().isoformat(timespec="seconds"),
                      "reason": (reason or "").strip()[:200]}
    REJECT_LEDGER.write_text(json.dumps(led, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "rejected": len(led)}


def judged() -> dict:
    """source_id -> {"hash", "at", "reason"}: sources the charter has already looked at and passed
    over. Absent or corrupt reads as empty, which costs a re-run and nothing else."""
    try:
        data = json.loads(JUDGED_LEDGER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def mark_judged(source_id: str, hash_: str, reason: str = "") -> None:
    """Record that the charter has already answered for this exact hash. Errors are NOT recorded:
    a timed-out call is not an answer, and retrying it is the right behaviour."""
    led = judged()
    led[source_id] = {"hash": hash_, "at": dt.datetime.now().isoformat(timespec="seconds"),
                      "reason": (reason or "").strip()[:200]}
    JUDGED_LEDGER.write_text(json.dumps(led, indent=2, ensure_ascii=False), encoding="utf-8")


def unjudge(source_id: str) -> dict:
    """Force a re-judge without waiting for the source to change."""
    led = judged()
    led.pop(source_id, None)
    JUDGED_LEDGER.write_text(json.dumps(led, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "judged": len(led)}


def unreject(source_id: str) -> dict:
    """Undo a rejection, so a mis-click is recoverable without hand-editing JSON."""
    led = rejected()
    led.pop(source_id, None)
    REJECT_LEDGER.write_text(json.dumps(led, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "rejected": len(led)}


# ---- what needs looking at ---------------------------------------------------------------
def pending(reader: str | None = None) -> list[dict]:
    """Sources that warrant a model pass, newest first. Two kinds:

      "new"    -> never harvested, never rejected, and touched since the watermark
      "update" -> harvested before, but the source has changed since; the fact was revised, so the
                  note may need revising (this is most of the conflict machinery, for free)

    The `text` is carried through because the caller (the charter) needs it; the count endpoint
    ignores it.
    """
    seen, dead, done = harvested(), rejected(), judged()
    try:
        import atlas_sessions
        ours = atlas_sessions.known()
    except Exception:  # noqa: BLE001 — degrades to the old overlapping behaviour, never an error
        ours = set()
    out = []
    for s in sources(reader):
        # Reader 2 owns Atlas's own conversations and judged this turn as it happened. Skipping the
        # memory file it produced is what makes the two readers' territories disjoint: every turn
        # judged exactly once, by a reader you can name in advance (§14.10).
        if s.get("origin_session") and s["origin_session"] in ours:
            continue
        prior, no, seen_by_charter = seen.get(s["id"]), dead.get(s["id"]), done.get(s["id"])
        if prior:
            if prior["hash"] != s["hash"]:
                out.append({**s, "state": "update", "note": prior["note"]})
            continue
        if no and no.get("hash") == s["hash"]:
            continue                                    # declined by the operator, and unchanged since
        if seen_by_charter and seen_by_charter.get("hash") == s["hash"]:
            continue                                    # already judged; do not pay for it twice
        if s["modified"] < WATERMARK.isoformat():
            continue                                    # predates CP3's by-hand review
        out.append({**s, "state": "new"})
    out.sort(key=lambda r: (r["modified"], r["id"]), reverse=True)
    return out


# ===================== the charter: does this fact belong to the operator? =====================
# The only model in brain-keeper, and the only unproven part of it. Everything above is a hash scan;
# this is a judgment call with no test that can gate it, so the design leans on one asymmetry
# (§14.6): a fact it misses stays in its source and comes back when that source changes, while a bad
# proposal spends the operator's attention permanently. Hence: when in doubt, reject.
#
# It cannot write. It returns proposals; brain.write_note commits only after the operator accepts (§6).
CHARTER = """You are brain-keeper. You decide whether ONE distilled fact belongs in the operator's personal \
knowledge corpus.

That corpus holds facts about THE OPERATOR: his taste, his standing constraints, how he \
decides, what he is committed to. It does NOT hold facts about his projects, his code, his tooling \
or his progress. Those already live in status.yaml, plan.yaml and the decision vaults.

THE TEST. All three must pass.

1. SUBJECT. If every project the operator runs were cancelled tomorrow, would this still be true of him?
   Yes, it is about him. No, it is about the work, and it belongs somewhere else.
2. ACTIONABLE. Does it change what an assistant would DO or WRITE? If nothing acts on it, it is not
   a note, however true it is.
3. RECOGNISABLE. Would the operator read it and recognise himself? A vague slogan fails even when accurate.
   "Prefers a rollback path" failed this: real of him, and nothing acts on it.

ALWAYS REJECT: build or branch status, what is merged, test counts, file paths, environment and
tooling setup, migration numbers, credentials, and anything dated that will simply be false next
month. Also reject anything about how Atlas itself was built: that is the work.

BIAS: REJECT. A fact you miss stays in its source and is offered again later. A bad proposal costs
The operator attention he does not get back. When you hesitate, reject.

If it passes, write the note:
- DESCRIPTION is one line, and it is the retrieval key: specific enough to stand alone out of context.
- BODY is the fact in the third person ("He prefers...", "Never do X"), under 1000 characters, with a
  **Why:** line and an **Applies to:** line when they carry real information. Say when the rule does
  NOT apply if that is part of it.
- TYPE is one of: taste, preference, decision-pattern, constraint, person, fact.
- SCOPE is one of: {scopes}. Use global unless the fact is genuinely specific to one project.
- Never use markdown headings or bullet lists inside the note.

DUPLICATES. Reject if an existing note already covers this fact, even PARTIALLY. Do not argue that
your angle is new: if the substance already sits in a note's description or body, an assistant
already has it, and a second note earns nothing while making both harder to maintain. Propose only
when it would change what an assistant does in a case the existing note does not reach.

EXISTING NOTES (name one under CONFLICTS only if your fact genuinely contradicts it):
{index}

THE SOURCE, from {origin}:{kind_note}
---
{text}
---

Reply in EXACTLY this format and nothing else.

VERDICT: accept
REASON: <one line>
DESCRIPTION: <one line>
TYPE: <type>
SCOPE: <scope>
CONFLICTS: <existing note name, or NONE>
BODY:
<the note body>

or:

VERDICT: reject
REASON: <one line, which test it failed>"""


def _parse_proposal(text: str) -> dict | None:
    """The reply, line-parsed. Deliberately not JSON: a flat key/value block is far harder for a
    model to malform, and a malformed proposal is dropped rather than repaired."""
    out, body, in_body = {}, [], False
    for line in (text or "").splitlines():
        if in_body:
            body.append(line)
            continue
        if line.strip().upper().startswith("BODY:"):
            in_body = True
            rest = line.split(":", 1)[1].strip()
            if rest:
                body.append(rest)
            continue
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().upper()
            if k in ("VERDICT", "REASON", "DESCRIPTION", "TYPE", "SCOPE", "CONFLICTS"):
                out[k.lower()] = v.strip()
    out["body"] = "\n".join(body).strip()
    return out if out.get("verdict") else None


# Reader 1's sources are already distilled: another process decided a fact was worth writing down
# before we ever saw it. A conversation turn has had no such pass, so the charter is told plainly
# that it is reading raw speech and that most of it is nothing.
_KIND_NOTE = {
    "note": "",
    "turn": "\nThis is a RAW MESSAGE the operator typed in conversation, not a distilled note. Nobody has "
            "judged it yet. Extract at most ONE durable fact about him, or reject. Most conversation "
            "contains none: a question, an instruction for the task at hand, a passing reaction, or "
            "thinking aloud are all rejects. Accept only if he stated something about himself that "
            "would still be true next month.",
}


def judge(source: dict, index: list[dict] | None = None, kind: str = "note") -> dict:
    """One source in, one verdict out. Every field is checked against the corpus's own rules before
    it is offered, so a proposal that reaches the operator is at least SHAPED like a note; whether it is a
    good note is his call, which is the whole point of the gate."""
    idx = brain.index() if index is None else index
    listing = "\n".join(f"{n['name']}: {n['description']}" for n in idx) or "(the corpus is empty)"
    prompt = CHARTER.format(scopes=", ".join(brain.scopes()), index=listing,
                            origin=source.get("origin", "?"), text=source.get("text", ""),
                            kind_note=_KIND_NOTE.get(kind, ""))
    base = {"id": source["id"], "hash": source["hash"], "origin": source.get("origin", ""),
            "title": source.get("title", ""), "state": source.get("state", "new"),
            "note": source.get("note")}
    try:
        import substrate
        res = substrate.chat("brain-keeper", [{"role": "user", "content": prompt}],
                             temperature=0, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return {**base, "verdict": "error", "reason": str(exc)}
    if not res.get("ok"):
        return {**base, "verdict": "error", "reason": res.get("error") or "no reply"}

    p = _parse_proposal(res.get("text") or "")
    if not p:
        return {**base, "verdict": "error", "reason": "unparseable reply"}
    if p["verdict"].lower().startswith("reject"):
        return {**base, "verdict": "reject", "reason": p.get("reason", "")}

    # accepted by the model: now hold it to the corpus's rules, deterministically
    fields = {"description": p.get("description", ""), "body": p.get("body", ""),
              "type": (p.get("type") or "").lower(), "scope": (p.get("scope") or "").lower()}
    try:
        brain.validate({**fields, "confidence": "high", "source": source_ref(source["id"], source["hash"])})
    except brain.BrainError as exc:
        # a proposal that breaks the schema is dropped, never repaired: repairing it would be the
        # model's judgement laundered through code
        return {**base, "verdict": "error", "reason": f"malformed proposal: {exc}"}

    conflict = (p.get("conflicts") or "").strip().lower()
    if conflict in ("", "none", "n/a") or not any(n["name"] == conflict for n in idx):
        conflict = ""
    return {**base, "verdict": "accept", "reason": p.get("reason", ""), "conflict": conflict, **fields}


def run(limit: int | None = None, reader: str | None = None) -> dict:
    """A harvest pass: judge the pending sources, return the proposals. Writes nothing.

    Rejections are NOT auto-recorded. A model saying "not about the operator" is a suggestion, and silently
    filing it as the operator's decision would let the charter quietly shrink the corpus with no one
    watching. He rejects; the ledger records HIS call.
    """
    cap = MAX_PROPOSALS if limit is None else limit
    todo = pending(reader)
    idx = brain.index()
    proposals, errors, accepted = [], [], 0
    for src in todo:
        if accepted >= cap:
            break
        v = judge(src, idx)
        if v["verdict"] == "accept":
            proposals.append(v)
            accepted += 1
        elif v["verdict"] == "reject":
            # cache the answer so the next run does not pay for it again. This is NOT the operator's
            # rejection: it never enters the reject ledger, and it expires when the source changes.
            mark_judged(src["id"], src["hash"], v.get("reason", ""))
        elif v["verdict"] == "error":
            errors.append(v)
    if proposals:
        queue(proposals)
    return {"ok": True, "proposals": proposals, "errors": errors,
            "scanned": len(todo), "held_back": max(0, len(todo) - len(proposals) - len(errors)),
            "cap": cap, "queued": len(load_proposals())}


# ===================== the proposal queue =====================
# Proposals persist between the run that produced them and the operator's decision on each. They are NOT
# corpus content (nothing here is a fact about the operator yet) and not history worth keeping, so they live
# beside the ledgers as gitignored local state. Losing the file loses a batch, which costs one
# re-run and nothing else.
PROPOSALS = brain._ATLAS_ROOT / "brain_proposals.json"


def load_proposals() -> list[dict]:
    try:
        data = json.loads(PROPOSALS.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save_proposals(items: list[dict]) -> None:
    PROPOSALS.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def queue(items: list[dict]) -> list[dict]:
    """Add a run's proposals, replacing any earlier proposal for the same source."""
    keep = {p["id"]: p for p in load_proposals()}
    for p in items:
        keep[p["id"]] = p
    out = list(keep.values())
    _save_proposals(out)
    return out


def drop_proposal(source_id: str) -> list[dict]:
    out = [p for p in load_proposals() if p.get("id") != source_id]
    _save_proposals(out)
    return out


def accept(source_id: str, description: str, body: str, type: str = "", scope: str = "",
           name: str = "", overwrite: bool = False) -> dict:
    """The operator accepted a proposal. The note is written by brain.write_note and nothing else: the same
    function the palette calls, so §6's rule holds no matter which door the fact came through.

    The note records the exact source and hash it came from, which is what makes "already harvested"
    derivable from the corpus rather than from a ledger (§14.5).
    """
    p = next((x for x in load_proposals() if x.get("id") == source_id), None)
    if not p:
        raise brain.BrainError("That proposal is no longer in the queue.")
    res = brain.write_note(description=description, body=body, name=name,
                           type=type, scope=scope, confidence="high",
                           source=source_ref(p["id"], p["hash"]), overwrite=overwrite)
    drop_proposal(source_id)
    unjudge(source_id)                     # settled by the corpus now; the cache is redundant
    return {**res, "remaining": len(load_proposals())}


def reject(source_id: str, reason: str = "") -> dict:
    """The operator declined. THIS is what writes the reject ledger: the charter's own opinion never does
    (§14.6), so the ledger only ever records his call."""
    p = next((x for x in load_proposals() if x.get("id") == source_id), None)
    if not p:
        raise brain.BrainError("That proposal is no longer in the queue.")
    mark_rejected(p["id"], p["hash"], reason)
    drop_proposal(source_id)
    unjudge(source_id)                     # the operator's ledger supersedes the cache
    return {"ok": True, "remaining": len(load_proposals())}


def status() -> dict:
    """The cheap summary the Brain panel shows (§14.4). No model, no tokens: safe on every poll.

    It lives in the PANEL and never in the Pulse rail: an unharvested source does not need the operator
    *now*, and the rail only stays trustworthy while everything in it does.
    """
    try:
        p = pending()
        try:
            import atlas_sessions
            atlas_own = len(atlas_sessions.known())
        except Exception:  # noqa: BLE001
            atlas_own = 0
        return {"ok": True, "pending": len(p), "queued": len(load_proposals()),
                "judged": len(judged()), "atlas_sessions": atlas_own,
                "new": sum(1 for x in p if x["state"] == "new"),
                "updates": sum(1 for x in p if x["state"] == "update"),
                "harvested": len(harvested()), "rejected": len(rejected()),
                "watermark": WATERMARK.isoformat(), "readers": sorted(READERS),
                "cap": MAX_PROPOSALS}
    except Exception as exc:  # noqa: BLE001 — the panel must render even if a scan fails
        return {"ok": False, "error": str(exc), "pending": 0}
