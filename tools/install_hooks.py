"""Install the Atlas v2 hooks (docs/v2/contracts.md §6).

Copies hooks/atlas_hook.py + cockpit/ledger2/areas.py + ledger_chain.py + atlas_config.json (+ JSON copies of
workflows.yaml / areas.yaml) into the hooks folder, installs the grant-rework, ack-break, allow-publish and allow-config skills, widens
team_events.py's type regex, and MERGES the v2 entries into settings.json (timestamped backup first,
idempotent). Every registered event is dry-run with a sample payload first; settings.json is only
written when every run exits 0 with an empty stdout.

Targets (overridable for tests): ATLAS_HOOKS_DIR (default ~/.claude/hooks/atlas), ATLAS_SETTINGS_FILE
(default ~/.claude/settings.json), ATLAS_SKILLS_DIR (default ~/.claude/skills).
NEVER run this against the live home except at the orchestrator's Join 1.

    py -3.12 tools/install_hooks.py [--dry-run] [--python PATH] [--repo DIR] [--no-team-events]
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))
from ledger2 import areas as areas_mod  # noqa: E402
from ledger2 import store  # noqa: E402

PRE_MATCHER = ("Agent|Edit|Write|NotebookEdit|Bash|PowerShell|Skill|SubagentHandback|CronCreate|ScheduleWakeup|SendMessage|"
               "mcp__scheduled-tasks__create_scheduled_task|mcp__scheduled-tasks__update_scheduled_task|RemoteTrigger")
POST_MATCHER = "Agent|Edit|Write|NotebookEdit"
EVENTS = [("PreToolUse", PRE_MATCHER), ("PostToolUse", POST_MATCHER), ("SubagentStart", None),
          ("SubagentStop", None), ("Stop", None), ("SessionStart", None), ("SessionEnd", None),
          ("UserPromptSubmit", None), ("UserPromptExpansion", None)]
HOOK_NAME = "atlas_hook.py"
SKILLS = ("grant-rework", "ack-break", "allow-publish", "allow-config")


def targets() -> dict:
    home = Path(os.path.expanduser("~")) / ".claude"
    return {
        "hooks": Path(os.environ.get("ATLAS_HOOKS_DIR") or home / "hooks" / "atlas"),
        "settings": Path(os.environ.get("ATLAS_SETTINGS_FILE") or home / "settings.json"),
        "skills": Path(os.environ.get("ATLAS_SKILLS_DIR") or home / "skills"),
    }


def _fwd(p) -> str:
    return str(p).replace("\\", "/")


def read_yaml(path: Path):
    return areas_mod.mini_yaml(path.read_text(encoding="utf-8"))


def build_config(repo: Path, existing: dict | None) -> dict:
    cfg = json.loads((repo / "hooks" / "atlas_config.json").read_text(encoding="utf-8"))
    cfg["workflows_file"] = str(repo / "workflows.yaml")
    cfg["areas_file"] = str(repo / "areas.yaml")
    if existing:
        if existing.get("mode") in ("record", "enforce"):
            cfg["mode"] = existing["mode"]          # re-install never flips the mode
        for k in ("interactive_entries", "exempt_dirs", "ledger_dir", "plan_url"):
            if k in existing:
                cfg[k] = existing[k]
    return cfg


def preflight(repo: Path) -> list[str]:
    """The checkout must carry parseable workflows.yaml / areas.yaml (and the hook sources)."""
    bad = []
    for name, key in (("workflows.yaml", "types"), ("areas.yaml", "areas")):
        try:
            obj = read_yaml(repo / name)
            if not isinstance(obj, dict) or not obj.get(key):
                bad.append(f"{name}: missing '{key}'")
        except Exception as e:  # noqa: BLE001
            bad.append(f"{name}: {type(e).__name__}")
    for rel in ("hooks/atlas_hook.py", "hooks/atlas_config.json", "cockpit/ledger2/areas.py",
                "hooks/skills/grant-rework/SKILL.md", "hooks/skills/ack-break/SKILL.md", "hooks/skills/allow-publish/SKILL.md", "hooks/skills/allow-config/SKILL.md",
                "cockpit/ledger2/ledger_chain.py",
                "hooks/team_events.py"):
        if not (repo / rel).is_file():
            bad.append(f"{rel}: not found")
    return bad


def stage_files(dest: Path, repo: Path, existing: dict | None = None) -> list[str]:
    """Write the full hooks folder into `dest`. Raises RuntimeError when a generated JSON copy fails."""
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo / "hooks" / HOOK_NAME, dest / HOOK_NAME)
    shutil.copyfile(repo / "cockpit" / "ledger2" / "areas.py", dest / "areas.py")
    shutil.copyfile(repo / "cockpit" / "ledger2" / "ledger_chain.py", dest / "ledger_chain.py")
    done = [HOOK_NAME, "areas.py", "ledger_chain.py"]
    cfg = build_config(repo, existing)
    (dest / "atlas_config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    done.append("atlas_config.json")
    for src, out in (("workflows.yaml", "workflows.json"), ("areas.yaml", "areas.json")):
        try:
            (dest / out).write_text(json.dumps(read_yaml(repo / src), indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"could not generate {out}: {type(e).__name__}") from e
        done.append(out)
    return done


def desired_entry(python: str, hook_path: str, matcher: str | None) -> dict:
    g = {"hooks": [{"type": "command", "command": python, "args": [hook_path], "timeout": 5}]}
    if matcher:
        g = {"matcher": matcher, **g}
    return g


def _is_ours(group: dict, hook_path: str) -> bool:
    return any(isinstance(h, dict) and any(str(a).replace("\\", "/").endswith("/" + HOOK_NAME)
                                           for a in (h.get("args") or []))
               for h in (group.get("hooks") or []))


def merge_settings(settings: dict, python: str, hook_path: str) -> dict:
    out = json.loads(json.dumps(settings))
    hooks = out.setdefault("hooks", {})
    for event, matcher in EVENTS:
        want = desired_entry(python, hook_path, matcher)
        groups = hooks.setdefault(event, [])
        idx = [i for i, g in enumerate(groups) if isinstance(g, dict) and _is_ours(g, hook_path)]
        if idx and groups[idx[0]] == want and len(idx) == 1:
            continue
        if idx:
            groups[idx[0]] = want
            for i in reversed(idx[1:]):
                del groups[i]
        else:
            groups.append(want)
    return out


SAMPLES = {
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": ": team-type quick-question"}},
    "PostToolUse": {"tool_name": "Edit", "tool_input": {"file_path": "C:/atlas-dry-run/none.txt"}},
    "SubagentStart": {"agent_id": "dry-run-agent", "agent_type": "scout"},
    "SubagentStop": {"agent_id": "dry-run-agent", "agent_type": "scout", "last_assistant_message": "done"},
    "Stop": {"stop_hook_active": False, "transcript_path": "C:/atlas-dry-run/none.jsonl"},
    "SessionStart": {"source": "startup", "transcript_path": "C:/atlas-dry-run/none.jsonl"},
    "SessionEnd": {"reason": "other", "transcript_path": "C:/atlas-dry-run/none.jsonl"},
    "UserPromptSubmit": {"prompt": "hello"},
    "UserPromptExpansion": {"command_name": "dry-run", "expansion_type": "slash_command"},
}


EXPECTED_EVENTS = ("session_start", "prompt", "work_type", "edit", "helper_paired", "helper_end", "session_end")
MAX_RUN_S = 3.0


def dry_run_events(python: str, stage_dir: Path) -> list[str]:
    """Run the staged hook once per registered event against a temp ledger, in record mode. Returns
    problems (empty = all ok): non-zero exit, any stdout, a run over MAX_RUN_S, a hook-errors.log, or
    an expected event missing from the temp ledger."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="atlas-hook-dryrun-") as tmp:
        copy = Path(tmp) / "hooks"
        shutil.copytree(stage_dir, copy)
        cfgp = copy / "atlas_config.json"
        cfg = json.loads(cfgp.read_text(encoding="utf-8"))
        cfg["mode"] = "record"
        cfgp.write_text(json.dumps(cfg), encoding="utf-8")
        ledger = Path(tmp) / "ledger"
        env = dict(os.environ, ATLAS_LEDGER2_DIR=str(ledger), ATLAS_HOOKS_DIR=str(copy))
        for event, _m in EVENTS:
            payload = {"session_id": "dryrun-session", "cwd": tmp, "hook_event_name": event,
                       "transcript_path": os.path.join(tmp, "none.jsonl")}
            payload.update(SAMPLES[event])
            t0 = time.time()
            try:
                r = subprocess.run([python, str(copy / HOOK_NAME)], input=json.dumps(payload),
                                   capture_output=True, text=True, timeout=20, env=env)
            except Exception as e:  # noqa: BLE001
                problems.append(f"{event}: {type(e).__name__}")
                continue
            took = time.time() - t0
            if r.returncode != 0:
                problems.append(f"{event}: exit code {r.returncode}")
            elif r.stdout.strip():
                problems.append(f"{event}: printed to stdout")
            if took > MAX_RUN_S:
                problems.append(f"{event}: took {took:.1f} s (limit {MAX_RUN_S:.0f} s)")
        if (ledger / "hook-errors.log").exists():
            problems.append("hook-errors.log was written during the dry run")
        seen = set()
        for p in ledger.glob("events-*.jsonl"):
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    seen.add(json.loads(line).get("ev"))
                except Exception:  # noqa: BLE001
                    pass
        for ev in EXPECTED_EVENTS:
            if ev not in seen:
                problems.append(f"expected event not logged: {ev}")
    return problems


