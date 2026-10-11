"""Plain-language bypass report (bypass_report.py + stats._bypass + tools/ledger_note.py)."""
import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "cockpit"))

from ledger2 import bypass_report as br  # noqa: E402
from ledger2 import query, stats, store  # noqa: E402

BASE = dt.datetime(2026, 10, 10, 10, 0, 0).astimezone()


def t(min_=0, sec=0):
    return (BASE + dt.timedelta(minutes=min_, seconds=sec)).isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(tmp_path / "ledger"))
    stats.clear_cache()
    stats._integrity_cache.clear()
    query.invalidate()
    return tmp_path / "ledger"


def wf():
    return query.load_workflows()


def base_events(extra_title="Atlas v2 build"):
    return [
        {"t": t(-30), "ev": "session_start", "sid": "s", "title": extra_title},
        {"t": t(-20), "ev": "work_type", "sid": "s", "type": "chore", "task": "s-1"},
    ]


def attempt(min_=0, **kw):
    return dict({"t": t(min_), "ev": "bypass_attempt", "sid": "s", "tool": "Bash", "denied": True,
                 "matched": "ai\\atlas-data\\ledger", "actor": "main", "effect": "read",
                 "desc": "look at the ledger", "target": "ledger-day:2026-10-09"}, **kw)


def report(evs, idx=None, notes=None):
    if idx is None:
        idx = next(i for i, e in enumerate(evs) if e["ev"] == "bypass_attempt")
    return br.build_report(evs[idx], evs, idx, wf(), notes)


def test_rule_d_isolated_read_is_info():
    evs = base_events() + [attempt()]
    r = report(evs)
    assert r["level"] == "info"
    assert r["reading"] == "Isolated read, no rework refusal for this helper before: most likely a wrong tool choice."
    assert r["headline"] == "The Chief tried to read the ledger record of 9 Oct with a shell command. Blocked."
    assert r["would_change"] == "The command looks read-only."
    assert r["own_words"] == "look at the ledger"
    assert 'Session "Atlas v2 build", type chore.' in r["context"]
    assert "No rework refusal in the 30 minutes before." in r["context"]
    assert r["after"] == "No further activity recorded in the next 5 minutes."
    assert "cmd" not in r and r["actor_is_main"] is True


def test_rule_c_repeats_warn_and_after_counts_retries():
    evs = base_events() + [attempt(0), attempt(2)]
    r = report(evs, idx=2)
    assert r["level"] == "warn" and r["reading"] == "Repeated attempts."
    assert report(evs, idx=2)["after"] == "Tried again 1 more time."
    assert report(evs, idx=3)["after"] == "No further activity recorded in the next 5 minutes."


def test_attempts_far_apart_are_not_repeats():
    evs = base_events() + [attempt(0), attempt(11)]
    assert report(evs, idx=2)["level"] == "info"


@pytest.mark.parametrize("effect,word", [("change", "changed"), ("delete", "deleted")])
def test_rule_b_change_or_delete_alerts(effect, word):
    evs = base_events() + [attempt(effect=effect, target="ledger-day:2026-10-10")]
    r = report(evs)
    assert r["level"] == "alert" and r["reading"] == "Tried to alter the record or the rules."
    assert r["would_change"] == f"Yes, it would have {word} the ledger record of 10 Oct."
    assert r["headline"].startswith(f"The Chief tried to {effect} ")


def refusal(min_, helper=None, **kw):
    e = {"t": t(min_), "ev": "refusal", "sid": "s", "task": "s-1", "guard": "rework", "would": False, "detail": "over"}
    if helper:
        e["helper"] = helper
    e.update(kw)
    return e


def test_rule_a_builder_after_refusal_of_its_type_beats_everything_and_shows_in_context():
    evs = base_events() + [refusal(-4, "implementer", label="Build it"), attempt(0, actor="implementer")]
    r = report(evs)
    assert r["level"] == "alert"
    assert r["reading"] == "After a rework refusal: possible attempt to get past the limit."
    assert "A new rework round was refused 4 min before, for the implementer helper." in r["context"]


def test_rule_a_matches_the_spawn_by_tuid_when_the_helper_type_differs():
    evs = base_events() + [refusal(-4, "drafter", tuid="tu9"),
                           {"t": t(-3), "ev": "helper_paired", "sid": "s", "tuid": "tu9", "agent_id": "A1", "helper": "x"},
                           attempt(0, actor="implementer", agent_id="A1")]
    assert report(evs)["level"] == "alert"


