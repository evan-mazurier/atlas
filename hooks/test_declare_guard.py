"""Tests for the declare guard (contracts 6.6a): the main session launches a helper only inside a declared piece of work."""
from test_atlas_hook import Env, env  # noqa: F401  (env is the fixture)

DENY = "Declare the work before launching a helper: run `: team-type <type> [area]` on its own"


def denied(out):
    return bool(out) and out["hookSpecificOutput"].get("permissionDecision") == "deny"


def declare_refusals(env):
    return [r for r in env.events("refusal") if r["guard"] == "declare"]


def test_no_task_refused_in_enforce(env):
    env.set_mode("enforce")
    out = env.agent("reviewer", "t1")
    assert denied(out)
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith(DENY)
    r = declare_refusals(env)[-1]
    assert r["would"] is False and r["helper"] == "reviewer"


def test_no_task_allowed_and_logged_in_record(env):
    assert env.agent("reviewer", "t1") is None
    r = declare_refusals(env)
    assert len(r) == 1 and r[0]["would"] is True


def test_declared_task_allows(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    assert env.agent("implementer", "t1") is None
    assert not declare_refusals(env)


def test_team_done_closes_the_declaration(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    assert env.agent("implementer", "t1") is None
    env.bash(": team-done")
    assert denied(env.agent("reviewer", "t2"))
    env.bash(": team-type research")
    assert env.agent("researcher", "t3") is None


def test_playbook_task_allows(env):
    env.set_mode("enforce")
    env.run("UserPromptSubmit", prompt="/daily-review")
    env.run("UserPromptExpansion", command_name="daily-review", expansion_type="slash_command")
    assert env.events("playbook")
    assert env.agent("sweeper", "t1") is None
    assert not declare_refusals(env)


def test_quick_question_counts_as_declared(env):
    env.set_mode("enforce")
    env.bash(": team-type quick-question")
    assert env.agent("scout", "t1") is None
    assert denied(env.agent("implementer", "t2"))
    assert [r["guard"] for r in env.events("refusal")] == ["quick_question"]


def test_helper_scope_is_never_refused(env):
    env.set_mode("enforce")
    out = env.run("PreToolUse", tool_name="Agent", tool_use_id="n1", agent_id="A1", agent_type="implementer",
                  tool_input={"subagent_type": "scout", "description": "x", "run_in_background": False})
    assert out is None
    assert not declare_refusals(env)


def test_implicit_undeclared_task_is_refused(env):
    env.set_mode("enforce")
    env.edit("a.py")
    assert env.events("work_type")[0]["type"] == "undeclared"
    assert denied(env.agent("reviewer", "t1"))
    env.bash(": team-type coding")
    assert env.agent("reviewer", "t2") is None
