"""
Brain retrieval — the read path into the receptionist (docs/brain.md §7, §8).

The properties that matter here are not "does it find the right note" (that is what the SOAK
measures, with the operator judging). They are the structural ones the design leans on:

  - the candidate set is deterministic: every note is always visible to the selection pass;
  - a name the model invents is dropped, so retrieval can surface the wrong REAL note but never
    a fabricated one;
  - selection failure degrades to deterministic matching, not to nothing;
  - the chips report what retrieval SUPPLIED, which is what makes "wrong note" measure retrieval.

The model call is stubbed throughout: these assert the plumbing, never a model's judgement.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import brain  # noqa: E402
import assistant  # noqa: E402


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_BRAIN", str(tmp_path))
    brain.write_note(description="Never use wildcard imports in committed code",
                     body="They break static analysis and hide name collisions.",
                     name="no-wildcard-imports", type="taste", scope="global")
    brain.write_note(description="Reviews merged work on Friday afternoons",
                     body="Never Monday - the week's evidence is not in yet.",
                     name="review-rhythm", type="fact", scope="global")
    brain.write_note(description="Prefers a rollback path over a forward fix",
                     body="Restore service first, diagnose second.",
                     name="prefers-a-rollback-path", type="decision-pattern", scope="global")
    return tmp_path


def stub_select(monkeypatch, reply, ok=True):
    """Replace the selection call. Records what the model was shown, so the deterministic-candidate
    property can be asserted rather than assumed."""
    seen = {}
    def fake_chat(role, messages, **kw):
        seen["prompt"] = messages[-1]["content"]
        return {"ok": ok, "text": reply}
    monkeypatch.setattr(assistant.substrate, "chat", fake_chat)
    return seen


# ---- the deterministic candidate set ----------------------------------------------------

def test_every_note_is_visible_to_the_selection_pass(corpus, monkeypatch):
    seen = stub_select(monkeypatch, "no-wildcard-imports")
    assistant.brain_evidence("how should I write the launch copy?")
    for name in ("no-wildcard-imports", "review-rhythm", "prefers-a-rollback-path"):
        assert name in seen["prompt"]          # no pre-filter narrows what the model may choose from


def test_selection_returns_whole_notes(corpus, monkeypatch):
    stub_select(monkeypatch, "no-wildcard-imports")
    notes = assistant.brain_evidence("how should I write the launch copy?")
    assert [n["name"] for n in notes] == ["no-wildcard-imports"]
    assert notes[0]["body"] == "They break static analysis and hide name collisions."


def test_a_fabricated_name_is_dropped(corpus, monkeypatch):
    # THE guard: retrieval may surface the wrong REAL note, in front of the operator. It can never
    # surface one that does not exist.
    stub_select(monkeypatch, "no-wildcard-imports, a-note-that-was-never-written")
    assert [n["name"] for n in assistant.brain_evidence("q")] == ["no-wildcard-imports"]


def test_none_means_none(corpus, monkeypatch):
    stub_select(monkeypatch, "NONE")
    assert assistant.brain_evidence("what is 2 + 2") == []


def test_selection_is_capped(corpus, monkeypatch):
    stub_select(monkeypatch, "no-wildcard-imports, review-rhythm, prefers-a-rollback-path")
    assert len(assistant.brain_evidence("q", k=2)) == 2      # retrieval injects WHOLE notes


def test_duplicate_picks_collapse(corpus, monkeypatch):
    stub_select(monkeypatch, "review-rhythm, review-rhythm")
    assert [n["name"] for n in assistant.brain_evidence("q")] == ["review-rhythm"]


def test_model_formatting_noise_is_tolerated(corpus, monkeypatch):
    stub_select(monkeypatch, "`review-rhythm`,\n [no-wildcard-imports]")
    assert sorted(n["name"] for n in assistant.brain_evidence("q")) == [
        "no-wildcard-imports", "review-rhythm"]


# ---- degradation ------------------------------------------------------------------------

def test_selection_failure_falls_back_to_deterministic_matching(corpus, monkeypatch):
    stub_select(monkeypatch, "", ok=False)
    notes = assistant.brain_evidence("what does his review week look like?")
    assert [n["name"] for n in notes] == ["review-rhythm"]   # degrades to search(), not to nothing


def test_an_empty_corpus_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_BRAIN", str(tmp_path))
    assert assistant.brain_evidence("anything") == []


def test_a_broken_brain_never_breaks_the_receptionist(corpus, monkeypatch):
    monkeypatch.setattr(brain, "index", lambda: (_ for _ in ()).throw(OSError("disk gone")))
    assert assistant.brain_evidence("q") == []      # enrichment, never a dependency


# ---- deterministic search (the v1 interface + the fallback) -----------------------------

def test_search_scores_description_above_body(corpus):
    brain.write_note(description="A note about nothing in particular",
                     body="Mentions wildcard imports only in passing.", name="body-only")
    hits = [n["name"] for n in brain.search("wildcard")]
    assert hits[0] == "no-wildcard-imports" and "body-only" in hits


def test_search_ignores_question_words(corpus):
    # "what should I do about..." must not match every note through its filler
    assert brain.search("what should I do") == []


# ---- what the chips report --------------------------------------------------------------

def test_chips_report_what_retrieval_supplied(corpus, monkeypatch):
    stub_select(monkeypatch, "review-rhythm")
    cited = assistant._cited(assistant.brain_evidence("q"))
    assert cited == [{"name": "review-rhythm",
                      "description": "Reviews merged work on Friday afternoons"}]


# ---- how the notes are injected ---------------------------------------------------------

def test_rules_are_injected_as_constraints_not_background(corpus):
    # Fed as background, a style rule got cited and violated in the same reply: the model read
    # a style rule as something true ABOUT the operator rather than binding on the answer.
    block = assistant.brain_block([brain.read_note("no-wildcard-imports")])
    assert "HARD RULES" in block
    assert "override your own habits" in block
    assert "check it against every rule" in block.replace("\n", " ")
    assert "WHAT YOU KNOW" not in block          # a pure-rule set gets no background section


def test_facts_stay_context(corpus):
    block = assistant.brain_block([brain.read_note("review-rhythm")])
    assert "WHAT YOU KNOW ABOUT THE USER" in block and "HARD RULES" not in block


def test_a_mixed_set_is_split(corpus):
    block = assistant.brain_block([brain.read_note("no-wildcard-imports"),
                                   brain.read_note("review-rhythm")])
    assert block.index("HARD RULES") < block.index("WHAT YOU KNOW")   # rules lead
    assert "RULE [no-wildcard-imports]" in block
    assert "[review-rhythm]" in block


def test_every_rule_type_binds(corpus):
    for t in ("taste", "preference", "constraint", "decision-pattern"):
        assert "HARD RULES" in assistant.brain_block([{"name": "n", "description": "d",
                                                       "body": "b", "type": t}])


def test_an_unclassified_note_is_context_not_a_rule(corpus):
    # capture leaves `type` unset by design; an unclassified note must not silently acquire
    # the force of a standing instruction
    block = assistant.brain_block([{"name": "n", "description": "d", "body": "b", "type": ""}])
    assert "HARD RULES" not in block and "WHAT YOU KNOW ABOUT THE USER" in block


def test_the_machinery_obeys_the_rule_it_carries(corpus):
    # The corpus carries typography rules, and the block that introduces them was itself written
    # with the very character one of them forbids, directly above the rule. A mixed signal at the
    # exact moment the model decides whether the rule binds, so the machinery has to stay clean.
    block = assistant.brain_block([brain.read_note("no-wildcard-imports")])
    prompt_text = block.replace(brain.read_note("no-wildcard-imports")["body"], "")
    assert "—" not in prompt_text
    assert "—" not in assistant._BRAIN_SELECT


def test_the_receptionist_cannot_claim_to_have_remembered(corpus):
    # Live, on the first reader-2 turn: "the no-Friday-merge rule is now locked in memory". It was
    # not. Nothing is written until the operator accepts a proposal, and an agent that says otherwise
    # teaches him to trust a corpus that does not contain what he thinks it does.
    sys_prompt = assistant.SYSTEM.lower()
    assert "cannot write to the user's personal knowledge base" in sys_prompt
    assert "never say or imply that you" in sys_prompt


def test_the_block_never_leaks_its_own_existence(corpus):
    block = assistant.brain_block([brain.read_note("review-rhythm")])
    assert "never mention that you were given notes" in block


# ---- the soak ledger ---------------------------------------------------------------------

def test_feedback_is_counted_not_remembered(corpus, monkeypatch):
    monkeypatch.setattr(brain, "FEEDBACK_LOG", corpus / "soak.jsonl")
    brain.log_feedback("wrong", "how should I write the copy?", ["review-rhythm"])
    brain.log_feedback("missing", "where do I live?")
    s = brain.soak_stats()
    assert (s["wrong"], s["missing"]) == (1, 1)
    # judgements alone leave the denominator empty — answers are counted separately (§8)
    assert s["answers"] == 0


def test_feedback_never_enters_the_corpus(corpus, monkeypatch):
    # observations about retrieval are not facts about the operator; mixing them would break the one
    # rule that makes the corpus trustworthy
    monkeypatch.setattr(brain, "FEEDBACK_LOG", corpus / "soak.jsonl")
    before = {n["name"] for n in brain.list_notes()}
    brain.log_feedback("wrong", "q", ["review-rhythm"])
    assert {n["name"] for n in brain.list_notes()} == before


def test_unknown_feedback_kind_is_rejected(corpus, monkeypatch):
    monkeypatch.setattr(brain, "FEEDBACK_LOG", corpus / "soak.jsonl")
    with pytest.raises(brain.BrainError, match="Unknown feedback kind"):
        brain.log_feedback("meh", "q")
