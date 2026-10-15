"""Hardening round 3 (contracts §6.4c, §6.5): an open config grant does not switch off the publish gate or the forged-grant
check; `..` is collapsed in both guards; the grant is confirmed in the ledger, not only in the state cache; the
spawned-claude rule; globs and escapes; 8.3 short names. Subprocess runs against temp ledgers, plus in-process checks."""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_atlas_hook import SID, env  # noqa: E402,F401  (env is a fixture)
import atlas_hook  # noqa: E402

HOME = Path.home()
MEMORY = str(HOME / ".claude" / "projects" / "D--AI-Atlas" / "memory")


def denied(out):
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def grant(env, text="/allow-config"):
    return env.run("UserPromptSubmit", prompt=text)


class Stub:
    cwd = r"D:\work"
    ledger = ""


def shell(cmd, cwd=r"D:\work"):
    s = Stub()
    s.cwd = cwd
    return atlas_hook.config_shell(s, cmd)


def forge_cache(env, **keys):
    p = env.ledger / "state" / f"{SID}.json"
    S = json.loads(p.read_text(encoding="utf-8"))
    S.update(keys)
    p.write_text(json.dumps(S), encoding="utf-8")


FUTURE = (datetime.now().astimezone() + timedelta(days=1)).isoformat()


# ------------------------------------------------------------------ 1. a config grant opens only the config gate

def test_a_config_grant_does_not_open_the_publish_gate(env):
    env.run("SessionStart")
    grant(env)
    out = env.bash("echo x >> ~/.claude/CLAUDE.md; git push origin main")
    assert denied(out) and "allow-publish" in reason(out)
    assert not env.events("publish_allowed")


def test_a_config_grant_does_not_skip_the_forged_grant_check(env):
    env.run("SessionStart")
    grant(env)
    out = env.bash("echo x >> ~/.claude/CLAUDE.md; claude -p '/grant-rework 5'")
    assert denied(out) and "/grant-rework" in reason(out)
    assert any(e["what"] == "forged grant command" for e in env.events("ignored"))


def test_a_command_needing_both_grants_needs_both(env):
    env.run("SessionStart")
    grant(env)
    assert denied(env.bash("echo x >> ~/.claude/CLAUDE.md; git push origin main"))
    grant(env, "/allow-publish")
    assert env.bash("echo x >> ~/.claude/CLAUDE.md; git push origin main") is None
    assert len(env.events("publish_allowed")) == 1


def test_a_config_allowance_is_logged_once_per_command(env):
    env.run("SessionStart")
    grant(env)
    assert env.bash("echo x >> ~/.claude/CLAUDE.md") is None
    assert len(env.events("config_allowed")) == 1


def test_a_publish_grant_alone_does_not_open_the_config_gate(env):
    env.run("SessionStart")
    grant(env, "/allow-publish")
    assert denied(env.bash("echo x >> ~/.claude/CLAUDE.md; git push origin main"))
    assert not env.events("publish_allowed")


# ------------------------------------------------------------------ 2a. `..` is collapsed

DOTS_CONFIG = [
    "echo x > ~/.claude/projects/../agents/evil.md",
    "cp /tmp/x ~/.claude/projects/../settings.json",
    "cp /tmp/x ~/.claude/projects/../hooks/require_named_helper.py",
    "cp -r /tmp/evil ~/.claude/projects/../skills/",
    "echo x > ~/.claude/x/../agents/evil.md",
    'echo x > "~/.claude/projects/../agents/evil.md"',
    "echo x > '~/.claude/projects'/../agents/evil.md",
    r"echo x > ~\.claude\projects\..\agents\evil.md",
    "echo x > $HOME/.claude/projects/../agents/evil.md",
    "echo x > ~/.claude/a/b/../../CLAUDE.md",
]


@pytest.mark.parametrize("cmd", DOTS_CONFIG)
def test_dot_dot_does_not_hide_a_config_path(cmd):
    assert shell(cmd), cmd


@pytest.mark.parametrize("cmd", DOTS_CONFIG[:4])
def test_dot_dot_config_is_denied_end_to_end(env, cmd):
    env.run("SessionStart")
    assert denied(env.bash(cmd))
    assert env.events("refusal")[-1]["guard"] == "config"


