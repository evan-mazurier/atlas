"""Plan engine API (W1 Lane 3, contracts.md section 5.3).

GET  /api/v2/plan?from=&to=      the whole Plan payload (never state.snapshot(), never transcripts)
POST /api/v2/plan/...            every write carries "base"; one locked read-modify-write each
"""
from __future__ import annotations

import contextlib
import datetime as dt
import sys
import time
import traceback
import uuid

from . import model, store, versions
from .model import (EVENT_TYPES, TASK_STATES, OWNERS, schedmath, valid_day, valid_hhmm)
from .store import PlanError

SCHED_PREFIXES = ("evt:", "plc:", "blk:")
_tray_cache: dict = {"t": 0.0, "n": 0}


# ── answers ────────────────────────────────────────────────────────────────────────────────────
def _fail(exc: PlanError):
    return exc.code, {"ok": False, "error": exc.message, **exc.extra}


def _unexpected(exc: Exception):
    tb = traceback.extract_tb(exc.__traceback__)
    where = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "?"
    print(f"plan.api: {type(exc).__name__} at {where}", file=sys.stderr)
    return 500, {"ok": False, "error": f"plan: {type(exc).__name__} at {where}"}


# ── GET ────────────────────────────────────────────────────────────────────────────────────────
def get(path, query):
    if path.rstrip("/") != "/api/v2/plan":
        return None
    try:
        return 200, _read(query or {})
    except PlanError as exc:
        return _fail(exc)
    except Exception as exc:  # noqa: BLE001
        return _unexpected(exc)


def _range(query: dict):
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    frm = valid_day(query["from"]) if query.get("from") else monday.isoformat()
    to = valid_day(query["to"]) if query.get("to") else (monday + dt.timedelta(days=13)).isoformat()
    if frm is None or to is None:
        raise PlanError(400, "from/to must be YYYY-MM-DD")
    frm, to = dt.date.fromisoformat(frm), dt.date.fromisoformat(to)
    if to < frm:
        raise PlanError(400, "to is before from")
    if (to - frm).days > 400:
        raise PlanError(400, "range too long (max 400 days)")
    return frm, to


def _locks(stack: contextlib.ExitStack, *paths):
    for p in paths:
        if p.exists():
            stack.enter_context(store.locked(p))


def _read(query: dict) -> dict:
    frm, to = _range(query)
    sp, tp, vp = store.schedule_path(), store.tasks_path(), store.version_path()
    today = dt.date.today()
    with contextlib.ExitStack() as stack:
        _locks(stack, sp, tp)
        stack.enter_context(store.locked(vp))
        sched, stext = store.read_doc(sp)
        tdoc, ttext = store.read_doc(tp)
        runs = model._auto_runs(min(frm, today - dt.timedelta(days=model.TRAY_DAYS)).isoformat(),
                                max(to, today).isoformat())
        tray_src = model.tray_runs()        # the SAME set the writes use, so every listed item can be scheduled
        tray = model.review_tray(tray_src, tdoc.get("reviews") or [], sched.get("events") or [])
        st = versions.load()
        current, prefixes = model.tray_objects(tray), ["rv:"]
        if stext is not None:
            current.update(model.schedule_objects(sched))
            prefixes += SCHED_PREFIXES
        if ttext is not None:
            current.update(model.tasks_objects(tdoc))
            prefixes.append("task:")
        if versions.detect(st, tuple(prefixes), current, quiet_new=("rv:",)):
            with contextlib.suppress(PlanError):
                versions.save(st)
        version = st.version
    return model.build(sched, tdoc, frm, to, version, runs, tray_src)


def review_tray_count() -> int:
    """Pending reviews for the attention bar. Cheap (5 s cache); 0 on any trouble."""
    now = time.monotonic()
    if now - _tray_cache["t"] < 5:
        return _tray_cache["n"]
    try:
        tdoc = store.parse(store.read_text(store.tasks_path()), "tasks.yaml")
        sched = store.parse(store.read_text(store.schedule_path()), "schedule.yaml")
        n = len(model.review_tray(model.tray_runs(), tdoc.get("reviews") or [], sched.get("events") or []))
    except Exception:  # noqa: BLE001
        n = 0
    _tray_cache.update(t=now, n=n)
    return n


# ── the write transaction ──────────────────────────────────────────────────────────────────────
class Ctx:
    def __init__(self, sched, tdoc, st, base, current, runs, areas):
        self.sched, self.tdoc, self.st, self.base = sched, tdoc, st, base
        self.current, self.runs, self.areas = current, runs, areas
        self.area_ids = {a["id"] for a in areas}
        self.touched: dict = {}
        self.removed: list = []
        self.dirty_sched: set = set()
        self.dirty_tasks: set = set()
        self.extra: dict = {}

    def section(self, doc: dict, name: str, dirty: set) -> list:
        lst = doc.get(name)
        if not isinstance(lst, list):
            lst = doc[name] = []
        dirty.add(name)
        return lst

    def s(self, name: str) -> list:
        return self.section(self.sched, name, self.dirty_sched)

    def t(self, name: str) -> list:
        return self.section(self.tdoc, name, self.dirty_tasks)

    def check(self, *keys):
        versions.check(self.st, keys, self.base, self.current)


