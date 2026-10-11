"""cockpit/shell_api.py (areas, attention, team, roster) and cockpit/core/usage.window_by_model()
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
import usage  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_AREAS_FILE", str(ROOT / "cockpit" / "ledger2" / "fixtures" / "areas.yaml"))
    monkeypatch.setenv("ATLAS_CLAUDE_DIR", str(tmp_path / "claude-projects"))
    monkeypatch.setenv("ATLAS_TEAM_DOC", str(tmp_path / "no-team-doc.md"))
    monkeypatch.setattr(usage, "WINDOW_PIN", tmp_path / "no-pin.json")
    usage._WINDOW_CACHE.update({"at": None, "hours": None, "value": None})
    yield
    usage._WINDOW_CACHE.update({"at": None, "hours": None, "value": None})


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
    assert d["areas"][0] == {"id": "md", "name": "Product", "color": "#d95926", "group": "Product"}


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
    path = shell_api.ROOT / "areas.yaml"
    want = [a for a in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("areas") or []
            if isinstance(a, dict) and a.get("id")]
    assert la.load_areas(str(path)) == want


def test_flow_mapping_closed_by_bracket_raises():
    import pytest
    from ledger2 import areas as la
    with pytest.raises(ValueError):
        la.mini_yaml("k: [x, {y: 1]")


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
    assert t["via_helpers"] is True and t["live_helpers"][0]["name"] == "Builder"
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


# --- the usage window ----------------------------------------------------------------------------------
def line(ts, mid, rid, model, i, o, cr=0, cc=0, sidechain=False):
    rec = {"timestamp": ts.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"), "requestId": rid,
           "message": {"id": mid, "model": model, "usage": {"input_tokens": i, "output_tokens": o,
                                                           "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}}}
    if sidechain:
        rec["isSidechain"] = True
    return json.dumps(rec)


def write_transcript(tmp_path, lines, name="s1.jsonl", sub=None):
    d = tmp_path / "claude-projects" / "proj"
    if sub:
        d = d / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_window_by_model_counts_a_message_once_and_weights_per_model(tmp_path):
    now = dt.datetime.now().replace(microsecond=0)
    t = now - dt.timedelta(minutes=30)
    lines = [
        # one Opus message written as 3 lines (3 content blocks), usage repeated: counts once, max of each field
        line(t, "m1", "r1", "claude-opus-5-5", 100, 10), line(t, "m1", "r1", "claude-opus-5-5", 100, 400),
        line(t, "m1", "r1", "claude-opus-5-5", 100, 1000),
        line(t, "m2", "r2", "claude-sonnet-5-5", 200, 800),
        line(t, "m3", "r3", "claude-haiku-5-5", 1000, 3000),
        line(t, "m4", "r4", "claude-opus-5-5", 5, 5, sidechain=True),      # main-transcript sidechain: skipped
    ]
    write_transcript(tmp_path, lines)
    w = usage.window_by_model(now=now)
    assert w["ok"] and w["window_start"]
    # opus (100+1000) x 2.0, sonnet (200+800) x 1.0, haiku-5 (1000+3000) x 0.05
    assert w["by_model"] == {"opus": 2200, "sonnet": 1000, "haiku": 200}
    assert w["total"] == 3400


def test_window_by_model_dedupes_across_files_and_reads_subagents(tmp_path):
    now = dt.datetime.now().replace(microsecond=0)
    t = now - dt.timedelta(minutes=10)
    write_transcript(tmp_path, [line(t, "m1", "r1", "claude-sonnet-5-5", 10, 90)], "a.jsonl")
    write_transcript(tmp_path, [line(t, "m1", "r1", "claude-sonnet-5-5", 10, 90)], "b.jsonl")          # same message elsewhere
    write_transcript(tmp_path, [line(t, "h1", "hr1", "claude-sonnet-5-5", 0, 50, sidechain=True)], "agent-x.jsonl", sub="subagents")
    w = usage.window_by_model(now=now)
    assert w["by_model"]["sonnet"] == 100 + 50


def test_window_by_model_with_no_open_window(tmp_path):
    now = dt.datetime.now().replace(microsecond=0)
    old = now - dt.timedelta(hours=8)
    write_transcript(tmp_path, [line(old, "m1", "r1", "claude-opus-5-5", 10, 90)])
    w = usage.window_by_model(now=now)
    assert w["ok"] and w["total"] == 0 and w["window_start"] is None


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
    assert shell_api.area_of(areas, cwd, None, "start-of-day") == "md"
    assert shell_api.area_of(areas, cwd, None, "/daily-review") == "job"
    assert shell_api.area_of(areas, cwd, "train", "daily-review") == "train"
    assert shell_api.area_of(areas, cwd, "nope", None) == "atlas"       # unknown explicit area is ignored


def test_sessions_use_their_playbook_and_explicit_area(monkeypatch):
    snap = dict(SNAP, sessions=[dict(SNAP["sessions"][0], cwd="D:/AI/Atlas", playbook="start-of-day", area_arg=None),
                                dict(SNAP["sessions"][0], sid="z", cwd="D:/AI/Atlas", playbook=None, area_arg="job")])
    monkeypatch.setattr(shell_api, "_snapshot", lambda: snap)
    assert [s["area"] for s in shell_api.get("/api/v2/team", {})[1]["sessions"]] == ["md", "job"]


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
