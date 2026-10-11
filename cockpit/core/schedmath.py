"""
Atlas — schedmath: the deterministic collision guard (Contract C1).

Pure, stdlib-only "can this go here" arithmetic over the raw `schedule.yaml` shape
(a plain dict as returned by `yaml.safe_load`). This is the single source of truth
every write path (dispatcher.py: save_event, approve_planner's schedule branch)
consults before mutating schedule.yaml — no two schedule items (placements OR busy
events) may occupy overlapping time on the same day, and nothing may land on
unavailable time.

Lives in cockpit/core/ (the v1 dispatcher that used it was removed) and is FULLY SELF-CONTAINED — no YAML I/O,
no Atlas package imports, no `import state` (implements its own tiny recurrence
expander rather than reusing cockpit/state.py's) — so `dispatcher.py` can `import
schedmath` cleanly whether it's loaded by the cockpit server or by the standalone
Windows-side `worker.py`. Callers load schedule.yaml themselves and pass in the dict.

Day-view lane is 06:00-23:00, matching the frontend lane (cockpit/web/app.js's
SCHED_H0/SCHED_H1). Touching edges do NOT overlap — 10:00-12:00 sitting right next to
12:00-14:00 is fine, see `overlap()`.
"""
from __future__ import annotations

import datetime as dt
import re

DAY_START_MIN = 6 * 60     # 06:00
DAY_END_MIN = 23 * 60      # 23:00 — the lane's inclusive upper bound (end == DAY_END_MIN is valid)

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def to_min(hhmm: str) -> int:
    """'HH:MM' -> minutes since 00:00. Raises ValueError on anything unparseable."""
    if not isinstance(hhmm, str):
        raise ValueError(f"bad HH:MM: {hhmm!r}")
    m = _HHMM_RE.match(hhmm.strip())
    if not m:
        raise ValueError(f"bad HH:MM: {hhmm!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if mi >= 60 or h > 24 or (h == 24 and mi != 0):
        raise ValueError(f"bad HH:MM: {hhmm!r}")
    return h * 60 + mi


def overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    """True if minute-ranges [a0,a1) and [b0,b1) truly overlap. Touching edges
    (a1 == b0 or b1 == a0) do NOT count as overlap."""
    return a0 < b1 and b0 < a1


def _parse_day(day_iso) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(day_iso))
    except (ValueError, TypeError):
        return None


def _hhmm_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if _HHMM_RE.match(s) else None


def _occurs_on(day0: dt.date, recur: dict | None, target: dt.date) -> bool:
    """Whether an event anchored at `day0` with recurrence `recur` has an occurrence
    exactly on `target`. Mirrors cockpit/state.py's `_expand_recurrence` semantics
    (freq daily|weekly|monthly, interval, until inclusive, count) — computed directly
    for the one day being queried rather than materializing a whole window."""
    if target < day0:
        return False
    freq = (recur or {}).get("freq") if isinstance(recur, dict) else None
    if freq not in ("daily", "weekly", "monthly"):
        return target == day0
    try:
        interval = max(1, int(recur.get("interval") or 1))
    except (TypeError, ValueError):
        interval = 1
    until = None
    if recur.get("until"):
        try:
            until = dt.date.fromisoformat(str(recur["until"]))
        except (ValueError, TypeError):
            until = None
    count = None
    if recur.get("count") is not None:
        try:
            count = int(recur["count"])
        except (TypeError, ValueError):
            count = None
    if until is not None and target > until:
        return False
    if freq == "monthly":
        if target.day != day0.day:
            return False
        delta_months = (target.year - day0.year) * 12 + (target.month - day0.month)
        if delta_months < 0 or delta_months % interval != 0:
            return False
        m = delta_months // interval
        if count is None:
            return True
        # Mirrors cockpit/state.py's `_expand_recurrence`/`_add_period`: `count` limits VALID
        # occurrences only — a month where the anchor day doesn't exist (e.g. day 31 in a
        # 30-day month) is skipped WITHOUT consuming a count slot, so a day-29–31 anchor can
        # diverge from a naive "step index < count" check once a short month is skipped.
        # Same safety cap as the canonical loop (never load-bearing at realistic scales).
        if m > 2000:
            return False
        valid = 0
        for k in range(m + 1):
            total = day0.month - 1 + k * interval
            year = day0.year + total // 12
            month = total % 12 + 1
            try:
                dt.date(year, month, day0.day)
            except ValueError:
                continue
            valid += 1
        return valid <= count
    step = interval if freq == "daily" else interval * 7   # weekly
    delta = (target - day0).days
    if delta < 0 or delta % step != 0:
        return False
    n = delta // step
    return count is None or n < count