def _txn(body: dict, write: str, fn, tray: bool = False):
    """write = 'sched' | 'tasks'. Runs fn(ctx, body) -> changed object inside the locks."""
    base = versions.base_of(body)
    sp, tp, vp = store.schedule_path(), store.tasks_path(), store.version_path()
    target = sp if write in ("sched", "both") else tp
    if not target.exists():
        raise PlanError(503, "no plan file")
    with contextlib.ExitStack() as stack:
        _locks(stack, sp, tp)
        stack.enter_context(store.locked(vp))
        sched, stext = store.read_doc(sp)
        tdoc, ttext = store.read_doc(tp)
        areas = model.load_areas()
        runs, current, prefixes = [], {}, []
        if stext is not None:
            current.update(model.schedule_objects(sched))
            prefixes += SCHED_PREFIXES
        if ttext is not None:
            current.update(model.tasks_objects(tdoc))
            prefixes.append("task:")
        if tray:
            runs = model.tray_runs()
            current.update({k: v for k, v in model.tray_objects(
                model.review_tray(runs, tdoc.get("reviews") or [], sched.get("events") or [])).items()
                if k not in current})
            prefixes.append("rv:")
        st = versions.load()
        if versions.detect(st, tuple(prefixes), current, quiet_new=("rv:",)):
            with contextlib.suppress(PlanError):
                versions.save(st)
        ctx = Ctx(sched, tdoc, st, base, current, runs, areas)
        changed = fn(ctx, body)
        # write the touched sections only; everything else in the file stays byte-identical
        do_s = write == "sched" or (write == "both" and bool(ctx.dirty_sched))
        do_t = write == "tasks" or (write == "both" and bool(ctx.dirty_tasks))
        if do_t and ttext is None:
            raise PlanError(503, "no plan file")
        out_s = out_t = None
        if do_s:
            out_s = store.splice(stext, {k: sched.get(k, []) for k in sorted(ctx.dirty_sched, key=_sched_order)})
        if do_t:
            out_t = store.splice(ttext, {k: tdoc.get(k, []) for k in sorted(ctx.dirty_tasks)})
        if out_s is not None:
            store.atomic_write(sp, out_s.encode("utf-8"))
        if out_t is not None:
            store.atomic_write(tp, out_t.encode("utf-8"))
        touched = {k: (v if v is not None else {}) for k, v in ctx.touched.items()}
        version = versions.commit(st, touched, ctx.removed)
        with contextlib.suppress(PlanError):
            versions.save(st)
        _tray_cache["t"] = 0.0
    return {"ok": True, "version": version, "changed": changed, **ctx.extra}


def _sched_order(name: str) -> int:
    return ("availability", "placements", "events", "work_blocks").index(name) if name in (
        "availability", "placements", "events", "work_blocks") else 9


def post(path, body):
    route = path.rstrip("/")
    handler = _ROUTES.get(route)
    if handler is None:
        return None
    try:
        write, fn, tray = handler
        return 200, _txn(body if isinstance(body, dict) else {}, write, fn, tray)
    except PlanError as exc:
        return _fail(exc)
    except Exception as exc:  # noqa: BLE001
        return _unexpected(exc)


# ── helpers shared by handlers ─────────────────────────────────────────────────────────────────
def _find(lst: list, field: str, value):
    for i, e in enumerate(lst):
        if isinstance(e, dict) and e.get(field) == value:
            return i
    return None


def _need(body: dict, field: str) -> str:
    v = str(body.get(field) or "").strip()
    if not v:
        raise PlanError(400, f"{field} required")
    return v


def _day(v, what="day") -> str:
    d = valid_day(v)
    if d is None:
        raise PlanError(400, f"invalid {what}: {v!r}")
    return d


def _hm(v, what="time") -> str:
    h = valid_hhmm(v)
    if h is None:
        raise PlanError(400, f"invalid {what} (HH:MM): {v!r}")
    return h


def _sched_for_guard(ctx: Ctx) -> dict:
    return ctx.sched


def _window_reason(start, end) -> str | None:
    """Timed events stay inside the Plan day (07:00 to 22:00)."""
    s, e = model._min(start), model._min(end)
    if s is None or e is None:
        return None
    if s < model.DAY_START_MIN or e > model.DAY_END_MIN:
        return f"{start}-{end} is outside the {model._hhmm(model.DAY_START_MIN)}-{model._hhmm(model.DAY_END_MIN)} day"
    return None


def _all_day_title(ctx: Ctx, day: str, exclude_id: str | None) -> str | None:
    """Title of an all-day event on `day` (a recurring occurrence counts, a skipped one does not)."""
    d = dt.date.fromisoformat(day)
    for r in model.expand_events(ctx.sched.get("events") or [], d, d, []):
        if r["id"] != exclude_id and not (r.get("start") and r.get("end")):
            return str(r.get("title") or r["id"])
    return None


