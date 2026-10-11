"""Atlas — the work-block layer's deterministic math (cockpit/core/schedmath.py).

Covers the three things the UI is not allowed to re-derive for itself: which blocks exist and
where they may sit (`work_blocks` / `block_collision`), how the ordered set fans out over the
3 dispatch lanes + 1 manual track (`fill_lanes`), and the order guard (`order_violations` /
`block_sequence`). Pure/stdlib, like the module under test — no YAML, no cockpit import.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import schedmath as sm   # noqa: E402


# -- fixtures ---------------------------------------------------------------------------------
def _sched(**kw):
    base = {"availability": {"blocks": [], "full_days": []}, "events": [], "placements": [],
            "work_blocks": []}
    base.update(kw)
    return base


def _blk(bid, day, start, end, label=""):
    return {"id": bid, "day": day, "start": start, "end": end, "label": label}


def _t(tid, est_min, project="p", manual=False):
    return {"id": tid, "project": project, "estimate_min": est_min, "manual": manual}


# -- work_blocks: normalization + ordering ----------------------------------------------------
def test_work_blocks_normalizes_and_sorts_by_day_then_start():
    s = _sched(work_blocks=[
        _blk("b2", "2026-08-24", "13:00", "17:00", "Afternoon"),
        _blk("b3", "2026-08-25", "09:00", "12:00"),
        _blk("b1", "2026-08-24", "07:30", "11:30", "Morning"),
    ])
    assert [b["id"] for b in sm.work_blocks(s)] == ["b1", "b2", "b3"]
    b1 = sm.work_blocks(s, "2026-08-24")[0]
    assert (b1["start_min"], b1["end_min"], b1["label"]) == (7 * 60 + 30, 11 * 60 + 30, "Morning")


def test_work_blocks_drops_unusable_entries_rather_than_raising():
    s = _sched(work_blocks=[
        _blk("ok", "2026-08-24", "09:00", "10:00"),
        _blk("noend", "2026-08-24", "09:00", None),
        _blk("backwards", "2026-08-24", "12:00", "10:00"),
        _blk("badday", "not-a-day", "09:00", "10:00"),
        {"day": "2026-08-24", "start": "09:00", "end": "10:00"},    # no id
        "not-a-dict",
    ])
    assert [b["id"] for b in sm.work_blocks(s)] == ["ok"]


def test_block_span_min_from_raw_and_normalized():
    raw = _blk("b", "2026-08-24", "07:30", "11:30")
    assert sm.block_span_min(raw) == 240
    assert sm.block_span_min(sm.work_blocks(_sched(work_blocks=[raw]))[0]) == 240


# -- block_collision: blocks respect unavailable time and each other, NOT placements -----------
def test_block_may_overlap_a_legacy_placement():
    """Placements are the per-task layer blocks REPLACE - one sitting under a new block is not a
    conflict, and treating it as one would make every block unplaceable on a scheduled day."""
    s = _sched(placements=[{"session": "s1", "project": "p", "task": "t",
                            "day": "2026-08-24", "start": "08:00", "end": "10:00"}])
    assert sm.block_collision(s, "2026-08-24", "07:30", "11:30") is None


def test_block_collides_with_unavailable_time():
    s = _sched(availability={"blocks": [{"day": "2026-08-24", "start": "09:00", "end": "10:00"}],
                             "full_days": []})
    assert "unavailable" in sm.block_collision(s, "2026-08-24", "07:30", "11:30")
    assert sm.block_collision(s, "2026-08-24", "10:00", "12:00") is None   # touching edges are fine


def test_block_collides_with_a_busy_life_event():
    s = _sched(events=[{"id": "e1", "title": "Dentist", "day": "2026-08-24",
                        "start": "10:00", "end": "11:00"}])
    assert "Dentist" in sm.block_collision(s, "2026-08-24", "07:30", "11:30")


def test_block_collides_with_another_block_but_not_with_itself():
    s = _sched(work_blocks=[_blk("b1", "2026-08-24", "07:30", "11:30", "Morning")])
    assert "Morning" in sm.block_collision(s, "2026-08-24", "10:00", "12:00")
    # editing b1 in place must not collide with its own current slot
    assert sm.block_collision(s, "2026-08-24", "07:00", "11:00", exclude_block_id="b1") is None


def test_block_rejects_full_day_bad_range_and_outside_the_lane():
    s = _sched(availability={"blocks": [], "full_days": ["2026-08-24"]})
    assert "fully unavailable" in sm.block_collision(s, "2026-08-24", "09:00", "10:00")
    assert "must be after" in sm.block_collision(_sched(), "2026-08-24", "12:00", "10:00")
    assert "06:00" in sm.block_collision(_sched(), "2026-08-24", "05:00", "07:00")
    assert "06:00" in sm.block_collision(_sched(), "2026-08-24", "22:00", "23:30")


# -- fill_lanes: the greedy derivation --------------------------------------------------------
def test_fill_lanes_spreads_across_lanes_and_frees_greedily():
    """Three tasks take one lane each; the fourth goes to whichever lane frees EARLIEST - which is
    what the worker does when a claude session frees, not a round-robin."""
    out = sm.fill_lanes([_t("a", 60), _t("b", 30), _t("c", 60), _t("d", 15)])
    assert [[r["id"] for r in lane] for lane in out["lanes"]] == [["a"], ["b", "d"], ["c"]]
    assert out["lane_ends"] == [60, 45, 60]
    assert out["dispatch_n"] == 4 and out["manual_n"] == 0


def test_fill_lanes_ties_take_the_lowest_lane():
    out = sm.fill_lanes([_t("a", 60), _t("b", 60), _t("c", 60), _t("d", 60)])
    assert out["lanes"][0][1]["id"] == "d"


def test_fill_lanes_manual_track_is_serial_and_separate():
    """the operator has one pair of hands: the manual track is a queue, never fanned across lanes."""
    out = sm.fill_lanes([_t("m1", 30, manual=True), _t("d1", 60), _t("m2", 45, manual=True)])
    assert [r["id"] for r in out["manual"]] == ["m1", "m2"]
    assert [(r["start_min"], r["end_min"]) for r in out["manual"]] == [(0, 30), (30, 75)]
    assert out["manual_n"] == 2 and out["dispatch_n"] == 1
    assert all(r["lane"] == "manual" for r in out["manual"])


def test_fill_lanes_counts_tasks_per_project():
    out = sm.fill_lanes([_t("a", 60, project="client-app"),
                         _t("b", 60, project="client-app"),
                         _t("c", 60, project="app-one"),
                         _t("d", 30, project="app-two", manual=True)])
    assert out["by_project"] == {"client-app": 2, "app-one": 1, "app-two": 1}


def test_fill_lanes_fill_pct_is_committed_over_four_track_capacity():
    """A 4h block offers 4 tracks of 240m = 960 track-minutes. Two 120m dispatch tasks land in
    two different lanes, so they commit 240 of it - 25%."""
    out = sm.fill_lanes([_t("a", 120), _t("b", 120)], capacity_min=240)
    assert out["capacity_min"] == 240 * 4
    assert out["committed_min"] == 240
    assert out["fill_pct"] == 25


def test_fill_lanes_marks_overflow_but_keeps_the_task_put():
    """Overrun STAYS PUT (nothing auto-carries): an overflowing task is still in its lane, only
    flagged - the next block is never silently repacked."""
    out = sm.fill_lanes([_t("a", 120), _t("b", 120), _t("c", 120), _t("d", 60)], capacity_min=120)
    assert out["overflow"] == ["d"]                     # d starts at 120 == the block's end
    assert out["lanes"][0][-1]["id"] == "d"             # still in a lane, not dropped
    assert out["lanes"][0][-1]["overflow"] is True
    assert out["lanes"][0][0]["overflow"] is False


def test_fill_lanes_committed_is_clipped_at_capacity():
    """Work running past the block's end isn't capacity the block has to give, so the bar clips."""
    out = sm.fill_lanes([_t("a", 600)], capacity_min=120)
    assert out["committed_min"] == 120
    assert out["fill_pct"] == 25                        # 120 of 4 x 120


def test_fill_lanes_flags_a_task_that_STARTS_inside_but_ENDS_late():
    """OVERRUN is a different, commoner condition than overflow, and used to be flagged nowhere:
    the task begins inside the block and simply finishes after it. Its forecast bar was clipped at
    the track's right edge, so the ordinary overload case rendered as a perfect fit."""
    out = sm.fill_lanes([_t("a", 180)], capacity_min=120)
    row = out["lanes"][0][0]
    assert row["overflow"] is False                     # it DID start - not an overflow
    assert row["runs_over_min"] == 60                   # ...and ran 60m past the end
    assert out["over_min"] == 60


