"""The statistics behind the ledger panel (docs/ledger.md §4, §7, §8).

The panel is presentation; these are the numbers. Three properties are pinned, because each is a
way a chart can lie while looking fine:

  * **a series that did not exist before the ledger begins AT the ledger.** Backfill knows state
    transitions and escalation counts and nothing else. Drawing verdicts or tokens as zero for July
    would assert "nothing was rejected in July" — a claim the data does not make.
  * **an efficiency figure cannot be built alone.** `_efficiency` raises without the quality block.
    This is the guard against optimising tokens into worse output and reporting it as a win.
  * **nothing is stored.** Every figure is derived from the events on read, so a corrected formula
    corrects the whole history rather than only the future.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger                                        # noqa: E402


def ev(t, e, **kw):
    return {"t": t, "ev": e, **kw}


# A fortnight either side of the switch: July is backfill (states + escalation counts only),
# August is the real thing.
EVENTS = [
    ev("2026-07-06T09:00:00", "state", job="j1", project="alpha", to="running", src="backfill"),
    ev("2026-07-06T09:30:00", "state", job="j1", project="alpha", to="needs_input", src="backfill"),
    ev("2026-07-06T09:30:00", "escalation", job="j1", project="alpha", src="backfill",
       text_lost=True),
    ev("2026-07-06T11:30:00", "state", job="j1", project="alpha", to="merged", src="backfill"),
    ev("2026-07-13T09:00:00", "escalation", job="j2", project="alpha", src="backfill",
       text_lost=True),
    ev("2026-08-24T18:00:00", "ledger_start", note="partial above"),
    # --- the live period ---
    ev("2026-08-25T09:00:00", "started", job="j3", project="beta", task="Do the thing"),
    ev("2026-08-25T09:00:00", "state", job="j3", project="beta", to="running"),
    ev("2026-08-25T09:10:00", "phase_tokens", job="j3", project="beta", phase="coding",
       fresh=1000, out=200, cache_read=8000, cache_write=500),
    ev("2026-08-25T09:20:00", "state", job="j3", project="beta", to="needs_input"),
    ev("2026-08-25T09:20:00", "escalation", job="j3", project="beta", q="which one?"),
    ev("2026-08-25T09:25:00", "escalation_answered", job="j3", project="beta", by="evan",
       waited_s=300),
    ev("2026-08-25T09:30:00", "state", job="j3", project="beta", to="running"),
    ev("2026-08-25T10:00:00", "review", job="j3", project="beta", verdict="commit-ready",
       **{"pass": 1}),
    ev("2026-08-25T10:05:00", "gate", job="j3", project="beta", which="merge", action="approved",
       by="evan"),
    ev("2026-08-25T10:06:00", "merged", job="j3", project="beta", commit="abc123"),
    ev("2026-08-25T10:06:00", "state", job="j3", project="beta", to="merged"),
    ev("2026-08-25T10:06:30", "finished", job="j3", project="beta", status="merged", rework=0,
       tokens=1200),
    ev("2026-08-26T09:00:00", "started", job="j4", project="beta", task="Another"),
    ev("2026-08-26T09:00:00", "state", job="j4", project="beta", to="running"),
    ev("2026-08-26T09:30:00", "review", job="j4", project="beta", verdict="needs-changes",
       **{"pass": 1}),
    ev("2026-08-26T10:00:00", "review", job="j4", project="beta", verdict="commit-ready",
       **{"pass": 2}),
    ev("2026-08-26T10:05:00", "merged", job="j4", project="beta", commit="def456"),
    ev("2026-08-26T10:05:00", "state", job="j4", project="beta", to="merged"),
    ev("2026-08-26T10:06:00", "finished", job="j4", project="beta", status="merged", rework=1,
       tokens=4000),
    ev("2026-08-26T15:00:00", "revert", job="j4", project="beta", deliberate=True,
       reason="built the wrong thing"),
]


@pytest.fixture()
def s():
    return ledger.stats(EVENTS)


# ── the switch line (§8) ───────────────────────────────────────────────────────────────────────

def test_a_series_that_did_not_exist_before_the_ledger_starts_at_the_ledger(s):
    verdicts = {x["key"]: x for x in s["review_verdicts"]}
    for series in verdicts.values():
        assert series["from_week"] == s["ledger_start_week"]
        assert all(p["week"] >= s["ledger_start_week"] for p in series["points"]), \
            "no zero-valued July point — that would read as 'nothing was rejected in July'"


def test_escalation_counts_DO_extend_back_through_the_backfill(s):
    """The one series that honestly predates the ledger: before the monitor existed, every
    escalation stopped for the operator, so the count IS the intervention count."""
    esc = next(x for x in s["interventions"] if x["key"] == "escalations")
    assert esc["from_week"] is None
    july = [p for p in esc["points"] if p["week"] < s["ledger_start_week"]]
    assert sum(p["v"] for p in july) == 2


def test_the_week_axis_includes_the_empty_weeks(s):
    """A month of silence must draw as a month, not as a straight line between two points."""
    assert s["weeks"] == sorted(s["weeks"])
    assert len(s["weeks"]) >= 7, "July to late August is more than a few weeks"


# ── efficiency never travels alone (§7b) ───────────────────────────────────────────────────────

def test_an_efficiency_block_cannot_be_built_without_the_quality_figures():
    with pytest.raises(ValueError) as e:
        ledger._efficiency({"fresh_total": 10}, {"rework_rate": 0.1})
    assert "quality" in str(e.value)


def test_the_panel_payload_carries_the_quality_figures_beside_the_tokens(s):
    beside = s["efficiency"]["beside"]
    assert set(beside) == {"rework_rate", "reviewer_rejection_rate", "reverts"}
    # and their sample sizes, because a rate over two jobs is not a rate
    assert set(s["efficiency"]["beside_samples"]) == {"rework", "reviewer", "reverts"}


def test_fresh_and_cache_tokens_are_never_mixed(s):
    eff = s["efficiency"]
    assert eff["fresh_total"] == 1200, "fresh in + out, and nothing else"
    assert eff["cache_read_total"] == 8000
    assert 0.86 < eff["cache_read_ratio"] < 0.88


def test_backfilled_token_rows_do_not_enter_the_burn_timeline():
    """The backfill carries a single job-total row per old job with no phase and no real time.
    Letting it into the timeline would draw a month of flat, meaningless burn."""
    evs = EVENTS + [ev("2026-07-06T09:00:00", "phase_tokens", job="j1", project="alpha",
                       phase="job_total", fresh=99999, cache_read=0, src="backfill")]
    assert ledger.stats(evs)["efficiency"]["fresh_total"] == 1200


# ── the statistics themselves (§4) ─────────────────────────────────────────────────────────────

def test_first_pass_rejection_is_counted_on_the_first_pass_only(s):
    q = s["quality"]
    assert q["reviewer_sample"] == 2, "j3 and j4 — the second-pass review is not a new job"
    assert q["reviewer_rejection_rate"] == 0.5


def test_rework_distribution_counts_finished_jobs(s):
    assert s["rework_distribution"] == {"0": 1, "1": 1, "2": 0, "3+": 0}


def test_reverts_are_split_by_gate_as_counts_with_their_sample(s):
    """§5: the comparison is the point, and it is meaningless without the denominators."""
    rv = s["reverts_by_gate"]
    assert rv["approved"] == {"reverts": 0, "of": 1}, "j3 — the operator approved the merge"
    assert rv["ungated"] == {"reverts": 1, "of": 1}, "j4 — merged with no human gate, later undone"


def test_wall_clock_separates_time_blocked_on_the_operator(s):
    """The hours the machine spent waiting are invisible in every existing surface.

    LIVE events only. The backfill looks usable here — it carries timestamped transitions — but it
    only ever recorded needs_input/ready/merged/failed/stopped, never a start-of-work transition.
    So before the ledger, waiting is well recorded and machine time is structurally invisible, and
    one bar over both would report an artefact of the log as a finding about Atlas."""
    wc = s["wall_clock"]["total_hours"]
    assert wc["blocked"] == pytest.approx(0.2, abs=0.05), "j3's ten minutes, and nothing from July"
    week = {w["week"]: w for w in s["wall_clock"]["weeks"]}
    assert "2026-W28" not in week, "j1 is backfilled — it cannot enter the split"
    assert week["2026-W35"]["blocked"] == pytest.approx(0.2, abs=0.05)
    # not discarded, though: reported on its own, where it cannot pass as a share of a total
    assert s["wall_clock"]["blocked_before_ledger_h"] == pytest.approx(2.0, abs=0.05)


def test_an_abandoned_job_does_not_count_as_days_blocked():
    """A job that stopped for the operator and was never answered would otherwise report a week of
    'blocked', which is a fact about a dead record, not about waiting."""
    evs = [ev("2026-08-01T09:00:00", "state", job="jx", to="needs_input"),
           ev("2026-08-20T09:00:00", "state", job="jx", to="stopped")]
    assert ledger.stats(evs)["wall_clock"]["total_hours"]["blocked"] == 0


def test_escalations_are_split_by_who_answered_them(s):
    split = {x["key"]: sum(p["v"] for p in x["points"]) for x in s["escalation_split"]}
    assert split == {"evan": 1, "monitor": 0}


def test_the_recent_table_marks_backfilled_rows(s):
    rows = {r["job"]: r for r in s["recent"]}
    assert rows["j1"]["backfilled"] is True
    assert rows["j3"]["backfilled"] is False
    assert rows["j3"]["task"] == "Do the thing"
    assert rows["j3"]["verdict"] == "commit-ready"


def test_nothing_is_derived_from_a_stored_summary(s):
    """The storage rule (§2): stats() reads events and only events. If a future edit caches a
    figure into the file, this recomputation from the same list would disagree with it."""
    again = ledger.stats(EVENTS)
    assert again["quality"] == s["quality"]
    assert again["rework_distribution"] == s["rework_distribution"]


def test_an_empty_ledger_renders_rather_than_raising():
    out = ledger.stats([])
    assert out["events"] == 0 and out["weeks"] == []
    assert out["quality"]["rework_rate"] is None, "no rate over no jobs — not a zero"


def test_a_job_waiting_on_the_merge_gate_is_blocked_not_working():
    """`ready` means tests green, reviewer done, worker finished — the job is sitting on the operator's
    merge gate. Counting it as machine time understated the exact number the ledger exists to
    move: the ledger's first real day carried a 20-hour `ready` wait that read as 20h of work."""
    evs = [ev("2026-08-25T09:00:00", "state", job="jr", to="running"),
           ev("2026-08-25T10:00:00", "state", job="jr", to="ready"),
           ev("2026-08-25T14:00:00", "state", job="jr", to="merged")]
    wc = ledger.stats(evs)["wall_clock"]["total_hours"]
    assert wc["working"] == pytest.approx(1.0, abs=0.05)
    assert wc["blocked"] == pytest.approx(4.0, abs=0.05), "the four hours on the merge gate"


