"""The ledger — one append-only file, one line per thing that happened.

`docs/ledger.md`.

Three needs, one artefact: a safety net for unattended merging (revert rate, rework counts and
reviewer rejection rate are the instruments that say when to widen autonomy), an answer to "Atlas
burns a lot of tokens" that is measurable rather than felt, and the record of an applied experiment
in agentic engineering that **cannot be written retroactively**.

That last point is why this exists before anything that consumes it. Atlas has been destroying the
evidence it now needs: `ATLAS_KEEP=10` deletes all but the ten newest finished records, and the
`question` field is overwritten by the answer — the monitoring layer's design (not in this
snapshot) had to be reconstructed from five recoverable escalations out of 118.

THE STORAGE RULE (§2): **events only, statistics derived.** One line per fact. Never edited,
never pruned, never summarised in place. A stored summary is a second source of truth that drifts —
`CLAUDE.md`'s inputs-vs-outputs rule, the one that demoted `health` to legacy text. If a chart is
slow, cache the RESULT: a cache is disposable and rebuildable, so it is not a second truth.

The payoff is twofold and worth stating, because it is what makes the discipline worth keeping: a
metric nobody has thought of yet is answerable from July's data in December, and a formula found to
be wrong is fixed once, correcting every chart retroactively.
"""
from __future__ import annotations

import json
import os
import threading
import datetime as dt
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = Path(os.environ.get("ATLAS_LEDGER_DIR") or (ATLAS_ROOT / "ledger"))
EVENTS = LEDGER_DIR / "events.jsonl"

# The worker runs several jobs and several lanes concurrently in one process, so writes race.
# A lock plus one append per line is enough: every line is well under the pipe-atomic size and
# there is exactly one writing process (the worker holds a heartbeat lock against a second).
_LOCK = threading.Lock()


_DEFAULT_EVENTS = ATLAS_ROOT / "ledger" / "events.jsonl"