def install_skill(repo: Path, skills_dir: Path) -> str:
    done = []
    for name in SKILLS:
        dest = skills_dir / name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / "hooks" / "skills" / name / "SKILL.md", dest / "SKILL.md")
        done.append(str(dest / "SKILL.md"))
    return ", ".join(done)


def install_team_events(repo: Path, hooks_dir: Path, ts: str) -> str:
    dst = hooks_dir.parent / "team_events.py"
    src = repo / "hooks" / "team_events.py"
    if dst.is_file() and dst.read_bytes() == src.read_bytes():
        return "team_events.py unchanged"
    if dst.is_file():
        shutil.copyfile(dst, dst.with_name(f"team_events.py.bak-{ts}"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return f"team_events.py updated ({dst})"


def ensure_mode_event(mode: str) -> None:
    base = store.ledger_dir()
    for p in reversed(store.day_files(base)):
        if any(e.get("ev") == "mode" for e in store.read_jsonl(p)):
            return
    store.append([{"ev": "mode", "sid": "installer", "cwd": "", "from": None, "to": mode, "mode": mode}], base)
    sd = base / "state"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "last_mode.json").write_text(json.dumps({"mode": mode, "tamper_for": None}), encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the planned settings diff, write nothing")
    ap.add_argument("--python", default=None, help="python.exe the hooks run with (default: this one)")
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--no-team-events", action="store_true")
    a = ap.parse_args(argv)
    repo = Path(a.repo).resolve()
    t = targets()
    python = _fwd(a.python or sys.executable)
    hooks_dir = t["hooks"]
    hook_path = _fwd(hooks_dir / HOOK_NAME)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    if t["settings"].is_file():
        before = json.loads(t["settings"].read_text(encoding="utf-8"))
    else:
        before = {}
    after = merge_settings(before, python, hook_path)
    diff = list(difflib.unified_diff(json.dumps(before, indent=2).splitlines(),
                                     json.dumps(after, indent=2).splitlines(), "settings.json (current)",
                                     "settings.json (planned)", lineterm=""))

    bad = preflight(repo)
    if bad:
        print("REFUSED: this checkout cannot be installed from:")
        for b in bad:
            print("  -", b)
        return 1

    existing = None
    live_cfg = hooks_dir / "atlas_config.json"
    if live_cfg.is_file():
        try:
            existing = json.loads(live_cfg.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = None
    stage = Path(tempfile.mkdtemp(prefix="atlas-hooks-stage-"))
    try:
        try:
            files = stage_files(stage, repo, existing)
        except RuntimeError as e:
            print("REFUSED:", e)
            return 1
        problems = dry_run_events(python, stage)
        if problems:
            print("REFUSED: the hook failed its dry run, nothing was installed:")
            for p in problems:
                print("  -", p)
            return 1
        print(f"dry run ok for {len(EVENTS)} events (exit 0, empty stdout, expected events logged)")

        if a.dry_run:
            print("planned files in", hooks_dir, ":", ", ".join(files))
            print("planned skills:", ", ".join(str(t["skills"] / n / "SKILL.md") for n in SKILLS))
            print("\n".join(diff) if diff else "settings.json: no change (already installed)")
            return 0

        hooks_dir.mkdir(parents=True, exist_ok=True)
        for n in files:                       # only now does anything reach the live folder
            shutil.copyfile(stage / n, hooks_dir / n)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    print("files:", ", ".join(files), "->", hooks_dir)
    print("skill:", install_skill(repo, t["skills"]))
    if not a.no_team_events:
        print(install_team_events(repo, hooks_dir, ts))
    if after == before and t["settings"].is_file():
        print("settings.json: no change (already installed)")
    else:
        if t["settings"].is_file():
            bak = t["settings"].with_name(t["settings"].name + f".bak-{ts}")
            shutil.copyfile(t["settings"], bak)
            print("backup:", bak)
        t["settings"].parent.mkdir(parents=True, exist_ok=True)
        tmpf = t["settings"].with_name(t["settings"].name + ".tmp")
        tmpf.write_text(json.dumps(after, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmpf, t["settings"])
        print("settings.json: merged")
    cfg = json.loads((hooks_dir / "atlas_config.json").read_text(encoding="utf-8"))
    ensure_mode_event(cfg.get("mode", "record"))
    print("mode:", cfg.get("mode"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