def _attention_reason(ctx: Ctx, ev: dict, day: str, start: str, end: str, eid: str) -> str | None:
    """The parallel-planning guard (contracts.md 5.3) over the other timed busy occurrences of `day`
    (recurrences expanded, overrides and skips applied, `eid` left out). A FULL event overlaps nothing;
    a SHARED one overlaps no FULL event and at most MAX_PARALLEL shared ones run at any instant."""
    d = dt.date.fromisoformat(day)
    s0, e0 = model._min(start), model._min(end)
    mine = model.effective_attention(ev, ctx.areas)[0]
    name = ev.get("title") or eid
    full, shared = [], []
    for r in model.expand_events(ctx.sched.get("events") or [], d, d, ctx.areas):
        if r["id"] == eid or not (bool(r["busy"]) if "busy" in r else True):
            continue
        a, b = model._min(r.get("start")), model._min(r.get("end"))
        if a is None or b is None or b <= a:
            continue                                  # all-day: not a timed event
        (full if r["attention"] == "full" else shared).append((a, b, str(r.get("title") or r["id"])))
    span = lambda a, b: f"{model._hhmm(a)}-{model._hhmm(b)}"  # noqa: E731
    if mine == "full":
        for a, b, t in sorted(full + shared):
            if a < e0 and b > s0:
                return f"overlaps {t!r} ({span(a, b)}); {name!r} needs full attention"
        return None
    for a, b, t in sorted(full):
        if a < e0 and b > s0:
            return f"overlaps {t!r} ({span(a, b)}), which needs full attention"
    cuts = sorted({x for a, b, _ in shared for x in (a, b)})
    busy_runs: list = []
    for a, b in zip(cuts, cuts[1:]):
        if sum(1 for x, y, _ in shared if x < b and y > a) >= model.MAX_PARALLEL:
            if busy_runs and busy_runs[-1][1] == a:
                busy_runs[-1][1] = b
            else:
                busy_runs.append([a, b])
    for a, b in busy_runs:
        if a < e0 and b > s0:
            return f"{model.MAX_PARALLEL} sessions already run in parallel {span(a, b)}"
    return None


def _slot_reason(ctx: Ctx, ev: dict, day: str, start: str, end: str, eid: str) -> str | None:
    """schedmath.collision against placements, availability blocks and full days (old reason text),
    then the attention guard against the other events. Events are left out of the schedmath check."""
    sched = dict(ctx.sched)
    sched["events"] = []
    return schedmath.collision(sched, day, start, end, exclude_event_id=eid) or _attention_reason(ctx, ev, day, start, end, eid)


def _event_guard(ctx: Ctx, ev: dict, old: dict | None, verb: str = "save") -> None:
    """Checked only when the day/time/busy/attention fields are new or changed (a notes-only edit on a
    legacy event is not refused): the 07:00-22:00 window; a day that already has an all-day event takes no
    NEW event (timed or all-day); an all-day event is never busy-checked; a timed busy event may not land
    on a placement or unavailable time, and obeys the attention rules (_attention_reason)."""
    busy = bool(ev.get("busy")) if "busy" in ev else True
    sig = lambda e: (e.get("day"), e.get("start"), e.get("end"), bool(e.get("busy")) if "busy" in e else True,  # noqa: E731
                     model.effective_attention(e, ctx.areas)[0])
    if old is not None and sig(old) == sig(ev):
        return
    name = ev.get("title") or ev["id"]
    timed = bool(ev.get("start") and ev.get("end"))
    if timed:
        reason = _window_reason(ev["start"], ev["end"])
        if reason:
            raise PlanError(400, f"can't {verb} {name!r}: {reason}")
    if old is None or old.get("day") != ev.get("day"):
        other = _all_day_title(ctx, ev["day"], ev["id"])
        if other:
            raise PlanError(400, f"can't {verb} {name!r}: {ev['day']} already has the all-day event {other!r}")
    if not timed or not busy:
        return
    reason = _slot_reason(ctx, ev, ev["day"], ev["start"], ev["end"], ev["id"])
    if reason:
        raise PlanError(400, f"can't {verb} {name!r}: {reason}")


