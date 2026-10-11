"""Publish gate (contracts §6.4b): a publish command is denied in EVERY mode unless the operator typed /allow-publish
in this session. Subprocess runs against temp ledgers; the classifier is also checked in-process. Never the live ledger."""
import base64
import json
import sys
import time

import pytest

from test_atlas_hook import HERE, SID, env  # noqa: F401  (env is a fixture)

DENY = "Publishing needs the operator's OK: ask the operator to type /allow-publish."


def denied(out):
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def refusals(env):
    return [e for e in env.events("refusal") if e.get("guard") == "publish"]


def grant(env, text="/allow-publish", **kw):
    return env.run("UserPromptSubmit", prompt=text, **kw)


class Stub:
    cwd = r"D:\work"


def classify(cmd, cwd=r"D:\work"):
    sys.path.insert(0, str(HERE))
    import atlas_hook
    stub = Stub()
    stub.cwd = cwd
    found = atlas_hook.publish_families(stub, cmd)
    return found[0] if found else None


def classify_all(cmd, cwd=r"D:\work"):
    import atlas_hook
    stub = Stub()
    stub.cwd = cwd
    return atlas_hook.publish_families(stub, cmd)


PUBLISH = [
    "git push", "git push origin main", "git push --tags", "git push -u origin HEAD", "git push --force-with-lease origin x",
    "git -C /x push", "git -c a=b push", "/usr/bin/git push", r'"C:\Program Files\Git\bin\git.exe" push',
    "GIT_SSH_COMMAND=ssh git push", "sudo git push", "env FOO=1 git push", "time git push",
    "echo x && git push", "echo x; git push", "false || git push", "echo hi\ngit push", "git push \\\n origin",
    "if true; then git push; fi", "echo $(git push)", 'echo "$(git push)"', "eval 'git push'",
    'bash -c "git push"', "sh -c 'git push origin'", "bash -lc 'git push'", "bash -c \"bash -c 'git push'\"",
    "pwsh -Command 'git push origin'", "powershell -NoProfile -c git push", 'cmd /c "git push"', "cmd /c git push",
    "wsl git push", "wsl -e git push", "bash <<< 'git push'", "cat <<EOF | bash\ngit push\nEOF",
    "pwsh -EncodedCommand " + base64.b64encode("git push".encode("utf-16-le")).decode(),
    "echo $((1<<2))\ngit push",
    "gh release create v1", "gh release upload v1 f.zip", "gh release edit v1", "gh pr merge 5 --squash", "gh repo create foo",
    "gh api -X POST repos/a/b/issues", "gh api --method=DELETE repos/a/b", "gh api -XPUT x", "gh api repos/a/b -f title=x",
    "gh api -X patch repos/a/b",
    "npm publish", "pnpm publish --access public", "yarn publish", "yarn npm publish", "npm --prefix x publish",
    "twine upload dist/*", "python -m twine upload dist/*", "py -3.12 -m twine upload x", "cargo publish", "cargo +nightly publish",
    "docker push img:1", "docker image push img:1",
    "wrangler deploy", "wrangler publish", "wrangler pages deploy dist", "npx wrangler deploy", "npx -y wrangler pages deploy .",
    "vercel --prod", "vercel deploy --prod", "vc --prod", "netlify deploy --prod", "netlify deploy -p",
    "firebase deploy --only hosting", "flyctl deploy", "fly deploy", "gcloud run deploy x", "gcloud app deploy",
    "az webapp deploy --name x", "az functionapp deploy",
    "aws s3 sync . s3://bucket", "aws s3 cp f s3://b/f", "aws s3 cp --acl public-read f s3://b/f", "aws s3 sync ./d s3://b --delete",
]

