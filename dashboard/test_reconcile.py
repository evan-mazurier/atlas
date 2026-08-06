"""Unit tests for the plan<->schedule reconciliation (dashboard/dispatcher.py):
`reconcile_task` (grow-then-split-fill) and its auto-trigger from `save_remainder`.

Isolation: dispatcher.SCHEDULE, dispatcher.JOBS_DIR, and dispatcher.REGISTRY are monkeypatched
onto a tmp_path copy for every test — the real repo files are never touched. A minimal
plan.yaml (matching the real projects' shape — see D:\\work\\orchard-ops\\plan.yaml) backs the
`project`/`task` pair "demo"/"t1".

Run (from dashboard/):
    py -3.12 -m unittest test_reconcile -v
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dispatcher  # noqa: E402

DAY = "2026-07-22"          # the placement's day, throughout
DAY_D = dt.date.fromisoformat(DAY)


def _plan_yaml(remaining: str = "2h", estimate: str = "4h") -> str:
    return f"""\
project: demo

tasks:
  - id: t1
    title: "Write docs"
    state: in_progress
    estimate: {estimate}
    remaining: {remaining}
    sessions_done: []
"""


class ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        import yaml
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)

        self.schedule_path = tmp_path / "schedule.yaml"
        self.jobs_dir = tmp_path / "jobs"
        self.jobs_dir.mkdir()
        self.plan_path = tmp_path / "plan.yaml"
        self.registry_path = tmp_path / "registry.yaml"
        self.registry_path.write_text(
            yaml.safe_dump({"projects": [{"id": "demo", "plan_file": str(self.plan_path)}]}),
            encoding="utf-8")

        self._orig = (dispatcher.SCHEDULE, dispatcher.JOBS_DIR, dispatcher.REGISTRY)
        dispatcher.SCHEDULE = self.schedule_path
        dispatcher.JOBS_DIR = self.jobs_dir
        dispatcher.REGISTRY = self.registry_path

    def tearDown(self):
        dispatcher.SCHEDULE, dispatcher.JOBS_DIR, dispatcher.REGISTRY = self._orig
        self._tmpdir.cleanup()

    def _write_schedule(self, text: str):
        self.schedule_path.write_text(text, encoding="utf-8")

    def _write_plan(self, remaining: str = "2h", estimate: str = "4h"):
        self.plan_path.write_text(_plan_yaml(remaining, estimate), encoding="utf-8")

    def _raw_schedule(self) -> str:
        return self.schedule_path.read_text(encoding="utf-8")

    def _placements(self) -> list:
        import yaml
        data = yaml.safe_load(self._raw_schedule()) or {}
        return data.get("placements") or []


BASE_SCHEDULE = f"""\
# header comment that must survive
availability:
  blocks: []
  full_days: []

placements:
- session: sess_1
  project: demo
  task: t1
  day: '{DAY}'
  start: '10:00'
  end: '12:00'

events: []
"""


class ReconcileNoOp(ReconcileTestCase):
    def test_no_op_when_already_fully_planned(self):
        self._write_schedule(BASE_SCHEDULE)
        self._write_plan(remaining="2h")   # == the 2h (10:00-12:00) already on the calendar
        before = self._raw_schedule()
        out = dispatcher.reconcile_task("demo", "t1")
        self.assertEqual(out, {"grew": 0, "added": [], "unfilled_min": 0})
        self.assertEqual(self._raw_schedule(), before)   # nothing written

    def test_no_op_when_remaining_is_less_than_scheduled(self):
        self._write_schedule(BASE_SCHEDULE)
        self._write_plan(remaining="1h")   # LESS than the 2h already scheduled
        out = dispatcher.reconcile_task("demo", "t1")
        self.assertEqual(out, {"grew": 0, "added": [], "unfilled_min": 0})

    def test_unknown_task_raises(self):
        self._write_schedule(BASE_SCHEDULE)
        self._write_plan(remaining="2h")
        with self.assertRaises(ValueError):
            dispatcher.reconcile_task("demo", "no-such-task")


class ReconcileGrowsInPlace(ReconcileTestCase):
    def test_grows_the_leftover_placement_when_the_gap_fits_after_it(self):
        # nothing busy after 12:00 on this day -> the placement can grow its END to close a 1h gap
        self._write_schedule(BASE_SCHEDULE)
        self._write_plan(remaining="3h")   # scheduled=2h -> 1h (60min) gap
        out = dispatcher.reconcile_task("demo", "t1")
        self.assertEqual(out["grew"], 60)
        self.assertEqual(out["added"], [])
        self.assertEqual(out["unfilled_min"], 0)
        placements = self._placements()
        self.assertEqual(len(placements), 1)
        pl = placements[0]
        self.assertEqual(pl["session"], "sess_1")       # grown in place, not a new session
        self.assertEqual(pl["start"], "10:00")
        self.assertEqual(pl["end"], "13:00")
        self.assertTrue(pl["pinned"])


class ReconcileSplitFills(ReconcileTestCase):
    SCHEDULE_BLOCKED_ADJACENT = f"""\
