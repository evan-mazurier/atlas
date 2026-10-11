"""Unit tests for cockpit/core/schedmath.py — Contract C1 (deterministic collision guard).

Run (from the repo root):
    py -3.12 -m pytest -q cockpit/core/test_schedmath.py
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import schedmath  # noqa: E402


class ToMin(unittest.TestCase):
    def test_parses_hh_mm(self):
        self.assertEqual(schedmath.to_min("06:00"), 360)
        self.assertEqual(schedmath.to_min("9:05"), 545)
        self.assertEqual(schedmath.to_min("23:59"), 1439)
        self.assertEqual(schedmath.to_min("24:00"), 1440)

    def test_rejects_garbage(self):
        for bad in ("", "abc", "25:00", "12:60", None, 900):
            with self.assertRaises(ValueError):
                schedmath.to_min(bad)


class Overlap(unittest.TestCase):
    def test_touching_edges_do_not_overlap(self):
        self.assertFalse(schedmath.overlap(600, 720, 720, 840))   # 10:00-12:00 vs 12:00-14:00
        self.assertFalse(schedmath.overlap(720, 840, 600, 720))   # order-independent

    def test_true_overlap_detected(self):
        self.assertTrue(schedmath.overlap(600, 780, 720, 840))    # 10:00-13:00 vs 12:00-14:00
        self.assertTrue(schedmath.overlap(600, 900, 650, 700))    # fully nested


class BusyIntervalsAndCollision(unittest.TestCase):
    def test_placement_vs_event_collision(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "placements": [],
            "events": [{"id": "evt1", "title": "Dentist", "day": "2026-08-03",
                        "start": "10:00", "end": "11:00", "type": "appointment"}],
        }
        reason = schedmath.collision(sched, "2026-08-03", "10:30", "11:30")
        self.assertIsNotNone(reason)
        self.assertIn("Dentist", reason)

    def test_placement_vs_placement_collision(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [],
            "placements": [{"session": "s1", "project": "p", "task": "t",
                             "day": "2026-08-03", "start": "09:00", "end": "10:00"}],
        }
        reason = schedmath.collision(sched, "2026-08-03", "09:30", "10:30")
        self.assertIsNotNone(reason)

    def test_no_collision_when_free(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Dentist", "day": "2026-08-03",
                        "start": "10:00", "end": "11:00"}],
            "placements": [],
        }
        # touching edges — right after the event ends
        self.assertIsNone(schedmath.collision(sched, "2026-08-03", "11:00", "12:00"))

    def test_availability_block_collides(self):
        sched = {
            "availability": {"blocks": [{"day": "2026-08-03", "start": "13:00", "end": "17:00"}],
                              "full_days": []},
            "events": [], "placements": [],
        }
        reason = schedmath.collision(sched, "2026-08-03", "14:00", "15:00")
        self.assertIsNotNone(reason)
        self.assertIn("unavailable", reason)

    def test_all_day_event_blocks_everything(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Trip", "day": "2026-08-05", "type": "personal"}],
            "placements": [],
        }
        reason = schedmath.collision(sched, "2026-08-05", "08:00", "09:00")
        self.assertIsNotNone(reason)
        self.assertTrue(schedmath.is_full_day(sched, "2026-08-05"))

    def test_full_day_block_blocks_everything(self):
        sched = {
            "availability": {"blocks": [], "full_days": ["2026-08-06"]},
            "events": [], "placements": [],
        }
        self.assertTrue(schedmath.is_full_day(sched, "2026-08-06"))
        reason = schedmath.collision(sched, "2026-08-06", "08:00", "09:00")
        self.assertIsNotNone(reason)

    def test_non_busy_event_does_not_collide(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Optional thing", "day": "2026-08-03",
                        "start": "10:00", "end": "11:00", "busy": False}],
            "placements": [],
        }
        self.assertIsNone(schedmath.collision(sched, "2026-08-03", "10:00", "11:00"))

    def test_outside_lane_and_bad_order_rejected(self):
        sched = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": []}
        self.assertIsNotNone(schedmath.collision(sched, "2026-08-03", "05:00", "07:00"))   # before 06:00
        self.assertIsNotNone(schedmath.collision(sched, "2026-08-03", "22:30", "23:30"))   # past 23:00
        self.assertIsNotNone(schedmath.collision(sched, "2026-08-03", "10:00", "09:00"))   # end<=start

    def test_ends_exactly_at_23_00_is_valid_but_23_30_is_not(self):
        sched = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": []}
        self.assertIsNone(schedmath.collision(sched, "2026-08-03", "22:00", "23:00"))      # touches ceiling -> ok
        self.assertIsNotNone(schedmath.collision(sched, "2026-08-03", "22:00", "23:30"))   # past ceiling -> rejected

    def test_recurring_event_collides_on_its_occurrence(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Standup", "day": "2026-08-03",
                        "start": "09:00", "end": "09:15", "type": "appointment",
                        "recur": {"freq": "weekly", "interval": 1}}],
            "placements": [],
        }
        # a week later — same weekday
        reason = schedmath.collision(sched, "2026-08-10", "09:00", "09:30")
        self.assertIsNotNone(reason)
        # a day later — not an occurrence
        self.assertIsNone(schedmath.collision(sched, "2026-08-04", "09:00", "09:30"))


class MonthlyRecurrenceCount(unittest.TestCase):
    """Regression: `count` on a monthly recurrence anchored on a day-29–31 must count only
    VALID occurrences (matching cockpit/state.py's `_expand_recurrence`/`_add_period`) — a
    skipped short month (the anchor day doesn't exist) must NOT consume a count slot."""

    def _sched(self, count):
        return {
            "availability": {"blocks": [], "full_days": []},
            "placements": [],
            "events": [{"id": "evt1", "title": "Rent", "day": "2026-01-31",
                        "start": "10:00", "end": "11:00", "type": "appointment",
                        "recur": {"freq": "monthly", "interval": 1, "count": count}}],
        }

    def test_second_valid_occurrence_detected_after_skipped_february(self):
        # day0=2026-01-31, count=2 -> real occurrences are [2026-01-31, 2026-03-31]
        # (February has no 31st and is skipped WITHOUT consuming a count slot).
        sched = self._sched(2)
        self.assertTrue(schedmath._occurs_on(
            dt.date(2026, 1, 31), sched["events"][0]["recur"], dt.date(2026, 3, 31)))
        reason = schedmath.collision(sched, "2026-03-31", "10:30", "10:45")
        self.assertIsNotNone(reason)
        self.assertIn("Rent", reason)

    def test_count_exhausted_before_the_skipped_month_occurrence(self):
        # count=1 -> only the January anchor occurrence; March is beyond the count.
        sched = self._sched(1)
        reason = schedmath.collision(sched, "2026-03-31", "10:30", "10:45")
        self.assertIsNone(reason)

    def test_first_occurrence_still_matches_with_count(self):
        sched = self._sched(2)
        reason = schedmath.collision(sched, "2026-01-31", "10:30", "10:45")
        self.assertIsNotNone(reason)


class Exclusion(unittest.TestCase):
    def test_moving_a_placement_does_not_collide_with_itself(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [],
            "placements": [{"session": "s1", "project": "p", "task": "t",
                             "day": "2026-08-03", "start": "09:00", "end": "10:00"}],
        }
        # same slot, excluding its own session -> free
        self.assertIsNone(schedmath.collision(sched, "2026-08-03", "09:00", "10:00",
                                                exclude_session="s1"))
        # without the exclusion it collides with itself
        self.assertIsNotNone(schedmath.collision(sched, "2026-08-03", "09:00", "10:00"))

    def test_editing_an_event_does_not_collide_with_itself(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Dentist", "day": "2026-08-03",
                        "start": "10:00", "end": "11:00"}],
            "placements": [],
        }
        self.assertIsNone(schedmath.collision(sched, "2026-08-03", "10:00", "11:00",
                                                exclude_event_id="evt1"))
        self.assertIsNotNone(schedmath.collision(sched, "2026-08-03", "10:00", "11:00",
                                                   exclude_event_id="other"))


