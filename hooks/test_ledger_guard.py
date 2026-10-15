"""Contract amendment §6.4: rule A (hash chain written by the hook) and rule B (bypass_attempt guard).
Every run is a real subprocess against temp dirs, like test_atlas_hook.py."""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "cockpit"))

from test_atlas_hook import Env, SID  # noqa: E402
from ledger2 import store  # noqa: E402

LEDGER = "D:\\AI\\atlas-data\\ledger"
ATLAS = "D:\\AI\\Atlas"


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def sh(env, cmd, tool="Bash", **kw):
    return env.run("PreToolUse", tool_name=tool, tool_input={"command": cmd}, **kw)


def denied(out):
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ------------------------------------------------------------------ rule A: the hook chains its lines

def test_hook_lines_form_an_intact_chain_read_by_the_store(env):
    env.run("SessionStart", source="startup")
    env.run("UserPromptSubmit", prompt="hi")
    sh(env, ": team-type coding")
    evs = env.events()
    assert len(evs) >= 3 and all("prev" in e for e in evs)
    assert evs[0]["prev"] == "genesis"
    res = store.verify_chain(env.ledger)
    assert res["intact"] and res["breaks"] == [] and res["lines"] == len(evs)


def test_hook_chain_detects_a_deleted_line(env):
    env.run("SessionStart", source="startup")
    sh(env, ": team-type coding")
    env.edit("a.py")
    env.edit("b.py")
    f = next(env.ledger.glob("events-*.jsonl"))
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 4
    del lines[1]
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = store.verify_chain(env.ledger)
    assert not res["intact"] and res["breaks"][0] == {"file": f.name, "line": 2}


def test_hook_chain_continues_from_the_previous_day_file(env):
    env.ledger.mkdir(parents=True, exist_ok=True)
    old = env.ledger / "events-2020-01-01.jsonl"
    old.write_text(json.dumps({"v": 2, "t": "2020-01-01T10:00:00+00:00", "ev": "x", "prev": "genesis"}) + "\n", encoding="utf-8")
    env.run("SessionStart", source="startup")
    first = json.loads(next(p for p in sorted(env.ledger.glob("events-*.jsonl")) if p.name != old.name)
                       .read_text(encoding="utf-8").splitlines()[0])
    assert first["prev"] == store.line_hash(store.last_line(old))
    assert store.verify_chain(env.ledger)["intact"]


# ------------------------------------------------------------------ rule B: shell commands naming the ledger or the hooks

@pytest.mark.parametrize("mode", ["off", "record", "enforce"])
@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_bypass_denied_in_every_mode_and_logged(env, mode, tool):
    env.set_mode(mode)
    out = sh(env, "type D:\\AI\\atlas-data\\ledger\\events-2026-10-09.jsonl SECRETWORD", tool=tool)
    assert denied(out)
    ev = env.events("bypass_attempt")
    assert len(ev) == 1
    assert ev[0]["tool"] == tool and ev[0]["denied"] is True and len(ev[0]["matched"]) <= 60
    # the command text is kept ONLY on bypass_attempt (clipped, secrets masked); `matched` stays the match alone
    assert "SECRETWORD" not in ev[0]["matched"]
    assert "SECRETWORD" not in json.dumps([e for e in env.events() if e["ev"] != "bypass_attempt"])


@pytest.mark.parametrize("cmd", [
    "cat D:/AI/atlas-data/ledger/events-2026-10-09.jsonl",
    "cat /d/AI/atlas-data/ledger/state/x.json",
    "Remove-Item d:\\ai\\ATLAS-DATA\\Ledger -Recurse",
    "rm -rf D:\\\\AI\\\\atlas-data\\\\ledger",
    "echo x > /mnt/d/AI/atlas-data/ledger/events-2026-10-09.jsonl",
    "sed -i s/a/b/ ~/.claude/hooks/atlas/atlas_hook.py",
    "notepad ~\\.claude\\hooks\\atlas\\atlas_config.json",
    "python -c \"print(open('~/.claude/hooks/atlas/atlas_hook.py').read())\"",
])
def test_bypass_variants_denied(env, cmd):
    assert denied(sh(env, cmd))
    assert len(env.events("bypass_attempt")) == 1


