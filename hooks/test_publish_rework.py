"""Publish gate, review round 1: one grant = one publish, runner/wrapper/escape bypasses, deny before the lock, the
GitHub-action families, the parallel-reviewer race of the carry rule, and a linear forged-grant check."""
import json
import subprocess
import sys
import time

import pytest

from test_atlas_hook import HERE, SID, env  # noqa: F401  (env is a fixture)
from test_publish_gate import classify, classify_all, denied, grant, refusals
from test_resolved_carry import age, carried

# ------------------------------------------------------------------ one grant = one publish


def test_the_scan_collects_every_publish_in_the_command():
    assert classify_all("git push && gh pr merge 3; npm publish") == ["git push", "gh pr merge", "npm publish"]
    assert classify_all('bash -c "git push; git push --tags"') == ["git push", "git push"]
    assert classify_all("git status") == []


def test_a_chain_of_three_on_a_one_grant_is_denied_and_keeps_the_grant(env):
    env.set_mode("enforce")
    grant(env)
    assert denied(env.bash("git push && gh pr merge 3 && npm publish"))
    assert env.state()["pub_n"] == 1 and not env.events("publish_allowed")
    r = refusals(env)[-1]
    assert r["count"] == 3 and r["what"] == "git push"
    assert env.bash("git push") is None                      # the allowance was not spent by the refused chain


def test_a_chain_of_three_on_a_three_grant_is_allowed_and_logs_each(env):
    env.set_mode("enforce")
    grant(env, "/allow-publish 3")
    assert env.bash("git push && gh pr merge 3 && npm publish") is None
    assert [e["what"] for e in env.events("publish_allowed")] == ["git push", "gh pr merge", "npm publish"]
    assert env.state()["pub_n"] == 0
    assert denied(env.bash("git push"))


# ------------------------------------------------------------------ bypasses

BYPASS = [
    # package-runner specs with a version, scopes kept, package aliases
    "npx wrangler@latest pages deploy dist", "npx wrangler@3 deploy", "pnpm dlx wrangler@latest deploy",
    "bunx wrangler@latest deploy", "npx vercel@latest --prod", "npx @cloudflare/wrangler@3.1 deploy",
    "npx netlify-cli deploy --prod", "npx netlify-cli@17 deploy --prod", "npx firebase-tools deploy",
    "npx firebase-tools@latest deploy --only hosting", "npm x wrangler deploy", "bun x wrangler deploy",
    "yarn dlx wrangler@latest deploy", "npx -c 'git push'",
    # pnpm / yarn / bun run a local binary
    "pnpm wrangler deploy", "yarn wrangler deploy", "bun wrangler deploy", "pnpm vercel --prod", "yarn netlify deploy --prod",
    "bun publish",
    # PowerShell
    'iex "git push"', "Invoke-Expression 'git push origin'", "iex -Command 'git push'",
    "Start-Process git -ArgumentList push", "Start-Process -FilePath git -ArgumentList 'push','origin'",
    'saps git -ArgumentList "push"', "start git push", 'start /b "" git push',
    "Start-Process pwsh -ArgumentList '-Command','git push'", "Start-Process bash -ArgumentList '-c','git push'",
    "Invoke-Command -ScriptBlock { git push }", "icm { git push }", "pwsh { git push }", "powershell -NoProfile { gh pr merge 3 }",
    "pwsh -Command { git push }", 'pwsh "git push"',
    # prefixes that take values
    "timeout 30 git push", "timeout -s KILL -k 5 30 git push", "nice -n 5 git push", "sudo -u deploy git push",
    "sudo -u deploy -g staff git push", "env -u HOME git push", "env -C /repo git push", "env -S 'git push'",
    "env -S'git push origin'", "FOO=1 nice -n 3 timeout 9 git push", "ionice -c 3 git push", "setsid git push",
    # xargs
    "echo x | xargs git push", "xargs -I {} git push {}", "xargs -n 1 -P 4 gh pr merge", "echo | xargs -0 -r npm publish",
    # bash -c edge forms
    "bash -c -- 'git push'", "bash --norc -c 'git push'", "sh -ec 'git push'",
    # escape tricks
    r"g\it push", "gi`t push", r"/usr/local/my\ dir/git push", r"git pu\sh", r"\git push", "g''it push", 'g""it push',
    # new families
    "uv publish", "poetry publish --build", "hatch publish", "flit publish", "git subtree push --prefix=x origin main",
    "docker buildx build --push -t x .", "docker build --push .", "docker buildx bake --push", "docker compose push",
    "docker-compose push", "aws s3 mv f s3://b/f", "aws s3 mv ./d s3://b/d --recursive",
    # runner wrappers
    "uvx twine upload dist/*", "uvx twine@6 upload dist/*", "uv run twine upload dist/*", "uv run --with x twine upload d",
    "uv run -- twine upload d", "uv tool run twine upload d", "pipx run twine upload d", "pipx run --spec twine twine upload d",
    "poetry run twine upload d", "uv run python -m twine upload d",
    # GitHub actions
    "gh repo edit --visibility public", "gh repo edit foo --visibility=public --accept-visibility-change-consequences",
    "gh gist create --public f", "gh gist create -p f", "gh pr create --fill", "gh issue create -t x", "gh issue comment 3 -b x",
    "gh pr comment 3 -b x", "gh pr review 3 --approve", "gh workflow run ci.yml", "gh release delete v1", "gh repo delete foo --yes",
    "gh repo archive foo",
]

