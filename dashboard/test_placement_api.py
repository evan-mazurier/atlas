"""Unit tests for the Contract C2 per-item move/delete API (dashboard/dispatcher.py):
move_placement, delete_placement, move_event, and the approve_planner pinned-preservation
branch. Server-validated against the collision guard (schedmath, Contract C1) before ANY
write — an invalid target is refused with a reason and the file is left byte-identical.

Isolation: dispatcher.SCHEDULE (and dispatcher.JOBS_DIR, for the approve_planner test) are
monkeypatched onto a tmp_path copy for every test — the real repo schedule.yaml/jobs/ are
never touched.

Run (from dashboard/):
    py -3.12 -m unittest test_placement_api -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dispatcher  # noqa: E402

BASE_SCHEDULE = """\
# ATLAS GLOBAL SCHEDULE — header comment that must survive every write.
availability:
  blocks: []
  full_days: []

# placements comment that must survive too
placements:
- session: sess_a
  project: demo
  task: write-docs
  day: '2026-07-22'
  start: '10:00'
  end: '12:00'

# events comment
events:
- id: evt_1
  title: Dentist
  day: '2026-07-22'
  type: appointment
  start: '13:00'
  end: '14:00'
"""


class PlacementApiTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)
        self.schedule_path = tmp_path / "schedule.yaml"
        self.schedule_path.write_text(BASE_SCHEDULE, encoding="utf-8")
        self.jobs_dir = tmp_path / "jobs"
        self.jobs_dir.mkdir()
        self._orig_schedule = dispatcher.SCHEDULE
        self._orig_jobs = dispatcher.JOBS_DIR
        dispatcher.SCHEDULE = self.schedule_path
        dispatcher.JOBS_DIR = self.jobs_dir

    def tearDown(self):
        dispatcher.SCHEDULE = self._orig_schedule
        dispatcher.JOBS_DIR = self._orig_jobs
        self._tmpdir.cleanup()

    def _raw(self) -> str:
        return self.schedule_path.read_text(encoding="utf-8")


class MovePlacement(PlacementApiTestCase):
    def test_valid_move_rewrites_fields_and_pins(self):
        out = dispatcher.move_placement("sess_a", "2026-07-23", "09:00", "11:00")
        self.assertEqual(out["day"], "2026-07-23")
        self.assertEqual(out["start"], "09:00")
        self.assertEqual(out["end"], "11:00")
        self.assertTrue(out["pinned"])
        raw = self._raw()
        self.assertIn("header comment that must survive", raw)
        self.assertIn("events comment", raw)
        self.assertIn("Dentist", raw)          # events untouched
        import yaml
        data = yaml.safe_load(raw)
        pl = next(p for p in data["placements"] if p["session"] == "sess_a")
        self.assertEqual(pl["day"], "2026-07-23")
        self.assertEqual(pl["start"], "09:00")
        self.assertEqual(pl["end"], "11:00")
        self.assertTrue(pl["pinned"])
        self.assertEqual(len(data["events"]), 1)

    def test_colliding_move_raises_and_leaves_file_untouched(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            # collides with the on-disk Dentist event (13:00-14:00) on the same day
            dispatcher.move_placement("sess_a", "2026-07-22", "13:30", "14:30")
        self.assertEqual(self._raw(), before)

    def test_unknown_session_raises(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            dispatcher.move_placement("no-such-session", "2026-07-23", "09:00", "10:00")
        self.assertEqual(self._raw(), before)

    def test_moving_onto_itself_is_not_a_self_collision(self):
        # same day/slot the placement is already in should never collide with itself
        out = dispatcher.move_placement("sess_a", "2026-07-22", "10:00", "12:00")
        self.assertEqual(out["start"], "10:00")


class AddPlacement(PlacementApiTestCase):
    def test_valid_add_writes_a_pinned_placement_with_a_fresh_session(self):
        pl = dispatcher.add_placement("demo", "new-task", "2026-07-23", "09:00", "11:00")
        self.assertTrue(pl["session"])
        self.assertNotEqual(pl["session"], "sess_a")     # distinct from the existing one
        self.assertEqual(pl["day"], "2026-07-23")
        self.assertEqual(pl["start"], "09:00")
        self.assertEqual(pl["end"], "11:00")
        self.assertTrue(pl["pinned"])
        import yaml
        data = yaml.safe_load(self._raw())
        sessions = {p["session"] for p in data["placements"]}
        self.assertIn("sess_a", sessions)                # the original placement survives
        self.assertIn(pl["session"], sessions)
        self.assertEqual(len(data["placements"]), 2)
        self.assertEqual(len(data["events"]), 1)          # events untouched
        self.assertIn("header comment that must survive", self._raw())

    def test_colliding_add_raises_and_leaves_file_untouched(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            # collides with the on-disk sess_a placement (10:00-12:00) on the same day
            dispatcher.add_placement("demo", "new-task", "2026-07-22", "10:30", "11:30")
        self.assertEqual(self._raw(), before)

    def test_missing_project_or_task_raises(self):
        with self.assertRaises(ValueError):
            dispatcher.add_placement("", "new-task", "2026-07-23", "09:00", "10:00")
        with self.assertRaises(ValueError):
            dispatcher.add_placement("demo", "", "2026-07-23", "09:00", "10:00")

    def test_fresh_ids_never_collide_across_repeated_calls(self):
        seen = set()
        for _ in range(20):
            pl = dispatcher.add_placement("demo", "t", "2026-09-01", "06:00", "06:05")
            self.assertNotIn(pl["session"], seen)
            seen.add(pl["session"])
            dispatcher.delete_placement(pl["session"])   # keep the slot free for the next add


class DeletePlacement(PlacementApiTestCase):
    def test_delete_removes_placement_and_preserves_events(self):
        dispatcher.delete_placement("sess_a")
        import yaml
        data = yaml.safe_load(self._raw())
        self.assertEqual(data["placements"], [])
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["title"], "Dentist")
        self.assertIn("header comment that must survive", self._raw())

    def test_delete_unknown_session_raises(self):
        with self.assertRaises(ValueError):
            dispatcher.delete_placement("nope")


class MoveEvent(PlacementApiTestCase):
    def test_valid_move_rewrites_timing(self):
        out = dispatcher.move_event("evt_1", "2026-07-24", "15:00", "16:00")
        self.assertEqual(out["day"], "2026-07-24")
        self.assertEqual(out["start"], "15:00")
        self.assertEqual(out["end"], "16:00")
        import yaml
        data = yaml.safe_load(self._raw())
        ev = next(e for e in data["events"] if e["id"] == "evt_1")
        self.assertEqual(ev["day"], "2026-07-24")
        self.assertEqual(len(data["placements"]), 1)   # placements untouched

    def test_colliding_move_raises_and_leaves_file_untouched(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            # collides with the on-disk placement (sess_a, 10:00-12:00) on the same day
            dispatcher.move_event("evt_1", "2026-07-22", "10:30", "11:30")
        self.assertEqual(self._raw(), before)

    def test_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            dispatcher.move_event("evt_nope", "2026-07-24", "09:00", "10:00")

    def test_day_only_move_keeps_existing_timing(self):
        out = dispatcher.move_event("evt_1", "2026-07-25", None, None)
        self.assertEqual(out["day"], "2026-07-25")
        self.assertEqual(out["start"], "13:00")
        self.assertEqual(out["end"], "14:00")


class SaveEventAllDayHardBlock(PlacementApiTestCase):
    """An all-day busy event occupies the WHOLE day (06:00-23:00): saving/moving one onto a day
    that already has ANY busy item — a placement, a timed event, or another all-day event — is
    refused, same as any other collision (docs/scheduling.md §12)."""

    def test_all_day_event_blocked_by_existing_placement(self):
        # a fresh day with only a placement, nothing else
        import yaml
        data = yaml.safe_load(self._raw())
        data["placements"] = [{"session": "s2", "project": "demo", "task": "solo",
                                "day": "2026-08-01", "start": "09:00", "end": "10:00"}]
        data["events"] = []
        self.schedule_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        before = self._raw()
        with self.assertRaises(ValueError):
            dispatcher.save_event({"title": "Trip", "day": "2026-08-01", "type": "personal"})
        self.assertEqual(self._raw(), before)

    def test_all_day_event_blocked_by_existing_timed_event(self):
        # a fresh day with only a timed event, no placement
        import yaml
        data = yaml.safe_load(self._raw())
        data["placements"] = []
        data["events"] = [{"id": "evt_solo", "title": "Dentist", "day": "2026-08-02",
                            "type": "appointment", "start": "10:00", "end": "11:00"}]
        self.schedule_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        before = self._raw()
        with self.assertRaises(ValueError):
            dispatcher.save_event({"title": "Trip", "day": "2026-08-02", "type": "personal"})
        self.assertEqual(self._raw(), before)

    def test_all_day_event_blocked_by_existing_all_day_event(self):
        # two all-day busy events on the SAME day are now mutually exclusive — the second is refused
        dispatcher.save_event({"title": "Trip", "day": "2026-08-03", "type": "personal"})
        before = self._raw()
        with self.assertRaises(ValueError):
            dispatcher.save_event({"title": "Conference", "day": "2026-08-03", "type": "personal"})
        self.assertEqual(self._raw(), before)

    def test_all_day_event_on_empty_day_succeeds(self):
        out = dispatcher.save_event({"title": "Trip", "day": "2026-08-10", "type": "personal"})
        self.assertEqual(out["day"], "2026-08-10")
        self.assertNotIn("start", out)
        self.assertNotIn("end", out)

    def test_editing_an_all_day_event_on_its_own_day_does_not_self_collide(self):
        saved = dispatcher.save_event({"title": "Trip", "day": "2026-08-15", "type": "personal"})
        # re-save with the same id on the same day (e.g. a title edit) must not collide with itself
        out = dispatcher.save_event({"id": saved["id"], "title": "Trip (renamed)",
                                      "day": "2026-08-15", "type": "personal"})
        self.assertEqual(out["day"], "2026-08-15")

    def test_moving_an_all_day_event_from_a_day_it_alone_occupies_to_an_empty_day_succeeds(self):
        saved = dispatcher.save_event({"title": "Trip", "day": "2026-08-20", "type": "personal"})
        out = dispatcher.move_event(saved["id"], "2026-08-21")
        self.assertEqual(out["day"], "2026-08-21")


class ApprovePlannerPinned(PlacementApiTestCase):
    def _write_job(self, **over):
        job = {
            "id": "job_sched", "status": "needs_input", "mode": "schedule",
            "kind": "planner", "project": "atlas-core",
        }
        job.update(over)
        (self.jobs_dir / "job_sched.json").write_text(json.dumps(job), encoding="utf-8")
        return job

    def test_pinned_placement_survives_a_reschedule_and_wins_on_collision(self):
        # Pin the existing placement by hand-editing it first (sets pinned: True on disk).
        dispatcher.move_placement("sess_a", "2026-07-22", "10:00", "12:00")
        self._write_job()

        proposed_yaml = """\