def _occurrence_override(ev: dict, day_iso: str) -> tuple[bool, str | None, str | None]:
    """Per-occurrence exceptions (Part 2 §8b: `overrides`/`skips` on a recurring event) for one
    event's instance on `day_iso` — shared by `busy_intervals`/`is_full_day` so the two never
    disagree, and mirrors cockpit/state.py's `_load_events` handling of the same fields.
    Returns (skipped, start_override, end_override); the overrides are None when `day_iso` isn't
    listed in `overrides` (base timing applies). `skipped` True means the date has no occurrence
    at all — callers must treat it as if the event weren't there that day."""
    skips = ev.get("skips") or []
    if not isinstance(skips, list):
        skips = []
    if day_iso in [str(s) for s in skips]:
        return True, None, None
    overrides = ev.get("overrides") if isinstance(ev.get("overrides"), dict) else {}
    ov = overrides.get(day_iso) if isinstance(overrides, dict) else None
    if isinstance(ov, dict):
        return False, _hhmm_or_none(ov.get("start")), _hhmm_or_none(ov.get("end"))
    return False, None, None


def busy_intervals(sched: dict, day_iso: str, *, exclude_session=None, exclude_event_id=None
                    ) -> list[tuple[int, int, str]]:
    """Every BUSY [start_min, end_min, label] interval on `day_iso`, drawn from:
    availability.blocks (matching that day), busy life-events (recur-expanded;
    all-day/full-day -> the whole 06:00-23:00 lane), and placements. `exclude_session`
    skips the placement whose `session` matches; `exclude_event_id` skips the event
    whose `id` matches — so a caller checking "does moving/editing THIS item collide
    with anything else" doesn't collide with its own current slot."""
    sched = sched or {}
    day = _parse_day(day_iso)
    out: list[tuple[int, int, str]] = []
    if day is None:
        return out

    av = sched.get("availability") or {}
    for b in (av.get("blocks") or []):
        if not isinstance(b, dict) or str(b.get("day")) != str(day_iso):
            continue
        s, e = _hhmm_or_none(b.get("start")), _hhmm_or_none(b.get("end"))
        if not s or not e:
            continue
        try:
            s0, e0 = to_min(s), to_min(e)
        except ValueError:
            continue
        if e0 > s0:
            out.append((s0, e0, "unavailable"))

    for ev in (sched.get("events") or []):
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id")
        if not eid or (exclude_event_id is not None and eid == exclude_event_id):
            continue
        busy = bool(ev.get("busy")) if "busy" in ev else True
        if not busy:
            continue
        day0 = _parse_day(ev.get("day"))
        if day0 is None:
            continue
        recur = ev.get("recur") if isinstance(ev.get("recur"), dict) else None
        if not _occurs_on(day0, recur, day):
            continue
        skipped, ov_start, ov_end = _occurrence_override(ev, day_iso)
        if skipped:
            continue
        title = ev.get("title") or eid
        s = ov_start or _hhmm_or_none(ev.get("start"))
        e = ov_end or _hhmm_or_none(ev.get("end"))
        s0 = to_min(s) if s else None
        e0 = to_min(e) if e else None
        if s0 is None or e0 is None or e0 <= s0:   # no/invalid/zero-length window == all-day
            out.append((DAY_START_MIN, DAY_END_MIN, title))
        else:
            out.append((s0, e0, title))

    for pl in (sched.get("placements") or []):
        if not isinstance(pl, dict) or str(pl.get("day")) != str(day_iso):
            continue
        if exclude_session is not None and pl.get("session") == exclude_session:
            continue
        s, e = _hhmm_or_none(pl.get("start")), _hhmm_or_none(pl.get("end"))
        if not s or not e:
            continue
        try:
            s0, e0 = to_min(s), to_min(e)
        except ValueError:
            continue
        if e0 > s0:
            label = pl.get("task") or pl.get("session") or "session"
            out.append((s0, e0, label))

    return out


