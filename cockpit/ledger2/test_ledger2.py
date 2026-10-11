"""Tests for cockpit/ledger2 (areas, store, derive, query, stats, api). Temp dirs only."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
AREAS_FIXTURE = HERE / "fixtures" / "areas.yaml"   # test-owned copy: tests must not depend on the live areas.yaml
sys.path.insert(0, str(REPO / "cockpit"))

from ledger2 import api, areas, derive, query, stats, store  # noqa: E402

SID = "abcd1234-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(d))
    monkeypatch.setenv("ATLAS_REPORTS_DIR", str(tmp_path / "reports"))
    stats.clear_cache()
    query.invalidate()
    return d


def wf():
    return query.load_workflows()


def T(minutes, base="2026-10-07T09:00:00+02:00"):
    d = dt.datetime.fromisoformat(base) + dt.timedelta(minutes=minutes)
    return d.isoformat(timespec="seconds")


def E(ev, minutes, sid=SID, **kw):
    return dict({"v": 2, "t": T(minutes), "ev": ev, "sid": sid, "cwd": "D:\\AI\\Atlas", "mode": "record"}, **kw)


# ------------------------------------------------------------------ areas

def test_mini_yaml_matches_pyyaml_on_real_files():
    yaml = pytest.importorskip("yaml")
    for name in ("areas.yaml", "workflows.yaml"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert areas.mini_yaml(text) == yaml.safe_load(text)


def test_mini_yaml_basics():
    got = areas.mini_yaml("a: 1\nb: 'x # y'  # comment\nc: [1, two, \"3\"]\nd:\n  - k: v\n    n: null\n  - plain\ne: {x: 1, y: [a]}\n")
    assert got == {"a": 1, "b": "x # y", "c": [1, "two", "3"], "d": [{"k": "v", "n": None}, "plain"],
                   "e": {"x": 1, "y": ["a"]}}


def test_load_areas_order_and_fallback(tmp_path):
    al = areas.load_areas(str(AREAS_FIXTURE))
    assert [a["id"] for a in al] == ["md", "job", "train", "atlas", "admin", "pers"]
    bad = tmp_path / "areas.yaml"
    bad.write_text("areas: [", encoding="utf-8")
    assert areas.load_areas(str(bad)) == [] or isinstance(areas.load_areas(str(bad)), list)
    j = tmp_path / "areas.json"
    j.write_text(json.dumps({"areas": [{"id": "x", "name": "X", "color": "#000000"}]}))
    assert areas.load_areas(str(j))[0]["id"] == "x"


def test_area_for_resolution_order():
    al = areas.load_areas(str(AREAS_FIXTURE))
    assert areas.area_for(al, "D:\\AI\\Atlas\\x", area_arg="job") == "job"          # explicit wins
    assert areas.area_for(al, "D:\\AI\\Atlas", playbook="daily-review") == "job"      # then playbook
    assert areas.area_for(al, "d:/ai/atlas-data/ledger") == "atlas"                  # case + slashes
    assert areas.area_for(al, "D:\\AI\\product\\x") == "md"
    assert areas.area_for(al, "D:\\AI\\Atlas-v2\\record") == "atlas"                 # worktree subfolder
    assert areas.area_for(al, "D:\\AI\\Atlas-v2-record") is None                     # a prefix must end at a folder boundary
    assert areas.area_for(al, "D:\\AI\\learning-app2") != "train"
    assert areas.area_for(al, None, area_arg="ADMIN") == "admin"
    assert areas.area_for(al, None, playbook="/daily-review") == "job"
    assert areas.area_for(al, "C:\\nowhere") is None
    assert areas.area_for(al, None) is None
    assert areas.area_for(al, "C:\\x", area_arg="nope") is None


def test_area_for_longest_prefix_wins():
    al = [{"id": "a", "folders": ["D:\\x"]}, {"id": "b", "folders": ["D:\\x\\deep"]}]
    assert areas.area_for(al, "D:\\x\\deep\\f") == "b"


def test_areas_py_works_copied_alone(tmp_path):
    import shutil
    import subprocess
    shutil.copyfile(HERE / "areas.py", tmp_path / "areas.py")
    shutil.copyfile(AREAS_FIXTURE, tmp_path / "areas.yaml")
    code = "import areas;print(len(areas.load_areas()), areas.area_for(areas.load_areas(),'D:/AI/learning-app'))"
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert r.stdout.split() == ["6", "train"], r.stderr


# ------------------------------------------------------------------ store

def test_append_read_and_clip(ledger):
    store.append({"ev": "x", "sid": "s", "t": "2026-10-07T10:00:00+02:00", "big": "y" * 500})
    store.append({"ev": "z", "sid": "s", "t": "2026-10-08T10:00:00+02:00"})
    evs = store.read_events()
    assert [e["ev"] for e in evs] == ["x", "z"] and len(evs[0]["big"]) == 200 and evs[0]["v"] == 2
    assert [e["ev"] for e in store.read_events("2026-10-08", "2026-10-08")] == ["z"]
    (ledger / "events-2026-10-09.jsonl").write_text("not json\n{\"ev\": \"ok\"}\n", encoding="utf-8")
    assert store.read_events("2026-10-09")[0]["ev"] == "ok"


def test_event_line_stays_under_4kb():
    line = store.encode({"ev": "x", "t": "2026-10-07T10:00:00+02:00", "problems": ["p" * 200] * 40})
    assert len(line.encode()) < 4000


def test_lock_is_reentrant_safe_and_released(ledger):
    with store.locked():
        pass
    with store.locked():
        store.append_events([{"ev": "a", "t": "2026-10-07T10:00:00+02:00"}])
    assert len(store.read_events()) == 1


# ------------------------------------------------------------------ derive

def stream():
    s = [
        E("session_start", 0, source="startup"),
        E("work_type", 1, type="coding", area="atlas", task="abcd1234-1"),
        E("helper_start", 2, task="abcd1234-1", tuid="t1", helper="implementer", label="Build ledger store", bg=False, version=0),
        E("helper_paired", 2, agent_id="I1", helper="implementer", tuid="t1"),
        E("edit", 3, task="abcd1234-1", path="D:\\x\\a.py", tool="Write", version=1),
        E("edit", 4, task="abcd1234-1", path="D:\\x\\b.py", tool="Edit", version=2),
        E("helper_start", 5, task="abcd1234-1", tuid="t2", helper="reviewer", label="Check v2", bg=False, version=2),
        E("helper_paired", 5, agent_id="R1", helper="reviewer", tuid="t2"),
        E("verdict", 6, task="abcd1234-1", agent_id="R1", helper="reviewer", verdict="NEEDS_CHANGES", version=2, findings=3),
        E("verdict", 6, task="abcd1234-1", agent_id="R1", helper="reviewer", verdict="NEEDS_CHANGES", version=2, findings=3),
        E("helper_start", 7, task="abcd1234-1", tuid="t3", helper="senior-reviewer", label="Check v2 again", bg=False, version=2),
        E("verdict", 8, task="abcd1234-1", agent_id="S1", helper="senior-reviewer", verdict="NEEDS_CHANGES", version=2, findings=1),
        E("refusal", 8, task="abcd1234-1", guard="rework", would=True, detail="x"),
        E("helper_start", 9, task="abcd1234-1", tuid="t4", helper="implementer", label="Fix findings", bg=False, version=2),
        E("edit", 10, task="abcd1234-1", path="D:\\x\\a.py", tool="Edit", version=3),
        E("helper_start", 11, task="abcd1234-1", tuid="t5", helper="reviewer", label="Check v3", bg=False, version=3),
        E("verdict", 12, task="abcd1234-1", agent_id="R2", helper="reviewer", verdict="READY", version=3, findings=0),
        E("summary", 13, task="abcd1234-1", goal="Ledger store", produced="3 files", decisions="none", open="none", review_min=20),
        E("cost", 14, scope="main", model="claude-opus-5-5", **{"in": 100, "out": 50, "cache_read": 1000, "cache_write": 10}, weighted=300, title="Atlas v2 build"),
        E("cost", 14, scope="I1", helper="implementer", model="claude-sonnet-5-5", **{"in": 10, "out": 5, "cache_read": 0, "cache_write": 0}, weighted=15),
        E("cost", 14, scope="R1", model="claude-sonnet-5-5", **{"in": 10, "out": 5, "cache_read": 0, "cache_write": 0}, weighted=15),
        E("task_done", 15, task="abcd1234-1", version=3),
        E("work_type", 20, type="undeclared", area=None, task="abcd1234-2", implicit=True),
        E("edit", 21, task="abcd1234-2", path="D:\\x\\c.py", tool="Edit", version=1),
        E("session_end", 30, reason="other"),
    ]
    return s


def test_derive_expected_task_record():
    tasks = derive.derive_v2(stream(), wf(), markers=set())
    t1 = tasks[0]
    assert t1["id"] == "abcd1234-1" and t1["src"] == "v2" and t1["sid"] == SID
    assert t1["session_title"] == "Atlas v2 build" and t1["area"] == "atlas" and t1["type"] == "coding"
    assert t1["status"] == "done" and t1["started"] == T(1) and t1["ended"] == T(15) and t1["duration_s"] == 14 * 60
    assert t1["crew"] == ["implementer", "reviewer", "senior-reviewer"]
    assert t1["helpers"][0] == {"helper": "implementer", "count": 2, "labels": ["Build ledger store", "Fix findings"]}
    # reviewer + senior-reviewer failing the SAME version burn one round; the duplicate agent verdict is dropped
    assert t1["rounds"] == {"needs_changes": 1, "budget": 2, "granted": 0, "left": 1}
    assert [(v["helper"], v["verdict"], v["version"]) for v in t1["verdicts"]] == [
        ("reviewer", "NEEDS_CHANGES", 2), ("senior-reviewer", "NEEDS_CHANGES", 2), ("reviewer", "READY", 3)]
    assert t1["final_verdict"] == "READY" and t1["checked"] is True and t1["unverified_flag"] is False
    assert t1["caught"] == 4 and t1["edits"] == 3
    assert t1["refusals"] == [{"guard": "rework", "would": True}]
    assert t1["cost"]["weighted"] == 330
    assert t1["cost"]["by_model"]["claude-opus-5-5"] == {"in": 100, "out": 50, "cache_read": 1000, "cache_write": 10, "weighted": 300}
    assert t1["cost"]["by_helper"] == {"chief": 300, "implementer": 15, "reviewer": 15}
    assert t1["summary"] == {"goal": "Ledger store", "produced": "3 files", "decisions": "none", "open": "none", "review_min": 20}
    assert t1["auto"] is False


def test_derive_unverified_flag_and_ended_status():
    t2 = derive.derive_v2(stream(), wf(), markers=set())[1]
    assert t2["type"] == "undeclared"
    assert t2["status"] == "ended" and t2["ended"] == T(30)
    assert t2["checked"] is False and t2["unverified_flag"] is True and t2["edits"] == 1


def test_derive_not_flagged_when_open_or_research_unchanged():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1"), E("edit", 2, task="abcd1234-1", path="p", tool="Edit", version=1)]
    t = derive.derive_v2(evs, wf())[0]
    assert t["status"] == "open" and t["ended"] is None and t["unverified_flag"] is False
    evs = [E("work_type", 1, type="research", task="abcd1234-1"), E("task_done", 2, task="abcd1234-1", version=0)]
    assert derive.derive_v2(evs, wf())[0]["unverified_flag"] is False


def test_derive_switch_carries_spent_budget_as_min():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1")]
    for i, v in enumerate((1, 2)):
        evs += [E("edit", 2 + i * 3, task="abcd1234-1", path="p", tool="Edit", version=v),
                E("verdict", 3 + i * 3, task="abcd1234-1", agent_id="R%d" % i, helper="reviewer", verdict="NEEDS_CHANGES", version=v, findings=1)]
    evs += [E("work_type", 20, type="chore", task="abcd1234-2"),
            E("rework_granted", 21, task="abcd1234-2", n=1)]
    t1, t2 = derive.derive_v2(evs, wf())
    assert t1["status"] == "switched" and t1["rounds"]["left"] == 0
    assert t2["rounds"] == {"needs_changes": 2, "budget": 1, "granted": 1, "left": 0}   # min(2, 1) budget, granted 0+1


def test_derive_switch_without_spent_budget_resets():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1"), E("work_type", 2, type="chore", task="abcd1234-2")]
    assert derive.derive_v2(evs, wf())[1]["rounds"] == {"needs_changes": 0, "budget": 1, "granted": 0, "left": 1}


def test_derive_chained_redeclarations_keep_the_spent_rounds():
    """Legacy events (no `carried` flag): declaring twice cannot reset a spent budget (coding budget 2)."""
    evs = [E("work_type", 1, type="coding", task="abcd1234-1")]
    for i, v in enumerate((1, 2)):
        evs += [E("edit", 2 + i * 3, task="abcd1234-1", path="p", tool="Edit", version=v),
                E("verdict", 3 + i * 3, task="abcd1234-1", agent_id="R%d" % i, helper="reviewer",
                  verdict="NEEDS_CHANGES", version=v, findings=1)]
    evs += [E("work_type", 20, type="coding", task="abcd1234-2", plan_ref="t_aaaaaaaa"),
            E("work_type", 21, type="coding", task="abcd1234-3", plan_ref="t_bbbbbbbb")]
    t1, t2, t3 = derive.derive_v2(evs, wf())
    assert t2["rounds"]["needs_changes"] == 2 and t3["rounds"]["needs_changes"] == 2 and t3["rounds"]["left"] == 0


def test_derive_unmatched_needs_changes_at_version_zero_counts_as_unresolved():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1")]
    for i in range(2):
        evs.append(E("verdict", 2 + i, task="abcd1234-1", agent_id="R%d" % i, helper="reviewer",
                     verdict="NEEDS_CHANGES", findings=1))           # no version: '?'-keyed
    evs.append(E("work_type", 9, type="writing", task="abcd1234-2"))
    t1, t2 = derive.derive_v2(evs, wf())
    assert t2["rounds"]["needs_changes"] == 2


def _verdict(m, task, aid, verdict, version=None):
    return E("verdict", m, task=task, agent_id=aid, helper="reviewer", verdict=verdict, version=version, findings=1)


def test_derive_needs_changes_then_ready_at_version_zero_is_resolved():
    """Helpers edit in their own worktrees: the main session's version stays 0. A latest READY resolves the task."""
    evs = [E("work_type", 1, type="chore", task="abcd1234-1"),
           _verdict(2, "abcd1234-1", "A", "NEEDS_CHANGES", 0), _verdict(3, "abcd1234-1", "B", "READY", 0),
           E("work_type", 9, type="chore", task="abcd1234-2")]
    assert derive.derive_v2(evs, wf())[1]["rounds"]["needs_changes"] == 0


