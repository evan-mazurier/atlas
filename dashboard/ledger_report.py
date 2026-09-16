"""The ledger reader — a monitor CAPABILITY, not an agent (`docs/ledger.md` §7b).

The ledger will hold more data than is readable by eye, and the operator is the one engineering Atlas: they
need the data turned into decisions, not charts to interpret alone. By the monitor's rule — *an agent is
judgment, a tool is a capability* — that is something the monitor already owns the data for, and
adding a "ledger analyst" agent would recreate exactly the sprawl the monitoring layer removed.

It produces a **weekly report card**, plus on demand: what changed, what it cost, what improved,
what got worse, and **one** recommendation. Same card shape as everything else, same register.

**Two rules are properties of this module, not habits of whoever writes the report.**

1. *No efficiency number without the quality numbers beside it.* `ledger._efficiency` already
   refuses to build a token block without rework rate, reviewer rejection rate and revert rate —
   so this module reads the paired structure and `_lines` cannot render one half of it. Rework
   rate, rejection rate and revert rate accompany every claim about tokens. Without that pairing,
   tokens can always be driven down by doing worse work.
2. *No claimed improvement without the split checked* (§6 rule 3). A change in project or size
   MIX must never be reported as an efficiency gain. `check_split` is what decides whether a
   movement may be called an improvement, and a movement that fails it is reported as a movement.

`summary()` and `text_report()` are the checkable core and are what the tests cover. `report()` hands
the same numbers to the monitor's card composer for a written reading; the monitor is not part of
this snapshot, so `report()` is documented here and not runnable.
"""
from __future__ import annotations

# §6 rule 1: "needs roughly ten jobs per bucket to believe — about a week at current volume."
MIN_SAMPLE = 10
# Below this, a difference is noise. Stated once here rather than judged per report.
MIN_MOVE_PCT = 10.0


def _pct(a, b):
    """Percentage change from `a` to `b`, or None when there is no honest baseline. Zero is not a
    baseline: everything is an infinite improvement over nothing."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if a <= 0:
        return None
    return (b - a) / a * 100.0


def check_split(before: dict, after: dict, min_sample: int = MIN_SAMPLE) -> dict:
    """§6 rule 3 — the mix trap, as a function.

    `before`/`after` are `{segment: {"value": x, "n": k}}` — per size bucket, per session type, or
    both. The combined number can suggest a conclusion; only the split can confirm it, so:

    * every segment with enough samples must move the same way, and
    * a segment that moved the wrong way is named, because that is the finding.

    Tokens per implementation task falling from 180k to 140k looks like a 22% improvement until
    the split shows the week was fifteen cheap jobs and three expensive ones against five and ten.
    Nothing got more efficient; the mix changed. `{claimable, direction, segments, why}`.
    """
    segs, thin = [], []
    moved: dict[str, list[str]] = {"up": [], "down": []}
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key) or {}, after.get(key) or {}
        n = min(int(b.get("n") or 0), int(a.get("n") or 0))
        move = _pct(b.get("value"), a.get("value"))
        segs.append({"segment": key, "before": b.get("value"), "after": a.get("value"),
                     "n": n, "move_pct": move})
        if move is None or n < min_sample:
            thin.append(key)
        elif abs(move) >= MIN_MOVE_PCT:
            moved["down" if move < 0 else "up"].append(key)

    if not segs or len(thin) == len(segs):
        return {"claimable": False, "direction": None, "segments": segs,
                "why": "not enough jobs in any segment yet to tell a change from noise"}
    # DISAGREEMENT IS SYMMETRIC. There is no privileged direction here: this function does not
    # know which way is "better", and an earlier version that took the first segment's direction as
    # the truth named the outlier by alphabetical accident — reporting the wrong half of a real
    # finding. When both directions have segments, neither is the exception; the split simply does
    # not support a single claim, and both sides are named.
    if moved["up"] and moved["down"]:
        return {"claimable": False, "direction": None, "segments": segs,
                "why": f"the split disagrees — {', '.join(moved['down'])} moved down while "
                       f"{', '.join(moved['up'])} moved up, so this is a change in the MIX of "
                       f"work, not a change in efficiency"}
    direction = "down" if moved["down"] else ("up" if moved["up"] else None)
    if direction is None:
        return {"claimable": False, "direction": None, "segments": segs,
                "why": "no segment moved far enough to call it a change"}
    if thin:
        return {"claimable": True, "direction": direction, "segments": segs,
                "why": f"holds where there is enough data; {', '.join(thin)} is still too thin "
                       f"to say"}
    return {"claimable": True, "direction": direction, "segments": segs,
            "why": "every segment moved the same way"}


class UnpairedEfficiency(ValueError):
    """A token claim was assembled without the quality figures beside it (§7b)."""


def _quality_line(eff: dict) -> str:
    """The line that must sit beside every token figure. Raises rather than omitting: a report
    that quietly drops the pairing reads exactly like a report that never needed it."""
    beside = (eff or {}).get("beside") or {}
    need = ("rework_rate", "reviewer_rejection_rate", "reverts")
    if any(k not in beside for k in need):
        raise UnpairedEfficiency(
            "refusing to state a token figure without rework rate, reviewer rejection rate and "
            "revert count beside it (§7b) — efficiency can always be bought with worse work")
    samples = (eff or {}).get("beside_samples") or {}
    return (f"rework {_as_pct(beside['rework_rate'])} of {samples.get('rework', '?')} · "
            f"reviewer rejected {_as_pct(beside['reviewer_rejection_rate'])} of "
            f"{samples.get('reviewer', '?')} · {beside['reverts']} undone of "
            f"{samples.get('reverts', '?')} merged")


def _as_pct(rate) -> str:
    """`ledger._rate` returns a FRACTION (0–1), which the panel formats with its own `pct()`.
    Printing it beside a `%` sign was wrong by a factor of a hundred — a rework rate of 14% read as
    "0.1429%", which is not a small formatting slip: it is a number that goes into a model prompt
    and into a report the operator acts on, and it makes a bad week look flawless. `None` means no
    denominator, and says so rather than showing 0%."""
    if rate is None:
        return "n/a"
    return f"{round(float(rate) * 100, 1)}%"


def summary(stats: dict) -> dict:
    """The report's raw material, from `ledger.stats()`. Numbers only — no prose, no judgment."""
    q = stats.get("quality") or {}
    eff = stats.get("efficiency") or {}
    rev = stats.get("reverts_by_gate") or {}
    wall = stats.get("wall_clock") or {}
    esc = {s.get("key"): sum(int(p.get("v") or 0) for p in (s.get("points") or []))
           for s in (stats.get("escalation_split") or [])}
    return {
        "jobs": stats.get("jobs"),
        "events": stats.get("events"),
        "answered_by_monitor": esc.get("monitor", 0),
        "answered_by_evan": esc.get("evan", 0),
        "blocked_hours": (wall.get("total_hours") or {}).get("blocked"),
        "blocked_hours_before_ledger": wall.get("blocked_before_ledger_h"),
        "quality_line": _quality_line(eff),
        "reverts": {"approved": rev.get("approved"), "ungated": rev.get("ungated")},
        "rework_distribution": stats.get("rework_distribution"),
        "sample": {"rework": q.get("rework_sample"), "reviewer": q.get("reviewer_sample"),
                   "reverts": q.get("revert_sample")},
    }


