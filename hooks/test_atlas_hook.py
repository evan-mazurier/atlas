"""Tests for hooks/atlas_hook.py. Every run is a real subprocess against temp dirs (env overrides only)."""
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SID = "s1abcdefgh"


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.hooks = root / "hooks"
        self.ledger = root / "ledger"
        self.work = root / "work"
        self.work.mkdir()
        self.hooks.mkdir()
        shutil.copyfile(HERE / "atlas_hook.py", self.hooks / "atlas_hook.py")
        shutil.copyfile(REPO / "cockpit" / "ledger2" / "areas.py", self.hooks / "areas.py")
        shutil.copyfile(REPO / "cockpit" / "ledger2" / "ledger_chain.py", self.hooks / "ledger_chain.py")
        cfg = json.loads((HERE / "atlas_config.json").read_text(encoding="utf-8"))
        cfg["workflows_file"] = str(REPO / "workflows.yaml")
        cfg["areas_file"] = str(REPO / "areas.yaml")
        cfg["exempt_temp"] = False
        self.cfg = cfg
        self.set_mode("record")
        self.n = 0

    def set_mode(self, mode):
        self.cfg["mode"] = mode
        (self.hooks / "atlas_config.json").write_text(json.dumps(self.cfg), encoding="utf-8")

    def env(self):
        e = dict(os.environ, ATLAS_HOOKS_DIR=str(self.hooks), ATLAS_LEDGER2_DIR=str(self.ledger))
        e["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        return e

    def run_raw(self, text, extra_env=None):
        e = self.env()
        e.update(extra_env or {})
        r = subprocess.run([sys.executable, str(self.hooks / "atlas_hook.py")], input=text,
                           capture_output=True, text=True, timeout=30, env=e)
        return r

    def run(self, event, sid=SID, extra_env=None, **kw):
        self.n += 1
        d = {"hook_event_name": event, "session_id": sid, "cwd": str(self.work),
             "tool_use_id": "tu%d" % self.n}
        d.update(kw)
        r = self.run_raw(json.dumps(d), extra_env)
        assert r.returncode == 0, r.stderr
        out = r.stdout.strip()
        return json.loads(out) if out else None

    def bash(self, cmd, **kw):
        return self.run("PreToolUse", tool_name="Bash", tool_input={"command": cmd}, **kw)

    def edit(self, name="a.py", sid=SID, **kw):
        p = str(self.work / name)
        self.run("PreToolUse", tool_name="Edit", tool_input={"file_path": p}, sid=sid, **kw)
        return self.run("PostToolUse", tool_name="Edit", tool_input={"file_path": p}, sid=sid, **kw)

    def pre_edit(self, name="a.py", **kw):
        return self.run("PreToolUse", tool_name="Edit", tool_input={"file_path": str(self.work / name)}, **kw)

    def agent(self, helper, tuid, bg=False, label="job"):
        return self.run("PreToolUse", tool_name="Agent", tool_use_id=tuid,
                        tool_input={"subagent_type": helper, "description": label, "run_in_background": bg})

    def events(self, ev=None):
        out = []
        for p in sorted(self.ledger.glob("events-*.jsonl")) if self.ledger.exists() else []:
            for line in p.read_text(encoding="utf-8").splitlines():
                out.append(json.loads(line))
        return [e for e in out if ev is None or e["ev"] == ev]

    def state(self, sid=SID):
        return json.loads((self.ledger / "state" / f"{sid}.json").read_text(encoding="utf-8"))

    def review(self, tuid, aid, text, version_edit=None, helper="reviewer"):
        self.agent(helper, tuid)
        self.run("SubagentStart", agent_id=aid, agent_type=helper)
        self.run("PreToolUse", tool_name="SubagentHandback", agent_id=aid, agent_type=helper,
                 tool_input={"message": text})
        self.run("SubagentStop", agent_id=aid, agent_type=helper, last_assistant_message="")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def trip(env):
    """Open a coding task, edit, and burn three NEEDS_CHANGES versions (budget 2)."""
    env.bash(": team-type coding")
    for i in range(1, 4):
        env.edit("a.py")
        env.review("r%d" % i, "A%d" % i, "NEEDS_CHANGES\n- one\n- two")
    assert env.state()["needs_changes"] == 3


# ------------------------------------------------------------------ fail open

@pytest.mark.parametrize("text", ["", "not json", "[]", "{}", '{"session_id": 5}', "\xff\xfe", "null"])
def test_garbage_stdin_exits_zero_empty_stdout(env, text):
    r = env.run_raw(text)
    assert r.returncode == 0
    assert r.stdout == ""


def test_broken_config_still_fails_open(env):
    (env.hooks / "atlas_config.json").write_text("{nope", encoding="utf-8")
    assert env.bash(": team-type coding") is None
    assert env.events("work_type")


def test_unwritable_ledger_fails_open(env, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    r = env.run_raw(json.dumps({"hook_event_name": "SessionStart", "session_id": SID, "cwd": "x"}),
                    {"ATLAS_LEDGER2_DIR": str(blocker / "sub")})
    assert r.returncode == 0 and r.stdout == ""


# ------------------------------------------------------------------ logger

def test_session_and_prompt_events_carry_no_text(env):
    env.run("SessionStart", source="startup", transcript_path="t.jsonl")
    env.run("UserPromptSubmit", prompt="/daily-review secret words here")
    env.run("SessionEnd", reason="clear")
    evs = env.events()
    assert [e["ev"] for e in evs] == ["session_start", "prompt", "session_end"]
    assert evs[1]["cmd"] == "daily-review"
    assert "secret" not in json.dumps(evs)
    assert all(e["v"] == 2 and e["mode"] == "record" and e["sid"] == SID for e in evs)


def test_work_type_area_alias_and_noop(env):
    assert env.bash(": team-type coding atlas") is None
    wt = env.events("work_type")[0]
    assert wt["type"] == "coding" and wt["area"] == "atlas" and wt["task"] == "s1abcdef-1"
    env.bash(": team-type coding")           # same type within 60 s: accidental duplicate, no-op
    assert len(env.events("work_type")) == 1
    assert env.events("ignored")
    env.bash(": team-type quick")            # alias
    assert env.events("work_type")[-1]["type"] == "quick-question"
    assert env.events("work_type")[-1]["task"] == "s1abcdef-2"


def test_team_commands_from_a_helper_are_ignored(env):
    env.bash(": team-type coding", agent_id="A9", agent_type="implementer")
    assert not env.events("work_type")
    assert env.events("ignored")


def test_summary_event(env):
    env.bash(": team-type coding")
    env.bash(""": team-summary '{"goal": "g", "produced": "p", "decisions": "d", "open": "o", "review_min": 20}'""")
    s = env.events("summary")[0]
    assert s["goal"] == "g" and s["review_min"] == 20 and s["task"] == "s1abcdef-1"


def test_playbook_opens_once_within_10s(env):
    env.run("UserPromptSubmit", prompt="/daily-review")
    env.run("UserPromptExpansion", command_name="daily-review", expansion_type="slash_command")
    env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "daily-review"})
    pb = env.events("playbook")
    assert len(pb) == 1 and pb[0]["area"] == "job"