def _merge_event(ctx: Ctx, stored: dict | None, req: dict, eid: str) -> dict:
    out = dict(stored) if stored else {"id": eid}
    touches_time = False
    for k, v in req.items():
        if k == "id":
            continue
        if v is None or (v == "" and k in ("start", "end", "notes", "location", "remind", "area", "attention")):
            if k in ("start", "end"):
                touches_time = True
            out.pop(k, None)
            continue
        if k == "title":
            s = str(v).strip()
            if not s:
                raise PlanError(400, "event requires a title")
            out[k] = s
        elif k == "day":
            out[k] = _day(v)
        elif k in ("start", "end"):
            touches_time = True
            out[k] = _hm(v, k)
        elif k == "type":
            t = str(v).strip().lower()
            out[k] = t if t in EVENT_TYPES else "personal"
        elif k == "area":
            if str(v) not in ctx.area_ids:
                raise PlanError(400, f"unknown area: {v!r}")
            out[k] = str(v)
        elif k == "attention":
            if v not in model.ATTENTIONS:
                raise PlanError(400, f"attention must be one of {', '.join(model.ATTENTIONS)}")
            out[k] = v
        elif k == "busy":
            out[k] = bool(v)
        elif k == "recur":
            if not isinstance(v, dict):
                raise PlanError(400, "recur must be an object or null")
            if v:
                out[k] = store.normalise(v)
            else:
                out.pop(k, None)
        else:
            out[k] = store.normalise(v)
    if not str(out.get("title") or "").strip():
        raise PlanError(400, "event requires a title")
    if not out.get("day"):
        raise PlanError(400, "event requires a valid day (YYYY-MM-DD)")
    if touches_time and bool(out.get("start")) != bool(out.get("end")):
        raise PlanError(400, "start and end go together (or neither for an all-day event)")
    if req.get("attention"):
        aid, _ = model.event_area(out, ctx.areas)
        area = next((a for a in ctx.areas if a["id"] == aid), None)
        if area and area.get("attention_fixed") is True and out["attention"] != model.area_attention(area):
            raise PlanError(400, f"can't save {out['title']!r}: {area.get('name') or area['id']} is always {model.area_attention(area)}")
    if stored is None and "type" not in out:
        out["type"] = "personal"
    if stored is None:
        order = ["id", "title", "day", "type", "start", "end"]
        out = {**{k: out[k] for k in order if k in out}, **{k: v for k, v in out.items() if k not in order}}
    return out


def _new_id(prefix: str, existing: set) -> str:
    while True:
        i = f"{prefix}_{uuid.uuid4().hex[:8]}"
        if i not in existing:
            return i


def _ids(lst: list, field: str = "id") -> set:
    return {e.get(field) for e in lst if isinstance(e, dict)}


# ── events ─────────────────────────────────────────────────────────────────────────────────────
def h_event(ctx: Ctx, body: dict):
    req = body.get("event")
    if not isinstance(req, dict):
        raise PlanError(400, "event must be an object")
    events = ctx.s("events")
    eid = str(req.get("id") or "").strip()
    idx = _find(events, "id", eid) if eid else None
    if not eid:
        eid = _new_id("evt", _ids(events))
    stored = events[idx] if idx is not None else None
    if stored is not None:
        ctx.check(f"evt:{eid}")
    ev = _merge_event(ctx, stored, req, eid)
    _event_guard(ctx, ev, stored)
    if idx is None:
        events.append(ev)
    else:
        events[idx] = ev
    ctx.touched[f"evt:{eid}"] = ev
    return ev


def _event_or_404(ctx: Ctx, body: dict):
    eid = _need(body, "id")
    events = ctx.s("events")
    idx = _find(events, "id", eid)
    if idx is None:
        raise PlanError(404, f"unknown event: {eid!r}")
    ctx.check(f"evt:{eid}")
    return events, idx, eid


def h_event_delete(ctx: Ctx, body: dict):
    events, idx, eid = _event_or_404(ctx, body)
    ev = events.pop(idx)
    ctx.removed.append(f"evt:{eid}")
    return ev


def _is_recurring(ev: dict) -> bool:
    return isinstance(ev.get("recur"), dict) and bool(ev["recur"])


def h_event_move(ctx: Ctx, body: dict):
    events, idx, eid = _event_or_404(ctx, body)
    ev = dict(events[idx])
    day = _day(body.get("day"))
    s_req = _hm(body["start"], "start") if body.get("start") else None
    e_req = _hm(body["end"], "end") if body.get("end") else None
    occ = _day(body["occurrence"], "occurrence") if body.get("occurrence") else None
    if occ and _is_recurring(ev):
        ov = (ev.get("overrides") or {}).get(occ) if isinstance(ev.get("overrides"), dict) else None
        base_s = valid_hhmm(ov.get("start")) if isinstance(ov, dict) and ov.get("start") else ev.get("start")
        base_e = valid_hhmm(ov.get("end")) if isinstance(ov, dict) and ov.get("end") else ev.get("end")
        start, end = s_req or base_s, e_req or base_e
        if day == occ:
            if not (start and end):
                raise PlanError(400, "start and end required to retime one occurrence")
            reason = _window_reason(start, end) or _slot_reason(ctx, ev, occ, start, end, eid)
            if reason:
                raise PlanError(400, f"can't retime {ev.get('title') or eid!r} on {occ}: {reason}")
            overrides = dict(ev.get("overrides") or {})
            overrides[occ] = {"start": start, "end": end}
            ev["overrides"] = overrides
            events[idx] = ev
            ctx.touched[f"evt:{eid}"] = ev
            return ev
        new = {"id": _new_id("evt", _ids(events)), "title": ev.get("title"), "day": day,
               "type": ev.get("type") or "personal"}
        if ev.get("area"):
            new["area"] = ev["area"]
        if start and end:
            new["start"], new["end"] = start, end
        for k in ("busy", "notes", "location", "remind"):
            if k in ev:
                new[k] = ev[k]
        new["from_series"] = eid
        was_done = occ in [str(d) for d in (ev.get("done_on") or [])]
        if was_done:
            new["done"] = True
        _event_guard(ctx, new, None, "move")
        skips = [str(s) for s in (ev.get("skips") or [])]
        if occ not in skips:
            skips.append(occ)
        ev["skips"] = skips
        if was_done:
            rest = [d for d in (ev.get("done_on") or []) if str(d) != occ]
            if rest:
                ev["done_on"] = rest
            else:
                ev.pop("done_on", None)
        overrides = dict(ev.get("overrides") or {})
        if overrides.pop(occ, None) is not None:
            if overrides:
                ev["overrides"] = overrides
            else:
                ev.pop("overrides", None)
        events[idx] = ev
        events.append(new)
        ctx.touched[f"evt:{eid}"] = ev
        ctx.touched[f"evt:{new['id']}"] = new
        ctx.extra["series"] = ev
        return new
    # a single event (or the whole series: the anchor day moves, every occurrence with it)
    old = dict(ev)
    ev["day"] = day
    if s_req:
        ev["start"] = s_req
    if e_req:
        ev["end"] = e_req
    if bool(ev.get("start")) != bool(ev.get("end")):
        raise PlanError(400, "start and end go together (or neither for an all-day event)")
    _event_guard(ctx, ev, old, "move")
    events[idx] = ev
    ctx.touched[f"evt:{eid}"] = ev
    return ev