NOT_PUBLISH = [
    "git push --dry-run", "git push -n", "git push origin -n", "git push -nu origin x", "git status", "git pull", "git stash push",
    "git log --grep push", "git -C D:/x status", "echo git push", 'git commit -m "git push is blocked"',
    'git commit -m "a; git push"', "grep -rn 'git push' docs", "man git push", "# git push", "echo x # git push",
    "cat <<EOF\ngit push\nEOF", 'git commit -m "$(cat <<\'EOF\'\nPublish gate\n\ngit push is blocked\nEOF\n)"',
    "bash -c 'echo hi'", "pwsh -Command 'Get-ChildItem'", "python -c 'print(1)'", "pytest -q hooks", "ls", "unknowncmd push",
    "npm install", "npm run test", "npm publish --dry-run", "cargo publish --dry-run", "vercel", "vercel deploy", "netlify deploy",
    "gh pr view 5", "gh pr list", "gh release list", "gh release view v1", "gh api repos/a/b", "gh api -X GET repos/a/b",
    "gh api --method GET x -f a=b", "docker build .", "docker run push", "docker pull x", "twine check dist/*",
    "wrangler dev", "wrangler pages dev", "firebase login", "fly status", "gcloud auth list", "az login",
    "aws s3 ls", "aws s3 cp s3://b/f ./f", "aws s3 cp --profile x s3://b/f ./f", "aws s3 sync . s3://b --dryrun",
    r"py -3.12 D:\AI\Atlas\tools\anchor_ledger.py",
]


@pytest.mark.parametrize("cmd", PUBLISH)
def test_classifier_finds_every_publish_family_and_form(cmd):
    assert classify(cmd), cmd


@pytest.mark.parametrize("cmd", NOT_PUBLISH)
def test_classifier_passes_everything_else(cmd):
    assert classify(cmd) is None, cmd


Q = chr(34)

# braces are decided where quoting is known: a quoted brace never cuts a command; block braces still do
BRACE_SHOULD = [
    f"git -c {Q}user.name={{me}}{Q} push", "git -c 'alias.x={y}' push",
    f"docker build -t {Q}{{img}}{Q} --push .", "docker build -t 'img:{tag}' --push .",
    f"vercel deploy {Q}{{dir}}{Q} --prod", "netlify deploy --dir '{x}' --prod", "gh gist create '{f}.md' --public",
    "aws s3 cp '{f}' s3://b/f", "git push origin '{x}'", "git push --repo '{r}'", "gh release create v1 --notes '{json}'",
    "npx wrangler pages deploy '{dist}'", "timeout '{n}' git push", "gh workflow run x.yml -f 'payload={\"a\":1}'",
    "gh api -f 'body={\"a\":1}' repos/a/b/issues",
    "gh api -X POST repos/{owner}/{repo}/issues -f title=x", "gh api repos/{owner}/{repo}/releases -f tag_name=v1",
    "git push origin {main,dev}", "git -c 'core.sshCommand=ssh -i {k}' push", "aws s3 cp f 's3://b/{k}'",
    "find . -name .git -execdir git push ';'",
    # blocks keep working
    "try { git push } catch { Write-Host failed }", "% { git push }", "Get-ChildItem | ForEach-Object { git push }",
    "if ($x) { git push }", "foreach ($d in $dirs) {git push}", "{ git push }", "{{ git push }}", "function p { git push }",
    "find . -exec git push {} \\;", "find . -name x -exec sh -c 'git push' _ {} +", "find . -ok git push ';'",
    # a word-start { before # must not turn the # into a comment
    "echo {#}; git push", "echo {#} && git push", "ls {#,a} | git push",
    # several -exec in one find
    "find . -exec true \\; -exec git push \\;", "find . -exec echo {} ';' -exec git push ';'",
    "find . -exec echo {} + -exec git push {} +", "find . -name x -exec cp {} /tmp \\; -exec git push \\;",
    # PowerShell blocks written without spaces
    "1..3 | %{git push}", "try{git push}catch{}", ".{git push}", "if($x){echo a}else{git push}",
    "do{git push}while($false)", "$sb={git push}; & $sb", "Invoke-Command -ScriptBlock{git push}", "icm{git push}",
    "function p{git push}; p", "&{git push}x", "&{git push}'x'", "&{git push}#c",
    "pwsh -Command \"try{git push}catch{}\"", "bash -c \"echo {#}; git push\"",
    # a # right after a brace is text in the second reading
    "echo {#}; pwsh -c \"try{git push}catch{}\"", "echo }#; pwsh -c \"1|%{git push}\"",
    "echo a}#; pwsh -c \"try{git push}catch{}\"", "echo a{#; pwsh -c \"try{git push}catch{}\"",
    "echo {a}#; pwsh -c \"1|%{git push}\"", "echo {#} && powershell -Command \".{git push}\"",
    # {} glued to text
    "find . -type d -name .git -exec git -C {}/.. push \\;", "find . -exec git -C {}.d push \\;",
    # a second -exec not right after the \;
    "find . -exec true \\; -print -exec git push \\;", "find . -exec true \\; ! -name x -exec git push \\;",
    "find . -exec true \\; , -exec git push \\;", "find . -exec true {} \\; -o -exec git push \\;",
]

