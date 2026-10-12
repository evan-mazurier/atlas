"""Tests for tools/install_hooks.py, hooks_mode.py, import_v1_ledger.py. Temp dirs and env overrides only."""
import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "cockpit"))

import hooks_mode  # noqa: E402
import import_v1_ledger  # noqa: E402
import install_hooks  # noqa: E402
from ledger2 import store  # noqa: E402

PY = __import__("sys").executable.replace("\\", "/")


def entry(script, matcher=None, **extra):
    h = {"type": "command", "command": PY, "args": ["~/.claude/hooks/" + script], "timeout": 5, **extra}
    g = {"hooks": [h]}
    if matcher:
        g = {"matcher": matcher, **g}
    return g


REAL_SHAPED_SETTINGS = {
    "autoUpdatesChannel": "latest", "theme": "dark", "effortLevel": "xhigh",
    "permissions": {"defaultMode": "auto", "allow": ["Read", "Edit"], "ask": ["Bash(git push:*)"]},
    "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
    "hooks": {
        "PreToolUse": [entry("require_named_helper.py", "Agent"),
                       entry("team_events.py", "Agent|Skill|Bash|PowerShell", **{"async": True})],
        "PostToolUse": [entry("team_events.py", "Agent", **{"async": True})],
        "SessionStart": [entry("team_events.py", **{"async": True})],
        "Stop": [entry("team_events.py", **{"async": True})],
        "SubagentStop": [entry("team_events.py", **{"async": True})],
    },
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    hooks = tmp_path / "claude" / "hooks" / "atlas"
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(REAL_SHAPED_SETTINGS, indent=2), encoding="utf-8")
    monkeypatch.setenv("ATLAS_HOOKS_DIR", str(hooks))
    monkeypatch.setenv("ATLAS_SETTINGS_FILE", str(settings))
    monkeypatch.setenv("ATLAS_SKILLS_DIR", str(tmp_path / "claude" / "skills"))
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(tmp_path / "ledger"))
    return tmp_path


def baks(home):
    return sorted((home / "claude").glob("settings.json.bak-*"))


# ------------------------------------------------------------------ installer

def test_install_merges_keeps_existing_and_is_idempotent(home, capsys):
    settings = home / "claude" / "settings.json"
    assert install_hooks.main(["--python", PY]) == 0
    after = json.loads(settings.read_text(encoding="utf-8"))
    # every existing entry is still there, unchanged
    for ev, groups in REAL_SHAPED_SETTINGS["hooks"].items():
        for g in groups:
            assert g in after["hooks"][ev]
    assert {k: v for k, v in after.items() if k != "hooks"} == {k: v for k, v in REAL_SHAPED_SETTINGS.items() if k != "hooks"}
    # the v2 entries: sync, full python path + args form, timeout 5
    ours = {ev: [g for g in groups if "atlas_hook.py" in json.dumps(g)] for ev, groups in after["hooks"].items()}
    assert set(k for k, v in ours.items() if v) == {e for e, _m in install_hooks.EVENTS}
    pre = ours["PreToolUse"][0]
    assert pre["matcher"] == "Agent|Edit|Write|NotebookEdit|Bash|PowerShell|Skill|SubagentHandback|CronCreate|ScheduleWakeup|SendMessage"
    h = pre["hooks"][0]
    assert h["command"] == PY and h["timeout"] == 5 and "async" not in h and h["args"][0].endswith("/atlas/atlas_hook.py")
    assert ours["PostToolUse"][0]["matcher"] == "Agent|Edit|Write|NotebookEdit"
    assert "matcher" not in ours["Stop"][0]
    assert len(baks(home)) == 1
    # files and skill
    d = home / "claude" / "hooks" / "atlas"
    for n in ("atlas_hook.py", "areas.py", "ledger_chain.py", "atlas_config.json", "workflows.json", "areas.json"):
        assert (d / n).is_file(), n
    assert json.loads((d / "atlas_config.json").read_text())["mode"] == "record"
    assert json.loads((d / "workflows.json").read_text())["types"]["coding"]["budget"] == 2
    skill = (home / "claude" / "skills" / "grant-rework" / "SKILL.md").read_text(encoding="utf-8")
    assert "disable-model-invocation: true" in skill
    ack = (home / "claude" / "skills" / "ack-break" / "SKILL.md").read_text(encoding="utf-8")
    assert "disable-model-invocation: true" in ack
    pub = (home / "claude" / "skills" / "allow-publish" / "SKILL.md").read_text(encoding="utf-8")
    assert "disable-model-invocation: true" in pub
    assert (home / "claude" / "hooks" / "team_events.py").read_bytes() == (REPO / "hooks" / "team_events.py").read_bytes()
    # initial mode event written
    assert [e["to"] for e in store.read_events() if e["ev"] == "mode"] == ["record"]
    # second run changes nothing: same settings bytes, no new backup, no second mode event
    before = settings.read_bytes()
    assert install_hooks.main(["--python", PY]) == 0
    assert settings.read_bytes() == before
    assert len(baks(home)) == 1
    assert len([e for e in store.read_events() if e["ev"] == "mode"]) == 1
    assert "no change" in capsys.readouterr().out


