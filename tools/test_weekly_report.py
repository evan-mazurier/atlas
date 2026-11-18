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


# ---- senior review caught what the routine review passed

def _verdicts(tid, seq):
    """A coding task closed in the week with the given verdicts: [(minute, helper, verdict)]."""
    ev = _task_events(tid, [])
    ev = [e for e in ev if e["ev"] != "task_done"]
    for i, (m, helper, v) in enumerate(seq):
        ev.append(E("verdict", m, agent_id="v%d" % i, helper=helper, verdict=v, version=0,
                    findings=1 if v == "NEEDS_CHANGES" else 0, task=tid))
    ev.append(E("task_done", 60, task=tid, version=0))
    return ev


def test_senior_after_routine_ready(tmp_path, monkeypatch):
    i = [f"{SID[:8]}-{n}" for n in range(3, 8)]
    ev = (_verdicts(i[0], [(10, "reviewer", "READY"), (20, "senior-reviewer", "NEEDS_CHANGES")])      # counts
          + _verdicts(i[1], [(10, "senior-reviewer", "NEEDS_CHANGES"), (20, "reviewer", "READY")])   # wrong order
          + _verdicts(i[2], [(10, "reviewer", "NEEDS_CHANGES"), (20, "senior-reviewer", "NEEDS_CHANGES")])  # no routine READY
          + _verdicts(i[3], [(10, "reviewer", "READY"), (20, "senior-reviewer", "READY")])           # senior agrees
          + _verdicts(i[4], [(10, "reviewer", "READY"), (20, "senior-reviewer", "NEEDS_CHANGES"),
                             (30, "reviewer", "READY"), (40, "senior-reviewer", "NEEDS_CHANGES")]))  # once per task
    _strict_ledger(tmp_path, monkeypatch, ev)
    r = wr.compute(WEEK)
    assert r["review"]["senior_after_routine_ready"] == [i[0], i[4]]
    md = wr.numbers_md(r)
    assert ": 2 task(s) (" + i[0] + ", " + i[4] + ")" in md and "Senior review caught what the routine review passed" in md
    assert "\u2014" not in md


def test_senior_after_routine_ready_none(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _verdicts(T1, [(10, "reviewer", "READY")]))
    r = wr.compute(WEEK)
    assert r["review"]["senior_after_routine_ready"] == []
    assert "routine review passed (NEEDS CHANGES from senior-reviewer after a READY from another reviewer on the same task): 0 task(s)\n" in wr.numbers_md(r)


# ---- cost split by helper, area and type

def _cost_tasks(tmp_path, monkeypatch, specs):
    """specs: list of (area, type, {helper: weighted}); one task each, one after the other, all in WEEK."""
    ev = [E("session_start", 0, source="startup")]
    for i, (area, type_, costs) in enumerate(specs, start=1):
        m = i * 10
        ev.append(E("work_type", m, type=type_, area=area, task=f"{SID[:8]}-{i}"))
        for h, w in costs.items():
            kw = {"scope": "main"} if h == "chief" else {"scope": f"a{i}{h[:2]}", "helper": h}
            ev.append(E("cost", m + 1, model="claude-sonnet-5-5",
                        **{"in": 1, "out": 1, "cache_read": 0, "cache_write": 0, "weighted": w}, **kw))
        ev.append(E("task_done", m + 5, task=f"{SID[:8]}-{i}", version=0))
    ev.append(E("session_end", len(specs) * 10 + 20, reason="other"))
    _strict_ledger(tmp_path, monkeypatch, ev)


def test_cost_split_sums(tmp_path, monkeypatch):
    _cost_tasks(tmp_path, monkeypatch, [
        ("atlas", "coding", {"implementer": 500, "senior-reviewer": 200}),
        ("atlas", "coding", {"implementer": 300}),
        ("job", "writing", {"implementer": 100}),
    ])
    u = wr.compute(WEEK)["usage_weighted"]
    rows = {(x["helper"], x["area"], x["type"]): x["weighted"] for x in u["by_helper_area_type"]}
    assert rows == {("implementer", "atlas", "coding"): 800, ("senior-reviewer", "atlas", "coding"): 200,
                    ("implementer", "job", "writing"): 100}
    assert [x["weighted"] for x in u["by_helper_area_type"]] == [800, 200, 100]


def test_senior_top_tasks_cap_and_order(tmp_path, monkeypatch):
    specs = [("atlas", "coding", {"senior-reviewer": w}) for w in (10, 70, 30, 60, 20, 50, 40)]
    specs.append(("atlas", "coding", {"implementer": 999}))          # no senior cost: excluded
    _cost_tasks(tmp_path, monkeypatch, specs)
    top = wr.compute(WEEK)["usage_weighted"]["senior_top_tasks"]
    assert [x["weighted"] for x in top] == [70, 60, 50, 40, 30]
    assert top[0] == {"id": f"{SID[:8]}-2", "area": "atlas", "type": "coding", "weighted": 70}