BRACE_NOT = [
    "echo {a,b}", "cp file.{txt,bak}", "mkdir -p src/{a,b}", "find . -name '*.pyc' -exec rm {} ';'",
    "find . -exec grep push {} +", "awk '{print $1}' f", "jq '.items[] | {name}' x.json", "python -c \"x = {'a': 1}\"",
    "git log --format='{%H}'", "git commit -m '{wip} fix'", "git commit -m 'feat: {x} git push docs'",
    "echo '{' git push '}'", "Write-Host \"{0}\" -f x", "'{0} {1}' -f $a, $b", "Get-ChildItem | ForEach-Object { $_.Name }",
    "$x = @{ a = 1 }", "$h = @{}; $h.k = 'git push'", "if ($x) { Write-Host ok } else { Write-Host no }",
    "curl -d '{\"cmd\": \"git push\"}' http://localhost", "gh api repos/{owner}/{repo}/pulls",
    "gh pr view --json title --jq '{t: .title}'", "pnpm --filter '{packages/*}' build", "git diff HEAD@{1}",
    "git reset --hard HEAD@{u}", "git rev-parse @{u}", "git log origin/main@{1}..HEAD", "$env:X = 'git push'",
    "$msg = \"run git push later\"", "$s = 'git push'", "$r = git status", "$out = git log -1", "${x} = 1",
    "printf '%s\\n' '{}'", "echo ${HOME}", "ls ${PWD}/{a,b}",
    "find . -exec rm {} ';' -name push", "find . -execdir echo git push ';'",
    "find . -exec echo {} \\; -exec echo git push \\;", "echo {#}", "echo ${x} {a,b} @{u}",
    "find . -exec true \\; -print -exec echo git push \\;", "find . -name x -print", "ls; -print git push",
]


@pytest.mark.parametrize("cmd", BRACE_SHOULD)
def test_braces_a_quoted_brace_never_hides_a_publish_and_blocks_are_read(cmd):
    assert classify(cmd), cmd


@pytest.mark.parametrize("cmd", BRACE_NOT)
def test_braces_ordinary_brace_uses_are_not_publishes(cmd):
    assert classify(cmd) is None, cmd


def test_a_big_command_of_braces_and_quotes_is_classified_in_under_half_a_second():
    cmd = ("{ } '{' \"}\" " * 20000)[:199000]
    assert len(cmd) > 150000
    best = 1e9
    for _ in range(3):
        t0 = time.perf_counter()
        classify(cmd)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.5, best


BS, BT = chr(92), chr(96)


@pytest.mark.parametrize("make", [
    lambda: "bash -c \"bash -c 'eval eval " + (f"a{BT}{BS}{chr(125)} " * 39000)[:195000] + "'\"; git push",
    lambda: "eval eval eval eval " + (f"a{BT}{BS}{{}} " * 40000)[:195000] + "; git push",
])
def test_a_slow_nested_command_is_read_fast_or_refused_never_left_to_the_timeout(make):
    cmd = make()
    took = 1e9
    for _ in range(3):
        t0 = time.perf_counter()
        found = classify_all(cmd)
        took = min(took, time.perf_counter() - t0)
    assert took < 2.0, took
    assert "unreadable command" in found or "git push" in found, found


def test_two_readings_are_merged_by_family_so_every_distinct_publish_counts():
    assert classify_all("try{git push}catch{}; git push") == ["git push", "git push"]
    assert classify_all("git push; aws s3 cp f s3://b/f") == ["git push", "aws s3 cp"]
    assert sorted(classify_all("%{git push}; aws s3 cp f s3://b/f; echo {a}")) == ["aws s3 cp", "git push"]