class DayBusy(unittest.TestCase):
    """An all-day busy event is a HARD BLOCK on the whole day — `day_busy()` is the guard an
    all-day event must pass before landing on/moving to a day: refused if the day already has
    ANY busy item (placement, timed event, or another all-day event)."""

    def test_free_day_is_none(self):
        sched = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": []}
        self.assertIsNone(schedmath.day_busy(sched, "2026-08-03"))

    def test_blocked_by_a_placement(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [],
            "placements": [{"session": "s1", "project": "p", "task": "t",
                             "day": "2026-08-03", "start": "09:00", "end": "10:00"}],
        }
        reason = schedmath.day_busy(sched, "2026-08-03")
        self.assertIsNotNone(reason)

    def test_blocked_by_a_timed_event(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Dentist", "day": "2026-08-03",
                        "start": "10:00", "end": "11:00"}],
            "placements": [],
        }
        reason = schedmath.day_busy(sched, "2026-08-03")
        self.assertIsNotNone(reason)
        self.assertIn("Dentist", reason)

    def test_blocked_by_another_all_day_event(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Trip", "day": "2026-08-03"}],   # no start/end -> all-day
            "placements": [],
        }
        reason = schedmath.day_busy(sched, "2026-08-03")
        self.assertIsNotNone(reason)

    def test_self_exclusion_by_event_id(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Trip", "day": "2026-08-03"}],
            "placements": [],
        }
        self.assertIsNone(schedmath.day_busy(sched, "2026-08-03", exclude_event_id="evt1"))

    def test_self_exclusion_by_session(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [],
            "placements": [{"session": "s1", "project": "p", "task": "t",
                             "day": "2026-08-03", "start": "09:00", "end": "10:00"}],
        }
        self.assertIsNone(schedmath.day_busy(sched, "2026-08-03", exclude_session="s1"))


