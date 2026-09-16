"""Unit tests for per-occurrence editing of recurring life-events (docs/scheduling.md
§12): a single `overrides`/`skips` exception on a recurring event, without touching the rest
of the series. Covers all three layers that must agree:

  - dashboard/dispatcher.py: override_occurrence / skip_occurrence (the writers)
  - dashboard/schedmath.py:  busy_intervals / collision honoring overrides + skips

Isolation: dispatcher.SCHEDULE is monkeypatched onto a tmp_path copy for every dispatcher test —
the real repo schedule.yaml is never touched. The schedmath tests are pure (in-memory dicts),
no I/O at all.

Run (from dashboard/):
    py -3.12 -m unittest test_occurrence_overrides -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dispatcher  # noqa: E402
import schedmath  # noqa: E402

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
  day: '2026-07-29'
  start: '10:00'
  end: '11:00'

# events comment
events:
- id: evt_recur
  title: Standup
  day: '2026-07-22'
  type: appointment
  start: '08:00'
  end: '09:00'
  recur:
    freq: weekly
    interval: 1
- id: evt_single
  title: Dentist
  day: '2026-07-24'
  type: appointment
  start: '13:00'
  end: '14:00'
"""


class OccurrenceApiTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)
        self.schedule_path = tmp_path / "schedule.yaml"
        self.schedule_path.write_text(BASE_SCHEDULE, encoding="utf-8")
        self._orig_schedule = dispatcher.SCHEDULE
        dispatcher.SCHEDULE = self.schedule_path

    def tearDown(self):
        dispatcher.SCHEDULE = self._orig_schedule
        self._tmpdir.cleanup()

    def _raw(self) -> str:
        return self.schedule_path.read_text(encoding="utf-8")

    def _data(self):
        import yaml
        return yaml.safe_load(self._raw())