def test_oversized_and_too_deeply_wrapped_commands_fail_closed():
    assert classify("echo " + "x" * 200001) == "oversized command"
    assert classify("bash -c \"bash -c 'bash -c \\\"bash -c \\\\\\\"bash -c ls\\\\\\\"\\\"'\"") in (None, "nested wrappers")
    deep = "git push"
    for _ in range(7):
        deep = "bash -c " + json.dumps(deep)
    assert classify(deep)                                   # found, or refused as too deep: never passed


def test_the_family_label_is_the_family_not_the_command():
    assert classify("git push origin secret-branch") == "git push"
    assert classify("gh api -X POST repos/a/b -f token=abc") == "gh api POST"
    assert classify("aws s3 sync . s3://private-bucket") == "aws s3 sync"


# ------------------------------------------------------------------ the hook

@pytest.mark.parametrize("mode", ["record", "enforce"])
@pytest.mark.parametrize("cmd", ["git push", "git push --tags", "gh release create v1", "gh pr merge 3", "gh repo create x",
                                 "gh api -X POST repos/a/b", "npm publish", "pnpm publish", "yarn publish", "twine upload dist/*",
                                 "cargo publish", "docker push x", "wrangler deploy", "wrangler pages deploy d",
                                 "vercel --prod", "netlify deploy --prod", "firebase deploy", "flyctl deploy",
                                 "gcloud run deploy x", "az webapp deploy", "aws s3 cp f s3://b/f"])
def test_publish_is_denied_without_a_grant_in_every_mode(env, mode, cmd):
    env.set_mode(mode)
    out = env.bash(cmd)
    assert denied(out)
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == DENY
    r = refusals(env)
    assert len(r) == 1 and r[0]["actor"] == "main" and r[0]["would"] is False and r[0]["what"]


def test_refusal_logs_the_family_and_the_actor_but_never_the_command(env):
    env.bash("git push origin very-secret-branch-name")
    r = refusals(env)[0]
    assert r["what"] == "git push" and r["actor"] == "main"
    text = "".join(p.read_text(encoding="utf-8") for p in env.ledger.glob("events-*.jsonl"))
    assert "very-secret-branch-name" not in text


def test_powershell_tool_is_gated_too(env):
    out = env.run("PreToolUse", tool_name="PowerShell", tool_input={"command": "git push"})
    assert denied(out)


def test_dry_run_and_ordinary_commands_pass_and_log_nothing(env):
    env.set_mode("enforce")
    for cmd in ("git push --dry-run", "git push -n origin x", "git status", "git commit -m 'git push later'", "npm publish --dry-run"):
        assert env.bash(cmd) is None, cmd
    assert not refusals(env) and not env.events("publish_allowed")


def test_chained_and_wrapped_pushes_are_denied(env):
    for cmd in ("echo x && git push", 'bash -c "git push"', "pwsh -Command 'git push'", 'cmd /c "git push"'):
        assert denied(env.bash(cmd)), cmd
    assert len(refusals(env)) == 4


def test_the_anchor_tool_is_exempt_but_a_second_command_is_not(env):
    env.set_mode("enforce")
    for cmd in (r"py -3.12 D:\AI\Atlas\tools\anchor_ledger.py", r"py D:\AI\Atlas\.claude\worktrees\w1\tools\anchor_ledger.py --quiet"):
        assert env.bash(cmd) is None, cmd
    assert denied(env.bash(r"py -3.12 D:\AI\Atlas\tools\anchor_ledger.py && git push"))


def test_anchor_tool_relative_path_needs_an_atlas_cwd():
    cmd = "py tools/anchor_ledger.py -m twine upload"          # the exemption must not hide a second family in the same segment
    assert classify(cmd, cwd=r"D:\AI\Atlas") is None
    assert classify(cmd, cwd=r"D:\elsewhere") == "twine upload"


# ------------------------------------------------------------------ the grant

def test_a_typed_grant_allows_exactly_one_then_denies(env):
    env.set_mode("enforce")
    assert denied(env.bash("git push"))
    assert grant(env) is None
    g = env.events("publish_grant")
    assert len(g) == 1 and g[0]["n"] == 1 and g[0]["expires"]
    assert env.bash("git push") is None
    assert env.events("publish_allowed")[0]["what"] == "git push"
    assert denied(env.bash("git push"))