def test_relative_dot_dot_from_a_memory_folder_reaches_the_config_folder():
    assert shell("echo x > ../../../agents/evil.md", cwd=MEMORY)
    assert shell("cp /tmp/x ../../../settings.json", cwd=MEMORY)
    assert shell("echo x > ../notes.txt", cwd=MEMORY) is None
    assert shell("ls ../../../agents", cwd=MEMORY) is None
    assert shell("echo x > ../../../agents/evil.md", cwd=r"D:\work") is None


def test_the_relative_dot_dot_case_is_denied_end_to_end(env):
    env.run("SessionStart")
    assert denied(env.bash("echo x > ../../../agents/evil.md", cwd=MEMORY))


def test_ordinary_dot_dot_commands_stay_free():
    for cmd in ("cd .. && git status", "cp a ../b.txt", "echo x > ../out.txt", "ls ../../src", "python ../tools/x.py"):
        assert shell(cmd) is None, cmd


LEDGER_DOTS = [
    ("echo x > ~/.claude/projects/../hooks/atlas/atlas_hook.py", None),
    ("echo '{}' > D:/AI/Atlas/../atlas-data/ledger/state/abc.json", None),
    (r"echo x > D:\AI\Atlas\..\atlas-data\ledger\state\abc.json", None),
    ("echo x > ../atlas-data/ledger/state/abc.json", r"D:\AI\Atlas"),
    ("echo x > ../../atlas-data/ledger/state/abc.json", r"D:\AI\Atlas\sub"),
]


@pytest.mark.parametrize("cmd,cwd", LEDGER_DOTS)
def test_dot_dot_does_not_hide_the_ledger_or_the_hooks_folder(env, cmd, cwd):
    env.run("SessionStart")
    out = env.bash(cmd, **({"cwd": cwd} if cwd else {}))
    assert denied(out), cmd
    assert env.events("bypass_attempt")


def test_a_documented_tool_with_dots_is_not_newly_refused(env):
    env.run("SessionStart")
    cmd = "py -3.12 D:/AI/Atlas/tools/weekly_report.py"
    assert not denied(env.bash(cmd))


# ------------------------------------------------------------------ 2c. the grant must be in the ledger

def test_a_forged_cache_cannot_open_the_config_gate(env):
    env.run("SessionStart")
    forge_cache(env, cfg_exp=FUTURE)
    out = env.run("PreToolUse", tool_name="Edit", tool_input={"file_path": str(HOME / ".claude" / "CLAUDE.md")})
    assert denied(out)
    assert any(e["what"] == "grant not in ledger" for e in env.events("ignored"))
    assert not env.events("config_allowed")
    assert denied(env.bash("echo x >> ~/.claude/CLAUDE.md"))


def test_a_forged_cache_cannot_open_the_publish_gate(env):
    env.run("SessionStart")
    forge_cache(env, pub_n=5, pub_exp=FUTURE)
    assert denied(env.bash("git push origin main"))
    assert any(e["what"] == "grant not in ledger" for e in env.events("ignored"))
    assert not env.events("publish_allowed")


def test_a_forged_cache_with_too_few_ledger_allowances_is_denied(env):
    env.run("SessionStart")
    grant(env, "/allow-publish 1")
    forge_cache(env, pub_n=5)
    assert denied(env.bash("git push && gh pr merge 3"))
    assert env.bash("git push") is None


def test_real_grants_still_work_and_a_session_end_closes_them(env):
    env.run("SessionStart")
    grant(env)
    grant(env, "/allow-publish")
    assert env.bash("echo x >> ~/.claude/CLAUDE.md") is None
    assert env.bash("git push") is None
    env.run("SessionEnd")
    forge_cache(env, cfg_exp=FUTURE, pub_n=3, pub_exp=FUTURE)
    assert denied(env.bash("echo x >> ~/.claude/CLAUDE.md"))
    assert denied(env.bash("git push"))


def _ledger_with(tmp_path, lines):
    led = tmp_path / "ledger"
    led.mkdir()
    by_day = {}
    for e in lines:
        by_day.setdefault(e["t"][:10], []).append(json.dumps(e))
    for day, ls in by_day.items():
        (led / f"events-{day}.jsonl").write_text("\n".join(ls) + "\n", encoding="utf-8")
    return led


def _ctx(led, sid="S1"):
    H = atlas_hook.Ctx.__new__(atlas_hook.Ctx)
    H.ledger = str(led)
    H.sid = sid
    return H