def test_implicit_undeclared_task_on_edit(env):
    env.edit("a.py")
    assert env.events("work_type")[0]["type"] == "undeclared"
    assert env.events("edit")[0]["version"] == 1


def test_exempt_paths_are_not_edits(env):
    env.run("PostToolUse", tool_name="Write", tool_input={"file_path": str(env.work / "x.build-log.md")})
    env.run("PostToolUse", tool_name="Write",
            tool_input={"file_path": str(Path.home() / ".claude" / "plans" / "p.md")})
    assert not env.events("edit")


def test_fast_path_writes_nothing(env):
    assert env.bash("ls -la") is None
    assert not env.ledger.exists()


# ------------------------------------------------------------------ rework guard

def test_rework_guard_record_allows_and_logs_would(env):
    trip(env)
    out = env.agent("implementer", "b1", label="Fix the findings")
    # record mode: allowed, with the once-per-task reminder as non-blocking context
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert ": team-type <type> [area]" in out["hookSpecificOutput"]["additionalContext"]
    assert env.agent("implementer", "b0") is None
    r = [e for e in env.events("refusal") if e.get("tuid") == "b1"][0]
    assert r["guard"] == "rework" and r["would"] is True
    assert r["helper"] == "implementer" and r["label"] == "Fix the findings" and r["tuid"] == "b1"
    assert env.pre_edit("a.py") is None          # main-session edit also only logged
    assert [e["would"] for e in env.events("refusal")][-2:] == [True, True]