def test_a_typed_grant_of_n_allows_exactly_n(env):
    env.set_mode("enforce")
    grant(env, "/allow-publish 3")
    assert env.events("publish_grant")[0]["n"] == 3
    for _ in range(3):
        assert env.bash("git push") is None
    assert len(env.events("publish_allowed")) == 3
    assert denied(env.bash("git push"))


def test_the_grant_cap_is_five_and_grants_add_up_to_the_cap(env):
    grant(env, "/allow-publish 99")
    assert env.events("publish_grant")[-1]["n"] == 5 and env.state()["pub_n"] == 5
    grant(env, "/allow-publish 4")
    assert env.state()["pub_n"] == 5
    env.set_mode("enforce")
    for _ in range(5):
        assert env.bash("git push") is None
    assert denied(env.bash("git push"))


def test_a_grant_expires_after_thirty_minutes(env):
    env.set_mode("enforce")
    grant(env)
    g = env.events("publish_grant")[0]
    t, exp = g["t"], g["expires"]
    from datetime import datetime
    assert 29 * 60 <= (datetime.fromisoformat(exp) - datetime.fromisoformat(t)).total_seconds() <= 30 * 60 + 5
    sp = env.ledger / "state" / f"{SID}.json"
    S = json.loads(sp.read_text(encoding="utf-8"))
    S["pub_exp"] = "2020-01-01T00:00:00+00:00"
    sp.write_text(json.dumps(S), encoding="utf-8")
    assert denied(env.bash("git push"))
    assert not env.events("publish_allowed")


def test_a_grant_ends_with_the_session(env):
    env.set_mode("enforce")
    grant(env)
    env.run("SessionEnd", reason="clear")
    assert denied(env.bash("git push"))


def test_a_deleted_state_cache_denies_at_once_and_a_new_grant_works_again(env):
    env.set_mode("enforce")
    grant(env, "/allow-publish 2")
    (env.ledger / "state" / f"{SID}.json").unlink()
    assert denied(env.bash("git push"))               # the deny is decided from the cache: a cold cache never rebuilds
    assert not (env.ledger / "state" / f"{SID}.json").exists()      # and the refusal log did not write a bogus cache
    grant(env)
    assert env.bash("git push") is None


def test_a_grant_needs_no_open_task_and_other_sessions_do_not_share_it(env):
    env.set_mode("enforce")
    grant(env)
    assert not env.events("work_type")
    assert denied(env.bash("git push", sid="s2abcdefgh"))
    assert env.bash("git push") is None


def test_a_helper_is_denied_even_with_a_grant(env):
    env.set_mode("enforce")
    grant(env, "/allow-publish 2")
    out = env.bash("git push", agent_id="A1", agent_type="implementer")
    assert denied(out)
    r = refusals(env)[-1]
    assert r["actor"] == "implementer" and r["agent_id"] == "A1"
    assert env.state()["pub_n"] == 2 and not env.events("publish_allowed")
    assert env.bash("git push") is None                      # the main session still has it


def test_a_grant_typed_inside_a_helper_is_ignored(env):
    grant(env, "/allow-publish", agent_id="A1", agent_type="implementer")
    assert not env.events("publish_grant")


def test_the_model_cannot_invoke_the_skill_in_any_mode(env):
    for mode in ("record", "enforce"):
        env.set_mode(mode)
        out = env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "allow-publish"})
        assert denied(out)
    out = env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "atlas:allow-publish"})
    assert denied(out)
    assert not env.events("publish_grant")
    assert any(e["what"] == "model invoked allow-publish skill" for e in env.events("ignored"))


@pytest.mark.parametrize("tool,key", [("CronCreate", "prompt"), ("ScheduleWakeup", "prompt"), ("SendMessage", "message"),
                                      ("SendMessage", "content")])
@pytest.mark.parametrize("mode", ["record", "enforce"])
@pytest.mark.parametrize("text", ["/allow-publish", "  /allow-publish 5", "/ALLOW-PUBLISH"])
def test_forwarded_allow_publish_is_refused(env, tool, key, mode, text):
    env.set_mode(mode)
    out = env.run("PreToolUse", tool_name=tool, tool_input={key: text, "cron": "* * * * *"})
    assert denied(out)
    assert any(e["what"].startswith("forged user-only command via " + tool) for e in env.events("ignored"))


def test_a_nested_forwarded_allow_publish_is_refused(env):
    out = env.run("PreToolUse", tool_name="SendMessage", tool_input={"to": "x", "message": {"summary": "/allow-publish"}})
    assert denied(out)