def test_fill_lanes_does_not_double_flag_an_overflowing_task_as_an_overrun():
    """A task that cannot START carries `overflow`; `runs_over_min` stays 0 so the two conditions
    never render on the same bar."""
    out = sm.fill_lanes([_t("a", 120), _t("b", 120), _t("c", 120), _t("d", 60)], capacity_min=120)
    d = out["lanes"][0][-1]
    assert d["overflow"] is True and d["runs_over_min"] == 0


def test_over_min_reports_the_WORST_track_and_includes_the_manual_one():
    """`over_min` is what the block reports as "N past the block", so it must be the track that
    overruns most - The operator's own serial track included."""
    out = sm.fill_lanes([_t("a", 180), _t("b", 300, manual=True)], capacity_min=120)
    assert out["over_min"] == 180                       # manual runs 300-120 past; the lane only 60


def test_over_min_is_zero_when_everything_fits():
    out = sm.fill_lanes([_t("a", 60), _t("b", 60)], capacity_min=120)
    assert out["over_min"] == 0
    assert all(r["runs_over_min"] == 0 for lane in out["lanes"] for r in lane)


def test_no_capacity_means_nothing_can_overrun():
    """An unbounded block has no end to run past - neither flag may fire."""
    out = sm.fill_lanes([_t("a", 600)])
    assert out["over_min"] == 0
    assert out["lanes"][0][0]["runs_over_min"] == 0