def test_cost_split_empty_renders_none(tmp_path, monkeypatch):
    _cost_tasks(tmp_path, monkeypatch, [("atlas", "coding", {"chief": 0})])
    r = wr.compute(WEEK)
    assert r["usage_weighted"]["by_helper_area_type"] == [] and r["usage_weighted"]["senior_top_tasks"] == []
    md = wr.numbers_md(r)
    assert "By helper, area and type\n\n_none_\n" in md
    assert "Most expensive senior-reviewer tasks\n\n_none_\n" in md


def test_cost_split_markdown_and_rerun(tmp_path, monkeypatch):
    _cost_tasks(tmp_path, monkeypatch, [("atlas", "coding", {"implementer": 1500, "senior-reviewer": 2500})])
    out = tmp_path / "out"
    md, _ = wr.write(wr.compute(WEEK), out)
    text = md.read_text(encoding="utf-8")
    assert "| Helper | Area | Type | Weighted |" in text and "| senior-reviewer | atlas | coding | 2,500 |" in text
    assert f"| {SID[:8]}-1 | atlas | coding | 2,500 |" in text
    md.write_text(text.replace(wr.PLACEHOLDER, "KEEP ME", 1), encoding="utf-8")
    wr.write(wr.compute(WEEK), out)
    again = md.read_text(encoding="utf-8")
    assert "KEEP ME" in again and again.count(wr.N_START) == 1


# ---- Advisor section (spec design notes, not in this copy, section 12)

W46 = "2026-W46"   # Mon 9 Nov to Sun 15 Nov 2026
B46 = "2026-11-10T09:00:00+01:00"


def EB(ev, m, sid=SID, **kw):
    return dict({"v": 2, "t": T(m, B46), "ev": ev, "sid": sid, "cwd": "D:\\AI\\Atlas", "mode": "record"}, **kw)


def _adv_task(n, steps, consults=()):
    """steps: (minute, 'nc' | 'nc-no-round' | 'ready'); consults: (minute, label). One session per task."""
    sid = f"abcd1234-0000-4000-8000-0000000001{n:02d}"
    tid = f"{sid[:8]}-{n}"
    ev = [EB("session_start", 0, sid=sid), EB("work_type", 0, sid=sid, type="coding", area="atlas", task=tid)]
    for i, (m, label) in enumerate(consults):
        ev.append(EB("helper_start", m, sid=sid, helper="advisor", label=label, tuid=f"u{n}{i}", bg=False, version=0, task=tid))
        ev.append(EB("helper_paired", m, sid=sid, agent_id=f"ag{n}{i}", helper="advisor", tuid=f"u{n}{i}"))
        ev.append(EB("cost", m + 1, sid=sid, scope=f"ag{n}{i}", helper="advisor", model="claude-fable-5-2",
                    **{"in": 10, "out": 5, "cache_read": 0, "cache_write": 0, "weighted": 100 * (i + 1)}))
    for m, k in steps:
        if k == "ready":
            ev.append(EB("verdict", m, sid=sid, agent_id="r1", helper="reviewer", verdict="READY", version=1, findings=0, task=tid))
        else:
            ev.append(EB("verdict", m, sid=sid, agent_id="r1", helper="reviewer", verdict="NEEDS_CHANGES", version=1,
                        findings=1, task=tid, round=(k == "nc")))
    ev.append(EB("task_done", 80, sid=sid, task=tid, version=1))
    ev.append(EB("session_end", 90, sid=sid, reason="other"))
    return tid, ev


def _adv_ledger(tmp_path, monkeypatch, tasks):
    d = tmp_path / "ledger"
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(d))
    stats.clear_cache()
    query.invalidate()
    ev = []
    for _tid, evs in tasks:
        ev += evs
    store.append_events(ev, base=d)


@pytest.fixture
def advisor_ledger(tmp_path, monkeypatch):
    tasks = [
        # A: troubled, then consulted: 1 round before the first consult, 2 after
        _adv_task(1, [(10, "nc"), (20, "nc"), (30, "nc"), (40, "ready")], consults=[(15, "unstick: stuck"), (35, "unstick: again")]),
        # B: scoping consult before any round (not compared), 1 round after
        _adv_task(2, [(10, "nc"), (20, "ready")], consults=[(2, "scope: big task")]),
        # C: troubled, no consult: 3 rounds, 2 after the first (and a same-batch NEEDS CHANGES that is no round)
        _adv_task(3, [(10, "nc"), (11, "nc-no-round"), (20, "nc"), (30, "nc"), (40, "ready")]),
        # D: troubled, no consult: 1 round, 0 after
        _adv_task(4, [(10, "nc"), (20, "ready")]),
        # E: design consult, unlabelled consult, no rounds
        _adv_task(5, [(10, "ready")], consults=[(3, "design: api shape"), (4, "just asking")]),
    ]
    _adv_ledger(tmp_path, monkeypatch, tasks)
    return tasks