class EarliestFree(unittest.TestCase):
    def test_respects_existing_busy_intervals(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Dentist", "day": "2026-08-03",
                        "start": "06:00", "end": "09:00"}],
            "placements": [{"session": "s1", "project": "p", "task": "t",
                             "day": "2026-08-03", "start": "09:00", "end": "10:00"}],
        }
        self.assertEqual(schedmath.earliest_free(sched, "2026-08-03", 30), "10:00")

    def test_06_00_floor(self):
        sched = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": []}
        self.assertEqual(schedmath.earliest_free(sched, "2026-08-03", 60), "06:00")

    def test_23_00_ceiling_none_when_nothing_fits(self):
        sched = {
            "availability": {"blocks": [{"day": "2026-08-03", "start": "06:00", "end": "22:45"}],
                              "full_days": []},
            "events": [], "placements": [],
        }
        self.assertIsNone(schedmath.earliest_free(sched, "2026-08-03", 30))
        self.assertEqual(schedmath.earliest_free(sched, "2026-08-03", 15), "22:45")

    def test_none_on_full_day(self):
        sched = {"availability": {"blocks": [], "full_days": ["2026-08-06"]}, "events": [], "placements": []}
        self.assertIsNone(schedmath.earliest_free(sched, "2026-08-06", 30))


