"""Unit tests for schedmath honoring per-occurrence `overrides`/`skips` on recurring life-events
(docs/scheduling-part2.md section 8b). Pure in-memory dicts, no I/O. (The writers and the v1 state
expander were removed with Atlas v1.)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import schedmath  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
