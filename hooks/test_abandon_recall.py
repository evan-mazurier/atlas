"""The rework rule built on reviewer launches and verdicts only (contracts §1.2): rounds count by reviewer launch, an open
failing review blocks `: team-done`, a declaration over one abandons the old task (fresh budget, once per chain) or
carries its count. No file lock, no shell parsing. Temp ledgers only."""
import json
import sys

from test_atlas_hook import HERE, SID, _age_task, env, trip  # noqa: F401  (env is a fixture)

OLD = "s1abcdef-1"
NC = "NEEDS_CHANGES\n- one"


def denied(out):
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def start(env, aid, helper="reviewer", tuid=None):
    env.agent(helper, tuid or "t" + aid)
    env.run("SubagentStart", agent_id=aid, agent_type=helper)


def answer(env, aid, text, helper="reviewer"):
    """A launched reviewer answers (handback then stop)."""
    env.run("PreToolUse", tool_name="SubagentHandback", agent_id=aid, agent_type=helper, tool_input={"message": text})
    env.run("SubagentStop", agent_id=aid, agent_type=helper, last_assistant_message="")


def resume(env, to):
    """The main session resumes a finished reviewer (SendMessage); it starts running again at once."""
    out = env.run("PreToolUse", tool_name="SendMessage", tool_input={"to": to, "message": "re-check please"})
    env.run("SubagentStart", agent_id=to, agent_type="reviewer")
    return out


def shell_only(env, rounds, typ="coding"):
    """A task worked through the shell only (no edit event): `rounds` sequential failing reviews."""
    env.bash(": team-type " + typ)
    for i in range(rounds):
        env.review("q%d" % i, "Q%d" % i, NC)


def nc_events(env):
    return [e for e in env.events("verdict") if e["verdict"] == "NEEDS_CHANGES"]


def _derive(env, events=None):
    sys.path.insert(0, str(HERE.parent / "cockpit"))
    from ledger2 import derive, query
    return derive.derive_v2(events if events is not None else env.events(), query.load_workflows())


# ---- rounds

def test_shell_only_fix_and_fresh_reviewer_is_two_rounds(env):
    shell_only(env, 2)
    S = env.state()
    assert S["version"] == 0 and S["needs_changes"] == 2 and S["nc_rounds"] == 2
    assert [e["round"] for e in nc_events(env)] == [True, True]


def test_shell_only_task_trips_after_three_rounds_and_guards_refuse(env):
    shell_only(env, 3)
    env.set_mode("enforce")
    S = env.state()
    assert S["needs_changes"] == 3 and S["budget"] == 2
    assert denied(env.agent("implementer", "b1"))
    assert denied(env.pre_edit("a.py"))


def test_two_reviewers_launched_together_count_once(env):
    env.bash(": team-type coding")
    start(env, "A1")
    start(env, "A2")
    answer(env, "A1", NC)
    answer(env, "A2", NC)
    assert env.state()["needs_changes"] == 1
    assert [e["round"] for e in nc_events(env)] == [True, False]


def test_routine_and_senior_launched_together_are_one_round_each(env):
    env.bash(": team-type coding")
    start(env, "A1")
    start(env, "A2", "senior-reviewer")
    answer(env, "A1", NC)
    answer(env, "A2", NC, "senior-reviewer")
    S = env.state()
    assert S["needs_changes"] == 2 and S["nc_rounds_t"] == {"routine": 1, "senior": 1}
    assert [e["round"] for e in nc_events(env)] == [True, True]


def test_reviewer_launched_after_the_failing_review_arrived_is_a_new_round(env):
    env.bash(": team-type coding")
    start(env, "A1")
    answer(env, "A1", NC)
    start(env, "A2")
    answer(env, "A2", NC)
    assert env.state()["needs_changes"] == 2 and [e["round"] for e in nc_events(env)] == [True, True]


def test_reviewer_resumed_after_the_failing_review_that_fails_is_a_new_round(env):
    env.bash(": team-type coding")
    start(env, "A1")
    answer(env, "A1", NC)
    resume(env, "A1")
    answer(env, "A1", NC)
    assert env.state()["needs_changes"] == 2 and [e["round"] for e in nc_events(env)] == [True, True]