def test_advisor_section_numbers(advisor_ledger):
    r = wr.compute(W46)
    a = r["advisor"]
    assert a["consults"] == 5 and a["tasks_with_consult"] == 3 and a["consults_per_task"] == 1.67
    # costs: A 100 + 200, B 100, E 100 + 200
    assert a["weighted_fable_cost"] == 700
    kinds = {k["kind"]: (k["count"], k["weighted"]) for k in a["by_kind"]}
    assert kinds == {"scope": (1, 100), "unstick": (2, 300), "design": (1, 100), "unlabelled": (1, 200)}
    assert a["consult_group"] == {"tasks": 1, "mean_rounds_after": 2.0}
    assert a["comparison_group"] == {"tasks": 2, "mean_rounds_after": 1.0}        # C: 2, D: 0
    assert a["other_consult_tasks"] == {"tasks": 2, "mean_rounds_after": 0.5}     # B: 1, E: 0
    ids = {x["task"]: x for x in a["consult_tasks"]}
    assert len(ids) == 3 and all(x["in_week"] for x in ids.values())
    assert sorted(r["tasks"]["with_consult"]) == sorted(ids)


def test_advisor_section_markdown(advisor_ledger):
    md = wr.numbers_md(wr.compute(W46))
    assert "### Advisor" in md and "Consults this week: 5" in md
    assert "Consult group" in md and "Comparison group" in md and "never compared" in md
    assert "| unstick | 2 | 300 |" in md
    assert "\u2014" not in md


def test_advisor_section_empty_before_baseline(ledger):
    a = wr.compute(WEEK)["advisor"]       # W41 is before the ledger baseline
    assert a["consults"] == 0 and a["consult_group"]["tasks"] == 0 and a["consult_group"]["mean_rounds_after"] is None
    assert "### Advisor" in wr.numbers_md(wr.compute(WEEK))


MON_AFTER = 6 * 24 * 60    # minutes from B46 (Tue 10 Nov 09:00) to Mon 16 Nov 09:00, the day after the report week


def _adv_extra(n, specs):
    """Extra events for task n of _adv_task, appended later in the file. specs: (minute, 'round' | 'consult')."""
    sid = f"abcd1234-0000-4000-8000-0000000001{n:02d}"
    tid = f"{sid[:8]}-{n}"
    ev = []
    for i, (m, k) in enumerate(specs):
        if k == "round":
            ev.append(EB("verdict", m, sid=sid, agent_id="r9", helper="reviewer", verdict="NEEDS_CHANGES", version=1,
                         findings=1, task=tid, round=True))
        else:
            ev.append(EB("helper_start", m, sid=sid, helper="advisor", label="unstick: late", tuid=f"x{n}{i}", bg=False, version=0, task=tid))
            ev.append(EB("helper_paired", m, sid=sid, agent_id=f"agx{n}{i}", helper="advisor", tuid=f"x{n}{i}"))
            ev.append(EB("cost", m + 1, sid=sid, scope=f"agx{n}{i}", helper="advisor", model="claude-fable-5-2",
                         **{"in": 10, "out": 5, "cache_read": 0, "cache_write": 0, "weighted": 1000}))
    return ev


def test_advisor_ignores_day_after_the_week(tmp_path, monkeypatch):
    tasks = [
        _adv_task(1, [(10, "nc"), (20, "nc"), (40, "ready")], consults=[(15, "unstick: stuck")]),
        _adv_task(3, [(10, "nc"), (20, "nc"), (40, "ready")]),
        _adv_task(4, [(10, "nc"), (20, "ready")]),
    ]
    _adv_ledger(tmp_path, monkeypatch, tasks)
    before = wr.compute(W46)
    # Monday after the week: a round on a consulted task, a consult on a comparison task, a round on another
    extra = _adv_extra(1, [(MON_AFTER, "round")]) + _adv_extra(3, [(MON_AFTER, "consult")]) + _adv_extra(4, [(MON_AFTER, "round")])
    store.append_events(extra, base=tmp_path / "ledger")
    stats.clear_cache()
    query.invalidate()
    after = wr.compute(W46)
    assert after["advisor"] == before["advisor"]
    assert after["tasks"]["with_consult"] == before["tasks"]["with_consult"]
    assert after["advisor"]["consults"] == 1 and after["advisor"]["weighted_fable_cost"] == 100