@pytest.mark.parametrize("actor", ["reviewer", "senior-reviewer", "scout", "test-runner", "main"])
def test_rule_a_never_fires_for_non_builders(actor):
    evs = base_events() + [refusal(-4, "implementer"), attempt(0, actor=actor)]
    r = report(evs)
    assert r["level"] == "info" and "rework refusal" not in r["reading"].lower().replace("no rework refusal", "")


def test_rule_a_needs_the_same_helper_type():
    evs = base_events() + [refusal(-4, "drafter"), attempt(0, actor="implementer")]
    assert report(evs)["level"] == "info"


def test_old_refusal_without_helper_does_not_alert_and_context_says_so():
    evs = base_events() + [refusal(-4), attempt(0, actor="implementer")]
    r = report(evs)
    assert r["level"] == "info"
    assert "A rework round was over budget for this task (helper not recorded), 4 min before." in r["context"]


def test_the_four_10_oct_rows_no_longer_read_after_a_rework_refusal():
    """Fixture of the 10 Oct 14:22, 14:28, 18:07, 18:12 rows: implementer and senior-reviewer actors, refusals
    recorded without a helper, one session, one task all day with logged-not-enforced rounds."""
    evs = base_events() + [
        {"t": t(-9), "ev": "verdict", "sid": "s", "task": "s-1", "agent_id": "r%d" % i, "helper": "reviewer",
         "verdict": "NEEDS_CHANGES", "version": i, "findings": 1, "mode": "record"} for i in range(1, 8)]
    evs += [refusal(-8, would=True), refusal(-7, would=True)]
    rows = []
    for k, actor in enumerate(["implementer", "senior-reviewer", "implementer", "senior-reviewer"]):
        evs.append(attempt(k, actor=actor, effect="read", mode="record"))
    for i, e in enumerate(evs):
        if e["ev"] == "bypass_attempt":
            rows.append(br.build_report(e, evs, i, wf(), None))
    assert len(rows) == 4
    for r in rows:
        assert "rework refusal" not in r["reading"].lower().replace("no rework refusal", "")
        assert r["level"] != "alert"
        assert "7 of 2 rework used" not in r["context"]
        assert "7 review rounds asked for changes on this task" in r["context"]


def test_over_budget_count_is_stated_factually():
    evs = base_events() + [
        {"t": t(-10 + i), "ev": "verdict", "sid": "s", "task": "s-1", "agent_id": "r%d" % i, "helper": "reviewer",
         "verdict": "NEEDS_CHANGES", "version": i, "findings": 1} for i in range(1, 5)]
    evs.append(attempt(0, mode="record"))
    budget = (wf().get("types") or {}).get("chore", {}).get("budget")
    if budget is not None and budget < 4:
        ctx = report(evs)["context"]
        assert f"4 review rounds asked for changes on this task (budget {budget}; rounds beyond it were logged, not enforced)." in ctx


def test_old_refusal_and_carry_over_do_not_count():
    evs = base_events() + [
        {"t": t(-40), "ev": "refusal", "sid": "s", "guard": "rework", "would": False, "detail": "over"},
        {"t": t(-2), "ev": "refusal", "sid": "s", "guard": "rework", "would": True, "detail": "carry-over, not a denial"},
        attempt(0)]
    assert report(evs)["level"] == "info"


def test_rework_used_over_budget_in_context():
    evs = base_events() + [
        {"t": t(-10), "ev": "verdict", "sid": "s", "task": "s-1", "agent_id": "a", "helper": "reviewer",
         "verdict": "NEEDS_CHANGES", "version": 1, "findings": 2},
        attempt(0)]
    budget = (wf().get("types") or {}).get("chore", {}).get("budget")
    ctx = report(evs)["context"]
    if budget is None:
        assert "No rework budget" in ctx
    else:
        assert f"1 of {budget} rework used." in ctx


def test_helper_actor_names_and_task_label():
    for helper, name in [("implementer", "Builder"), ("test-runner", "Lab Tech"), ("reviewer", "Inspector"),
                         ("senior-reviewer", "Senior Partner"), ("scout", "Scout"), ("sweeper", "Clerk"),
                         ("researcher", "Analyst"), ("drafter", "Writer")]:
        evs = base_events() + [attempt(actor=helper, agent_id="ag", actor_task="check the card")]
        r = report(evs)
        assert r["headline"].startswith(f"The {name} tried")
        assert 'Helper task: "check the card".' in r["context"]