def test_rework_guard_enforce_denies_with_documented_json(env):
    trip(env)
    env.set_mode("enforce")
    out = env.agent("implementer", "b1")
    h = out["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse" and h["permissionDecision"] == "deny"
    assert h["permissionDecisionReason"].startswith("Rework budget spent for this coding task (3 NEEDS CHANGES, budget 2).")
    assert "/grant-rework" in h["permissionDecisionReason"]
    assert env.events("refusal")[-1]["would"] is False
    assert not [e for e in env.events("helper_start") if e["tuid"] == "b1"]
    assert env.agent("reviewer", "r9") is None   # reviewers are never refused
    assert env.agent("scout", "s9") is None
    assert env.pre_edit("a.py")["hookSpecificOutput"]["permissionDecision"] == "deny"
    # a helper's own edit is not refused by this guard
    assert env.pre_edit("a.py", agent_id="Z", agent_type="implementer") is None


def test_budget_zero_and_distinct_versions(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    # reviewer and senior-reviewer both fail the SAME version: one round burned
    env.review("r1", "A1", "NEEDS_CHANGES\n- x")
    env.review("r2", "A2", "NEEDS_CHANGES\n- y", helper="senior-reviewer")
    assert env.state()["needs_changes"] == 1


def test_grant_rework_from_user_prompt(env):
    trip(env)
    env.set_mode("enforce")
    assert env.agent("implementer", "b1") is not None
    env.run("UserPromptSubmit", prompt="/grant-rework 2")
    env.run("UserPromptExpansion", command_name="grant-rework", expansion_type="slash_command")
    g = env.events("rework_granted")
    assert len(g) == 1 and g[0]["n"] == 2          # the expansion did not double it
    assert env.agent("implementer", "b2") is None   # 3 > 2 + 2 is false
    assert env.state()["granted"] == 2


def test_grant_requires_prompt_start_and_open_task(env):
    env.run("UserPromptSubmit", prompt="/grant-rework")
    assert not env.events("rework_granted")
    env.bash(": team-type coding")
    env.run("UserPromptSubmit", prompt="please run /grant-rework 3")
    assert not env.events("rework_granted")


def test_grant_entry_check_uses_interactive_entries(env):
    env.bash(": team-type coding")
    # empty list = the fixed fallback set (cli, claude-desktop, claude-vscode), never "any entry"
    env.run("UserPromptSubmit", prompt="/grant-rework", extra_env={"CLAUDE_CODE_ENTRYPOINT": "weird-entry"})
    env.run("UserPromptSubmit", prompt="/grant-rework", extra_env={"CLAUDE_CODE_ENTRYPOINT": ""})
    assert not env.events("rework_granted")
    env.run("UserPromptSubmit", prompt="/grant-rework", extra_env={"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"})
    assert env.events("rework_granted")[0]["entry"] == "claude-vscode"
    env.cfg["interactive_entries"] = ["claude-desktop", "cli"]
    env.set_mode("record")
    env.run("UserPromptSubmit", prompt="/grant-rework", extra_env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert len(env.events("rework_granted")) == 1
    env.run("UserPromptSubmit", prompt="/grant-rework", extra_env={"CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert len(env.events("rework_granted")) == 2


@pytest.mark.parametrize("mode", ["record", "enforce"])
@pytest.mark.parametrize("cmd", [
    'claude --resume abc123 -p "/grant-rework 5"',
    "cd x && claude -p '/grant-rework'",
    "claude.exe --print hi",
    "~\\.local\\bin\\claude.exe -p hello",
    "~/.local/bin/claude.exe --resume abc -p x",
    "C:/x/claude.exe -p hi",
    "cd x && claude --resume abc",
    "Claude -p x",
    "CLAUDE.EXE -p x",
    "/c/bin/claude -p x",
])
def test_forged_grant_via_shell_denied_in_every_mode(env, mode, cmd):
    env.set_mode(mode)
    for tool in ("Bash", "PowerShell"):
        out = env.run("PreToolUse", tool_name=tool, tool_input={"command": cmd})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert env.run("PreToolUse", tool_name="Bash", tool_input={"command": cmd}, agent_id="H") is not None


@pytest.mark.parametrize("cmd", [
    "mkdir -p ~/.claude/skills/grant-rework",
    "ls -p ~/.claude",
    "git add hooks/skills/grant-rework/SKILL.md",
    'git commit -m "add /grant-rework skill"',
    'grep -n "/grant-rework" docs',
    "claude --version",
    "which claude",
    "cat ~/.claude/CLAUDE.md",
    "pip install -p x; echo claude",
    "/usr/local/bin/claude-code -p",
    'git commit -m "docs: explain the /grant-rework flow"',
    'grep -rn "/grant-rework" docs hooks',
    "echo /grant-rework 3",
    "ls ~/.claude/skills/ -p",
    "cat ~/.claude/projects/x.jsonl -p",
])
def test_no_false_deny_on_required_commands(env, cmd):
    env.set_mode("enforce")
    assert env.bash(cmd) is None


def test_model_cannot_invoke_grant_skill_even_in_record_mode(env):
    out = env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "grant-rework"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ------------------------------------------------------------------ type switch carry-over

def test_type_switch_carries_spent_budget(env):
    trip(env)
    env.bash(": team-type writing")
    wt = env.events("work_type")[-1]
    assert wt["type"] == "writing" and wt["carried"] is True
    S = env.state()
    assert S["needs_changes"] == 3 and S["budget"] == 2 and S["task"] == "s1abcdef-2"
    assert env.events("refusal")[-1]["guard"] == "rework"


def test_type_switch_without_spent_budget_resets(env):
    env.bash(": team-type coding")
    env.bash(": team-type writing")
    S = env.state()
    assert S["needs_changes"] == 0 and S["version"] == 0


# ------------------------------------------------------------------ done-lock

def test_done_lock_record_mode_allows_and_logs(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    assert env.bash(": team-done") is None
    r = env.events("refusal")[-1]
    assert r["guard"] == "done_lock" and r["would"] is True
    assert env.events("task_done")


def test_done_lock_enforce_denies_then_allows_after_ready(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.edit("a.py")
    out = env.bash(": team-done")
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.startswith("Not closed: version 1 has no passing review (last READY at version 0).")
    assert not env.events("task_done")
    env.review("r1", "A1", "READY")
    assert env.bash(": team-done") is None
    assert env.events("task_done")[0]["version"] == 1


def test_done_lock_stale_ready_does_not_count(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.edit("a.py")
    env.review("r1", "A1", "READY")
    env.edit("b.py")
    assert env.bash(": team-done") is not None


def test_done_with_no_edits_is_allowed(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    assert env.bash(": team-done") is None


# ------------------------------------------------------------------ quick-question lock

def test_quick_question_lock(env):
    env.bash(": team-type quick-question")
    assert env.pre_edit("a.py") is None
    assert env.events("refusal")[-1]["guard"] == "quick_question"
    assert env.events("refusal")[-1]["would"] is True
    env.set_mode("enforce")
    out = env.pre_edit("a.py")
    assert out["hookSpecificOutput"]["permissionDecisionReason"].startswith("A quick question can't change files")
    assert env.agent("implementer", "t1")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert env.agent("scout", "t2") is None


# ------------------------------------------------------------------ Stop send-back

def test_stop_sendback_record_then_enforce(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    assert env.run("Stop", stop_hook_active=False) is None
    sb = env.events("sendback")
    assert len(sb) == 1 and sb[0]["would"] is True and sb[0]["version"] == 1


def test_stop_sendback_enforce_blocks_once_per_version(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.edit("a.py")
    out = env.run("Stop", stop_hook_active=False)
    assert out["decision"] == "block"
    assert out["reason"].startswith("Unchecked changes (version 1, last check at 0).")
    assert env.run("Stop", stop_hook_active=False) is None
    env.edit("b.py")
    assert env.run("Stop", stop_hook_active=True) is None      # stop_hook_active wins
    assert env.run("Stop", stop_hook_active=False)["decision"] == "block"


def test_stop_no_sendback_when_checked_or_builder_running(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("implementer", "b1", bg=True)
    assert env.run("Stop") is None                              # builder still running (< 2 h)
    env.run("SubagentStart", agent_id="B1", agent_type="implementer")
    env.run("SubagentStop", agent_id="B1", agent_type="implementer")
    env.review("r1", "A1", "READY")
    assert env.run("Stop") is None


def test_stop_ignores_builder_older_than_two_hours(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("implementer", "b1", bg=True)
    st = env.state()
    st["pending"]["b1"]["t"] = "2020-01-01T00:00:00+00:00"
    (env.ledger / "state" / f"{SID}.json").write_text(json.dumps(st), encoding="utf-8")
    assert env.run("Stop")["decision"] == "block"


# ------------------------------------------------------------------ verdict capture and pairing

def test_verdict_from_handback_payload(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("reviewer", "r1")
    env.run("SubagentStart", agent_id="A1", agent_type="reviewer")
    env.run("PreToolUse", tool_name="SubagentHandback", agent_id="A1", agent_type="reviewer",
            tool_input={"message": "**Verdict: NEEDS CHANGES**\n- first\n* second\n3. third\nplain"})
    v = env.events("verdict")
    assert len(v) == 1
    assert v[0]["verdict"] == "NEEDS_CHANGES" and v[0]["findings"] == 3
    assert v[0]["version"] == 1 and v[0]["task"] == "s1abcdef-1" and v[0]["agent_id"] == "A1"
    # SubagentStop for the same agent: no second verdict
    env.run("SubagentStop", agent_id="A1", agent_type="reviewer", last_assistant_message="READY")
    assert len(env.events("verdict")) == 1
    assert len(env.events("helper_end")) == 1


def test_verdict_from_subagent_stop(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("senior-reviewer", "r1")
    env.run("SubagentStart", agent_id="A1", agent_type="senior-reviewer")
    env.run("SubagentStop", agent_id="A1", agent_type="senior-reviewer",
            last_assistant_message="\n  ESCALATE TO OPERATOR\n- needs a call")
    v = env.events("verdict")[0]
    assert v["verdict"] == "ESCALATE" and v["findings"] == 1 and v["helper"] == "senior-reviewer"
    assert env.state()["escalated"] is True


def test_non_matching_hand_back_records_no_verdict(env):
    env.bash(": team-type coding")
    env.agent("reviewer", "r1")
    env.run("SubagentStart", agent_id="A1", agent_type="reviewer")
    env.run("SubagentStop", agent_id="A1", agent_type="reviewer", last_assistant_message="Looks fine to me")
    assert not env.events("verdict") and env.events("helper_end")


def test_non_reviewer_verdict_text_is_ignored(env):
    env.bash(": team-type coding")
    env.agent("implementer", "b1")
    env.run("SubagentStart", agent_id="B1", agent_type="implementer")
    env.run("SubagentStop", agent_id="B1", agent_type="implementer", last_assistant_message="READY")
    assert not env.events("verdict")


def test_foreground_post_tool_use_verdict_and_dedupe(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("reviewer", "r1")
    env.run("SubagentStart", agent_id="A1", agent_type="reviewer")
    env.run("SubagentStop", agent_id="A1", agent_type="reviewer", last_assistant_message="READY")
    env.run("PostToolUse", tool_name="Agent", tool_use_id="r1",
            tool_input={"subagent_type": "reviewer"},
            tool_response={"agentId": "A1", "content": [{"type": "text", "text": "READY"}]})
    assert len(env.events("verdict")) == 1
    # foreground only (no SubagentStart/Stop capture): PostToolUse captures it
    env.agent("reviewer", "r2")
    env.run("PostToolUse", tool_name="Agent", tool_use_id="r2",
            tool_input={"subagent_type": "reviewer"},
            tool_response={"content": [{"type": "text", "text": "NEEDS_CHANGES\n- a"}]})
    v = env.events("verdict")
    assert len(v) == 2 and v[1]["verdict"] == "NEEDS_CHANGES" and v[1]["version"] == 1


def test_pairing_uses_oldest_unclaimed_same_type_entry(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("reviewer", "r1")                  # version 1
    env.agent("implementer", "i1")               # other type: must not be claimed by a reviewer
    env.edit("b.py")
    env.agent("reviewer", "r2")                  # version 2
    env.run("SubagentStart", agent_id="A", agent_type="reviewer")
    env.run("SubagentStart", agent_id="B", agent_type="reviewer")
    pairs = {e["agent_id"]: e["tuid"] for e in env.events("helper_paired")}
    assert pairs == {"A": "r1", "B": "r2"}
    env.run("PreToolUse", tool_name="SubagentHandback", agent_id="B", agent_type="reviewer",
            tool_input={"message": "READY"})
    env.run("PreToolUse", tool_name="SubagentHandback", agent_id="A", agent_type="reviewer",
            tool_input={"message": "NEEDS_CHANGES\n- x"})
    by = {e["agent_id"]: e["version"] for e in env.events("verdict")}
    assert by == {"A": 1, "B": 2}


# ------------------------------------------------------------------ tamper, protected paths, check

def test_tamper_logged_once_when_config_mode_differs_from_last_mode_event(env):
    sd = env.ledger / "state"
    sd.mkdir(parents=True)
    (sd / "last_mode.json").write_text(json.dumps({"mode": "record", "tamper_for": None}))
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.bash(": team-type writing")
    t = env.events("tamper")
    assert len(t) == 1 and t[0]["found"] == "enforce" and t[0]["expected"] == "record"


def test_protected_paths_denied_in_record_mode(env):
    env.ledger.mkdir()
    out = env.run("PreToolUse", tool_name="Write", tool_input={"file_path": str(env.ledger / "events-x.jsonl")})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    out = env.run("PreToolUse", tool_name="Edit", tool_input={"file_path": str(env.hooks / "atlas_config.json")})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_anchor_clone_is_protected_from_edit_and_write(env):
    for tool, p in (("Write", "D:\\AI\\atlas-anchor\\anchors.jsonl"), ("Edit", "D:\\AI\\atlas-anchor\\.git\\config"),
                    ("Edit", "E:\\keep\\my-anchor\\anchors.jsonl")):
        out = env.run("PreToolUse", tool_name=tool, tool_input={"file_path": p},
                      extra_env={"ATLAS_ANCHOR_REPO": "E:\\keep\\my-anchor"})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", p


def test_areas_look_check_event_and_context(env):
    repo = env.root / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "tools" / "check_look.py").write_text(
        "import json,sys\nprint(json.dumps({'ok': False, 'problems': ['colour md too close']}))\nsys.exit(1)\n")
    f = repo / "areas.yaml"
    f.write_text("areas: []\n")
    env.bash(": team-type coding")
    out = env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": str(f)})
    assert "colour md too close" in out["hookSpecificOutput"]["additionalContext"]
    c = env.events("check")[0]
    assert c["ok"] is False and c["problems"] == ["colour md too close"] and c["version"] == 1
    # a passing check on a change touching only those files counts as verified
    (repo / "tools" / "check_look.py").write_text("import json\nprint(json.dumps({'ok': True, 'problems': []}))\n")
    assert env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": str(f)}) is None
    assert env.state()["last_verified"] == 2


def test_json_copies_refresh_when_workflows_edited(env):
    wf = env.root / "workflows.yaml"
    wf.write_text("default_budget: 7\ntypes: {}\n")
    env.cfg["workflows_file"] = str(wf)
    env.set_mode("record")
    env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": str(wf)})
    assert json.loads((env.hooks / "workflows.json").read_text())["default_budget"] == 7


# ------------------------------------------------------------------ concurrency

def test_parallel_hook_processes_lose_no_version_bump(env):
    env.bash(": team-type coding")

    def one(i):
        return env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": str(env.work / f"f{i}.py")})

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, range(8)))
    edits = env.events("edit")
    assert sorted(e["version"] for e in edits) == list(range(1, 9))
    assert env.state()["version"] == 8
    assert len(env.events("work_type")) == 1


# ------------------------------------------------------------------ cost capture

def _line(mid, rid, out, model="claude-opus-5-5", inp=10, cr=100, cw=5, side=False, extra=None):
    d = {"type": "assistant", "requestId": rid, "isSidechain": side, "timestamp": "2026-10-09T10:00:00Z",
         "message": {"id": mid, "model": model, "role": "assistant",
                     "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
                               "cache_creation_input_tokens": cw}}}
    if extra:
        d.update(extra)
    return json.dumps(d) + "\n"


def test_cost_capture_dedupes_repeated_usage_lines(env, tmp_path):
    proj = tmp_path / "proj"
    sub = proj / SID / "subagents"
    sub.mkdir(parents=True)
    main = proj / f"{SID}.jsonl"
    # message m1 repeated per content block with a growing output count; m2 once; a sidechain line skipped
    main.write_text(_line("m1", "q1", 5) + _line("m1", "q1", 8) + _line("m1", "q1", 40) + _line("m2", "q2", 7)
                    + _line("m3", "q3", 999, side=True) + "garbage line\n"
                    + json.dumps({"type": "custom-title", "customTitle": "Atlas v2 build"}) + "\n",
                    encoding="utf-8")
    (sub / "agent-AG1.jsonl").write_text(_line("h1", "r1", 100, model="claude-sonnet-5-5", side=True)
                                         + _line("h1", "r1", 120, model="claude-sonnet-5-5", side=True),
                                         encoding="utf-8")
    (sub / "agent-AG1.meta.json").write_text(json.dumps({"agentType": "implementer"}))
    env.bash(": team-type coding")
    env.run("Stop", transcript_path=str(main))
    costs = env.events("cost")
    by = {(c["scope"], c["model"]): c for c in costs}
    m = by[("main", "claude-opus-5-5")]
    assert (m["in"], m["out"], m["cache_read"], m["cache_write"]) == (20, 47, 200, 10)   # 40+7 out, two messages
    assert m["weighted"] == int((20 + 47) * 2.0)
    h = by[("AG1", "claude-sonnet-5-5")]
    assert h["out"] == 120 and h["in"] == 10 and h["helper"] == "implementer" and h["weighted"] == 130
    assert m.get("title") == "Atlas v2 build"
    # second pass with nothing new: no events
    n = len(costs)
    env.run("Stop", transcript_path=str(main))
    assert len(env.events("cost")) == n
    # a message that straddles a Stop: only the delta is emitted
    with open(main, "a", encoding="utf-8") as fh:
        fh.write(_line("m2", "q2", 17) + _line("m4", "q4", 3))
    env.run("SessionEnd", transcript_path=str(main), reason="other")
    new = env.events("cost")[n:]
    assert len(new) == 1 and new[0]["out"] == 10 + 3 and new[0]["in"] == 10 and "title" not in new[0]


def test_cost_pass_survives_truncated_transcript_and_missing_file(env, tmp_path):
    main = tmp_path / f"{SID}.jsonl"
    main.write_text(_line("m1", "q1", 5), encoding="utf-8")
    env.run("Stop", transcript_path=str(main))
    main.write_text(_line("m9", "q9", 2, cr=1), encoding="utf-8")       # shrank: offset resets to 0
    env.run("Stop", transcript_path=str(main))
    env.run("Stop", transcript_path=str(tmp_path / "missing.jsonl"))
    assert sum(c["out"] for c in env.events("cost")) == 7


# ------------------------------------------------------------------ hook stream feeds derive

def test_hook_stream_derives_to_same_numbers_as_state_cache(env):
    sys.path.insert(0, str(REPO / "cockpit"))
    from ledger2 import derive, query
    trip(env)
    env.bash(": team-type writing")        # carried budget
    env.edit("b.py")
    env.review("r9", "A9", "READY")
    env.bash(": team-done")
    tasks = derive.derive_v2(env.events(), query.load_workflows())
    t1, t2 = tasks
    assert t1["rounds"]["needs_changes"] == 3 and t1["status"] == "switched"
    assert t2["type"] == "writing" and t2["rounds"]["needs_changes"] == 3 and t2["rounds"]["budget"] == 2
    assert t2["status"] == "done" and t2["checked"] is True and t2["unverified_flag"] is False
    S = env.state()
    assert S["needs_changes"] == t2["rounds"]["needs_changes"] and S["version"] == t2["edits"]


def test_state_rebuilds_from_events_when_cache_is_deleted(env):
    trip(env)
    before = env.state()
    (env.ledger / "state" / f"{SID}.json").unlink()
    env.set_mode("enforce")
    out = env.agent("implementer", "b1")                  # decision needs the rebuilt state
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    after = env.state()
    for k in ("task", "version", "nc_versions", "budget", "type"):
        assert after[k] == before[k]


# ------------------------------------------------------------------ rework round 1 (senior review)

def test_stop_capture_keeps_launch_version_not_current(env):
    """Reviewer launched at v1, main edits on to v3, READY arrives: logged at v1, done-lock still refuses."""
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("reviewer", "r1", bg=True)
    env.run("SubagentStart", agent_id="R1", agent_type="reviewer")
    env.edit("b.py")
    env.edit("c.py")
    env.run("SubagentStop", agent_id="R1", agent_type="reviewer", last_assistant_message="READY\nall good")
    v = env.events("verdict")[0]
    assert v["version"] == 1 and v["verdict"] == "READY"
    out = env.bash(": team-done")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "version 3" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "last READY at version 1" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_unknown_launch_version_is_null_and_never_passes_done_lock(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.edit("a.py")
    # a reviewer that was never launched through the Agent tool of this session: no pairing at all
    env.run("SubagentStop", agent_id="X1", agent_type="reviewer", last_assistant_message="READY")
    v = env.events("verdict")[0]
    assert v["version"] is None
    assert env.bash(": team-done")["hookSpecificOutput"]["permissionDecision"] == "deny"
    # an unknown NEEDS_CHANGES with no version still burns a round
    env.run("SubagentStop", agent_id="X2", agent_type="reviewer", last_assistant_message="NEEDS_CHANGES\n- a")
    assert env.state()["needs_changes"] == 1


def test_handback_after_helper_end_still_uses_launch_version(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("reviewer", "r1")
    env.run("SubagentStart", agent_id="R1", agent_type="reviewer")
    env.run("SubagentStop", agent_id="R1", agent_type="reviewer", last_assistant_message="working on it")
    env.edit("b.py")
    env.run("SubagentStop", agent_id="R1", agent_type="reviewer", last_assistant_message="NEEDS_CHANGES\n- x")
    v = env.events("verdict")
    assert len(v) == 1 and v[0]["version"] == 1            # captured on the second stop, at the launch version
    assert len(env.events("helper_end")) == 1               # helper_end once


def test_subagent_stop_unknown_agent_without_type_is_skipped(env):
    env.bash(": team-type coding")
    env.run("SubagentStop", agent_id="Q", agent_type="", last_assistant_message="READY")
    assert not env.events("helper_end") and not env.events("verdict")


def test_foreground_post_tool_use_without_agent_id_does_not_duplicate(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.agent("reviewer", "r1")
    env.run("SubagentStart", agent_id="A1", agent_type="reviewer")
    env.run("SubagentStop", agent_id="A1", agent_type="reviewer", last_assistant_message="READY")
    env.run("PostToolUse", tool_name="Agent", tool_use_id="r1", tool_input={"subagent_type": "reviewer"},
            tool_response={"content": [{"type": "text", "text": "READY"}]})
    assert len(env.events("verdict")) == 1


def test_carried_rounds_do_not_collide_with_new_task_versions(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.review("r1", "A1", "NEEDS_CHANGES\n- a")      # NC at version 1
    env.edit("a.py")
    env.review("r2", "A2", "NEEDS_CHANGES\n- a")      # NC at version 2
    env.bash(": team-type chore")                       # carry: 2 rounds, budget min(2, 1) = 1
    S = env.state()
    assert S["needs_changes"] == 2 and S["budget"] == 1 and S["version"] == 0
    env.edit("b.py")
    env.review("r3", "A3", "NEEDS_CHANGES\n- a")      # NC at the NEW task's version 1: must add, not collide
    S = env.state()
    assert S["needs_changes"] == 3
    env.set_mode("enforce")
    assert env.agent("implementer", "b1")["hookSpecificOutput"]["permissionDecision"] == "deny"
    sys.path.insert(0, str(REPO / "cockpit"))
    from ledger2 import derive, query
    t2 = derive.derive_v2(env.events(), query.load_workflows())[1]
    assert t2["rounds"]["needs_changes"] == 3


def test_carry_refusal_is_not_counted_as_enforced(env):
    trip(env)
    env.set_mode("enforce")
    env.bash(": team-type writing")
    r = [e for e in env.events("refusal") if "carry-over" in e["detail"]]
    assert r and r[-1]["would"] is True


def test_stale_and_foreign_pending_launches_are_not_paired(env):
    env.bash(": team-type coding")
    env.agent("reviewer", "stale")
    st = env.state()
    st["pending"]["stale"]["t"] = "2020-01-01T00:00:00+00:00"
    (env.ledger / "state" / f"{SID}.json").write_text(json.dumps(st), encoding="utf-8")
    env.edit("a.py")
    env.agent("reviewer", "real")
    env.run("SubagentStart", agent_id="A", agent_type="reviewer")
    assert env.events("helper_paired")[0]["tuid"] == "real"
    assert "stale" not in env.state()["pending"]
    # another task's unclaimed launch is pruned too
    env.agent("reviewer", "old-task")
    env.bash(": team-type writing")
    assert "old-task" not in env.state()["pending"]
    env.run("SubagentStart", agent_id="B", agent_type="reviewer")
    assert env.events("helper_paired")[-1]["tuid"] is None


def test_exact_pairing_through_meta_tool_use_id(env, tmp_path):
    proj = tmp_path / "proj"
    sub = proj / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-A.meta.json").write_text(json.dumps({"toolUseId": "second"}))
    env.bash(": team-type coding")
    env.agent("reviewer", "first")
    env.agent("reviewer", "second")
    env.run("SubagentStart", agent_id="A", agent_type="reviewer", transcript_path=str(proj / f"{SID}.jsonl"))
    assert env.events("helper_paired")[0]["tuid"] == "second"


def test_findings_count_ignores_indented_and_unbulleted_lines(env):
    sys.path.insert(0, str(HERE))
    import atlas_hook
    assert atlas_hook.parse_verdict("NEEDS_CHANGES\n- one\n* two\n1. three\n  - nested\n-nospace\ntext") == ("NEEDS_CHANGES", 3)


def test_check_failure_context_is_capped(env):
    repo = env.root / "repo2"
    (repo / "tools").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "tools" / "check_look.py").write_text(
        "import json\nprint(json.dumps({'ok': False, 'problems': ['p' * 100] * 5}))\n")
    f = repo / "areas.yaml"
    f.write_text("areas: []\n")
    env.bash(": team-type coding")
    out = env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": str(f)})
    assert len(out["hookSpecificOutput"]["additionalContext"]) <= 1000


def test_no_mode_event_baselines_last_mode_with_current_mode(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    lm = json.loads((env.ledger / "state" / "last_mode.json").read_text())
    assert lm["mode"] == "enforce" and not env.events("tamper")


def test_read_new_checks_deadline_before_advancing_offset(tmp_path):
    sys.path.insert(0, str(HERE))
    import atlas_hook
    f = tmp_path / "t.jsonl"
    lines = [_line("m%d" % i, "q%d" % i, 1) for i in range(450)]
    f.write_bytes("".join(lines).encode())
    msgs, pos, _t, _size = atlas_hook.read_new(str(f), 0, 0.0, True)      # deadline already passed
    assert pos == sum(len(x.encode()) for x in lines[:199])
    assert len(msgs) == 199 and "m199|q199" not in msgs


def test_session_end_survives_cost_failure(env, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / SID).write_text("a file where the helper dir should be")   # makes the subagents lookup misbehave
    main = proj / f"{SID}.jsonl"
    main.write_text(_line("m1", "q1", 5), encoding="utf-8")
    env.run("SessionEnd", transcript_path=str(main), reason="other")
    assert env.events("session_end")


def test_work_type_plan_ref(env):
    env.bash(": team-type writing md task:evt_0a1b2c3d")
    wt = env.events("work_type")[-1]
    assert wt["type"] == "writing" and wt["area"] == "md" and wt["plan_ref"] == "evt_0a1b2c3d"
    env.bash(": team-type coding task:evt_0a1b2c3d@2026-10-12")      # task without area, series occurrence
    wt = env.events("work_type")[-1]
    assert wt["type"] == "coding" and wt["plan_ref"] == "evt_0a1b2c3d@2026-10-12" and wt["area"] != "md"
    env.bash(": team-type coding task:t_deadbeef")                    # same type, new ref: reopens
    assert env.events("work_type")[-1]["plan_ref"] == "t_deadbeef"
    env.bash(": team-type coding task:atlas/some-slug")
    assert env.events("work_type")[-1]["plan_ref"] == "atlas/some-slug"
    n = len(env.events("work_type"))
    env.bash(": team-type coding task:atlas/some-slug")               # identical: no-op
    assert len(env.events("work_type")) == n


def test_work_type_bad_plan_ref_is_ignored_type_still_declared(env):
    env.bash(": team-type research atlas task:write the onboarding email")
    assert not env.events("work_type")                               # free text: arguments rejected
    env.bash(": team-type research atlas task:bad!id")
    wt = env.events("work_type")[-1]
    assert wt["type"] == "research" and "plan_ref" not in wt
    assert any("plan ref" in (e.get("what") or "") for e in env.events("ignored"))

def test_forward_texts_walk_lists_and_just_opened_window(env):
    sys.path.insert(0, str(HERE))
    import atlas_hook
    ti = {"to": "x", "message": [{"type": "text", "text": "/ack-break abc"}]}
    assert "/ack-break abc" in list(atlas_hook._forward_texts(ti))
    r = env.run("PreToolUse", tool_name="SendMessage",
                tool_input={"to": "x", "content": [{"type": "text", "text": "/grant-rework 2"}]})
    assert r and r["hookSpecificOutput"]["permissionDecision"] == "deny"
    now = atlas_hook.dt.datetime.now().astimezone()
    assert atlas_hook._just_opened({"task_t": now.isoformat()})
    assert not atlas_hook._just_opened({"task_t": (now - atlas_hook.dt.timedelta(minutes=5)).isoformat()})



def test_same_type_redeclaration_after_the_window_still_carries_a_spent_budget(env):
    trip(env)
    env.set_mode("enforce")
    sp = env.ledger / "state" / f"{SID}.json"
    S = json.loads(sp.read_text(encoding="utf-8"))
    S["task_t"] = "2020-01-01T00:00:00+00:00"                 # age the open task past the 60 s duplicate window
    sp.write_text(json.dumps(S), encoding="utf-8")
    env.bash(": team-type coding")
    wt = env.events("work_type")[-1]
    assert wt["task"] == "s1abcdef-2" and wt["carried"] is True
    assert env.events("refusal")[-1]["guard"] == "rework" and env.events("refusal")[-1]["would"] is True
    out = env.agent("implementer", "b1")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_same_type_redeclaration_under_budget_starts_fresh(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    env.review("r1", "A1", "NEEDS_CHANGES\n- one")
    sp = env.ledger / "state" / f"{SID}.json"
    S = json.loads(sp.read_text(encoding="utf-8"))
    S["task_t"] = "2020-01-01T00:00:00+00:00"
    sp.write_text(json.dumps(S), encoding="utf-8")
    env.bash(": team-type coding")
    assert not env.events("work_type")[-1].get("carried")
    assert env.state()["needs_changes"] == 0


@pytest.mark.parametrize("ti", [
    {"message": ["/grant-rework 2"]},
    {"prompt": [[{"text": "/grant-rework"}]]},
    {"a": {"b": {"c": [{"text": "/ack-break x"}]}}},
])
def test_forward_texts_find_nested_user_only_commands(ti):
    sys.path.insert(0, str(HERE))
    import atlas_hook
    assert any(atlas_hook.USER_ONLY_RE.match(x) for x in atlas_hook._forward_texts(ti))


def test_forward_texts_fail_closed_on_oversized_or_too_deep_input():
    sys.path.insert(0, str(HERE))
    import atlas_hook
    refused = lambda ti: any(atlas_hook.USER_ONLY_RE.match(x) for x in atlas_hook._forward_texts(ti))
    assert refused({"message": [{"text": "x"}] * 3000 + [{"text": "/grant-rework"}]})
    deep = {"text": "x"}
    for _ in range(8):
        deep = {"a": deep}
    assert refused(deep)
    assert not refused({"message": "plain " * 3500})                    # a realistic 20 KB plain message passes
    assert not refused({"content": [{"type": "text", "text": "hello " * 500}] * 20})


def test_task_that_ended_ready_does_not_carry_its_count(env):
    trip(env)
    env.edit("a.py")
    env.review("r9", "A9", "READY\n- fine")
    assert env.state()["last_verified"] >= env.state()["version"]
    sp = env.ledger / "state" / f"{SID}.json"
    S = json.loads(sp.read_text(encoding="utf-8"))
    S["task_t"] = "2020-01-01T00:00:00+00:00"
    sp.write_text(json.dumps(S), encoding="utf-8")
    env.bash(": team-type coding")
    assert not env.events("work_type")[-1].get("carried")
    assert env.state()["needs_changes"] == 0
