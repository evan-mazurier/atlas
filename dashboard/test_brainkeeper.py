"""
brain-keeper, deterministic half (docs/brain.md §14.4, §14.5).

No model runs in any of this, which is the point: the layer that decides WHAT to look at must be
free enough to run on every control center poll and idempotent enough that "run it again" is always safe.

The properties under test are the ones that make a harvest survivable:

  - the same source is never offered twice;
  - a REVISED source is offered again, as an update rather than a duplicate;
  - a declined source stays declined, until it changes;
  - accepted state is derived from the corpus, so deleting every ledger loses nothing important;
  - the watermark stops run one re-litigating the 107 files already reviewed by hand at CP3;
  - nothing here ever writes to a source file.
"""
from __future__ import annotations

import sys
import datetime as dt
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import brain  # noqa: E402
import brainkeeper as bk  # noqa: E402

FRESH = (dt.date.today()).isoformat()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """A throwaway corpus + a throwaway Claude Code memory tree.

    autouse, deliberately: an earlier version was opt-in, and the tests written before PROPOSALS
    was redirected wrote their fixtures into the REAL Atlas root, where they showed up in the
    control center as three proposals about files that do not exist. Redirection cannot be optional when
    the module under test writes to well-known paths.
    """
    corpus, ccroot = tmp_path / "brain", tmp_path / "cc"
    corpus.mkdir()
    monkeypatch.setenv("ATLAS_BRAIN", str(corpus))
    monkeypatch.setenv("ATLAS_CC_MEMORY", str(ccroot))
    monkeypatch.setattr(bk, "REJECT_LEDGER", tmp_path / "rejected.json")
    monkeypatch.setattr(bk, "PROPOSALS", tmp_path / "proposals.json")
    monkeypatch.setattr(bk, "JUDGED_LEDGER", tmp_path / "judged.json")
    monkeypatch.setattr(bk, "WATERMARK", dt.date.today() - dt.timedelta(days=30))
    return tmp_path


def write_memory(env, project: str, name: str, text: str, age_days: int = 0):
    """Create a Claude Code memory file, optionally backdated behind the watermark."""
    d = env / "cc" / project / "memory"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.md"
    f.write_text(text, encoding="utf-8")
    if age_days:
        old = (dt.datetime.now() - dt.timedelta(days=age_days)).timestamp()
        import os
        os.utime(f, (old, old))
    return f


# ---- the reader --------------------------------------------------------------------------