def test_advisor_same_timestamp_orders_by_file_position(tmp_path, monkeypatch):
    # consult written before the round, same timestamp: the round counts as after the consult
    t_a = _adv_task(1, [(10, "nc"), (30, "ready")], consults=[(10, "unstick: same minute")])
    # round written before the consult, same timestamp: the round counts as before the consult
    t_b = _adv_task(2, [(10, "nc"), (30, "ready")])
    t_b = (t_b[0], t_b[1] + _adv_extra(2, [(10, "consult")]))
    _adv_ledger(tmp_path, monkeypatch, [t_a, t_b])
    rows = {r["task"]: r for r in wr.compute(W46)["advisor"]["consult_tasks"]}
    a, b = rows[t_a[0]], rows[t_b[0]]
    assert (a["rounds_before_first"], a["rounds_after_first"]) == (0, 1)
    assert (b["rounds_before_first"], b["rounds_after_first"]) == (1, 0)


def test_advisor_task_straddling_the_week_boundary(tmp_path, monkeypatch):
    before_week = -4 * 24 * 60          # Fri 6 Nov, before the report week
    t = _adv_task(1, [(before_week, "nc"), (40, "ready")], consults=[(15, "unstick: stuck")])
    t = (t[0], t[1] + _adv_extra(1, [(MON_AFTER + 60, "round")]))
    _adv_ledger(tmp_path, monkeypatch, [t])
    a = wr.compute(W46)["advisor"]
    row = a["consult_tasks"][0]
    # the round before the week counts as before; the round after the week is not read
    assert (row["rounds_before_first"], row["rounds_after_first"], row["in_week"]) == (1, 0, True)
    # k=1 has no comparison task: unmatched, so it is no longer in the consult group mean (changed with fix 2)
    assert a["consult_group"] == {"tasks": 0, "mean_rounds_after": None} and a["unmatched_consult_tasks"] == 1
    assert a["comparison_group"]["tasks"] == 0 and a["other_consult_tasks"]["tasks"] == 0
    assert a["consults"] == 1 and a["weighted_fable_cost"] == 100


# ---- Advisor: legacy rounds, matched-k comparison, cross-week labels

def _legacy_task(n, steps, consults=()):
    """Like _adv_task, but NEEDS CHANGES verdicts carry no `round` field: steps are (minute, version)."""
    tid, ev = _adv_task(n, [], consults=consults)
    sid = f"abcd1234-0000-4000-8000-0000000001{n:02d}"
    ev = [e for e in ev if e["ev"] not in ("task_done", "session_end")]
    for i, (m, ver) in enumerate(steps):
        ev.append(EB("verdict", m, sid=sid, agent_id=f"L{n}{i}", helper="reviewer", verdict="NEEDS_CHANGES", version=ver, findings=1, task=tid))
    ev.append(EB("task_done", 80, sid=sid, task=tid, version=1))
    return tid, ev


def test_advisor_legacy_verdicts_count_per_distinct_failing_version(tmp_path, monkeypatch):
    # versions 1, 1, 2 -> two rounds (minutes 10 and 30); the consult at 20 splits them: 1 before, 1 after
    t = _legacy_task(1, [(10, 1), (15, 1), (30, 2)], consults=[(20, "unstick: stuck")])
    _adv_ledger(tmp_path, monkeypatch, [t])
    row = wr.compute(W46)["advisor"]["consult_tasks"][0]
    assert (row["rounds_before_first"], row["rounds_after_first"]) == (1, 1)


def test_advisor_matched_k_trajectory(tmp_path, monkeypatch):
    # consulted: R1, R2, consult, R3 -> k=2, after 1. No-consult: R1..R4 -> reached 2, after the 2nd = 2
    a = _adv_task(1, [(10, "nc"), (20, "nc"), (40, "nc"), (50, "ready")], consults=[(30, "unstick: stuck")])
    b = _adv_task(2, [(10, "nc"), (20, "nc"), (30, "nc"), (40, "nc"), (50, "ready")])
    _adv_ledger(tmp_path, monkeypatch, [a, b])
    adv = wr.compute(W46)["advisor"]
    assert adv["matched"]["by_k"] == [{"k": 2, "consult_tasks": 1, "consult_mean_after": 1.0,
                                       "comparison_tasks": 1, "comparison_mean_after": 2.0}]
    assert adv["matched"]["pooled"] == {"consult_tasks": 1, "consult_mean_after": 1.0, "comparison_mean_after": 2.0}
    assert adv["consult_group"] == {"tasks": 1, "mean_rounds_after": 1.0}
    assert adv["comparison_group"] == {"tasks": 1, "mean_rounds_after": 2.0}