def test_install_keeps_enforce_mode_on_reinstall(home):
    assert install_hooks.main(["--python", PY]) == 0
    cfgp = home / "claude" / "hooks" / "atlas" / "atlas_config.json"
    cfg = json.loads(cfgp.read_text())
    cfg["mode"] = "enforce"
    cfgp.write_text(json.dumps(cfg))
    assert install_hooks.main(["--python", PY]) == 0
    assert json.loads(cfgp.read_text())["mode"] == "enforce"


def test_dry_run_writes_nothing_and_prints_diff(home, capsys):
    settings = home / "claude" / "settings.json"
    before = settings.read_bytes()
    assert install_hooks.main(["--python", PY, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert settings.read_bytes() == before and not baks(home)
    assert not (home / "claude" / "hooks" / "atlas").exists()
    assert not (home / "claude" / "skills").exists()
    assert "+" in out and "atlas_hook.py" in out and "SubagentHandback" in out


def test_install_refuses_when_hook_fails_dry_run(home, tmp_path, capsys):
    bad = tmp_path / "badrepo"
    for rel in ("hooks", "cockpit/ledger2"):
        shutil.copytree(REPO / rel, bad / rel, ignore=shutil.ignore_patterns("test_*", "__pycache__"))
    shutil.copyfile(REPO / "workflows.yaml", bad / "workflows.yaml")
    shutil.copyfile(REPO / "areas.yaml", bad / "areas.yaml")
    hook = bad / "hooks" / "atlas_hook.py"
    hook.write_text(hook.read_text(encoding="utf-8").replace("def main():\n    out = None",
                                                             "def main():\n    print('oops')\n    out = None"),
                    encoding="utf-8")
    settings = home / "claude" / "settings.json"
    before = settings.read_bytes()
    assert install_hooks.main(["--python", PY, "--repo", str(bad)]) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert settings.read_bytes() == before and not baks(home)
    assert not (home / "claude" / "hooks" / "atlas").exists()          # nothing reached the live folder


def _bad_repo(tmp_path, edit):
    bad = tmp_path / "badrepo"
    for rel in ("hooks", "cockpit/ledger2"):
        shutil.copytree(REPO / rel, bad / rel, ignore=shutil.ignore_patterns("test_*", "__pycache__"))
    shutil.copyfile(REPO / "workflows.yaml", bad / "workflows.yaml")
    shutil.copyfile(REPO / "areas.yaml", bad / "areas.yaml")
    edit(bad)
    return bad


def _patch_hook(bad, old, new):
    hook = bad / "hooks" / "atlas_hook.py"
    text = hook.read_text(encoding="utf-8")
    assert old in text
    hook.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_install_refuses_when_hook_writes_error_log(home, tmp_path, capsys):
    bad = _bad_repo(tmp_path, lambda b: _patch_hook(b, "def dispatch(H):\n", "def dispatch(H):\n    raise RuntimeError('x')\n"))
    assert install_hooks.main(["--python", PY, "--repo", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "hook-errors.log" in out and "expected event not logged" in out
    assert not (home / "claude" / "hooks" / "atlas").exists() and not baks(home)


def test_install_refuses_when_expected_events_missing(home, tmp_path, capsys):
    bad = _bad_repo(tmp_path, lambda b: _patch_hook(b, "def h_subagent_stop(H):\n", "def h_subagent_stop(H):\n    return None\n"))
    assert install_hooks.main(["--python", PY, "--repo", str(bad)]) == 1
    assert "expected event not logged: helper_end" in capsys.readouterr().out


def test_install_refuses_slow_hook(home, monkeypatch, capsys):
    monkeypatch.setattr(install_hooks, "MAX_RUN_S", 0.0)
    assert install_hooks.main(["--python", PY]) == 1
    assert "took" in capsys.readouterr().out
    assert not (home / "claude" / "hooks" / "atlas").exists()


def test_install_refuses_checkout_with_unparsable_yaml(home, tmp_path, capsys):
    def ruin(b):
        (b / "workflows.yaml").write_text("types: {a: [b}\n", encoding="utf-8")
    bad = _bad_repo(tmp_path, ruin)
    assert install_hooks.main(["--python", PY, "--repo", str(bad)]) == 1
    assert "workflows.yaml" in capsys.readouterr().out
    assert not (home / "claude" / "hooks" / "atlas").exists()


def test_install_replaces_stale_atlas_entry_in_place(home):
    s = json.loads((home / "claude" / "settings.json").read_text())
    s["hooks"]["Stop"].append(entry("atlas/atlas_hook.py", **{"async": True}))
    (home / "claude" / "settings.json").write_text(json.dumps(s))
    assert install_hooks.main(["--python", PY]) == 0
    stop = json.loads((home / "claude" / "settings.json").read_text())["hooks"]["Stop"]
    assert len([g for g in stop if "atlas_hook.py" in json.dumps(g)]) == 1
    assert "async" not in json.dumps([g for g in stop if "atlas_hook.py" in json.dumps(g)])


def test_build_config_keeps_plan_url_off_and_defaults_when_absent():
    repo = Path(__file__).resolve().parent.parent
    default = json.loads((repo / "hooks" / "atlas_config.json").read_text(encoding="utf-8"))["plan_url"]
    assert install_hooks.build_config(repo, {"plan_url": "off"})["plan_url"] == "off"
    assert install_hooks.build_config(repo, {"mode": "record"})["plan_url"] == default
    assert install_hooks.build_config(repo, None)["plan_url"] == default


def test_merge_settings_is_pure():
    before = copy.deepcopy(REAL_SHAPED_SETTINGS)
    out = install_hooks.merge_settings(before, PY, "C:/x/atlas/atlas_hook.py")
    assert before == REAL_SHAPED_SETTINGS and out != before
    assert install_hooks.merge_settings(out, PY, "C:/x/atlas/atlas_hook.py") == out
    assert install_hooks.merge_settings({}, PY, "C:/x/atlas/atlas_hook.py")["hooks"]["Stop"]


def test_installed_hook_runs_end_to_end(home):
    import os
    import subprocess
    assert install_hooks.main(["--python", PY]) == 0
    d = home / "claude" / "hooks" / "atlas"
    payload = {"hook_event_name": "SessionStart", "session_id": "inst0001", "cwd": str(home), "source": "startup"}
    r = subprocess.run([sys.executable, str(d / "atlas_hook.py")], input=json.dumps(payload), capture_output=True,
                       text=True, env=dict(os.environ))
    assert r.returncode == 0 and r.stdout == ""
    assert any(e["ev"] == "session_start" for e in store.read_events())


# ------------------------------------------------------------------ hooks_mode

def test_hooks_mode_switches_config_and_logs(home):
    assert install_hooks.main(["--python", PY]) == 0
    assert hooks_mode.main(["enforce"]) == 0
    cfg = json.loads((home / "claude" / "hooks" / "atlas" / "atlas_config.json").read_text())
    assert cfg["mode"] == "enforce"
    modes = [e for e in store.read_events() if e["ev"] == "mode"]
    assert (modes[-1]["from"], modes[-1]["to"]) == ("record", "enforce")
    assert hooks_mode.main(["bogus"]) == 2
    # the hook agrees with the mode event: no tamper
    import subprocess
    payload = {"hook_event_name": "SessionStart", "session_id": "m0001", "cwd": str(home)}
    subprocess.run([sys.executable, str(home / "claude" / "hooks" / "atlas" / "atlas_hook.py")],
                   input=json.dumps(payload), capture_output=True, text=True)
    assert not [e for e in store.read_events() if e["ev"] == "tamper"]


def test_hooks_mode_writes_under_the_ledger_lock(home, monkeypatch):
    import contextlib
    assert install_hooks.main(["--python", PY]) == 0
    cfg_path = home / "claude" / "hooks" / "atlas" / "atlas_config.json"
    seen = {}
    real = store.locked

    @contextlib.contextmanager
    def spy(base=None):
        with real(base):
            seen["before"] = json.loads(cfg_path.read_text())["mode"]
            yield
            seen["inside_after"] = json.loads(cfg_path.read_text())["mode"]
    monkeypatch.setattr(store, "locked", spy)
    assert hooks_mode.main(["enforce"]) == 0
    assert seen == {"before": "record", "inside_after": "enforce"}


def test_hooks_mode_without_install_fails(home):
    assert hooks_mode.main(["record"]) == 1


# ------------------------------------------------------------------ v1 import

V1_LINES = [
    {"t": "2026-01-05T11:45:13", "ev": "started", "job": "job_a", "project": "client-app", "lane": None,
     "task": "SECRET free text brief", "model": "opus", "effort": "high", "type": "implementation", "src": "backfill"},
    {"t": "2026-01-05T12:22:52", "ev": "review", "job": "job_a", "project": "client-app", "lane": "s2",
     "verdict": "needs-changes", "pass": 1, "notes": "long reviewer notes", "q": "a question"},
    {"t": "2026-01-05T12:30:00", "ev": "review", "job": "job_a", "project": "client-app", "verdict": "commit-ready", "pass": 2},
    {"t": "2026-01-05T12:31:00", "ev": "review", "job": "job_a", "verdict": "escalate", "pass": 3},
    {"t": "2026-01-05T15:24:14", "ev": "merged", "job": "job_a", "commit": "abc1234", "branch": "atlas/job_a",
     "diffstat": "a | 2 +-", "note": "n", "why": "w", "error": "e", "question": "q?", "answer": "a!"},
    {"t": "2026-01-05T15:28:17", "ev": "finished", "job": "job_a", "status": "merged", "tokens": 600000,
     "cache_tokens": 5000000, "rework": 0, "created": "2026-01-05T11:45:10"},
    {"t": "2026-01-05T12:01:57", "ev": "phase_tokens", "job": "job_a", "phase": "scoping", "fresh": 60100,
     "input": 100, "output": 60000, "cache_read": 5000000, "cache_write": 170000, "plan_task": "app-routes"},
]


def write_src(tmp_path):
    src = tmp_path / "v1src" / "events.jsonl"
    src.parent.mkdir()
    src.write_text("\n".join(json.dumps(x) for x in V1_LINES) + "\nnot json\n", encoding="utf-8")
    return src


def test_v1_import_drops_non_allowlisted_fields_and_maps_verdicts(tmp_path, capsys):
    src, dst = write_src(tmp_path), tmp_path / "out" / "v1" / "events.jsonl"
    before = src.read_bytes()
    assert import_v1_ledger.main(["--src", str(src), "--dst", str(dst)]) == 0
    assert src.read_bytes() == before                                    # source untouched
    rows = [json.loads(x) for x in dst.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(V1_LINES)
    assert all(r["v"] == 1 and r["src"] == "v1" for r in rows)
    text = dst.read_text(encoding="utf-8")
    for leaked in ("SECRET", "long reviewer notes", "a question", "diffstat", "a!", "q?", "cache_tokens"):
        assert leaked not in text, leaked
    for r in rows:
        assert not ({"task", "notes", "q", "note", "why", "error", "diffstat", "question", "answer"} & set(r))
    assert [r.get("verdict") for r in rows if r["ev"] == "review"] == ["NEEDS_CHANGES", "READY", "ESCALATE"]
    assert rows[0]["model"] == "opus" and rows[0]["type"] == "implementation"
    assert rows[-1]["plan_task"] == "app-routes" and rows[-1]["cache_read"] == 5000000
    out = capsys.readouterr().out
    assert "dropped fields" in out and "notes=1" in out and "diffstat=1" in out and "(1 unparsed)" in out


def test_v1_import_is_idempotent(tmp_path):
    src, dst = write_src(tmp_path), tmp_path / "v1.jsonl"
    import_v1_ledger.main(["--src", str(src), "--dst", str(dst)])
    first = dst.read_bytes()
    import_v1_ledger.main(["--src", str(src), "--dst", str(dst)])
    assert dst.read_bytes() == first


def test_v1_import_default_dst_follows_ledger_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(tmp_path / "led"))
    assert import_v1_ledger.main(["--src", str(write_src(tmp_path))]) == 0
    assert (tmp_path / "led" / "v1" / "events.jsonl").is_file()


def test_v1_import_missing_source(tmp_path):
    assert import_v1_ledger.main(["--src", str(tmp_path / "nope.jsonl"), "--dst", str(tmp_path / "x")]) == 1


def test_imported_v1_feeds_derive(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(tmp_path / "led"))
    import_v1_ledger.main(["--src", str(write_src(tmp_path))])
    from ledger2 import derive, query
    t = derive.derive_v1(store.read_v1(), [], query.load_workflows())[0]
    assert t["id"] == "job_a" and t["title"] == "app-routes" and t["status"] == "done"
    assert [v["verdict"] for v in t["verdicts"]] == ["NEEDS_CHANGES", "READY", "ESCALATE"]
