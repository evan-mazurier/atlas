"""Unit tests for the planner's `mode="reestimate"` capability (docs/scheduling.md §7):

  - dispatcher.enqueue_planner(mode="reestimate", ...) creates a job carrying project/task/goal.
  - worker.detect_reestimate parses a well-formed planner proposal into a `remaining` value
    (and returns None on garbage/incomplete text).
  - dispatcher.approve_planner's `reestimate` branch reads the proposed `remaining` off the job
    and applies it via save_remainder(project, task, remaining=<value>) — NEVER writing
    schedule.yaml directly.

The LLM/agent call itself is never invoked here (worker.run_claude / process_planner are not
exercised) — only the plumbing: job creation, proposal parsing, and the approval gate.

Isolation: dispatcher.SCHEDULE, dispatcher.JOBS_DIR, and dispatcher.REGISTRY are monkeypatched
onto a tmp_path copy for every test — the real repo files are never touched.

Run (from dashboard/):
    py -3.12 -m unittest test_planner_reestimate -v
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
import worker  # noqa: E402


def _plan_yaml(remaining: str = "4h", estimate: str = "8h") -> str:
    return f"""\
project: demo

tasks:
  - id: t1
    title: "Write docs"
    state: in_progress
    estimate: {estimate}
    remaining: {remaining}
    sessions_done: [sess_1]