def h_event_skip(ctx: Ctx, body: dict):
    events, idx, eid = _event_or_404(ctx, body)
    ev = dict(events[idx])
    occ = _day(body.get("occurrence"), "occurrence")
    if not _is_recurring(ev):
        raise PlanError(400, f"{eid!r} is not a recurring event")
    skips = [str(s) for s in (ev.get("skips") or [])]
    if occ not in skips:
        skips.append(occ)
    ev["skips"] = skips
    overrides = dict(ev.get("overrides") or {})
    if overrides.pop(occ, None) is not None:
        if overrides:
            ev["overrides"] = overrides
        else:
            ev.pop("overrides", None)
    events[idx] = ev
    ctx.touched[f"evt:{eid}"] = ev
    return ev


def h_event_done(ctx: Ctx, body: dict):
    """Manual, optional done mark; changes no time, so no collision/window guards. Idempotent."""
    events, idx, eid = _event_or_404(ctx, body)
    ev = dict(events[idx])
    flag = body.get("done")
    if not isinstance(flag, bool):
        raise PlanError(400, "done must be true or false")
    if _is_recurring(ev):
        if not body.get("occurrence"):
            raise PlanError(400, "occurrence required to mark one occurrence of a recurring event")
        occ = _day(body["occurrence"], "occurrence")
        day0 = schedmath._parse_day(ev.get("day"))
        skips = [str(x) for x in (ev.get("skips") or [])]
        cur = schedmath._parse_day(occ)
        if day0 is None or cur is None or cur < day0 or occ in skips or not (
                cur == day0 or schedmath._occurs_on(day0, ev["recur"], cur)):
            raise PlanError(400, f"{occ} is not an occurrence of {eid!r}")
        done_on = sorted({str(d) for d in (ev.get("done_on") or [])} | ({occ} if flag else set()))
        if not flag:
            done_on = [d for d in done_on if d != occ]
        if done_on:
            ev["done_on"] = done_on
        else:
            ev.pop("done_on", None)
    elif flag:
        ev["done"] = True
    else:
        ev.pop("done", None)
    events[idx] = ev
    ctx.touched[f"evt:{eid}"] = ev
    return ev


# ── placements ─────────────────────────────────────────────────────────────────────────────────
def h_placement_move(ctx: Ctx, body: dict):
    sess = _need(body, "session")
    pls = ctx.s("placements")
    idx = _find(pls, "session", sess)
    if idx is None:
        raise PlanError(404, f"unknown placement: {sess!r}")
    ctx.check(f"plc:{sess}")
    day = _day(body.get("day"))
    start, end = str(body.get("start") or ""), str(body.get("end") or "")
    reason = schedmath.collision(ctx.sched, day, start, end, exclude_session=sess)
    if reason:
        raise PlanError(400, f"can't move {pls[idx].get('task') or sess!r}: {reason}")
    pl = dict(pls[idx])
    pl["day"], pl["start"], pl["end"], pl["pinned"] = day, valid_hhmm(start), valid_hhmm(end), True
    pls[idx] = pl
    ctx.touched[f"plc:{sess}"] = pl
    return pl


def h_placement_delete(ctx: Ctx, body: dict):
    sess = _need(body, "session")
    pls = ctx.s("placements")
    idx = _find(pls, "session", sess)
    if idx is None:
        raise PlanError(404, f"unknown placement: {sess!r}")
    ctx.check(f"plc:{sess}")
    pl = pls.pop(idx)
    ctx.removed.append(f"plc:{sess}")
    return pl