NOT_BYPASS = [
    "npx cowsay hi", "npx wrangler@latest --version", "npx vercel@latest", "npx netlify-cli deploy", "npx -y prettier --write .",
    "uv run pytest", "uvx ruff check", "poetry run pytest -q", "pipx run black .", "uv sync", "uv pip install x",
    "pnpm test", "pnpm install", "yarn build", "yarn", "bun run dev", "bun install", "pnpm run deploy", "npm run build",
    "timeout 5 ls", "nice ls", "sudo ls", "env", "env FOO=1 ls", "env -S 'echo hi'", "xargs echo", "echo x | xargs -n 1 echo",
    "bash -c -- 'echo hi'", "iex 'Get-Date'", "Start-Process notepad", "start notepad", "saps calc", "icm { Get-Date }",
    "pwsh -File x.ps1", "pwsh -NoProfile -Command Get-Date", "git subtree pull --prefix=x origin main", "git subtree split",
    "docker build .", "docker buildx build -t x .", "docker compose up", "docker-compose up -d", "docker buildx bake",
    "aws s3 mv s3://a/f ./f", "aws s3 mv s3://a/f s3://a/g --dryrun",
    "gh repo view foo", "gh repo edit --visibility private", "gh repo edit --description x", "gh gist create f", "gh gist list",
    "gh pr view 3", "gh pr checkout 3", "gh issue list", "gh issue view 3", "gh workflow list", "gh release list",
    "echo gi`t", r"dir C:\Temp\me", r"type C:\Windows\win.ini", r"cd C:\Program\ Files && dir",
]


@pytest.mark.parametrize("cmd", BYPASS)
def test_realistic_bypasses_are_closed(cmd):
    assert classify(cmd), cmd


@pytest.mark.parametrize("cmd", NOT_BYPASS)
def test_the_new_rules_keep_false_positives_down(cmd):
    assert classify(cmd) is None, cmd


def test_a_versioned_runner_keeps_the_scope_and_the_version_is_dropped():
    sys.path.insert(0, str(HERE))
    import atlas_hook
    assert atlas_hook._pkg_head("wrangler@latest") == "wrangler"
    assert atlas_hook._pkg_head("@cloudflare/wrangler@3.1") == "@cloudflare/wrangler"
    assert atlas_hook._pkg_head("@cloudflare/wrangler") == "@cloudflare/wrangler"
    assert atlas_hook._pkg_head("netlify-cli@17") == "netlify"


def test_env_dash_s_string_is_rescanned_and_the_family_label_is_right():
    assert classify_all("env -S 'git push'") == ["git push"]
    assert classify_all("timeout 30 npm publish") == ["npm publish"]
    assert classify_all("uv publish") == ["uv publish"]
    assert classify_all("docker buildx build --push .") == ["docker build --push"]


# ------------------------------------------------------------------ the deny is decided before the lock

def _hold_lock(env):
    import msvcrt
    env.ledger.mkdir(parents=True, exist_ok=True)
    fh = open(env.ledger / ".lock", "a+b")
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    return fh


def _release(fh):
    import msvcrt
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    fh.close()


def _timed_bash(env, cmd):
    t0 = time.perf_counter()
    out = env.bash(cmd)
    return out, time.perf_counter() - t0


def test_deny_is_fast_with_the_lock_held_and_a_cold_cache(env):
    env.set_mode("enforce")
    env.bash("echo warm up the interpreter")
    fh = _hold_lock(env)
    try:
        out, took = _timed_bash(env, "git push")
    finally:
        _release(fh)
    assert denied(out)
    print("cold-cache deny with the lock held: %.3f s" % took)
    assert took < 1.0, took


