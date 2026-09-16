"""The ledger records what Atlas had been throwing away.

`docs/ledger.md`. The acceptance criteria this file pins, in the contract's own words:

  * a job run end to end produces a complete event trail; every statistic in §4 is computable
    from the file alone, with nothing derived stored;
  * **the public exporter has no code path that can emit a string from the ledger. Not a filter —
    a property. A test asserts it.**

This snapshot carries the ledger-only half of the suite. The other half drives the worker's
hooks (`save()` emitting state changes, per-phase token figures, revert detection) and belongs
with the worker that has them.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger                                       # noqa: E402


@pytest.fixture()
def led(tmp_path, monkeypatch):
    """Point the ledger at a throwaway file. NEVER let a test write the real one."""
    d = tmp_path / "ledger"
    monkeypatch.setattr(ledger, "LEDGER_DIR", d)
    monkeypatch.setattr(ledger, "EVENTS", d / "events.jsonl")
    return d / "events.jsonl"


# ── the file itself ───────────────────────────────────────────────────────────────────────────

def test_an_event_is_one_line_carrying_job_project_and_lane(led):
    ledger.emit("test", {"id": "j1", "project": "p", "lane_id": "a"}, result="pass")
    rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["ev"] == "test" and rows[0]["job"] == "j1"
    assert rows[0]["project"] == "p" and rows[0]["lane"] == "a"
    assert rows[0]["t"]


def test_a_ledger_write_never_raises(monkeypatch, capsys):
    """A ledger that can fail a job is worse than no ledger."""
    monkeypatch.setattr(ledger, "LEDGER_DIR", Path("\0:/nope"))
    monkeypatch.setattr(ledger, "EVENTS", Path("\0:/nope/events.jsonl"))
    ledger.emit("test", {"id": "j1"})                      # must not raise
    assert "ledger write skipped" in capsys.readouterr().out


def test_a_torn_line_does_not_cost_the_rest(led):
    ledger.emit("a", {"id": "j"})
    led.open("a", encoding="utf-8").write('{"ev": "b", "trunc\n')
    ledger.emit("c", {"id": "j"})
    assert [e["ev"] for e in ledger.read_events()] == ["a", "c"]


# ── the public export — numbers only, as a property ───────────────────────────────────────────

POISON = "CLIENT-PRODUCTION-DATABASE-SECRET"


def test_the_exporter_has_no_code_path_that_can_emit_a_string(led):
    """Every field that could carry client material, poisoned. One surviving escalation named a
    client's production database; that must not be publishable by accident."""
    for ev, extra in (
        ("started", {"task": POISON, "type": POISON, "model": POISON, "effort": POISON}),
        ("escalation", {"q": POISON, "trigger": POISON}),
        ("escalation_answered", {"by": "evan", "answer": POISON, "waited_s": 12, "q": POISON}),
        ("review", {"verdict": "needs-changes", "notes": POISON}),
        ("review", {"verdict": "commit-ready", "notes": POISON}),
        ("merged", {"commit": POISON, "diffstat": POISON}),
        ("state", {"to": "merged", "was": POISON}),
        ("revert", {"reason": POISON, "deliberate": True}),
        ("phase_tokens", {"phase": POISON, "fresh": 10, "cache_read": 5, "cache_write": 1}),
    ):
        ledger.emit(ev, {"id": "j1", "project": POISON, "lane_id": POISON}, **extra)

    out = ledger.public_export()

    blob = json.dumps(out)
    assert POISON not in blob
    assert "j1" not in blob, "not even a job id"
    ledger._numeric_only(out)                      # every leaf is a number, re-checked
    assert out["escalations"] == 1 and out["reverts"] == 1
    assert out["reviews_by_verdict"]["needs_changes"] == 1
    assert list(out["projects"]) == ["project_1"], "projects are aliased by ordinal"


def test_the_property_check_actually_catches_a_leak():
    """Guards the guard — a numeric-only assertion that never fails proves nothing."""
    with pytest.raises(ledger.ExportLeak):
        ledger._numeric_only({"a": {"b": "text"}})
    with pytest.raises(ledger.ExportLeak):
        ledger._numeric_only({"a": [1, 2, "text"]})