def _is_test_run() -> bool:
    """True when a test would otherwise write the REAL ledger.

    Found the day the first slice merged: one `pytest` run injected 308 fake events — `job_a`,
    `j1`, project `demo` — into the live file, because a worker whose `save()` emits is driven by
    its own tests too, and only `test_ledger.py` had thought to redirect the path.

    A guard here rather than a fixture alone, because the failure is silent and cumulative: nothing
    would have gone wrong, the evidence would just have quietly stopped being evidence. The check
    is deliberately narrow — under pytest AND pointed at the real file. A test that redirects
    `EVENTS` (as `test_ledger.py` does) still writes normally, so the ledger's own coverage is
    unaffected."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and EVENTS == _DEFAULT_EVENTS


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def emit(ev: str, job: dict | None = None, **fields) -> None:
    """Append one event. NEVER raises.

    A ledger that can fail a job is worse than no ledger: the whole point is that it runs beside
    real work for months without anyone thinking about it. A write that fails prints and is lost —
    the loss is one line, and the alternative is a lost job.
    """
    if _is_test_run():
        return
    try:
        rec = {"t": _now(), "ev": ev}
        if job is not None:
            # A lane IS the gate context (see worker.LANE_INHERITS), so a lane dict carries the
            # job's id. §3: every event carries its lane, because a fanned job runs several with
            # their own tests, reviewers and rework — without it they collapse into one blurred row.
            rec["job"] = job.get("id")
            rec["project"] = job.get("project")
            rec["lane"] = job.get("lane_id")
        rec.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with _LOCK:
            LEDGER_DIR.mkdir(parents=True, exist_ok=True)
            with EVENTS.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 — see the docstring; a ledger never fails a job
        print(f"  ! ledger write skipped ({exc})")


def read_events(path: Path | None = None) -> list[dict]:
    """Every event, in file order. A malformed line is skipped rather than fatal — the file is
    append-only and a torn last line from a hard kill must not cost the other 40,000."""
    p = path or EVENTS
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def jobs_index(events: list[dict] | None = None) -> dict:
    """Group events by job id, in order. The one derived structure kept here because every
    statistic needs it; it is computed on read and never stored (§2)."""
    evs = events if events is not None else read_events()
    out: dict[str, list[dict]] = {}
    for e in evs:
        jid = e.get("job")
        if jid:
            out.setdefault(jid, []).append(e)
    return out


# ═══ the statistics, derived on read (§2, §4, §7) ════════════════════════════════════
# NOTHING here is stored. Every number below is computed from the event file each time it is asked
# for, which is the storage rule doing its job: a formula found to be wrong is fixed once and every
# past week corrects itself, and a metric nobody has thought of yet is still answerable from July.
#
# Two properties are structural rather than remembered:
#
#   * A SERIES THAT DID NOT EXIST BEFORE THE LEDGER BEGINS AT THE LEDGER (§8). Backfill knows
#     state transitions and escalation COUNTS; it knows no verdicts, no tokens, no who-answered.
#     Drawing those as zero for July would say "nothing was rejected in July" — a claim the data
#     does not make. Every series therefore carries `from_week`, and the panel starts its line
#     there rather than at the axis.
#   * AN EFFICIENCY FIGURE CANNOT BE BUILT ALONE (§7b). `_efficiency` REQUIRES the quality block
#     and raises without it, so a token chart cannot reach the panel with nothing beside it. This
#     is the guard against optimising tokens into worse output and calling it a win.

_WEEK_FMT = "%G-W%V"


def _dt(v):
    try:
        return dt.datetime.fromisoformat(str(v))
    except Exception:  # noqa: BLE001
        return None


def _week(v) -> str | None:
    d = _dt(v)
    return d.strftime(_WEEK_FMT) if d else None


def _week_span(evs) -> list[str]:
    """Every week from the first event to the last, INCLUDING the empty ones. A week with no work
    is a fact about the week; skipping it would draw a fortnight's gap as a straight line."""
    stamps = [d for d in (_dt(e.get("t")) for e in evs) if d]
    if not stamps:
        return []
    cur, last = min(stamps).date(), max(stamps).date()
    cur -= dt.timedelta(days=cur.weekday())
    out = []
    while cur <= last:
        out.append(cur.strftime(_WEEK_FMT))
        cur += dt.timedelta(days=7)
    return out


def _week_starts(weeks) -> dict:
    """`{"2026-W35": "2026-08-24"}` — the Monday each ISO week begins on.

    The axis is labelled with the DATE, not the week number: "W35" is a correct label nobody can
    read without counting, and the charts exist to be glanced at. Computed here rather than in the
    panel so the two never disagree about which Monday a week starts on."""
    out = {}
    for w in weeks:
        try:
            out[w] = dt.datetime.strptime(w + "-1", "%G-W%V-%u").date().isoformat()
        except Exception:  # noqa: BLE001
            continue
    return out


def _series(weeks, counts: dict, label: str, key: str, from_week: str | None) -> dict:
    """One drawable line. `from_week` is where the series BEGINS to exist; before it the panel draws
    nothing at all rather than a zero."""
    pts = [{"week": w, "v": counts.get(w, 0)} for w in weeks
           if from_week is None or w >= from_week]
    return {"key": key, "label": label, "from_week": from_week, "points": pts}


def _ledger_start(evs) -> str | None:
    for e in evs:
        if e.get("ev") == "ledger_start":
            return e.get("t")
    return None


def _live(evs):
    """Events the ledger itself recorded — everything the backfill could not know lives here."""
    return [e for e in evs if e.get("src") != "backfill"]


def _rate(n, d):
    return None if not d else round(n / d, 4)


def _finished_per_job(evs) -> dict:
    """The LAST `finished` event of each job. One job finishes once, however many times the worker
    happened to write the record."""
    out = {}
    for e in evs:
        if e.get("ev") == "finished" and e.get("job"):
            out[e["job"]] = e
    return out