def test_advisor_matched_k_pools_by_the_consult_k_distribution(tmp_path, monkeypatch):
    # consult tasks: k=1 (after 0) and k=2 (after 0). Comparison: R1 only (after 1st = 0), R1..R3 (after 1st = 2, after 2nd = 1)
    a = _adv_task(1, [(10, "nc"), (30, "ready")], consults=[(20, "unstick: x")])
    b = _adv_task(2, [(10, "nc"), (20, "nc"), (50, "ready")], consults=[(30, "unstick: y")])
    c = _adv_task(3, [(10, "nc"), (20, "ready")])
    d = _adv_task(4, [(10, "nc"), (20, "nc"), (30, "nc"), (40, "ready")])
    _adv_ledger(tmp_path, monkeypatch, [a, b, c, d])
    m = wr.compute(W46)["advisor"]["matched"]
    assert [(x["k"], x["comparison_tasks"], x["comparison_mean_after"]) for x in m["by_k"]] == [(1, 2, 1.0), (2, 1, 1.0)]
    assert m["pooled"]["comparison_mean_after"] == 1.0 and m["pooled"]["consult_mean_after"] == 0.0


def test_advisor_k_zero_consult_tasks_stay_out_of_the_comparison(tmp_path, monkeypatch):
    a = _adv_task(1, [(10, "nc"), (20, "ready")], consults=[(2, "scope: big")])      # k=0
    b = _adv_task(2, [(10, "nc"), (20, "nc"), (30, "ready")])
    _adv_ledger(tmp_path, monkeypatch, [a, b])
    adv = wr.compute(W46)["advisor"]
    assert adv["matched"]["by_k"] == [] and adv["consult_group"]["tasks"] == 0
    assert adv["comparison_group"] == {"tasks": 0, "mean_rounds_after": None}
    assert adv["other_consult_tasks"] == {"tasks": 1, "mean_rounds_after": 1.0}


def test_advisor_open_consult_task_not_compared(tmp_path, monkeypatch):
    # open consult task: 1 round before the consult, 0 after only because it is unfinished
    a = _adv_task(1, [(10, "nc")], consults=[(20, "unstick: x")])
    a = (a[0], [e for e in a[1] if e["ev"] not in ("task_done", "session_end")])
    b = _adv_task(2, [(10, "nc"), (20, "nc"), (30, "ready")], consults=[(15, "unstick: y")])   # closed, k=1, after 1
    c = _adv_task(3, [(10, "nc"), (20, "nc"), (30, "ready")])                                    # closed comparison: after 1st = 1
    d = _adv_task(4, [(10, "nc"), (20, "nc"), (30, "nc")])                                       # open comparison candidate
    d = (d[0], [e for e in d[1] if e["ev"] not in ("task_done", "session_end")])
    _adv_ledger(tmp_path, monkeypatch, [a, b, c, d])
    adv = wr.compute(W46)["advisor"]
    assert adv["consult_tasks_open"] == 1
    assert adv["matched"]["pooled"] == {"consult_tasks": 1, "consult_mean_after": 1.0, "comparison_mean_after": 1.0}
    assert adv["consult_group"] == {"tasks": 1, "mean_rounds_after": 1.0}
    assert adv["comparison_group"]["tasks"] == 1            # the open task 4 is not a comparison task
    assert "Consult tasks still open (not yet compared): 1" in wr.numbers_md(wr.compute(W46))


def test_advisor_unmatched_k_not_in_consult_group(tmp_path, monkeypatch):
    a = _adv_task(1, [(10, "nc"), (30, "ready")], consults=[(20, "unstick: x")])                 # k=1, after 0, matched
    b = _adv_task(2, [(10, "nc"), (20, "nc"), (30, "nc"), (50, "ready")], consults=[(35, "unstick: y")])  # k=3, after 0... no comparison reaches 3
    c = _adv_task(3, [(10, "nc"), (20, "ready")])                                               # reaches 1, after 0
    _adv_ledger(tmp_path, monkeypatch, [a, b, c])
    adv = wr.compute(W46)["advisor"]
    assert adv["unmatched_consult_tasks"] == 1
    assert adv["consult_group"] == {"tasks": 1, "mean_rounds_after": 0.0}
    assert [x["k"] for x in adv["matched"]["by_k"]] == [1, 3]
    assert "no comparison (unmatched" in wr.numbers_md(wr.compute(W46))