def test_fill_lanes_tolerates_junk_entries_and_missing_estimates():
    out = sm.fill_lanes(["nope", {}, {"id": "a"}, {"id": "b", "estimate_min": "x"}, _t("c", 30)])
    assert [r["id"] for lane in out["lanes"] for r in lane] == ["a", "b", "c"]
    assert sorted(out["lane_ends"]) == [0, 0, 30]


def test_fill_lanes_empty_set():
    out = sm.fill_lanes([], capacity_min=240)
    assert out["fill_pct"] == 0 and out["dispatch_n"] == 0 and out["overflow"] == []


# -- block_sequence + order_violations: the order guard ----------------------------------------
def _seq_fixture():
    return _sched(work_blocks=[_blk("b1", "2026-08-24", "07:30", "11:30"),
                               _blk("b2", "2026-08-24", "13:00", "17:00"),
                               _blk("b3", "2026-08-25", "09:00", "12:00")])


def test_block_sequence_orders_by_block_chronology_then_position():
    assigned = {
        "p::late": {"block": "b3", "block_pos": 0},
        "p::second": {"block": "b1", "block_pos": 1},
        "p::first": {"block": "b1", "block_pos": 0},
        "p::mid": {"block": "b2", "block_pos": 0},
    }
    assert [r["key"] for r in sm.block_sequence(_seq_fixture(), assigned)] == [
        "p::first", "p::second", "p::mid", "p::late"]


def test_block_sequence_skips_tasks_pointing_at_a_missing_block():
    assigned = {"p::a": {"block": "b1", "block_pos": 0},
                "p::ghost": {"block": "gone", "block_pos": 0}}
    assert [r["key"] for r in sm.block_sequence(_seq_fixture(), assigned)] == ["p::a"]


def test_order_guard_passes_when_the_dependency_comes_first():
    assigned = {"p::dep": {"block": "b1", "block_pos": 0}, "p::t": {"block": "b1", "block_pos": 1}}
    tasks = {"p::dep": {"id": "dep", "title": "Dep", "state": "todo"},
             "p::t": {"id": "t", "title": "T", "state": "todo", "depends_on": ["dep"]}}
    assert sm.order_violations(sm.block_sequence(_seq_fixture(), assigned), tasks) == []