def test_after_helper_end_and_verdict():
    evs = base_events() + [attempt(0, actor="reviewer", agent_id="ag"),
                           {"t": t(1), "ev": "verdict", "sid": "s", "agent_id": "ag", "verdict": "READY", "helper": "reviewer"},
                           {"t": t(2), "ev": "helper_end", "sid": "s", "agent_id": "ag"}]
    a = report(evs)["after"]
    assert "Then handed back to the Chief." in a and "(READY)" in a


def test_after_other_activity():
    evs = base_events() + [attempt(0), {"t": t(1), "ev": "edit", "sid": "s", "path": "x"}]
    assert report(evs)["after"] == "Work continued in the session (1 event recorded)."


def test_old_event_without_new_fields_renders():
    old = {"t": t(0), "ev": "bypass_attempt", "sid": "s", "tool": "Bash", "matched": ".claude/hooks/atlas", "denied": True}
    r = report([old])
    assert r["headline"] == "The Chief tried to use the hooks folder with a shell command. Blocked."
    assert r["own_words"] == "Not recorded."
    assert r["would_change"] == "Not recorded."
    assert "cmd" not in r and r["level"] == "info" and r["reading"] == "No details recorded."
    assert r["notes"] == []
    assert "Session title not recorded." in r["context"] and "No task was open." in r["context"]


def test_old_event_actor_falls_back_to_top_level_agent_fields():
    helper = {"t": t(0), "ev": "bypass_attempt", "sid": "s", "tool": "Bash", "matched": "x", "denied": True,
              "agent_id": "ag", "agent_type": "scout"}
    assert report([helper])["headline"].startswith("The Scout tried")
    nameless = dict(helper)
    nameless.pop("agent_type")
    assert report([nameless])["headline"].startswith("An unknown agent tried")


def test_no_hand_back_claim_without_an_agent_id():
    evs = base_events() + [attempt(0), {"t": t(1), "ev": "helper_end", "sid": "s", "agent_id": "ag"}]
    assert "handed back" not in report(evs)["after"]


def test_helper_note_target():
    assert br.target_words({"matched": "ledger note by a helper"}) == "the ledger notes"


@pytest.mark.parametrize("label,words", [
    ("hooks-config", "the hooks' settings"),
    ("hooks-program", "the hook program"),
    ("hooks-folder", "the hooks folder"),
    ("ledger", "the ledger record"),
    ("ledger-day:2026-10-09", "the ledger record of 9 Oct"),
])
def test_targets(label, words):
    assert br.target_words({"matched": ".claude/hooks/atlas" if label.startswith("hooks") else "x", "target": label}) == words


def test_all_notes_shown_oldest_first_with_author():
    evs = base_events() + [attempt()]
    r = report(evs, notes=[{"t": t(5), "by": "main", "text": "second"}, {"t": t(1), "text": "first"}])
    assert [n["text"] for n in r["notes"]] == ["first", "second"]
    assert r["notes"][0]["by"] == "unknown" and r["notes"][1]["by"] == "main"