def test_every_week_carries_the_date_it_starts_on(s):
    """The axis is labelled by day, not by ISO week number — 'W31' is correct and unreadable. The
    server sends the Monday so the label and the data cannot disagree about where a week begins."""
    starts = s["week_starts"]
    assert starts["2026-W35"] == "2026-08-24"      # a Monday
    assert set(starts) == set(s["weeks"])


# ── a job finishes once, however many times its record was written ───────────────────────────────

DUPES = [
    ev("2026-08-26T15:00:00", "started", job="d1", project="p", estimate="1h"),
    ev("2026-08-26T16:00:00", "state", job="d1", project="p", to="merged"),
    ev("2026-08-26T16:00:00", "merged", job="d1", project="p", commit="abc"),
    ev("2026-08-26T16:00:01", "finished", job="d1", project="p", status="merged", rework=1,
       tokens=1000),
] + [e for i in range(400) for e in (                    # the record re-saved on every sweep
    ev(f"2026-08-26T16:{i // 60:02d}:{i % 60:02d}", "state", job="d1", project="p", to="merged"),
    ev(f"2026-08-26T16:{i // 60:02d}:{i % 60:02d}", "finished", job="d1", project="p",
       status="merged", rework=1, tokens=1000))]


def test_a_re_saved_record_counts_as_one_job_not_four_hundred():
    """Live on 2026-08-26: a merged job whose cleanup kept failing was retried by the sweep on
    every pass, and the ledger hook re-emitted `state` + `finished` each time — 401 of each. The
    source is fixed, but the file is APPEND-ONLY: those events are history and can never be edited
    out, so the readers must count what they mean. §2 promises exactly this — a formula found to
    be wrong is fixed once and corrects every past week with it."""
    s = ledger.stats(DUPES)
    assert s["rework_distribution"] == {"0": 0, "1": 1, "2": 0, "3+": 0}
    assert s["quality"]["rework_sample"] == 1
    assert s["quality"]["rework_rate"] == 1.0


def test_the_public_export_counts_jobs_not_records():
    out = ledger.public_export(DUPES)
    assert out["jobs_by_final_state"]["merged"] == 1
    assert out["jobs"] == 1