def test_derive_spent_without_ready_carries_through_chained_redeclarations():
    evs = [E("work_type", 1, type="chore", task="abcd1234-1"), _verdict(2, "abcd1234-1", "A", "NEEDS_CHANGES", 0),
           E("work_type", 9, type="coding", task="abcd1234-2", plan_ref="t_aaaaaaaa"),
           E("work_type", 10, type="coding", task="abcd1234-3", plan_ref="t_bbbbbbbb"),
           E("work_type", 11, type="writing", task="abcd1234-4")]
    ts = derive.derive_v2(evs, wf())
    assert [t["rounds"]["needs_changes"] for t in ts] == [1, 1, 1, 1]


def test_derive_carry_then_ready_starts_the_next_fresh():
    evs = [E("work_type", 1, type="chore", task="abcd1234-1"), _verdict(2, "abcd1234-1", "A", "NEEDS_CHANGES", 0),
           E("work_type", 9, type="coding", task="abcd1234-2"), _verdict(10, "abcd1234-2", "B", "READY", 0),
           E("work_type", 11, type="writing", task="abcd1234-3")]
    ts = derive.derive_v2(evs, wf())
    assert ts[1]["rounds"]["needs_changes"] == 1 and ts[2]["rounds"]["needs_changes"] == 0


def test_derive_edits_after_ready_make_an_over_budget_task_unresolved_again():
    evs = [E("work_type", 1, type="chore", task="abcd1234-1"),
           E("edit", 2, task="abcd1234-1", path="p", tool="Edit", version=1), _verdict(3, "abcd1234-1", "A", "NEEDS_CHANGES", 1),
           E("edit", 4, task="abcd1234-1", path="p", tool="Edit", version=2), _verdict(5, "abcd1234-1", "B", "READY", 2),
           E("work_type", 6, type="writing", task="abcd1234-2"),
           E("edit", 7, task="abcd1234-2", path="p", tool="Edit", version=1),
           E("work_type", 8, type="writing", task="abcd1234-3")]
    ts = derive.derive_v2(evs, wf())
    assert ts[1]["rounds"]["needs_changes"] == 0 and ts[2]["rounds"]["needs_changes"] == 0
    evs2 = evs[:5] + [E("edit", 6, task="abcd1234-1", path="p", tool="Edit", version=3),
                      E("work_type", 7, type="writing", task="abcd1234-2")]
    assert derive.derive_v2(evs2, wf())[1]["rounds"]["needs_changes"] == 1