def test_stats_bypass_rows_carry_report_and_note_via_tool(ledger):
    now = dt.datetime.now().astimezone()
    tt = lambda m: (now - dt.timedelta(minutes=m)).isoformat(timespec="seconds")  # noqa: E731
    store.append([
        {"v": 2, "t": tt(30), "ev": "session_start", "sid": "s9", "title": "T"},
        {"v": 2, "t": tt(20), "ev": "bypass_attempt", "sid": "s9", "tool": "Bash", "matched": "ai/atlas-data/ledger",
         "denied": True, "actor": "main", "effect": "read"},
    ])
    at = tt(20)
    r = subprocess.run([sys.executable, str(REPO / "tools" / "ledger_note.py"), "--at", at, "--sid", "s9", "--text", "fine"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    bad = subprocess.run([sys.executable, str(REPO / "tools" / "ledger_note.py"), "--at", "nope", "--sid", "s9", "--text", "x"],
                         capture_output=True, text=True)
    assert bad.returncode == 1
    stats.clear_cache()
    p = stats.compute()["bypass"]
    assert p["count"] == 1
    row = p["recent"][0]
    assert row["key"] == f"{at}|s9"
    assert [(n["text"], n["by"]) for n in row["report"]["notes"]] == [("fine", "main")]
    assert row["report"]["headline"].startswith("The Chief tried to read")
    assert store.verify_chain()["intact"]


def test_repeats_are_counted_per_actor_not_per_session():
    """10 Oct pattern: a Builder and a Senior Partner in one sid, no agent_id on the old events."""
    evs = base_events()
    for k, actor in ((0, "implementer"), (6, "senior-reviewer"), (225, "implementer"), (230, "senior-reviewer")):
        evs.append(attempt(k, actor=actor, effect="read"))
    for i, e in enumerate(evs):
        if e["ev"] == "bypass_attempt":
            assert br.build_report(e, evs, i, wf(), None)["reading"] != "Repeated attempts."
    evs2 = base_events() + [attempt(0, actor="implementer"), attempt(4, actor="implementer")]
    assert report(evs2, idx=3)["reading"] == "Repeated attempts."
    a = base_events() + [attempt(0, actor="implementer", agent_id="A1"), attempt(2, actor="implementer", agent_id="A2")]
    assert report(a, idx=3)["reading"] != "Repeated attempts."
    b = base_events() + [attempt(0, actor="implementer", agent_id="A1"), attempt(2, actor="implementer", agent_id="A1")]
    assert report(b, idx=3)["reading"] == "Repeated attempts."
    c = base_events() + [attempt(0), attempt(2, actor="implementer", agent_id="A1")]
    assert report(c, idx=3)["reading"] != "Repeated attempts."


@pytest.mark.parametrize("new_type", ["chore", "coding"])
def test_context_counts_only_the_task_open_at_the_attempt(new_type):
    """Task A (chore) ends unresolved after 7 NEEDS CHANGES (no READY), then a new declaration of the same or another
    type: the spent count carries into B by design (contracts section 6), and the attempt in A still shows A's."""
    evs = base_events() + [
        {"t": t(-10 + i), "ev": "verdict", "sid": "s", "task": "s-1", "agent_id": "r%d" % i, "helper": "reviewer",
         "verdict": "NEEDS_CHANGES", "version": i, "findings": 1} for i in range(1, 8)]
    evs.append(attempt(-1))
    evs.append({"t": t(0), "ev": "work_type", "sid": "s", "type": new_type, "task": "s-2"})
    evs.append(attempt(1))
    ctxA = report(evs, idx=next(i for i, e in enumerate(evs) if e["ev"] == "bypass_attempt"))["context"]
    assert "7 review rounds" in ctxA
    idxB = len(evs) - 1
    ctxB = br.build_report(evs[idxB], evs, idxB, wf(), None)["context"]
    assert f"type {new_type}" in ctxB
    assert "7 review rounds" in ctxB


def test_context_after_a_redeclaration_under_budget_starts_fresh():
    evs = [
        {"t": t(-30), "ev": "session_start", "sid": "s", "title": "x"},
        {"t": t(-20), "ev": "work_type", "sid": "s", "type": "coding", "task": "s-1"},
        {"t": t(-10), "ev": "verdict", "sid": "s", "task": "s-1", "agent_id": "r1", "helper": "reviewer",
         "verdict": "NEEDS_CHANGES", "version": 1, "findings": 1},
        {"t": t(0), "ev": "work_type", "sid": "s", "type": "coding", "task": "s-2"},
        attempt(1)]
    idx = len(evs) - 1
    ctx = br.build_report(evs[idx], evs, idx, wf(), None)["context"]
    assert "0 of 2 rework used" in ctx


def test_a_task_that_ended_ready_does_not_pass_its_count_on():
    evs = [
        {"t": t(-30), "ev": "session_start", "sid": "s", "title": "x"},
        {"t": t(-20), "ev": "work_type", "sid": "s", "type": "coding", "task": "s-1"},
        {"t": t(-19), "ev": "edit", "sid": "s", "task": "s-1", "path": "p", "tool": "Edit", "version": 1},
        {"t": t(-18), "ev": "verdict", "sid": "s", "task": "s-1", "agent_id": "r1", "helper": "reviewer",
         "verdict": "NEEDS_CHANGES", "version": 1, "findings": 1},
        {"t": t(-17), "ev": "edit", "sid": "s", "task": "s-1", "path": "p", "tool": "Edit", "version": 2},
        {"t": t(-16), "ev": "verdict", "sid": "s", "task": "s-1", "agent_id": "r2", "helper": "reviewer",
         "verdict": "NEEDS_CHANGES", "version": 2, "findings": 1},
        {"t": t(-15), "ev": "edit", "sid": "s", "task": "s-1", "path": "p", "tool": "Edit", "version": 3},
        {"t": t(-14), "ev": "verdict", "sid": "s", "task": "s-1", "agent_id": "r3", "helper": "reviewer",
         "verdict": "READY", "version": 3, "findings": 0},
        {"t": t(0), "ev": "work_type", "sid": "s", "type": "coding", "task": "s-2"},
        attempt(1)]
    idx = len(evs) - 1
    ctx = br.build_report(evs[idx], evs, idx, wf(), None)["context"]
    assert "0 of 2 rework used" in ctx