def h_placement_add(ctx: Ctx, body: dict):
    project, task = _need(body, "project"), _need(body, "task")
    day = _day(body.get("day"))
    start, end = str(body.get("start") or ""), str(body.get("end") or "")
    reason = schedmath.collision(ctx.sched, day, start, end)
    if reason:
        raise PlanError(400, f"can't add {task!r}: {reason}")
    pls = ctx.s("placements")
    sess = _new_id("sess", _ids(pls, "session"))
    pl = {"session": sess, "project": project, "task": task, "day": day,
          "start": valid_hhmm(start), "end": valid_hhmm(end), "pinned": True}
    pls.append(pl)
    ctx.touched[f"plc:{sess}"] = pl
    ctx.extra["session"] = sess
    return pl


# ── work blocks ────────────────────────────────────────────────────────────────────────────────
def _save_block(ctx: Ctx, req: dict, existing: dict | None):
    blocks = ctx.s("work_blocks")
    day = valid_day(req.get("day"))
    if day is None:
        raise PlanError(400, f"a work block requires a valid day (YYYY-MM-DD): {req.get('day')!r}")
    start, end = valid_hhmm(req.get("start")), valid_hhmm(req.get("end"))
    if not start or not end:
        raise PlanError(400, f"a work block requires HH:MM start and end: {req.get('start')!r}-{req.get('end')!r}")
    bid = str(req.get("id") or "").strip() or _new_id("blk", _ids(blocks))
    reason = schedmath.block_collision(ctx.sched, day, start, end, exclude_block_id=bid)
    if reason:
        raise PlanError(400, f"cannot place this block on {day} {start}-{end}: {reason}")
    rec = {"id": bid, "day": day, "start": start, "end": end}
    label = str(req.get("label") or "").strip()
    if label:
        rec["label"] = label
    idx = _find(blocks, "id", bid)
    if idx is None:
        blocks.append(rec)
    else:
        blocks[idx] = rec
    blocks.sort(key=lambda b: (str(b.get("day")), str(b.get("start"))) if isinstance(b, dict) else ("", ""))
    ctx.touched[f"blk:{bid}"] = rec
    return rec


def h_block(ctx: Ctx, body: dict):
    req = body.get("block") if isinstance(body.get("block"), dict) else body
    bid = str(req.get("id") or "").strip()
    if bid and _find(ctx.s("work_blocks"), "id", bid) is not None:
        ctx.check(f"blk:{bid}")
    return _save_block(ctx, req, None)


def h_block_delete(ctx: Ctx, body: dict):
    bid = _need(body, "id")
    blocks = ctx.s("work_blocks")
    idx = _find(blocks, "id", bid)
    if idx is None:
        raise PlanError(404, f"unknown work block: {bid!r}")
    ctx.check(f"blk:{bid}")
    blk = blocks.pop(idx)
    ctx.removed.append(f"blk:{bid}")
    # v1 also released the tasks assigned to the block (writes into project plan.yaml files);
    # v2 may not write those files (contract section 0), so nothing is released here.
    return blk


def h_block_move(ctx: Ctx, body: dict):
    bid = _need(body, "id")
    blocks = ctx.s("work_blocks")
    idx = _find(blocks, "id", bid)
    if idx is None:
        raise PlanError(404, f"unknown work block: {bid!r}")
    ctx.check(f"blk:{bid}")
    rec = dict(blocks[idx])
    rec["day"] = str(body.get("day") or rec.get("day"))
    if body.get("start"):
        rec["start"] = str(body["start"])
    if body.get("end"):
        rec["end"] = str(body["end"])
    return _save_block(ctx, rec, blocks[idx])


# ── tasks.yaml ─────────────────────────────────────────────────────────────────────────────────
def _merge_task(ctx: Ctx, stored: dict | None, req: dict, tid: str) -> dict:
    out = dict(stored) if stored else {"id": tid}
    for k, v in req.items():
        if k in ("id", "source"):
            continue
        if v is None or v == "":
            out.pop(k, None)
            continue
        if k == "area":
            if str(v) not in ctx.area_ids:
                raise PlanError(400, f"unknown area: {v!r}")
            out[k] = str(v)
        elif k == "title":
            out[k] = str(v).strip()
        elif k == "state":
            if v not in TASK_STATES:
                raise PlanError(400, f"state must be one of {', '.join(TASK_STATES)}")
            out[k] = v
        elif k == "owner":
            if v not in OWNERS:
                raise PlanError(400, "owner must be me or agents")
            out[k] = v
        elif k == "estimate_min":
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise PlanError(400, "estimate_min must be a whole number of minutes")
            out[k] = v
        elif k == "reversible":
            if not isinstance(v, bool):
                raise PlanError(400, "reversible must be true or false")
            out[k] = v
        elif k == "planned":
            if not isinstance(v, dict):
                raise PlanError(400, "planned must be {day, start, end} or null")
            p = {"day": _day(v.get("day"), "planned day"), "start": _hm(v.get("start"), "planned start"),
                 "end": _hm(v.get("end"), "planned end")}
            out[k] = p
        else:
            out[k] = store.normalise(v)
    if not out.get("area"):
        raise PlanError(400, "task requires an area")
    if not str(out.get("title") or "").strip():
        raise PlanError(400, "task requires a title")
    out.setdefault("state", "todo")
    out.setdefault("owner", "me")
    out.setdefault("reversible", True)
    if stored is None:
        order = ["id", "area", "title", "state", "owner", "estimate_min", "reversible"]
        out = {**{k: out[k] for k in order if k in out}, **{k: v for k, v in out.items() if k not in order}}
    return out