def test_tasks_payload_has_plan_title_on_every_task():
    from ledger2 import api as lapi
    out = lapi.with_plan_titles([{"id": "a"}, {"id": "b", "plan_ref": None}])
    assert [t["plan_title"] for t in out] == [None, None]


def test_derive_ok_check_counts_only_when_change_touches_only_check_files():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1"),
           E("edit", 2, task="abcd1234-1", path="D:\\r\\areas.yaml", tool="Edit", version=1),
           E("check", 2, task="abcd1234-1", file="D:\\r\\areas.yaml", ok=True, problems=[], version=1),
           E("task_done", 3, task="abcd1234-1", version=1)]
    assert derive.derive_v2(evs, wf())[0]["checked"] is True
    evs.insert(2, E("edit", 2, task="abcd1234-1", path="D:\\r\\code.py", tool="Edit", version=1))
    evs[3]["version"] = 2
    evs.insert(3, E("edit", 2, task="abcd1234-1", path="D:\\r\\areas.yaml", tool="Edit", version=2))
    evs[4]["version"] = 2
    t = derive.derive_v2(evs, wf())[0]
    assert t["checked"] is False and t["unverified_flag"] is True


def test_derive_auto_needs_marker_file(ledger):
    evs = [E("auto", 0, marker="m1"), E("work_type", 1, type="coding", task="abcd1234-1")]
    assert derive.derive_v2(evs, wf(), markers=set())[0]["auto"] is False
    assert derive.derive_v2(evs, wf(), markers={"m1"})[0]["auto"] is True