# header comment that must survive
availability:
  blocks: []
  full_days: []

placements:
- session: sess_1
  project: demo
  task: t1
  day: '{DAY}'
  start: '10:00'
  end: '12:00'

events:
- id: evt_before
  title: Morning call
  day: '{DAY}'
  type: appointment
  start: '09:00'
  end: '10:00'
- id: evt_after
  title: Standup
  day: '{DAY}'
  type: appointment
  start: '12:00'
  end: '12:30'
"""

    def test_splits_into_a_new_slot_when_growth_is_blocked_both_sides(self):
        self._write_schedule(self.SCHEDULE_BLOCKED_ADJACENT)
        self._write_plan(remaining="3h30")   # scheduled=2h -> 90min gap; growth blocked both sides
        out = dispatcher.reconcile_task("demo", "t1")
        self.assertEqual(out["grew"], 0)
        self.assertEqual(out["unfilled_min"], 0)
        self.assertEqual(len(out["added"]), 1)
        added = out["added"][0]
        self.assertEqual(added["day"], DAY)
        # the earliest free opening that day is 06:00-09:00 (before the Morning call)
        self.assertEqual(added["start"], "06:00")
        self.assertEqual(added["end"], "07:30")

        placements = self._placements()
        self.assertEqual(len(placements), 2)
        by_session = {p["session"]: p for p in placements}
        self.assertEqual(by_session["sess_1"]["start"], "10:00")   # untouched
        self.assertEqual(by_session["sess_1"]["end"], "12:00")
        new_sessions = [s for s in by_session if s != "sess_1"]
        self.assertEqual(len(new_sessions), 1)
        new_pl = by_session[new_sessions[0]]
        self.assertEqual(new_pl["project"], "demo")
        self.assertEqual(new_pl["task"], "t1")
        self.assertTrue(new_pl["pinned"])

    def test_reports_unfilled_min_when_capacity_is_short(self):
        # block growth both sides (as above) AND exhaust the whole reconcile horizon (21 days,
        # including the placement's own day) via full_days -> nothing at all can be filled.
        full_days = [DAY] + [(DAY_D + dt.timedelta(days=i)).isoformat() for i in range(1, 21)]
        import yaml
        sched = yaml.safe_load(self.SCHEDULE_BLOCKED_ADJACENT)
        sched["availability"]["full_days"] = full_days
        self.schedule_path.write_text(yaml.safe_dump(sched, sort_keys=False), encoding="utf-8")
        self._write_plan(remaining="3h30")

        out = dispatcher.reconcile_task("demo", "t1")
        self.assertEqual(out["grew"], 0)
        self.assertEqual(out["added"], [])
        self.assertEqual(out["unfilled_min"], 90)
        # nothing new was written to the calendar
        self.assertEqual(len(self._placements()), 1)


class SaveRemainderAutoTrigger(ReconcileTestCase):
    def test_raising_remaining_above_scheduled_triggers_reconciliation(self):
        self._write_schedule(BASE_SCHEDULE)
        self._write_plan(remaining="2h")
        out = dispatcher.save_remainder("demo", "t1", remaining="3h", complete=False)
        self.assertIn("reconcile", out)
        self.assertEqual(out["reconcile"]["grew"], 60)
        placements = self._placements()
        pl = next(p for p in placements if p["session"] == "sess_1")
        self.assertEqual(pl["end"], "13:00")   # grew by 1h to close the gap

        import yaml
        plan = yaml.safe_load(self.plan_path.read_text(encoding="utf-8"))
        task = plan["tasks"][0]
        self.assertEqual(str(task["remaining"]), "3h")
        self.assertEqual(str(task["review_pending"]).lower(), "false")

    def test_lowering_remaining_below_scheduled_is_a_reconcile_no_op(self):
        self._write_schedule(BASE_SCHEDULE)
        self._write_plan(remaining="3h")
        out = dispatcher.save_remainder("demo", "t1", remaining="1h", complete=False)
        self.assertIn("reconcile", out)
        self.assertEqual(out["reconcile"], {"grew": 0, "added": [], "unfilled_min": 0})
        # nothing added/changed on the calendar
        placements = self._placements()
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0]["end"], "12:00")

    def test_complete_branch_is_unchanged_no_reconcile_key(self):
        self._write_schedule(BASE_SCHEDULE)
        self._write_plan(remaining="2h")
        out = dispatcher.save_remainder("demo", "t1", remaining="", complete=True)
        self.assertNotIn("reconcile", out)
        self.assertIn("pruned", out)


if __name__ == "__main__":
    unittest.main()
