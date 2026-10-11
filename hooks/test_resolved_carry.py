"""Carry rule: a task is RESOLVED when its latest reviewer verdict is READY (covering the latest main-session version).
Helpers edit in their own worktrees, so the main session's `version` stays 0 and cannot be the signal. Temp ledgers only."""
import json
import sys

import pytest

from test_atlas_hook import REPO, SID, env  # noqa: F401  (env is a fixture)


def age(env):
    """Age the open task past the 60 s duplicate window so a same-type re-declaration is a real one."""
    sp = env.ledger / "state" / f"{SID}.json"
    S = json.loads(sp.read_text(encoding="utf-8"))
    S["task_t"] = "2020-01-01T00:00:00+00:00"
    sp.write_text(json.dumps(S), encoding="utf-8")


def carried(env):
    return bool(env.events("work_type")[-1].get("carried"))


def spent_chore(env):
    """A chore (budget 1) with one NEEDS CHANGES while the main session made no edit (version 0)."""
    env.bash(": team-type chore")
    env.review("r1", "A1", "NEEDS_CHANGES\n- one")
    S = env.state()
    assert S["version"] == 0 and S["needs_changes"] == 1 and S["budget"] == 1


def test_needs_changes_then_ready_with_version_zero_is_resolved(env):
    spent_chore(env)
    env.review("r2", "A2", "READY")
    age(env)
    env.bash(": team-type chore")
    assert not carried(env) and env.state()["needs_changes"] == 0
    env.bash(": team-type writing")                        # and the declaration after that is fresh as well
    assert not carried(env)


@pytest.mark.parametrize("decls", [
    [": team-type coding task:t_aaaaaaaa", ": team-type coding task:t_bbbbbbbb"],
    [": team-type writing", ": team-type research"],
    [": team-type coding task:t_cccccccc", ": team-type writing", ": team-type research task:t_dddddddd"],
])
def test_spent_without_ready_carries_twice_in_a_row(env, decls):
    spent_chore(env)
    for d in decls:
        env.bash(d)
        assert carried(env), d
    assert env.state()["needs_changes"] == 1


def test_spent_then_carry_then_ready_starts_the_next_fresh(env):
    spent_chore(env)
    env.bash(": team-type coding")
    assert carried(env) and env.state()["needs_changes"] == 1
    env.review("r2", "A2", "READY")
    env.bash(": team-type writing")
    assert not carried(env) and env.state()["needs_changes"] == 0


def test_main_session_edits_after_ready_make_an_over_budget_task_unresolved_again(env):
    env.bash(": team-type chore")
    env.edit("a.py")
    env.review("r1", "A1", "NEEDS_CHANGES\n- one")
    env.edit("a.py")
    env.review("r2", "A2", "READY")
    assert env.state()["last_verified"] >= env.state()["version"]
    age(env)
    env.bash(": team-type chore")
    assert not carried(env)                                 # resolved: READY covers the latest version
    env.edit("a.py")
    env.review("r3", "A3", "NEEDS_CHANGES\n- one")
    env.edit("a.py")
    env.review("r4", "A4", "READY")
    env.edit("a.py")                                        # an edit after the READY
    assert env.state()["last_verified"] < env.state()["version"]
    age(env)
    env.bash(": team-type writing")
    assert carried(env)


def test_a_later_needs_changes_after_a_ready_is_unresolved(env):
    env.bash(": team-type chore")
    env.review("r1", "A1", "NEEDS_CHANGES\n- one")
    env.review("r2", "A2", "READY")
    env.edit("a.py")
    env.review("r3", "A3", "NEEDS_CHANGES\n- two")
    env.bash(": team-type writing")
    assert carried(env)


def test_reminder_does_not_fire_after_a_resolved_task(env):
    env.bash(": team-type chore")
    env.edit("a.py")
    env.review("r1", "A1", "NEEDS_CHANGES\n- one")
    env.edit("a.py")
    env.review("r2", "A2", "NEEDS_CHANGES\n- two")        # nc 2 > budget 1: the reminder's condition, were it carried
    env.edit("a.py")
    env.review("r3", "A3", "READY")
    age(env)
    env.bash(": team-type chore")
    assert not carried(env)
    out = env.agent("implementer", "b1")
    assert out is None and not env.events("reminder")


def test_reminder_still_fires_for_a_carried_unresolved_task(env):
    env.bash(": team-type chore")
    env.edit("a.py")
    env.review("r1", "A1", "NEEDS_CHANGES\n- one")
    env.edit("a.py")
    env.review("r2", "A2", "NEEDS_CHANGES\n- two")
    age(env)
    env.bash(": team-type chore")
    assert carried(env)
    out = env.agent("implementer", "b1")
    assert "over its rework budget" in out["hookSpecificOutput"]["additionalContext"]


def test_hook_stream_and_derive_agree_on_the_resolved_rule(env):
    sys.path.insert(0, str(REPO / "cockpit"))
    from ledger2 import derive, query
    spent_chore(env)
    env.review("r2", "A2", "READY")
    age(env)
    env.bash(": team-type chore")
    env.bash(": team-type writing")
    legacy = [{k: v for k, v in e.items() if k != "carried"} for e in env.events()]     # recompute, as for old events
    for evs in (env.events(), legacy):
        ts = derive.derive_v2(evs, query.load_workflows())
        assert [t["rounds"]["needs_changes"] for t in ts] == [1, 0, 0]
