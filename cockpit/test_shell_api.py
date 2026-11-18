"""cockpit/shell_api.py (areas, attention, team, roster)
(docs/v2/contracts.md §5.1).

Run:  py -3.12 -m pytest -q cockpit/test_shell_api.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (HERE, HERE / "core"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import shell_api  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_AREAS_FILE", str(ROOT / "cockpit" / "ledger2" / "fixtures" / "areas.yaml"))
    monkeypatch.setenv("ATLAS_CLAUDE_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.setenv("ATLAS_TEAM_DOC", str(tmp_path / "no-team-doc.md"))
    yield


SNAP = {"ok": True, "now": "2026-10-09T12:00:00", "working": 1, "counts": {}, "active": 0, "cost": 0,
        "timeline": {"start": 8, "end": 20, "rows": []},
        "sessions": [
            {"sid": "w", "title": "Build the shell", "project": "Atlas-v2-shell", "type": "Coding", "status": "working", "waiting": False, "cwd": "D:\\AI\\Atlas\\cockpit"},
            {"sid": "q", "title": None, "project": "product", "type": "Research", "status": "idle", "waiting": True, "cwd": "D:\\AI\\product\\missions"},
            {"sid": "i", "title": "Old", "project": "x", "type": "Coding", "status": "idle", "waiting": False, "cwd": "E:\\elsewhere"},
        ]}


def test_areas_endpoint_lists_the_six_in_order():
    code, d = shell_api.get("/api/v2/areas", {})
    assert code == 200 and d["ok"]
    assert [a["id"] for a in d["areas"]] == ["md", "job", "train", "atlas", "admin", "pers"]
    assert d["areas"][0] == {"id": "md", "name": "Product", "color": "#d95926", "group": "Product", "archived": False}


def test_unknown_path_is_not_mine():
    assert shell_api.get("/api/v2/other", {}) is None
    assert shell_api.post("/api/v2/areas", {}) is None


def test_longest_folder_prefix_wins_and_is_case_insensitive():
    areas = [{"id": "a", "folders": ["D:\\AI"]}, {"id": "b", "folders": ["D:\\AI\\Atlas"]}]
    assert shell_api.area_of(areas, "d:/ai/atlas/cockpit") == "b"
    assert shell_api.area_of(areas, "D:\\AI\\other") == "a"
    assert shell_api.area_of(areas, "D:\\AIX\\other") is None       # a prefix must end at a folder boundary
    assert shell_api.area_of(areas, None) is None


def test_area_resolver_edge_cases():
    areas = shell_api.load_areas()
    assert shell_api.area_of(areas, r"D:\AI\learning-app2") != "train"
    assert shell_api.area_of(areas, r"D:\AI\learning-app\src") == "train"
    assert shell_api.area_of(areas, None, "ADMIN") == "admin"
    assert shell_api.area_of(areas, None, None, "/daily-review") == "job"
    assert shell_api.area_of(areas, None, None, "/Daily-review") == "job"


def test_load_areas_equals_yaml_safe_load():
    import yaml
    from ledger2 import areas as la
    path = ROOT / "cockpit" / "ledger2" / "fixtures" / "areas.yaml"     # test-owned, never the live areas.yaml
    want = [a for a in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("areas") or []
            if isinstance(a, dict) and a.get("id")]
    assert la.load_areas(str(path)) == want


def test_flow_mapping_closed_by_bracket_raises():
    import pytest
    from ledger2 import areas as la
    with pytest.raises(ValueError):
        la.mini_yaml("k: [x, {y: 1]")


def test_attention_done_session_is_not_waiting(monkeypatch):
    snap = dict(SNAP, sessions=[{"sid": "d", "title": None, "project": "product", "type": "Research", "status": "idle",
                                 "waiting": False, "done": True, "cwd": "D:\\AI\\product\\missions"}])
    monkeypatch.setattr(shell_api, "_snapshot", lambda: snap)
    monkeypatch.setattr(shell_api, "_pending_reviews", lambda: 0)
    code, d = shell_api.get("/api/v2/attention", {})
    assert d["waiting"] == 0 and d["sessions"][0]["status"] == "idle"


def test_attention_payload(monkeypatch):
    monkeypatch.setattr(shell_api, "_snapshot", lambda: SNAP)
    monkeypatch.setattr(shell_api, "_pending_reviews", lambda: 2)
    code, d = shell_api.get("/api/v2/attention", {})
    assert code == 200 and d["ok"]
    assert (d["working"], d["waiting"], d["pending_reviews"]) == (1, 1, 2)
    by = {s["sid"]: s for s in d["sessions"]}
    assert by["w"]["status"] == "working" and by["w"]["area"] == "atlas"
    assert by["q"]["status"] == "waiting" and by["q"]["area"] == "md" and by["q"]["title"] == "Research \u00b7 product"
    assert by["i"]["status"] == "idle" and by["i"]["area"] is None
    assert "window" not in d            # the usage-window meter was removed (contracts.md 5.1)


def test_session_with_a_live_background_helper_counts_as_working_not_waiting(monkeypatch):
    import teamlive

    def e(t, name, **kw):
        d = {"ts": f"2026-10-08T{t}", "ev": name, "sid": "s1", "cwd": r"D:\AI\Atlas"}
        d.update(kw)
        d["_t"] = dt.datetime.fromisoformat(d["ts"])
        return d
    evs = [e("12:00:00", "UserPromptSubmit", transcript=r"C:\x\D--AI-Atlas\s1.jsonl"),
           e("12:00:05", "WorkType", type="coding"),
           e("12:01:00", "PreToolUse", tool="Agent", agent_type="implementer", label="Build", bg=True, tuid="t1"),
           e("12:01:00", "SubagentStart", in_agent="h1", agent_id="h1", agent_type="implementer"),
           e("12:01:01", "PostToolUse", tool="Agent", tuid="t1", status="async_launched", agent_id="h1"),
           e("12:02:00", "Stop")]
    now = dt.datetime(2026, 10, 8, 12, 30, 0)
    mt = {"h1": now - dt.timedelta(seconds=20)}
    snap = teamlive.build_state(evs, now, (), None, None, lambda sid, tr, aid: mt.get(aid))
    monkeypatch.setattr(shell_api, "_snapshot", lambda: snap)
    monkeypatch.setattr(shell_api, "_pending_reviews", lambda: 0)
    d = shell_api.get("/api/v2/attention", {})[1]
    assert (d["working"], d["waiting"]) == (1, 0) and d["sessions"][0]["status"] == "working"
    t = shell_api.get("/api/v2/team", {})[1]["sessions"][0]
    assert t["via_helpers"] is True and t["live_helpers"][0]["name"] == "implementer"
    mt["h1"] = now - dt.timedelta(minutes=10)                # the helper stopped writing: back to waiting
    snap2 = teamlive.build_state(evs, now, (), None, None, lambda sid, tr, aid: mt.get(aid))
    monkeypatch.setattr(shell_api, "_snapshot", lambda: snap2)
    d = shell_api.get("/api/v2/attention", {})[1]
    assert (d["working"], d["waiting"]) == (0, 1)


def test_attention_has_no_window_key_and_starts_no_thread(monkeypatch):
    monkeypatch.setattr(shell_api, "_snapshot", lambda: SNAP)
    monkeypatch.setattr(shell_api, "_pending_reviews", lambda: 0)
    before = {t.name for t in threading.enumerate()}
    d = shell_api.get("/api/v2/attention", {})[1]
    assert set(d) == {"ok", "working", "waiting", "sessions", "pending_reviews", "team_error"}
    assert not hasattr(shell_api, "compute_window") and not hasattr(shell_api, "_ensure_refresher")
    assert {t.name for t in threading.enumerate()} - before == set()


def test_attention_survives_a_broken_snapshot(monkeypatch):
    monkeypatch.setattr(shell_api, "_snapshot", lambda: {"ok": False, "error": "x"})
    code, d = shell_api.get("/api/v2/attention", {})
    assert code == 200 and d["working"] == 0 and d["sessions"] == []


def test_pending_reviews_falls_back_to_zero_without_the_plan_module(monkeypatch):
    import importlib
    real = importlib.import_module

    def fake(name, *a, **k):
        if name == "plan.api":
            raise ImportError("absent")
        return real(name, *a, **k)

    monkeypatch.setattr(shell_api.importlib, "import_module", fake)
    assert shell_api._pending_reviews() == 0


def test_team_payload_adds_area_per_session(monkeypatch):
    monkeypatch.setattr(shell_api, "_snapshot", lambda: SNAP)
    code, d = shell_api.get("/api/v2/team", {})
    assert code == 200 and d["timeline"] == SNAP["timeline"]
    assert [s["area"] for s in d["sessions"]] == ["atlas", "md", None]
    assert d["sessions"][1]["waiting"] is True                     # teamlive's fields pass through


def test_team_payload_error_is_a_500(monkeypatch):
    monkeypatch.setattr(shell_api, "_snapshot", lambda: {"ok": False, "error": "boom"})
    code, d = shell_api.get("/api/v2/team", {})
    assert code == 500 and d == {"ok": False, "error": "boom"}


def test_attention_reports_a_team_log_error(monkeypatch):
    monkeypatch.setattr(shell_api, "_snapshot", lambda: {"ok": False, "error": "log unreadable"})
    d = shell_api.get("/api/v2/attention", {})[1]
    assert d["ok"] and d["team_error"] == "log unreadable" and "window" not in d
    monkeypatch.setattr(shell_api, "_snapshot", lambda: SNAP)
    assert shell_api.get("/api/v2/attention", {})[1]["team_error"] is None


def test_area_order_is_explicit_then_playbook_then_folder():
    areas = shell_api.load_areas()
    cwd = "D:/AI/Atlas"
    assert shell_api.area_of(areas, cwd, None, None) == "atlas"
    assert shell_api.area_of(areas, cwd, None, "daily-review") == "job"
    assert shell_api.area_of(areas, cwd, None, "/daily-review") == "job"
    assert shell_api.area_of(areas, cwd, "train", "daily-review") == "train"
    assert shell_api.area_of(areas, cwd, "nope", None) == "atlas"       # unknown explicit area is ignored


def test_sessions_use_their_playbook_and_explicit_area(monkeypatch):
    snap = dict(SNAP, sessions=[dict(SNAP["sessions"][0], cwd="D:/AI/Atlas", playbook="daily-review", area_arg=None),
                                dict(SNAP["sessions"][0], sid="z", cwd="D:/AI/Atlas", playbook=None, area_arg="job")])
    monkeypatch.setattr(shell_api, "_snapshot", lambda: snap)
    assert [s["area"] for s in shell_api.get("/api/v2/team", {})[1]["sessions"]] == ["job", "job"]


def test_review_tray_failure_is_logged_once(monkeypatch, capsys):
    import sys
    import types
    bad = types.ModuleType("plan.api")

    def boom():
        raise RuntimeError("tray down")
    bad.review_tray_count = boom
    monkeypatch.setitem(sys.modules, "plan.api", bad)
    monkeypatch.setattr(shell_api, "_TRAY_LOGGED", False)
    assert shell_api._pending_reviews() == 0
    assert shell_api._pending_reviews() == 0
    err = capsys.readouterr().err
    assert err.count("review_tray_count failed") == 1


# --- roster (GET /api/v2/shell/roster) ---------------------------------------------------------------
def test_roster_reads_agent_files_and_workflows(monkeypatch, tmp_path):
    ad = tmp_path / "agents"
    ad.mkdir()
    (ad / "zed.md").write_text("---\nname: zed\ndescription: Zed (Haiku). Finds things fast — quickly. Never edits.\nmodel: haiku\n---\n\nBody.\n- Never touch files.\n", encoding="utf-8")
    (ad / "amy.md").write_text("---\nname: amy\ndescription: Builds stuff.\nmodel: sonnet\n---\n\nNo prohibitions here.\n", encoding="utf-8")
    wf = tmp_path / "wf.yaml"
    wf.write_text("types:\n  coding:\n    label: Coding\n    crew: [amy, zed]\n    budget: 2\n    rung: \"2\"\n    move: \"Stays\"\n"
                  "  quick-question:\n    label: Quick question\n    crew: [zed]\n    budget: null\n    rung: \"1\"\n    move: \"Read\"\n"
                  "  undeclared:\n    label: Undeclared\n    crew: []\n    budget: 2\n    rung: \"-\"\n    move: \"x\"\n"
                  "default_budget: 2\nplaybooks: [daily-review, nope]\n"
                  "playbook_crews:\n  daily-review:\n    crew: [amy, zed]\n    budget: 3\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_AGENTS_DIR", str(ad))
    monkeypatch.setenv("ATLAS_WORKFLOWS_FILE", str(wf))
    code, d = shell_api.get("/api/v2/shell/roster", {})
    assert code == 200 and d["ok"]
    hs = {h["id"]: h for h in d["helpers"]}
    assert set(hs) == {"zed", "amy"} and hs["zed"]["model"] == "haiku"
    assert hs["zed"]["use_for"] == "Finds things fast, quickly."          # prefix, Never-sentence and em-dash dropped
    assert hs["zed"]["never"] == "Never touch files." and hs["amy"]["never"] == ""
    assert [t["id"] for t in d["types"]] == ["coding", "quick-question"]  # undeclared skipped
    assert d["types"][0]["crew"] == ["amy", "zed"] and d["types"][1]["budget"] is None
    assert d["playbooks"][0] == {"command": "/daily-review", "area": "Research", "crew": ["amy", "zed"], "budget": 3, "per_task": False}
    assert d["playbooks"][1] == {"command": "/nope", "area": None, "crew": [], "budget": 2, "per_task": False}


def test_roster_with_missing_agents_dir_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_AGENTS_DIR", str(tmp_path / "absent"))
    code, d = shell_api.get("/api/v2/shell/roster", {})
    assert code == 200 and d["helpers"] == []


def test_roster_use_for_and_never_come_from_the_team_doc_table(monkeypatch, tmp_path):
    ad = tmp_path / "agents"
    ad.mkdir()
    (ad / "zed.md").write_text("---\nname: zed\ndescription: Long text. Never edits.\nmodel: haiku\n---\n\n- Never touch files.\n", encoding="utf-8")
    (ad / "amy.md").write_text("---\nname: amy\ndescription: Builds stuff.\n---\n\nbody\n", encoding="utf-8")
    doc = tmp_path / "team.md"
    doc.write_text("# Team\n\nintro\n\n| Helper | Model | Use it for | Never |\n| --- | --- | --- | --- |\n"
                   "| `zed` | Haiku | Finds `things`, fast | Edits |\n"
                   "| `amy` | Sonnet | Builds for the operator | Deciding the operator's calls |\n\nafter\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_AGENTS_DIR", str(ad))
    monkeypatch.setenv("ATLAS_TEAM_DOC", str(doc))
    monkeypatch.setenv("ATLAS_WORKFLOWS_FILE", str(tmp_path / "none.yaml"))
    _, d = shell_api.get("/api/v2/shell/roster", {})
    hs = {h["id"]: h for h in d["helpers"]}
    assert hs["zed"]["use_for"] == "Finds things, fast" and hs["zed"]["never"] == "Edits" and hs["zed"]["model"] == "haiku"
    assert hs["amy"]["model"] == "sonnet"                                  # frontmatter has none: table column
    assert hs["amy"]["use_for"] == "Builds for the operator" and hs["amy"]["never"] == "Deciding the operator's calls"
    assert "The operator" not in str(d)


def test_roster_survives_bad_bytes_in_team_doc_and_agent_file(monkeypatch, tmp_path):
    ad = tmp_path / "agents"
    ad.mkdir()
    (ad / "bad.md").write_bytes(b"\xff\xfe\x00\x81 not utf-8")
    (ad / "ok.md").write_text("---\nname: ok\ndescription: Fine thing.\nmodel: sonnet\n---\n\nbody\n", encoding="utf-8")
    doc = tmp_path / "team.md"
    doc.write_bytes(b"\xff\xfe\x00\x81")
    monkeypatch.setenv("ATLAS_AGENTS_DIR", str(ad))
    monkeypatch.setenv("ATLAS_TEAM_DOC", str(doc))
    code, d = shell_api.get("/api/v2/shell/roster", {})
    assert code == 200 and d["ok"] and [h["id"] for h in d["helpers"]] == ["ok"]


def test_roster_never_500s_when_a_loader_raises(monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(shell_api, "roster_payload", boom)
    assert shell_api.get("/api/v2/shell/roster", {}) == (200, {"ok": True, "helpers": [], "types": [], "playbooks": []})


def test_team_table_escaped_pipe_and_string_crew(monkeypatch, tmp_path):
    ad = tmp_path / "agents"
    ad.mkdir()
    (ad / "zed.md").write_text("---\nname: zed\nmodel: haiku\n---\n\nbody\n", encoding="utf-8")
    doc = tmp_path / "team.md"
    doc.write_text("| Helper | Model | Use it for | Never |\n| --- | --- | --- | --- |\n| `zed` | Haiku | Reads a \\| b | Edits \\| deletes |\n", encoding="utf-8")
    wf = tmp_path / "wf.yaml"
    wf.write_text("types:\n  coding:\n    label: Coding\n    crew: implementer\n    budget: 1\n    rung: \"2\"\n    move: m\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_AGENTS_DIR", str(ad))
    monkeypatch.setenv("ATLAS_TEAM_DOC", str(doc))
    monkeypatch.setenv("ATLAS_WORKFLOWS_FILE", str(wf))
    _, d = shell_api.get("/api/v2/shell/roster", {})
    assert d["helpers"][0]["use_for"] == "Reads a | b" and d["helpers"][0]["never"] == "Edits | deletes"
    assert d["types"][0]["crew"] == ["implementer"]


def test_use_for_strips_the_fable_tier_prefix_like_the_others():
    from shell_api import _use_for
    assert _use_for("Advisor (Fable). Second opinion at a hard decision point. Never reviews.") == "Second opinion at a hard decision point."
    assert _use_for("Scout (Haiku). Finds things. Never edits.") == "Finds things."


# ---- verdict join for the Team chart ------------------------------------------------------------------
def _chart_snap():
    h = lambda aid, ty: {"agent_id": aid, "agent": ty, "type": "x", "n": 1, "label": "", "bg": False, "start": "11:00", "stop": "11:05",
                         "start_h": 11.0, "stop_h": 11.08, "running": False, "resumed": False, "verdict": None}
    return {"ok": True, "now": "2026-10-09T12:00:00", "working": 0, "counts": {}, "active": 0, "cost": 0,
            "timeline": {"start": 8, "end": 20, "rows": []},
            "sessions": [{"sid": "w", "title": "T", "project": "p", "type": "Coding", "status": "idle", "waiting": False,
                          "cwd": "E:/x", "helpers": [h("r1", "inspector"), h("r2", "inspector"), h("b1", "builder")],
                          "helpers_total": 3, "span": {"start_h": 11.0, "end_h": 12.0}}]}


def test_team_payload_fills_verdicts_from_todays_ledger(monkeypatch, tmp_path):
    led = tmp_path / "ledger"
    led.mkdir()
    rows = [{"ev": "verdict", "agent_id": "r1", "verdict": "NEEDS_CHANGES"}, {"ev": "verdict", "agent_id": "r1", "verdict": "READY"},
            {"ev": "verdict", "agent_id": "r2", "verdict": "NEEDS_CHANGES"}, {"ev": "helper_end", "agent_id": "b1"}]
    (led / "events-2026-10-09.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(led))
    monkeypatch.setattr(shell_api, "_snapshot", _chart_snap)
    monkeypatch.setitem(shell_api._verdict_cache, "key", None)
    hs = shell_api.get("/api/v2/team", {})[1]["sessions"][0]["helpers"]
    assert [h["verdict"] for h in hs] == ["READY", "NEEDS_CHANGES", None]       # the last verdict of a run wins


def test_team_payload_verdict_join_failure_leaves_null(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(shell_api, "_snapshot", _chart_snap)
    monkeypatch.setitem(shell_api._verdict_cache, "key", None)
    code, d = shell_api.get("/api/v2/team", {})
    assert code == 200 and [h["verdict"] for h in d["sessions"][0]["helpers"]] == [None, None, None]
    real_import = shell_api.importlib.import_module

    def flaky(name):
        if name == "ledger2.store":
            raise RuntimeError("ledger unavailable")
        return real_import(name)
    monkeypatch.setattr(shell_api.importlib, "import_module", flaky)
    code, d = shell_api.get("/api/v2/team", {})
    assert code == 200 and d["sessions"][0]["helpers"][0]["verdict"] is None


# ---- task groups for the Team chart (helper task, tasks[]) ---------------------------------------------
def _grp_snap(ids=("a", "b", "c", "d", "e")):
    def h(aid):
        return {"agent_id": aid, "agent": "builder", "type": "implementer", "n": 1, "label": "", "bg": False, "start": "11:00",
                "stop": "11:05", "start_h": 11.0, "stop_h": 11.08, "running": False, "resumed": False, "verdict": None}
    return lambda: {"ok": True, "now": "2026-10-09T12:00:00", "working": 0, "counts": {}, "active": 0, "cost": 0,
                    "timeline": {"start": 8, "end": 20, "rows": []},
                    "sessions": [{"sid": "w", "title": "T", "project": "p", "type": "Coding", "status": "idle", "waiting": False,
                                  "cwd": "E:/x", "helpers": [h(i) for i in ids], "helpers_total": len(ids),
                                  "helper_ids_all": None, "span": {"start_h": 11.0, "end_h": 12.0}}]}


def _grp_ledger(monkeypatch, tmp_path, rows, tasks):
    led = tmp_path / "ledger"
    led.mkdir()
    (led / "events-2026-10-09.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(led))
    monkeypatch.setitem(shell_api._verdict_cache, "key", None)
    monkeypatch.setattr(shell_api, "_ledger_tasks", lambda: tasks)


def _rec(tid, **kw):
    return dict({"id": tid, "type": "coding", "area": "atlas", "title": f"title {tid}", "plan_ref": None, "started": "2026-10-09T10:00:00",
                 "status": "done", "final_verdict": "READY"}, **kw)


def test_helper_task_is_the_launch_task_even_when_paired_after_a_new_declaration(monkeypatch, tmp_path):
    rows = [{"ev": "work_type", "task": "T-1", "type": "coding", "area": "atlas", "t": "2026-10-09T10:00:00"},
            {"ev": "helper_start", "task": "T-1", "tuid": "u1", "helper": "implementer"},
            {"ev": "work_type", "task": "T-2", "type": "research", "area": "pers", "t": "2026-10-09T10:30:00"},
            {"ev": "helper_paired", "task": "T-2", "agent_id": "a", "helper": "implementer", "tuid": "u1"},   # paired after the new declaration
            {"ev": "helper_paired", "task": "T-2", "agent_id": "b", "helper": "implementer", "tuid": None},   # unmatched: the pairing task
            {"ev": "helper_paired", "task": "T-2", "agent_id": "a", "helper": "implementer", "tuid": None},   # resume pairing keeps the first
            {"ev": "helper_resume", "task": "T-2", "agent_id": "a", "helper": "implementer"},
            {"ev": "helper_resume", "task": "T-2", "agent_id": "d", "helper": "implementer"}]
    _grp_ledger(monkeypatch, tmp_path, rows, [_rec("T-1", title="Build it", final_verdict="NEEDS_CHANGES"), _rec("T-2", type="research", status="open", final_verdict=None)])
    monkeypatch.setattr(shell_api, "_snapshot", _grp_snap())
    s = shell_api.get("/api/v2/team", {})[1]["sessions"][0]
    assert {h["agent_id"]: h["task"] for h in s["helpers"]} == {"a": "T-1", "b": "T-2", "c": None, "d": "T-2", "e": None}
    assert "helper_ids_all" not in s
    g = {t["id"]: t for t in s["tasks"]}
    assert [t["id"] for t in s["tasks"]] == ["T-1", "T-2", None]                       # by started, the other group last
    assert g["T-1"]["runs"] == 1 and g["T-1"]["title"] == "Build it" and g["T-1"]["final_verdict"] == "NEEDS_CHANGES"
    assert g["T-2"]["runs"] == 2 and g["T-2"]["status"] == "open" and g[None]["runs"] == 2
    assert g[None]["title"] == "Other helper runs" and g[None]["id"] is None


def test_task_missing_from_all_tasks_falls_back_to_the_work_type_event(monkeypatch, tmp_path):
    rows = [{"ev": "work_type", "task": "T-9", "type": "legal", "area": "admin", "t": "2026-10-09T11:00:00"},
            {"ev": "helper_paired", "task": "T-9", "agent_id": "a", "helper": "x", "tuid": None}]
    _grp_ledger(monkeypatch, tmp_path, rows, [])
    monkeypatch.setattr(shell_api, "_snapshot", _grp_snap(("a",)))
    t = shell_api.get("/api/v2/team", {})[1]["sessions"][0]["tasks"]
    assert t == [{"id": "T-9", "type": "legal", "area": "admin", "title": None, "plan_ref": None, "plan_title": None,
                  "started": "2026-10-09T11:00:00", "status": None, "final_verdict": None, "runs": 1}]


def test_per_task_counts_use_the_uncapped_id_list(monkeypatch, tmp_path):
    rows = [{"ev": "helper_paired", "task": "T-1", "agent_id": f"x{i}", "helper": "x", "tuid": None} for i in range(10)]
    _grp_ledger(monkeypatch, tmp_path, rows, [_rec("T-1")])
    snap = _grp_snap(("x8", "x9"))()
    snap["sessions"][0].update(helpers_total=10, helper_ids_all=[f"x{i}" for i in range(10)])
    monkeypatch.setattr(shell_api, "_snapshot", lambda: snap)
    s = shell_api.get("/api/v2/team", {})[1]["sessions"][0]
    assert len(s["helpers"]) == 2 and s["helpers_total"] == 10 and s["tasks"][0]["runs"] == 10


def test_task_groups_survive_a_missing_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(tmp_path / "missing"))
    monkeypatch.setitem(shell_api._verdict_cache, "key", None)
    monkeypatch.setattr(shell_api, "_snapshot", _grp_snap(("a", "b")))
    s = shell_api.get("/api/v2/team", {})[1]["sessions"][0]
    assert [t["id"] for t in s["tasks"]] == [None] and s["tasks"][0]["runs"] == 2 and s["helpers"][0]["task"] is None