def test_deny_is_fast_with_the_lock_held_and_a_warm_cache_without_a_grant(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    fh = _hold_lock(env)
    try:
        out, took = _timed_bash(env, "git push")
    finally:
        _release(fh)
    assert denied(out)
    print("no-grant deny with the lock held: %.3f s" % took)
    assert took < 1.0, took


def test_a_grant_cannot_be_used_while_the_lock_is_held_and_is_kept(env):
    env.set_mode("enforce")
    grant(env)
    fh = _hold_lock(env)
    try:
        out, took = _timed_bash(env, "git push")
    finally:
        _release(fh)
    assert denied(out)
    print("grant held up by the lock, deny after: %.3f s" % took)
    assert took < 2.0, took
    assert env.state()["pub_n"] == 1 and not env.events("publish_allowed")
    assert env.bash("git push") is None                      # lock free again: the grant is still there


def test_the_deny_never_rebuilds_the_state(env):
    """A cold cache must not trigger the 30-day ledger scan: seed a big ledger and time the deny."""
    env.set_mode("enforce")
    env.ledger.mkdir(parents=True)
    day = env.ledger / "events-2026-10-09.jsonl"
    line = json.dumps({"v": 2, "t": "2026-10-09T10:00:00+02:00", "ev": "prompt", "sid": "other"}) + "\n"
    day.write_text(line * 40000, encoding="utf-8")
    out, took = _timed_bash(env, "git push")
    assert denied(out) and took < 1.0, took


# ------------------------------------------------------------------ carry rule: parallel reviewers

def _spawn(env, tuid, aid, helper="reviewer"):
    env.agent(helper, tuid)
    return aid


def _start(env, aid, helper="reviewer"):
    env.run("SubagentStart", agent_id=aid, agent_type=helper)


def _hand_back(env, aid, text, helper="reviewer"):
    env.run("PreToolUse", tool_name="SubagentHandback", agent_id=aid, agent_type=helper, tool_input={"message": text})
    env.run("SubagentStop", agent_id=aid, agent_type=helper, last_assistant_message="")


def test_a_ready_from_a_reviewer_launched_alongside_the_failing_one_does_not_resolve(env):
    env.bash(": team-type chore")
    env.edit("a.py")
    env.agent("reviewer", "rA")                  # A and B are launched together
    env.agent("reviewer", "rB")
    _start(env, "A")
    _start(env, "B")
    _hand_back(env, "A", "NEEDS_CHANGES\n- one")
    _hand_back(env, "B", "READY")
    S = env.state()
    assert S["last_verdict"] == "READY" and S["last_verified"] >= S["version"]
    age(env)
    env.bash(": team-type chore")
    assert carried(env)                          # still unresolved: B started before A's NEEDS CHANGES arrived


def test_a_ready_from_a_reviewer_launched_after_the_failure_resolves(env):
    env.bash(": team-type chore")
    env.edit("a.py")
    env.agent("reviewer", "rA")
    _start(env, "A")
    _hand_back(env, "A", "NEEDS_CHANGES\n- one")
    env.agent("implementer", "i1")               # the fix, then a new review
    env.edit("a.py")
    env.agent("reviewer", "rB")
    _start(env, "B")
    _hand_back(env, "B", "READY")
    age(env)
    env.bash(": team-type chore")
    assert not carried(env)


def test_derive_mirrors_the_parallel_reviewer_race(env):
    sys.path.insert(0, str(HERE.parent / "cockpit"))
    from ledger2 import derive, query
    env.bash(": team-type chore")
    env.edit("a.py")
    env.agent("reviewer", "rA")
    env.agent("reviewer", "rB")
    _start(env, "A")
    _start(env, "B")
    _hand_back(env, "A", "NEEDS_CHANGES\n- one")
    _hand_back(env, "B", "READY")
    age(env)
    env.bash(": team-type chore")
    legacy = [{k: v for k, v in e.items() if k != "carried"} for e in env.events()]
    for evs in (env.events(), legacy):
        ts = derive.derive_v2(evs, query.load_workflows())
        assert [t["rounds"]["needs_changes"] for t in ts] == [1, 1]


# ------------------------------------------------------------------ forged_grant is linear

def test_forged_grant_is_linear_on_many_mentions():
    sys.path.insert(0, str(HERE))
    import atlas_hook
    for cmd, expect in (("claude " * 25000, False), ("claude " * 25000 + "-p x", True), ("echo claude -p " * 12000, True),
                        ("claude x " * 25000 + "--print", True), ("claude -p x " * 10000, True)):
        best = 1e9
        for _ in range(3):
            t0 = time.perf_counter()
            got = atlas_hook.forged_grant(cmd)
            best = min(best, time.perf_counter() - t0)
        assert got is expect, cmd[:30]
        assert best < 0.1, (cmd[:30], best)


@pytest.mark.parametrize("cmd,expect", [
    ('claude --resume abc -p "/grant-rework 5"', True), ("cd x && claude -p '/grant-rework'", True),
    (r"C:\tools\claude.exe --print x", True), ("echo claude then -p later", True), ("claude only", False),
    ("git commit -m 'mention /grant-rework and claude'", False), ("-p before claude", False),
])
def test_forged_grant_keeps_its_meaning(cmd, expect):
    sys.path.insert(0, str(HERE))
    import atlas_hook
    assert atlas_hook.forged_grant(cmd) is expect


def test_escape_variants_and_backslash_paths_do_not_slow_the_scan_down():
    cmds = [r"a\b " * 5000, ("bash -c " + json.dumps(r"x\y " * 3000)), "`" * 20000, r"\ " * 8000]
    for cmd in cmds:
        best = 1e9
        for _ in range(3):
            t0 = time.perf_counter()
            classify(cmd)
            best = min(best, time.perf_counter() - t0)
        assert best < 0.05, (cmd[:20], best)