def test_bypass_matches_the_configured_ledger_dir(env):
    assert denied(sh(env, "cat '%s/events-2026-10-09.jsonl'" % str(env.ledger).replace("\\", "/")))


def test_subagent_commands_are_guarded_too(env):
    assert denied(sh(env, "cat D:/AI/atlas-data/ledger/x", agent_id="H1", agent_type="implementer"))


def test_bypass_matched_is_clipped_to_60_chars(env):
    cmd = "cat " + "D:/AI/atlas-data/ledger/" + "a" * 200
    assert denied(sh(env, cmd))
    assert len(env.events("bypass_attempt")[0]["matched"]) <= 60


@pytest.mark.parametrize("cmd", [
    "py -3.12 tools/hooks_mode.py enforce",
    "py -3.12 D:\\AI\\Atlas\\tools\\install_hooks.py --ledger D:\\AI\\atlas-data\\ledger",
    "py -3.12 tools/install_hooks.py --home ~ --hooks ~\\.claude\\hooks\\atlas",
    "py -3.12 tools/launch_session.py --record-session abc --ledger D:/AI/atlas-data/ledger",
    "py -3.12 tools/import_v1_ledger.py --ledger D:\\AI\\atlas-data\\ledger",
    "py -3.12 tools/weekly_report.py --ledger D:/AI/atlas-data/ledger --week 2026-W41",
    "cd D:\\AI\\Atlas && py -3.12 tools/weekly_report.py D:\\AI\\atlas-data\\ledger",
    "py -3.12 D:\\AI\\Atlas\\tools\\anchor_ledger.py --dry-run",
    "py -3.12 tools/anchor_ledger.py --ledger D:/AI/atlas-data/ledger --dry-run",
])
def test_documented_tools_are_not_denied(env, cmd):
    env.run("UserPromptSubmit", prompt="/allow-config")      # install_hooks.py also needs the config gate's OK (6.4c)
    assert not denied(sh(env, cmd, cwd=ATLAS))                # a relative tools/x.py needs the Atlas checkout as cwd
    assert env.events("bypass_attempt") == []


@pytest.mark.parametrize("cwd", [ATLAS, ATLAS + "\\.claude\\worktrees\\agent-ab12", "/mnt/d/AI/Atlas"])
def test_relative_documented_tool_ok_only_from_an_atlas_checkout(env, cwd):
    assert not denied(sh(env, "py -3.12 tools/weekly_report.py --ledger D:/AI/atlas-data/ledger", cwd=cwd))


@pytest.mark.parametrize("cwd", [None, "C:\\tmp\\evil", ATLAS + "\\cockpit", "D:\\AI\\Atlas2", ATLAS + "\\.claude\\worktrees"])
def test_relative_documented_tool_denied_elsewhere(env, cwd):
    kw = {"cwd": cwd} if cwd else {}
    assert denied(sh(env, "py -3.12 tools/weekly_report.py --ledger D:/AI/atlas-data/ledger", **kw))


@pytest.mark.parametrize("cmd", [
    "py -3.12 D:\\AI\\Atlas\\tools\\weekly_report.py --ledger D:/AI/atlas-data/ledger",
    "py -3.12 D:\\AI\\Atlas\\.claude\\worktrees\\agent-ab12\\tools\\weekly_report.py --ledger D:/AI/atlas-data/ledger",
    "py -3.12 /mnt/d/AI/Atlas/tools/weekly_report.py --ledger D:/AI/atlas-data/ledger",
])
def test_absolute_documented_tool_ok_from_any_cwd(env, cmd):
    assert not denied(sh(env, cmd, cwd="C:\\tmp\\evil"))


