"""Two-tier rework budget (contracts 1.2 and 6.3), the 6-edit stop (6.9) and the test-runner reminder (6.9a).
Subprocess runs against temp ledgers; never live data."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_atlas_hook import REPO, SID, _age_task, env  # noqa: E402,F401  (env is a fixture)

NC = "NEEDS_CHANGES\n- one"


def denied(out):
    return bool(out) and out["hookSpecificOutput"].get("permissionDecision") == "deny"


def ctx(out):
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext") or ""


def fail(env, n, helper, tag):
    """`n` sequential failing reviews by `helper` (each launched after the previous one arrived)."""
    for i in range(n):
        env.review("%s%d" % (tag, i), "%s%d" % (tag.upper(), i), NC, helper=helper)


# ------------------------------------------------------------------ 1. two-tier budget

def test_budget_is_counted_per_tier(env):
    env.bash(": team-type coding")                       # budget 2
    env.set_mode("enforce")
    fail(env, 2, "reviewer", "r")
    fail(env, 2, "senior-reviewer", "s")
    S = env.state()
    assert S["needs_changes"] == 4 and S["nc_rounds_t"] == {"routine": 2, "senior": 2}
    assert not denied(env.agent("implementer", "b1"))     # 2 per tier is within budget 2
    fail(env, 1, "reviewer", "x")
    assert env.state()["nc_rounds_t"]["routine"] == 3
    assert denied(env.agent("implementer", "b2"))
    assert denied(env.pre_edit("a.py"))


def test_a_grant_raises_both_tiers(env):
    env.bash(": team-type coding")
    fail(env, 3, "senior-reviewer", "s")
    env.set_mode("enforce")
    assert denied(env.agent("implementer", "b1"))
    env.run("UserPromptSubmit", prompt="/grant-rework 1")
    assert not denied(env.agent("implementer", "b2"))     # 3 > 2 + 1 is false


def test_legal_budget_is_one_round_per_tier(env):
    env.bash(": team-type legal")
    env.set_mode("enforce")
    S0 = env.state()
    assert S0["budget"] == 1
    fail(env, 1, "reviewer", "r")
    fail(env, 1, "senior-reviewer", "s")
    assert not denied(env.agent("drafter", "d1"))         # one free round each
    fail(env, 1, "senior-reviewer", "t")
    assert denied(env.agent("drafter", "d2"))             # senior tier now at 2 > 1


def test_carried_rounds_count_against_each_tier(env):
    env.bash(": team-type chore")                         # budget 1
    fail(env, 1, "reviewer", "a")
    _age_task(env)
    env.bash(": team-type chore")                         # abandoned, fresh, chained
    fail(env, 1, "reviewer", "b")
    _age_task(env)
    env.bash(": team-type chore")                         # carried: 1 carried round
    S = env.state()
    assert S["carried_nc"] == 1 and S["nc_rounds"] == 0
    env.set_mode("enforce")
    assert not denied(env.agent("implementer", "b1"))     # 1 carried is within budget 1
    fail(env, 1, "senior-reviewer", "c")                  # the senior tier had no round of its own, carried 1 + 1 > 1
    assert denied(env.agent("implementer", "b2"))


def test_state_rebuilt_from_the_ledger_keeps_the_tiers(env):
    env.bash(": team-type coding")
    fail(env, 1, "reviewer", "r")
    fail(env, 2, "senior-reviewer", "s")
    before = env.state()
    (env.ledger / "state" / ("%s.json" % SID)).unlink()
    env.pre_edit("a.py")                                  # any state-reading hook rebuilds the cache
    after = env.state()
    assert after["nc_rounds_t"] == before["nc_rounds_t"] == {"routine": 1, "senior": 2}
    assert after["nc_at_t"] == before["nc_at_t"] and after["needs_changes"] == 3


# ------------------------------------------------------------------ 2. the 6-edit stop

def test_stop_at_the_sixth_main_edit_record_then_enforce(env):
    env.bash(": team-type coding")
    for n in "abcdef":
        assert not denied(env.pre_edit(n + ".py"))
        env.edit(n + ".py")
    assert env.state()["builder_edits"] == 6
    assert not env.events("refusal")
    assert env.pre_edit("g.py") is None                   # record mode: logged, allowed
    r = env.events("refusal")[-1]
    assert r["guard"] == "edit_limit" and r["would"] is True
    env.set_mode("enforce")
    out = env.pre_edit("g.py")
    assert denied(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == ("Main-session edit limit reached on this task (6 since the last builder): hand the rest "
                      "to implementer or drafter.")
    assert "—" not in reason
    assert env.events("refusal")[-1]["would"] is False
    # a helper's own edit is never refused by it
    assert env.pre_edit("g.py", agent_id="Z", agent_type="implementer") is None


def test_five_edits_are_still_allowed(env):
    env.bash(": team-type coding")
    env.set_mode("enforce")
    for n in "abcde":
        env.edit(n + ".py")
    assert not denied(env.pre_edit("f.py"))


def test_builder_launch_resets_the_stop(env):
    env.bash(": team-type coding")
    for n in "abcdef":
        env.edit(n + ".py")
    env.set_mode("enforce")
    assert denied(env.pre_edit("g.py"))
    env.agent("test-runner", "t1")                        # not a builder: no reset
    assert env.state()["builder_edits"] == 6
    assert denied(env.pre_edit("g.py"))
    env.agent("implementer", "b1")
    assert env.state()["builder_edits"] == 0
    assert not denied(env.pre_edit("g.py"))
    for n in "abcdef":
        env.edit(n + ".py")
    assert denied(env.pre_edit("g.py"))
    env.agent("drafter", "d1")
    assert env.state()["builder_edits"] == 0
    assert not denied(env.pre_edit("g.py"))
    # the 3-edit reminder counter is not reset by it
    assert env.state()["main_edits"] == 12


def test_new_task_resets_the_stop(env):
    env.bash(": team-type coding")
    for n in "abcdef":
        env.edit(n + ".py")
    env.bash(": team-type writing")
    env.set_mode("enforce")
    assert env.state()["builder_edits"] == 0
    assert not denied(env.pre_edit("g.py"))


def test_memory_files_are_not_counted_and_not_stopped(env):
    mem = Path.home() / ".claude" / "projects" / "D--AI-Atlas-test" / "memory"
    env.bash(": team-type coding")
    for i in range(8):
        p = str(mem / ("m%d.md" % i))
        env.run("PostToolUse", tool_name="Write", tool_input={"file_path": p})
    S = env.state()
    assert S["main_edits"] == 0 and S["builder_edits"] == 0 and not env.events("edit")
    for n in "abcdef":
        env.edit(n + ".py")
    env.set_mode("enforce")
    out = env.run("PreToolUse", tool_name="Write", tool_input={"file_path": str(mem / "m1.md")})
    assert not denied(out)
    assert denied(env.pre_edit("g.py"))


def test_memory_path_matcher():
    import atlas_hook
    h = Path.home() / ".claude" / "projects" / "p1" / "memory"
    assert atlas_hook.memory_path(str(h / "x.md"))
    assert atlas_hook.memory_path(str(h / "sub" / "x.md"))
    assert not atlas_hook.memory_path(str(Path.home() / ".claude" / "projects" / "p1" / "notes.md"))
    assert not atlas_hook.memory_path(str(Path.home() / ".claude" / "memory" / "x.md"))
    assert not atlas_hook.memory_path(str(Path.home() / "memory" / "x.md"))
    assert not atlas_hook.memory_path(str(Path.home() / ".claude" / "projects" / "memory" / "x.md"))   # project named memory
    assert not atlas_hook.memory_path(str(Path.home() / ".claude" / "projects" / "p1" / "a" / "memory" / "x.md"))   # nested


def test_no_stop_when_the_crew_has_no_builder(env, tmp_path):
    wf = (REPO / "workflows.yaml").read_text(encoding="utf-8")
    wf = wf.replace("types:\n", "types:\n  recon:\n    label: Recon\n    crew: [scout]\n    budget: 2\n"
                    "    rung: \"1\"\n    needs_check: true\n    move: x\n", 1)
    p = tmp_path / "wf-recon.yaml"
    p.write_text(wf, encoding="utf-8")
    env.cfg["workflows_file"] = str(p)
    env.set_mode("enforce")
    env.bash(": team-type recon")
    assert env.state()["type"] == "recon"
    for n in "abcdefgh":
        env.edit(n + ".py")
    assert not denied(env.pre_edit("z.py"))


def test_research_task_is_never_stopped_but_coding_is(env):
    env.set_mode("enforce")
    env.bash(": team-type research")
    assert env.state()["type"] == "research"
    for n in "abcdef":
        env.edit(n + ".py")
    assert not denied(env.pre_edit("g.py"))               # 7th edit: researcher/sweeper cannot reset the stop
    env.bash(": team-type coding")
    for n in "abcdef":
        env.edit(n + ".py")
    assert denied(env.pre_edit("g.py"))


# ------------------------------------------------------------------ 3. test-runner reminder

def implementer_returns(env, tuid="b1", aid="B1"):
    env.agent("implementer", tuid)
    env.run("SubagentStart", agent_id=aid, agent_type="implementer")
    env.run("SubagentStop", agent_id=aid, agent_type="implementer", last_assistant_message="done")


def test_reminder_when_implementer_returned_and_no_test_runner(env):
    env.bash(": team-type coding")
    assert "test-runner" not in ctx(env.agent("reviewer", "r0"))      # no implementer yet
    implementer_returns(env)
    env.set_mode("enforce")
    out = env.agent("reviewer", "r1")
    assert not denied(out)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "test-runner" in ctx(out) and "—" not in ctx(out)
    assert "test-runner" in ctx(env.agent("senior-reviewer", "r2"))   # either tier


def test_no_reminder_after_a_test_runner_launch(env):
    env.bash(": team-type coding")
    implementer_returns(env)
    env.agent("test-runner", "t1")
    assert "test-runner" not in ctx(env.agent("reviewer", "r1"))
    assert "test-runner" not in ctx(env.agent("senior-reviewer", "r2"))


def test_a_test_runner_launched_before_the_return_does_not_count(env):
    env.bash(": team-type coding")
    env.agent("implementer", "b1")
    env.run("SubagentStart", agent_id="B1", agent_type="implementer")
    env.agent("test-runner", "t1")                                    # launched while the implementer still ran
    env.run("SubagentStop", agent_id="B1", agent_type="implementer", last_assistant_message="done")
    assert "test-runner" in ctx(env.agent("reviewer", "r1"))


def test_a_second_implementer_return_reminds_again(env):
    env.bash(": team-type coding")
    implementer_returns(env)
    env.agent("test-runner", "t1")
    assert "test-runner" not in ctx(env.agent("reviewer", "r1"))
    implementer_returns(env, "b2", "B2")
    assert "test-runner" in ctx(env.agent("reviewer", "r2"))


def test_no_reminder_for_other_types_or_non_reviewers(env):
    env.bash(": team-type writing")
    implementer_returns(env)
    assert "test-runner" not in ctx(env.agent("reviewer", "r1"))
    env.bash(": team-type coding")
    implementer_returns(env, "b2", "B2")
    assert "test-runner" not in ctx(env.agent("scout", "s1"))
    assert "test-runner" not in ctx(env.agent("implementer", "b3"))


def test_reminder_state_resets_with_the_task(env):
    env.bash(": team-type coding")
    implementer_returns(env)
    env.bash(": team-type writing")
    env.bash(": team-type coding")
    assert env.state()["impl_ret"] is None
    assert "test-runner" not in ctx(env.agent("reviewer", "r1"))


# ------------------------------------------------------------------ 5. state files from the single-tier hook

def _strip(env, keys):
    p = env.ledger / "state" / ("%s.json" % SID)
    S = json.loads(p.read_text(encoding="utf-8"))
    for k in keys:
        S.pop(k, None)
    p.write_text(json.dumps(S), encoding="utf-8")


OLD_KEYS = ["nc_rounds_t", "nc_at_t", "builder_edits", "ord_n", "impl_ret", "tr_launch"]


def test_old_state_file_keeps_a_tripped_task_tripped(env):
    env.bash(": team-type chore")                         # budget 1
    fail(env, 2, "reviewer", "a")
    env.set_mode("enforce")
    assert denied(env.agent("implementer", "b1"))
    _strip(env, OLD_KEYS)
    assert denied(env.agent("implementer", "b2"))
    S = env.state()
    assert S["nc_rounds_t"] == {"routine": 2} and "builder_edits" in S    # re-derived from the verdict lines


def test_old_state_fallback_migrates_in_place(env):
    import atlas_hook as ah
    env.bash(": team-type chore")
    fail(env, 2, "reviewer", "a")
    _strip(env, OLD_KEYS)
    p = env.ledger / "state" / ("%s.json" % SID)

    class H:
        sid = SID
        ledger = str(env.ledger)
        wf = {}

    def boom(_H):
        raise RuntimeError("no rebuild")

    real, ah.rebuild_state = ah.rebuild_state, boom
    try:
        S = ah.load_state(H)
    finally:
        ah.rebuild_state = real
    assert S["nc_rounds_t"] == {"routine": 2} and S["nc_at_t"] == {"senior": S["nc_at"], "routine": S["nc_at"]}
    assert ah.tripped(S)                                  # the old answer is kept
    assert p.exists()


def test_refusal_logging_light_txn_does_not_replay_but_the_saving_one_does(env):
    import atlas_hook as ah
    env.bash(": team-type chore")
    fail(env, 2, "reviewer", "a")
    _strip(env, OLD_KEYS)
    p = env.ledger / "state" / ("%s.json" % SID)
    before = p.read_text(encoding="utf-8")
    task = json.loads(before)["task"]

    class H:
        sid = SID
        ledger = str(env.ledger)
        wf = {}

    calls = []
    real = ah.rebuild_state
    ah.rebuild_state = lambda _H: (calls.append(1), real(_H))[1]
    try:
        with ah.Txn(H, light=True, migrate=False) as tx:      # the refusal-logging shape
            tx.nosave = True
            assert tx.state.get("task") == task
        assert calls == [] and p.read_text(encoding="utf-8") == before   # no replay, nothing saved
        with ah.Txn(H, light=True, migrate=False) as tx:      # a caller that forgets nosave still never writes
            assert tx.nosave is True
            tx.state["task"] = "changed"
        assert p.read_text(encoding="utf-8") == before
        with ah.Txn(H, light=True) as tx:                     # the grant-consuming shape: migrates, then saves
            assert tx.state["nc_rounds_t"] == {"routine": 2}
        assert calls == [1]
    finally:
        ah.rebuild_state = real
    assert json.loads(p.read_text(encoding="utf-8"))["nc_rounds_t"] == {"routine": 2}


def test_old_state_file_rebuilds_the_main_edit_count(env):
    env.bash(": team-type coding")
    env.set_mode("enforce")
    for n in "abcdef":
        env.edit(n + ".py")
    assert env.state()["builder_edits"] == 6
    _strip(env, OLD_KEYS)
    assert denied(env.pre_edit("g.py"))
    assert env.state()["builder_edits"] == 6


def _rewrite(env, **kv):
    p = env.ledger / "state" / ("%s.json" % SID)
    S = json.loads(p.read_text(encoding="utf-8"))
    S.update(kv)
    p.write_text(json.dumps(S), encoding="utf-8")


def test_light_transaction_migrates_an_old_state_and_stays_tripped(env):
    env.run("SessionStart")
    env.bash(": team-type chore")
    fail(env, 2, "reviewer", "a")
    env.set_mode("enforce")
    assert denied(env.agent("implementer", "b1"))
    env.run("UserPromptSubmit", prompt="/allow-publish")
    _strip(env, OLD_KEYS)
    assert not denied(env.bash("git push origin main"))   # allowed publish: light transaction, then saved
    S = env.state()
    assert S["nc_rounds_t"] == {"routine": 2} and "builder_edits" in S
    assert denied(env.agent("implementer", "b2"))


def test_old_state_fallback_when_the_rebuilt_task_differs(env):
    env.bash(": team-type chore")
    fail(env, 2, "reviewer", "a")
    env.set_mode("enforce")
    _strip(env, OLD_KEYS)
    _rewrite(env, task="other-9")                         # the replay finds another task than the file's
    assert denied(env.agent("implementer", "b1"))
    S = env.state()
    assert S["task"] == "other-9" and S["nc_rounds_t"] == {"routine": S["nc_rounds"]}
    assert S["nc_rounds_t"]["routine"] == 2


def test_old_state_with_mixed_tiers_is_at_least_as_strict(env):
    env.bash(": team-type chore")                         # budget 1
    fail(env, 2, "reviewer", "a")
    fail(env, 1, "senior-reviewer", "s")
    env.set_mode("enforce")
    assert denied(env.agent("implementer", "b1"))
    _strip(env, OLD_KEYS)
    assert denied(env.agent("implementer", "b2"))
    t = env.state()["nc_rounds_t"]
    assert t.get("routine") == 2 and t.get("senior") == 1
    # the fallback (total kept as routine) is never laxer
    _strip(env, OLD_KEYS)
    _rewrite(env, task="other-9")
    assert denied(env.agent("implementer", "b3"))
    assert env.state()["nc_rounds_t"]["routine"] == 3     # the old total (2 routine + 1 senior): a dropped round fails here


def test_undeclared_task_is_stopped_at_the_seventh_edit(env):
    env.set_mode("enforce")
    for n in "abcdef":
        env.edit(n + ".py")                               # the first edit opens an undeclared task
    assert env.state()["type"] == "undeclared"
    assert denied(env.pre_edit("g.py"))


def test_playbook_crews_decide_the_edit_stop(env):
    for cmd, stopped in (("daily-review", True), ("tune-up", True), ("weekly-report", False), ("atlas-check", False)):
        e = env
        e.set_mode("enforce")
        e.run("UserPromptSubmit", prompt="/" + cmd)
        e.run("UserPromptExpansion", command_name=cmd, expansion_type="slash_command")
        S = e.state()
        assert S["type"] == "playbook" and S["playbook_last"]["cmd"] == cmd, (cmd, S["type"], S["playbook_last"])
        for n in "abcdef":
            e.edit(n + ".py")
        assert denied(e.pre_edit("g.py")) is stopped, cmd
        env.bash(": team-type chore")                     # leave the playbook task