def test_the_export_still_carries_the_numbers_worth_publishing(led):
    ledger.emit("escalation_answered", {"id": "j1", "project": "p"}, by="monitor", waited_s=30)
    ledger.emit("escalation_answered", {"id": "j2", "project": "p"}, by="evan", waited_s=1064)
    out = ledger.public_export()
    assert out["escalations_answered_by_monitor"] == 1
    assert out["escalations_answered_by_human"] == 1
    assert out["escalation_wait_seconds_total"] == 1094


# ── backfill — the partial period, marked as such ─────────────────────────────────────────────

def _fake_log(tmp_path, utf16=True):
    text = (
        "[2026-07-01T19:17:23] coding job_20260701_191723_3c80  (orchard-site: do a thing...)\n"
        "[2026-07-01T19:26:55] job_20260701_191723_3c80 -> needs_input\n"
        "[2026-07-01T20:02:11] job_20260701_191723_3c80 -> ready\n"
        "[2026-07-01T20:14:00] merging job_20260701_191723_3c80 (orchard-site: atlas/job_x)\n"
        "[2026-07-01T20:14:09] job_20260701_191723_3c80 -> merged\n"
    )
    p = tmp_path / "worker.log"
    p.write_bytes(text.encode("utf-16" if utf16 else "utf-8"))
    return p


@pytest.mark.parametrize("utf16", [True, False])
def test_backfill_reads_the_supervisors_utf16_log(led, tmp_path, utf16):
    """PowerShell redirects as UTF-16. Guessing utf-8 yields 1,500 lines of mojibake that parse to
    nothing and look exactly like an empty log."""
    res = ledger.backfill(worker_log=_fake_log(tmp_path, utf16), jobs_dir=tmp_path / "none")
    assert res["ok"] and res["jobs"] == 1
    states = [e["to"] for e in ledger.read_events() if e["ev"] == "state"]
    assert states == ["needs_input", "ready", "merged"]
    assert all(e.get("project") == "orchard-site"
               for e in ledger.read_events() if e.get("job"))


def test_backfill_marks_the_period_partial_rather_than_implying_zero(led, tmp_path):
    """A chart implying "no escalations in July" because none were recorded would be worse than no
    chart. The switch line is an event; the lost text is stated, not blanked."""
    ledger.backfill(worker_log=_fake_log(tmp_path), jobs_dir=tmp_path / "none")
    evs = ledger.read_events()
    assert evs[-1]["ev"] == "ledger_start" and "BACKFILLED" in evs[-1]["note"]
    esc = [e for e in evs if e["ev"] == "escalation"]
    assert esc and esc[0]["text_lost"] is True
    assert "q" not in esc[0], "an unknown question must not read as an empty one"
    assert all(e.get("src") == "backfill" for e in evs if e["ev"] != "ledger_start")


def test_backfill_takes_verdicts_and_tokens_from_surviving_records(led, tmp_path):
    jdir = tmp_path / "jobs"
    jdir.mkdir()
    (jdir / "job_20260825_143122_c496.json").write_text(json.dumps({
        "id": "job_20260825_143122_c496", "project": "orchard-api", "created": "2026-08-25T14:31:22",
        "finished": "2026-08-25T15:02:00", "review_verdict": "commit-ready",
        "tokens": 41200, "cache_tokens": 3_100_000, "merged_at": "2026-08-25T15:05:00",
        "commit": "abc1234"}), encoding="utf-8")
    ledger.backfill(worker_log=tmp_path / "absent.log", jobs_dir=jdir)
    evs = {e["ev"] for e in ledger.read_events()}
    assert {"started", "review", "phase_tokens", "merged", "ledger_start"} <= evs


# ── the ledger is not written by a test run ───────────────────────────────────────────────────

def test_a_test_that_redirects_the_path_still_writes(led):
    """The guard must be narrow enough that the ledger's own coverage keeps working."""
    ledger.emit("state", {"id": "j1"}, to="merged")
    assert len(ledger.read_events()) == 1