_PROMPT = """Write the operator's weekly report card on how their automated work is going.

Same shape as every other card: TITLE, WHAT, WHY_YOU, CHECKED, RECOMMEND. Product language only —
no file paths, library names, environment variables, commands or code. They run the company.

Cover, briefly: what changed, what it cost, what improved, what got worse, and exactly ONE
recommendation. Any claim about efficiency MUST carry the quality figures in the same breath —
a week that spent fewer tokens and reworked more spent MORE, not less.

{split_note}

THE NUMBERS
{numbers}"""


def report(stats: dict, split: dict | None = None, effort: str = "medium") -> dict:
    """The weekly report card. `split` is `check_split`'s verdict when a movement is being claimed;
    an unclaimable split is passed to the model as a REFUSAL to call it an improvement, rather than
    left out and quietly re-derived by a model reading two numbers."""
    import monitor          # the monitor's card composer — not part of this snapshot
    s = summary(stats)
    note = ""
    if split is not None:
        note = ("THE SPLIT CHECK: " + split["why"] +
                (". You MAY call this an improvement."
                 if split.get("claimable") else
                 ". You MUST NOT call this an improvement — report it as a change and say why "
                 "it cannot be read as one."))
    return monitor.compose("report", _PROMPT.format(numbers=_numbers(s), split_note=note),
                           effort=effort, evidence={"stats": s, "split": split})


def _numbers(s: dict) -> str:
    return "\n".join([
        f"jobs recorded: {s['jobs']} (from {s['events']} events)",
        f"escalations answered by the monitor: {s['answered_by_monitor']}; by the operator: "
        f"{s['answered_by_evan']}",
        f"hours the machine spent blocked waiting on the operator: {s['blocked_hours']} "
        f"(plus {s['blocked_hours_before_ledger']} recorded before the ledger, which is a "
        f"measurement of waiting only — machine time was never recorded on that side)",
        f"quality: {s['quality_line']}",
        f"reverts by gate: {s['reverts']}",
        f"rework passes needed: {s['rework_distribution']}",
    ])


def text_report(stats: dict) -> str:
    """The same numbers with no model involved — what a test asserts against and what a terminal
    prints. The card is the model's READING of this; this is the thing itself, and it is what
    makes the report card checkable against something."""
    return _numbers(summary(stats))


if __name__ == "__main__":               # `py -3.12 dashboard/ledger_report.py`
    import ledger
    print(text_report(ledger.stats()))