def is_full_day(sched: dict, day_iso: str, *, exclude_event_id=None) -> bool:
    """True if `day_iso` is wholesale unavailable: a `full_days` entry OR an
    all-day busy event landing on it."""
    sched = sched or {}
    av = sched.get("availability") or {}
    if str(day_iso) in [str(d) for d in (av.get("full_days") or [])]:
        return True
    day = _parse_day(day_iso)
    if day is None:
        return False
    for ev in (sched.get("events") or []):
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id")
        if exclude_event_id is not None and eid == exclude_event_id:
            continue
        busy = bool(ev.get("busy")) if "busy" in ev else True
        if not busy:
            continue
        day0 = _parse_day(ev.get("day"))
        if day0 is None:
            continue
        recur = ev.get("recur") if isinstance(ev.get("recur"), dict) else None
        if not _occurs_on(day0, recur, day):
            continue
        skipped, ov_start, ov_end = _occurrence_override(ev, day_iso)
        if skipped:
            continue
        if ov_start or ov_end:   # an overridden occurrence always carries explicit times -> never all-day
            continue
        s, e = _hhmm_or_none(ev.get("start")), _hhmm_or_none(ev.get("end"))
        all_day = not s or not e
        if not all_day:
            try:
                all_day = to_min(e) <= to_min(s)
            except ValueError:
                all_day = True
        if all_day:
            return True
    return False


def _fmt(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def collision(sched: dict, day_iso: str, start: str, end: str, *,
              exclude_session=None, exclude_event_id=None) -> str | None:
    """Reason string if [start, end) can't be placed on `day_iso` — overlaps a busy
    interval, the day is fully unavailable, or the slot falls outside 06:00-23:00 /
    has start>=end. None if the slot is free."""
    day = _parse_day(day_iso)
    if day is None:
        return f"invalid day: {day_iso!r}"
    try:
        s0, e0 = to_min(start), to_min(end)
    except ValueError as exc:
        return str(exc)
    if e0 <= s0:
        return f"end ({end}) must be after start ({start})"
    if s0 < DAY_START_MIN or e0 > DAY_END_MIN:
        return "outside the 06:00–23:00 day"
    if is_full_day(sched, day_iso, exclude_event_id=exclude_event_id):
        return f"{day_iso} is fully unavailable"
    for b0, b1, label in busy_intervals(sched, day_iso, exclude_session=exclude_session,
                                         exclude_event_id=exclude_event_id):
        if overlap(s0, e0, b0, b1):
            return f"collides with {label} ({_fmt(b0)}–{_fmt(b1)})"
    return None


def day_busy(sched: dict, day_iso: str, *, exclude_session=None, exclude_event_id=None
             ) -> str | None:
    """Reason string if `day_iso` is occupied by ANY busy item — a placement, a timed busy
    event, another all-day busy event, or a `full_days` entry — else None if the day is
    wholly free. This is `collision()` over the FULL 06:00-23:00 lane: the guard an all-day
    busy event must pass before landing on/moving to a day (an all-day event occupies the
    entire day, so it collides with anything already busy that day, not just overlapping
    hours). `exclude_session`/`exclude_event_id` let a caller re-checking its OWN existing
    placement/event not collide with itself."""
    return collision(sched, day_iso, _fmt(DAY_START_MIN), _fmt(DAY_END_MIN),
                      exclude_session=exclude_session, exclude_event_id=exclude_event_id)


def earliest_free(sched: dict, day_iso: str, duration_min: int) -> str | None:
    """Earliest 'HH:MM' >= 06:00 where a `duration_min` block fits before 23:00 with
    no collision; None if nothing fits (fully unavailable, or every gap is too small)."""
    if duration_min <= 0:
        return None
    day = _parse_day(day_iso)
    if day is None:
        return None
    if is_full_day(sched, day_iso):
        return None
    busy = sorted(busy_intervals(sched, day_iso), key=lambda t: t[0])
    cursor = DAY_START_MIN
    for b0, b1, _label in busy:
        if b0 > cursor and b0 - cursor >= duration_min:
            return _fmt(cursor)
        if b1 > cursor:
            cursor = b1
    if DAY_END_MIN - cursor >= duration_min:
        return _fmt(cursor)
    return None


def _free_intervals(sched: dict, day_iso: str) -> list[tuple[int, int]]:
    """Every FREE [start_min, end_min) gap on `day_iso` within the 06:00-23:00 lane — the
    complement of `busy_intervals` (overlapping busy spans merged first). [] if the day is
    fully unavailable (a `full_days` entry or an all-day busy event). Internal helper backing
    `fill_free_slots`."""
    day = _parse_day(day_iso)
    if day is None or is_full_day(sched, day_iso):
        return []
    busy = sorted(busy_intervals(sched, day_iso), key=lambda t: t[0])
    merged: list[tuple[int, int]] = []
    for b0, b1, _label in busy:
        b0, b1 = max(b0, DAY_START_MIN), min(b1, DAY_END_MIN)
        if b1 <= b0:
            continue
        if merged and b0 <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b1))
        else:
            merged.append((b0, b1))
    free: list[tuple[int, int]] = []
    cursor = DAY_START_MIN
    for b0, b1 in merged:
        if b0 > cursor:
            free.append((cursor, b0))
        cursor = max(cursor, b1)
    if cursor < DAY_END_MIN:
        free.append((cursor, DAY_END_MIN))
    return free