def _quality(evs) -> dict:
    """The three numbers that must accompany any claim about tokens (§7b), plus the sample size
    each rests on. A rate with no denominator beside it is how a run of four jobs becomes '25%'."""
    live = _live(evs)
    # PER JOB, not per event. The file is append-only and permanent, so a period that recorded a
    # job's `finished` more than once (the worker's save hook, not in this snapshot, once wrote one job's 401 times) is history
    # now: it cannot be edited, and it must not be counted twice. §2's own promise is that a
    # formula found to be wrong is fixed once and corrects every past week with it, so the fix
    # belongs HERE rather than in the file.
    rework = [int(f.get("rework") or 0) for f in _finished_per_job(live).values()]
    first = [e for e in live if e.get("ev") == "review" and int(e.get("pass") or 1) == 1]
    rejected = [e for e in first if e.get("verdict") != "commit-ready"]
    merged = [e for e in live if e.get("ev") == "merged"]
    reverts = [e for e in live if e.get("ev") == "revert"]
    return {
        "rework_rate": _rate(sum(1 for r in rework if r > 0), len(rework)),
        "rework_sample": len(rework),
        "reviewer_rejection_rate": _rate(len(rejected), len(first)),
        "reviewer_sample": len(first),
        # §5: rare and slow. A running COUNT with its sample beside it, never a percentage of a
        # handful — and split by gate, because the comparison is the entire point.
        "reverts": len(reverts),
        "revert_sample": len(merged),
    }


def _efficiency(tokens: dict, quality: dict) -> dict:
    """Token figures, and the quality figures that must be read with them.

    Raises without the quality block on purpose (§7b): "no efficiency number without the quality
    numbers beside it" is a property of this payload, not a habit of whoever draws the chart. A
    panel physically cannot render a token series that arrived alone.
    """
    need = ("rework_rate", "reviewer_rejection_rate", "reverts")
    missing = [k for k in need if k not in (quality or {})]
    if missing:
        raise ValueError(
            "refusing to build an efficiency block without the quality figures beside it "
            f"(missing {', '.join(missing)}) — see docs/ledger.md §7b")
    return {**tokens, "beside": {k: quality[k] for k in need},
            "beside_samples": {"rework": quality.get("rework_sample"),
                               "reviewer": quality.get("reviewer_sample"),
                               "reverts": quality.get("revert_sample")}}


def _reverts_by_gate(evs) -> dict:
    """§5 — the comparison that licenses more autonomy: did work the operator approved revert any less
    often than work that went through ungated? Read from the ledger alone: a job carries a
    human-approved merge gate, or it does not."""
    by_job = jobs_index(_live(evs))
    gated_merges = gated_reverts = ungated_merges = ungated_reverts = 0
    for evl in by_job.values():
        if not any(e.get("ev") == "merged" for e in evl):
            continue
        human = any(e.get("ev") == "gate" and e.get("which") == "merge" and e.get("by") == "evan"
                    for e in evl)
        reverted = any(e.get("ev") == "revert" for e in evl)
        if human:
            gated_merges += 1
            gated_reverts += 1 if reverted else 0
        else:
            ungated_merges += 1
            ungated_reverts += 1 if reverted else 0
    return {"approved": {"reverts": gated_reverts, "of": gated_merges},
            "ungated": {"reverts": ungated_reverts, "of": ungated_merges}}