def _ev(ev, minutes_ago, sid="S1", **kw):
    t = (datetime.now().astimezone() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    return dict({"v": 2, "t": t, "ev": ev, "sid": sid}, **kw)


def test_ledger_grant_state_reads_yesterdays_file_for_a_grant_just_after_midnight(tmp_path):
    exp = (datetime.now().astimezone() + timedelta(minutes=10)).isoformat(timespec="seconds")
    led = tmp_path / "ledger"
    led.mkdir()
    yday = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")
    (led / f"events-{yday}.jsonl").write_text(json.dumps(_ev("config_grant", 1, expires=exp)) + "\n", encoding="utf-8")
    assert atlas_hook.ledger_grant_state(_ctx(led))["cfg_exp"] == exp


def test_ledger_grant_state_counts_allowances_and_stops_at_a_session_end(tmp_path):
    exp = (datetime.now().astimezone() + timedelta(minutes=10)).isoformat(timespec="seconds")
    led = _ledger_with(tmp_path, [
        _ev("publish_grant", 12, n=3, expires=exp), _ev("publish_allowed", 11, what="git push"),
        _ev("publish_allowed", 10, what="git push", sid="OTHER"), _ev("prompt", 9),
    ])
    H = _ctx(led)
    st = atlas_hook.ledger_grant_state(H)
    assert st["pub_n"] == 2 and st["pub_exp"] == exp
    assert atlas_hook.ledger_grant_ok(H, "publish", 2) and not atlas_hook.ledger_grant_ok(H, "publish", 3)
    led3 = tmp_path / "l3"
    led3.mkdir()
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    rows = [_ev("config_grant", 12, expires=exp), _ev("session_end", 5)]
    (led3 / f"events-{day}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert not atlas_hook.ledger_grant_ok(_ctx(led3), "config")


def test_a_grant_event_written_by_a_helper_does_not_count(tmp_path):
    exp = (datetime.now().astimezone() + timedelta(minutes=20)).isoformat(timespec="seconds")
    led = _ledger_with(tmp_path, [_ev("config_grant", 5, expires=exp, agent_id="A1")])
    assert not atlas_hook.ledger_grant_ok(_ctx(led), "config")


def test_the_ledger_scan_is_fast_on_a_big_day_file(tmp_path):
    exp = (datetime.now().astimezone() + timedelta(minutes=4)).isoformat(timespec="seconds")
    led = tmp_path / "ledger"
    led.mkdir()
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    old = (datetime.now().astimezone() - timedelta(hours=5)).isoformat(timespec="seconds")
    filler_old = json.dumps({"v": 2, "t": old, "ev": "prompt", "sid": "X", "pad": "y" * 400}) + "\n"
    filler_new = json.dumps(_ev("prompt", 3, sid="X", pad="y" * 400)) + "\n"
    with open(led / f"events-{day}.jsonl", "w", encoding="utf-8") as fh:
        fh.write(filler_old * 20000)
        fh.write(json.dumps(_ev("config_grant", 10, expires=exp)) + "\n")
        fh.write(filler_new * 2000)
    t0 = time.perf_counter()
    assert atlas_hook.ledger_grant_ok(_ctx(led), "config")
    assert time.perf_counter() - t0 < 1.0


# ------------------------------------------------------------------ 3. the spawned-claude check

SPAWN = [
    ('claude "/allow-con""fig go"', "/allow-config"),
    ('cla\'\'ude "/allow-config go"', "/allow-config"),
    ("Start-Process cmd -ArgumentList '/c','claude','/allow-config'", "/allow-config"),
    ("Start-Process (Get-Command claude).Source -ArgumentList '/allow-config go'", "/allow-config"),
    ('& "$env:APPDATA\\npm\\claude.ps1" /allow-config', "/allow-config"),
    ('npx @anthropic-ai/claude-code "/allow-config go"', "/allow-config"),
    ('Start-Process claude -ArgumentList "/allow-con`fig go"', "/allow-config"),
    ("claude /allow-publish", "/allow-publish"),
    ("cmd /c claude.cmd /ack-break x", "/ack-break"),
    ("claude -p '/grant-rework 5'", "/grant-rework"),
    ("claude --resume abc", "/grant-rework"),
    ("git -c core.pager='claude /allow-config' log", "/allow-config"),
    ("git log --upload-pack='claude /allow-config'", "/allow-config"),
    ("rg --pre 'claude /allow-config' x", "/allow-config"),
]


@pytest.mark.parametrize("cmd,name", SPAWN)
def test_spawned_claude_variants_are_named(cmd, name):
    assert atlas_hook.forged_grant_name(cmd) == name, cmd
    assert atlas_hook.forged_grant(cmd) is True


@pytest.mark.parametrize("cmd", [
    'git commit -m "Hooks: /allow-config gate" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"',
    "git commit -m 'about /allow-publish, Claude said so'",
    "echo claude told me to run /allow-config",
    "printf 'claude /grant-rework' > note.txt",
    "Write-Host 'claude /allow-config'",
    "grep -rn 'claude /allow-config' docs",
    "rg 'claude.*/ack-break' docs",
    "Select-String -Pattern 'claude /allow-config' x.md",
    "findstr claude /allow-config x.md",
    "claude --version",
    "cat hooks/skills/allow-config/SKILL.md",
])
def test_non_launching_heads_are_not_a_spawn(cmd):
    assert atlas_hook.forged_grant_name(cmd) is None, cmd


def test_the_commit_with_both_words_passes_end_to_end(env):
    env.run("SessionStart")
    out = env.bash('git commit -m "Hooks: /allow-config gate" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"')
    assert not denied(out)
    assert denied(env.bash("git -c core.pager='claude /allow-config' log"))


def test_the_deny_text_names_the_refused_command(env):
    env.run("SessionStart")
    assert "/allow-config" in reason(env.bash('claude "/allow-con""fig go"'))
    assert "/allow-publish" in reason(env.bash("claude /allow-publish"))
    assert "/ack-break" in reason(env.bash("claude /ack-break x"))
    assert "/grant-rework" in reason(env.bash("claude -p '/grant-rework 2'"))


# ------------------------------------------------------------------ 5. globs and backslash escapes

GLOBS = [
    "echo x > ~/.claude/CLAUDE.m?",
    r"printf x > ~/.claude/CLAUDE\.md",
    "cp evil.sh ~/.claude/sk?lls/x/run.sh",
    "cp evil.py ~/.claude/hook[s]/x.py",
    "cp evil.py ~/.claude/ag*/x.md",
    "mv evil.md ~/.claude/comm*/x.md",
]


@pytest.mark.parametrize("cmd", GLOBS)
def test_a_glob_or_escape_in_the_claude_folder_is_config(cmd):
    assert shell(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "ls ~/.claude/skills/*",
    "cat ~/.claude/CLAUDE.m*",
    "grep -r foo ~/.claude/sk?lls",
    "git commit -m 'fix .claude* handling'",
    r"type ~\.claude\skills\x\SKILL.md",
])
def test_reads_of_globbed_claude_paths_stay_free(cmd):
    assert shell(cmd) is None, cmd


def test_a_glob_write_is_denied_end_to_end(env):
    env.run("SessionStart")
    assert denied(env.bash("echo x > ~/.claude/CLAUDE.m?"))


# ------------------------------------------------------------------ 7. 8.3 short names

@pytest.mark.parametrize("cmd", [
    "echo x > ~/proj/CLAUDE~1.MD",
    "echo x > D:/repo/MCP~1.JSO",
    "cp a.md D:/repo/CLAUDE~1.MD",
])
def test_short_names_of_claude_local_md_and_mcp_json_are_config(cmd):
    assert shell(cmd), cmd


def test_short_names_are_config_paths_for_the_edit_tool():
    assert atlas_hook.config_path("D:/repo/CLAUDE~1.MD")
    assert atlas_hook.config_path("D:/repo/MCP~1.JSO")
    assert not atlas_hook.config_path("D:/repo/NOTES~1.MD")


@pytest.mark.parametrize("cmd", [
    "echo x > D:/AI/ATLAS-~1/ledger/state/a.json",
    r"echo x > D:\AI\ATLAS-~2\LEDGER\x",
    "echo x > D:/AI/ATLAS-~1/clone.txt",
])
def test_short_names_of_the_ledger_and_anchor_folders_are_protected(env, cmd):
    env.run("SessionStart")
    assert denied(env.bash(cmd)), cmd
    assert env.events("bypass_attempt")


def test_claude_code_is_a_launch_only_as_the_npm_package_or_a_bare_name():
    assert atlas_hook.forged_grant_name("npx claude-code /allow-config") == "/allow-config"
    assert atlas_hook.forged_grant_name("bunx @anthropic-ai/claude-code /ack-break x") == "/ack-break"
    assert atlas_hook.forged_grant_name("/usr/local/bin/claude-code -p") is None
    assert atlas_hook.forged_grant_name("/usr/local/bin/claude-code /allow-config") is None