def test_an_ordinary_scheduled_prompt_that_only_mentions_it_passes(env):
    assert env.run("PreToolUse", tool_name="CronCreate", tool_input={"prompt": "remind me about /allow-publish docs"}) is None
    assert env.run("PreToolUse", tool_name="CronCreate", tool_input={"prompt": "see /allow-publish-notes.md"}) is None


@pytest.mark.parametrize("entry", ["sdk-cli", "weird", "", "claude-code-scheduled"])
def test_a_grant_from_a_non_interactive_entry_is_ignored(env, entry):
    env.set_mode("enforce")
    assert env.cfg["interactive_entries"] == []
    grant(env, extra_env={"CLAUDE_CODE_ENTRYPOINT": entry})
    assert not env.events("publish_grant")
    assert any(e["what"] == "publish grant from non-interactive entry" for e in env.events("ignored"))
    assert denied(env.bash("git push"))


@pytest.mark.parametrize("entry", ["cli", "claude-desktop", "claude-vscode"])
def test_the_fixed_fallback_entries_are_honoured(env, entry):
    env.set_mode("enforce")
    grant(env, extra_env={"CLAUDE_CODE_ENTRYPOINT": entry})
    assert env.bash("git push") is None


def test_a_configured_entry_list_replaces_the_fallback(env):
    env.cfg["interactive_entries"] = ["claude-desktop"]
    env.set_mode("enforce")
    grant(env, extra_env={"CLAUDE_CODE_ENTRYPOINT": "cli"})
    assert not env.events("publish_grant")
    grant(env, extra_env={"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})
    assert env.bash("git push") is None


def test_only_a_typed_prompt_at_the_start_grants(env):
    env.set_mode("enforce")
    grant(env, "please /allow-publish 3")
    grant(env, "/allow-publish-now")
    env.bash("echo /allow-publish 3")
    env.run("UserPromptExpansion", command_name="allow-publish", expansion_type="slash_command")
    assert not env.events("publish_grant")
    assert denied(env.bash("git push"))


def test_the_grant_event_and_use_carry_metadata_only(env):
    env.set_mode("enforce")
    grant(env, "/allow-publish 2")
    env.bash("git push origin another-secret-name")
    allowed = env.events("publish_allowed")[0]
    assert allowed["what"] == "git push" and "cmd" not in allowed and "command" not in allowed
    text = "".join(p.read_text(encoding="utf-8") for p in env.ledger.glob("events-*.jsonl"))
    assert "another-secret-name" not in text


def test_the_bypass_guard_still_wins_over_the_publish_gate(env):
    env.set_mode("enforce")
    grant(env)
    out = env.bash(r"git -C D:\AI\atlas-data\ledger push")
    assert denied(out) and "Protected Atlas ledger" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not env.events("publish_allowed")


def test_the_skill_file_is_user_only_and_installed():
    text = (HERE / "skills" / "allow-publish" / "SKILL.md").read_text(encoding="utf-8")
    assert "disable-model-invocation: true" in text and text.startswith("---")
    sys.path.insert(0, str(HERE.parent / "tools"))
    import install_hooks
    assert "allow-publish" in install_hooks.SKILLS


# ------------------------------------------------------------------ speed

@pytest.mark.parametrize("make", [
    lambda: "echo " + "x" * 20000,
    lambda: " && ".join(["git status"] * 2000),
    lambda: "echo '" + "a b " * 5000 + "' && git push",
    lambda: 'bash -c "' + "ab " * 6000 + '"',
    lambda: '"' * 20000,
    lambda: "(" * 20000,
    lambda: "<<A\n" * 4000,
    lambda: "cat <<EOF\n" + "git status\n" * 2000 + "EOF\n",
    lambda: "a=b " * 5000 + "git push",
    lambda: "bash -c " * 2500 + "ls",
])
def test_a_twenty_kilobyte_command_is_classified_in_under_fifty_milliseconds(make):
    cmd = make()
    assert 15000 <= len(cmd) <= 30000
    best = 1e9
    for _ in range(3):
        t0 = time.perf_counter()
        classify(cmd)
        best = min(best, time.perf_counter() - t0)
    assert best < 0.05, best
