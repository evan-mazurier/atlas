"""Publish gate, review round 2: PowerShell blocks and assignments, the git dry-run cluster, CRLF line continuation, and
a refusal log that never writes the state cache."""
import json

import pytest

from test_atlas_hook import SID, env  # noqa: F401  (env is a fixture)
from test_publish_gate import classify, classify_all, denied

BLOCKS = [
    "$out = git push 2>&1", "$null = git push", "$x=git push", "$env:FOO = 'x'; git push", "$x += git push",
    "try { git push } catch { Write-Host failed }", "try { git push } catch {}", "if ($ok) { git push } else { git status }",
    "$remotes | ForEach-Object { git push $_ }", "1..2 | % { git push }", "Measure-Command { git push }",
    "function p { git push }; p", "switch ($x) { 'a' { git push } }", ". { git push }", "& { git push }",
    "Invoke-Command -ScriptBlock { git push } -ArgumentList x", "icm { git push } -ComputerName x",
    "foreach ($r in $remotes) { git push $r }", "{git push}", "% {git push}", "pwsh -Command { git push }",
    "$ok = $(git push)", "$r = (git push)", "$x = { git push }",
]

ORDINARY = [
    "try { git status } catch {}", "$x = git log -1", "1..3 | % { npm test }", "$null = git status", "$x=git status",
    "$remotes | ForEach-Object { git fetch $_ }", "function p { git status }; p", "Measure-Command { npm test }",
    "switch ($x) { 'a' { git pull } }", "$x = 1", "$x", "Write-Host {hi}", "echo ${HOME}", "echo {a,b}",
    "curl -d '{\"a\": 1}' http://localhost", "git push --dry-run $x", "$out = git push -n 2>&1",
    "git push -Name", "git push -Verbose",
]


@pytest.mark.parametrize("cmd", BLOCKS)
def test_publish_inside_blocks_and_after_assignments_is_found(cmd):
    assert classify(cmd) == "git push", cmd


@pytest.mark.parametrize("cmd", ORDINARY)
def test_ordinary_block_and_assignment_commands_still_pass(cmd):
    # `git push -Name` / `-Verbose` are not dry runs: they are pushes (see the cluster test); the rest do not publish
    if cmd in ("git push -Name", "git push -Verbose"):
        assert classify(cmd) == "git push"
    else:
        assert classify(cmd) is None, cmd


def test_a_block_with_two_publishes_counts_two():
    assert classify_all("try { git push; gh pr merge 3 } catch { }") == ["git push", "gh pr merge"]
    assert classify_all("icm { git push } -ArgumentList x") == ["git push"]


def test_blocks_are_denied_by_the_hook(env):
    env.set_mode("enforce")
    for cmd in ("try { git push } catch { }", "$null = git push", "% { git push }"):
        assert denied(env.run("PreToolUse", tool_name="PowerShell", tool_input={"command": cmd})), cmd


@pytest.mark.parametrize("flag", ["-n", "-nu", "-vn", "-fn", "-nv", "-un4"])
def test_a_real_short_flag_cluster_with_n_is_a_dry_run(flag):
    assert classify("git push " + flag + " origin main") is None
    assert classify("git push origin " + flag) is None


@pytest.mark.parametrize("flag", ["-Name", "-NoVerify", "-nope", "-N", "-Verbose", "-ReportNothing", "-n1", "-no", "-on"])
def test_a_powershell_parameter_is_never_a_dry_run(flag):
    assert classify("git push " + flag) == "git push"


def test_dry_run_long_flag_still_works_and_other_clusters_push():
    assert classify("git push --dry-run") is None
    assert classify("git push -fu origin x") == "git push"
    assert classify("git push -v") == "git push"


@pytest.mark.parametrize("text", ["git \\\r\n push", "git \\\n push", "git push \\\r\n origin", "git\\\r\n push"])
def test_backslash_crlf_continues_the_line(text):
    assert classify(text) == "git push"


def test_backslash_crlf_does_not_split_the_segment():
    import sys
    from test_publish_gate import HERE
    sys.path.insert(0, str(HERE))
    import atlas_hook
    segs, _d, _n = atlas_hook._shell_split("git \\\r\n push \\\r\n origin")
    assert segs == [["git", "push", "origin"]]
    segs, _d, _n = atlas_hook._shell_split("git \\\n push")
    assert segs == [["git", "push"]]


def test_the_refusal_never_writes_the_state_cache(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    sp = env.ledger / "state" / f"{SID}.json"
    before = json.loads(sp.read_text(encoding="utf-8"))
    sp.write_text(json.dumps(before, sort_keys=True), encoding="utf-8")
    stamp = sp.stat().st_mtime_ns
    assert denied(env.bash("git push"))
    assert sp.stat().st_mtime_ns == stamp                    # untouched
    assert json.loads(sp.read_text(encoding="utf-8")) == before
    assert [e["guard"] for e in env.events("refusal")][-1] == "publish"


def test_a_quoted_command_substitution_text_is_a_documented_false_positive():
    assert classify('echo "$(date) git push"') == "git push"          # contracts §6.4b: kept on purpose
    assert classify('echo "no substitution git push"') is None