availability:
  blocks: []
  full_days: []
placements:
- session: sess_b
  project: demo
  task: other-task
  day: '2026-07-22'
  start: '10:30'
  end: '11:30'
- session: sess_c
  project: demo
  task: free-task
  day: '2026-07-22'
  start: '15:00'
  end: '16:00'
"""
        dispatcher.approve_planner("job_sched", proposed_yaml)
        import yaml
        data = yaml.safe_load(self._raw())
        sessions = {p["session"]: p for p in data["placements"]}
        # the pinned hand-edit survives untouched
        self.assertIn("sess_a", sessions)
        self.assertTrue(sessions["sess_a"]["pinned"])
        self.assertEqual(sessions["sess_a"]["start"], "10:00")
        # sess_b collided with the pinned placement (10:00-12:00 overlaps 10:30-11:30) -> dropped
        self.assertNotIn("sess_b", sessions)
        # sess_c didn't collide with anything -> kept
        self.assertIn("sess_c", sessions)
        # the on-disk life-event is preserved too
        self.assertEqual(len(data["events"]), 1)
        self.assertEqual(data["events"][0]["title"], "Dentist")
        # NOTE: approve_planner writes the planner's OWN proposal text wholesale (splicing only
        # the events + pinned-placement regions back in) — a header comment outside those
        # regions is legitimately replaced by the new proposal, same as before Contract C2.

    def test_pinned_placement_colliding_with_new_unavailable_block_rejects_write(self):
        # Pin the existing placement (sess_a, 2026-07-22 10:00-12:00).
        dispatcher.move_placement("sess_a", "2026-07-22", "10:00", "12:00")
        self._write_job()
        before = self._raw()

        # The re-schedule proposal marks 2026-07-22 09:00-13:00 unavailable — which now
        # overlaps the preserved pinned placement (10:00-12:00).
        proposed_yaml = """\