def test_unknown_launch_counts_as_a_round(env):
    env.bash(": team-type coding")
    start(env, "A1")
    answer(env, "A1", NC)
    env.run("SubagentStart", agent_id="A9", agent_type="reviewer")         # never launched through the Agent tool
    answer(env, "A9", NC)
    assert env.state()["needs_changes"] == 2 and [e["round"] for e in nc_events(env)] == [True, True]


def test_round_field_is_only_on_needs_changes_verdicts(env):
    env.bash(": team-type coding")
    env.review("r1", "A1", NC)
    env.review("r2", "A2", "READY")
    vs = env.events("verdict")
    assert vs[0]["round"] is True and "round" not in vs[1]


def test_state_rebuild_keeps_the_rounds(env):
    shell_only(env, 2)
    (env.ledger / "state" / f"{SID}.json").unlink()
    env.set_mode("enforce")
    env.agent("implementer", "b1")                                         # decided on the rebuilt state
    assert env.state()["needs_changes"] == 2 and env.state()["nc_rounds"] == 2


# ---- closing

def test_done_refused_with_an_open_failing_review_under_budget_at_version_zero(env):
    shell_only(env, 1)
    env.set_mode("enforce")
    out = env.bash(": team-done")
    assert denied(out) and "last review of this task did not pass" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "—" not in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not env.events("task_done") and env.state()["open"] is True
    r = env.events("refusal")[-1]
    assert r["guard"] == "done_lock" and r["would"] is False


def test_done_in_record_mode_logs_would_and_closes(env):
    shell_only(env, 1)
    assert env.bash(": team-done") is None
    r = env.events("refusal")[-1]
    assert r["guard"] == "done_lock" and r["would"] is True and "did not pass" in r["detail"]
    assert len(env.events("task_done")) == 1 and env.state()["open"] is False


def test_done_closes_after_ready_from_a_reviewer_launched_after_the_failing_one(env):
    shell_only(env, 1)
    env.set_mode("enforce")
    env.review("ok1", "OK1", "READY")
    assert env.bash(": team-done") is None
    assert len(env.events("task_done")) == 1


def test_ready_from_a_reviewer_launched_before_the_failing_one_still_refuses(env):
    env.bash(": team-type coding")
    start(env, "RA")                                                       # launched first, answers last
    env.review("rB", "RB", NC)
    answer(env, "RA", "READY")
    assert env.state()["last_verdict"] == "READY"
    env.set_mode("enforce")
    assert denied(env.bash(": team-done"))
    assert not env.events("task_done")


def test_escalated_task_is_refused_until_a_grant_clears_it(env):
    env.bash(": team-type coding")
    env.review("e1", "E1", "ESCALATE TO OPERATOR\n- needs a call", helper="senior-reviewer")
    env.set_mode("enforce")
    assert denied(env.bash(": team-done"))
    env.run("UserPromptSubmit", prompt="/grant-rework")
    assert env.bash(": team-done") is None
    assert len(env.events("task_done")) == 1


def test_done_with_no_failing_review_closes(env):
    env.bash(": team-type coding")
    env.set_mode("enforce")
    assert env.bash(": team-done") is None and not env.events("refusal")


# ---- declaring

def test_declaring_over_an_unresolved_under_budget_task_abandons_and_opens_fresh_chained(env):
    shell_only(env, 1)
    _age_task(env)
    env.bash(": team-type writing")
    ab = env.events("task_abandoned")
    assert len(ab) == 1 and ab[0]["task"] == OLD and ab[0]["needs_changes"] == 1 and ab[0]["granted"] == 0
    assert env.events()[-2]["ev"] == "task_abandoned" and env.events()[-1]["ev"] == "work_type"
    assert not env.events("work_type")[-1].get("carried")
    S = env.state()
    assert S["needs_changes"] == 0 and S["granted"] == 0 and S["chained"] is True and S["task"] == "s1abcdef-2"
    assert not env.events("refusal")