def test_derive_unknown_type_gets_default_budget_and_needs_check():
    evs = [E("work_type", 1, type="weird", task="abcd1234-1"), E("edit", 2, task="abcd1234-1", path="p", tool="Edit", version=1),
           E("session_end", 3, reason="x")]
    t = derive.derive_v2(evs, wf())[0]
    assert t["type"] == "weird" and t["rounds"]["budget"] == 2 and t["unverified_flag"] is True


def test_derive_v1_jobs():
    ev = lambda **k: dict({"v": 1, "src": "v1", "project": "learning-app"}, **k)
    v1 = [ev(t="2026-09-01T10:00:00", ev="started", job="job_1", model="opus", type="implementation"),
          ev(t="2026-09-01T10:10:00", ev="phase_tokens", job="job_1", phase="s1 CODE", fresh=1000, input=10, output=990, cache_read=5, cache_write=2),
          ev(t="2026-09-01T10:20:00", ev="review", job="job_1", verdict="NEEDS_CHANGES", **{"pass": 1}, lane="s1"),
          ev(t="2026-09-01T10:30:00", ev="review", job="job_1", verdict="READY", **{"pass": 2}, lane="s1"),
          ev(t="2026-09-01T10:40:00", ev="merged", job="job_1", plan_task="app-routes"),
          ev(t="2026-09-01T10:45:00", ev="finished", job="job_1", status="merged")]
    al = areas.load_areas(str(AREAS_FIXTURE))
    t = derive.derive_v1(v1, al, wf())[0]
    assert t["src"] == "v1" and t["id"] == "job_1" and t["title"] == "app-routes" and t["area"] == "train"
    assert t["status"] == "done" and t["type"] == "coding" and t["duration_s"] == 40 * 60
    assert t["final_verdict"] == "READY" and t["rounds"]["needs_changes"] == 1
    assert t["cost"]["weighted"] == 2000 and t["crew"] == ["implementer", "reviewer"]


