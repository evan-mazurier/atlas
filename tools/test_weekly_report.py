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