"""


class ReestimateTestCase(unittest.TestCase):
    def setUp(self):
        import yaml
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)

        self.schedule_path = tmp_path / "schedule.yaml"
        self.schedule_path.write_text(
            "availability:\n  blocks: []\n  full_days: []\nplacements: []\nevents: []\n",
            encoding="utf-8")
        self.jobs_dir = tmp_path / "jobs"
        self.jobs_dir.mkdir()
        self.plan_path = tmp_path / "plan.yaml"
        self.plan_path.write_text(_plan_yaml(), encoding="utf-8")
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

    def _read_plan(self) -> dict:
        import yaml
        return yaml.safe_load(self.plan_path.read_text(encoding="utf-8"))


class EnqueuePlannerReestimate(ReestimateTestCase):
    def test_creates_a_job_carrying_project_task_goal(self):
        job = dispatcher.enqueue_planner(
            project_id="demo", project_path=str(self.plan_path.parent),
            goal="I think I need ~3h more", mode="reestimate", task_id="t1")
        self.assertEqual(job["kind"], "planner")
        self.assertEqual(job["mode"], "reestimate")
        self.assertEqual(job["project"], "demo")
        self.assertEqual(job["task_id"], "t1")
        self.assertEqual(job["task"], "I think I need ~3h more")
        self.assertEqual(job["status"], "queued")
        # the job record round-trips through disk, same as any other job
        reloaded = dispatcher.get_job(job["id"])
        self.assertEqual(reloaded["mode"], "reestimate")
        self.assertEqual(reloaded["task_id"], "t1")

    def test_rejects_missing_task_id(self):
        with self.assertRaises(ValueError):
            dispatcher.enqueue_planner(
                project_id="demo", project_path=str(self.plan_path.parent),
                goal="I think I need ~3h more", mode="reestimate")

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            dispatcher.enqueue_planner(
                project_id="demo", project_path=str(self.plan_path.parent),
                goal="hi", mode="bogus")


class DetectReestimate(unittest.TestCase):
    def test_parses_a_well_formed_proposal(self):
        text = (
            "Some chatter before.\n\n"
            "PROPOSAL\nSUMMARY:\nGiven one session already burned 2h and the operator's note, 3h "
            "remaining is more realistic than the current 4h.\n"
            "--- remaining ---\n3h\n--- end ---\n"
        )
        prop = worker.detect_reestimate(text)
        self.assertIsNotNone(prop)
        self.assertEqual(prop["remaining"], "3h")
        self.assertIn("3h remaining", prop["summary"])

    def test_returns_none_on_garbage(self):
        self.assertIsNone(worker.detect_reestimate("I am still thinking about it."))
        self.assertIsNone(worker.detect_reestimate(""))
        self.assertIsNone(worker.detect_reestimate(None))

    def test_returns_none_when_remaining_marker_is_empty(self):
        text = "PROPOSAL\nSUMMARY:\nNothing to say.\n--- remaining ---\n\n--- end ---\n"
        self.assertIsNone(worker.detect_reestimate(text))


class ApprovePlannerReestimate(ReestimateTestCase):
    def _queued_job(self, proposed_remaining="5h"):
        job = dispatcher.enqueue_planner(
            project_id="demo", project_path=str(self.plan_path.parent),
            goal="I think I need ~3h more", mode="reestimate", task_id="t1")
        job["status"] = "needs_input"
        job["proposal"] = {"summary": "Revised to 5h.", "remaining": proposed_remaining}
        dispatcher._write(job)
        return job

    def test_approve_calls_save_remainder_with_the_proposed_value(self):
        job = self._queued_job("5h")
        calls = []
        orig = dispatcher.save_remainder

        def spy(project_id, task_id, remaining="", complete=False):
            calls.append((project_id, task_id, remaining, complete))
            return orig(project_id, task_id, remaining=remaining, complete=complete)

        dispatcher.save_remainder = spy
        try:
            res = dispatcher.approve_planner(job["id"], "")
        finally:
            dispatcher.save_remainder = orig

        self.assertTrue(res["ok"])
        self.assertEqual(calls, [("demo", "t1", "5h", False)])
        plan = self._read_plan()
        task = plan["tasks"][0]
        self.assertEqual(str(task["remaining"]), "5h")
        self.assertEqual(str(task["state"]), "in_progress")   # save_remainder's non-complete path

        reloaded = dispatcher.get_job(job["id"])
        self.assertEqual(reloaded["status"], "done")
        self.assertTrue(reloaded["proposal_written"])

    def test_approve_honors_an_edited_remaining_over_the_proposal(self):
        # The operator edited the proposed number in the (editable) card before clicking approve — the
        # frontend posts the EDITED textarea value as `plan_yaml` regardless of mode
        # (the control center front-end's plannerApprove); the reestimate branch must use it, not the
        # original proposal it's overriding.
        job = self._queued_job("5h")   # planner proposed 5h
        calls = []
        orig = dispatcher.save_remainder

        def spy(project_id, task_id, remaining="", complete=False):
            calls.append((project_id, task_id, remaining, complete))
            return orig(project_id, task_id, remaining=remaining, complete=complete)

        dispatcher.save_remainder = spy
        try:
            res = dispatcher.approve_planner(job["id"], "2h30")   # the operator edited it down
        finally:
            dispatcher.save_remainder = orig

        self.assertTrue(res["ok"])
        self.assertEqual(calls, [("demo", "t1", "2h30", False)])   # the EDIT reached save_remainder
        plan = self._read_plan()
        task = plan["tasks"][0]
        self.assertEqual(str(task["remaining"]), "2h30")            # not the original 5h proposal

    def test_approve_rejects_an_edited_value_that_is_not_a_sane_duration(self):
        job = self._queued_job("5h")
        with self.assertRaises(ValueError):
            dispatcher.approve_planner(job["id"], "not a duration")
        # rejected before it ever reached the plan file — the original task numbers are untouched
        plan = self._read_plan()
        self.assertEqual(str(plan["tasks"][0]["remaining"]), "4h")

    def test_approve_falls_back_to_the_proposal_when_plan_yaml_is_blank(self):
        # mirrors the unedited-card path above but pinned specifically to whitespace-only input,
        # which the frontend can send if the textarea is cleared then re-submitted empty.
        job = self._queued_job("5h")
        res = dispatcher.approve_planner(job["id"], "   ")
        self.assertTrue(res["ok"])
        plan = self._read_plan()
        self.assertEqual(str(plan["tasks"][0]["remaining"]), "5h")

    def test_approve_never_writes_schedule_yaml_directly(self):
        # A NOT-done session already covers 2h for t1 (no sessions_done), and the proposed
        # `remaining` (2h) matches what's already scheduled, so save_remainder's auto-reconcile
        # is a legitimate no-op — proving the reestimate branch itself never touches
        # schedule.yaml (unlike the schedule-mode branch, which writes it on every approve).
        self.plan_path.write_text(_plan_yaml(remaining="4h", estimate="8h").replace(
            "sessions_done: [sess_1]", "sessions_done: []"), encoding="utf-8")
        self.schedule_path.write_text(
            "availability:\n  blocks: []\n  full_days: []\n"
            "placements:\n"
            "- session: sess_1\n  project: demo\n  task: t1\n"
            "  day: '2026-07-22'\n  start: '10:00'\n  end: '12:00'\n"
            "events: []\n", encoding="utf-8")
        before = self.schedule_path.read_text(encoding="utf-8")
        job = self._queued_job("2h")
        res = dispatcher.approve_planner(job["id"], "")
        self.assertTrue(res["ok"])
        after = self.schedule_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_approve_raises_when_no_proposal_captured(self):
        job = dispatcher.enqueue_planner(
            project_id="demo", project_path=str(self.plan_path.parent),
            goal="I think I need ~3h more", mode="reestimate", task_id="t1")
        with self.assertRaises(ValueError):
            dispatcher.approve_planner(job["id"], "")

    def test_approve_raises_for_unknown_task_id(self):
        job = dispatcher.enqueue_planner(
            project_id="demo", project_path=str(self.plan_path.parent),
            goal="note", mode="reestimate", task_id="no-such-task")
        job["status"] = "needs_input"
        job["proposal"] = {"summary": "x", "remaining": "1h"}
        dispatcher._write(job)
        with self.assertRaises(ValueError):
            dispatcher.approve_planner(job["id"], "")


if __name__ == "__main__":
    unittest.main()
