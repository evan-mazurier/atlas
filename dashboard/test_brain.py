"""
Brain store tests — the deterministic write path (docs/brain.md §6).

Every test runs against a throwaway corpus via ATLAS_BRAIN, so nothing here can touch the real
D:\\work\\brain. The properties under test are the ones the whole design rests on:

  - a write that violates the contract is REJECTED, with a message a human can act on;
  - a write never silently clobbers a different fact;
  - a note round-trips through disk unchanged;
  - an unclassified capture is still a first-class, retrievable note;
  - one corrupt file degrades to a record and never blanks the library.

git is not exercised (the corpus dirs here are not repos) — `_commit` is a no-op without .git,
which is exactly the "a missing git never fails a write" guarantee.
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


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_BRAIN", str(tmp_path))
    return tmp_path


# ---- validation: the gate on the corpus's shape -----------------------------------------

def test_rejects_missing_description(corpus):
    with pytest.raises(brain.BrainError, match="description"):
        brain.write_note(description="", body="a fact")


def test_rejects_empty_body(corpus):
    with pytest.raises(brain.BrainError, match="body"):
        brain.write_note(description="something", body="   ")


def test_rejects_body_over_the_cap(corpus):
    with pytest.raises(brain.BrainError) as e:
        brain.write_note(description="too long", body="x" * (brain.BODY_MAX_CHARS + 1))
    # the message must name the number AND the way out — it's shown inline where it was typed
    assert str(brain.BODY_MAX_CHARS) in str(e.value)
    assert "two notes" in str(e.value)


def test_accepts_body_exactly_at_the_cap(corpus):
    res = brain.write_note(description="at the cap", body="x" * brain.BODY_MAX_CHARS)
    assert res["ok"]


def test_rejects_unknown_type(corpus):
    with pytest.raises(brain.BrainError, match="Unknown type"):
        brain.write_note(description="d", body="b", type="vibes")


def test_rejects_unknown_scope(corpus):
    # §13.1 resolved at build time: scope is CONSTRAINED to registry ids + global, so a typo
    # cannot create a scope that silently filters to nothing.
    with pytest.raises(brain.BrainError, match="Unknown scope"):
        brain.write_note(description="d", body="b", scope="atlus")


def test_accepts_every_registry_scope(corpus):
    allowed = brain.scopes()
    assert "global" in allowed and "almanac" in allowed
    assert "orchard" in allowed          # umbrella parents count, not just leaf project ids
    # `atlas` is not a registry project (it's the control plane) but IS a scope — a large share of
    # the corpus is taste about how Atlas gets built. Deriving from the registry alone loses those.
    assert "atlas" in allowed
    for s in allowed[:4]:
        brain.write_note(description=f"scoped to {s}", body="b", scope=s)


def test_rejects_overlong_description(corpus):
    with pytest.raises(brain.BrainError, match="characters"):
        brain.write_note(description="d" * (brain.DESC_MAX_CHARS + 1), body="b")


# ---- round-trip -------------------------------------------------------------------------

def test_note_round_trips(corpus):
    brain.write_note(description="Never commit generated build artifacts",
                     body="They churn the diff and mask the change that matters.\n\n**Why:** a "
                          "reviewer cannot see intent through noise. Related: [[build-output-policy]]",
                     type="taste", scope="atlas", confidence="high", source="session 2026-07-05")
    rec = brain.read_note("never-commit-generated-build-artifacts")
    assert rec["description"] == "Never commit generated build artifacts"
    assert rec["type"] == "taste" and rec["scope"] == "atlas" and rec["confidence"] == "high"
    assert rec["source"] == "session 2026-07-05"
    assert rec["created"] == dt.date.today().isoformat()
    assert rec["links"] == ["build-output-policy"]        # wikilinks are a relatedness signal (§5)
    assert not rec["unclassified"] and not rec["stale"]


def test_slug_derives_from_description_when_name_is_absent(corpus):
    res = brain.write_note(description="Decides by shipping the smallest change first", body="b")
    assert res["name"] == "decides-shipping-smallest-change-first"
    assert (corpus / f"{res['name']}.md").exists()


# ---- the naming convention: the right name from the start, so rename never has to exist ----

@pytest.mark.parametrize("description,expected", [
    # 1. first clause only — the head of a sentence carries the fact, the tail qualifies it
    ("Adds an index at the point of need, never ahead of a measured slow query.",
     "adds-an-index-at-the-point-of-need"),
    ("Says orchard ops, not ops, when speaking to the voice layer",
     "says-orchard-ops"),
    # 2. no clause boundary + too long → stop-words dropped, content words kept in order
    ("Never commit generated build artifacts to a shared branch",
     "never-commit-generated-build-artifacts"),
    ("Prefers a small reversible change over a large rewrite",
     "prefers-small-reversible-change-large"),
    # 3. short enough already → left exactly as written
    ("Log lines must name the fix", "log-lines-must-name-the-fix"),
])
def test_derive_name_reads_like_a_human_picked_it(description, expected):
    assert brain.derive_name(description) == expected


def test_a_derived_name_never_ends_in_a_dangling_connector(corpus):
    res = brain.capture("Adds an index at the point of need, never ahead of a measured slow query.")
    assert res["name"].split("-")[-1] not in brain._STOPWORDS
    assert len(res["name"]) <= brain.NAME_MAX_CHARS


def test_slugify_never_truncates(corpus):
    # slugify is the LOOKUP path: a lookup that shortens its argument can't find a long filename
    long = "a-very-long-hand-picked-name-that-runs-well-past-the-derived-name-cap"
    assert brain.slugify(long) == long
    brain.write_note(description="d", body="b", name=long)
    assert brain.read_note(long) is not None


def test_an_overlong_explicit_name_is_rejected_not_silently_cut(corpus):
    with pytest.raises(brain.BrainError, match="under 80"):
        brain.write_note(description="d", body="b", name="x" * 81)


# ---- aliases: why renaming never has to exist -------------------------------------------

def test_an_alias_is_a_second_link_target(corpus):
    brain.write_note(description="Never commit generated build artifacts", body="b",
                     aliases=["no-build-artifacts"])
    # the note keeps its derived name AND answers to the short handle — nothing was renamed,
    # so no inbound link can dangle
    assert brain.read_note("never-commit-generated-build-artifacts") is not None
    assert brain.read_note("no-build-artifacts")["name"] == "never-commit-generated-build-artifacts"
    assert brain.resolve_link("no-build-artifacts") == "never-commit-generated-build-artifacts"


def test_aliases_survive_a_round_trip_in_obsidian_list_form(corpus):
    brain.write_note(description="d", body="b", name="n1", aliases=["short", "shorter"])
    assert "aliases: [short, shorter]" in (corpus / "n1.md").read_text(encoding="utf-8")
    assert brain.read_note("n1")["aliases"] == ["short", "shorter"]


def test_an_alias_cannot_hijack_another_notes_link(corpus):
    brain.write_note(description="first", body="b", name="taken-name")
    with pytest.raises(brain.BrainError, match="already points at"):
        brain.write_note(description="second", body="b", name="other", aliases=["taken-name"])


def test_a_dangling_link_resolves_to_none(corpus):
    assert brain.resolve_link("never-written") is None


def test_reconfirm_preserves_aliases(corpus):
    brain.write_note(description="d", body="b", name="n1", aliases=["short"])
    brain.reconfirm("n1")
    assert brain.read_note("n1")["aliases"] == ["short"]


def test_explicit_name_wins(corpus):
    res = brain.write_note(description="anything", body="b", name="no-build-artifacts")
    assert res["name"] == "no-build-artifacts"


def test_frontmatter_key_order_is_stable(corpus):
    # written by hand in a fixed order (not yaml.dump) so git diffs stay readable
    brain.write_note(description="a fact", body="b", type="fact", scope="global")
    text = (corpus / "a-fact.md").read_text(encoding="utf-8")
    keys = [ln.split(":")[0] for ln in text.splitlines()[1:9] if ln and not ln.startswith("---")]
    assert keys == ["name", "description", "type", "scope", "confidence", "source", "created", "reviewed"]


# ---- never clobber ----------------------------------------------------------------------

def test_colliding_slug_never_overwrites(corpus):
    a = brain.write_note(description="Prefers a rollback path", body="first fact")
    b = brain.write_note(description="Prefers a rollback path", body="a DIFFERENT fact")
    assert a["name"] != b["name"] and b["name"].endswith("-2")
    assert brain.read_note(a["name"])["body"] == "first fact"


def test_edit_preserves_created_and_stamps_reviewed(corpus):
    brain.write_note(description="d", body="original", name="n1")
    p = corpus / "n1.md"
    p.write_text(p.read_text(encoding="utf-8").replace(f"created: {dt.date.today().isoformat()}",
                                                       "created: 2025-01-01"), encoding="utf-8")
    brain.write_note(description="d", body="revised", name="n1", overwrite=True)
    rec = brain.read_note("n1")
    assert rec["body"] == "revised"
    assert rec["created"] == "2025-01-01"                        # history is not rewritten
    assert rec["reviewed"] == dt.date.today().isoformat()        # editing IS re-confirming (§9)


def test_edit_of_a_missing_note_is_rejected(corpus):
    with pytest.raises(brain.BrainError, match="No note named"):
        brain.write_note(description="d", body="b", name="ghost", overwrite=True)


# ---- capture: the ⌘K / voice path -------------------------------------------------------

def test_capture_asks_for_nothing_but_the_text(corpus):
    res = brain.capture("Fails the build on a lint error. It keeps the main branch releasable.")
    rec = brain.read_note(res["name"])
    assert rec["description"] == "Fails the build on a lint error."                    # first sentence
    assert rec["body"].endswith("It keeps the main branch releasable.")                              # full text kept
    assert rec["type"] == "" and rec["scope"] == ""                                    # unset, deliberately
    assert rec["unclassified"]


def test_an_unclassified_note_is_still_fully_retrievable(corpus):
    # the §11 claim this design rests on: retrieval v1 matches on `description`, so classification
    # is a browsing nicety — never the note's basic usefulness.
    brain.capture("Adds an index at the point of need, never ahead of it.")
    assert any("index" in r["description"] for r in brain.index())


def test_capture_truncates_a_long_first_sentence_into_the_description(corpus):
    long = "x" * (brain.DESC_MAX_CHARS + 50)
    rec = brain.read_note(brain.capture(long)["name"])
    assert len(rec["description"]) <= brain.DESC_MAX_CHARS
    assert rec["body"] == long          # the body keeps everything the description had to drop


def test_capture_rejects_empty_text(corpus):
    with pytest.raises(brain.BrainError, match="Nothing to remember"):
        brain.capture("   ")


# ---- forget -----------------------------------------------------------------------------

def test_delete_is_gone(corpus):
    brain.write_note(description="a mistaken belief", body="b", name="gone")
    brain.delete_note("gone")
    assert brain.read_note("gone") is None
    assert not (corpus / "gone.md").exists()          # no tombstone, no archive-by-default (§9)
    assert brain.index() == []


def test_delete_of_a_missing_note_is_rejected(corpus):
    with pytest.raises(brain.BrainError, match="No note named"):
        brain.delete_note("never-existed")


# ---- staleness --------------------------------------------------------------------------

def test_old_notes_go_stale(corpus):
    brain.write_note(description="an old belief", body="b", name="old")
    p = corpus / "old.md"
    old = (dt.date.today() - dt.timedelta(days=brain.STALE_DAYS + 1)).isoformat()
    p.write_text(p.read_text(encoding="utf-8").replace(
        f"reviewed: {dt.date.today().isoformat()}", f"reviewed: {old}"), encoding="utf-8")
    assert brain.read_note("old")["stale"]
    brain.reconfirm("old")                             # one click: "yes, still true"
    rec = brain.read_note("old")
    assert not rec["stale"] and rec["body"] == "b"


# ---- defensive reads --------------------------------------------------------------------

def test_a_corrupt_note_degrades_and_never_blanks_the_library(corpus):
    brain.write_note(description="a good note", body="b")
    (corpus / "broken.md").write_text("---\nname: broken\n  bad: [unclosed\n---\nstill a body\n",
                                      encoding="utf-8")
    notes = brain.list_notes()
    assert len(notes) == 2
    broken = next(n for n in notes if n["name"] == "broken")
    assert broken["description"] == "still a body"     # falls back to the first body line
    assert any(n["description"] == "a good note" for n in notes)


def test_a_note_with_no_frontmatter_is_still_readable(corpus):
    (corpus / "plain.md").write_text("Just a naked fact.\n", encoding="utf-8")
    rec = brain.read_note("plain")
    assert rec["description"] == "Just a naked fact." and rec["unclassified"]


def test_readme_is_not_a_note(corpus):
    (corpus / "README.md").write_text(brain.README, encoding="utf-8")
    brain.write_note(description="a real note", body="b")
    assert [n["name"] for n in brain.list_notes()] == ["a-real-note"]


def test_missing_corpus_reads_as_empty_not_an_error(corpus, monkeypatch):
    monkeypatch.setenv("ATLAS_BRAIN", str(corpus / "does-not-exist"))
    assert brain.list_notes() == [] and brain.index() == []


# ---- init -------------------------------------------------------------------------------

def test_init_is_idempotent(corpus):
    a = brain.init_repo()
    b = brain.init_repo()
    assert a["ok"] and b["ok"]
    assert (corpus / "README.md").exists() and (corpus / ".gitignore").exists()
    assert ".obsidian/" in (corpus / ".gitignore").read_text(encoding="utf-8")


def test_stats_counts_what_the_panel_shows(corpus):
    brain.write_note(description="classified", body="b", type="taste", scope="global")
    brain.capture("unclassified thing")
    s = brain.stats()
    assert s["count"] == 2 and s["unclassified"] == 1 and s["stale"] == 0
    assert "global" in s["scopes"] and "taste" in s["types"]


# ---- the soak's readout (docs/brain.md §8) ----------------------------------------------

@pytest.fixture
def soak_log(tmp_path, monkeypatch):
    """Point the ledger at a throwaway file — the real one is live operational state."""
    monkeypatch.setattr(brain, "FEEDBACK_LOG", tmp_path / "soak.jsonl")
    return brain.FEEDBACK_LOG


def test_soak_reads_as_zero_before_anything_happens(soak_log):
    s = brain.soak_stats()
    assert s == {"answers": 0, "with_notes": 0, "wrong": 0, "missing": 0,
                 "target": brain.SOAK_TARGET}


def test_soak_counts_answers_against_judgements(soak_log):
    brain.log_answer(["a-note"])
    brain.log_answer(["a-note", "b-note"])
    brain.log_feedback("wrong", "why is x", notes=["a-note"], note="a-note")
    brain.log_feedback("missing", "why is y")
    s = brain.soak_stats()
    assert (s["answers"], s["with_notes"], s["wrong"], s["missing"]) == (2, 2, 1, 1)


def test_an_answer_with_no_retrieval_still_counts_as_a_question(soak_log):
    """The denominator is questions asked, not questions retrieval happened to like.

    A question it should have known about but stayed silent on is precisely a 'should have known';
    excluding those would flatter the rate exactly where the soak needs to be honest.
    """
    brain.log_answer([])
    brain.log_answer(["a-note"])
    s = brain.soak_stats()
    assert s["answers"] == 2 and s["with_notes"] == 1


def test_an_answer_record_keeps_no_question_text(soak_log):
    brain.log_answer(["a-note"])
    rec = soak_log.read_text(encoding="utf-8").strip()
    assert '"kind": "answer"' in rec and "question" not in rec


def test_a_corrupt_ledger_line_is_skipped_not_fatal(soak_log):
    brain.log_answer(["a-note"])
    with soak_log.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    brain.log_feedback("wrong", "q")
    s = brain.soak_stats()
    assert s["answers"] == 1 and s["wrong"] == 1


def test_an_unwritable_ledger_never_breaks_a_reply(soak_log, monkeypatch):
    monkeypatch.setattr(brain, "FEEDBACK_LOG", soak_log.parent / "no-such-dir" / "soak.jsonl")
    assert brain.log_answer(["a-note"]) == {"ok": False}


def test_stats_carries_the_soak_so_the_panel_can_show_it(corpus, soak_log):
    brain.log_answer(["a-note"])
    assert brain.stats()["soak"]["answers"] == 1


def test_an_answer_records_note_NAMES_not_whole_records(soak_log):
    """Retrieval returns full note records; the ledger must keep only their names.

    Caught on the real ledger, not by a test: the first live answer wrote a kilobyte of dict repr
    into a file whose only job is to be countable.
    """
    import json
    brain.log_answer([{"name": "a-note", "body": "x" * 900, "description": "d"}])
    rec = json.loads(soak_log.read_text(encoding="utf-8").strip())
    assert rec["notes"] == ["a-note"] and len(soak_log.read_text(encoding="utf-8")) < 120
