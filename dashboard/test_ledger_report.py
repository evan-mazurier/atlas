"""The ledger reader: a monitor capability, and its two template properties.

`docs/ledger.md` §7b, and §6 rules 1 and 3 which it enforces.

The two rules are asserted as PROPERTIES of the module, in the same way `ledger._efficiency`
already refuses to build a token block without the quality figures. A report that can only
sometimes be trusted is a report nobody can act on.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger                                          # noqa: E402
import ledger_report as lr                             # noqa: E402


# ── rule 1: no efficiency number without the quality numbers beside it ───────────────────────────

def test_a_token_figure_without_quality_beside_it_is_refused():
    """'Tokens-per-task can always be driven down by doing worse work: a cheaper model, lower
    effort, a shallower reviewer.' A change that cuts tokens 30% and raises rework 50% has COST
    tokens, not saved them."""
    with pytest.raises(lr.UnpairedEfficiency):
        lr._quality_line({"fresh": 120_000})


def test_a_partial_quality_block_is_refused_too():
    with pytest.raises(lr.UnpairedEfficiency):
        lr._quality_line({"beside": {"rework_rate": 12, "reverts": 0}})     # no rejection rate


def test_the_quality_line_names_every_denominator():
    """'A rate with no denominator beside it is how a run of four jobs becomes 25%.'"""
    line = lr._quality_line({
        "beside": {"rework_rate": 0.125, "reviewer_rejection_rate": 0.30, "reverts": 1},
        "beside_samples": {"rework": 40, "reviewer": 33, "reverts": 28}})
    for n in ("40", "33", "28"):
        assert n in line


def test_a_rate_is_a_fraction_and_is_printed_as_a_percentage():
    """`ledger._rate` returns 0-1; the panel formats it with its own `pct()`. Printing it beside a
    `%` sign was wrong by a factor of a hundred — a 14% rework rate read as '0.1429%', which makes
    a bad week look flawless in a report the operator acts on."""
    line = lr._quality_line({
        "beside": {"rework_rate": 0.1429, "reviewer_rejection_rate": 0.3333, "reverts": 0},
        "beside_samples": {"rework": 7, "reviewer": 9, "reverts": 6}})
    assert "rework 14.3%" in line and "reviewer rejected 33.3%" in line


def test_no_denominator_reads_as_not_applicable_never_as_zero():
    line = lr._quality_line({
        "beside": {"rework_rate": None, "reviewer_rejection_rate": None, "reverts": 0},
        "beside_samples": {"rework": 0, "reviewer": 0, "reverts": 0}})
    assert "rework n/a" in line and "0%" not in line


def test_the_summary_cannot_be_built_without_the_pairing():
    """The refusal is not local to one helper: nothing downstream can assemble a report that
    quietly omits it."""
    with pytest.raises(lr.UnpairedEfficiency):
        lr.summary({"efficiency": {"fresh": 1}, "quality": {}})


# ── rule 3: no claimed improvement without the split checked (the mix trap) ──────────────────────

def _seg(value, n):
    return {"value": value, "n": n}


def test_the_mix_trap_is_caught():
    """§6's own example: tokens per task fall 180k → 140k and it looks like a 22% improvement,
    until the split shows one bucket went the other way. Nothing got more efficient; the MIX
    changed."""
    before = {"small": _seg(100_000, 12), "large": _seg(300_000, 11)}
    after = {"small": _seg(70_000, 15), "large": _seg(340_000, 13)}
    v = lr.check_split(before, after)
    assert v["claimable"] is False
    assert "small moved down" in v["why"] and "large moved up" in v["why"]


def test_a_change_that_holds_in_every_segment_is_claimable():
    before = {"small": _seg(100_000, 12), "large": _seg(300_000, 11)}
    after = {"small": _seg(70_000, 15), "large": _seg(210_000, 13)}
    v = lr.check_split(before, after)
    assert v["claimable"] is True and v["direction"] == "down"


def test_disagreement_names_both_sides_not_whichever_came_first():
    """Neither direction is privileged: this function does not know which way is better, and an
    earlier version took the first segment's direction as the truth — naming the outlier by
    alphabetical accident, i.e. reporting the wrong half of a real finding."""
    a = lr.check_split({"x": _seg(100, 12), "z": _seg(100, 12)},
                       {"x": _seg(50, 12), "z": _seg(200, 12)})
    b = lr.check_split({"z": _seg(100, 12), "x": _seg(100, 12)},
                       {"z": _seg(200, 12), "x": _seg(50, 12)})
    assert a["why"] == b["why"]
    assert "x moved down" in a["why"] and "z moved up" in a["why"]


def test_a_failed_transfer_is_exactly_the_table_the_design_draws():
    """Small waste 22% → 14%, large 31% → 30%. §6: 'That is a failed transfer, visible only
    because of the split.' The large bucket barely moved, so the claim does not transfer — and a
    combined number would have hidden it."""
    v = lr.check_split({"small": _seg(22, 12), "large": _seg(31, 12)},
                       {"small": _seg(14, 12), "large": _seg(30, 12)})
    assert v["claimable"] is True and v["direction"] == "down"
    large = next(s for s in v["segments"] if s["segment"] == "large")
    assert abs(large["move_pct"]) < lr.MIN_MOVE_PCT, "the large bucket did not really move"


def test_too_few_jobs_is_not_a_finding():
    """'Needs roughly ten jobs per bucket to believe — about a week at current volume.'"""
    v = lr.check_split({"small": _seg(100, 3)}, {"small": _seg(50, 4)})
    assert v["claimable"] is False and "noise" in v["why"]


def test_a_thin_segment_beside_a_solid_one_is_named_not_hidden():
    v = lr.check_split({"small": _seg(100, 12), "large": _seg(300, 2)},
                       {"small": _seg(70, 14), "large": _seg(100, 2)})
    assert v["claimable"] is True and "large" in v["why"]


def test_no_baseline_is_not_an_infinite_improvement():
    """Zero is not a baseline: everything is an infinite improvement over nothing."""
    assert lr._pct(0, 50) is None
    assert lr._pct(None, 50) is None
    v = lr.check_split({"small": _seg(0, 12)}, {"small": _seg(40, 12)})
    assert v["claimable"] is False


# ── the numbers themselves ───────────────────────────────────────────────────────────────────────

_STATS = {
    "jobs": 40, "events": 900,
    "escalation_split": [
        {"key": "evan", "points": [{"week": "2026-W34", "v": 6}, {"week": "2026-W35", "v": 2}]},
        {"key": "monitor", "points": [{"week": "2026-W35", "v": 9}]},
    ],
    "wall_clock": {"total_hours": {"working": 12.0, "blocked": 40.5, "queued": 3.0},
                   "blocked_before_ledger_h": 95.8},
    "quality": {"rework_rate": 0.125, "reviewer_rejection_rate": 0.30, "reverts": 1,
                "rework_sample": 40, "reviewer_sample": 33, "revert_sample": 28},
    "efficiency": {"fresh": 1_200_000,
                   "beside": {"rework_rate": 0.125, "reviewer_rejection_rate": 0.30, "reverts": 1},
                   "beside_samples": {"rework": 40, "reviewer": 33, "reverts": 28}},
    "reverts_by_gate": {"approved": {"reverts": 1, "of": 20}, "ungated": {"reverts": 0, "of": 8}},
    "rework_distribution": {"0": 30, "1": 7, "2": 2, "3+": 1},
}


def test_the_headline_arc_is_computed_from_the_events():
    s = lr.summary(_STATS)
    assert s["answered_by_monitor"] == 9 and s["answered_by_evan"] == 8
    assert s["blocked_hours"] == 40.5


def test_pre_ledger_waiting_is_reported_separately_never_folded_in():
    """§8 / `ledger._wall_clock`: on the far side of the switch line, WAITING is well recorded and
    MACHINE TIME is structurally invisible. Adding them would produce the most quotable wrong
    number on the panel."""
    text = lr.text_report(_STATS)
    assert "40.5" in text and "95.8" in text
    assert "136.3" not in text


def test_the_text_report_carries_the_quality_line():
    assert "rework 12.5% of 40" in lr.text_report(_STATS)


def test_the_report_reads_the_real_ledger_without_raising():
    """The live file, whatever is in it. A reader that only works on a fixture is not a reader."""
    assert isinstance(lr.text_report(ledger.stats()), str)
