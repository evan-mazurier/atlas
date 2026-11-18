"""The Brief read model (design notes, not in this copy, contracts.md 5.3a): deadlines, the day's Plan blocks, counts.

Pure functions over already-parsed documents; no file, ledger, network or model access.
`build(day, schedule, tasks, areas, act)` is the one entry point. Deadlines live in the `deadlines:`
section of schedule.yaml (written only by the plan engine); a deadline has no time and never blocks anything.
"""
from __future__ import annotations

import datetime as dt

from . import model
from .model import schedmath

KINDS = ("deadline", "follow-up")
PRIORITIES = ("normal", "high")
FREQS = ("daily", "weekly", "monthly")
WEEKDAYS = schedmath.WEEKDAYS
DEFAULT_REMIND = {"deadline": 14, "follow-up": 0}
NEXT_DAYS = 30
MAX_REMIND = 366
SCAN_BACK_DAYS = 3660          # a series is scanned from at most ten years back (older misses are not listed)
MAX_ANCHOR_DAY = 28


# ── small helpers ──────────────────────────────────────────────────────────────────────────────
def _d(v) -> dt.date | None:
    return schedmath._parse_day(v)


def is_series(dl: dict) -> bool:
    return isinstance(dl.get("recur"), dict) and bool(dl["recur"])


def remind_days(dl: dict) -> int:
    v = dl.get("remind_days")
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return min(v, MAX_REMIND)
    return DEFAULT_REMIND.get(dl.get("kind"), DEFAULT_REMIND["deadline"])


def strs(v) -> set:
    """A list field as a set of strings; anything else (a hand-edit gone wrong) is empty."""
    return {str(x) for x in v} if isinstance(v, list) else set()


def days_text(n: int) -> str:
    """days_left wording: today, in 1 day, in N days; overdue: 1 day overdue, N days overdue."""
    if n == 0:
        return "today"
    if n > 0:
        return "in 1 day" if n == 1 else f"in {n} days"
    return "1 day overdue" if n == -1 else f"{-n} days overdue"


weekday_match = schedmath.weekday_match


def occurs_on(day0: dt.date, recur: dict, target: dt.date) -> bool:
    """schedmath._occurs_on (which also handles the monthly weekday rule); kept as the brief's entry point."""
    return schedmath._occurs_on(day0, recur, target)


def is_occurrence(dl: dict, key, allow_skipped: bool = False) -> bool:
    """Whether `key` (an ISO day) is a real series-generated occurrence day, and not skipped.
    A one-off item has exactly one: its own day."""
    k, day0 = _d(key), _d(dl.get("day"))
    if k is None or day0 is None:
        return False
    if not is_series(dl):
        return k == day0
    if not allow_skipped and k.isoformat() in strs(dl.get("skips")):
        return False
    return k >= day0 and (k == day0 or occurs_on(day0, dl["recur"], k))


def _occurrences(dl: dict, hi: dt.date, lo: dt.date | None = None) -> list[tuple[dt.date, dt.date, bool]]:
    """[(occurrence key, displayed day, done)] for keys up to `hi` (plus every override key that is real)."""
    day0 = _d(dl.get("day"))
    if day0 is None:
        return []
    ov = dl.get("overrides") if isinstance(dl.get("overrides"), dict) else {}
    if not is_series(dl):
        shown = _d((ov.get(day0.isoformat()) or {}).get("day")) if isinstance(ov.get(day0.isoformat()), dict) else None
        return [(day0, shown or day0, dl.get("done") is True)]
    skips, done_on = strs(dl.get("skips")), strs(dl.get("done_on"))
    until = _d(dl["recur"].get("until")) if dl["recur"].get("until") else None
    last = min(hi, until) if until else hi
    keys, cur = [], (day0 if lo is None else max(day0, lo))
    while cur <= last:
        if cur == day0 or occurs_on(day0, dl["recur"], cur):
            keys.append(cur)
        cur += dt.timedelta(days=1)
    seen = set(keys)
    for k in ov:
        kd = _d(k)
        if kd is not None and kd not in seen and kd > last and is_occurrence(dl, k):
            keys.append(kd)
    out = []
    for k in keys:
        iso = k.isoformat()
        if iso in skips:
            continue
        o = ov.get(iso)
        shown = _d(o.get("day")) if isinstance(o, dict) and o.get("day") else None
        out.append((k, shown or k, iso in done_on))
    return out


# ── deadlines ──────────────────────────────────────────────────────────────────────────────────
def _entry(dl: dict, key: dt.date, shown: dt.date, today: dt.date, areas: list) -> dict:
    left = (shown - today).days
    area = model.event_area(dl, areas)[0]
    return {"id": dl["id"], "occurrence_key": key.isoformat(), "day": shown.isoformat(),
            "title": dl.get("title"), "area": area,
            "kind": dl.get("kind") if dl.get("kind") in KINDS else "deadline",
            "priority": dl.get("priority") if dl.get("priority") in PRIORITIES else "normal",
            "days_left": left, "days_text": days_text(left), "overdue": left < 0,
            "notes": dl.get("notes") or None, "recurring": is_series(dl)}


