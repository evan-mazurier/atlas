"""3-edit rule reminder (contracts 6.9): every 3rd main-session edit on a task gets a reminder in additionalContext.
Reminder only, no event. Subprocess runs against temp ledgers; never live data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_atlas_hook import SID, env  # noqa: E402,F401  (env is a fixture)
import atlas_hook  # noqa: E402


def ctx(out):
    return out["hookSpecificOutput"]["additionalContext"] if out else None


def is_nudge(out, nth):
    c = ctx(out)
    return bool(c) and c.startswith("3-edit rule: this is the main session's %s edit" % nth)


def test_every_third_main_edit_only(env):
    env.bash(": team-type coding")
    assert env.edit("a.py") is None
    assert env.edit("b.py") is None
    assert is_nudge(env.edit("c.py"), "3rd")
    assert env.edit("d.py") is None
    assert env.edit("e.py") is None
    assert is_nudge(env.edit("f.py"), "6th")
    assert env.edit("g.py") is None and env.edit("h.py") is None
    assert is_nudge(env.edit("i.py"), "9th")
    assert not env.events("reminder")           # no event kind for it
    assert len(env.events("edit")) == 9


def test_helper_edits_do_not_count(env):
    env.bash(": team-type coding")
    env.run("SubagentStart", agent_id="B1", agent_type="implementer")
    for n in "abcd":
        assert env.edit(n + ".py", agent_id="B1", agent_type="implementer") is None
    assert env.edit("m1.py") is None
    assert env.edit("m2.py") is None
    assert is_nudge(env.edit("m3.py"), "3rd")


def test_new_task_resets_the_counter(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.edit("b.py")
    env.bash(": team-type writing")          # same type within 60 s would be a no-op duplicate
    assert env.edit("c.py") is None
    assert env.edit("d.py") is None
    assert is_nudge(env.edit("e.py"), "3rd")


def test_type_without_builder_gets_nothing(env):
    env.bash(": team-type quick-question")
    for n in "abcd":
        assert env.edit(n + ".py") is None


def test_check_failure_and_reminder_are_joined(env):
    repo = env.root / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "tools" / "check_look.py").write_text(
        "import json,sys\nprint(json.dumps({'ok': False, 'problems': ['colour md too close']}))\nsys.exit(1)\n")
    f = repo / "areas.yaml"
    f.write_text("areas: []\n")
    env.bash(": team-type coding")
    env.edit("a.py")
    env.edit("b.py")
    out = env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": str(f)})
    c = ctx(out)
    assert "colour md too close" in c and "3-edit rule: this is the main session's 3rd edit" in c


def test_exception_in_nudge_returns_normal_output(monkeypatch, tmp_path):
    emitted = []

    class FakeTxn:
        def __init__(self, H):
            self.state = {"open": True, "task": "t-1", "version": 0, "main_edits": 3, "type": "coding"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def emit(self, e):
            emitted.append(e)

    class H:
        tool = "Edit"
        cfg = {"exempt_temp": False}
        ledger = str(tmp_path / "ledger")
        agent_id = None
        is_config = staticmethod(lambda p: False)
        is_protected = staticmethod(lambda p: False)
        is_exempt = staticmethod(lambda p: False)

        @property
        def wf(self):
            raise RuntimeError("workflows broke")

    monkeypatch.setattr(atlas_hook, "Txn", FakeTxn)
    monkeypatch.setattr(atlas_hook, "edit_path", lambda H: str(tmp_path / "x.txt"))
    monkeypatch.setattr(atlas_hook, "refresh_json_copies", lambda H, p: None)
    assert atlas_hook.h_post_edit(H()) is None
    assert [e["ev"] for e in emitted] == ["edit"]


def test_undeclared_always_reminded(env):
    for n in "ab":
        assert env.edit(n + ".py") is None
    assert is_nudge(env.edit("c.py"), "3rd")


def test_playbook_uses_its_own_crew(env):
    env.run("UserPromptExpansion", command_name="daily-review", expansion_type="slash_command")   # crew has drafter
    assert env.state()["type"] == "playbook"
    assert env.edit("a.py") is None and env.edit("b.py") is None
    assert is_nudge(env.edit("c.py"), "3rd")


def test_playbook_without_builder_gets_nothing(env):
    env.run("UserPromptExpansion", command_name="weekly-report", expansion_type="slash_command")   # crew: []
    assert env.state()["type"] == "playbook"
    for n in "abcd":
        assert env.edit(n + ".py") is None


def _fake(wf, state):
    class H:
        agent_id = None
        builders = ["implementer", "drafter"]
    H.wf = wf
    return H, dict({"main_edits": 3}, **state)


def test_unlisted_playbook_or_missing_name_fires():
    wf = {"types": {"playbook": {"crew": []}}, "playbook_crews": {"x": {"crew": []}}}
    assert atlas_hook.edit_nudge(*_fake(wf, {"type": "playbook", "playbook_last": {"cmd": "nope"}}))
    assert atlas_hook.edit_nudge(*_fake(wf, {"type": "playbook"}))
    assert not atlas_hook.edit_nudge(*_fake(wf, {"type": "playbook", "playbook_last": {"cmd": "x"}}))


def test_malformed_crew_fires():
    for bad in ("scout", {"a": 1}, None):
        wf = {"types": {"coding": {"crew": bad}}}
        assert atlas_hook.edit_nudge(*_fake(wf, {"type": "coding"}))
