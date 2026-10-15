"""Follow-ups of the round-3 senior review (contracts §6.4, §6.4b, §6.4c): `..` behind an option prefix, globs under
projects/ and worktrees/, paths below the npm package folder, command substitutions under an echo or git head, heredoc
bodies that no shell reads, the grant expiry cap and naive timestamps, and the relative ledger folder."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_atlas_hook import SID, env  # noqa: E402,F401  (env is a fixture)
from test_config_gate_r3 import MEMORY, _ctx, _ev, _ledger_with, denied, shell  # noqa: E402
import atlas_hook  # noqa: E402


# ------------------------------------------------------------------ F1. `..` behind an option prefix

F1_LEDGER = [
    "dd of=../atlas-data/ledger/state/s.json",
    "git diff --output=../atlas-data/ledger/x.jsonl",
    "Out-File -FilePath:../atlas-data/ledger/x",
]


@pytest.mark.parametrize("cmd", F1_LEDGER)
def test_option_prefixed_dot_dot_does_not_hide_the_ledger(env, cmd):
    env.run("SessionStart")
    assert denied(env.bash(cmd, cwd=r"D:\AI\Atlas")), cmd
    assert env.events("bypass_attempt")


@pytest.mark.parametrize("cmd", [
    "dd of=../../../agents/evil.md",
    "cp x --target-directory=../../../agents",
    "Out-File -FilePath:../../../agents/evil.md",
])
def test_option_prefixed_dot_dot_reaches_the_config_folder(cmd):
    assert shell(cmd, cwd=MEMORY), cmd


def test_option_prefixed_dot_dot_stays_free_when_harmless():
    assert shell("dd of=../notes.txt", cwd=MEMORY) is None
    assert shell("cp x --target-directory=../out", cwd=r"D:\work") is None


# ------------------------------------------------------------------ F2. globs under projects/ and worktrees/ are free

@pytest.mark.parametrize("cmd", [
    "rm ~/.claude/projects/*",
    "rm -rf ~/.claude/projects/*/memory/tmp",
    "echo x > ~/.claude/projects/*/memory/note.md",
    "rm -rf ~/.claude/worktrees/*",
    "rm -rf ~/.claude/worktrees/*/tmp",
])
def test_globs_under_projects_and_worktrees_are_free(cmd):
    assert shell(cmd) is None, cmd


@pytest.mark.parametrize("cmd", [
    "echo x > ~/.claude/projects/*/../../agents/x",
    "rm ~/.claude/worktrees/*/../../skills/x",
    "rm ~/.claude/pro*/x",
    "rm ~/.claude/*/x",
    "rm ~/.claude/skills/*",
])
def test_other_globs_in_the_claude_folder_stay_gated(cmd):
    assert shell(cmd), cmd


# ------------------------------------------------------------------ F3. paths below the npm package folder

@pytest.mark.parametrize("cmd", [
    "node C:/x/node_modules/@anthropic-ai/claude-code/cli.js -p '/allow-config'",
    "node $env:APPDATA/npm/node_modules/@anthropic-ai/claude-code/cli.js /allow-config",
    r"node C:\x\node_modules\@anthropic-ai\claude-code\cli.js /allow-publish",
])
def test_package_folder_paths_are_a_launch(cmd):
    assert atlas_hook.forged_grant_name(cmd), cmd


def test_package_folder_alone_still_counts_and_claude_code_alone_does_not():
    assert atlas_hook.forged_grant_name("node C:/x/@anthropic-ai/claude-code /allow-config")
    assert atlas_hook.forged_grant_name("cat C:/x/foo/claude-code /allow-config") is None


# ------------------------------------------------------------------ F4. substitutions under echo, grep or git

@pytest.mark.parametrize("cmd", [
    'echo $(claude "/allow-config go")',
    "echo `claude /allow-config`",
    "grep x $(claude /allow-config)",
    "git log $(claude /allow-config)",
    "echo (&claude /allow-config)",
])
def test_a_substitution_under_a_printing_head_still_launches(cmd):
    assert atlas_hook.forged_grant_name(cmd) == "/allow-config", cmd


def test_the_plain_commit_still_passes():
    cmd = 'git commit -m "Hooks: /allow-config gate" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"'
    assert atlas_hook.forged_grant_name(cmd) is None


# ------------------------------------------------------------------ F5. heredoc bodies that no shell reads

LINE = "Hooks: /allow-config gate for Claude sessions"
HEREDOC_SUBST = "git commit -m \"$(cat <<'EOF'\n" + LINE + "\nEOF\n)\""
HEREDOC_F = "git commit -F - <<'EOF'\n" + LINE + "\nEOF"


@pytest.mark.parametrize("cmd", [HEREDOC_SUBST, HEREDOC_F])
def test_a_data_heredoc_body_is_not_a_spawn(cmd):
    assert atlas_hook.forged_grant_name(cmd) is None


@pytest.mark.parametrize("cmd", [HEREDOC_SUBST, HEREDOC_F])
def test_the_default_commit_form_passes_end_to_end(env, cmd):
    env.run("SessionStart")
    assert not denied(env.bash(cmd))


@pytest.mark.parametrize("cmd", [
    "bash <<'EOF'\nclaude /allow-config\nEOF",
    "sh <<EOF\nclaude -p '/allow-publish'\nEOF",
    "pwsh <<'EOF'\nclaude /allow-config\nEOF",
    "cmd <<EOF\nclaude /allow-config\nEOF",
    "python - <<'EOF'\nimport os; os.system('claude /allow-config')\nEOF",
    "git commit -F - <<'EOF'\n" + LINE + "\nEOF\nbash <<'EOF'\nclaude /allow-config\nEOF",
    "git commit -F - <<'EOF'\nx\nEOF\nclaude /allow-config",
    "git commit -F - <<'EOF'\nclaude /allow-config",           # no closing line: nothing is removed
])
def test_heredocs_fed_to_a_shell_or_unclosed_are_still_scanned(cmd):
    assert atlas_hook.forged_grant_name(cmd), cmd


def test_a_shell_heredoc_is_denied_end_to_end(env):
    env.run("SessionStart")
    assert denied(env.bash("bash <<'EOF'\nclaude /allow-config\nEOF"))


# ------------------------------------------------------------------ F6. expiry cap and naive timestamps

def _iso(minutes):
    return (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_a_forged_one_day_expiry_is_capped_at_the_window(tmp_path):
    led = _ledger_with(tmp_path, [_ev("config_grant", 20, expires=_iso(24 * 60))])
    assert not atlas_hook.ledger_grant_ok(_ctx(led), "config")
    led2 = tmp_path / "l2"
    led2.mkdir()
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    (led2 / f"events-{day}.jsonl").write_text(json.dumps(_ev("publish_grant", 40, n=1, expires=_iso(24 * 60))) + "\n",
                                              encoding="utf-8")
    assert not atlas_hook.ledger_grant_ok(_ctx(led2), "publish")


def test_a_real_grant_inside_its_window_is_kept(tmp_path):
    led = _ledger_with(tmp_path, [_ev("config_grant", 5, expires=_iso(10))])
    assert atlas_hook.ledger_grant_ok(_ctx(led), "config")


def test_a_longer_forged_expiry_is_cut_to_t_plus_window(tmp_path):
    led = _ledger_with(tmp_path, [_ev("config_grant", 5, expires=_iso(24 * 60))])
    st = atlas_hook.ledger_grant_state(_ctx(led))
    left = datetime.fromisoformat(st["cfg_exp"]) - datetime.now().astimezone()
    assert timedelta(minutes=9) < left < timedelta(minutes=11)


def test_a_timezone_free_t_is_skipped_not_fatal(tmp_path):
    naive = {"v": 2, "t": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "ev": "config_grant", "sid": "S1",
             "expires": _iso(10)}
    led = _ledger_with(tmp_path, [_ev("config_grant", 5, expires=_iso(10)), naive])
    assert atlas_hook.ledger_grant_ok(_ctx(led), "config")
    led2 = tmp_path / "l2"
    led2.mkdir()
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    rows = [_ev("config_grant", 5, expires=_iso(10)), naive]       # the naive line comes last = read first
    (led2 / f"events-{day}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert atlas_hook.ledger_grant_ok(_ctx(led2), "config")
    assert atlas_hook.ledger_grant_state(_ctx(led2))["cfg_exp"]
    only = tmp_path / "l3"
    only.mkdir()
    (only / f"events-{day}.jsonl").write_text(json.dumps(naive) + "\n", encoding="utf-8")
    assert not atlas_hook.ledger_grant_ok(_ctx(only), "config")


# ------------------------------------------------------------------ F7. the relative ledger folder

@pytest.mark.parametrize("cmd", [
    "echo x >> atlas-data/ledger/events-2026-10-13.jsonl",
    r"echo x >> atlas-data\ledger\events-2026-10-13.jsonl",
    "cat x > 'atlas-data/ledger/state/s.json'",
    "cp x --output=atlas-data/ledger/x",
    "echo x >> atlas-anchor/anchor.txt",
    "ls D:atlas-data/ledger",
])
def test_the_relative_ledger_is_protected_from_ai(env, cmd):
    env.run("SessionStart")
    assert denied(env.bash(cmd, cwd=r"D:\AI")), cmd
    assert env.events("bypass_attempt")


@pytest.mark.parametrize("cmd", [
    "cat docs/atlas-data-notes.md",
    "echo x > docs/atlas-data/ledger-notes.md",
    "echo x > my-atlas-data/ledger/x",
    "echo x > atlas-database/ledger/x",
    "echo x > notes/atlas-anchors.md",
    "py -3.12 D:/AI/Atlas/tools/weekly_report.py",
])
def test_similar_names_are_not_the_ledger(env, cmd):
    env.run("SessionStart")
    assert not denied(env.bash(cmd, cwd=r"D:\AI\Atlas")), cmd