def fill_free_slots(sched: dict, from_day_iso: str, minutes_needed: int, *,
                     horizon_days: int = 21, slot_cap_min: int | None = None) -> list[dict]:
    """Deterministic multi-slot fill (the reconciliation grow-then-split-fill, docs/scheduling.md):
    walk days forward from `from_day_iso` (inclusive) for up to `horizon_days` days, carving free
    openings — respecting availability blocks/full_days, busy events, and existing placements, all
    via `_free_intervals`/`busy_intervals` — into slices that sum to `minutes_needed` minutes.

    Returns a list of `{day, start, end}` slices, always within 06:00-23:00, never overlapping
    anything busy (or each other — each day is visited once). `slot_cap_min`, if given, caps how
    much of a single free opening one slice takes (a big empty day becomes several capped slices
    instead of one long one); None takes as much of the opening as still needed.

    Stops once `minutes_needed` is met or the horizon runs out — a short horizon or a packed
    calendar returns a PARTIAL fill (whatever fit); this never raises. Pure/stdlib, like the rest
    of this module."""
    day0 = _parse_day(from_day_iso)
    if day0 is None or minutes_needed <= 0:
        return []
    out: list[dict] = []
    remaining = minutes_needed
    for i in range(max(0, horizon_days)):
        if remaining <= 0:
            break
        day = day0 + dt.timedelta(days=i)
        day_iso = day.isoformat()
        for f0, f1 in _free_intervals(sched, day_iso):
            if remaining <= 0:
                break
            cursor = f0
            while cursor < f1 and remaining > 0:
                cap = slot_cap_min if slot_cap_min and slot_cap_min > 0 else (f1 - cursor)
                take = min(f1 - cursor, remaining, cap)
                if take <= 0:
                    break
                out.append({"day": day_iso, "start": _fmt(cursor), "end": _fmt(cursor + take)})
                remaining -= take
                cursor += take
    return out