class OverrideOccurrence(OccurrenceApiTestCase):
    def test_retimes_only_the_target_date(self):
        out = dispatcher.override_occurrence("evt_recur", "2026-08-05", "15:00", "16:00")
        self.assertEqual(out["overrides"], {"2026-08-05": {"start": "15:00", "end": "16:00"}})
        data = self._data()
        ev = next(e for e in data["events"] if e["id"] == "evt_recur")
        # base timing untouched — the series itself keeps its original start/end
        self.assertEqual(ev["start"], "08:00")
        self.assertEqual(ev["end"], "09:00")
        self.assertEqual(ev["overrides"], {"2026-08-05": {"start": "15:00", "end": "16:00"}})
        # other sections preserved
        self.assertIn("header comment that must survive", self._raw())
        self.assertIn("placements comment that must survive", self._raw())
        self.assertEqual(len(data["placements"]), 1)
        self.assertEqual(len(data["events"]), 2)

    def test_second_override_on_a_different_date_keeps_the_first(self):
        dispatcher.override_occurrence("evt_recur", "2026-08-05", "15:00", "16:00")
        dispatcher.override_occurrence("evt_recur", "2026-08-12", "17:00", "18:00")
        data = self._data()
        ev = next(e for e in data["events"] if e["id"] == "evt_recur")
        self.assertEqual(ev["overrides"], {
            "2026-08-05": {"start": "15:00", "end": "16:00"},
            "2026-08-12": {"start": "17:00", "end": "18:00"},
        })

    def test_colliding_override_raises_and_leaves_file_untouched(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            # collides with the on-disk sess_a placement (2026-07-29, 10:00-11:00)
            dispatcher.override_occurrence("evt_recur", "2026-07-29", "10:30", "10:45")
        self.assertEqual(self._raw(), before)

    def test_unknown_event_raises(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            dispatcher.override_occurrence("evt_nope", "2026-08-05", "15:00", "16:00")
        self.assertEqual(self._raw(), before)

    def test_non_recurring_event_raises(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            dispatcher.override_occurrence("evt_single", "2026-07-24", "15:00", "16:00")
        self.assertEqual(self._raw(), before)

    def test_bad_date_or_time_raises(self):
        with self.assertRaises(ValueError):
            dispatcher.override_occurrence("evt_recur", "not-a-date", "15:00", "16:00")
        with self.assertRaises(ValueError):
            dispatcher.override_occurrence("evt_recur", "2026-08-05", "bad", "16:00")

    def test_own_occurrence_on_target_date_is_not_a_self_collision(self):
        # retiming the SAME series' own occurrence (still at its base time on that date, since
        # no prior override) to a time that overlaps its base slot must not self-collide
        out = dispatcher.override_occurrence("evt_recur", "2026-08-05", "08:30", "09:30")
        self.assertEqual(out["overrides"]["2026-08-05"], {"start": "08:30", "end": "09:30"})


class SkipOccurrence(OccurrenceApiTestCase):
    def test_removes_only_the_target_date(self):
        out = dispatcher.skip_occurrence("evt_recur", "2026-08-05")
        self.assertEqual(out["skips"], ["2026-08-05"])
        data = self._data()
        ev = next(e for e in data["events"] if e["id"] == "evt_recur")
        self.assertEqual(ev["skips"], ["2026-08-05"])
        # series otherwise intact — base timing/day/recur untouched
        self.assertEqual(ev["day"], "2026-07-22")
        self.assertEqual(ev["start"], "08:00")
        self.assertEqual(ev["recur"], {"freq": "weekly", "interval": 1})
        self.assertEqual(len(data["events"]), 2)
        self.assertEqual(len(data["placements"]), 1)
        self.assertIn("header comment that must survive", self._raw())

    def test_dedupes_repeated_skip_of_the_same_date(self):
        dispatcher.skip_occurrence("evt_recur", "2026-08-05")
        out = dispatcher.skip_occurrence("evt_recur", "2026-08-05")
        self.assertEqual(out["skips"], ["2026-08-05"])

    def test_skip_clears_an_existing_override_for_that_date(self):
        dispatcher.override_occurrence("evt_recur", "2026-08-05", "15:00", "16:00")
        out = dispatcher.skip_occurrence("evt_recur", "2026-08-05")
        self.assertNotIn("2026-08-05", out.get("overrides") or {})
        self.assertEqual(out["skips"], ["2026-08-05"])

    def test_skip_leaves_other_overrides_intact(self):
        dispatcher.override_occurrence("evt_recur", "2026-08-05", "15:00", "16:00")
        dispatcher.override_occurrence("evt_recur", "2026-08-12", "17:00", "18:00")
        out = dispatcher.skip_occurrence("evt_recur", "2026-08-05")
        self.assertEqual(out["overrides"], {"2026-08-12": {"start": "17:00", "end": "18:00"}})

    def test_unknown_event_raises(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            dispatcher.skip_occurrence("evt_nope", "2026-08-05")
        self.assertEqual(self._raw(), before)

    def test_non_recurring_event_raises(self):
        before = self._raw()
        with self.assertRaises(ValueError):
            dispatcher.skip_occurrence("evt_single", "2026-07-24")
        self.assertEqual(self._raw(), before)

    def test_bad_date_raises(self):
        with self.assertRaises(ValueError):
            dispatcher.skip_occurrence("evt_recur", "not-a-date")


class SchedmathOverridesAndSkips(unittest.TestCase):
    """Direct (in-memory) checks that schedmath's collision guard honors overrides/skips —
    what override_occurrence/skip_occurrence rely on to refuse a bad write."""

    def _sched(self, *, overrides=None, skips=None):
        ev = {"id": "evt1", "title": "Standup", "day": "2026-08-03", "start": "09:00",
              "end": "09:15", "type": "appointment", "recur": {"freq": "weekly", "interval": 1}}
        if overrides is not None:
            ev["overrides"] = overrides
        if skips is not None:
            ev["skips"] = skips
        return {"availability": {"blocks": [], "full_days": []}, "placements": [], "events": [ev]}

    def test_override_collides_at_its_new_time(self):
        sched = self._sched(overrides={"2026-08-10": {"start": "14:00", "end": "14:30"}})
        reason = schedmath.collision(sched, "2026-08-10", "14:15", "14:45")
        self.assertIsNotNone(reason)
        self.assertIn("Standup", reason)

    def test_override_is_free_at_its_old_time(self):
        sched = self._sched(overrides={"2026-08-10": {"start": "14:00", "end": "14:30"}})
        # the base 09:00-09:15 slot is now free on the overridden date
        self.assertIsNone(schedmath.collision(sched, "2026-08-10", "09:00", "09:15"))

    def test_other_occurrences_keep_base_timing(self):
        sched = self._sched(overrides={"2026-08-10": {"start": "14:00", "end": "14:30"}})
        # an untouched occurrence a week later still collides at the BASE time
        reason = schedmath.collision(sched, "2026-08-17", "09:00", "09:15")
        self.assertIsNotNone(reason)

    def test_skipped_date_no_longer_collides(self):
        sched = self._sched(skips=["2026-08-10"])
        self.assertIsNone(schedmath.collision(sched, "2026-08-10", "09:00", "09:15"))
        # the series is otherwise intact — the next occurrence still collides
        reason = schedmath.collision(sched, "2026-08-17", "09:00", "09:15")
        self.assertIsNotNone(reason)

    def test_skip_beats_a_stale_override_on_the_same_date(self):
        # defensive: even if a caller left a stale overrides entry alongside skips for the same
        # date (skip_occurrence itself clears it, but schedmath must not depend on that)
        sched = self._sched(overrides={"2026-08-10": {"start": "14:00", "end": "14:30"}},
                             skips=["2026-08-10"])
        self.assertIsNone(schedmath.collision(sched, "2026-08-10", "14:00", "14:30"))

    def test_all_day_series_override_is_no_longer_a_full_day_block(self):
        sched = self._sched(overrides={"2026-08-10": {"start": "14:00", "end": "14:30"}})
        sched["events"][0].pop("start")
        sched["events"][0].pop("end")   # base series is all-day
        self.assertTrue(schedmath.is_full_day(sched, "2026-08-17"))    # untouched date: full day
        self.assertFalse(schedmath.is_full_day(sched, "2026-08-10"))   # overridden date: timed, not full-day
        reason = schedmath.collision(sched, "2026-08-10", "09:00", "09:15")
        self.assertIsNone(reason)   # free outside the override's 14:00-14:30 window

    def test_all_day_series_skip_is_no_longer_a_full_day_block(self):
        sched = self._sched(skips=["2026-08-10"])
        sched["events"][0].pop("start")
        sched["events"][0].pop("end")
        self.assertTrue(schedmath.is_full_day(sched, "2026-08-17"))
        self.assertFalse(schedmath.is_full_day(sched, "2026-08-10"))


# NOTE — a third test class lived here, covering the control center's render-layer expander
# (`_load_events`) against the same overrides/skips fixtures, to prove the UI and schedmath
# can never disagree about which occurrences exist. The control center is the operator-facing
# layer and is not part of this repo (see README), so those two tests were dropped rather than
# left importing a module that isn't here. The invariant they guarded still holds where it
# matters: schedmath is the single collision guard every write path consults, and it is
# covered above and in test_schedmath.py.


if __name__ == "__main__":
    unittest.main()
