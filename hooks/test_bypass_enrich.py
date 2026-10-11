"""bypass_attempt enrichment (contracts §1 exception): actor, desc, masked cmd, effect. The deny never depends on it,
and a crafted command must never make the hook slow (a timed-out PreToolUse hook does not block)."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atlas_hook as ah  # noqa: E402
from test_atlas_hook import Env  # noqa: E402

L = "D:\\AI\\atlas-data\\ledger"
LF = "D:/AI/atlas-data/ledger"
STUB = SimpleNamespace(ledger="")

CASES = [
    # read: every segment has a read head and no change signal
    (f"cat {L}\\events-2026-10-10.jsonl", "read"),
    (f"type {L}\\events-2026-10-10.jsonl", "read"),
    (f"grep x {L}\\f.jsonl > out.txt", "read"),
    (f"grep -c bypass {LF}/f.jsonl | wc -l", "read"),
    (f"cat {LF}/f.jsonl 2>&1 | head -5", "read"),
    ("tail -n 5 /mnt/d/AI/atlas-data/ledger/f.jsonl", "read"),
    (f"Get-Content {L}\\f.jsonl | Select-String bypass", "read"),
    (f"Get-ChildItem {L}", "read"),
    ("ls ~/.claude/hooks/atlas/", "read"),
    (f"cp {L}\\f.jsonl /tmp/f", "read"),
    (f"find {LF} -name '*.jsonl'", "read"),
    # unclear: never claim read when something downstream or inside is opaque
    (f"sed -n s/a/b/p {LF}/f.jsonl", "unclear"),
    (f"sed '1e rm -rf {LF}' /tmp/f", "unclear"),
    (f"rg --pre ./wipe.sh x {LF}", "unclear"),
    (f"git grep -Orm x -- {LF}", "unclear"),
    (f"ls -o {LF}", "unclear"),
    # a read tool that writes a file through an output flag
    (f"sed -n 's/a/b/w {LF}/e.jsonl' /tmp/f", "change"),
    (f"sed -n 'w {LF}/e.jsonl' /tmp/f", "change"),
    (f"git log --output={LF}/e.jsonl", "change"),
    (f"git diff --output {LF}/e.jsonl", "change"),
    (f"less -o {LF}/e.jsonl /tmp/x", "change"),
    (f"less --log-file={LF}/x /tmp/x", "change"),
    (f"grep -o x {LF}/f.jsonl", "read"),
    (f"Get-Content {L}\\f.jsonl | Set-Content out.txt", "unclear"),
    (f"python -c \"print(open(r'{L}\\f.jsonl').read())\"", "unclear"),
    (f"cd {L}", "unclear"),
    (f"somecmd {L}", "unclear"),
    (f"cat $(echo {L}\\f.jsonl)", "unclear"),
    (f"cat {LF}/f.jsonl | python -c \"import sys\"", "unclear"),
    (f"cat {LF}/f.jsonl | sh", "unclear"),
    (f"bash -c \"cat {LF}/f.jsonl\"", "unclear"),
    (f"find {LF} -exec cat {{}} ;", "unclear"),
    # change
    (f"echo x >> {LF}/f.jsonl", "change"),
    (f"echo x > {L}\\f.jsonl", "change"),
    (f"cat /tmp/evil>{LF}/events-x.jsonl", "change"),
    (f"type nul>{L}\\x", "change"),
    (f"echo x >| {LF}/f.jsonl", "change"),
    (f"echo x 1> {LF}/f.jsonl", "change"),
    (f"sed -i s/a/b/ {LF}/f.jsonl", "change"),
    (f"sed -ie s/a/b/ {LF}/e", "change"),
    (f"sed --in-place s/a/b/ {LF}/e", "change"),
    (f"sort -o {LF}/e {LF}/e", "change"),
    (f"uniq {LF}/e {LF}/out", "change"),
    (f"tree -o {LF}/e {LF}", "change"),
    (f"find /tmp -fprint {LF}/e", "change"),
    (f"mv {L}\\f.jsonl /tmp/f", "change"),
    (f"cp /tmp/f {L}\\f.jsonl", "change"),
    (f"cp -t {LF} /tmp/x", "change"),
    (f"cp --target-directory={LF} /tmp/x", "change"),
    (f"Copy-Item -Destination {L} -Path x", "change"),
    (f"xcopy src {L} /Y", "change"),
    (f"Set-Content -Path {L}\\f.jsonl -Value x", "change"),
    (f"Add-Content {L}\\f.jsonl x", "change"),
    (f"echo x | tee {L}\\f.jsonl", "change"),
    (f"Out-File -FilePath {L}\\f.jsonl", "change"),
    (f"python -c \"open('{LF}/f.jsonl','w').write('')\"", "change"),
    (f"python -c \"f=open('{LF}/e', mode='w')\"", "change"),
    (f"node -e \"require('fs').writeFileSync('{LF}/f.jsonl','')\"", "change"),
    ("git checkout -- ~/.claude/hooks/atlas/atlas_hook.py", "change"),
    ("git restore ~/.claude/hooks/atlas/atlas_config.json", "change"),
    (f"truncate -s 0 {LF}/f.jsonl", "change"),
    ("chmod 777 ~/.claude/hooks/atlas/atlas_hook.py", "change"),
    ("attrib -r ~/.claude/hooks/atlas/atlas_hook.py", "change"),
    ("icacls ~/.claude/hooks/atlas/atlas_hook.py /grant x:F", "change"),
    (f"cat {L}\\f.jsonl; echo x >> {L}\\f.jsonl", "change"),
    # delete
    (f"rm {L}\\events-2026-10-10.jsonl", "delete"),
    (f"del {L}\\f.jsonl", "delete"),
    (f"Remove-Item -Recurse {L}", "delete"),
    ("rmdir /s D:\\AI\\atlas-data\\ledger", "delete"),
    ("git rm ~/.claude/hooks/atlas/atlas_hook.py", "delete"),
    (f"ls {L} | xargs rm", "delete"),
    (f"find {LF} -name '*.jsonl' | xargs rm -f", "delete"),
    (f"find {LF} -delete", "delete"),
    (f"Get-ChildItem {L} | Remove-Item", "delete"),
    (f"gci {L} -Recurse | ri -Force", "delete"),
    (f"robocopy C:/tmp/empty {L} /MIR", "delete"),
    (f"awk 'BEGIN{{system(\"rm {LF}/e\")}}'", "delete"),
    (f"python -c \"print(__import__('os').system('rm -rf {LF}'))\"", "delete"),
    (f"python -c \"import os; os.unlink('{LF}/f.jsonl')\"", "delete"),
]


@pytest.mark.parametrize("cmd,expect", CASES)
def test_effect_table(cmd, expect):
    assert ah.classify_effect(STUB, cmd) == expect


def test_over_long_command_is_unclear_without_classifying():
    assert ah.classify_effect(STUB, "cat " + LF + "/f " + "x " * 3000) == "unclear"


WORST = [
    "perl - " * 585, "sed " * 1000, "open(" * 800, "open(a" + "," * 4000, "/./" * 1300, "x=y " * 1000,
    "a;" * 2000, "> " * 2000, "-p" * 2000, "a://" * 1000, "A" * 4000, "-i " * 1300, "(" * 4000, "\"" * 4000,
    "find " + "-exec " * 600, ("cat " + LF + "/f | ") * 150,
]


@pytest.mark.parametrize("cmd", WORST)
def test_worst_cases_stay_fast(cmd):
    cmd = cmd[:4096]
    t0 = time.perf_counter()
    ah.classify_effect(STUB, cmd)
    assert time.perf_counter() - t0 < 0.05
    t0 = time.perf_counter()
    ah.mask_secrets(cmd)
    assert time.perf_counter() - t0 < 0.05


def test_secrets_are_masked():
    m = ah.mask_secrets
    secrets = [
        "sk-abcdefghijklmnop", "ghp_abcdefghijklmnopqrstuvwxyz0123", "hunter2", "xoxb-1234567890-abcdefgh",
        "glpat-abcdefghijklmnopqrst", "npm_abcdefghijklmnop", "hf_abcdefghijklmnop", "github_pat_abcdefghijklmnop",
        "AKIAABCDEFGHIJKLMNOP", "abc.def.ghi", "s3cr3tvalue",
    ]
    cmds = [
        "curl -H x sk-abcdefghijklmnop", "git push https://ghp_abcdefghijklmnopqrstuvwxyz0123@github.com",
        "password=hunter2 ls", "TOKEN: hunter2", "login --password hunter2", "X_PASSWORD=hunter2 run",
        "PASSWORD=hunter2 run", "X_TOKEN=hunter2 run", "A_SECRET=hunter2 run", "MY_KEY=hunter2 run",
        "API_KEY=hunter2 run", "$env:X_KEY='hunter2'; run", "git clone https://bob:hunter2@host.example/r.git",
        "mysql -phunter2 db", "sshpass -p hunter2 ssh h", "export S=xoxb-1234567890-abcdefgh",
        "x glpat-abcdefghijklmnopqrst", "x npm_abcdefghijklmnop", "x hf_abcdefghijklmnop",
        "x github_pat_abcdefghijklmnop", "x AKIAABCDEFGHIJKLMNOP", 'curl -H "Authorization: Bearer abc.def.ghi" x',
        "curl -H Bearer s3cr3tvalue",
        "curl -u bob:hunter2 https://h", "set DB_PASSWORD hunter2", "export API_KEY hunter2",
        "docker run -e MY_TOKEN hunter2 img", "Authorization=Basic hunter2",
    ]
    for c in cmds:
        out = m(c)
        assert "***" in out, c
        for s in secrets:
            assert s not in out, (c, out)
    assert m("password=hunter2 ls") == "password=*** ls"
    assert m("echo 0123456789abcdef0123456789abcdef") == "echo ***"
    assert m("echo QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Njc4") == "echo ***"
    assert m("echo QWxhZGRpbjpvcGVu/c2VzYW1lMTIzNDU2Nzg5MA==") == "echo ***"
    path = "cat D:/AI/atlas-data/ledger/events-2026-10-10.jsonl"
    assert m(path) == path


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def attempts(env):
    return env.events("bypass_attempt")


def test_main_actor_desc_clipped_command_never_stored(env):
    cmd = "cat D:/AI/atlas-data/ledger/events-2026-10-09.jsonl  # token=SECRETVALUE " + "x" * 300
    out = env.run("PreToolUse", tool_name="Bash", tool_input={"command": cmd, "description": "word " * 80})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    e = attempts(env)[0]
    assert e["actor"] == "main" and "agent_id" not in e
    assert len(e["desc"]) == 160
    assert "cmd" not in e and e["target"] == "ledger-day:2026-10-09"
    assert "SECRETVALUE" not in str(env.events())
    assert e["effect"] in ("read", "unclear")


def test_desc_secrets_are_masked_before_storing(env):
    env.run("PreToolUse", tool_name="Bash", tool_input={
        "command": "cat D:/AI/atlas-data/ledger/f", "description": "check with password=hunter2 and sk-abcdefghijklmnop"})
    d = attempts(env)[0]["desc"]
    assert "hunter2" not in d and "sk-abcdefghij" not in d


@pytest.mark.parametrize("cmd", [
    "grep -n x tools/ledger_note.py", "git diff -- tools/ledger_note.py", "cat tools/ledger_note.py",
    "echo ledger_note.py", "py -3.12 tools/test_ledger_note.py",
])
def test_helper_merely_mentioning_the_note_tool_is_not_denied(env, cmd):
    assert env.run("PreToolUse", tool_name="Bash", agent_id="ag9", agent_type="implementer", tool_input={"command": cmd}) is None


@pytest.mark.parametrize("cmd", [
    "py -3.12 tools/ledger_note.py --at x --sid y --text z", "python tools/ledger_note.py --at x",
    "cd tools && python ledger_note.py --at x", "LEDGER=1 python D:/AI/Atlas/tools/ledger_note.py --at x",
    "bash -c \"python tools/ledger_note.py --at x\"", "echo hi; py tools/ledger_note.py --at x",
])
def test_helper_running_the_note_tool_is_denied(env, cmd):
    out = env.run("PreToolUse", tool_name="Bash", agent_id="ag9", agent_type="implementer", tool_input={"command": cmd})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert attempts(env)[-1]["matched"] == "ledger note by a helper"


def test_helper_actor_and_task_label(env):
    env.agent("implementer", "tuA", label="build the card")
    env.run("SubagentStart", agent_id="ag1", agent_type="implementer")
    out = env.run("PreToolUse", tool_name="Bash", agent_id="ag1", agent_type="implementer",
                  tool_input={"command": "echo x >> D:/AI/atlas-data/ledger/f.jsonl", "description": "append"})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    e = attempts(env)[0]
    assert e["actor"] == "implementer" and e["agent_id"] == "ag1"
    assert e["actor_task"] == "build the card"
    assert e["effect"] == "change" and e["desc"] == "append"


def test_enrichment_failure_still_denies_and_emits_minimal_event(monkeypatch):
    monkeypatch.setattr(ah, "bypass_extra", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    fake = SimpleNamespace(ti={"command": "cat D:/AI/atlas-data/ledger/f"}, tool="Bash", cwd="", ledger="", sid="s",
                           agent_id=None, agent_type=None)
    monkeypatch.setattr(ah, "ledger_bypass", lambda H, c: "ai\\atlas-data\\ledger")
    emitted = []

    class FakeTx:
        state = {}

        def __init__(self, H): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def emit(self, e): emitted.append(e)

    monkeypatch.setattr(ah, "Txn", FakeTx)
    out = ah.h_pre_bash(fake)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert emitted and emitted[0]["ev"] == "bypass_attempt" and emitted[0]["denied"] is True
    assert "effect" not in emitted[0] and "cmd" not in emitted[0]


def test_huge_crafted_command_is_denied_fast_by_the_real_hook(env):
    cmd = "rm -rf D:/AI/atlas-data/ledger # " + "perl - " * 15000
    assert len(cmd) > 100_000
    t0 = time.perf_counter()
    out = env.run("PreToolUse", tool_name="Bash", tool_input={"command": cmd})
    dt_s = time.perf_counter() - t0
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert dt_s < 1.0, dt_s
    e = attempts(env)[0]
    assert e["effect"] == "unclear" and "cmd" not in e


def test_ledger_note_tool_is_for_the_main_session_only(env):
    cmd = "py -3.12 tools/ledger_note.py --at x --sid y --text z"
    assert env.run("PreToolUse", tool_name="Bash", tool_input={"command": cmd}) is None
    out = env.run("PreToolUse", tool_name="Bash", agent_id="ag9", agent_type="implementer", tool_input={"command": cmd})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    e = attempts(env)[-1]
    assert e["matched"] == "ledger note by a helper" and e["actor"] == "implementer"


def test_chain_stays_intact_with_enriched_events(env):
    sys.path.insert(0, str(HERE.parent / "cockpit"))
    from ledger2 import store
    env.run("SessionStart", source="startup")
    env.run("PreToolUse", tool_name="Bash", tool_input={"command": "cat D:/AI/atlas-data/ledger/f"})
    assert store.verify_chain(env.ledger)["intact"]