def _wall_clock(evs) -> dict:
    """Where a job's wall clock actually went: working · blocked on the operator · queued on capacity.

    Reconstructed from consecutive `state` events, which is the only honest source — a job's own
    record holds `created` and `finished` and nothing in between.

    LIVE EVENTS ONLY, and this is the §8 rule doing real work rather than being quoted. The
    backfill looked usable here: it carries timestamped transitions back to July. But reading the
    log showed it only ever recorded `needs_input`, `ready`, `merged`, `failed` and `stopped` —
    **never a start-of-work transition**. So on the far side of the switch line, WAITING is well
    recorded and MACHINE TIME is structurally invisible, and a stacked bar over both said "1.6h
    working, 95.8h blocked". That is not a finding about Atlas, it is an artefact of what the log
    happened to print, and it would have been the most quotable number on the panel.

    The pre-ledger waiting is not thrown away — it is reported beside the split as its own figure,
    where it cannot masquerade as a share of a total nobody can compute.
    """
    # `ready` is BLOCKED, not working. It means the tests are green, the reviewer is done, and the
    # job is sitting on the merge gate waiting for the operator — the worker has finished with it entirely.
    # Counting it as machine time understated the one number the ledger exists to move: the
    # first day of real data carried a 20-hour `ready` wait, which read as twenty hours of work.
    BLOCKED = {"needs_input", "ready"}
    QUEUED = {"queued", "held", "paused"}
    tot = {"working": 0, "blocked": 0, "queued": 0}
    per_week: dict = {}
    for evl in jobs_index(evs).values():
        states = [e for e in evl if e.get("ev") == "state" and _dt(e.get("t"))]
        for a, b in zip(states, states[1:]):
            secs = int((_dt(b["t"]) - _dt(a["t"])).total_seconds())
            if secs <= 0 or secs > 86400 * 3:
                continue          # a clock that ran overnight into a dead job is not "blocked"
            to = str(a.get("to") or "")
            kind = "blocked" if to in BLOCKED else "queued" if to in QUEUED else "working"
            tot[kind] += secs
            w = _week(a["t"])
            per_week.setdefault(w, {"working": 0, "blocked": 0, "queued": 0})[kind] += secs
    return {"total_hours": {k: round(v / 3600, 1) for k, v in tot.items()},
            "weeks": [{"week": w, **{k: round(v / 3600, 1) for k, v in d.items()}}
                      for w, d in sorted(per_week.items())]}


def _blocked_before_the_ledger(evs) -> float:
    """Hours of waiting recorded by the backfill — reported on its own, never as part of the
    split. See `_wall_clock`: the same events know when a job started waiting and never know when
    it started working, so these hours are a real measurement of one thing and no measurement at
    all of the other."""
    bf = [e for e in evs if e.get("src") == "backfill"]
    return _wall_clock(bf)["total_hours"]["blocked"]


def _token_timeline(evs, bucket_min: int = 60) -> dict:
    """A CONTINUOUS timeline of burn (§6) — never bucketed into either clock, because the rolling
    5-hour window and the operator's work blocks answer different questions and adding them is meaningless.
    Fresh in+out is the subscription-relevant number; cache read/write is tracked beside it and
    never mixed in, since re-sent context dwarfs the real figure 10-80x.
    """
    pts: dict = {}
    for e in evs:
        if e.get("ev") != "phase_tokens":
            continue
        d = _dt(e.get("t"))
        if not d:
            continue
        key = d.replace(minute=0, second=0, microsecond=0) if bucket_min >= 60 else d
        row = pts.setdefault(key.isoformat(timespec="seconds"),
                             {"t": key.isoformat(timespec="seconds"), "fresh": 0,
                              "cache_read": 0, "cache_write": 0})
        row["fresh"] += int(e.get("fresh") or 0) + int(e.get("out") or 0)
        row["cache_read"] += int(e.get("cache_read") or 0)
        row["cache_write"] += int(e.get("cache_write") or 0)
    series = [pts[k] for k in sorted(pts)]
    # the rolling 5-hour window — the CONSTRAINT ("am I about to be cut off?"), drawn against the
    # cap rather than against the schedule.
    roll = []
    for i, row in enumerate(series):
        t1 = _dt(row["t"])
        window = sum(r["fresh"] for r in series[:i + 1]
                     if (t1 - _dt(r["t"])).total_seconds() < 5 * 3600)
        roll.append({"t": row["t"], "v": window})
    by_phase: dict = {}
    for e in evs:
        if e.get("ev") == "phase_tokens":
            p = str(e.get("phase") or "session")
            b = by_phase.setdefault(p, {"phase": p, "fresh": 0, "cache_read": 0, "cache_write": 0})
            b["fresh"] += int(e.get("fresh") or 0) + int(e.get("out") or 0)
            b["cache_read"] += int(e.get("cache_read") or 0)
            b["cache_write"] += int(e.get("cache_write") or 0)
    total_fresh = sum(r["fresh"] for r in series)
    total_read = sum(r["cache_read"] for r in series)
    return {"points": series, "rolling_5h": roll,
            "by_phase": sorted(by_phase.values(), key=lambda x: -x["fresh"]),
            "fresh_total": total_fresh, "cache_read_total": total_read,
            # higher is better: context reused rather than re-sent
            "cache_read_ratio": _rate(total_read, total_read + total_fresh)}