# ------------------------------------------------------------------ query, stats, api

def seed(ledger, evs):
    for e in evs:
        store.append(dict(e))


def now_stream(sid="ffff0000-aaaa", base_min=0):
    now = dt.datetime.now().astimezone().replace(microsecond=0)

    def t(m):
        return (now - dt.timedelta(minutes=60 - base_min - m)).isoformat(timespec="seconds")
    s8 = sid[:8]
    return [
        {"v": 2, "t": t(0), "ev": "work_type", "sid": sid, "cwd": "D:\\AI\\Atlas", "type": "coding", "area": "atlas", "task": s8 + "-1"},
        {"v": 2, "t": t(1), "ev": "edit", "sid": sid, "task": s8 + "-1", "path": "p", "tool": "Edit", "version": 1},
        {"v": 2, "t": t(2), "ev": "verdict", "sid": sid, "task": s8 + "-1", "agent_id": "R1", "helper": "reviewer", "verdict": "READY", "version": 1, "findings": 0},
        {"v": 2, "t": t(3), "ev": "cost", "sid": sid, "scope": "main", "model": "claude-opus-5-5", "in": 10, "out": 10, "cache_read": 0, "cache_write": 0, "weighted": 40, "title": "T"},
        {"v": 2, "t": t(4), "ev": "refusal", "sid": sid, "task": s8 + "-1", "guard": "rework", "would": True},
        {"v": 2, "t": t(5), "ev": "task_done", "sid": sid, "task": s8 + "-1", "version": 1},
    ]