class FillFreeSlots(unittest.TestCase):
    def _spans(self, slots):
        return [(s["day"], schedmath.to_min(s["start"]), schedmath.to_min(s["end"])) for s in slots]

    def test_fills_a_single_free_day(self):
        sched = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": []}
        slots = schedmath.fill_free_slots(sched, "2026-08-03", 120)
        self.assertEqual(slots, [{"day": "2026-08-03", "start": "06:00", "end": "08:00"}])

    def test_never_overlaps_existing_busy_intervals(self):
        sched = {
            "availability": {"blocks": [], "full_days": []},
            "events": [{"id": "evt1", "title": "Dentist", "day": "2026-08-03",
                        "start": "09:00", "end": "10:00"}],
            "placements": [{"session": "s1", "project": "p", "task": "t",
                             "day": "2026-08-03", "start": "06:00", "end": "08:00"}],
        }
        slots = schedmath.fill_free_slots(sched, "2026-08-03", 120)
        spans = self._spans(slots)
        for _day, s0, e0 in spans:
            self.assertFalse(schedmath.overlap(s0, e0, 540, 600))   # 09:00-10:00 (Dentist)
            self.assertFalse(schedmath.overlap(s0, e0, 360, 480))   # 06:00-08:00 (s1)
        total = sum(e - s for _d, s, e in spans)
        self.assertEqual(total, 120)

    def test_spills_into_a_later_day_when_the_first_is_too_small(self):
        sched = {
            "availability": {"blocks": [{"day": "2026-08-03", "start": "07:00", "end": "23:00"}],
                              "full_days": []},
            "events": [], "placements": [],
        }
        # 2026-08-03 only has 06:00-07:00 free (60min); ask for 90min -> spills into 08-04
        slots = schedmath.fill_free_slots(sched, "2026-08-03", 90)
        self.assertEqual(slots[0], {"day": "2026-08-03", "start": "06:00", "end": "07:00"})
        self.assertEqual(slots[1]["day"], "2026-08-04")
        total = sum(schedmath.to_min(s["end"]) - schedmath.to_min(s["start"]) for s in slots)
        self.assertEqual(total, 90)

    def test_respects_horizon_and_returns_partial_fill(self):
        sched = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": []}
        # every day fully unavailable except a 1-day horizon (30min free per non-blocked day) —
        # here: block every day out entirely via full_days for a wide range except none, so
        # instead cap the horizon tightly and ask for more than 1 day can hold.
        needed = 17 * 60 + 60   # more than one 17h day (06:00-23:00) can hold
        slots = schedmath.fill_free_slots(sched, "2026-08-03", needed, horizon_days=1)
        total = sum(schedmath.to_min(s["end"]) - schedmath.to_min(s["start"]) for s in slots)
        self.assertEqual(total, 17 * 60)          # only day 1 (2026-08-03) was walked
        self.assertLess(total, needed)            # partial fill, never raises
        self.assertTrue(all(s["day"] == "2026-08-03" for s in slots))

    def test_full_day_is_skipped_entirely(self):
        sched = {"availability": {"blocks": [], "full_days": ["2026-08-03"]}, "events": [], "placements": []}
        slots = schedmath.fill_free_slots(sched, "2026-08-03", 60, horizon_days=3)
        self.assertTrue(all(s["day"] != "2026-08-03" for s in slots))
        total = sum(schedmath.to_min(s["end"]) - schedmath.to_min(s["start"]) for s in slots)
        self.assertEqual(total, 60)

    def test_slot_cap_splits_a_big_opening_into_multiple_slices(self):
        sched = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": []}
        slots = schedmath.fill_free_slots(sched, "2026-08-03", 180, slot_cap_min=60)
        self.assertEqual(len(slots), 3)
        for s in slots:
            self.assertEqual(schedmath.to_min(s["end"]) - schedmath.to_min(s["start"]), 60)

    def test_zero_or_negative_minutes_needed_returns_empty(self):
        sched = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": []}
        self.assertEqual(schedmath.fill_free_slots(sched, "2026-08-03", 0), [])
        self.assertEqual(schedmath.fill_free_slots(sched, "2026-08-03", -10), [])


if __name__ == "__main__":
    unittest.main()