availability:
  blocks:
  - day: '2026-07-22'
    start: '09:00'
    end: '13:00'
  full_days: []
placements: []
"""
        with self.assertRaises(ValueError) as ctx:
            dispatcher.approve_planner("job_sched", proposed_yaml)
        self.assertIn("pinned", str(ctx.exception).lower())
        self.assertEqual(self._raw(), before)   # nothing written on rejection

    def test_pinned_placement_colliding_with_new_full_day_rejects_write(self):
        dispatcher.move_placement("sess_a", "2026-07-22", "10:00", "12:00")
        self._write_job()
        before = self._raw()

        proposed_yaml = """\
availability:
  blocks: []
  full_days: ['2026-07-22']
placements: []
"""
        with self.assertRaises(ValueError):
            dispatcher.approve_planner("job_sched", proposed_yaml)
        self.assertEqual(self._raw(), before)

    def test_pinned_placement_colliding_with_new_busy_event_rejects_write(self):
        # Pin the placement first (no conflict yet), THEN a new busy event conflicting with
        # the pinned slot is added directly on disk (approve_planner reads the CURRENT on-disk
        # events, never the proposal's — mirroring how a second, unrelated flow might add it).
        dispatcher.move_placement("sess_a", "2026-07-22", "10:00", "12:00")
        import yaml
        data = yaml.safe_load(self._raw())
        data["events"].append({"id": "evt_2", "title": "Surgery", "day": "2026-07-22",
                                "type": "appointment", "start": "11:00", "end": "13:00"})
        self.schedule_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

        self._write_job()
        before = self._raw()

        proposed_yaml = """\
availability:
  blocks: []
  full_days: []
placements: []
"""
        with self.assertRaises(ValueError):
            dispatcher.approve_planner("job_sched", proposed_yaml)
        self.assertEqual(self._raw(), before)

    def test_pinned_placement_on_still_free_time_still_succeeds(self):
        # Baseline: a pinned placement that does NOT collide with the final availability/events
        # is preserved and the write succeeds normally (no false-positive rejection).
        dispatcher.move_placement("sess_a", "2026-07-22", "10:00", "12:00")
        self._write_job()

        proposed_yaml = """\
availability:
  blocks:
  - day: '2026-07-22'
    start: '15:00'
    end: '17:00'
  full_days: []
placements: []
"""
        dispatcher.approve_planner("job_sched", proposed_yaml)
        import yaml
        data = yaml.safe_load(self._raw())
        sessions = {p["session"]: p for p in data["placements"]}
        self.assertIn("sess_a", sessions)
        self.assertTrue(sessions["sess_a"]["pinned"])


if __name__ == "__main__":
    unittest.main()