@pytest.mark.parametrize("cmd", [
    "py -3.12 D:\\AI\\Atlas-evil\\tools\\weekly_report.py --ledger D:/AI/atlas-data/ledger",
    "py -3.12 D:\\AI\\Atlas\\.claude\\evil\\tools\\weekly_report.py --ledger D:/AI/atlas-data/ledger",
    "py -3.12 D:\\Other\\AI\\Atlas\\tools\\weekly_report.py --ledger D:/AI/atlas-data/ledger",
    "py -3.12 D:\\AI\\Atlas\\x\\tools\\weekly_report.py --ledger D:/AI/atlas-data/ledger",
])
def test_lookalike_tool_folders_are_denied(env, cmd):
    assert denied(sh(env, cmd, cwd=ATLAS))


# the anchor clone is protected: only tools/anchor_ledger.py may name it
@pytest.mark.parametrize("cmd", [
    "cat D:\\AI\\atlas-anchor\\anchors.jsonl",
    "rm D:/AI/atlas-anchor/anchors.jsonl",
    "git -C D:\\AI\\atlas-anchor reset --hard",
    "py -3.12 tools/weekly_report.py --ledger D:/AI/atlas-anchor",           # another documented tool is not enough
    "py -3.12 tools/anchor_ledger.py; rm D:/AI/atlas-anchor/anchors.jsonl",
    "echo x > D:\\AI\\atlas-anchor\\anchors.jsonl",
])
def test_anchor_clone_is_protected(env, cmd):
    assert denied(sh(env, cmd, cwd=ATLAS))


def test_anchor_clone_named_by_the_anchor_tool_is_allowed(env):
    assert not denied(sh(env, "py -3.12 D:\\AI\\Atlas\\tools\\anchor_ledger.py --repo D:\\AI\\atlas-anchor --dry-run", cwd="C:\\tmp"))
    assert not denied(sh(env, "py -3.12 tools/anchor_ledger.py --repo D:\\AI\\atlas-anchor --dry-run", cwd=ATLAS))
    assert denied(sh(env, "ls", cwd="D:\\AI\\atlas-anchor"))


def test_configured_anchor_path_is_protected_too(env):
    out = sh(env, "cat E:\\keep\\my-anchor\\anchors.jsonl", cwd=ATLAS, extra_env={"ATLAS_ANCHOR_REPO": "E:\\keep\\my-anchor"})
    assert denied(out)


MUST_DENY = [
    "type D:\\AI\\atlas-data\\ledger.\\events-2026-10-09.jsonl",
    "Remove-Item ~\\.claude\\hooks\\atlas.\\atlas_hook.py",
    "python C:\\tmp\\tools\\weekly_report.py D:\\AI\\atlas-data\\ledger --wipe",
    "rm -rf D:/AI/atlas-data/ledger tools/hooks_mode.py",
    "Remove-Item -Recurse -Force ~\\.claude\\hooks\\atlas tools\\install_hooks.py",
    "rm D:/AI/atlas-data/ledger/x pytest",
    "python -c \"import shutil; shutil.rmtree(r'D:\\AI\\atlas-data\\ledger')\" pytest",
    "ATLAS_LEDGER2_DIR=D:/AI/atlas-data/ledger py -3.12 -m pytest hooks -q",
    "pytest --basetemp=D:\\AI\\atlas-data\\ledger",
    "py -3.12 -m pytest -q hooks --basetemp D:\\AI\\atlas-data\\ledger\\tmp",
    # quoting and dot segments cannot hide the path
    "cat 'D:/AI/atlas-data/ledger'/events-2026-10-09.jsonl",
    "cat D:/AI/atlas-data/\"ledger\"/events-2026-10-09.jsonl",
    "cat D:/AI/atlas-data/./ledger/events-2026-10-09.jsonl",
    "cat D:/AI/atlas-data/ledger/./././events-2026-10-09.jsonl",
    "type '~\\.claude\\.\\hooks\\atlas\\atlas_hook.py'",
]


@pytest.mark.parametrize("cmd", MUST_DENY)
def test_allowlist_is_anchored_and_quotes_dots_do_not_hide_the_path(env, cmd):
    assert denied(sh(env, cmd))
    assert len(env.events("bypass_attempt")) == 1


