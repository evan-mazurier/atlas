"""`: team-done` on a task with an open failing review, worked through the shell only and over its budget. Temp ledgers
only. (The general closing rule is covered in test_abandon_recall.py.)"""
from test_atlas_hook import _age_task, env  # noqa: F401  (env is a fixture)
from test_abandon_recall import OLD, denied, shell_only

REASON = "last review of this task did not pass"


def test_shell_only_tripped_task_cannot_be_closed_in_enforce(env):
    shell_only(env, 3)
    S = env.state()
    assert S["version"] == 0 and S["needs_changes"] == 3 and S["budget"] == 2 and S["open"]
    env.set_mode("enforce")
    out = env.bash(": team-done")
    assert denied(out) and REASON in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not env.events("task_done") and env.state()["open"] is True
    r = env.events("refusal")[-1]
    assert r["guard"] == "done_lock" and r["would"] is False
    _age_task(env)
    env.bash(": team-type writing")                          # the next declaration abandons it
    assert len(env.events("task_abandoned")) == 1 and env.events("task_abandoned")[0]["task"] == OLD


def test_shell_only_tripped_task_in_record_mode_logs_and_closes(env):
    shell_only(env, 3)
    assert env.bash(": team-done") is None
    r = env.events("refusal")[-1]
    assert r["guard"] == "done_lock" and r["would"] is True and REASON in r["detail"]
    assert len(env.events("task_done")) == 1 and env.state()["open"] is False


def test_tripped_then_ready_from_a_later_reviewer_closes(env):
    shell_only(env, 3)
    env.set_mode("enforce")
    env.review("ok1", "OK1", "READY")
    assert env.bash(": team-done") is None
    assert len(env.events("task_done")) == 1


def test_the_version_check_still_refuses_on_its_own(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.set_mode("enforce")
    out = env.bash(": team-done")
    assert denied(out) and "has no passing review" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not env.events("task_done")