def test_advisor_unpaired_helper_start_is_not_a_consult(tmp_path, monkeypatch):
    t = _adv_task(1, [(10, "nc"), (30, "ready")], consults=[(20, "unstick: denied")])
    t = (t[0], [e for e in t[1] if e["ev"] != "helper_paired"])
    p = _adv_task(2, [(10, "nc"), (30, "ready")], consults=[(20, "unstick: real")])
    _adv_ledger(tmp_path, monkeypatch, [t, p])
    adv = wr.compute(W46)["advisor"]
    assert adv["consults"] == 1 and adv["tasks_with_consult"] == 1
    assert [r["task"] for r in adv["consult_tasks"]] == [p[0]]
    assert {k["kind"]: k["count"] for k in adv["by_kind"]}["unstick"] == 1
    assert wr.compute(W46)["tasks"]["with_consult"] == [p[0]]


def test_advisor_cost_label_resolves_across_weeks(tmp_path, monkeypatch):
    # the consult started on Fri 6 Nov (previous week); its cost event lands in the report week
    t = _adv_task(1, [(10, "nc"), (40, "ready")])
    sid = f"abcd1234-0000-4000-8000-000000000101"
    tid = t[0]
    start = -4 * 24 * 60
    ev = [EB("helper_start", start, sid=sid, helper="advisor", label="design: old", tuid="uold", bg=False, version=0, task=tid),
          EB("helper_paired", start, sid=sid, agent_id="agold", helper="advisor", tuid="uold"),
          EB("cost", 20, sid=sid, scope="agold", helper="advisor", model="claude-fable-5-2",
             **{"in": 10, "out": 5, "cache_read": 0, "cache_write": 0, "weighted": 500})]
    _adv_ledger(tmp_path, monkeypatch, [(tid, t[1] + ev)])
    a = wr.compute(W46)["advisor"]
    kinds = {k["kind"]: (k["count"], k["weighted"]) for k in a["by_kind"]}
    assert a["consults"] == 0 and a["weighted_fable_cost"] == 500
    assert kinds["design"] == (0, 500) and kinds["unlabelled"] == (0, 0)


def _excl_tasks():
    a = _adv_task(1, [(10, "nc"), (20, "nc"), (30, "ready")], consults=[(15, "unstick: x")])     # k=1, after 1
    b = _adv_task(2, [(10, "nc"), (20, "nc"), (30, "ready")], consults=[(15, "unstick: y")])     # k=1, after 1
    c = _adv_task(3, [(10, "nc"), (20, "nc"), (30, "ready")])                                    # comparison
    d = _adv_task(4, [(10, "ready")], consults=[(3, "scope: z")])                                # k=0
    return a, b, c, d


def test_advisor_excluded_task_leaves_trial_figures_keeps_counts(tmp_path, monkeypatch):
    a, b, c, d = _excl_tasks()
    _adv_ledger(tmp_path, monkeypatch, [a, b, c, d])
    base = wr.compute(W46)["advisor"]
    assert base["excluded_from_trial"] == [] and base["consult_group"]["tasks"] == 2
    assert "Excluded from the trial" not in wr.numbers_md(wr.compute(W46))
    monkeypatch.setattr(wr, "ADVISOR_TRIAL_EXCLUDED", {a[0]: "test consult"})
    adv = wr.compute(W46)["advisor"]
    assert adv["excluded_from_trial"] == [{"task": a[0], "reason": "test consult"}]
    assert adv["consult_group"] == {"tasks": 1, "mean_rounds_after": 1.0}
    assert adv["matched"]["pooled"]["consult_tasks"] == 1
    assert adv["comparison_group"]["tasks"] == 1       # the excluded task is not a comparison task either
    assert adv["unmatched_consult_tasks"] == 0 and adv["consult_tasks_open"] == 0
    # consults, cost and label split are unchanged
    assert adv["consults"] == base["consults"] == 3
    assert adv["weighted_fable_cost"] == base["weighted_fable_cost"]
    assert adv["by_kind"] == base["by_kind"]
    md = wr.numbers_md(wr.compute(W46))
    assert f"Excluded from the trial: 1 ({a[0]}: test consult)" in md and "\u2014" not in md


def test_advisor_excluded_k_zero_task_leaves_other_group(tmp_path, monkeypatch):
    a, b, c, d = _excl_tasks()
    _adv_ledger(tmp_path, monkeypatch, [a, b, c, d])
    assert wr.compute(W46)["advisor"]["other_consult_tasks"]["tasks"] == 1
    monkeypatch.setattr(wr, "ADVISOR_TRIAL_EXCLUDED", {d[0]: "test"})
    adv = wr.compute(W46)["advisor"]
    assert adv["other_consult_tasks"] == {"tasks": 0, "mean_rounds_after": None}
    assert adv["consults"] == 3