def test_deny_message_points_to_the_right_tools(env):
    out = sh(env, "cat D:/AI/atlas-data/ledger/x")
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Protected Atlas ledger/hooks folder. To look, use the Read or Grep tools; "
        "to change it, use the documented Atlas tools (tools/*.py).")


def test_deny_is_returned_even_when_the_ledger_cannot_be_written(env, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")                     # makedirs under a file fails
    d = {"hook_event_name": "PreToolUse", "session_id": SID, "cwd": str(env.work), "tool_use_id": "t1",
         "tool_name": "Bash", "tool_input": {"command": "rm -rf D:/AI/atlas-data/ledger"}}
    r = env.run_raw(json.dumps(d), {"ATLAS_LEDGER2_DIR": str(blocker / "ledger")})
    assert r.returncode == 0
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("cmd", [
    "cat D:/AI/atlas-data/ledger-notes.txt",
    "cat D:/AI/atlas-data/ledger.bak",
    "ls ~\\.claude\\hooks\\atlas_old",
    "ls ~\\.claude\\hooks\\atlas-backup",
])
def test_end_boundary_lets_lookalike_names_through(env, cmd):
    assert not denied(sh(env, cmd))


@pytest.mark.parametrize("cmd", [
    "cat D:/AI/atlas-data/ledger",
    "cat D:/AI/atlas-data/ledger\\x",
    "ls ~/.claude/hooks/atlas/",
])
def test_end_boundary_still_denies_the_folder_itself(env, cmd):
    assert denied(sh(env, cmd))


def test_environment_assignment_before_a_documented_tool_is_allowed(env):
    assert not denied(sh(env, "ATLAS_LEDGER2_DIR=D:/AI/atlas-data/ledger py -3.12 tools/weekly_report.py", cwd=ATLAS))


@pytest.mark.parametrize("sub", ["", "events-x", "state"])
def test_any_command_from_a_session_inside_the_ledger_is_denied(env, sub):
    cwd = "D:\\AI\\atlas-data\\ledger" + ("\\" + sub if sub else "")
    assert denied(sh(env, "ls", cwd=cwd))
    assert denied(sh(env, "py -3.12 tools/weekly_report.py", cwd=cwd))


def test_any_command_from_a_session_inside_the_hooks_dir_is_denied(env):
    assert denied(sh(env, "ls", cwd="~\\.claude\\hooks\\atlas"))
    assert not denied(sh(env, "ls", cwd="~\\.claude\\hooks"))


@pytest.mark.parametrize("cmd", [
    "py -3.12 tools/weekly_report.py; cat D:/AI/atlas-data/ledger/events-2026-10-09.jsonl",
    "py -3.12 tools/hooks_mode.py status && rm D:\\AI\\atlas-data\\ledger\\events-2026-10-09.jsonl",
    "py -3.12 tools/launch_session.py --help D:/AI/atlas-data/ledger",           # not --record-session
    "py -3.12 tools/weekly_report.py > D:\\AI\\atlas-data\\ledger\\events-2026-10-09.jsonl",
    "py -3.12 tools/weekly_report.py $(cat D:/AI/atlas-data/ledger/x)",
    "py -3.12 tools/weekly_report.py\ncat D:/AI/atlas-data/ledger/x",
])
def test_allowlist_cannot_smuggle_a_second_command(env, cmd):
    assert denied(sh(env, cmd, cwd=ATLAS))


@pytest.mark.parametrize("cmd", [
    "git status",
    "ls D:\\AI\\atlas-data",
    "cat D:\\AI\\atlas-data\\finance\\notes.txt",
    "ls ~\\.claude\\hooks",
    "ls ~\\.claude\\skills",
    'git commit -m "ledger hash chain"',
    "py -3.12 -m pytest -q D:\\AI\\Atlas\\hooks",
])
def test_ordinary_commands_are_not_denied(env, cmd):
    assert not denied(sh(env, cmd))
    assert env.events("bypass_attempt") == []