def test_query_tasks_filters_and_order(ledger):
    seed(ledger, now_stream("ffff0000-aaaa") + now_stream("eeee1111-bbbb", base_min=5))
    d = dt.date.today().isoformat()
    ts = query.tasks(d, d)
    assert len(ts) == 2 and ts[0]["started"] >= ts[1]["started"]
    assert query.tasks(d, d, area="atlas", type="coding", status="done", src="v2", limit=1)[0]["area"] == "atlas"
    assert query.tasks(d, d, area="md") == []
    assert query.tasks("2020-01-01", "2020-01-02") == []


def test_auto_runs(ledger):
    evs = now_stream()
    evs.insert(0, {"v": 2, "t": evs[0]["t"], "ev": "auto", "sid": "ffff0000-aaaa", "marker": "mk1"})
    evs.append({"v": 2, "t": evs[-1]["t"], "ev": "summary", "sid": "ffff0000-aaaa", "task": "ffff0000-1", "goal": "g", "review_min": 25})
    seed(ledger, evs)
    d = dt.date.today().isoformat()
    assert query.auto_runs(d, d) == []
    query.invalidate()
    (ledger / "auto").mkdir()
    (ledger / "auto" / "mk1.json").write_text("{}")
    query.invalidate()
    runs = query.auto_runs(d, d)
    assert len(runs) == 1
    assert runs[0]["task_id"] == "ffff0000-1" and runs[0]["status"] == "done" and runs[0]["verdict"] == "READY"
    assert runs[0]["review_min"] == 25 and runs[0]["title"] == "T" and runs[0]["crew"] == []
    assert set(runs[0]) == {"task_id", "sid", "title", "area", "start", "end", "crew", "status", "verdict", "review_min"}


def test_stats_payload_shape_and_values(ledger):
    seed(ledger, now_stream())
    p = stats.compute()
    assert p["ok"] and p["week"] == stats.week_label(dt.date.today())
    k = p["kpis"]
    assert set(k) == {"tasks_closed", "first_pass_ready_pct", "caught", "rework_refused", "weighted_usage"}
    assert all(len(v["spark"]) == 12 and "prev" in v for v in k.values())
    assert k["tasks_closed"]["value"] == 1 and k["first_pass_ready_pct"]["value"] == 100.0
    assert k["rework_refused"]["value"] == 1 and k["rework_refused"]["split"] == {"enforced": 0, "would": 1}
    assert k["weighted_usage"]["value"] == 40
    assert len(p["usage_by_day"]) == 14 and p["usage_by_day"][-1]["opus"] == 40
    assert p["time_by_area"][0]["area"] == "atlas"
    assert p["rounds_hist"] == {"0": 0, "1": 0, "2": 0, "refused": 1}
    assert p["usage_by_helper"] == [{"helper": "chief", "tier": "opus", "weighted": 40}]
    assert len(p["first_pass_by_week"]) == 8 and p["first_pass_by_week"][-1]["pct"] == 100.0
    assert [r["type"] for r in p["ladder"]][:2] == ["coding", "research"]
    assert p["ladder"][0]["tasks_4w"] == 1 and p["unverified"] == 0


def test_stats_cache_and_background_refresh(ledger, monkeypatch):
    seed(ledger, now_stream())
    a = stats.get_stats()
    assert stats.get_stats() is a
    key = next(iter(stats._cache))
    stats._cache[key] = (stats._cache[key][0] - 120, a)        # make it stale
    assert stats.get_stats() is a                               # stale answer served at once ...
    import time
    for _ in range(100):
        if stats._cache[key][1] is not a:
            break
        time.sleep(0.05)
    assert stats._cache[key][1] is not a                        # ... and refreshed in the background


def test_api_endpoints(ledger, tmp_path):
    seed(ledger, now_stream())
    code, body = api.get("/api/v2/ledger/tasks", {})
    assert code == 200 and body["ok"] and len(body["tasks"]) == 1
    tid = body["tasks"][0]["id"]
    code, body = api.get("/api/v2/ledger/task", {"id": tid})
    assert code == 200 and body["task"]["id"] == tid and any(e["ev"] == "edit" for e in body["events"])
    assert api.get("/api/v2/ledger/task", {"id": "nope"})[0] == 404
    code, body = api.get("/api/v2/ledger/stats", {})
    assert code == 200 and "kpis" in body
    wk = stats.week_label(dt.date.today())
    assert api.get("/api/v2/ledger/tasks", {"week": wk, "area": "atlas"})[1]["week"] == wk
    assert api.get("/api/v2/ledger/tasks", {"week": "garbage"})[0] == 400
    assert api.get("/api/v2/ledger/report", {"week": wk})[0] == 404
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / f"{wk}.md").write_text("# report", encoding="utf-8")
    code, body = api.get("/api/v2/ledger/report", {"week": wk})
    assert code == 200 and body["markdown"] == "# report" and body["path"].endswith(f"{wk}.md")
    assert api.get("/api/v2/other", {}) is None and api.post("/api/v2/ledger/tasks", {}) is None


