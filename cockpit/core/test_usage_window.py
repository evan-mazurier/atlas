"""The five-hour window is a chain of windows, not a trailing sum (rework-findings #11).

A morning of work, then a held afternoon dispatch: the trailing sum still carried the whole
morning after the real window had reset. These pin the model that replaced it.
"""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import usage  # noqa: E402

T0 = dt.datetime(2026, 9, 15, 9, 14)          # the first message of the day in the finding
H = dt.timedelta(hours=1)


def at(h: float) -> dt.datetime:
    return T0 + h * H


# ─── the pure chain ────────────────────────────────────────────────────────────────────────────

def test_a_window_opens_at_the_first_message_and_the_next_at_the_first_message_after_expiry():
    times = [at(0), at(1), at(4.9), at(5.1), at(6)]
    assert usage.window_chain(times, 5) == [at(0), at(5.1)]


def test_a_message_exactly_at_expiry_opens_the_next_window():
    # (the expired first window is dropped by the gap search — same current window either way)
    assert usage.window_chain([at(0), at(5)], 5)[-1] == at(5)
    assert usage.window_chain([at(0), at(4.99)], 5) == [at(0)]


def test_the_chain_re_anchors_after_a_gap_of_a_full_window():
    # yesterday evening, a night, this morning: the morning's first message is the anchor whatever
    # the evening looked like
    times = [at(-14), at(-13.5), at(-12), at(0), at(2)]
    assert usage.window_chain(times, 5) == [at(0)]


def test_an_anchor_overrides_the_gap_search():
    times = [at(0), at(1), at(3)]
    assert usage.window_chain(times, 5, anchor=at(0.5)) == [at(0.5)]


def test_an_empty_chain():
    assert usage.window_chain([], 5) == []


# ─── over transcripts ──────────────────────────────────────────────────────────────────────────

def _stamp(t: dt.datetime) -> str:
    """Transcripts stamp UTC with a Z; `t` is local wall time, as the tests think."""
    return t.astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _transcripts(root: Path, records: list[tuple[dt.datetime, int]], name="s.jsonl"):
    d = root / "D--proj"
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for t, n in records:
        lines.append(json.dumps({"timestamp": _stamp(t),
                                 "message": {"usage": {"input_tokens": n, "output_tokens": 0}}}))
        lines.append(json.dumps({"type": "user", "timestamp": _stamp(t)}))   # no usage: ignored
    (d / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("ATLAS_CLAUDE_DIR", str(root))
    monkeypatch.setattr(usage, "WINDOW_PIN", tmp_path / "_window.json")
    monkeypatch.setattr(usage, "_WINDOW_CACHE", {"at": None, "hours": None, "value": None})
    return root


def test_the_finding_itself_the_afternoon_reads_a_fresh_window(transcripts):
    """09:14 first message, a morning of work, the window reset at 14:14; at 14:20 the trailing sum
    still carried the morning — the chain carries only what came after 14:14."""
    _transcripts(transcripts, [(at(0), 100_000), (at(2), 400_000), (at(4.5), 300_000),
                               (at(5.05), 20_000)])           # 14:17 — the first message of window 2
    w = usage.window_spend(5, now=at(5.1))                    # 14:20
    assert w["ok"] and w["anchored"] and not w["pinned"]
    assert w["fresh"] == 20_000
    assert w["window_start"] == at(5.05).isoformat(timespec="seconds")
    assert w["oldest"] == w["window_start"]                   # what estimate_reset adds 5h to
    assert w["window_end"] == at(10.05).isoformat(timespec="seconds")


def test_inside_the_first_window_the_spend_is_the_whole_window(transcripts):
    _transcripts(transcripts, [(at(0), 100_000), (at(2), 400_000), (at(4.5), 300_000)])
    w = usage.window_spend(5, now=at(4.75))
    assert w["fresh"] == 800_000 and w["entries"] == 3
    assert w["window_end"] == at(5).isoformat(timespec="seconds")


def test_past_the_last_expiry_with_nothing_sent_the_budget_is_full(transcripts):
    _transcripts(transcripts, [(at(0), 100_000), (at(2), 400_000)])
    w = usage.window_spend(5, now=at(5.5))
    assert w["ok"] and w["fresh"] == 0 and w["entries"] == 0
    assert w["window_start"] is None and w["oldest"] is None and w["window_end"] is None


def test_last_night_does_not_count_against_this_morning(transcripts):
    _transcripts(transcripts, [(at(-14), 900_000), (at(-12), 200_000), (at(0), 10_000)])
    w = usage.window_spend(5, now=at(0.5))
    assert w["fresh"] == 10_000 and w["anchored"]


def test_a_lookback_with_no_full_gap_is_reported_as_unanchored(transcripts):
    # messages every four hours for two days: no window boundary can be trusted
    _transcripts(transcripts, [(at(-32 + 4 * k), 1_000) for k in range(9)])
    w = usage.window_spend(5, now=at(0.5))
    assert w["ok"] and not w["anchored"]


def test_a_pinned_window_start_wins_and_expires(transcripts):
    _transcripts(transcripts, [(at(0), 100_000), (at(2), 400_000), (at(4.5), 300_000)])
    usage.WINDOW_PIN.write_text(json.dumps({"window_start": at(3).isoformat()}), encoding="utf-8")
    w = usage.window_spend(5, now=at(4.75))
    assert w["pinned"] and w["fresh"] == 300_000            # only what came after the pin
    assert w["window_start"] == at(3).isoformat(timespec="seconds")
    usage._WINDOW_CACHE.update({"at": None, "value": None})
    w = usage.window_spend(5, now=at(8.5))                   # the pin is more than a window old
    assert not w["pinned"] and w["fresh"] == 0               # ...and the model says the window is closed


def test_a_garbage_pin_is_ignored(transcripts):
    _transcripts(transcripts, [(at(0), 100_000)])
    usage.WINDOW_PIN.write_text("not json", encoding="utf-8")
    w = usage.window_spend(5, now=at(1))
    assert w["ok"] and not w["pinned"] and w["fresh"] == 100_000


def test_the_result_is_cached_for_thirty_seconds(transcripts):
    _transcripts(transcripts, [(at(0), 100_000)])
    assert usage.window_spend(5, now=at(1))["fresh"] == 100_000
    _transcripts(transcripts, [(at(0), 100_000), (at(1), 50_000)])
    assert usage.window_spend(5, now=at(1) + dt.timedelta(seconds=10))["fresh"] == 100_000
    assert usage.window_spend(5, now=at(1) + dt.timedelta(seconds=40))["fresh"] == 150_000


def test_no_transcript_dir_is_not_ok_but_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_CLAUDE_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(usage, "_WINDOW_CACHE", {"at": None, "hours": None, "value": None})
    w = usage.window_spend(5, now=at(1))
    assert not w["ok"] and w["fresh"] == 0 and w["oldest"] is None


# ─── price-weighted fresh tokens ───────────────────────────────────────────────────────────────

def test_the_window_weighs_fresh_tokens_by_model_price(transcripts):
    d = transcripts / "D--proj"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"timestamp": _stamp(at(0)), "message": {
            "model": "claude-haiku-5-5", "usage": {"input_tokens": 60_000, "output_tokens": 40_000}}}),
        json.dumps({"timestamp": _stamp(at(1)), "message": {
            "model": "claude-sonnet-5-5", "usage": {"input_tokens": 9_000, "output_tokens": 1_000}}}),
    ]
    (d / "w.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    w = usage.window_spend(5, now=at(2))
    assert w["fresh"] == 5_000 + 10_000          # 100k Haiku at 0.05 + 10k Sonnet at 1.0
