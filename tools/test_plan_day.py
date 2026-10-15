"""tools/plan_day.py against a tiny HTTP server that routes /api/v2/plan to the real plan engine
(cockpit/plan/api.py) on temp copies of the fixture files. Never touches the live schedule.

Run:  py -3.12 -m pytest -q tools/test_plan_day.py
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qsl

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "cockpit"))
import plan_day as pd  # noqa: E402

DAY = "2027-06-07"


@pytest.fixture
def server(tmp_path, monkeypatch):
    sched = tmp_path / "schedule.yaml"
    shutil.copyfile(ROOT / "cockpit" / "plan" / "fixtures" / "schedule.yaml", sched)
    tasks = tmp_path / "tasks.yaml"
    shutil.copyfile(ROOT / "cockpit" / "plan" / "fixtures" / "tasks.yaml", tasks)
    areas = tmp_path / "areas.yaml"
    shutil.copyfile(ROOT / "cockpit" / "ledger2" / "fixtures" / "areas.yaml", areas)
    monkeypatch.setenv("ATLAS_SCHEDULE_FILE", str(sched))
    monkeypatch.setenv("ATLAS_TASKS_FILE", str(tasks))
    monkeypatch.setenv("ATLAS_PLAN_VERSION_FILE", str(tmp_path / "data" / "plan-version.json"))
    monkeypatch.setenv("ATLAS_AREAS_FILE", str(areas))
    monkeypatch.setenv("ATLAS_REGISTRY_FILE", str(tmp_path / "registry.yaml"))
    from plan import api, model
    monkeypatch.setattr(model, "_auto_runs", lambda a, b: [])
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, res):
            code, body = res
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            self._send(api.get(u.path, dict(parse_qsl(u.query))))

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            seen.append((self.path, body))
            self._send(api.post(urlparse(self.path).path, body))

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield type("S", (), {"url": f"http://127.0.0.1:{srv.server_address[1]}", "seen": seen, "sched": sched})
    srv.shutdown()


def run(server, capsys, *args):
    code = pd.main([args[0], "--url", server.url, *args[1:]])
    return code, capsys.readouterr()


def write_ops(tmp_path, ops):
    p = tmp_path / "ops.json"
    p.write_text(json.dumps(ops), encoding="utf-8")
    return str(p)


def test_build_request_maps_every_op():
    assert pd.build_request({"op": "add", "title": "T", "day": DAY, "start": "09:00", "end": "10:00", "area": "md",
                             "attention": "shared", "notes": "n"}) == (
        "event", {"event": {"title": "T", "day": DAY, "start": "09:00", "end": "10:00", "area": "md",
                            "attention": "shared", "notes": "n"}})
    assert pd.build_request({"op": "move", "id": "evt_ab@2027-06-14", "day": DAY, "start": "09:00", "end": "10:00"}) == (
        "event/move", {"id": "evt_ab", "day": DAY, "start": "09:00", "end": "10:00", "occurrence": "2027-06-14"})
    assert pd.build_request({"op": "delete", "id": "evt_ab"}) == ("event/delete", {"id": "evt_ab"})
    assert pd.build_request({"op": "delete", "id": "evt_ab@2027-06-14"}) == (
        "event/skip", {"id": "evt_ab", "occurrence": "2027-06-14"})
    assert pd.build_request({"op": "retitle", "id": "evt_ab", "title": "New"}) == (
        "event", {"event": {"id": "evt_ab", "title": "New"}})
    for bad in ({"op": "add", "title": "T"}, {"op": "nope"}, {"op": "move", "id": "x"}, {"op": "retitle", "id": "x"}):
        with pytest.raises(pd.PlanDayError):
            pd.build_request(bad)


def test_apply_add_show_move_retitle_delete(server, capsys, tmp_path):
    ops = [{"op": "add", "title": "Launch", "day": DAY, "start": "09:00", "end": "11:00", "area": "md"},
           {"op": "add", "title": "Atlas work", "day": DAY, "start": "10:00", "end": "12:00", "area": "atlas"}]
    code, out = run(server, capsys, "apply", write_ops(tmp_path, ops))
    assert code == 0 and "applied 2 of 2" in out.out
    code, out = run(server, capsys, "show", "--day", DAY)
    lines = [ln.split("\t") for ln in out.out.strip().splitlines()]
    assert [(ln[1], ln[2], ln[3], ln[5]) for ln in lines] == [("09:00-11:00", "PROD", "shared", "Launch"),
                                                              ("10:00-12:00", "OPS", "shared", "Atlas work")]
    eid = lines[0][4]
    ops = [{"op": "move", "id": eid, "day": DAY, "start": "13:00", "end": "14:00"},
           {"op": "retitle", "id": eid, "title": "Launch v2"}]
    assert run(server, capsys, "apply", write_ops(tmp_path, ops))[0] == 0
    out = run(server, capsys, "show", "--day", DAY)[1].out
    assert "13:00-14:00\tPROD\tshared\t" in out and "Launch v2" in out
    assert run(server, capsys, "apply", write_ops(tmp_path, [{"op": "delete", "id": eid}]))[0] == 0
    assert "Launch v2" not in run(server, capsys, "show", "--day", DAY)[1].out


def test_each_write_uses_a_fresh_base(server, capsys, tmp_path):
    ops = [{"op": "add", "title": f"E{i}", "day": DAY, "start": f"{9 + i:02d}:00", "end": f"{10 + i:02d}:00", "area": "md"}
           for i in range(3)]
    assert run(server, capsys, "apply", write_ops(tmp_path, ops))[0] == 0
    bases = [b["base"] for _, b in server.seen]
    assert bases == sorted(set(bases)) and len(bases) == 3          # a new version before every write


def test_stops_at_the_first_refusal_and_prints_the_reason(server, capsys, tmp_path):
    ops = [{"op": "add", "title": "Solo", "day": DAY, "start": "09:00", "end": "10:00", "area": "pers"},
           {"op": "add", "title": "Clash", "day": DAY, "start": "09:30", "end": "10:30", "area": "md"},
           {"op": "add", "title": "Never", "day": DAY, "start": "14:00", "end": "15:00", "area": "md"}]
    code, out = run(server, capsys, "apply", write_ops(tmp_path, ops))
    assert code == 1
    assert "applied 1 of 3" in out.out
    assert "can't save 'Clash': overlaps 'Solo' (09:00-10:00), which needs full attention" in out.out
    shown = run(server, capsys, "show", "--day", DAY)[1].out
    assert "Solo" in shown and "Never" not in shown


def test_dry_run_sends_nothing(server, capsys, tmp_path):
    ops = [{"op": "add", "title": "T", "day": DAY, "start": "09:00", "end": "10:00"}]
    code, out = run(server, capsys, "apply", write_ops(tmp_path, ops), "--dry-run")
    assert code == 0 and "POST /api/v2/plan/event" in out.out and server.seen == []


def test_recurring_occurrence_ids_are_shown_and_accepted(server, capsys, tmp_path):
    ops = [{"op": "add", "title": "Standup", "day": DAY, "start": "09:00", "end": "09:30", "area": "md"}]
    assert run(server, capsys, "apply", write_ops(tmp_path, ops))[0] == 0
    eid = run(server, capsys, "show", "--day", DAY)[1].out.split("\t")[4]
    r = pd._call(server.url, "POST", "/api/v2/plan/event",
                 {"base": pd.get_plan(server.url, DAY, DAY)["version"],
                  "event": {"id": eid, "recur": {"freq": "daily", "interval": 1}}})
    assert r[0] == 200
    out = run(server, capsys, "show", "--day", DAY, "--days", "2")[1].out
    assert f"{eid}@{DAY}" in out and f"{eid}@2027-06-08" in out
    ops = [{"op": "move", "id": f"{eid}@2027-06-08", "day": "2027-06-08", "start": "15:00", "end": "15:30"}]
    assert run(server, capsys, "apply", write_ops(tmp_path, ops))[0] == 0
    assert "2027-06-08\t15:00-15:30" in run(server, capsys, "show", "--day", "2027-06-08")[1].out
    assert run(server, capsys, "apply", write_ops(tmp_path, [{"op": "delete", "id": f"{eid}@2027-06-08"}]))[0] == 0
    assert "2027-06-08" not in run(server, capsys, "show", "--day", "2027-06-08")[1].out


def test_unreachable_server_is_exit_1(capsys, tmp_path):
    p = write_ops(tmp_path, [{"op": "delete", "id": "evt_x"}])
    assert pd.main(["apply", p, "--url", "http://127.0.0.1:9"]) == 1
    assert "cannot reach" in capsys.readouterr().err


def test_done_and_undone_ops(server, capsys, tmp_path):
    assert pd.build_request({"op": "done", "id": "evt_ab@2027-06-14"}) == (
        "event/done", {"id": "evt_ab", "done": True, "occurrence": "2027-06-14"})
    assert pd.build_request({"op": "undone", "id": "evt_ab"}) == ("event/done", {"id": "evt_ab", "done": False})
    with pytest.raises(pd.PlanDayError):
        pd.build_request({"op": "done"})
    ops = [{"op": "add", "title": "Marked", "day": DAY, "start": "09:00", "end": "10:00"}]
    assert run(server, capsys, "apply", write_ops(tmp_path, ops))[0] == 0
    eid = run(server, capsys, "show", "--day", DAY)[1].out.split("\t")[4]
    assert run(server, capsys, "apply", write_ops(tmp_path, [{"op": "done", "id": eid}]))[0] == 0
    line = run(server, capsys, "show", "--day", DAY)[1].out.strip().split("\n")[0]
    assert line.split("\t")[5:] == ["Marked", "done"]
    assert run(server, capsys, "apply", write_ops(tmp_path, [{"op": "undone", "id": eid}]))[0] == 0
    line = run(server, capsys, "show", "--day", DAY)[1].out.strip().split("\n")[0]
    assert line.split("\t")[5:] == ["Marked"]


def test_memo_and_place_ops_and_memos_command(server, capsys, tmp_path):
    assert pd.build_request({"op": "memo", "title": "Call bank", "area": "pers", "estimate_min": 30}) == (
        "task", {"task": {"area": "pers", "title": "Call bank", "owner": "me", "state": "todo", "estimate_min": 30}})
    assert pd.build_request({"op": "place", "task": "t_1", "day": DAY, "start": "09:00"}) == (
        "task/place", {"id": "t_1", "target": "me", "day": DAY, "start": "09:00"})
    for bad in ({"op": "memo", "title": "x"}, {"op": "memo", "area": "md"}, {"op": "place", "task": "t", "day": DAY}):
        with pytest.raises(pd.PlanDayError):
            pd.build_request(bad)
    ops = [{"op": "memo", "title": "Write pitch", "area": "md", "estimate_min": 90},
           {"op": "memo", "title": "No estimate", "area": "atlas"}]
    code, out = run(server, capsys, "apply", write_ops(tmp_path, ops))
    assert code == 0 and "1. memo ok t_" in out.out
    rows = [ln.split("\t") for ln in run(server, capsys, "memos")[1].out.strip().splitlines()]
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [("MD", "90", "open", "Write pitch"), ("ATL", "", "open", "No estimate")]
    pitch, bare = rows[0][0], rows[1][0]
    # end defaults to start + estimate (90 min), or 60 min when the task has none
    ops = [{"op": "place", "task": pitch, "day": DAY, "start": "09:00"},
           {"op": "place", "task": bare, "day": DAY, "start": "13:00"}]
    assert run(server, capsys, "apply", write_ops(tmp_path, ops))[0] == 0
    shown = run(server, capsys, "show", "--day", DAY)[1].out
    assert "09:00-10:30\tMD" in shown and "13:00-14:00\tATL" in shown
    rows = [ln.split("\t") for ln in run(server, capsys, "memos")[1].out.strip().splitlines()]
    assert [r[3] for r in rows] == [f"planned {DAY} 09:00", f"planned {DAY} 13:00"]
    # an explicit end is kept; placing again moves the same event
    ops = [{"op": "place", "task": pitch, "day": DAY, "start": "15:00", "end": "15:30"}]
    assert run(server, capsys, "apply", write_ops(tmp_path, ops))[0] == 0
    assert run(server, capsys, "show", "--day", DAY)[1].out.count("Write pitch") == 1
    # a done event takes the memo off the list
    eid = [ln.split("\t")[4] for ln in run(server, capsys, "show", "--day", DAY)[1].out.splitlines() if "Write pitch" in ln][0]
    assert run(server, capsys, "apply", write_ops(tmp_path, [{"op": "done", "id": eid}]))[0] == 0
    assert [ln.split("\t")[4] for ln in run(server, capsys, "memos")[1].out.strip().splitlines()] == ["No estimate"]


def test_place_refusal_is_printed_and_dry_run_shows_the_end_rule(server, capsys, tmp_path):
    ops = [{"op": "place", "task": "t_missing", "day": DAY, "start": "09:00"}]
    code, out = run(server, capsys, "apply", write_ops(tmp_path, ops))
    assert code == 1 and "unknown task" in out.out
    n = len(server.seen)
    code, out = run(server, capsys, "apply", write_ops(tmp_path, ops), "--dry-run")
    assert code == 0 and "task/place" in out.out and "start + estimate_min" in out.out and len(server.seen) == n