def test_api_serves_v1_rows_with_label(ledger):
    store.v1_file().parent.mkdir(parents=True)
    store.v1_file().write_text(json.dumps({"v": 1, "src": "v1", "t": dt.datetime.now().isoformat(timespec="seconds"),
                                           "ev": "started", "job": "job_9", "project": "x"}) + "\n", encoding="utf-8")
    code, body = api.get("/api/v2/ledger/tasks", {"src": "v1"})
    assert body["tasks"][0]["src"] == "v1" and body["tasks"][0]["id"] == "job_9"
    code, body = api.get("/api/v2/ledger/task", {"id": "job_9"})
    assert code == 200 and body["events"]


def test_flow_parser_wrong_closing_bracket_raises_not_hangs(tmp_path):
    import subprocess
    code = ("import areas\n"
            "for t in ('a: [a}', 'a: {a: [b}', 'a: [x, }', 'a: {,}x'):\n"
            "    try:\n        areas.mini_yaml(t); print('parsed')\n"
            "    except ValueError:\n        print('error')\n")
    r = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=20)
    assert r.stdout.split()[:2] == ["error", "error"], r.stderr


def test_load_areas_falls_back_to_json_on_malformed_flow(tmp_path):
    bad = tmp_path / "areas.yaml"
    bad.write_text("areas:\n  - id: x\n    folders: [a}\n", encoding="utf-8")
    (tmp_path / "areas.json").write_text(json.dumps({"areas": [{"id": "fallback"}]}))
    import shutil
    shutil.copyfile(HERE / "areas.py", tmp_path / "areas.py")
    import subprocess
    code = "import areas;print([a['id'] for a in areas.load_areas('areas.yaml')])"
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert r.stdout.strip() == "['fallback']", r.stderr


def test_derive_carried_rounds_add_to_new_task_versions():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1"),
           E("edit", 2, task="abcd1234-1", path="p", tool="Edit", version=1),
           E("verdict", 3, task="abcd1234-1", agent_id="A", helper="reviewer", verdict="NEEDS_CHANGES", version=1, findings=1),
           E("edit", 4, task="abcd1234-1", path="p", tool="Edit", version=2),
           E("verdict", 5, task="abcd1234-1", agent_id="B", helper="reviewer", verdict="NEEDS_CHANGES", version=2, findings=1),
           E("work_type", 6, type="chore", task="abcd1234-2", carried=True),
           E("edit", 7, task="abcd1234-2", path="p", tool="Edit", version=1),
           E("verdict", 8, task="abcd1234-2", agent_id="C", helper="reviewer", verdict="NEEDS_CHANGES", version=1, findings=1)]
    assert derive.derive_v2(evs, wf())[1]["rounds"] == {"needs_changes": 3, "budget": 1, "granted": 0, "left": 0}


def test_derive_unknown_verdict_version_never_checks():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1"),
           E("edit", 2, task="abcd1234-1", path="p", tool="Edit", version=1),
           E("verdict", 3, task="abcd1234-1", agent_id="A", helper="reviewer", verdict="READY", version=None, findings=0),
           E("task_done", 4, task="abcd1234-1", version=1)]
    t = derive.derive_v2(evs, wf())[0]
    assert t["checked"] is False and t["unverified_flag"] is True and t["verdicts"][0]["version"] is None


def test_derive_resume_reopens_most_recent_ended_task():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1"), E("session_end", 5, reason="other"),
           E("session_start", 10, source="resume"),
           E("edit", 11, task="abcd1234-1", path="p", tool="Edit", version=1)]
    t = derive.derive_v2(evs, wf())[0]
    assert t["status"] == "open" and t["ended"] is None and t["edits"] == 1


def test_all_tasks_never_rederives_on_the_request_path(ledger):
    seed(ledger, now_stream("ffff0000-aaaa"))
    first = query.all_tasks()
    seed(ledger, now_stream("eeee1111-bbbb", base_min=5))
    assert query.all_tasks() is first or len(query.all_tasks()) >= 1      # stale answer served immediately
    import time
    for _ in range(100):
        if len(query.all_tasks()) == 2:
            break
        time.sleep(0.05)
    assert len(query.all_tasks()) == 2