def h_task(ctx: Ctx, body: dict):
    req = body.get("task")
    if not isinstance(req, dict):
        raise PlanError(400, "task must be an object")
    tasks = ctx.t("tasks")
    tid = str(req.get("id") or "").strip()
    if tid and tid in model.plan_task_ids(ctx.areas):
        raise PlanError(400, f"{tid!r} comes from a project plan.yaml and is read-only here")
    idx = _find(tasks, "id", tid) if tid else None
    if not tid:
        tid = _new_id("t", _ids(tasks))
    stored = tasks[idx] if idx is not None else None
    if stored is not None:
        ctx.check(f"task:{tid}")
    t = _merge_task(ctx, stored, req, tid)
    if idx is None:
        tasks.append(t)
    else:
        tasks[idx] = t
    ctx.touched[f"task:{tid}"] = t
    return t


def h_task_state(ctx: Ctx, body: dict):
    tid, state = _need(body, "id"), body.get("state")
    if state not in TASK_STATES:
        raise PlanError(400, f"state must be one of {', '.join(TASK_STATES)}")
    if tid in model.plan_task_ids(ctx.areas):
        raise PlanError(400, f"{tid!r} comes from a project plan.yaml and is read-only here")
    tasks = ctx.t("tasks")
    idx = _find(tasks, "id", tid)
    if idx is None:
        raise PlanError(404, f"unknown task: {tid!r}")
    ctx.check(f"task:{tid}")
    t = dict(tasks[idx])
    t["state"] = state
    tasks[idx] = t
    ctx.touched[f"task:{tid}"] = t
    return t


# ── placing a task on a timeline ───────────────────────────────────────────────────────────────
def _agents_busy_reason(ctx: Ctx, day: str, start: str, end: str, exclude_task: str | None) -> str | None:
    """Refuse when MAX_AGENT_LANES agents' items (actual runs and planned slots) already overlap
    somewhere inside [start, end) on `day`."""
    d = dt.date.fromisoformat(day)
    own = [t for t in (ctx.tdoc.get("tasks") or []) if isinstance(t, dict) and t.get("id") and t["id"] != exclude_task]
    lanes = model.agents_lanes(d, d, own, ctx.areas, model._auto_runs(day, day))
    spans = [(model._min(i["start"]), model._min(i["end"])) for ln in lanes for i in ln["items"]
             if i["day"] == day and i.get("task_id") != exclude_task]
    s0, e0 = model._min(start), model._min(end)
    cuts = sorted({x for sp in spans for x in sp})
    full = []                                   # merged stretches where the cap is already reached
    for a, b in zip(cuts, cuts[1:]):
        if sum(1 for x, y in spans if x < b and y > a) >= model.MAX_AGENT_LANES:
            if full and full[-1][1] == a:
                full[-1][1] = b
            else:
                full.append([a, b])
    for a, b in full:
        if a < e0 and b > s0:
            return f"{model.MAX_AGENT_LANES} agents already busy {model._hhmm(a)}-{model._hhmm(b)}"
    return None


def h_task_place(ctx: Ctx, body: dict):
    """target "me": an event in the calendar (title, area, task_of; the client chooses the slot from the
    estimate); target "agents": the task's `planned` slot on an agents' lane (no automatic trigger)."""
    tid = _need(body, "id")
    target = body.get("target")
    if target not in ("me", "agents"):
        raise PlanError(400, "target must be me or agents")
    day, start, end = _day(body.get("day")), _hm(body.get("start"), "start"), _hm(body.get("end"), "end")
    if model._min(end) <= model._min(start):
        raise PlanError(400, f"end ({end}) must be after start ({start})")
    reason = _window_reason(start, end)
    if reason:
        raise PlanError(400, f"can't place the task: {reason}")
    plan_rows = {r["id"]: (a, r) for a, rows in model.plan_yaml_tasks(ctx.areas).items() for r in rows}
    tasks = idx = stored_t = None
    if tid in plan_rows:
        area, row = plan_rows[tid]
        title, owner = row["title"], row["owner"]
    else:
        tasks = ctx.t("tasks")
        idx = _find(tasks, "id", tid)
        if idx is None:
            raise PlanError(404, f"unknown task: {tid!r}")
        ctx.check(f"task:{tid}")
        stored_t = tasks[idx]
        area, title, owner = stored_t.get("area"), stored_t.get("title"), stored_t.get("owner") or "me"
        if stored_t.get("state") == "done":
            raise PlanError(400, f"can't place {title!r}: a finished task can't be planned")
    slot = {"day": day, "start": start, "end": end}
    if target == "agents":
        if stored_t is None:
            raise PlanError(400, f"{tid!r} comes from a project plan.yaml and is read-only here: its slot can't be saved")
        if owner != "agents":
            raise PlanError(400, "only an agents' task goes on an agents' lane")
        reason = _agents_busy_reason(ctx, day, start, end, tid)
        if reason:
            raise PlanError(400, f"can't place {title!r}: {reason}")
        t = dict(stored_t)
        t["planned"] = slot
        tasks[idx] = t
        ctx.touched[f"task:{tid}"] = t
        return t
    events = ctx.s("events")
    eidx = next((i for i, e in enumerate(events) if isinstance(e, dict) and str(e.get("task_of")) == tid), None)
    stored = events[eidx] if eidx is not None else None
    eid = stored["id"] if stored else _new_id("evt", _ids(events))
    if stored is not None:
        ctx.check(f"evt:{eid}")
        req = dict(slot)
    else:
        req = {"title": title, "type": "personal", "task_of": tid, **slot}
        if area in ctx.area_ids:
            req["area"] = area
    ev = _merge_event(ctx, stored, req, eid)
    _event_guard(ctx, ev, stored)
    if eidx is None:
        events.append(ev)
    else:
        events[eidx] = ev
    ctx.touched[f"evt:{eid}"] = ev
    if stored_t is not None:
        t = dict(stored_t)
        t["planned"] = slot
        tasks[idx] = t
        ctx.touched[f"task:{tid}"] = t
        ctx.extra["task"] = t
    return ev