def test_reads_every_memory_dir(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    write_memory(env, "D--work-almanac", "another", "body")
    ids = {s["id"] for s in bk.sources()}
    assert ids == {"cc-memory:D--work-atlas/a-fact", "cc-memory:D--work-almanac/another"}


def test_the_index_is_not_a_fact(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    write_memory(env, "D--work-atlas", "MEMORY", "- [A fact](a-fact.md) — hook")
    assert [s["title"] for s in bk.sources()] == ["a-fact"]


def test_a_project_dir_without_memory_is_ignored(env):
    # dispatch worktrees get project dirs but write no memory (§14.3); they must not appear
    (env / "cc" / "D--work-atlas-worktrees-job-20260720-120548-d272").mkdir(parents=True)
    write_memory(env, "D--work-atlas", "a-fact", "body")
    assert len(bk.sources()) == 1


def test_a_missing_memory_root_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_CC_MEMORY", str(tmp_path / "nope"))
    assert bk.sources() == []


def test_one_unreadable_file_does_not_break_the_scan(env):
    # the bad file is skipped, not surfaced: something undecodable cannot be a fact. What matters
    # is that the rest of the scan still completes.
    write_memory(env, "D--work-atlas", "good", "body")
    (env / "cc" / "D--work-atlas" / "memory" / "bad.md").write_bytes(b"\xff\xfe\x00broken")
    assert [s["title"] for s in bk.sources()] == ["good"]


def test_the_hash_is_the_fact(env):
    f = write_memory(env, "D--work-atlas", "a-fact", "body")
    first = bk.sources()[0]["hash"]
    f.write_text("body", encoding="utf-8")          # rewritten, same content
    assert bk.sources()[0]["hash"] == first
    f.write_text("body, revised", encoding="utf-8")
    assert bk.sources()[0]["hash"] != first


# ---- what is pending ---------------------------------------------------------------------

def test_a_new_source_is_pending(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    p = bk.pending()
    assert len(p) == 1 and p[0]["state"] == "new"


def test_an_accepted_source_is_never_offered_again(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    s = bk.sources()[0]
    brain.write_note(description="a promoted fact", body="b",
                     source=bk.source_ref(s["id"], s["hash"]))
    assert bk.pending() == []


def test_accepted_state_survives_losing_every_ledger(env):
    # it is derived from the corpus, not stored: files are truth (principle 2)
    write_memory(env, "D--work-atlas", "a-fact", "body")
    s = bk.sources()[0]
    brain.write_note(description="a promoted fact", body="b",
                     source=bk.source_ref(s["id"], s["hash"]))
    bk.REJECT_LEDGER.unlink(missing_ok=True)
    assert bk.pending() == [] and len(bk.harvested()) == 1


def test_a_revised_source_comes_back_as_an_update(env):
    f = write_memory(env, "D--work-atlas", "a-fact", "body")
    s = bk.sources()[0]
    brain.write_note(description="a promoted fact", body="b", name="promoted",
                     source=bk.source_ref(s["id"], s["hash"]))
    f.write_text("body, now revised", encoding="utf-8")
    p = bk.pending()
    assert len(p) == 1 and p[0]["state"] == "update" and p[0]["note"] == "promoted"


def test_a_rejected_source_stays_rejected(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    s = bk.sources()[0]
    bk.mark_rejected(s["id"], s["hash"], reason="about the work, not about the operator")
    assert bk.pending() == []


def test_a_rejected_source_returns_when_it_changes(env):
    f = write_memory(env, "D--work-atlas", "a-fact", "body")
    s = bk.sources()[0]
    bk.mark_rejected(s["id"], s["hash"])
    f.write_text("body, now genuinely about him", encoding="utf-8")
    p = bk.pending()
    assert len(p) == 1 and p[0]["state"] == "new"     # the rejection was of the OLD fact


def test_a_rejection_can_be_undone(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    s = bk.sources()[0]
    bk.mark_rejected(s["id"], s["hash"])
    assert bk.pending() == []
    bk.unreject(s["id"])
    assert len(bk.pending()) == 1


def test_a_corrupt_ledger_reads_as_empty(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    bk.REJECT_LEDGER.write_text("{not json", encoding="utf-8")
    assert bk.rejected() == {} and len(bk.pending()) == 1


# ---- the watermark -----------------------------------------------------------------------

def test_the_watermark_hides_what_was_already_reviewed_by_hand(env):
    write_memory(env, "D--work-atlas", "old-fact", "body", age_days=60)
    assert bk.pending() == []           # predates CP3's review; do not re-litigate 90 files


def test_a_watermarked_file_returns_once_it_is_touched(env):
    f = write_memory(env, "D--work-atlas", "old-fact", "body", age_days=60)
    assert bk.pending() == []
    f.write_text("body, revised today", encoding="utf-8")
    assert len(bk.pending()) == 1


def test_the_watermark_never_hides_an_update(env):
    # harvested long ago, source since revised but still old on disk: an update must survive
    f = write_memory(env, "D--work-atlas", "a-fact", "body", age_days=60)
    s = bk.sources()[0]
    brain.write_note(description="promoted", body="b", name="promoted",
                     source=bk.source_ref(s["id"], "deadbeef"))     # different hash on record
    p = bk.pending()
    assert len(p) == 1 and p[0]["state"] == "update"


# ---- source refs -------------------------------------------------------------------------

@pytest.mark.parametrize("ref,expected", [
    ("cc-memory:D--work-orchard-vault/feedback_no-wildcard-imports@a1b2c3d4",
     ("cc-memory:D--work-orchard-vault/feedback_no-wildcard-imports", "a1b2c3d4")),
    ("capture", None),                       # a hand-typed note has no source ref
    ("seed 2026-08-02 (from Claude Code memory)", None),
    ("", None),
])
def test_source_refs_round_trip(ref, expected):
    assert bk.parse_source_ref(ref) == expected


def test_hand_typed_notes_are_not_mistaken_for_harvests(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    brain.capture("Something the operator typed himself.")
    assert bk.harvested() == {} and len(bk.pending()) == 1


# ---- the panel summary -------------------------------------------------------------------

def test_status_counts_what_the_panel_shows(env):
    write_memory(env, "D--work-atlas", "one", "body")
    write_memory(env, "D--work-atlas", "two", "body two")
    s2 = [s for s in bk.sources() if s["title"] == "two"][0]
    bk.mark_rejected(s2["id"], s2["hash"])
    st = bk.status()
    assert st["ok"] and st["pending"] == 1 and st["new"] == 1 and st["updates"] == 0
    assert st["rejected"] == 1 and st["readers"] == ["cc-memory"] and st["cap"] == 10


def test_status_never_hard_fails(env, monkeypatch):
    monkeypatch.setattr(bk, "pending", lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    st = bk.status()
    assert st["ok"] is False and st["pending"] == 0      # the panel still renders


# ---- the charter (model stubbed: the plumbing is under test, never a model's judgement) ----

def stub_charter(monkeypatch, reply, ok=True):
    seen = {}
    import substrate
    def fake_chat(role, messages, **kw):
        seen["role"] = role
        seen["prompt"] = messages[-1]["content"]
        return {"ok": ok, "text": reply}
    monkeypatch.setattr(substrate, "chat", fake_chat)
    return seen


ACCEPT = """VERDICT: accept
REASON: a standing rule about how he wants things written
DESCRIPTION: Writes commit messages in the imperative mood
TYPE: preference
SCOPE: global
CONFLICTS: NONE
BODY:
He writes commit subjects as commands.

**Applies to:** every repository."""


def test_an_accepted_proposal_is_shaped_like_a_note(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT)
    p = bk.run()["proposals"]
    assert len(p) == 1
    assert p[0]["verdict"] == "accept" and p[0]["type"] == "preference" and p[0]["scope"] == "global"
    assert p[0]["description"].startswith("Writes commit messages")
    assert p[0]["id"] == "cc-memory:D--work-atlas/a-fact"


def test_the_charter_runs_on_its_own_role(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    seen = stub_charter(monkeypatch, ACCEPT)
    bk.run()
    # its own substrate role, so the model is one config value and not the receptionist's
    assert seen["role"] == "brain-keeper"


def test_the_charter_sees_the_whole_corpus(env, monkeypatch):
    brain.write_note(description="An existing belief", body="b", name="existing")
    write_memory(env, "D--work-atlas", "a-fact", "body")
    seen = stub_charter(monkeypatch, ACCEPT)
    bk.run()
    assert "existing: An existing belief" in seen["prompt"]      # so it can spot a duplicate
    flat = seen["prompt"].replace("\n", " ").lower()
    assert "if every project the operator runs were cancelled" in flat      # the subject test, verbatim
    assert "when you hesitate, reject" in flat                      # the precision bias


def test_a_rejection_is_reported_but_never_recorded(env, monkeypatch):
    # a model saying "not about the operator" is a suggestion. Filing it as the operator's decision would let the
    # charter quietly shrink the corpus with nobody watching.
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, "VERDICT: reject\nREASON: about the work, not about him")
    out = bk.run()
    assert out["proposals"] == [] and out["held_back"] == 1
    assert bk.rejected() == {}                 # HIS ledger is untouched, which is the point
    # It does leave the pending list, via the judged CACHE rather than his ledger: the answer is
    # remembered so it is not paid for twice, without ever claiming to be his decision.
    assert bk.pending() == [] and len(bk.judged()) == 1


def test_a_proposal_breaking_the_schema_is_dropped_not_repaired(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT.replace("TYPE: preference", "TYPE: vibes"))
    out = bk.run()
    assert out["proposals"] == [] and len(out["errors"]) == 1
    assert "malformed" in out["errors"][0]["reason"]


def test_an_over_cap_body_is_dropped(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT.replace("He writes commit subjects as commands.", "x" * 1200))
    assert bk.run()["proposals"] == []


def test_an_unparseable_reply_is_an_error_not_a_crash(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, "I think this one is quite interesting, actually.")
    out = bk.run()
    assert out["proposals"] == [] and out["errors"][0]["reason"] == "unparseable reply"


def test_a_dead_model_is_an_error_not_a_crash(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, "", ok=False)
    assert bk.run()["errors"][0]["verdict"] == "error"


def test_a_conflict_must_name_a_real_note(env, monkeypatch):
    brain.write_note(description="An existing belief", body="b", name="existing")
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT.replace("CONFLICTS: NONE", "CONFLICTS: existing"))
    assert bk.run()["proposals"][0]["conflict"] == "existing"
    stub_charter(monkeypatch, ACCEPT.replace("CONFLICTS: NONE", "CONFLICTS: a-note-that-never-existed"))
    assert bk.run()["proposals"][0]["conflict"] == ""      # invented conflicts are dropped


def test_the_cap_holds_and_says_what_it_held_back(env, monkeypatch):
    for i in range(5):
        write_memory(env, "D--work-atlas", f"fact-{i}", f"body {i}")
    stub_charter(monkeypatch, ACCEPT)
    out = bk.run(limit=2)
    assert len(out["proposals"]) == 2 and out["scanned"] == 5 and out["held_back"] == 3


def test_an_update_carries_the_note_it_updates(env, monkeypatch):
    f = write_memory(env, "D--work-atlas", "a-fact", "body")
    s = bk.sources()[0]
    brain.write_note(description="promoted", body="b", name="promoted",
                     source=bk.source_ref(s["id"], s["hash"]))
    f.write_text("body, revised", encoding="utf-8")
    stub_charter(monkeypatch, ACCEPT)
    p = bk.run()["proposals"][0]
    assert p["state"] == "update" and p["note"] == "promoted"


def test_running_the_charter_writes_nothing_to_the_corpus(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT)
    bk.run()
    assert brain.list_notes() == []          # proposals only; §6 holds


# ---- the judged cache: pay for an answer once ---------------------------------------------

REJECT = "VERDICT: reject\nREASON: about the work, not about him"


def test_a_charter_rejection_is_not_re_judged(env, monkeypatch):
    # without this the source stays pending forever and every run pays again for the same answer
    write_memory(env, "D--work-atlas", "a-fact", "body")
    calls = []
    import substrate
    monkeypatch.setattr(substrate, "chat", lambda *a, **k: (calls.append(1), {"ok": True, "text": REJECT})[1])
    bk.run()
    assert len(calls) == 1 and bk.pending() == []
    bk.run()
    assert len(calls) == 1                      # second run costs nothing


def test_the_cache_is_not_the_operators_rejection(env, monkeypatch):
    # §14.6: the charter's own "no" must never be filed as his judgement
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, REJECT)
    bk.run()
    assert bk.rejected() == {}                  # his ledger stays his
    assert len(bk.judged()) == 1


def test_a_changed_source_is_judged_again(env, monkeypatch):
    f = write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, REJECT)
    bk.run()
    assert bk.pending() == []
    f.write_text("body, rewritten and now about him", encoding="utf-8")
    assert len(bk.pending()) == 1               # the cache expires with the hash


def test_an_error_is_never_cached(env, monkeypatch):
    # a timed-out call is not an answer; retrying it is the right behaviour
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, "", ok=False)
    bk.run()
    assert bk.judged() == {} and len(bk.pending()) == 1


def test_a_judgement_can_be_forced_open_again(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, REJECT)
    bk.run()
    s = bk.sources()[0]
    bk.unjudge(s["id"])
    assert len(bk.pending()) == 1


def test_a_corrupt_cache_reads_as_empty(env):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    bk.JUDGED_LEDGER.write_text("{not json", encoding="utf-8")
    assert bk.judged() == {} and len(bk.pending()) == 1


def test_accepting_clears_the_cache_entry(env, monkeypatch):
    f = write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, REJECT)
    bk.run()                                     # cached as "no"
    f.write_text("body, now clearly about him", encoding="utf-8")
    stub_charter(monkeypatch, ACCEPT)
    bk.run()
    p = bk.load_proposals()[0]
    bk.accept(p["id"], p["description"], p["body"], p["type"], p["scope"])
    assert bk.judged() == {}                     # the corpus settles it now


# ---- the review loop: propose -> the operator decides -> the source is settled --------------------

def test_a_run_queues_its_proposals_for_review(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT)
    out = bk.run()
    assert out["queued"] == 1 and len(bk.load_proposals()) == 1
    assert bk.status()["queued"] == 1          # the Pulse rail reads this


def test_re_running_replaces_rather_than_duplicates(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT)
    bk.run(); bk.run()
    assert len(bk.load_proposals()) == 1       # same source, one card


def test_accepting_writes_the_note_and_settles_the_source(env, monkeypatch):
    # THE round trip: once accepted, the source must never be offered again, and that has to hold
    # from the corpus alone (§14.5).
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT)
    bk.run()
    p = bk.load_proposals()[0]
    res = bk.accept(p["id"], p["description"], p["body"], p["type"], p["scope"])
    assert res["ok"]
    note = brain.read_note(res["name"])
    assert note["type"] == "preference" and note["source"] == bk.source_ref(p["id"], p["hash"])
    assert bk.load_proposals() == []
    assert bk.pending() == []                  # settled
    assert bk.rejected() == {}                 # accepting is not a rejection


def test_the_operator_can_edit_a_proposal_before_accepting(env, monkeypatch):
    # the card is editable, and what he accepts is what gets written, not what was proposed
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT)
    bk.run()
    p = bk.load_proposals()[0]
    res = bk.accept(p["id"], "A description he rewrote", "A body he rewrote.", "taste", "atlas")
    note = brain.read_note(res["name"])
    assert note["description"] == "A description he rewrote" and note["type"] == "taste"


def test_accepting_an_update_overwrites_the_linked_note(env, monkeypatch):
    f = write_memory(env, "D--work-atlas", "a-fact", "body")
    s = bk.sources()[0]
    brain.write_note(description="the original", body="original body", name="promoted",
                     source=bk.source_ref(s["id"], s["hash"]))
    f.write_text("body, revised", encoding="utf-8")
    stub_charter(monkeypatch, ACCEPT)
    bk.run()
    p = bk.load_proposals()[0]
    bk.accept(p["id"], p["description"], "the revised body", p["type"], p["scope"],
              name="promoted", overwrite=True)
    assert len(brain.list_notes()) == 1                    # updated, not duplicated
    assert brain.read_note("promoted")["body"] == "the revised body"
    assert bk.pending() == []                              # the new hash is on record


def test_rejecting_records_THE_OPERATORS_call_and_settles_the_source(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT)
    bk.run()
    p = bk.load_proposals()[0]
    bk.reject(p["id"], reason="not really about me")
    assert bk.load_proposals() == []
    assert bk.rejected()[p["id"]]["reason"] == "not really about me"
    assert bk.pending() == []
    assert brain.list_notes() == []            # nothing written


def test_deciding_on_a_stale_proposal_is_refused_not_guessed(env, monkeypatch):
    write_memory(env, "D--work-atlas", "a-fact", "body")
    stub_charter(monkeypatch, ACCEPT)
    bk.run()
    p = bk.load_proposals()[0]
    bk.reject(p["id"])
    with pytest.raises(brain.BrainError, match="no longer in the queue"):
        bk.accept(p["id"], "d", "b")


def test_a_corrupt_proposal_file_reads_as_empty(env):
    bk.PROPOSALS.write_text("[not json", encoding="utf-8")
    assert bk.load_proposals() == []           # a lost batch costs one re-run, nothing more


# ---- reader 2: judged in the moment, because the turn is gone a second later ---------------

@pytest.mark.parametrize("text,worth", [
    # first-person assertions: the shape a fact about him actually takes
    ("I never approve a pull request from my phone, the diff is unreadable", True),
    ("From now on, always run the tests before showing me anything", True),
    ("I prefer the smallest change first, then we see if it earns more", True),
    ("Remember that my German is B2 now, not B1", True),
    # not about him: questions, task instructions, reactions
    ("what is almanac's status", False),
    ("ok", False),
    ("go ahead", False),
    ("Add a button to the finance panel", False),
    ("Fix the failing test in dashboard/test_worker_gate.py please", False),
    # questions containing "I": the most common turn shape in a working session, and asking is
    # not telling. Rejected before the model rather than by it.
    ("Should I click on Run brain-keeper to run the test?", False),
    ("Can I keep this session open and come back tomorrow?", False),
    ("What should I be doing right now?", False),
    # a declarative that merely ends in a question mark still counts
    ("I never work before coffee, could we schedule around that?", True),
])
def test_the_pre_filter_is_blunt_in_the_safe_direction(text, worth):
    # wrong-in-the-safe-direction: a turn it lets through costs one call that probably rejects,
    # a turn it drops costs nothing and the operator can still capture it himself
    assert bk.turn_is_worth_judging(text) is worth


def test_a_turn_that_reveals_something_becomes_a_proposal(env, monkeypatch):
    seen = stub_charter(monkeypatch, ACCEPT)
    p = bk.observe_turn("brainstormer", "I never approve a pull request from my phone, ever")
    assert p and p["id"].startswith("atlas-chat:brainstormer/")
    assert len(bk.load_proposals()) == 1
    # the charter is told it is reading RAW speech, not a distilled note
    assert "RAW MESSAGE" in seen["prompt"]


def test_an_ordinary_turn_never_reaches_the_model(env, monkeypatch):
    called = []
    import substrate
    monkeypatch.setattr(substrate, "chat", lambda *a, **k: called.append(1))
    assert bk.observe_turn("receptionist", "what is almanac's status") is None
    assert called == []                      # the pre-filter paid for nothing


def test_saying_the_same_thing_twice_proposes_once(env, monkeypatch):
    stub_charter(monkeypatch, ACCEPT)
    said = "I never approve a pull request from my phone, ever"
    bk.observe_turn("brainstormer", said)
    bk.observe_turn("brainstormer", said)
    assert len(bk.load_proposals()) == 1     # identity is the hash of what he said


def test_a_turn_the_charter_rejects_is_not_asked_again(env, monkeypatch):
    stub_charter(monkeypatch, REJECT)
    said = "I think we should probably look at the finance panel at some point"
    assert bk.observe_turn("brainstormer", said) is None
    assert len(bk.judged()) == 1
    calls = []
    import substrate
    monkeypatch.setattr(substrate, "chat", lambda *a, **k: (calls.append(1), {"ok": True, "text": REJECT})[1])
    bk.observe_turn("brainstormer", said)
    assert calls == []                       # settled; the cache answers


def test_it_stops_when_the_review_grid_is_already_full(env, monkeypatch):
    bk._save_proposals([{"id": f"x{i}", "hash": "h"} for i in range(bk.MAX_PROPOSALS)])
    stub_charter(monkeypatch, ACCEPT)
    assert bk.observe_turn("brainstormer", "I never approve from my phone, ever") is None


def test_a_broken_charter_never_disturbs_the_conversation(env, monkeypatch):
    import substrate
    monkeypatch.setattr(substrate, "chat",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model down")))
    assert bk.observe_turn("brainstormer", "I never approve from my phone, ever") is None


def test_reader_2_stores_no_transcript(env, monkeypatch):
    # the whole reason it judges in the moment: nothing is kept except a proposal the operator may accept
    stub_charter(monkeypatch, REJECT)
    said = "I think the finance panel could use some work, honestly"
    bk.observe_turn("receptionist", said)
    on_disk = "".join(p.read_text(encoding="utf-8") for p in
                      (bk.JUDGED_LEDGER, bk.REJECT_LEDGER, bk.PROPOSALS) if p.exists())
    assert said not in on_disk               # only the hash of it survives


# ---- disjoint territories: one turn, one reader (§14.10) ----------------------------------

ATLAS_SID = "8634276e-5724-4932-9496-69a281bc045b"


def write_memory_with_session(env, project, name, text, session):
    """A memory file as Claude Code actually writes one, carrying the session that produced it."""
    body = (f"---\nname: {name}\ndescription: d\nmetadata:\n  node_type: memory\n"
            f"  originSessionId: {session}\n---\n\n{text}\n")
    return write_memory(env, project, name, body)


def test_the_reader_notices_which_session_wrote_a_file(env):
    write_memory_with_session(env, "D--work-atlas", "a-fact", "body", ATLAS_SID)
    assert bk.sources()[0]["origin_session"] == ATLAS_SID


def test_a_file_from_atlas_own_conversation_is_left_to_reader_2(env, monkeypatch):
    # reader 2 judged that turn as the operator typed it; reader 1 must not judge it a second time
    import atlas_sessions
    monkeypatch.setattr(atlas_sessions, "SESSIONS", env / "sessions.json")
    atlas_sessions.record(ATLAS_SID)
    write_memory_with_session(env, "D--work-atlas", "from-control center", "body", ATLAS_SID)
    assert bk.pending() == []


def test_a_file_from_a_terminal_session_is_reader_1s(env, monkeypatch):
    import atlas_sessions
    monkeypatch.setattr(atlas_sessions, "SESSIONS", env / "sessions.json")
    atlas_sessions.record(ATLAS_SID)
    write_memory_with_session(env, "D--work-atlas", "from-terminal", "body",
                              "11111111-2222-3333-4444-555555555555")
    assert [p["title"] for p in bk.pending()] == ["from-terminal"]


def test_a_file_with_no_session_recorded_stays_reader_1s(env, monkeypatch):
    # older memory files predate this; they must not silently vanish from the queue
    import atlas_sessions
    monkeypatch.setattr(atlas_sessions, "SESSIONS", env / "sessions.json")
    atlas_sessions.record(ATLAS_SID)
    write_memory(env, "D--work-atlas", "old-style", "no frontmatter at all")
    assert len(bk.pending()) == 1


def test_losing_the_session_ledger_degrades_to_overlap_not_error(env, monkeypatch):
    import atlas_sessions
    monkeypatch.setattr(atlas_sessions, "SESSIONS", env / "nope.json")
    write_memory_with_session(env, "D--work-atlas", "a-fact", "body", ATLAS_SID)
    assert len(bk.pending()) == 1     # the old behaviour, never a crash


def test_session_ids_are_recorded_once_and_capped(env, monkeypatch):
    import atlas_sessions
    monkeypatch.setattr(atlas_sessions, "SESSIONS", env / "sessions.json")
    monkeypatch.setattr(atlas_sessions, "KEEP", 3)
    for sid in ["a", "b", "c", "d"]:
        atlas_sessions.record(sid)
    atlas_sessions.record("d")                       # a repeat adds nothing
    assert atlas_sessions.known() == {"b", "c", "d"}
    atlas_sessions.record(None); atlas_sessions.record("")
    assert atlas_sessions.known() == {"b", "c", "d"}


def test_a_corrupt_session_ledger_reads_as_empty(env, monkeypatch):
    import atlas_sessions
    monkeypatch.setattr(atlas_sessions, "SESSIONS", env / "sessions.json")
    (env / "sessions.json").write_text("[not json", encoding="utf-8")
    assert atlas_sessions.known() == set()


# ---- the hard guarantee ------------------------------------------------------------------

def test_nothing_here_ever_writes_to_a_source(env):
    f = write_memory(env, "D--work-atlas", "a-fact", "body")
    before = (f.read_bytes(), f.stat().st_mtime)
    s = bk.sources()[0]
    bk.pending(); bk.status(); bk.mark_rejected(s["id"], s["hash"]); bk.unreject(s["id"])
    assert (f.read_bytes(), f.stat().st_mtime) == before
