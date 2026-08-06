"""Unit tests for dispatcher.advance_milestone (docs/planner.md §4's cheap deterministic path):
the completion prompt's `✓ achieved` button — close the ACTIVE milestone and promote the first
`next` milestone in file order, no LLM involved. The function re-verifies the guard itself
(every task tagged with the active milestone must be `done`) rather than trusting the caller.

Isolation: dispatcher.REGISTRY is monkeypatched onto a tmp_path registry.yaml pointing at a tmp
plan.yaml — the real repo files are never touched.

Run (from dashboard/):
    py -3.12 -m unittest test_milestone_advance -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dispatcher  # noqa: E402


class MilestoneAdvanceTestCase(unittest.TestCase):
    def setUp(self):
        import yaml
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)
        self.plan_path = tmp_path / "plan.yaml"
        self.registry_path = tmp_path / "registry.yaml"
        self.registry_path.write_text(
            yaml.safe_dump({"projects": [{"id": "demo", "plan_file": str(self.plan_path)}]}),
            encoding="utf-8")
        self._orig_registry = dispatcher.REGISTRY
        dispatcher.REGISTRY = self.registry_path

    def tearDown(self):
        dispatcher.REGISTRY = self._orig_registry
        self._tmpdir.cleanup()

    def _write_plan(self, text: str) -> None:
        self.plan_path.write_text(text, encoding="utf-8")

    def _read_plan(self) -> str:
        return self.plan_path.read_text(encoding="utf-8")


PLAN_WITH_NEXT = """\
project: demo

milestones:
  - id: m1
    title: "First milestone"
    status: active
  - id: m2
    title: "Second milestone"
    status: next
  - id: m3
    title: "Third milestone"
    status: later

tasks:
  - id: t1
    title: "Task one"
    milestone: m1
    state: done
  - id: t2
    title: "Task two"
    milestone: m1
    state: done
  - id: t3
    title: "Task three"
    milestone: m2
    state: todo
"""


class HappyPath(MilestoneAdvanceTestCase):
    def test_active_becomes_done_and_next_becomes_active(self):
        self._write_plan(PLAN_WITH_NEXT)
        res = dispatcher.advance_milestone("demo")
        self.assertEqual(res, {
            "ok": True,
            "completed": "m1",
            "completed_title": "First milestone",
            "promoted": "m2",
            "promoted_title": "Second milestone",
        })
        import yaml
        data = yaml.safe_load(self._read_plan())
        ms = {m["id"]: m for m in data["milestones"]}
        self.assertEqual(ms["m1"]["status"], "done")
        self.assertIn("done_date", ms["m1"])
        import datetime as dt
        self.assertEqual(str(ms["m1"]["done_date"]), dt.date.today().isoformat())
        self.assertEqual(ms["m2"]["status"], "active")
        self.assertEqual(ms["m3"]["status"], "later")   # untouched


PLAN_NO_NEXT = """\
project: demo

milestones:
  - id: m1
    title: "First milestone"
    status: active
  - id: m3
    title: "Third milestone"
    status: later

tasks:
  - id: t1
    title: "Task one"
    milestone: m1
    state: done
"""


class NoNextMilestone(MilestoneAdvanceTestCase):
    def test_active_closed_promoted_none_no_crash(self):
        self._write_plan(PLAN_NO_NEXT)
        res = dispatcher.advance_milestone("demo")
        self.assertTrue(res["ok"])
        self.assertEqual(res["completed"], "m1")
        self.assertIsNone(res["promoted"])
        self.assertIsNone(res["promoted_title"])
        import yaml
        data = yaml.safe_load(self._read_plan())
        ms = {m["id"]: m for m in data["milestones"]}
        self.assertEqual(ms["m1"]["status"], "done")
        self.assertEqual(ms["m3"]["status"], "later")


PLAN_TASK_NOT_DONE = """\
project: demo

milestones:
  - id: m1
    title: "First milestone"
    status: active
  - id: m2
    title: "Second milestone"
    status: next

tasks:
  - id: t1
    title: "Task one"
    milestone: m1
    state: done
  - id: t2
    title: "Task two"
    milestone: m1
    state: in_progress
"""


class RefusesIncomplete(MilestoneAdvanceTestCase):
    def test_refuses_when_a_task_is_not_done(self):
        self._write_plan(PLAN_TASK_NOT_DONE)
        before = self._read_plan()
        with self.assertRaises(ValueError) as ctx:
            dispatcher.advance_milestone("demo")
        self.assertIn("1 task", str(ctx.exception))
        self.assertEqual(self._read_plan(), before)   # nothing written


PLAN_ZERO_TASKS = """\
project: demo

milestones:
  - id: m1
    title: "First milestone"
    status: active
  - id: m2
    title: "Second milestone"
    status: next

tasks:
  - id: t1
    title: "Task one"
    milestone: m2
    state: todo
"""


class RefusesZeroTasks(MilestoneAdvanceTestCase):
    def test_refuses_when_active_milestone_has_no_tasks(self):
        self._write_plan(PLAN_ZERO_TASKS)
        before = self._read_plan()
        with self.assertRaises(ValueError):
            dispatcher.advance_milestone("demo")
        self.assertEqual(self._read_plan(), before)


PLAN_NO_ACTIVE = """\
project: demo

milestones:
  - id: m1
    title: "First milestone"
    status: later
  - id: m2
    title: "Second milestone"
    status: next

tasks:
  - id: t1
    title: "Task one"
    milestone: m1
    state: todo
"""


class RefusesNoActive(MilestoneAdvanceTestCase):
    def test_refuses_when_there_is_no_active_milestone(self):
        self._write_plan(PLAN_NO_ACTIVE)
        before = self._read_plan()
        with self.assertRaises(ValueError):
            dispatcher.advance_milestone("demo")
        self.assertEqual(self._read_plan(), before)


PLAN_WITH_COMMENTS = """\
# ATLAS PLAN — header comment that must survive every write.
project: demo

# comment above milestones
milestones:
  - id: m1
    title: "First milestone"
    status: active
  # comment between milestone entries
  - id: m2
    title: "Second milestone"
    status: next
  - id: m3
    title: "Third milestone"
    status: later

# comment above tasks — the tasks section must be byte-identical after the call
tasks:
  - id: t1
    title: "Task one"
    milestone: m1
    state: done
    notes: "keep this note untouched, including punctuation & quotes."
"""


class CommentsAndFormattingPreserved(MilestoneAdvanceTestCase):
    def test_comments_survive_and_tasks_section_is_byte_identical(self):
        self._write_plan(PLAN_WITH_COMMENTS)
        import re
        before = self._read_plan()
        tasks_before = before[before.index("# comment above tasks"):]

        dispatcher.advance_milestone("demo")

        after = self._read_plan()
        self.assertIn("# ATLAS PLAN — header comment that must survive every write.", after)
        self.assertIn("# comment above milestones", after)
        self.assertIn("# comment between milestone entries", after)
        self.assertIn("# comment above tasks — the tasks section must be byte-identical after the call",
                       after)
        tasks_after = after[after.index("# comment above tasks"):]
        self.assertEqual(tasks_before, tasks_after)


if __name__ == "__main__":
    unittest.main()