def _recent_jobs(evs, limit: int = 25) -> list:
    """The plain table under the charts. One row per job, newest first — what it was, what it cost,
    how it ended. Task text is included: this view is the operator's, not the public export's."""
    rows = []
    for jid, evl in jobs_index(evs).items():
        start = next((e for e in evl if e.get("ev") == "started"), None)
        fin = next((e for e in reversed(evl) if e.get("ev") == "finished"), None)
        rev = next((e for e in reversed(evl) if e.get("ev") == "review"), None)
        t0, t1 = _dt(evl[0].get("t")), _dt(evl[-1].get("t"))
        rows.append({
            "job": jid,
            "project": next((e.get("project") for e in evl if e.get("project")), None),
            "task": (start or {}).get("task"),
            "t": evl[0].get("t"),
            "minutes": int((t1 - t0).total_seconds() / 60) if (t0 and t1) else None,
            "state": (fin or {}).get("status")
                     or next((e.get("to") for e in reversed(evl) if e.get("ev") == "state"), None),
            "verdict": (rev or {}).get("verdict"),
            "rework": (fin or {}).get("rework"),
            "escalations": sum(1 for e in evl if e.get("ev") == "escalation"),
            "tokens": (fin or {}).get("tokens"),
            "backfilled": all(e.get("src") == "backfill" for e in evl),
        })
    rows.sort(key=lambda r: str(r.get("t") or ""), reverse=True)
    return rows[:limit]