# ══ WORK BLOCKS (the work-block layer) ════════════════════════════════════════════════════════
# A block is a stretch the operator is AT THE DESK: day, start, end, label. It replaces the per-task clock
# placement, which modelled the wrong resource — it timed each task as if worked one at a time,
# when in reality the dispatch engine runs three lanes at once and the operator's own time goes to steering
# them. So a block commits CAPACITY, not a task's wall-clock position.
#
# Four tracks per block: `LANES` dispatch lanes (the concurrent claude sessions the worker actually
# runs) + one MANUAL track (The operator's hands — serial, hand-placed). Lane assignment is DERIVED here,
# never stored: the worker takes the next ready task as a lane frees, so storing a lane per task
# would be a second source of truth that drifts from what the engine does. A task stores only its
# block and its position in that block's ordered set (plan.yaml `block` / `block_pos`).
#
# These live alongside `availability`/`events`/`placements` and deliberately do NOT enter
# `busy_intervals()`: a block is when work HAPPENS, not time that is unavailable, and folding it
# into the collision guard would make every block block its own tasks.

LANES = 3          # dispatch lanes per block — mirrors the worker's ATLAS_MAX_LANES / MAX_JOBS


def _blk_norm(b) -> dict | None:
    """One raw `work_blocks:` entry → a normalized dict, or None if unusable. Defensive like the
    rest of this module: a garbled entry is dropped, never raised on."""
    if not isinstance(b, dict):
        return None
    day = _parse_day(b.get("day"))
    s, e = _hhmm_or_none(b.get("start")), _hhmm_or_none(b.get("end"))
    if day is None or not s or not e:
        return None
    try:
        s0, e0 = to_min(s), to_min(e)
    except ValueError:
        return None
    if e0 <= s0:
        return None
    return {"id": str(b.get("id") or ""), "day": day.isoformat(), "start": s, "end": e,
            "start_min": s0, "end_min": e0, "label": str(b.get("label") or "").strip()}


def work_blocks(sched: dict, day_iso: str | None = None) -> list[dict]:
    """Every work block in `sched`, normalized and sorted by (day, start). `day_iso` narrows to one
    day. Unusable entries are dropped."""
    out = []
    for b in ((sched or {}).get("work_blocks") or []):
        n = _blk_norm(b)
        if n is None or not n["id"]:
            continue
        if day_iso is not None and n["day"] != str(day_iso):
            continue
        out.append(n)
    out.sort(key=lambda x: (x["day"], x["start_min"]))
    return out


def block_span_min(blk: dict) -> int:
    """A block's length in minutes (0 for an unusable block)."""
    n = _blk_norm(blk) if "start_min" not in (blk or {}) else blk
    return 0 if not n else max(0, n["end_min"] - n["start_min"])


def block_collision(sched: dict, day_iso: str, start: str, end: str, *,
                    exclude_block_id=None) -> str | None:
    """Reason string if a block can't sit on `day_iso` from `start` to `end`, else None.

    A block must land on time the operator actually HAS: it may not overlap unavailable time (availability
    blocks / full days / busy life-events) nor another work block, and must fit the 06:00–23:00
    lane. It deliberately ignores `placements` — those are the per-task layer blocks replace, and a
    legacy placement sitting under a new block is not a conflict."""
    day = _parse_day(day_iso)
    if day is None:
        return f"invalid day: {day_iso!r}"
    try:
        s0, e0 = to_min(start), to_min(end)
    except ValueError as exc:
        return str(exc)
    if e0 <= s0:
        return f"end ({end}) must be after start ({start})"
    if s0 < DAY_START_MIN or e0 > DAY_END_MIN:
        return "outside the 06:00–23:00 day"
    if is_full_day(sched, day_iso):
        return f"{day_iso} is fully unavailable"
    # unavailable time only — `busy_intervals` also returns placements, which a block may overlap.
    for b0, b1, label in busy_intervals(sched, day_iso):
        if label == "unavailable" or _is_event_label(sched, day_iso, label):
            if overlap(s0, e0, b0, b1):
                return f"collides with {label} ({_fmt(b0)}–{_fmt(b1)})"
    for other in work_blocks(sched, day_iso):
        if exclude_block_id is not None and other["id"] == exclude_block_id:
            continue
        if overlap(s0, e0, other["start_min"], other["end_min"]):
            lbl = other["label"] or other["id"]
            return f"collides with block {lbl} ({other['start']}–{other['end']})"
    return None


