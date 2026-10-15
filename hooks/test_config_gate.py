"""Config gate (contracts §6.4c) and the path hardenings (§6.5): a change to instructions, skills, agent definitions or
settings is denied in EVERY mode unless the operator typed /allow-config. Subprocess runs against temp ledgers plus
in-process checks of the path rules. Never the live ledger or the live ~/.claude."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_atlas_hook import SID, env  # noqa: E402,F401  (env is a fixture)
import atlas_hook  # noqa: E402

HOME = Path.home()


def denied(out):
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def grant(env, text="/allow-config", **kw):
    return env.run("UserPromptSubmit", prompt=text, **kw)


def refusals(env):
    return [e for e in env.events("refusal") if e.get("guard") == "config"]


def edit_cfg(env, path, tool="Edit", **kw):
    return env.run("PreToolUse", tool_name=tool, tool_input={"file_path": str(path)}, **kw)


@pytest.fixture
def hook(tmp_path):
    H = atlas_hook.Ctx.__new__(atlas_hook.Ctx)
    H.cfg = {"exempt_temp": False}
    (tmp_path / "ledger").mkdir()
    H.ledger = str(tmp_path / "ledger")
    return H


class Stub:
    cwd = r"D:\work"


def shell(cmd, cwd=r"D:\work"):
    s = Stub()
    s.cwd = cwd
    return atlas_hook.config_shell(s, cmd)


# ------------------------------------------------------------------ what is a config path

CONFIG = [
    "D:/repo/CLAUDE.md", r"D:\repo\claude.local.md", "D:/repo/skills/x/SKILL.md", "D:/repo/sub/skill.md",
    str(HOME / ".claude" / "CLAUDE.md"), str(HOME / ".claude" / "profile.md"),
    str(HOME / ".claude" / "settings.json"), str(HOME / ".claude" / "settings.local.json"),
    str(HOME / ".claude" / "skills" / "x" / "notes.txt"), str(HOME / ".claude" / "agents" / "a.md"),
    str(HOME / ".claude" / "commands" / "c.md"), str(HOME / ".claude" / "hooks" / "require_named_helper.py"),
    str(HOME / ".claude" / "rules" / "r.md"), r"D:\repo\.claude\skills\launch-auto\SKILL.md",
    r"D:\repo\.claude\agents\implementer.md", r"D:\repo\.claude\settings.json",
    r"D:\AI\Atlas\.claude\worktrees\w1\CLAUDE.md", r"D:\AI\Atlas\.claude\worktrees\w1\.claude\skills\s\a.txt",
    "D:/repo/.CLAUDE/SKILLS/x.txt",
    "D:/repo/CLAUDE.md:x", "D:/repo/CLAUDE.md::$DATA", str(HOME / ".claude" / "settings.json:y"),
]
NOT_CONFIG = [
    str(HOME / ".claude" / "projects" / "x" / "memory" / "a.md"), str(HOME / ".claude" / "projects" / "x" / "t.jsonl"),
    r"D:\AI\Atlas\.claude\worktrees\x\README.md", r"D:\AI\Atlas\.claude\worktrees\x\src\main.py",
    str(HOME / ".claude" / "plans" / "p.md"), str(HOME / ".claude" / "settings-notes.txt"),
    str(HOME / ".claude" / "keybindings.json"), "D:/repo/README.md", "D:/repo/skills/x/notes.md",
    "D:/repo/claude.md.bak", "D:/repo/my-CLAUDE.md", "D:/repo/agents/a.md", "D:/repo/.claude/other/x.md",
    "D:/repo/src/settings.json",
]


@pytest.mark.parametrize("path", CONFIG)
def test_config_paths(hook, path):
    assert hook.is_config(path), path


@pytest.mark.parametrize("path", NOT_CONFIG)
def test_not_config_paths(hook, path):
    assert not hook.is_config(path), path


def test_the_installed_hooks_folder_stays_protected_not_config_gated(env, tmp_path):
    installed = tmp_path / ".claude" / "hooks" / "atlas"          # looks like the installed folder: also a config path
    installed.mkdir(parents=True)
    xe = {"ATLAS_HOOKS_DIR": str(installed)}
    grant(env, extra_env=xe)
    out = edit_cfg(env, installed / "atlas_hook.py", extra_env=xe)
    assert denied(out) and "can't be edited" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not env.events("config_allowed") and not refusals(env)


# ------------------------------------------------------------------ the Edit tools

@pytest.mark.parametrize("mode", ["record", "enforce"])
@pytest.mark.parametrize("tool", ["Edit", "Write", "NotebookEdit"])
def test_edit_is_denied_without_a_grant_in_every_mode(env, mode, tool):
    env.set_mode(mode)
    out = edit_cfg(env, env.work / "CLAUDE.md", tool=tool)
    assert denied(out)
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == atlas_hook.CONFIG_DENY
    r = refusals(env)[-1]
    assert r["would"] is False and r["actor"] == "main" and r["path"].endswith("CLAUDE.md") and r["tool"] == tool


def test_a_refusal_logs_the_path_but_nothing_else(env):
    edit_cfg(env, env.work / "skills" / "x" / "SKILL.md")
    r = refusals(env)[0]
    assert set(r) >= {"guard", "would", "path", "actor", "detail", "tool"}
    assert "old_string" not in json.dumps(r)


def test_an_ordinary_edit_is_not_gated(env):
    env.set_mode("enforce")
    assert edit_cfg(env, env.work / "a.py") is None
    assert not refusals(env)


def test_a_grant_allows_the_edit_and_logs_it_without_an_edit_event_or_a_version_bump(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    grant(env)
    g = env.events("config_grant")[0]
    assert 14 * 60 <= (datetime.fromisoformat(g["expires"]) - datetime.fromisoformat(g["t"])).total_seconds() <= 15 * 60 + 5
    assert g["entry"] == "cli"
    p = env.work / "CLAUDE.md"
    assert edit_cfg(env, p) is None
    assert edit_cfg(env, p) is None                           # a window, not a count: a second edit passes too
    post = env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": str(p)})
    assert post is None
    ok = env.events("config_allowed")
    assert len(ok) == 2 and ok[0]["path"].endswith("CLAUDE.md") and ok[0]["tool"] == "Edit"
    assert not env.events("edit")
    assert env.state()["version"] == 0
    assert env.run("Stop", stop_hook_active=False) is None   # nothing unchecked


def test_a_grant_does_not_open_other_gates(env):
    env.set_mode("enforce")
    grant(env)
    assert denied(env.bash("git push"))


def test_an_expired_grant_denies(env):
    env.set_mode("enforce")
    grant(env)
    sp = env.ledger / "state" / f"{SID}.json"
    S = json.loads(sp.read_text(encoding="utf-8"))
    S["cfg_exp"] = "2020-01-01T00:00:00+00:00"
    sp.write_text(json.dumps(S), encoding="utf-8")
    assert denied(edit_cfg(env, env.work / "CLAUDE.md"))
    assert not env.events("config_allowed")


def test_a_helper_is_denied_even_with_a_grant(env):
    env.set_mode("enforce")
    grant(env)
    out = edit_cfg(env, env.work / "SKILL.md", agent_id="A1", agent_type="implementer")
    assert denied(out)
    r = refusals(env)[-1]
    assert r["actor"] == "implementer" and r["agent_id"] == "A1"
    assert not env.events("config_allowed")
    assert edit_cfg(env, env.work / "SKILL.md") is None      # the main session still has it


def test_a_grant_typed_inside_a_helper_is_ignored(env):
    grant(env, agent_id="A1", agent_type="implementer")
    assert not env.events("config_grant")


def test_a_grant_from_a_non_interactive_entry_is_ignored(env):
    env.set_mode("enforce")
    grant(env, extra_env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
    assert not env.events("config_grant")
    assert any(e["what"] == "config grant from non-interactive entry" for e in env.events("ignored"))
    assert denied(edit_cfg(env, env.work / "CLAUDE.md"))


def test_prompts_that_only_mention_the_command_grant_nothing(env):
    for text in ("please /allow-config", "/allow-configs", "/allow-config-x"):
        grant(env, text)
    assert not env.events("config_grant")


def test_the_grant_ends_with_the_session(env):
    env.set_mode("enforce")
    grant(env)
    env.run("SessionEnd", reason="clear")
    assert denied(edit_cfg(env, env.work / "CLAUDE.md"))


def test_a_deleted_state_cache_denies_at_once(env):
    env.set_mode("enforce")
    grant(env)
    (env.ledger / "state" / f"{SID}.json").unlink()
    assert denied(edit_cfg(env, env.work / "CLAUDE.md"))
    assert not (env.ledger / "state" / f"{SID}.json").exists()


def test_the_grant_is_per_session(env):
    grant(env)
    assert denied(edit_cfg(env, env.work / "CLAUDE.md", sid="s2abcdefgh"))
    assert edit_cfg(env, env.work / "CLAUDE.md") is None


def test_the_model_cannot_invoke_the_skill_in_any_mode(env):
    for mode in ("record", "enforce"):
        env.set_mode(mode)
        out = env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "allow-config"})
        assert denied(out) and "allow-config is user-only" in out["hookSpecificOutput"]["permissionDecisionReason"]
    out = env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "atlas:allow-config"})
    assert denied(out)
    assert any(e["what"] == "model invoked allow-config skill" for e in env.events("ignored"))


@pytest.mark.parametrize("tool,key", [("CronCreate", "prompt"), ("ScheduleWakeup", "prompt"), ("SendMessage", "message"),
                                      ("SendMessage", "content")])
def test_a_forwarded_allow_config_is_refused(env, tool, key):
    out = env.run("PreToolUse", tool_name=tool, tool_input={key: "/allow-config"})
    assert denied(out) and "/allow-config" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert any("forged user-only command" in e["what"] for e in env.events("ignored"))


def test_a_mention_inside_a_message_is_not_a_forward(env):
    assert env.run("PreToolUse", tool_name="CronCreate", tool_input={"prompt": "see /allow-config-notes.md"}) is None


# ------------------------------------------------------------------ the shell

GATED = [
    "echo x >> ~/.claude/CLAUDE.md",
    "echo x > $HOME/.claude/CLAUDE.md",
    r"Set-Content $env:USERPROFILE\.claude\settings.json -Value x",
    r"'x' | Out-File %USERPROFILE%\.claude\settings.local.json",
    "cp a.md ~/.claude/skills/x/SKILL.md",
    "cp a.md ~/.claude/agents/",
    "mv a.md ~/.claude/commands/c.md",
    "rm -rf ~/.claude/skills/x",
    r"Remove-Item -Recurse C:\Home\x\.claude\agents\old.md",
    "sed -i s/a/b/ CLAUDE.md",
    "sed -i.bak s/a/b/ ~/.claude/CLAUDE.md",
    "tee ~/.claude/rules/r.md",
    "printf x | tee -a ~/.claude/profile.md",
    "py -3.12 tools/install_skills.py",
    "python tools/install_hooks.py --python x",
    r"py -3.12 D:\AI\Atlas\tools\install_hooks.py",
    "bash -c 'py tools/install_skills.py'",
    "ATLAS_SKILLS_DIR=/tmp/x py tools/install_skills.py --dry-run",
    "echo hi > skills/x/SKILL.md",
    "cat a > CLAUDE.local.md",
    "python -c \"open('/c/Home/x/.claude/CLAUDE.md','w').write('x')\"",
    "git -C ~/.claude/skills checkout -- .",
    "cd ~/.claude/skills && rm -rf x",
    "cd ~/.claude/skills; python fix.py",
    "echo x > \"C:/Home/x/.claude/CLAUDE.md\"",
    "echo x >> '~/.claude//CLAUDE.md'",
    "echo x >> ~/.claude/./skills/x.md",
    "printf '%s' x > ~/.claude/settings.json",
    "cmd /c echo x > %USERPROFILE%\\.claude\\CLAUDE.md",
    "powershell -Command \"Add-Content $HOME\\.claude\\CLAUDE.md x\"",
]
FREE = [
    "cat ~/.claude/CLAUDE.md",
    "cat CLAUDE.md",
    r"Get-Content $env:USERPROFILE\.claude\settings.json",
    "ls ~/.claude/skills",
    "grep -rn foo ~/.claude/skills",
    "sed -n 1,20p ~/.claude/CLAUDE.md",
    "sed -n '/foo/p' CLAUDE.md",
    "head -5 skills/x/SKILL.md",
    "git diff -- CLAUDE.md",
    "git log -p CLAUDE.md",
    "git show HEAD:CLAUDE.md",
    "git status",
    "git add CLAUDE.md",
    "git commit -m 'update CLAUDE.md'",
    "git -C ~/.claude/skills status",
    "cp ~/.claude/skills/x/SKILL.md ./copy.md",
    "diff CLAUDE.md other.md",
    "cd ~/.claude/skills && ls",
    "cd ~/.claude/skills; cat x/SKILL.md",
    "echo 'see CLAUDE.md'",
    "echo hello > notes.txt",
    "cat a.md > b.md",
    "ls ~/.claude/projects/x/memory",
    "cat ~/.claude/projects/x/memory/a.md",
    "echo x >> ~/.claude/projects/x/memory/a.md",
    "echo x >> ~/.claude/plans/p.md",
    "pytest hooks/test_install_hooks.py",
    "git add tools/install_hooks.py",
    "cat tools/install_skills.py",
    "grep -n install_hooks.py docs",
    "ls -la",
    "py -3.12 -m pytest hooks -q",
    "cat > skills/x/notes.md",
    "mkdir -p ~/.claude/skills/new-skill",
]


@pytest.mark.parametrize("cmd", GATED)
def test_shell_gated(cmd):
    assert shell(cmd), cmd


@pytest.mark.parametrize("cmd", FREE)
def test_shell_free(cmd):
    assert shell(cmd) is None, cmd


def test_cwd_inside_a_config_folder_gates_anything_but_a_read():
    cwd = str(HOME / ".claude" / "skills" / "x")
    assert shell("python fix.py", cwd=cwd) == "cwd"
    assert shell("echo x > out.txt", cwd=cwd)
    assert shell("ls", cwd=cwd) is None
    assert shell("ls 2>/dev/null", cwd=cwd) is None
    assert shell("cat a > b.md", cwd=cwd)
    assert shell("cd .. && echo x > out.txt", cwd=cwd)
    assert shell("cat SKILL.md", cwd=cwd) is None
    assert shell("git status", cwd=cwd) is None
    assert shell("python fix.py", cwd=str(HOME / ".claude" / "projects" / "x")) is None


def test_a_backtick_escape_does_not_hide_a_path():
    assert shell("Set-Content $HOME\\.cla`ude\\CLAUDE.md x")


def test_oversized_and_unreadable_commands_fail_closed():
    assert shell("echo " + "a" * (atlas_hook.PUBLISH_MAX_CHARS + 1)) == "oversized command"

    assert atlas_hook.config_shell(Stub(), "ls") is None


@pytest.mark.parametrize("mode", ["record", "enforce"])
def test_shell_is_denied_without_a_grant_in_every_mode(env, mode):
    env.set_mode(mode)
    out = env.bash("echo x >> ~/.claude/CLAUDE.md")
    assert denied(out) and out["hookSpecificOutput"]["permissionDecisionReason"] == atlas_hook.CONFIG_DENY
    r = refusals(env)[-1]
    assert r["would"] is False and r["what"] == "claude-md" and r["actor"] == "main"
    assert "echo" not in json.dumps(r)


def test_powershell_tool_is_gated_too(env):
    out = env.run("PreToolUse", tool_name="PowerShell", tool_input={"command": r"Set-Content $env:USERPROFILE\.claude\settings.json x"})
    assert denied(out)


def test_reads_pass_and_log_nothing(env):
    env.set_mode("enforce")
    assert env.bash("cat ~/.claude/CLAUDE.md") is None
    assert not refusals(env) and not env.events("config_allowed")


def test_a_grant_lets_the_shell_change_through_and_logs_the_label_not_the_command(env):
    env.set_mode("enforce")
    grant(env)
    assert env.bash("echo secret-text >> ~/.claude/CLAUDE.md") is None
    ok = env.events("config_allowed")[0]
    assert ok["what"] == "claude-md" and ok["tool"] == "Bash"
    assert "secret-text" not in json.dumps(env.events())


def test_a_helper_shell_change_is_denied_even_with_a_grant(env):
    env.set_mode("enforce")
    grant(env)
    assert denied(env.bash("py tools/install_skills.py", agent_id="A1", agent_type="implementer"))
    assert env.bash("py tools/install_skills.py") is None


def test_the_ledger_guard_still_comes_first(env):
    out = env.bash("echo x >> ~/.claude/hooks/atlas/atlas_config.json")
    assert denied(out) and "Protected Atlas ledger/hooks folder" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not refusals(env)


def test_the_publish_gate_still_works_after_the_config_check(env):
    env.set_mode("enforce")
    out = env.bash("git push")
    assert denied(out) and "Publishing needs" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ------------------------------------------------------------------ NTFS alternate data streams

@pytest.mark.parametrize("path", [r"D:\p\scratch-a.md:x", r"D:\p\_scratch\log.txt:s", r"D:\p\a.scratch.md::$DATA",
                                  r"D:\p\x.build-log.md:y"])
def test_a_stream_is_never_exempt(hook, path):
    assert not hook.is_exempt(path)
    assert not atlas_hook.scratch_path(path)


def test_the_plain_scratch_name_stays_exempt(hook):
    assert hook.is_exempt(r"D:\p\scratch-a.md")
    assert atlas_hook.scratch_path(r"D:\p\_scratch\log.txt")


def test_stream_suffix_is_stripped_for_protected(hook, tmp_path):
    f = Path(hook.ledger) / "events-2026-10-13.jsonl"
    assert hook.is_protected(f)
    assert hook.is_protected(str(f) + ":x")
    assert hook.is_protected(str(Path(hook.ledger) / "state" / "s.json") + "::$DATA")
    assert not hook.is_protected(str(tmp_path / "other.txt:x"))


def test_a_stream_edit_is_recorded_and_a_config_stream_is_gated(env):
    env.set_mode("enforce")
    out = edit_cfg(env, str(env.work / "CLAUDE.md") + ":x")
    assert denied(out) and refusals(env)
    p = str(env.work / "scratch-a.md") + ":x"
    assert edit_cfg(env, p) is None
    env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": p})
    assert len(env.events("edit")) == 1


def test_a_ledger_stream_edit_is_denied(env):
    out = edit_cfg(env, str(env.ledger / "events-2026-10-13.jsonl") + ":x")
    assert denied(out) and "can't be edited" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ------------------------------------------------------------------ junctions and symlinks

def junction(link: Path, target: Path):
    """A directory junction (no privilege needed); the test is skipped when the platform cannot make one."""
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    except Exception as e:  # noqa: BLE001
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except Exception:  # noqa: BLE001
            pytest.skip("cannot create a junction or symlink here: %s" % e)


def test_a_scratch_named_junction_does_not_make_a_file_exempt(hook, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.md").write_text("x")
    link = tmp_path / "_scratch"
    junction(link, real)
    assert (link / "a.md").exists()
    assert not hook.is_exempt(link / "a.md")
    assert not hook.is_exempt(link / "new.md")                  # not yet created: the real form still resolves
    assert not atlas_hook.scratch_path(link / "a.md")
    plain = tmp_path / "_scratch" / ".." / "real" / "a.md"
    assert not hook.is_exempt(plain)


def test_a_real_scratch_folder_is_still_exempt(hook, tmp_path):
    d = tmp_path / "_scratch"
    d.mkdir()
    (d / "a.md").write_text("x")
    assert hook.is_exempt(d / "a.md")


def test_a_junction_into_an_exempt_dir_is_exempt_only_if_the_real_form_is_too(hook, tmp_path):
    hook.cfg = {"exempt_temp": False, "exempt_dirs": [str(tmp_path / "ok")]}
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    junction(tmp_path / "ok2", tmp_path / "bad")
    assert hook.is_exempt(tmp_path / "ok" / "x.py")
    assert not hook.is_exempt(tmp_path / "bad" / "x.py")
    assert not hook.is_exempt(tmp_path / "ok2" / "x.py")


def test_a_junction_into_the_ledger_is_protected(hook, tmp_path):
    link = tmp_path / "innocent"
    junction(link, Path(hook.ledger))
    assert hook.is_protected(link / "events-2026-10-13.jsonl")
    assert hook.is_protected(link / "state" / "s.json")
    assert not hook.is_protected(tmp_path / "other" / "x.txt")


def test_a_junction_into_a_config_folder_is_config(hook, tmp_path):
    real = tmp_path / "home" / ".claude" / "skills"
    real.mkdir(parents=True)
    link = tmp_path / "shortcut"
    junction(link, real)
    assert hook.is_config(link / "x" / "notes.txt")
    assert not hook.is_config(tmp_path / "plain" / "notes.txt")


def test_edit_through_a_ledger_junction_is_denied_end_to_end(env):
    env.ledger.mkdir(exist_ok=True)
    link = env.work / "innocent"
    junction(link, env.ledger)
    out = edit_cfg(env, link / "events-2026-10-13.jsonl")
    assert denied(out) and "can't be edited" in out["hookSpecificOutput"]["permissionDecisionReason"]


# ------------------------------------------------------------------ small path rules

@pytest.mark.parametrize("name", ["claude.local.md", "CLAUDE.LOCAL.MD", "agent.md", "AGENT.md"])
def test_more_never_scratch_names(hook, name):
    assert not hook.is_exempt(r"D:\p\_scratch\%s" % name)


def test_norm_path_is_unchanged():
    assert atlas_hook.norm_path(r"D:\A\b\..\C.md") == os.path.normcase(os.path.abspath(r"D:\A\b\..\C.md"))


def test_the_skill_file_exists_and_is_user_only():
    p = Path(__file__).resolve().parent / "skills" / "allow-config" / "SKILL.md"
    text = p.read_text(encoding="utf-8")
    assert "name: allow-config" in text and "disable-model-invocation: true" in text
    assert "\u2014" not in text


def test_the_config_gate_keeps_a_session_working_in_a_subprocess_with_a_clean_exit(env):
    r = env.run_raw(json.dumps({"hook_event_name": "PreToolUse", "session_id": SID, "cwd": str(env.work),
                                "tool_name": "Edit", "tool_input": {"file_path": ""}}))
    assert r.returncode == 0 and r.stdout == ""


# ------------------------------------------------------------------ round 2 (senior review findings)

GATED_R2 = [
    # 1 copy or move of a config-named file into a folder
    "cp /tmp/CLAUDE.md ~/.claude/",
    "cp /tmp/settings.json ~/.claude",
    r"Copy-Item C:\tmp\settings.json $HOME\.claude" + "\\",
    "cp /tmp/CLAUDE.md D:/AI/Atlas/",
    "cp /tmp/settings.json D:/AI/Atlas",
    "rsync /tmp/x/ ~/.claude/",
    "mv /tmp/CLAUDE.md /d/repo/",
    "cp /tmp/CLAUDE.md .",
    "cp /tmp/.mcp.json /d/repo",
    "robocopy C:\\tmp D:\\repo CLAUDE.md",
    # 2 quotes inside the path
    "echo x >> ~/.cl''aude/CLAU''DE.md",
    "echo x >> ~/.cl''aude/settings.json",
    'echo x >> ~/.cl"aude"/settings.json',
    # 3 the bare folder
    "cd ~/.claude && cp /tmp/x settings.json",
    "cd ~/.claude && cp /tmp/x agents/evil.md",
    "git -C ~/.claude reset --hard",
    "tar -xf /tmp/e.tar -C ~/.claude",
    "unzip -o /tmp/e.zip -d ~/.claude",
    "Expand-Archive e.zip -DestinationPath $HOME/.claude -Force",
    r"cmd /c mklink /J C:\tmp\j ~\.claude",
    "cp /tmp/x ~/.claude/",
    "tar -xf /tmp/e.tar -C ~/.claude/.",
    # 5 git programs and output flags
    'git fetch "--upload-pack=cp /tmp/x ~/.claude/CLAUDE.md #" /d/AI/Atlas',
    'git grep "-Ocp /tmp/x ~/.claude/CLAUDE.md #" foo',
    "git -c core.pager='cp /tmp/x ~/.claude/CLAUDE.md' log -p",
    "git ls-remote --upload-pack='cp /tmp/x ~/.claude/CLAUDE.md' origin",
    # 6 the path in one segment, the write in the next
    "echo ~/.claude/CLAUDE.md | xargs cp /tmp/x",
    "printf '%s' ~/.claude/settings.json | xargs -I{} cp /tmp/x {}",
    "echo 'cp /tmp/x ~/.claude/CLAUDE.md' | sh",
    "echo 'cp /tmp/x ~/.claude/CLAUDE.md' | bash",
    "'$HOME/.claude/settings.json' | % { Copy-Item C:/tmp/x $_ }",
    "echo 'Set-Content $HOME/.claude/CLAUDE.md x' | iex",
    "ls ~/.claude/skills | while read f; do cp /tmp/x $f; done",
    # 7 8.3 short names
    r"echo x > ~\CLAUDE~1\settings.json",
    "echo x > ~/CLAUDE~1/CLAUDE.md",
    "echo x > ~/CLAUDE~1/skills/s/SKILL.md",
    "echo x > ~/CLAUDE~1.JSO",
    "tar -xf /tmp/e.tar -C ~/CLAUDE~1",
    # 9 the wider scope
    "echo x > ~/.claude/output-styles/s.md",
    "echo x > ~/.claude/plugins/p/plugin.json",
    "echo x > ~/.claude.json",
    r"Set-Content $HOME\.claude.json x",
    "echo x > D:/repo/.mcp.json",
    "sed -i s/a/b/ .mcp.json",
]
FREE_R2 = [
    "cp ~/.claude/CLAUDE.md ./backup.md",
    "cp CLAUDE.md CLAUDE.md.bak",
    "cp /tmp/notes.txt D:/AI/Atlas/",
    "cp /tmp/x.py D:/AI/Atlas/tools/",
    "ls ~/.claude",
    "ls ~/.claude/",
    "find ~/.claude -name x",
    "grep -r x ~/.claude",
    "git -C ~/.claude status",
    "git -C ~/.claude log --oneline",
    "du -sh ~/.claude",
    "cd ~/.claude",
    "cd ~/.claude && ls",
    "cd ~/.claude && cat settings.json",
    "cat ~/.claude/projects/x/memory/a.md",
    "echo x >> ~/.claude/projects/x/memory/a.md",
    "cp /tmp/a.md ~/.claude/projects/x/memory/a.md",
    "ls ~/.claude/projects/x/memory",
    "cat ~/.claude.json",
    "cat .mcp.json",
    "ls ~/.claude/plugins",
    "ls ~/.claude/output-styles",
    "git commit -m 'note: /allow-config exists'",
    "grep allow-config docs/v2/contracts.md",
    "echo ~/.claude/CLAUDE.md | wc -c",
    "cat ~/.claude/CLAUDE.md | grep foo",
    "git diff -- CLAUDE.md",
    "git show HEAD:CLAUDE.md",
]


@pytest.mark.parametrize("cmd", GATED_R2)
def test_shell_gated_r2(cmd):
    assert shell(cmd), cmd


@pytest.mark.parametrize("cmd", FREE_R2)
def test_shell_free_r2(cmd):
    assert shell(cmd) is None, cmd


def test_cwd_bare_claude_folder_gates_anything_but_a_read():
    cwd = str(HOME / ".claude")
    assert shell("cp /tmp/x settings.json", cwd=cwd)
    assert shell("python fix.py", cwd=cwd)
    assert shell("ls", cwd=cwd) is None
    assert shell("git status", cwd=cwd) is None
    assert shell("cp /tmp/x settings.json", cwd=str(HOME / ".claude" / "projects" / "p")) is None
    assert shell("cp /tmp/x settings.json", cwd=r"~\CLAUDE~1")


def test_a_copy_into_a_plain_folder_logs_a_label(env):
    env.set_mode("enforce")
    out = env.bash("cp /tmp/CLAUDE.md D:/AI/Atlas/")
    assert denied(out)
    assert refusals(env)[-1]["what"] == "config-file-copy"


def test_a_short_name_hooks_folder_is_protected_by_the_ledger_guard(env):
    out = env.bash("echo x > ~/CLAUDE~1/hooks/atlas/atlas_config.json")
    assert denied(out) and "Protected Atlas ledger/hooks folder" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("path", [
    str(HOME / ".claude" / "output-styles" / "s.md"), str(HOME / ".claude" / "plugins" / "p" / "x.json"),
    str(HOME / ".claude.json"), r"D:\repo\.mcp.json", r"C:\Home\x\.claude.json:y", r"C:\Home\x\CLAUDE~1\settings.json",
    r"C:\Home\x\CLAUDE~1\skills\a.txt", r"C:\Home\x\CLAUDE~1.JSO", r"C:\Home\x\.claude\OUTPUT~1\a.md",
])
def test_the_wider_scope_is_config(hook, path):
    assert hook.is_config(path), path


@pytest.mark.parametrize("path", [
    str(HOME / ".claude" / "projects" / "x" / "memory" / "m.md"), str(HOME / ".claude-other" / "x.md"),
    r"D:\repo\claude.json", r"D:\repo\.claude.jsonl", r"D:\repo\mcp.json", r"D:\repo\x.mcp.json",
    str(HOME / ".claude" / "plugins-notes.txt"),
])
def test_the_wider_scope_is_not_sloppy(hook, path):
    assert not hook.is_config(path), path


@pytest.mark.parametrize("path", [str(HOME / ".claude.json"), r"D:\repo\.mcp.json", str(HOME / ".claude" / "plugins" / "a")])
def test_the_wider_scope_is_gated_for_the_edit_tools(env, path):
    env.set_mode("enforce")
    assert denied(edit_cfg(env, path))


# ------------------------------------------------------------------ an interactive claude handed a user-only command

FORGED = [
    "Start-Process claude -ArgumentList '/allow-config then do x'",
    'start cmd /c claude "/allow-config go"',
    "wt claude /allow-config",
    "claude /allow-publish",
    "claude -p '/allow-config'",
    'cmd /c claude.exe "/grant-rework 5"',
    r"~\.local\bin\claude.exe /ack-break x",
    "claude --resume abc",
]
NOT_FORGED = [
    "git commit -m 'document /allow-config'",
    "grep allow-config docs/v2/contracts.md",
    "echo /allow-publish",
    "claude --version",
    "cat hooks/skills/allow-config/SKILL.md",
]


@pytest.mark.parametrize("cmd", FORGED)
def test_forged_grant_spawn(cmd):
    assert atlas_hook.forged_grant(cmd), cmd


@pytest.mark.parametrize("cmd", NOT_FORGED)
def test_not_a_forged_grant(cmd):
    assert not atlas_hook.forged_grant(cmd), cmd


def test_a_spawned_claude_with_a_user_command_is_denied_end_to_end(env):
    out = env.bash('start cmd /c claude "/allow-config go"')
    assert denied(out)
    assert env.bash("git commit -m 'about /allow-config'") is None


# ------------------------------------------------------------------ scheduled tasks forwarding

@pytest.mark.parametrize("tool", ["mcp__scheduled-tasks__create_scheduled_task",
                                  "mcp__scheduled-tasks__update_scheduled_task", "RemoteTrigger"])
@pytest.mark.parametrize("cmd", ["/allow-config", "/allow-publish 3", "/grant-rework", "/ack-break x"])
def test_a_scheduled_task_prompt_with_a_user_command_is_refused(env, tool, cmd):
    out = env.run("PreToolUse", tool_name=tool, tool_input={"prompt": cmd})
    assert denied(out), (tool, cmd)


def test_a_scheduled_task_with_an_ordinary_prompt_passes(env):
    for tool in ("mcp__scheduled-tasks__create_scheduled_task", "RemoteTrigger"):
        assert env.run("PreToolUse", tool_name=tool, tool_input={"prompt": "check the build, see /allow-config docs"}) is None
