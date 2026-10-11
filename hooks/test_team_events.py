"""team_events.py: the optional area word of `: team-type <type> <area>` is recorded."""
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "team_events.py"


def _run(tmp_path, command):
    env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": str(tmp_path),
               "tool_name": "Bash", "tool_input": {"command": command}}
    subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
                   capture_output=True, env=env, timeout=30)
    recs = []
    for p in (tmp_path / ".claude" / "team-live").glob("events-*.jsonl"):
        recs += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    return recs


def test_worktype_carries_area(tmp_path):
    recs = _run(tmp_path, ": team-type coding Atlas")
    assert recs[-1]["ev"] == "WorkType" and recs[-1]["type"] == "coding" and recs[-1]["area"] == "atlas"


def test_worktype_without_area(tmp_path):
    recs = _run(tmp_path, ": team-type coding")
    assert recs[-1]["type"] == "coding" and "area" not in recs[-1]


def test_worktype_carries_plan_ref(tmp_path):
    recs = _run(tmp_path, ": team-type writing md task:evt_0a1b2c3d@2026-10-12")
    assert recs[-1]["type"] == "writing" and recs[-1]["area"] == "md" and recs[-1]["plan_ref"] == "evt_0a1b2c3d@2026-10-12"
    recs = _run(tmp_path, ": team-type coding task:t_deadbeef")
    assert recs[-1]["type"] == "coding" and "area" not in recs[-1] and recs[-1]["plan_ref"] == "t_deadbeef"


def test_worktype_bad_plan_ref_is_dropped_type_kept(tmp_path):
    recs = _run(tmp_path, ": team-type coding atlas task:bad!id")
    assert recs[-1]["type"] == "coding" and recs[-1]["area"] == "atlas" and "plan_ref" not in recs[-1]


def test_worktype_from_a_helper_is_not_logged(tmp_path):
    import json as _j
    env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    payload = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": str(tmp_path), "agent_id": "A1",
               "tool_name": "Bash", "tool_input": {"command": ": team-type coding task:t_aaaaaaaa"}}
    subprocess.run([sys.executable, str(HOOK)], input=_j.dumps(payload), text=True, capture_output=True, env=env, timeout=30)
    assert not list((tmp_path / ".claude" / "team-live").glob("events-*.jsonl"))