def _is_event_label(sched: dict, day_iso: str, label: str) -> bool:
    """Whether a `busy_intervals` label came from a life-EVENT (not a placement) on `day_iso`.
    `busy_intervals` flattens both into (start, end, label); a block must respect events but not
    placements, so the two have to be told apart."""
    day = _parse_day(day_iso)
    if day is None:
        return False
    for ev in ((sched or {}).get("events") or []):
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id")
        if not eid:
            continue
        if (ev.get("title") or eid) != label:
            continue
        day0 = _parse_day(ev.get("day"))
        recur = ev.get("recur") if isinstance(ev.get("recur"), dict) else None
        if day0 is not None and _occurs_on(day0, recur, day):
            return True
    return False


def fill_lanes(tasks: list[dict], *, lanes: int = LANES, capacity_min: int | None = None) -> dict:
    """Deterministic greedy lane fill — the derivation behind "3 dispatch lanes + 1 manual track".

    `tasks` is the block's ORDERED set (plan.yaml `block_pos` order), each entry a dict with at
    least `id`, and optionally `project`, `estimate_min`, `manual` (truthy → the manual track).
    Walk the order once; each dispatch task goes to the lane that frees EARLIEST (ties → the
    lowest-numbered lane), which is exactly what the worker does when a claude session frees. The
    manual track is serial by definition — the operator has one pair of hands — so it is simply a queue.

    Pure arithmetic over minutes-from-block-start; nothing here is a clock time, because a task in
    a block is never pinned to one. `capacity_min` (the block's length) marks which entries are
    COMMITTED (they start inside the block) versus overflowing it; a task that starts inside but
    runs past the end still stays put — overrun never auto-carries (that repacks the next day
    silently), it is reported and the operator re-assigns by hand.

    Returns {lanes: [[entry, ...], ...], manual: [entry, ...], committed_min, capacity_min,
    fill_pct, overflow: [id, ...], by_project: {pid: n}, manual_n, dispatch_n}."""
    n_lanes = max(1, int(lanes or 1))
    lane_ends = [0] * n_lanes
    lane_rows: list[list[dict]] = [[] for _ in range(n_lanes)]
    manual_rows: list[dict] = []
    manual_end = 0
    by_project: dict[str, int] = {}
    overflow: list[str] = []
    cap = capacity_min if (capacity_min and capacity_min > 0) else None

    for t in (tasks or []):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if not tid:
            continue
        try:
            dur = max(0, int(round(float(t.get("estimate_min") or 0))))
        except (TypeError, ValueError):
            dur = 0
        pid = t.get("project") or ""
        by_project[pid] = by_project.get(pid, 0) + 1
        if t.get("manual"):
            start, end = manual_end, manual_end + dur
            manual_end = end
            row = dict(t)
            row.update({"id": tid, "lane": "manual", "start_min": start, "end_min": end})
            manual_rows.append(row)
        else:
            # earliest-free lane; `min` over the list is stable, so ties take the lowest index
            li = lane_ends.index(min(lane_ends))
            start, end = lane_ends[li], lane_ends[li] + dur
            lane_ends[li] = end
            row = dict(t)
            row.update({"id": tid, "lane": li, "start_min": start, "end_min": end})
            lane_rows[li].append(row)
        if cap is not None and start >= cap:
            overflow.append(tid)
            row["overflow"] = True
        else:
            row["overflow"] = False
        # OVERRUN is a different, commoner condition than overflow. `overflow` means the task
        # could not even BEGIN before the block ended; a task that begins inside and simply runs
        # PAST the end was flagged nowhere, and its forecast bar was clipped at the track's right
        # edge — so the block's most ordinary overload case rendered as a perfect fit.
        row["runs_over_min"] = (max(0, end - cap)
                                if (cap is not None and start < cap) else 0)

    # COMMITTED lane-capacity: how much of the block's lane-time the assigned work actually claims,
    # clipped at the block's end (work running past it isn't capacity the block has to give).
    def _used(end: int) -> int:
        return end if cap is None else min(end, cap)
    committed = sum(_used(e) for e in lane_ends) + _used(manual_end)
    # How far past the block's end its work runs, on the track that overruns the most. 0 when
    # everything fits. Reported so a surface can SAY it rather than silently clipping a bar.
    over_min = 0 if cap is None else max([0] + [e - cap for e in (lane_ends + [manual_end])])
    total_cap = None if cap is None else cap * (n_lanes + 1)
    pct = 0 if not total_cap else int(round(min(committed / total_cap, 1.0) * 100))
    return {"lanes": lane_rows, "manual": manual_rows,
            "lane_ends": lane_ends, "manual_end": manual_end,
            "committed_min": committed, "capacity_min": total_cap, "fill_pct": pct,
            "over_min": over_min,
            "overflow": overflow, "by_project": by_project,
            "manual_n": len(manual_rows),
            "dispatch_n": sum(len(r) for r in lane_rows)}