def test_advisor_empty_exclusion_changes_nothing(tmp_path, monkeypatch):
    a, b, c, d = _excl_tasks()
    _adv_ledger(tmp_path, monkeypatch, [a, b, c, d])
    base = wr.compute(W46)["advisor"]
    monkeypatch.setattr(wr, "ADVISOR_TRIAL_EXCLUDED", {})
    again = wr.compute(W46)["advisor"]
    assert again == base and again["excluded_from_trial"] == []


# ---- edit-stop run, edit_limit refusals, test-runner reminders, rework concentration

def _seq(tid, steps, sid=SID, type_="coding"):
    """steps: (minute, kind[, extra]); kinds: m = main edit, b = builder edit, hs/he = helper_start/end (extra = helper),
    ref = edit_limit refusal (extra = would). Then a task_done."""
    ev = [E("session_start", 0, sid=sid), E("work_type", 0, sid=sid, type=type_, area="atlas", task=tid)]
    for i, (m, k, *x) in enumerate(steps):
        if k == "m":
            ev.append(E("edit", m, sid=sid, path="y.py", tool="Edit", version=i + 1, task=tid))
        elif k == "b":
            ev.append(E("edit", m, sid=sid, path="x.py", tool="Edit", version=i + 1, task=tid, agent_id="a1"))
        elif k == "hs":
            ev.append(E("helper_start", m, sid=sid, helper=x[0], tuid=f"u{i}", task=tid, bg=False, version=0))
        elif k == "he":
            ev.append(E("helper_end", m, sid=sid, helper=x[0], agent_id=f"g{i}", task=tid, status="ok"))
        elif k == "ref":
            ev.append(E("refusal", m, sid=sid, guard="edit_limit", would=x[0], task=tid, detail="x"))
    ev.append(E("task_done", 80, sid=sid, task=tid, version=9))
    return ev


def test_edit_run_over_stop_vs_reset(tmp_path, monkeypatch):
    t2 = f"{SID[:8]}-2"
    over = [(i, "m") for i in range(1, 5)] + [(5, "hs", "implementer")] + [(i, "m") for i in range(6, 14)]
    ok = [(i, "m") for i in range(1, 5)] + [(5, "hs", "drafter")] + [(i, "m") for i in range(6, 9)]
    _strict_ledger(tmp_path, monkeypatch, _seq(T1, over) + _seq(t2, ok))
    r = wr.compute(WEEK)
    assert r["waste"]["main_edit_run"] == {"max": 8, "max_task": T1, "stop": 6, "tasks_above_stop": [T1]}
    md = wr.numbers_md(r)
    assert f"stop): max 8 ({T1}); tasks above 6: 1 ({T1})" in md


def test_edit_run_below_stop(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _seq(T1, [(1, "m"), (2, "m"), (3, "b")]))
    r = wr.compute(WEEK)
    assert r["waste"]["main_edit_run"]["max"] == 2 and r["waste"]["main_edit_run"]["tasks_above_stop"] == []
    assert "tasks above 6: 0" in wr.numbers_md(r)


def test_edit_limit_refusals_enforced_and_would(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _seq(T1, [(1, "ref", False), (2, "ref", True), (3, "ref", True)]))
    r = wr.compute(WEEK)
    assert r["waste"]["edit_limit_refusals"] == {"enforced": 1, "would": 2}
    assert r["refusals"] == {"enforced": 0, "would": 0}      # budget refusals stay separate
    assert "6-edit stop refusals: 1 enforced, 2 would-have (record mode)" in wr.numbers_md(r)


def test_test_runner_reminder_fired_vs_runner_between(tmp_path, monkeypatch):
    t2, t3 = f"{SID[:8]}-2", f"{SID[:8]}-3"
    fired = [(1, "hs", "implementer"), (5, "he", "implementer"), (6, "hs", "reviewer")]
    covered = [(1, "hs", "implementer"), (5, "he", "implementer"), (6, "hs", "test-runner"), (7, "hs", "reviewer")]
    nocoding = [(1, "hs", "implementer"), (5, "he", "implementer"), (6, "hs", "reviewer")]
    _strict_ledger(tmp_path, monkeypatch,
                   _seq(T1, fired) + _seq(t2, covered) + _seq(t3, nocoding, type_="writing"))
    r = wr.compute(WEEK)
    assert r["waste"]["test_runner_reminders"] == {"total": 1, "tasks": [T1]}
    assert "test-runner since): 1 across 1 task(s)" in wr.numbers_md(r)


