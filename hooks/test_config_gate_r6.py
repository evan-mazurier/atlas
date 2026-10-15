"""Round-6 follow-ups of the senior review (contracts §6.4, §6.4b, §6.4c): process substitution and PowerShell grouping
under a reader head, globs in nested config names under worktrees/ and projects/, a Python list launch, the arithmetic
look-back, an escaped `<`, publishes inside an expanded heredoc body, a backslash inside the word, heredocs to a file,
false positives on process and file tools, the fail direction, and the bare atlas-anchor after a cd. In-process checks."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_atlas_hook import SID, env  # noqa: E402,F401  (env is a fixture)
from test_config_gate_r3 import Stub, denied, shell  # noqa: E402
import atlas_hook  # noqa: E402


def launches(cmd):
    return bool(atlas_hook.claude_shell_launch(cmd) or atlas_hook.forged_grant_name(cmd) is not None)


def publishes(cmd):
    return atlas_hook.publish_families(Stub(), cmd)


# ------------------------------------------------------------------ 1. process substitution under a reader head

B1 = [
    "cat <(claude /allow-config)",
    'cat <(claude "/allow-config go")',
    "head <(claude /allow-config)",
    "wc -l <(claude /allow-config)",
    "stat <(claude x)",
    "cat < <(claude /allow-config)",
    "which claude <(claude /allow-config)",
    "echo <(claude /allow-config)",
    "grep x <(claude /allow-config)",
    "echo hi > >(claude /allow-config)",
]


@pytest.mark.parametrize("cmd", B1)
def test_process_substitution_runs_under_any_head(cmd):
    assert launches(cmd), cmd


# ------------------------------------------------------------------ 2. PowerShell grouping

B2 = [
    "cat (claude /allow-config)",
    "gc (claude /allow-config)",
    "Get-Content (claude /allow-config)",
    "ls (claude x)",
    "which (claude x)",
    "echo (claude /allow-config)",
    "Write-Output (claude x)",
    "Write-Host @(claude x)",
    "git log (claude x)",
    "grep x (claude x)",
]


@pytest.mark.parametrize("cmd", B2)
def test_a_parenthesised_argument_runs_in_powershell(cmd):
    assert launches(cmd), cmd


@pytest.mark.parametrize("cmd", [
    'git commit -m "Hooks (claude launches refused)"',
    "git commit -m 'Hooks (claude launches refused)'",
    'git commit -m "Hooks (claude /allow-config refused)"',
    "echo 'see (claude /allow-config)'",
])
def test_a_paren_inside_quotes_is_text(cmd):
    assert not launches(cmd), cmd


# ------------------------------------------------------------------ 3. globs in a nested config name

B3_GATED = [
    "cp e D:/AI/Atlas/.claude/worktrees/w/.cl?ude/settings.json",
    "cp e D:/AI/Atlas/.claude/worktrees/w/.cl*/settings.json",
    "cp e ~/.claude/worktrees/w/CL?UDE.md",
    "cp e ~/.claude/worktrees/w/[C]LAUDE.md",
    "cp e ~/.claude/worktrees/w/.mc?.json",
    "cp e ~/.claude/projects/p/.cl?ude/skills/x.md",
    r"cp e ~/.claude/worktrees/w/.c\laude/settings.json",
]
B3_FREE = [
    "rm -rf ~/.claude/worktrees/*",
    "rm -rf D:/AI/Atlas/.claude/worktrees/agent-*",
    "rm ~/.claude/projects/*",
    "rm -rf ~/.claude/projects/*/memory/tmp",
    "echo x > ~/.claude/projects/*/memory/note.md",
]


@pytest.mark.parametrize("cmd", B3_GATED)
def test_a_glob_in_a_nested_config_name_is_gated(cmd):
    assert shell(cmd), cmd


@pytest.mark.parametrize("cmd", B3_FREE)
def test_worktree_and_project_cleanup_stays_free(cmd):
    assert not shell(cmd), cmd


# ------------------------------------------------------------------ 4. a Python list launch

@pytest.mark.parametrize("cmd", [
    "python -c \"import subprocess; subprocess.run(['claude','/allow-config'])\"",
    "python -c \"import subprocess; subprocess.run(['claude', '/allow-config'])\"",
    "python -c \"import subprocess; subprocess.run(['claude'])\"",
])
def test_a_python_list_launch_is_refused(cmd):
    assert launches(cmd), cmd


# ------------------------------------------------------------------ 5. arithmetic look-back padding

def test_padding_cannot_push_an_arithmetic_opener_out_of_sight():
    pad = "echo $((1 +" + " " * 2100 + "1<<2))\n"
    assert launches(pad + "claude /allow-config\n2")
    assert publishes(pad + "git push origin main\n2")
    # the arithmetic itself is still not a heredoc
    assert not launches("echo $((1<<2))\nls")
    assert not publishes("echo $((1<<2))\nls")


def test_arithmetic_closed_before_a_heredoc_is_a_heredoc():
    assert not launches("echo $((1))\ncat <<'EOF'\n/allow-config claude\nEOF")


# ------------------------------------------------------------------ 6. an escaped < is not a heredoc

def test_an_escaped_less_than_starts_no_heredoc():
    assert launches("echo a\\<<EOF\nclaude /allow-config\nEOF")
    assert publishes("echo a\\<<EOF\ngit push origin main\nEOF")


def test_an_escaped_backslash_before_less_than_is_still_a_heredoc():
    assert not launches("cat a\\\\<<'EOF'\nclaude /allow-config\nEOF")


# ------------------------------------------------------------------ 7. publishes in an expanded heredoc body

def test_a_publish_in_an_unquoted_heredoc_substitution_is_seen():
    assert publishes("cat <<EOF\n$(git push origin main)\nEOF")
    assert publishes("cat <<EOF\n`git push origin main`\nEOF")
    assert publishes("cat <<EOF\nnote $(git push origin main) end\nEOF")


def test_a_quoted_or_plain_heredoc_body_is_text_for_the_publish_gate():
    assert not publishes("cat <<'EOF'\n$(git push origin main)\nEOF")
    assert not publishes("cat <<EOF\ngit push origin main\nEOF")


# ------------------------------------------------------------------ 8. a backslash inside the word

def test_a_backslash_inside_the_word_is_dropped_by_the_shell():
    assert launches("c\\laude /allow-config")
    assert launches("cl\\aude")
    assert launches("c\\l\\a\\u\\d\\e hello")


# ------------------------------------------------------------------ 9. heredoc to a file by any redirect or writer

@pytest.mark.parametrize("cmd", [
    "cat <<'EOF' 1>s.sh\nclaude /allow-config\nEOF",
    "cat <<'EOF' 1>>s.sh\nclaude /allow-config\nEOF",
    "cat <<'EOF' > s.sh\nclaude /allow-config\nEOF",
    "cat <<'EOF' | dd of=s.sh\nclaude /allow-config\nEOF",
    "cat <<'EOF' | Out-File s.ps1\nclaude /allow-config\nEOF",
    "cat <<'EOF' | Set-Content s.ps1\nclaude /allow-config\nEOF",
    "cat <<'EOF' | Add-Content s.ps1\nclaude /allow-config\nEOF",
    "cat <<'EOF' | sponge s.sh\nclaude /allow-config\nEOF",
    "cat <<'EOF' | tee s.sh\nclaude /allow-config\nEOF",
])
def test_a_heredoc_written_to_a_file_keeps_its_body_scanned(cmd):
    assert launches(cmd), cmd


def test_a_stderr_redirect_does_not_keep_a_data_body():
    assert not launches("git commit -F - <<'EOF' 2>&1\nHooks: claude /allow-config\nEOF")


# ------------------------------------------------------------------ 10. false positives

FP = [
    "Get-Process claude", "gps claude", "Stop-Process -Name claude", "taskkill /im claude.exe",
    'tasklist /fi "imagename eq claude.exe"',
    "(Get-Command claude).Source", r"Test-Path ~\.local\bin\claude.exe",
    "npm view @anthropic-ai/claude-code version", "npm ls @anthropic-ai/claude-code",
    "gh pr list --search claude", "pip show claude",
    "cd claude", "mkdir claude", "rm -rf claude", "mv x claude", "find . -name claude",
    'py -3.12 tools/find_plan_item.py "claude team"',
    "ps | grep claude",
]


@pytest.mark.parametrize("cmd", FP)
def test_naming_claude_as_a_process_or_file_is_not_a_launch(cmd):
    assert not launches(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "find . -exec claude {} ;",
    "find . -name x -execdir claude ;",
    "find . -ok claude {} ;",
    "py -3.12 -c \"import os; os.system('claude x')\"",
    "py -3.12 -c \"print('claude')\"",
    "pytest -k claude",
    "npm exec claude",
    "Get-Process claude (claude x)",
    "rm -rf $(claude x)",
    "cd <(claude x)",
])
def test_the_exceptions_still_launch(cmd):
    assert launches(cmd), cmd


# ------------------------------------------------------------------ 11. fail direction

class Ctx(Stub):
    agent_id = None
    tool = "Bash"
    sid = SID
    cfg = {}

    def __init__(self, cmd):
        self.ti = {"command": cmd}


def _raise(*_a, **_k):
    raise RuntimeError("boom")


@pytest.mark.parametrize("fn", ["forged_grant_name", "claude_shell_launch"])
def test_an_internal_error_in_the_launch_check_denies_a_claude_mention(monkeypatch, fn):
    monkeypatch.setattr(atlas_hook, fn, _raise)
    monkeypatch.setattr(atlas_hook, "log_error", lambda: None)
    out = atlas_hook.h_pre_bash(Ctx("claude /allow-config"))
    assert denied(out)


@pytest.mark.parametrize("fn", ["forged_grant_name", "claude_shell_launch"])
def test_an_internal_error_in_the_launch_check_allows_other_commands(monkeypatch, fn):
    monkeypatch.setattr(atlas_hook, fn, _raise)
    monkeypatch.setattr(atlas_hook, "log_error", lambda: None)
    assert atlas_hook.h_pre_bash(Ctx("ls -la")) is None


def test_a_failing_log_never_turns_the_deny_into_an_allow(monkeypatch):
    monkeypatch.setattr(atlas_hook, "forged_grant_name", _raise)
    monkeypatch.setattr(atlas_hook, "log_error", _raise)
    assert denied(atlas_hook.h_pre_bash(Ctx("echo hi | claude")))


# ------------------------------------------------------------------ 13. a bare atlas-anchor after a cd

@pytest.mark.parametrize("cmd", [
    "cd atlas-anchor", "pushd atlas-anchor", "Set-Location atlas-anchor", "git -C atlas-anchor log",
    "cd atlas-anchor && git push",
])
def test_moving_into_the_anchor_clone_by_a_bare_name_is_a_bypass(cmd):
    H = Stub()
    H.cwd = "D:/AI"
    assert atlas_hook.ledger_bypass(H, cmd), cmd


@pytest.mark.parametrize("cmd", ["cd atlas-anchor-notes", "echo atlas-anchor", "git -C atlas-anchors log"])
def test_a_bare_anchor_mention_that_is_not_a_cd_is_text(cmd):
    H = Stub()
    H.cwd = "D:/AI"
    assert not atlas_hook.ledger_bypass(H, cmd), cmd


# ------------------------------------------------------------------ final review: an open publish allowance skips nothing

CL, AC = "cla" + "ude", "/allow-" + "config"


@pytest.mark.parametrize("cmd", ["git push origin main && " + CL + " " + AC, "git push origin main && " + CL])
def test_an_open_publish_allowance_does_not_skip_the_claude_checks(env, cmd):
    env.set_mode("enforce")
    assert env.run("UserPromptSubmit", prompt="/allow-" + "publish 2") is None
    assert denied(env.bash(cmd)), cmd


def test_a_publish_with_an_open_allowance_still_passes(env):
    env.set_mode("enforce")
    assert env.run("UserPromptSubmit", prompt="/allow-" + "publish") is None
    assert not denied(env.bash("git push origin main"))



# ------------------------------------------------------------------ final review 2: a newline inside quotes is not a new command

ATTR = "Co-Authored-By: " + "Clau" + "de Opus 5.5 <noreply@anthropic.com>"
MULTI_OK = [
    'git commit -m "Hooks: fix\n\n' + ATTR + '"',
    "git commit -m 'Hooks: fix\n\n" + ATTR + "'",
    'git commit -m "x\nfix the ' + CL + ' hook"',
    'echo "a\n' + CL + ' b"',
    'git commit -m "x # not a comment\n' + CL + ' y"',
]
MULTI_BAD = [
    'bash -c "x\n' + CL + ' ' + AC + '"',
    'bash -c "x\n' + CL + '"',
    'echo hi # don\'t\n' + CL + ' ' + AC,
    'echo "x\n' + CL,                       # unclosed quote: every line still read as a command
    'git commit -m "x"\n' + CL + ' ' + AC,
]


@pytest.mark.parametrize("cmd", MULTI_OK)
def test_a_newline_inside_quotes_does_not_start_a_command(cmd):
    assert not launches(cmd), cmd


@pytest.mark.parametrize("cmd", MULTI_BAD)
def test_a_real_launch_across_lines_is_still_refused(cmd):
    assert launches(cmd), cmd


def test_a_multi_line_attribution_commit_and_push_pass_with_an_allowance(env):
    env.set_mode("enforce")
    assert env.run("UserPromptSubmit", prompt="/allow-" + "publish") is None
    assert not denied(env.bash(MULTI_OK[0] + " && git push origin main"))


# ------------------------------------------------------------------ final review 3: escapes before a quote turn the joining off

ESC_BAD = [
    'echo "a\\\\"\n' + CL + ' ' + AC + '\n#"',
    "echo $'it\\'s'\n" + CL + " " + AC + "\n# '",
    "echo \\\\'x\necho '\n" + CL + " " + AC + "\n# '",
    'Get-ChildItem "D:\\AI\\"\n' + CL + ' ' + AC + '\n#"',
    'ls "a\\\\"\n' + CL + ' < prompt.txt\n#"',
]


@pytest.mark.parametrize("cmd", ESC_BAD)
def test_a_backslash_before_a_quote_keeps_every_line_a_command(cmd):
    assert launches(cmd), cmd