def test_order_guard_fires_when_a_task_sits_ahead_of_its_dependency():
    assigned = {"p::t": {"block": "b1", "block_pos": 0}, "p::dep": {"block": "b1", "block_pos": 1}}
    tasks = {"p::dep": {"id": "dep", "title": "Dep", "state": "todo"},
             "p::t": {"id": "t", "title": "T", "state": "todo", "depends_on": ["dep"]}}
    v = sm.order_violations(sm.block_sequence(_seq_fixture(), assigned), tasks)
    assert len(v) == 1 and v[0]["key"] == "p::t" and v[0]["dep_key"] == "p::dep"
    assert "sits ahead of Dep" in v[0]["reason"]


def test_order_guard_fires_across_blocks_on_different_days():
    assigned = {"p::t": {"block": "b1", "block_pos": 0}, "p::dep": {"block": "b3", "block_pos": 0}}
    tasks = {"p::dep": {"id": "dep", "title": "Dep", "state": "todo"},
             "p::t": {"id": "t", "title": "T", "state": "todo", "depends_on": ["dep"]}}
    assert len(sm.order_violations(sm.block_sequence(_seq_fixture(), assigned), tasks)) == 1


def test_order_guard_is_satisfied_by_a_done_dependency_anywhere():
    """A finished dependency constrains nothing - it may sit later in the sequence, or in no block
    at all, because the work it gates is already on disk."""
    assigned = {"p::t": {"block": "b1", "block_pos": 0}, "p::dep": {"block": "b3", "block_pos": 0}}
    tasks = {"p::dep": {"id": "dep", "title": "Dep", "state": "done"},
             "p::t": {"id": "t", "title": "T", "state": "todo", "depends_on": ["dep"]}}
    s = _seq_fixture()
    assert sm.order_violations(sm.block_sequence(s, assigned), tasks) == []
    assert sm.order_violations(sm.block_sequence(s, {"p::t": assigned["p::t"]}), tasks) == []


def test_order_guard_fires_when_the_dependency_is_in_no_block():
    assigned = {"p::t": {"block": "b1", "block_pos": 0}}
    tasks = {"p::dep": {"id": "dep", "title": "Dep", "state": "todo"},
             "p::t": {"id": "t", "title": "T", "state": "todo", "depends_on": ["dep"]}}
    v = sm.order_violations(sm.block_sequence(_seq_fixture(), assigned), tasks)
    assert len(v) == 1 and "in no block" in v[0]["reason"]


def test_order_guard_reports_an_unknown_dependency_id():
    assigned = {"p::t": {"block": "b1", "block_pos": 0}}
    tasks = {"p::t": {"id": "t", "title": "T", "state": "todo", "depends_on": ["ghost"]}}
    v = sm.order_violations(sm.block_sequence(_seq_fixture(), assigned), tasks)
    assert len(v) == 1 and "not in the plan" in v[0]["reason"]


def test_order_guard_resolves_a_cross_project_dependency_key():
    assigned = {"a::t": {"block": "b1", "block_pos": 0}, "b::dep": {"block": "b2", "block_pos": 0}}
    tasks = {"b::dep": {"id": "dep", "title": "Dep", "state": "todo"},
             "a::t": {"id": "t", "title": "T", "state": "todo", "depends_on": ["b::dep"]}}
    v = sm.order_violations(sm.block_sequence(_seq_fixture(), assigned), tasks)
    assert len(v) == 1 and v[0]["dep_key"] == "b::dep"


def test_order_guard_reports_every_violated_dependency_of_one_task():
    assigned = {"p::t": {"block": "b1", "block_pos": 0},
                "p::d1": {"block": "b1", "block_pos": 1},
                "p::d2": {"block": "b2", "block_pos": 0}}
    tasks = {"p::d1": {"id": "d1", "title": "D1", "state": "todo"},
             "p::d2": {"id": "d2", "title": "D2", "state": "todo"},
             "p::t": {"id": "t", "title": "T", "state": "todo", "depends_on": ["d1", "d2"]}}
    assert len(sm.order_violations(sm.block_sequence(_seq_fixture(), assigned), tasks)) == 2


def test_order_guard_ignores_blank_and_missing_depends_on():
    assigned = {"p::t": {"block": "b1", "block_pos": 0}}
    seq = sm.block_sequence(_seq_fixture(), assigned)
    assert sm.order_violations(seq, {"p::t": {"id": "t", "depends_on": ["", "  ", None]}}) == []
    assert sm.order_violations(seq, {"p::t": {"id": "t"}}) == []
