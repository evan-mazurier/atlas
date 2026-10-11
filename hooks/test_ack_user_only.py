"""/ack-break and /grant-rework are user-only: honoured only from a typed UserPromptSubmit on an interactive entry,
never from a helper, a model skill call, a scheduled prompt or a forwarded message. Temp ledgers only."""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "cockpit"))

from ledger2 import ledger_chain  # noqa: E402
from test_ack_and_reminder import acks, delete_line, seed  # noqa: E402
from test_atlas_hook import Env  # noqa: E402


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def one_break(env):
    f = seed(env)
    delete_line(f, 9)
    return ledger_chain.scan(env.ledger)["breaks"][0]["id"]


def test_ack_with_an_id_acknowledges_it(env):
    bid = one_break(env)
    out = env.run("UserPromptSubmit", prompt="/ack-break " + bid)
    assert out is None
    a = acks(env)
    assert len(a) == 1 and a[0]["by"] == "operator" and a[0]["break"] == bid
    res = ledger_chain.build(env.ledger)
    assert res["status"] == "intact, 1 acknowledged" and res["breaks"][0]["acknowledged"] is True


def test_ack_without_an_id_does_nothing_and_says_what_is_needed(env):
    one_break(env)
    out = env.run("UserPromptSubmit", prompt="/ack-break")
    assert not acks(env)
    assert "break id shown on the Ledger screen" in out["systemMessage"]
    assert "break id shown on the Ledger screen" in out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert any(e["what"] == "ack-break: id needed" for e in env.events("ignored"))


def test_unknown_id_and_whole_prompt_rule(env):
    bid = one_break(env)
    env.run("UserPromptSubmit", prompt="/ack-break 2026-10-09:99:deadbeef:00000000")
    env.run("UserPromptSubmit", prompt="/ack-break " + bid + " and then some words")     # not a whole-prompt command
    env.run("UserPromptSubmit", prompt="please /ack-break " + bid)
    assert not acks(env)
    env.run("UserPromptSubmit", prompt="  /ack-break " + bid + "  ")
    assert len(acks(env)) == 1


def test_ack_from_a_model_skill_call_a_helper_a_shell_mention_or_the_expansion_does_nothing(env):
    bid = one_break(env)
    for mode in ("record", "enforce"):
        env.set_mode(mode)
        out = env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "ack-break"})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    env.run("UserPromptSubmit", prompt="/ack-break " + bid, agent_id="A1", agent_type="implementer")
    env.bash("echo /ack-break " + bid)
    env.run("UserPromptExpansion", command_name="ack-break", expansion_type="slash_command")
    assert not acks(env)


@pytest.mark.parametrize("entry", ["sdk-cli", "weird", "", "claude-code-scheduled"])
def test_ack_and_grant_refused_from_a_non_interactive_entry_even_with_an_empty_list(env, entry):
    bid = one_break(env)
    env.bash(": team-type coding")
    assert env.cfg["interactive_entries"] == []
    env.run("UserPromptSubmit", prompt="/ack-break " + bid, extra_env={"CLAUDE_CODE_ENTRYPOINT": entry})
    env.run("UserPromptSubmit", prompt="/grant-rework", extra_env={"CLAUDE_CODE_ENTRYPOINT": entry})
    assert not acks(env) and not env.events("rework_granted")
    assert any("non-interactive entry" in e["what"] for e in env.events("ignored"))


@pytest.mark.parametrize("entry", ["cli", "claude-desktop", "claude-vscode"])
def test_fixed_fallback_entries_are_honoured(env, entry):
    bid = one_break(env)
    env.run("UserPromptSubmit", prompt="/ack-break " + bid, extra_env={"CLAUDE_CODE_ENTRYPOINT": entry})
    assert len(acks(env)) == 1


def test_a_configured_list_replaces_the_fallback(env):
    bid = one_break(env)
    env.cfg["interactive_entries"] = ["claude-desktop"]
    env.set_mode("record")
    env.run("UserPromptSubmit", prompt="/ack-break " + bid, extra_env={"CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert not acks(env)
    env.run("UserPromptSubmit", prompt="/ack-break " + bid, extra_env={"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
    assert len(acks(env)) == 1


@pytest.mark.parametrize("tool,key", [("CronCreate", "prompt"), ("ScheduleWakeup", "prompt"), ("SendMessage", "message"),
                                      ("SendMessage", "content")])
@pytest.mark.parametrize("mode", ["record", "enforce"])
@pytest.mark.parametrize("text", ["/ack-break 2026-10-09:1:ab:cd", "  /grant-rework 3", "/ACK-BREAK", "/grant-rework"])
def test_scheduled_or_forwarded_prompt_starting_with_a_user_only_command_is_denied(env, tool, key, mode, text):
    env.set_mode(mode)
    out = env.run("PreToolUse", tool_name=tool, tool_input={key: text, "cron": "* * * * *"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert any(e["what"].startswith("forged user-only command via " + tool) for e in env.events("ignored"))


@pytest.mark.parametrize("tool,ti", [
    ("CronCreate", {"prompt": "check the build, then /ack-break later"}),
    ("CronCreate", {"prompt": "remind me about /grant-rework docs"}),
    ("SendMessage", {"message": "see /ack-break-notes.md"}),
    ("SendMessage", {"to": "x", "message": "hello"}),
    ("ScheduleWakeup", {"prompt": "/loop check"}),
])
def test_ordinary_scheduled_or_forwarded_prompts_pass(env, tool, ti):
    assert env.run("PreToolUse", tool_name=tool, tool_input=ti) is None
    assert not env.events("ignored")


def test_nested_message_object_is_scanned(env):
    out = env.run("PreToolUse", tool_name="SendMessage", tool_input={"to": "x", "message": {"summary": "/ack-break"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_new_break_after_the_ack_turns_it_red_again(env):
    f = seed(env)
    delete_line(f, 9)
    bid = ledger_chain.scan(env.ledger)["breaks"][0]["id"]
    env.run("UserPromptSubmit", prompt="/ack-break " + bid)
    assert ledger_chain.build(env.ledger)["status"] == "intact, 1 acknowledged"
    delete_line(f, 16)
    res = ledger_chain.build(env.ledger)
    assert res["status"] == "broken" and res["intact"] is False


def test_the_scan_runs_before_the_lock_is_taken(env):
    """The chain scan reads the whole ledger: it must not run while the append lock is held."""
    src = (HERE / "atlas_hook.py").read_text(encoding="utf-8")
    body = src[src.index("def h_prompt(H):"):src.index("DEFAULT_INTERACTIVE")]
    assert body.index("ack_decision(") < body.index("with Txn(H)")
    assert "unacknowledged(" not in body