def order_violations(sequence: list[dict], tasks_by_key: dict) -> list[dict]:
    """The ORDER GUARD: a task must not sit ahead of one it depends on.

    `sequence` is every BLOCK-ASSIGNED task in global working order — blocks sorted by (day, start),
    tasks by `block_pos` within each — as dicts with `key` ('<project>::<task>'), `id`, `block`.
    `tasks_by_key` maps that same key to the task record (needs `depends_on`, `state`, `title`).

    A dependency is satisfied when it is already `done`, or sits EARLIER in the sequence. It is
    violated when the dependency comes later, is assigned to no block at all (so the ordering says
    nothing about it), or names a task that doesn't exist. Returns one entry per violated pair —
    {key, task, depends_on, dep_key, reason} — most-blocking first is not attempted; the sequence
    order is preserved so the overlay reports them where they sit."""
    pos = {row["key"]: i for i, row in enumerate(sequence)}
    out: list[dict] = []
    for i, row in enumerate(sequence):
        key = row["key"]
        t = tasks_by_key.get(key) or {}
        proj = key.split("::")[0]
        for dep in (t.get("depends_on") or []):
            # guard BEFORE str(): `str(None)` is the truthy string 'None', which would invent a
            # dependency on a task called None out of a blank list entry.
            if not dep:
                continue
            dep = str(dep).strip()
            if not dep:
                continue
            dep_key = dep if "::" in dep else f"{proj}::{dep}"
            d = tasks_by_key.get(dep_key)
            if d is None:
                out.append({"key": key, "task": t.get("id") or row.get("id"), "depends_on": dep,
                            "dep_key": dep_key, "reason": f"depends on {dep}, which is not in the plan"})
                continue
            if (d.get("state") or "") == "done":
                continue
            dep_title = d.get("title") or dep
            if dep_key not in pos:
                out.append({"key": key, "task": t.get("id") or row.get("id"), "depends_on": dep,
                            "dep_key": dep_key,
                            "reason": f"sits ahead of {dep_title}, which is in no block"})
            elif pos[dep_key] > i:
                out.append({"key": key, "task": t.get("id") or row.get("id"), "depends_on": dep,
                            "dep_key": dep_key,
                            "reason": f"sits ahead of {dep_title}, which it depends on"})
    return out


def block_sequence(sched: dict, assigned: dict) -> list[dict]:
    """Every block-assigned task in global working order, ready for `order_violations`.

    `assigned` maps '<project>::<task>' → {'block': <block id>, 'block_pos': <int>}. Blocks are
    ordered by (day, start) — their real chronology — and tasks by `block_pos` within a block, with
    the key as a stable tie-break so an unset or duplicated position can't make the order depend on
    dict iteration. Tasks pointing at a block that doesn't exist are left out: they are unassigned
    in every sense that matters here."""
    order = {b["id"]: i for i, b in enumerate(work_blocks(sched))}
    rows = []
    for key, a in (assigned or {}).items():
        bid = (a or {}).get("block")
        if not bid or bid not in order:
            continue
        try:
            p = int(a.get("block_pos") or 0)
        except (TypeError, ValueError):
            p = 0
        rows.append({"key": key, "id": key.split("::", 1)[-1], "block": bid,
                     "block_pos": p, "_o": order[bid]})
    rows.sort(key=lambda r: (r["_o"], r["block_pos"], r["key"]))
    for r in rows:
        r.pop("_o", None)
    return rows
