"""Tests for tools/weekly_report.py on a fixture ledger. Temp dirs only."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import weekly_report as wr  # noqa: E402
from ledger2 import query, stats, store  # noqa: E402

SID = "abcd1234-0000-4000-8000-000000000001"
T1, T2 = f"{SID[:8]}-1", f"{SID[:8]}-2"
WEEK = "2026-W41"   # Mon 5 Oct to Sun 11 Oct 2026


def T(minutes, base="2026-10-07T09:00:00+02:00"):
    return (dt.datetime.fromisoformat(base) + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def E(ev, m, sid=SID, **kw):
    return dict({"v": 2, "t": T(m), "ev": ev, "sid": sid, "cwd": "D:\\AI\\Atlas", "mode": "record"}, **kw)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(d))
    stats.clear_cache()
    query.invalidate()
    ev = [
        # task 1: coding in atlas, 60 min, NEEDS_CHANGES (2 findings) then READY, one main-session edit
        E("session_start", 0, source="startup"),
        E("work_type", 0, type="coding", area="atlas", task=T1),
        E("helper_start", 1, helper="implementer", label="Build it", tuid="u1", bg=False, version=0),
        E("helper_paired", 1, agent_id="a1", helper="implementer", tuid="u1"),
        E("edit", 5, path="D:\\AI\\Atlas\\x.py", tool="Edit", version=1, task=T1, agent_id="a1"),
        E("edit", 6, path="D:\\AI\\Atlas\\y.py", tool="Edit", version=2, task=T1),
        E("verdict", 10, agent_id="r1", helper="reviewer", verdict="NEEDS_CHANGES", version=2, findings=2, task=T1),
        E("refusal", 11, guard="rework", would=True, task=T1, detail="x"),
        E("verdict", 20, agent_id="r2", helper="reviewer", verdict="READY", version=2, findings=0, task=T1),
        E("cost", 21, scope="main", model="claude-opus-5-5",
          **{"in": 100, "out": 10, "cache_read": 900, "cache_write": 0, "weighted": 1000}),
        E("cost", 22, scope="a1", helper="implementer", model="claude-sonnet-5-5",
          **{"in": 100, "out": 10, "cache_read": 0, "cache_write": 0, "weighted": 500}),
        E("summary", 59, task=T1, goal="Build it", produced="branch", review_min=10),
        E("task_done", 60, task=T1, version=2),
        # task 2: undeclared edit with no check -> unverified; also a slash command
        E("prompt", 61, cmd="weekly-report"),
        E("work_type", 61, type="undeclared", area="atlas", task=T2),
        E("edit", 62, path="D:\\AI\\Atlas\\z.py", tool="Edit", version=1, task=T2),
        E("session_end", 90, reason="other"),
    ]
    store.append_events(ev, base=d)
    return d


def test_exact_numbers(ledger):
    r = wr.compute(WEEK)
    t = r["tasks"]
    assert (t["closed"], t["done"]) == (2, 1)
    assert r["time_hours"]["by_area"] == [{"key": "atlas", "value": 1.02}]   # active time: 60 min (a helper ran to the end of task 1) + 1 min (task 2; the 28 min gap to the session end is idle)
    assert r["usage_weighted"]["total_cost_events"] == 1500
    assert r["usage_weighted"]["total_task_cost"] == 1500
    assert {x["key"]: x["value"] for x in r["usage_weighted"]["by_helper"]} == {"chief": 1000, "implementer": 500}
    assert {x["key"]: x["value"] for x in r["usage_weighted"]["by_tier"]} == {"opus": 1000, "sonnet": 500}
    assert {x["key"]: x["value"] for x in r["usage_weighted"]["by_type"]} == {"coding": 1500, "undeclared": 0}
    assert r["review"]["needs_changes_rounds"] == 1 and r["review"]["caught"] == 2
    assert r["refusals"] == {"enforced": 0, "would": 1}
    assert r["unverified"] == [T2]
    assert r["waste"]["undeclared_tasks"] == [T2]
    cw = r["waste"]["chief_helper_sized_work"]
    assert [c["task"] for c in cw] == [T1] and cw[0]["main_edits"] == 1
    assert r["waste"]["cache_reuse_pct"] == 81.8          # 900 / (900 + 100 + 100)
    assert r["waste"]["cache_reuse_pct_prev_week"] == "not measured"
    assert r["skills"] == [{"key": "weekly-report", "value": 1}]
    assert t["first_pass_ready_pct"] == 0.0


def test_other_week_is_empty(ledger):
    r = wr.compute("2026-W40")
    assert r["tasks"]["closed"] == 0 and r["usage_weighted"]["total_cost_events"] == 0


def test_write_md_json_and_placeholders(ledger, tmp_path):
    out = tmp_path / "out"
    md, js = wr.write(wr.compute(WEEK), out)
    text = md.read_text(encoding="utf-8")
    assert text.startswith(f"# Weekly report {WEEK}")
    assert text.count(wr.PLACEHOLDER) == 3
    assert "not measured" in text
    assert "0 enforced, 1 would-have" in text
    assert json.loads(js.read_text(encoding="utf-8"))["week"] == WEEK
    assert "\u2014" not in text


def test_rerun_keeps_narrative(ledger, tmp_path):
    out = tmp_path / "out"
    md, _ = wr.write(wr.compute(WEEK), out)
    md.write_text(md.read_text(encoding="utf-8").replace(wr.PLACEHOLDER, "MY LESSON", 1), encoding="utf-8")
    wr.write(wr.compute(WEEK), out)
    text = md.read_text(encoding="utf-8")
    assert "MY LESSON" in text and text.count(wr.PLACEHOLDER) == 2 and text.count(wr.N_START) == 1


def test_cli(ledger, tmp_path, capsys):
    assert wr.main(["--week", WEEK, "--out", str(tmp_path / "o")]) == 0
    assert json.loads(capsys.readouterr().out)["tasks_closed"] == 2
    assert (tmp_path / "o" / f"{WEEK}.md").exists()
    assert wr.main(["--week", "garbage"]) == 2


# ---- strict first-pass READY

def _strict_ledger(tmp_path, monkeypatch, events):
    d = tmp_path / "ledger"
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(d))
    stats.clear_cache()
    query.invalidate()
    store.append_events(events, base=d)


def _task_events(tid, kinds, verdict=True, base="2026-10-07T09:00:00+02:00", done=60):
    """kinds: list of (minute, 'b'|'m'|'v'). Builder agent a1 = implementer."""
    ev = [dict(E("session_start", 0), t=T(0, base)),
          dict(E("work_type", 0, type="coding", area="atlas", task=tid), t=T(0, base)),
          dict(E("helper_paired", 1, agent_id="a1", helper="implementer", tuid="u1"), t=T(1, base))]
    for m, k in kinds:
        if k == "b":
            ev.append(dict(E("edit", m, path="x.py", tool="Edit", version=1, task=tid, agent_id="a1"), t=T(m, base)))
        elif k == "m":
            ev.append(dict(E("edit", m, path="y.py", tool="Edit", version=2, task=tid), t=T(m, base)))
        elif k == "v":
            ev.append(dict(E("verdict", m, agent_id="r1", helper="reviewer", verdict="READY", version=2,
                             findings=0, task=tid), t=T(m, base)))
    ev.append(dict(E("task_done", done, task=tid, version=2), t=T(done, "2026-10-07T09:00:00+02:00")))
    return ev


def test_strict_hidden_rework(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _task_events(T1, [(5, "b"), (6, "m"), (10, "v")]))
    r = wr.compute(WEEK)
    assert r["review"]["hidden_rework_tasks"] == [T1]
    assert r["tasks"]["first_pass_ready_pct"] == 100.0
    assert r["tasks"]["first_pass_ready_strict_pct"] == 0.0
    assert "hidden rework: 1 task(s) (" + T1 + ")" in wr.numbers_md(r)


def test_strict_main_edit_before_builder(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _task_events(T1, [(4, "m"), (5, "b"), (10, "v")]))
    r = wr.compute(WEEK)
    assert r["review"]["hidden_rework_tasks"] == []
    assert r["tasks"]["first_pass_ready_strict_pct"] == 100.0


def test_strict_main_edit_after_verdict(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _task_events(T1, [(5, "b"), (10, "v"), (12, "m")]))
    r = wr.compute(WEEK)
    assert r["review"]["hidden_rework_tasks"] == []
    assert r["tasks"]["first_pass_ready_strict_pct"] == 100.0


def test_strict_edits_in_previous_week(tmp_path, monkeypatch):
    # started Mon 5 Oct 2026 (W41) is the week of the close? use started Sat 3 Oct (W40), closed Wed 7 Oct (W41)
    base = "2026-10-03T09:00:00+02:00"
    ev = _task_events(T1, [(5, "b"), (6, "m"), (10, "v")], base=base, done=60)
    ev = [e for e in ev if e["ev"] != "task_done"] + [dict(E("task_done", 60, task=T1, version=2))]
    _strict_ledger(tmp_path, monkeypatch, ev)
    r = wr.compute(WEEK)
    assert r["tasks"]["closed"] == 1
    assert r["review"]["hidden_rework_tasks"] == [T1]
    assert r["tasks"]["first_pass_ready_strict_pct"] == 0.0


def test_strict_no_verdicts_excluded(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _task_events(T1, [(5, "b"), (6, "m")]))
    r = wr.compute(WEEK)
    assert r["review"]["hidden_rework_tasks"] == []
    assert r["tasks"]["first_pass_ready_strict_pct"] is None
    assert "strict (no main-session edits on a builder's work before the first verdict): n/a; hidden rework: 0 task(s)" in wr.numbers_md(r)


# ---- first-pass READY by type and by who built

def _fp_events(tid, kinds, type_="coding", verdict="READY"):
    ev = _task_events(tid, kinds)
    for e in ev:
        if e["ev"] == "work_type":
            e["type"] = type_
        if e["ev"] == "verdict":
            e["verdict"] = verdict
    return ev


def _rows(r, key):
    return {x["key"]: (x["ready"], x["reviewed"], x["pct"]) for x in r["review"][key]}


def test_fp_by_type(tmp_path, monkeypatch):
    ids = [f"{SID[:8]}-{i}" for i in range(3, 6)]
    ev = (_fp_events(ids[0], [(5, "b"), (10, "v")], "coding")
          + _fp_events(ids[1], [(5, "b"), (10, "v")], "coding")
          + _fp_events(ids[2], [(5, "m"), (10, "v")], "writing", verdict="NEEDS_CHANGES"))
    _strict_ledger(tmp_path, monkeypatch, ev)
    r = wr.compute(WEEK)
    assert _rows(r, "first_pass_by_type") == {"coding": (2, 2, 100.0), "writing": (0, 1, 0.0)}
    assert [x["key"] for x in r["review"]["first_pass_by_type"]] == ["coding", "writing"]
    md = wr.numbers_md(r)
    assert "\nFirst-pass READY by type\n\n| Type | Reviewed | READY first time | % |" in md
    assert "| coding | 2 | 2 | 100.0 % |" in md and "| writing | 1 | 0 | 0.0 % |" in md


def test_fp_builder_classes_and_late_edit(tmp_path, monkeypatch):
    i = [f"{SID[:8]}-{n}" for n in range(3, 7)]
    ev = (_fp_events(i[0], [(5, "m"), (10, "v")])
          + _fp_events(i[1], [(5, "b"), (10, "v")])
          + _fp_events(i[2], [(5, "b"), (6, "m"), (10, "v")], verdict="NEEDS_CHANGES")
          + _fp_events(i[3], [(10, "v"), (12, "m"), (13, "b")]))   # edits only after the verdict
    _strict_ledger(tmp_path, monkeypatch, ev)
    r = wr.compute(WEEK)
    assert _rows(r, "first_pass_by_builder") == {
        "orchestrator only": (1, 1, 100.0), "helper only": (1, 1, 100.0),
        "both": (0, 1, 0.0), "no file edits": (1, 1, 100.0)}
    md = wr.numbers_md(r)
    assert "First-pass READY by who built (edits before the first review)\n\n| Built by |" in md
    assert "| both | 1 | 0 | 0.0 % |" in md
    assert "—" not in md


def test_fp_no_verdicts_excluded(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _task_events(T1, [(5, "b"), (6, "m")]))
    r = wr.compute(WEEK)
    assert r["review"]["first_pass_by_type"] == [] and r["review"]["first_pass_by_builder"] == []
    assert wr.numbers_md(r).count("_none_") >= 2