def _one_entries(dl: dict, today: dt.date, horizon: dt.date, areas: list) -> list[dict]:
    remind = remind_days(dl)
    hi = max(horizon, today + dt.timedelta(days=remind))
    occ = [o for o in _occurrences(dl, hi, today - dt.timedelta(days=SCAN_BACK_DAYS)) if not o[2]]
    past = [o for o in occ if o[1] < today]
    upcoming = [o for o in occ if o[1] >= today]
    picks = []
    if past:
        picks.append(max(past, key=lambda o: (o[1], o[0])))
    if upcoming:
        picks.append(min(upcoming, key=lambda o: (o[1], o[0])))
    out = []
    for key, shown, _ in picks:
        e = _entry(dl, key, shown, today, areas)
        in_window = shown - dt.timedelta(days=remind) <= today
        if shown < today:
            e["group"] = "overdue"
        elif shown == today:
            e["group"] = "today"
        elif in_window and e["priority"] == "high":
            e["group"] = "high"
        elif in_window:
            e["group"] = "window"
        elif shown <= horizon:
            e["group"] = "next"
        else:
            e["group"] = "later"
        out.append(e)
    return out


def deadline_entries(deadlines: list, today: dt.date, areas: list) -> tuple[list[dict], int]:
    """(entries, skipped). At most one entry per one-off item and two per series (latest overdue, next
    upcoming); done ones are left out. Each carries `group`: overdue | today | high | window | next | later
    (later = shown nowhere). A malformed record (hand edit) is skipped and counted, never fatal."""
    out, skipped = [], 0
    horizon = today + dt.timedelta(days=NEXT_DAYS)
    for dl in deadlines if isinstance(deadlines, list) else []:
        try:
            if not isinstance(dl, dict) or not isinstance(dl.get("id"), str) or not dl["id"] \
                    or _d(dl.get("day")) is None \
                    or not isinstance(dl.get("title"), str) or not dl["title"].strip():
                raise ValueError("malformed deadline")
            out.extend(_one_entries(dl, today, horizon, areas))
        except Exception:  # noqa: BLE001 - one bad record never breaks the Brief
            skipped += 1
    return out, skipped


_ORDER = {"overdue": 0, "today": 1, "high": 2, "window": 3}


def _week(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


# ── today ──────────────────────────────────────────────────────────────────────────────────────
def today_items(day: dt.date, sched: dict, areas: list) -> list[dict]:
    iso = day.isoformat()
    items = []
    for r in model.expand_events(sched.get("events") or [], day, day, areas):
        items.append({"type": "event", "id": r["id"], "occurrence": r["occurrence"] if r.get("recur") else None,
                      "title": r.get("title"), "day": iso, "start": r.get("start"), "end": r.get("end"),
                      "area": r.get("area"), "done": r.get("done") is True, "can_mark_done": True})
    for b in sched.get("work_blocks") or []:
        if isinstance(b, dict) and str(b.get("day")) == iso:
            items.append({"type": "block", "id": b.get("id"), "occurrence": None,
                          "title": b.get("label") or "Work block", "day": iso,
                          "start": model.valid_hhmm(b.get("start")), "end": model.valid_hhmm(b.get("end")),
                          "area": None, "done": False, "can_mark_done": False})
    pa = model.project_areas(areas)
    for p in sched.get("placements") or []:
        if isinstance(p, dict) and str(p.get("day")) == iso:
            items.append({"type": "placement", "id": p.get("session"), "occurrence": None,
                          "title": p.get("task"), "day": iso,
                          "start": model.valid_hhmm(p.get("start")), "end": model.valid_hhmm(p.get("end")),
                          "area": pa.get(str(p.get("project"))), "done": bool(p.get("done")),
                          "can_mark_done": False})
    items.sort(key=lambda i: (bool(i["start"]), i["start"] or "", i["end"] or "", str(i["title"])))
    return items


# ── the payload ────────────────────────────────────────────────────────────────────────────────
def build(day, schedule: dict, tasks: dict | None, areas: list, act: list | None = None,
          act_status: dict | None = None) -> dict:
    """The GET /api/v2/plan/brief payload. `day` is a date or an ISO string. `tasks` is accepted for the
    contract's signature and unused (memo tasks are not shown, design notes, not in this copy). `act` and `act_status` come
    from the guarded gatherer (a later piece); empty by default."""
    today = day if isinstance(day, dt.date) else dt.date.fromisoformat(str(day))
    sched = schedule if isinstance(schedule, dict) else {}
    act = list(act or [])
    entries, skipped = deadline_entries(sched.get("deadlines") or [], today, areas)
    dnm = sorted((e for e in entries if e["group"] in _ORDER),
                 key=lambda e: (_ORDER[e["group"]], e["day"], str(e["title"])))
    nxt = sorted((e for e in entries if e["group"] == "next"), key=lambda e: (e["day"], str(e["title"])))
    for e in nxt:
        e["week"] = _week(e["day"])
    lo, hi = today.isoformat(), (today + dt.timedelta(days=NEXT_DAYS)).isoformat()
    in30 = [e for e in dnm + nxt if lo <= e["day"] <= hi]
    tod = today_items(today, sched, areas)
    events = [i for i in tod if i["type"] == "event"]
    return {"ok": True, "day": today.isoformat(), "do_not_miss": dnm, "act": act, "today": tod, "next": nxt,
            "counts": {"do_not_miss": len(dnm), "act": len(act), "today": len(tod), "next": len(nxt),
                       "overdue": sum(1 for e in dnm if e["overdue"]), "deadlines_30": len(in30),
                       "skipped": skipped, "events_today": len(events), "events_done": sum(1 for i in events if i["done"])},
            "act_status": dict(act_status or {})}