# ── review tray ────────────────────────────────────────────────────────────────────────────────
def _tray_item(ctx: Ctx, rid: str):
    if not rid.startswith("rv_") or len(rid) <= 3:
        raise PlanError(400, "id must be rv_<task_id>")
    tid = rid[3:]
    tray = model.review_tray(ctx.runs, ctx.tdoc.get("reviews") or [], ctx.sched.get("events") or [])
    item = next((t for t in tray if t["task_id"] == tid), None)
    return tid, item


def h_review_schedule(ctx: Ctx, body: dict):
    tid, item = _tray_item(ctx, _need(body, "id"))
    if item is None:
        raise PlanError(404, f"no pending review {body['id']!r}")
    ctx.check(f"rv:{tid}")
    day, start, end = _day(body.get("day")), _hm(body.get("start"), "start"), _hm(body.get("end"), "end")
    events = ctx.s("events")
    idx = next((i for i, e in enumerate(events) if isinstance(e, dict) and str(e.get("review_of")) == str(tid)), None)
    stored = events[idx] if idx is not None else None
    eid = stored["id"] if stored else _new_id("evt", _ids(events))
    if stored is not None:
        ctx.check(f"evt:{eid}")
    req = {"title": f"Review: {item['title']}", "day": day, "start": start, "end": end,
           "type": "personal", "review_of": tid}
    if item.get("area") in ctx.area_ids:
        req["area"] = item["area"]
    ev = _merge_event(ctx, stored, req, eid)
    _event_guard(ctx, ev, stored)
    if idx is None:
        events.append(ev)
    else:
        events[idx] = ev
    ctx.touched[f"evt:{eid}"] = ev
    _, after = _tray_item(ctx, f"rv_{tid}")
    ctx.touched[f"rv:{tid}"] = model.tray_objects([after])[f"rv:{tid}"]
    ctx.extra["tray_item"] = after
    return ev


def h_review_done(ctx: Ctx, body: dict):
    tid, item = _tray_item(ctx, _need(body, "id"))
    reviews = ctx.t("reviews")
    idx = _find(reviews, "task_id", tid)
    if idx is not None:                       # idempotent
        return reviews[idx]
    if item is None:
        raise PlanError(404, f"no pending review {body['id']!r}")
    ctx.check(f"rv:{tid}")
    rec = {"task_id": tid, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    reviews.append(rec)
    ctx.touched[f"rv:{tid}"] = rec
    return rec


_ROUTES = {
    "/api/v2/plan/event": ("sched", h_event, False),
    "/api/v2/plan/event/delete": ("sched", h_event_delete, False),
    "/api/v2/plan/event/move": ("sched", h_event_move, False),
    "/api/v2/plan/event/skip": ("sched", h_event_skip, False),
    "/api/v2/plan/event/done": ("sched", h_event_done, False),
    "/api/v2/plan/placement/move": ("sched", h_placement_move, False),
    "/api/v2/plan/placement/delete": ("sched", h_placement_delete, False),
    "/api/v2/plan/placement/add": ("sched", h_placement_add, False),
    "/api/v2/plan/block": ("sched", h_block, False),
    "/api/v2/plan/block/delete": ("sched", h_block_delete, False),
    "/api/v2/plan/block/move": ("sched", h_block_move, False),
    "/api/v2/plan/task": ("tasks", h_task, False),
    "/api/v2/plan/task/state": ("tasks", h_task_state, False),
    "/api/v2/plan/task/place": ("both", h_task_place, False),
    "/api/v2/plan/review/schedule": ("sched", h_review_schedule, True),
    "/api/v2/plan/review/done": ("tasks", h_review_done, True),
}
