"""plan_ref hardening: chained re-declarations cannot escape the rework budget, and the argument parser is linear."""
import sys
import time

import pytest

from test_atlas_hook import HERE, _age_task, env, trip  # noqa: F401  (env is a fixture)


@pytest.mark.parametrize("decls", [
    [": team-type coding task:t_aaaaaaaa", ": team-type coding task:t_bbbbbbbb"],
    [": team-type writing", ": team-type research"],
    [": team-type coding task:t_cccccccc", ": team-type coding task:t_dddddddd", ": team-type research"],
])
def test_chained_redeclarations_cannot_escape_the_budget(env, decls):
    trip(env)
    env.set_mode("enforce")
    env.bash(decls[0])                                   # the first declaration abandons: one fresh budget, once
    assert not env.events("work_type")[-1].get("carried") and env.events("task_abandoned")
    for i in range(3):                                   # the fresh task goes over budget (3 > 2)
        env.edit("b%d.py" % i)
        env.review("r%d" % (i + 10), "B%d" % i, "NEEDS_CHANGES\n- one")
    for d in decls[1:]:                                  # from there on every declaration carries: no second reset
        _age_task(env)
        env.bash(d)
    wts = env.events("work_type")[-len(decls[1:]):]
    assert all(w.get("carried") for w in wts), [w.get("carried") for w in wts]
    out = env.agent("implementer", "b1")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_declaring_again_after_a_passing_review_starts_fresh(env):
    trip(env)
    env.set_mode("enforce")
    env.bash(": team-type coding task:t_aaaaaaaa")
    env.edit("a.py")
    env.review("r9", "A9", "READY")
    env.bash(": team-type coding task:t_bbbbbbbb")
    assert not env.events("work_type")[-1].get("carried")


def test_plan_ref_is_stored_exactly_as_validated(env):
    env.bash(": team-type coding task:t_aaaaaaaa")
    assert env.events("work_type")[-1]["plan_ref"] == "t_aaaaaaaa"
    env.bash(": team-type research task:T_AAAAAAAA")                  # not the pattern: dropped, never rewritten
    wt = env.events("work_type")[-1]
    assert wt["type"] == "research" and "plan_ref" not in wt
    env.bash(": team-type writing TASK:evt_0a1b2c3d")                 # the keyword is case-insensitive, the id is not
    assert env.events("work_type")[-1]["plan_ref"] == "evt_0a1b2c3d"


def test_argument_parser_is_linear_and_capped():
    sys.path.insert(0, str(HERE))
    import atlas_hook as h
    assert not hasattr(h, "PLAN_REF_TOKEN_RE")
    for text in (" " * 20000 + "x", "a " * 10000 + "x", "x" * 20000, "task:" * 4000):
        t0 = time.perf_counter()
        _rest, ref, problem = h.split_plan_ref(text)
        assert time.perf_counter() - t0 < 0.05
        assert ref is None and problem == "team-type: bad arguments"
    assert h.split_plan_ref(" coding atlas task:t_aaaaaaaa") == ("coding atlas", "t_aaaaaaaa", None)
    assert h.split_plan_ref(" coding task:nope!") == ("coding", None, "team-type: bad plan ref")
    assert h.split_plan_ref(" coding") == (" coding", None, None)