def stats(events: list[dict] | None = None) -> dict:
    """Everything the panel draws, derived from the event file alone (§4, §7)."""
    evs = events if events is not None else read_events()
    weeks = _week_span(evs)
    start = _ledger_start(evs)
    start_week = _week(start)
    live = _live(evs)

    # 1. Human interventions per week — THE ARC. Backfilled escalations count as the operator's, because
    #    before the monitor existed nobody else could answer one; that is knowable, not assumed.
    #    Gate approvals begin at the ledger, so they are their own series and start there.
    esc_evan: dict = {}
    for e in evs:
        if e.get("ev") == "escalation":
            w = _week(e.get("t"))
            if w:
                esc_evan[w] = esc_evan.get(w, 0) + 1
    gates_evan: dict = {}
    for e in live:
        if e.get("ev") == "gate" and e.get("by") == "evan":
            w = _week(e.get("t"))
            if w:
                gates_evan[w] = gates_evan.get(w, 0) + 1

    # 2. Escalations: monitor vs the operator. Does not exist before the ledger — `by` was never recorded.
    by_monitor: dict = {}
    by_evan: dict = {}
    for e in live:
        if e.get("ev") == "escalation_answered":
            w = _week(e.get("t"))
            if not w:
                continue
            (by_monitor if e.get("by") == "monitor" else by_evan)[w] = \
                (by_monitor if e.get("by") == "monitor" else by_evan).get(w, 0) + 1

    # 3. Reviewer verdicts, first pass only: approved vs rejected.
    ok1: dict = {}
    rej1: dict = {}
    for e in live:
        if e.get("ev") == "review" and int(e.get("pass") or 1) == 1:
            w = _week(e.get("t"))
            if not w:
                continue
            d = ok1 if e.get("verdict") == "commit-ready" else rej1
            d[w] = d.get(w, 0) + 1

    # 4. Rework distribution — how many passes a job needed before it was accepted.
    dist = {"0": 0, "1": 0, "2": 0, "3+": 0}
    for f in _finished_per_job(live).values():          # one bar entry per JOB, not per event
        n = int(f.get("rework") or 0)
        dist["3+" if n >= 3 else str(n)] += 1

    quality = _quality(evs)
    return {
        "generated": _now(),
        "events": len(evs),
        "jobs": len(jobs_index(evs)),
        "weeks": weeks,
        "week_starts": _week_starts(weeks),
        # the switch line every chart draws (§8), and what is missing on the far side of it
        "ledger_start": start,
        "ledger_start_week": start_week,
        "partial_note": "before this line: timings, states and escalation counts only — "
                        "no question text, no verdicts, no tokens",
        "interventions": [
            _series(weeks, esc_evan, "escalations that stopped for the operator", "escalations", None),
            _series(weeks, gates_evan, "gates the operator approved", "gates", start_week),
        ],
        "escalation_split": [
            _series(weeks, by_evan, "answered by the operator", "evan", start_week),
            _series(weeks, by_monitor, "answered by the monitor", "monitor", start_week),
        ],
        "review_verdicts": [
            _series(weeks, ok1, "approved first pass", "approved", start_week),
            _series(weeks, rej1, "rejected", "rejected", start_week),
        ],
        "rework_distribution": dist,
        "wall_clock": {**_wall_clock(live),
                       "from_week": start_week,
                       "blocked_before_ledger_h": _blocked_before_the_ledger(evs)},
        "quality": quality,
        "reverts_by_gate": _reverts_by_gate(evs),
        "efficiency": _efficiency(_token_timeline(live), quality),
        "recent": _recent_jobs(evs),
    }


# ═══ the public export (§9) ════════════════════════════════════════════════════════════════════
# The ledger contains client material: task text, escalation questions, reviewer notes. One
# surviving escalation names a client's production database. That cannot be published.
#
# So the public export is numbers only — and that is a HARD PROPERTY OF THE EXPORTER, not a filter
# someone has to remember. There is no code path here that can put a value read from the ledger into
# the output unless it is a number. Project names become ordinals; verdicts and event names become
# KEYS drawn from literals written in this file, never from the data. `_numeric_only` re-checks the
# result before it is returned, so the property holds even if a future edit forgets it.


class ExportLeak(Exception):
    """The public export tried to emit something that was not a number. A bug, always."""