def test_test_runner_reminder_counts_every_reviewer_launch(tmp_path, monkeypatch):
    steps = [(1, "hs", "implementer"), (5, "he", "implementer"), (6, "hs", "reviewer"), (7, "hs", "senior-reviewer")]
    _strict_ledger(tmp_path, monkeypatch, _seq(T1, steps))
    assert wr.compute(WEEK)["waste"]["test_runner_reminders"] == {"total": 2, "tasks": [T1]}


def test_edit_run_skips_types_without_a_builder(tmp_path, monkeypatch):
    t2 = f"{SID[:8]}-2"
    eight = [(i, "m") for i in range(1, 9)]
    _strict_ledger(tmp_path, monkeypatch, _seq(T1, eight, type_="research") + _seq(t2, eight, type_="coding"))
    r = wr.compute(WEEK)["waste"]["main_edit_run"]
    assert r["tasks_above_stop"] == [t2] and r["max_task"] == t2


def test_edit_run_undeclared_counts_as_builder(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _seq(T1, [(i, "m") for i in range(1, 9)], type_="undeclared"))
    assert wr.compute(WEEK)["waste"]["main_edit_run"]["tasks_above_stop"] == [T1]


def test_constants_match_the_hook_source():
    import re
    src = (Path(__file__).resolve().parent.parent / "hooks" / "atlas_hook.py").read_text(encoding="utf-8")
    m = re.search(r"^BUILDER_RESET\s*=\s*\(([^)]*)\)", src, re.M)
    assert m and tuple(re.findall(r"[\"']([\w-]+)[\"']", m.group(1))) == wr.STOP_RESET
    m = re.search(r"^MAIN_EDIT_STOP\s*=\s*(\d+)", src, re.M)
    assert m and int(m.group(1)) == wr.MAIN_EDIT_STOP
    # the reminder's rule: any reviewer tier (workflows.yaml `reviewers`) on a coding task
    fn = re.search(r"def test_runner_reminder\(.*?\n\n\n", src, re.S).group(0)
    assert "H.reviewers" in fn and '"coding"' in fn
    assert set(wr.STOP_REVIEWERS) == set(query.load_workflows().get("reviewers") or [])


def _nc(tid, n, sid=SID):
    ev = [E("session_start", 0, sid=sid), E("work_type", 0, sid=sid, type="coding", area="atlas", task=tid)]
    for i in range(n):
        ev.append(E("verdict", 5 + i, sid=sid, agent_id=f"r{i}", helper="reviewer", verdict="NEEDS_CHANGES",
                    version=i + 1, findings=1, task=tid))
    ev.append(E("verdict", 30, sid=sid, agent_id="rz", helper="reviewer", verdict="READY", version=n + 1, findings=0, task=tid))
    ev.append(E("task_done", 40, sid=sid, task=tid, version=n + 1))
    return ev


def test_rework_concentration_two_sessions(tmp_path, monkeypatch):
    s2 = "feedbeef-0000-4000-8000-000000000002"
    evs = _nc(T1, 3) + _nc("abcd1234-2", 1) + _nc("feedbeef-1", 0, sid=s2)
    _strict_ledger(tmp_path, monkeypatch, evs)
    r = wr.compute(WEEK)
    c = r["review"]["rework_concentration"]
    assert r["review"]["needs_changes_rounds"] == 4 == c["rounds"]
    assert (c["closed_tasks"], c["per_task"]) == (3, 1.33)
    assert (c["top_session"], c["top_rounds"], c["top_pct"]) == ("abcd1234", 4, 100.0)
    assert c["per_task_without_top"] == 0.0
    assert "Rework per closed task: 1.33 (4/3); top session abcd1234 has 4 of 4 rounds (100.0 %); without it: 0.0 (0 / 1)" \
        in wr.numbers_md(r)


def test_rework_concentration_split(tmp_path, monkeypatch):
    s2 = "feedbeef-0000-4000-8000-000000000002"
    evs = _nc(T1, 3) + _nc("abcd1234-2", 0) + _nc("feedbeef-1", 1, sid=s2) + _nc("feedbeef-2", 0, sid=s2)
    _strict_ledger(tmp_path, monkeypatch, evs)
    c = wr.compute(WEEK)["review"]["rework_concentration"]
    assert (c["rounds"], c["closed_tasks"], c["top_session"], c["top_rounds"], c["top_pct"]) == (4, 4, "abcd1234", 3, 75.0)
    assert c["per_task_without_top"] == 0.5


def test_rework_concentration_zero_rounds(tmp_path, monkeypatch):
    _strict_ledger(tmp_path, monkeypatch, _nc(T1, 0))
    r = wr.compute(WEEK)
    assert r["review"]["rework_concentration"]["top_session"] is None
    assert "- Rework per closed task: n/a" in wr.numbers_md(r)