def test_declaring_over_a_spent_task_abandons_and_chains_the_new_one(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")
    assert env.events("task_abandoned")[-1]["needs_changes"] == 3
    S = env.state()
    assert S["needs_changes"] == 0 and S["chained"] is True and S["budget"] == 2
    assert env.agent("implementer", "b1") is None


def test_a_chained_task_spent_again_carries_and_is_tripped(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")
    for i in range(3):
        env.review("z%d" % i, "Z%d" % i, NC)
    n = len(env.events("task_abandoned"))
    _age_task(env)
    env.bash(": team-type research")
    wt = env.events("work_type")[-1]
    assert wt["carried"] is True and len(env.events("task_abandoned")) == n
    r = env.events("refusal")[-1]
    assert r["would"] is True and "carry-over, not a denial: budget carried over a new declaration (3" in r["detail"]
    S = env.state()
    assert S["needs_changes"] == 3 and S["chained"] is True and S["carried_nc"] == 3
    env.set_mode("enforce")
    assert denied(env.agent("implementer", "b9"))


def test_a_chained_task_with_any_open_failing_review_carries_even_under_budget(env):      # S3
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")
    env.review("z0", "Z0", NC)
    n = len(env.events("task_abandoned"))
    _age_task(env)
    env.bash(": team-type research")
    assert env.events("work_type")[-1]["carried"] is True and len(env.events("task_abandoned")) == n
    S = env.state()
    assert S["chained"] is True and S["carried_nc"] == 1


def test_chain_survives_an_empty_declaration_and_the_spent_third_task_carries(env):       # S2
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")                                         # T2 chained
    _age_task(env)
    env.bash(": team-type coding")                                         # T3: nothing failed, still chained
    assert env.state()["chained"] is True and not env.events("work_type")[-1].get("carried")
    for i in range(3):
        env.review("z%d" % i, "Z%d" % i, NC)
    n = len(env.events("task_abandoned"))
    _age_task(env)
    env.bash(": team-type research")
    assert env.events("work_type")[-1]["carried"] is True and len(env.events("task_abandoned")) == n
    env.set_mode("enforce")
    assert denied(env.agent("implementer", "b9"))


def test_chain_cleared_after_a_resolved_task_so_the_next_spent_task_abandons_again(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")                                         # chained
    env.edit("a.py")
    env.review("ok1", "OK1", "READY")
    _age_task(env)
    env.bash(": team-type coding")                                         # resolved after a change: chain cleared
    assert env.state()["chained"] is False
    for i in range(3):
        env.review("w%d" % i, "W%d" % i, NC)
    _age_task(env)
    env.bash(": team-type research")
    assert len(env.events("task_abandoned")) == 2 and not env.events("work_type")[-1].get("carried")


def test_escalated_task_carries(env):
    env.bash(": team-type coding")
    env.review("e1", "E1", "ESCALATE TO OPERATOR\n- needs a call", helper="senior-reviewer")
    assert env.state()["escalated"] is True
    _age_task(env)
    env.bash(": team-type writing")
    assert env.events("work_type")[-1]["carried"] is True and not env.events("task_abandoned")
    assert env.state()["chained"] is True and env.state()["escalated"] is True


def test_escalated_task_with_failing_reviews_carries_with_its_count(env):
    shell_only(env, 1)
    env.review("e1", "E1", "ESCALATE TO OPERATOR\n- needs a call", helper="senior-reviewer")
    _age_task(env)
    env.bash(": team-type writing")
    assert env.events("work_type")[-1]["carried"] is True and not env.events("task_abandoned")
    assert env.state()["carried_nc"] == 1


def test_declaring_over_a_resolved_task_is_fresh_and_unchained(env):
    shell_only(env, 3)
    env.review("ok1", "OK1", "READY")
    _age_task(env)
    env.bash(": team-type research")
    assert not env.events("work_type")[-1].get("carried") and not env.events("task_abandoned")
    assert env.state()["chained"] is False and env.state()["needs_changes"] == 0


def test_declaring_over_a_task_with_no_failing_review_is_fresh(env):
    env.bash(": team-type coding")
    _age_task(env)
    env.bash(": team-type writing")
    assert not env.events("task_abandoned") and not env.events("work_type")[-1].get("carried")


def test_chain_is_cleared_by_a_fresh_open_after_a_resolved_task(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")                                         # chained
    env.edit("a.py")
    env.review("ok1", "OK1", "READY")
    _age_task(env)
    env.bash(": team-type research")
    assert env.state()["chained"] is False


def test_abandon_event_is_metadata_only(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")
    ab = env.events("task_abandoned")
    assert len(ab) == 1
    assert set(ab[0]) <= {"v", "t", "ev", "sid", "cwd", "mode", "task", "needs_changes", "granted", "prev"}, ab[0]
    assert "paths" not in ab[0]
    json.dumps(env.events())


# ---- grants

def test_grant_is_plain_on_the_open_task(env):
    shell_only(env, 3)
    env.run("UserPromptSubmit", prompt="/grant-rework")
    g = env.events("rework_granted")[-1]
    assert "for_task" not in g and g["task"] == OLD
    S = env.state()
    assert S["granted"] == 1
    env.set_mode("enforce")
    assert env.agent("implementer", "b1") is None                          # 3 <= 2 + 1


def test_grant_after_an_abandon_lands_on_the_new_task(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")
    env.run("UserPromptSubmit", prompt="/grant-rework")
    g = env.events("rework_granted")[-1]
    assert "for_task" not in g and g["task"] == "s1abcdef-2" and env.state()["granted"] == 1


def test_grant_clears_escalation(env):
    env.bash(": team-type coding")
    env.review("e1", "E1", "ESCALATE TO OPERATOR\n- x", helper="senior-reviewer")
    env.run("UserPromptSubmit", prompt="/grant-rework")
    assert env.state()["escalated"] is False


# ---- no file lock, no shell parsing

def test_no_lock_state_and_edits_to_the_old_files_are_free_on_the_fresh_task(env):
    trip(env)
    env.set_mode("enforce")
    _age_task(env)
    env.bash(": team-type coding")
    S = env.state()
    for k in ("lock", "task_paths", "recalled", "recalled_from", "paths_overflow", "nc_versions"):
        assert k not in S
    assert env.pre_edit("a.py") is None
    assert env.bash("sed -i 's/x/y/' " + str(env.work / "a.py")) is None
    assert not env.events("rework_recalled")


# ---- derive parity

def _parity(env):
    S = env.state()
    t = _derive(env)[-1]
    r = t["rounds"]
    assert r["needs_changes"] == S["needs_changes"] and r["budget"] == S["budget"] and r["granted"] == S["granted"]
    used = S["carried_nc"] + max((S.get("nc_rounds_t") or {"routine": 0}).values() or [0])   # the hook's per-tier rule
    assert r["used"] == used and r["left"] == max(0, S["budget"] + S["granted"] - used)
    assert (r["own_routine"], r["own_senior"]) == (S["nc_rounds_t"].get("routine", 0), S["nc_rounds_t"].get("senior", 0))
    return t


def test_derive_matches_the_hook_through_shell_only_rounds_and_joint_reviewers(env):
    env.bash(": team-type coding")
    start(env, "A1")
    start(env, "A2")
    answer(env, "A1", NC)
    answer(env, "A2", NC)                                                  # same round
    t = _parity(env)
    assert t["rounds"]["own"] == 1
    env.review("r3", "A3", NC)
    resume(env, "A3")
    answer(env, "A3", NC)
    t = _parity(env)
    assert t["rounds"]["own"] == 3 == env.state()["needs_changes"]


def test_derive_mixed_tiers_match_the_hook(env):
    env.bash(": team-type coding")                                         # budget 2
    env.set_mode("enforce")
    for i in range(2):
        env.review("r%d" % i, "R%d" % i, NC)
    env.review("s0", "S0", NC, helper="senior-reviewer")
    S = env.state()
    assert S["needs_changes"] == 3 and S["nc_rounds_t"] == {"routine": 2, "senior": 1}
    t = _parity(env)
    assert t["rounds"]["needs_changes"] == 3 and t["rounds"]["left"] == 0 and t["rounds"]["used"] == 2
    assert t["rounds"]["own_routine"] == 2 and t["rounds"]["own_senior"] == 1
    env.review("s1", "S1", NC, helper="senior-reviewer")                   # senior tier at 2: still within budget
    assert not denied(env.agent("implementer", "b1"))
    _parity(env)
    env.review("s2", "S2", NC, helper="senior-reviewer")                   # senior tier at 3 > 2
    assert denied(env.agent("implementer", "b2"))
    t = _parity(env)
    assert t["rounds"]["used"] == 3 and t["rounds"]["left"] == 0


def test_derive_abandon_then_fresh_task_and_carry_match_the_hook(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")
    t1, t2 = _derive(env)
    assert t1["status"] == "abandoned" and t1["rounds"]["needs_changes"] == 3
    assert t2["rounds"]["needs_changes"] == 0 and t2["rounds"]["carried"] == 0
    _parity(env)
    env.run("UserPromptSubmit", prompt="/grant-rework")
    t = _parity(env)
    assert t["rounds"]["granted"] == 1
    for i in range(4):
        env.review("z%d" % i, "Z%d" % i, NC)                               # 4 > 2 + 1
    _age_task(env)
    env.bash(": team-type research")                                       # chained and spent: carries
    ts = _derive(env)
    assert ts[-1]["rounds"]["carried"] == 4 and ts[-1]["rounds"]["own"] == 0
    _parity(env)


def test_derive_under_budget_abandon_is_unchained_and_matches(env):
    shell_only(env, 1)
    _age_task(env)
    env.bash(": team-type writing")
    ts = _derive(env)
    assert ts[0]["status"] == "abandoned" and ts[1]["rounds"]["needs_changes"] == 0
    _parity(env)


def test_derive_escalated_carry_matches_the_hook(env):
    shell_only(env, 1)
    env.review("e1", "E1", "ESCALATE TO OPERATOR\n- x", helper="senior-reviewer")
    _age_task(env)
    env.bash(": team-type writing")
    t = _parity(env)
    assert t["rounds"]["carried"] == 1


# ---- legacy streams

def _E(seq, ev, **kw):
    d = {"v": 2, "t": "2026-10-20T10:%02d:00+00:00" % seq, "ev": ev, "sid": "sLEG"}
    d.update(kw)
    return d


def test_derive_verdicts_without_round_use_the_distinct_version_rule():
    T = "leg-1"
    evs = [_E(0, "work_type", task=T, type="coding", area="atlas"),
           _E(1, "edit", task=T, path="a.py", version=1),
           _E(2, "verdict", task=T, agent_id="a1", helper="reviewer", verdict="NEEDS_CHANGES", version=1, findings=1),
           _E(3, "verdict", task=T, agent_id="a2", helper="reviewer", verdict="NEEDS_CHANGES", version=1, findings=1),
           _E(4, "edit", task=T, path="a.py", version=2),
           _E(5, "verdict", task=T, agent_id="a3", helper="reviewer", verdict="NEEDS_CHANGES", version=2, findings=1)]
    r = _derive(None, evs)[0]["rounds"]
    assert r["own"] == 2 and r["needs_changes"] == 2


def test_derive_round_field_wins_over_versions():
    T = "leg-1"
    evs = [_E(0, "work_type", task=T, type="coding", area="atlas"),
           _E(2, "verdict", task=T, agent_id="a1", helper="reviewer", verdict="NEEDS_CHANGES", version=0, findings=1, round=True),
           _E(3, "verdict", task=T, agent_id="a2", helper="reviewer", verdict="NEEDS_CHANGES", version=0, findings=1, round=False),
           _E(4, "verdict", task=T, agent_id="a3", helper="reviewer", verdict="NEEDS_CHANGES", version=0, findings=1, round=True)]
    r = _derive(None, evs)[0]["rounds"]
    assert r["own"] == 2


def test_derive_legacy_recall_and_for_task_events_still_derive():
    T1, T2 = "leg-1", "leg-2"
    evs = [_E(0, "work_type", task=T1, type="coding", area="atlas")]
    for i in range(3):
        evs += [_E(1 + 2 * i, "edit", task=T1, path="a.py", version=i + 1),
                _E(2 + 2 * i, "verdict", task=T1, agent_id="a%d" % i, helper="reviewer", verdict="NEEDS_CHANGES",
                   version=i + 1, findings=1)]
    evs += [_E(10, "task_abandoned", task=T1, needs_changes=3, granted=0, paths=["a.py"]),
            _E(11, "work_type", task=T2, type="writing", area="atlas"),
            _E(12, "rework_granted", task=T2, n=1, for_task=T1),
            _E(13, "rework_recalled", task=T2, from_task=T1, nc=3, path="a.py")]
    t1, t2 = _derive(None, evs)
    assert t1["status"] == "abandoned" and t1["rounds"]["needs_changes"] == 3 and t1["rounds"]["granted"] == 1
    assert t2["rounds"]["carried"] == 3 and t2["rounds"]["own"] == 0
    assert t2["rounds"]["granted"] == 1 and t2["rounds"]["budget"] == 2


# ---- late verdicts (a declaration while a reviewer runs)

def late_nc(env, aid, text=NC):
    """A reviewer launched for the open task; a new declaration; then its verdict arrives."""
    start(env, aid)
    _age_task(env)
    env.bash(": team-type coding")
    answer(env, aid, text)


def test_late_needs_changes_counts_on_the_open_task_as_a_carried_round(env):
    env.bash(": team-type coding")
    late_nc(env, "L1")
    S = env.state()
    assert S["needs_changes"] == 1 and S["carried_nc"] == 1 and S["nc_rounds"] == 0 and S["chained"] is True
    v = env.events("verdict")[-1]
    assert v["counted_on"] == S["task"] and v["task"] == OLD and "round" not in v
    env.set_mode("enforce")
    assert denied(env.bash(": team-done"))


def test_four_late_cycles_trip_the_task(env):
    env.bash(": team-type coding")
    for i in range(4):
        late_nc(env, "L%d" % i)
    S = env.state()
    assert S["needs_changes"] == 4 and S["budget"] == 2
    assert not env.events("task_abandoned")
    env.set_mode("enforce")
    assert denied(env.agent("implementer", "b1"))


def test_late_needs_changes_with_no_open_task_applies_to_the_next_declaration(env):
    env.bash(": team-type coding")
    start(env, "L1")
    env.bash(": team-done")
    answer(env, "L1", NC)
    assert env.events("verdict")[-1]["counted_on"] == "next" and env.state()["late_nc"] == 1
    _age_task(env)
    env.bash(": team-type coding")
    S = env.state()
    assert S["needs_changes"] == 1 and S["carried_nc"] == 1 and S["chained"] is True and S["late_nc"] == 0
    assert S["nc_at"] > 0


def test_late_ready_changes_nothing(env):
    env.bash(": team-type coding")
    late_nc(env, "L1", "READY")
    S = env.state()
    assert S["needs_changes"] == 0 and "counted_on" not in env.events("verdict")[-1]


def test_late_escalate_escalates_the_open_task(env):
    env.bash(": team-type coding")
    start(env, "L1", "senior-reviewer")
    _age_task(env)
    env.bash(": team-type coding")
    answer(env, "L1", "ESCALATE TO OPERATOR" + chr(10) + "- call", "senior-reviewer")
    assert env.state()["escalated"] is True


def test_derive_matches_the_hook_for_late_verdicts(env):
    env.bash(": team-type coding")
    late_nc(env, "L1")
    late_nc(env, "L2")
    t = _parity(env)
    assert t["rounds"]["carried"] == 2 and t["rounds"]["own"] == 0
    ts = _derive(env)
    assert all(x["rounds"]["needs_changes"] <= 2 for x in ts) and ts[0]["rounds"]["own"] == 0


def test_derive_matches_the_hook_for_a_late_verdict_with_no_open_task(env):
    env.bash(": team-type coding")
    start(env, "L1")
    env.bash(": team-done")
    answer(env, "L1", NC)
    _age_task(env)
    env.bash(": team-type coding")
    t = _parity(env)
    assert t["rounds"]["carried"] == 1


def test_record_mode_logs_one_done_lock_refusal_per_close(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.review("r1", "A1", NC)                                              # version 1 unchecked AND an open failing review
    assert env.bash(": team-done") is None
    assert len([r for r in env.events("refusal") if r["guard"] == "done_lock"]) == 1


def test_chain_survives_team_done_of_an_unreviewed_chained_task(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")                                         # T2 chained, no edits, no review
    env.set_mode("enforce")
    assert env.bash(": team-done") is None and env.state()["open"] is False
    env.bash(": team-type coding")                                         # T3
    assert env.state()["chained"] is True
    for i in range(3):
        env.review("z%d" % i, "Z%d" % i, NC)
    n = len(env.events("task_abandoned"))
    _age_task(env)
    env.bash(": team-type research")
    assert env.events("work_type")[-1]["carried"] is True and len(env.events("task_abandoned")) == n


def test_chain_cleared_by_a_resolved_chained_task_that_was_then_closed(env):
    shell_only(env, 3)
    _age_task(env)
    env.bash(": team-type coding")                                         # T2 chained
    env.edit("a.py")
    env.review("ok1", "OK1", "READY")
    assert env.bash(": team-done") is None
    env.bash(": team-type coding")
    assert env.state()["chained"] is False


def test_p1b_edit_nc_declare_repeated_carries_and_trips(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.review("r1", "A1", NC)
    _age_task(env)
    env.bash(": team-type coding")                                         # abandon: T2 fresh but chained
    assert len(env.events("task_abandoned")) == 1 and env.state()["chained"] is True
    env.edit("a.py")
    env.review("r2", "A2", NC)
    _age_task(env)
    env.bash(": team-type coding")                                         # chained with an open failing review: carries
    assert env.events("work_type")[-1]["carried"] is True and len(env.events("task_abandoned")) == 1
    env.edit("a.py")
    env.review("r3", "A3", NC)
    env.edit("a.py")
    env.review("r4", "A4", NC)
    S = env.state()
    assert S["needs_changes"] == 3 and S["budget"] == 2
    env.set_mode("enforce")
    assert denied(env.agent("implementer", "b1"))
    assert denied(env.pre_edit("a.py"))
    t = _derive(env)[-1]
    assert t["rounds"]["needs_changes"] == 3 and t["rounds"]["carried"] == 1


def test_ready_on_a_no_edit_chained_task_does_not_clear_the_chain(env):
    shell_only(env, 1)
    _age_task(env)
    env.bash(": team-type coding")                                         # T2 chained, no edits
    env.review("ok1", "OK1", "READY")
    _age_task(env)
    env.bash(": team-type coding")
    assert env.state()["chained"] is True


def test_ready_on_a_chained_task_with_an_edit_clears_the_chain(env):
    shell_only(env, 1)
    _age_task(env)
    env.bash(": team-type coding")
    env.edit("a.py")
    env.review("ok1", "OK1", "READY")
    _age_task(env)
    env.bash(": team-type coding")
    assert env.state()["chained"] is False


def test_chain_state_survives_a_rebuild(env):
    shell_only(env, 1)
    _age_task(env)
    env.bash(": team-type coding")
    env.edit("a.py")
    env.review("ok1", "OK1", "READY")
    (env.ledger / "state" / f"{SID}.json").unlink()
    env.bash(": team-type research")                                       # rebuilt from events: version 1, resolved
    assert env.state()["chained"] is False


def test_late_verdict_entry_is_marked_and_is_not_the_final_verdict(env):
    env.bash(": team-type coding")
    env.review("ok1", "OK1", "READY")
    late_nc(env, "L1")
    ts = _derive(env)
    assert ts[0]["verdicts"][-1].get("late") is True and ts[0]["final_verdict"] == "READY"
    assert ts[1]["rounds"]["carried"] == 1


def test_late_verdict_before_any_task_derives_onto_the_next_task(env):
    env.run("SubagentStart", agent_id="L1", agent_type="reviewer")
    answer(env, "L1", NC)
    assert env.events("verdict")[-1].get("counted_on") == "next"
    env.bash(": team-type coding")
    t = _parity(env)
    assert t["rounds"]["carried"] == 1


def test_late_verdict_before_any_task_in_record_mode(env):
    env.set_mode("record")
    env.run("SubagentStart", agent_id="L1", agent_type="reviewer")
    answer(env, "L1", NC)
    assert env.state()["late_nc"] == 1