def _numeric_only(obj, where="export"):
    """Recursively assert every LEAF is a number. Keys are code-authored; values must not be text."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ExportLeak(f"{where}: non-string key {k!r}")
            _numeric_only(v, f"{where}.{k}")
        return obj
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            _numeric_only(v, f"{where}[{i}]")
        return obj
    if isinstance(obj, bool) or not isinstance(obj, (int, float)):
        raise ExportLeak(f"{where}: {type(obj).__name__} {obj!r} is not a number")
    return obj


# The vocabulary the export may report, written HERE as literals. A verdict or event name that does
# not appear in this file cannot appear in the output — it can only ever be counted.
_EXPORT_VERDICTS = ("commit-ready", "needs-changes", "escalate")
_EXPORT_STATES = ("merged", "failed", "stopped", "discarded")


def public_export(events: list[dict] | None = None) -> dict:
    """Numbers only, projects aliased by ordinal. Safe to publish as-is."""
    evs = events if events is not None else read_events()
    by_job = jobs_index(evs)

    projects: dict[str, int] = {}
    for e in evs:
        p = e.get("project")
        if isinstance(p, str) and p not in projects:
            projects[p] = len(projects) + 1

    per_project: dict[str, dict] = {}
    for p, n in projects.items():
        alias = f"project_{n}"                       # generated, never read from the ledger
        per_project[alias] = {"jobs": 0, "merged": 0}
    for jid, evl in by_job.items():
        p = next((e.get("project") for e in evl if e.get("project")), None)
        if not isinstance(p, str):
            continue
        row = per_project[f"project_{projects[p]}"]
        row["jobs"] += 1
        row["merged"] += sum(1 for e in evl if e.get("ev") == "merged")

    def count(ev, **match):
        return sum(1 for e in evs if e.get("ev") == ev
                   and all(e.get(k) == v for k, v in match.items()))

    esc = [e for e in evs if e.get("ev") == "escalation_answered"]
    waits = [e.get("waited_s") for e in esc if isinstance(e.get("waited_s"), (int, float))]

    out = {
        "jobs": len(by_job),
        "events": len(evs),
        "escalations": count("escalation"),
        "escalations_answered_by_monitor": count("escalation_answered", by="monitor"),
        "escalations_answered_by_human": count("escalation_answered", by="evan"),
        "escalation_wait_seconds_total": int(sum(waits)),
        "reverts": count("revert"),
        "reviews_by_verdict": {v.replace("-", "_"): count("review", verdict=v)
                               for v in _EXPORT_VERDICTS},
        # per JOB: a record written repeatedly must not report as many finished jobs
        "jobs_by_final_state": {
            st: sum(1 for evl in by_job.values()
                    if next((e.get("to") for e in reversed(evl) if e.get("ev") == "state"), None) == st)
            for st in _EXPORT_STATES},
        "tokens_fresh": int(sum(e.get("fresh") or 0 for e in evs if e.get("ev") == "phase_tokens")),
        "tokens_cache": int(sum((e.get("cache_read") or 0) + (e.get("cache_write") or 0)
                                for e in evs if e.get("ev") == "phase_tokens")),
        "projects": per_project,
    }
    return _numeric_only(out)


# ═══ backfill (§8) ═════════════════════════════════════════════════════════════════════════════
# `logs/worker.log` holds 81 jobs and 118 escalations back to 2026-07-01, and the surviving job
# records add verdicts and tokens for the newest ten. Imported as a CLEARLY-MARKED PARTIAL PERIOD,
# because the honesty matters more than the coverage: a chart implying "no escalations in July"
# because none were recorded would be worse than no chart.
#
# What the log actually yields, having read it rather than assumed: state transitions with
# timestamps (`<job> -> needs_input|ready|merged|failed`), merges with their project, and the first
# line that names a job's project and task. No question text, no tokens, no verdicts — those were
# deleted by ATLAS_KEEP long before this ran.

import re  # noqa: E402 — kept beside the parser it belongs to

_LOG_TS = r"\[(\d{4}-\d\d-\d\dT[\d:]+)\]"
_LOG_JOB = r"(job_\d{8}_\d{6}_[0-9a-f]{4})"
_RE_STATE = re.compile(rf"^{_LOG_TS}\s+{_LOG_JOB}\s+->\s+(\S+)")
_RE_MERGING = re.compile(rf"^{_LOG_TS}\s+merging\s+{_LOG_JOB}\s+\(([^:]+):")
_RE_START = re.compile(rf"^{_LOG_TS}\s+(?:resuming\s+)?(\w[\w-]*)\s+{_LOG_JOB}\s+\(([^:]+):")


def backfill(worker_log: Path | None = None, jobs_dir: Path | None = None,
             out: Path | None = None) -> dict:
    """Import the pre-ledger period. Idempotent by construction: it writes to its own file and the
    caller decides whether to run it, rather than trying to detect duplicates in an append-only log.

    Returns a summary. Every event it writes carries `"src": "backfill"` and the period is stamped
    with a `ledger_start` marker, so a chart can draw the switch line §8 requires and begin a
    series that does not exist before it AT the line, rather than drawing it as zero.
    """
    log = worker_log or (ATLAS_ROOT / "logs" / "worker.log")
    jdir = jobs_dir or (ATLAS_ROOT / "jobs")
    target = out or EVENTS

    events: list[dict] = []
    seen_jobs: dict[str, str] = {}                   # job id -> project, as discovered

    if log.exists():
        # The supervisor writes this as UTF-16 (PowerShell's default redirect). Read it as bytes
        # and sniff, rather than assuming either way — a wrong guess yields 1,500 lines of mojibake
        # that parse to nothing and look like an empty log.
        raw = log.read_bytes()
        text = raw.decode("utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8",
                          errors="replace")
        for line in text.splitlines():
            line = line.rstrip()
            m = _RE_MERGING.match(line) or _RE_START.match(line)
            if m:
                g = m.groups()
                jid, proj = (g[1], g[2]) if len(g) == 3 else (g[2], g[3])
                seen_jobs.setdefault(jid, proj.strip())
            m = _RE_STATE.match(line)
            if m:
                ts, jid, state = m.groups()
                state = state.lower().rstrip(".")
                events.append({"t": ts, "ev": "state", "job": jid,
                               "project": seen_jobs.get(jid), "to": state, "src": "backfill"})
                if state == "needs_input":
                    # The count survives; the QUESTION does not. Recorded without a `q` field
                    # rather than with an empty one, so "we do not know" never reads as "none".
                    events.append({"t": ts, "ev": "escalation", "job": jid,
                                   "project": seen_jobs.get(jid), "src": "backfill",
                                   "text_lost": True})

    # The surviving records add what the log never had, for the newest ~10 jobs.
    records = 0
    if jdir.exists():
        for f in sorted(jdir.glob("job_*.json")):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            records += 1
            base = {"job": j.get("id"), "project": j.get("project"), "src": "backfill"}
            if j.get("created"):
                events.append({**base, "t": j["created"], "ev": "started",
                               "type": j.get("session_type"), "model": j.get("model"),
                               "effort": j.get("effort")})
            if j.get("review_verdict"):
                events.append({**base, "t": j.get("finished") or j.get("created"),
                               "ev": "review", "verdict": j["review_verdict"]})
            if j.get("tokens") or j.get("cache_tokens"):
                events.append({**base, "t": j.get("finished") or j.get("created"),
                               "ev": "phase_tokens", "phase": "job_total",
                               "fresh": j.get("tokens") or 0,
                               "cache_read": j.get("cache_tokens") or 0, "cache_write": 0})
            if j.get("merged_at"):
                events.append({**base, "t": j["merged_at"], "ev": "merged",
                               "commit": j.get("commit")})

    events.sort(key=lambda e: str(e.get("t") or ""))
    if events:
        events.append({"t": _now(), "ev": "ledger_start",
                       "note": "events above this line are BACKFILLED and partial: no escalation "
                               "question text, and tokens/verdicts only for the newest records"})
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, target.open("a", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    jobs = {e["job"] for e in events if e.get("job")}
    return {"ok": True, "events": len(events), "jobs": len(jobs), "records": records,
            "escalations": sum(1 for e in events if e.get("ev") == "escalation")}


if __name__ == "__main__":   # `py -3.12 dashboard/ledger.py --backfill`
    import sys as _sys
    if "--backfill" in _sys.argv:
        print(json.dumps(backfill(), indent=2))
    elif "--export" in _sys.argv:
        print(json.dumps(public_export(), indent=2))
    else:
        evs = read_events()
        print(f"{len(evs)} events in {EVENTS}")
        if evs:
            print(f"  {evs[0]['t']}  ->  {evs[-1]['t']}")