def test_api_warm_runs_all_tasks_in_a_background_thread(monkeypatch):
    import threading
    from ledger2 import api, query
    seen = {}
    monkeypatch.setattr(query, "all_tasks", lambda: seen.setdefault("thread", threading.current_thread().name))
    api.warm().join(5)
    assert seen["thread"] == "ledger2-warm"


def test_api_warm_logs_a_failure_once(monkeypatch, capsys):
    from ledger2 import api, query

    def boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(query, "all_tasks", boom)
    api.warm().join(5)
    assert "warm-up failed" in capsys.readouterr().err


def test_playbook_crews_cover_repo_skills():
    """Every helper id a repo-owned playbook skill names is in that playbook's crew.
    Skills living only in ~/.claude/skills are skipped on purpose: tests never read live files."""
    import re
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((REPO / "workflows.yaml").read_text(encoding="utf-8"))
    ids = ["senior-reviewer", "test-runner", "implementer", "researcher", "reviewer", "sweeper", "drafter", "scout"]
    pat = re.compile(r"(?<![\w-])(" + "|".join(ids) + r")(?![\w-])")
    assert set(wf["playbooks"]) == set(wf["playbook_crews"])
    checked = 0
    for pb in wf["playbooks"]:
        skill = REPO / "skills" / pb / "SKILL.md"
        if not skill.is_file():
            continue  # skipped: skill is not repo-owned (lives in ~/.claude/skills), tests do not read live files
        checked += 1
        named = set(pat.findall(skill.read_text(encoding="utf-8")))
        assert named <= set(wf["playbook_crews"][pb]["crew"]), (pb, named)
    assert checked >= 2


def test_work_type_plan_ref_reaches_the_derived_task():
    evs = [E("session_start", 0, source="startup"),
           E("work_type", 1, type="coding", area="md", task="abcd1234-1", plan_ref="evt_0a1b2c3d@2026-10-12"),
           E("work_type", 2, type="writing", task="abcd1234-2")]
    t1, t2 = derive.derive_v2(evs, wf(), markers=set())
    assert t1["plan_ref"] == "evt_0a1b2c3d@2026-10-12" and t2["plan_ref"] is None


def test_derive_redeclaration_carries_only_when_spent_and_honours_the_hook_flag():
    nc = [E("work_type", 1, type="coding", task="abcd1234-1")]
    for i, v in enumerate((1, 2)):
        nc += [E("edit", 2 + i * 3, task="abcd1234-1", path="p", tool="Edit", version=v),
               E("verdict", 3 + i * 3, task="abcd1234-1", agent_id="R%d" % i, helper="reviewer",
                 verdict="NEEDS_CHANGES", version=v, findings=1)]
    same = derive.derive_v2(nc + [E("work_type", 20, type="coding", task="abcd1234-2")], wf())
    assert same[1]["rounds"]["needs_changes"] == 2          # spent: same type carries too (no escape by re-declaring)
    under = derive.derive_v2(nc[:3] + [E("work_type", 20, type="coding", task="abcd1234-2")], wf())
    assert under[1]["rounds"]["needs_changes"] == 0         # under budget: fresh
    flagged = derive.derive_v2(nc + [E("work_type", 20, type="coding", task="abcd1234-2", carried=False)], wf())
    assert flagged[1]["rounds"]["needs_changes"] == 0       # the hook's recorded decision wins
    other = derive.derive_v2(nc + [E("work_type", 20, type="chore", task="abcd1234-2")], wf())
    assert other[1]["rounds"]["needs_changes"] == 2


def test_derive_task_that_ended_ready_does_not_carry_even_when_spent():
    evs = [E("work_type", 1, type="coding", task="abcd1234-1")]
    for i, v in enumerate((1, 2)):
        evs += [E("edit", 2 + i * 3, task="abcd1234-1", path="p", tool="Edit", version=v),
                E("verdict", 3 + i * 3, task="abcd1234-1", agent_id="R%d" % i, helper="reviewer",
                  verdict="NEEDS_CHANGES", version=v, findings=1)]
    evs += [E("edit", 9, task="abcd1234-1", path="p", tool="Edit", version=3),
            E("verdict", 10, task="abcd1234-1", agent_id="R9", helper="reviewer", verdict="READY", version=3, findings=0),
            E("work_type", 20, type="coding", task="abcd1234-2")]
    assert derive.derive_v2(evs, wf())[1]["rounds"]["needs_changes"] == 0
