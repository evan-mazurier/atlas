"""Ledger v2 stats payload (contracts.md §5.2). Cached; recomputed in a background thread when > 60 s old."""
from __future__ import annotations

import datetime as dt
import threading
import time

try:
    from . import anchor, bypass_report, derive, ledger_chain, query, store
except ImportError:  # pragma: no cover
    import anchor  # type: ignore
    import bypass_report  # type: ignore
    import derive  # type: ignore
    import ledger_chain  # type: ignore
    import query  # type: ignore
    import store  # type: ignore

TTL_S = 60
_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}
_running: set[str] = set()


def week_label(d: dt.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_monday(label: str | None) -> dt.date:
    if not label:
        t = dt.date.today()
        return t - dt.timedelta(days=t.weekday())
    y, w = label.upper().split("-W")
    return dt.date.fromisocalendar(int(y), int(w), 1)


def _mon(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _local_date(s) -> dt.date | None:
    d = derive.parse_t(s)
    return d.astimezone().date() if d else None


def _closed(tasks):
    out = []
    for t in tasks:
        if t.get("status") == "open" or not t.get("ended"):
            continue
        d = _local_date(t["ended"])
        if d:
            out.append((d, t))
    return out


def _first_pass(ts):
    with_v = [t for t in ts if t.get("verdicts")]
    if not with_v:
        return None
    ok = sum(1 for t in with_v if t["verdicts"][0]["verdict"] == "READY")
    return round(100.0 * ok / len(with_v), 1)


def _in_week(closed, mon):
    end = mon + dt.timedelta(days=6)
    return [t for d, t in closed if mon <= d <= end]


def _refusals(ts):
    enforced = would = 0
    for t in ts:
        for r in t.get("refusals") or []:
            if r.get("guard") != "rework" or r.get("notice"):
                continue
            if r.get("would"):
                would += 1
            else:
                enforced += 1
    return enforced, would


def _kpi_values(closed, cost_by_week, mon):
    ts = _in_week(closed, mon)
    enforced, would = _refusals(ts)
    return {
        "tasks_closed": len(ts),
        "first_pass_ready_pct": _first_pass(ts),
        "caught": sum(t.get("caught") or 0 for t in ts),
        "rework_refused": enforced + would,
        "weighted_usage": int(cost_by_week.get(mon, 0)),
        "_split": {"enforced": enforced, "would": would},
    }


def _cost_events(events):
    helper_of = {}
    for e in events:
        if e.get("ev") == "helper_paired" and e.get("agent_id"):
            helper_of[e["agent_id"]] = e.get("helper")
    out = []
    for e in events:
        if e.get("ev") != "cost":
            continue
        d = _local_date(e.get("t"))
        if d is None:
            continue
        scope = e.get("scope") or "main"
        helper = "chief" if scope == "main" else (e.get("helper") or helper_of.get(scope) or "unknown")
        out.append((d, helper, store.tier(e.get("model")), float(e.get("weighted") or 0)))
    return out


_integrity_cache: dict = {}


def integrity() -> dict:
    """Hash-chain check of every day file (contracts §6.4 rule A), cached until a day file changes."""
    anchor.ensure_fresh()                       # background fetch when the cached remote result is stale
    key = (str(store.ledger_dir()), store.signature(), anchor.state_key())
    with _lock:
        hit = _integrity_cache.get("v")
    if hit and hit[0] == key:
        return hit[1]
    res = ledger_chain.build(store.ledger_dir(), anchor.status)
    with _lock:
        _integrity_cache["v"] = (key, res)
    return res


def _usage_by_type(events, wf, mon, wk_end):
    """Weighted tokens spent in [mon, wk_end] per declared type and per playbook (cost attributed to the
    session's most recently opened task, as derive does); cost with no task is Undeclared."""
    types = wf.get("types") or {}
    by_sid: dict = {}
    for e in events:
        if e.get("v") == 1 or e.get("src") == "v1":
            continue
        if e.get("sid"):
            by_sid.setdefault(e["sid"], []).append(e)
    acc: dict[str, dict] = {}
    for sid, evs in by_sid.items():
        cur = None                       # (key, label, kind, task id)
        for e in evs:
            ev = e.get("ev")
            if ev in ("work_type", "playbook") and e.get("task"):
                if ev == "playbook":
                    cmd = str(e.get("cmd") or "").lstrip("/")
                    cur = ("playbook:" + cmd, "/" + cmd, "playbook", e["task"])
                else:
                    typ = e.get("type") or "undeclared"
                    kind = "undeclared" if typ == "undeclared" else "type"
                    cur = (typ, (types.get(typ) or {}).get("label", typ), kind, e["task"])
            elif ev == "cost":
                d = _local_date(e.get("t"))
                if d is None or not (mon <= d <= wk_end):
                    continue
                if float(e.get("weighted") or 0) <= 0:
                    continue
                key, label, kind, tid = cur or ("undeclared", "Undeclared", "undeclared", "sid:" + sid)
                a = acc.setdefault(key, {"key": key, "label": label, "kind": kind, "weighted": 0.0, "_tasks": set()})
                a["weighted"] += float(e.get("weighted") or 0)
                a["_tasks"].add(tid)
    out = [{"key": a["key"], "label": a["label"], "kind": a["kind"], "weighted": int(a["weighted"]), "tasks": len(a["_tasks"])}
           for a in acc.values() if int(a["weighted"]) > 0]
    out.sort(key=lambda x: (x["kind"] == "undeclared", -x["weighted"]))
    return out


def _bypass(events, mon, wk_end, wf=None):
    wf = wf if wf is not None else query.load_workflows()
    by_sid: dict = {}
    notes: dict = {}
    for e in events:
        if e.get("ev") == "bypass_note" and e.get("ref"):
            notes.setdefault(str(e["ref"]), []).append(e)
        if e.get("sid") and e.get("v") != 1 and e.get("src") != "v1":
            by_sid.setdefault(e["sid"], []).append(e)
    rows = []
    for e in events:
        if e.get("ev") != "bypass_attempt":
            continue
        d = _local_date(e.get("t"))
        if d is not None and mon <= d <= wk_end:
            rows.append({"t": e.get("t"), "tool": e.get("tool"), "matched": e.get("matched"),
                         "denied": bool(e.get("denied", True)), "_e": e})
    rows.sort(key=lambda r: str(r["t"]), reverse=True)
    total, blocked = len(rows), sum(1 for r in rows if r["denied"])
    recent = rows[:20]
    for r in recent:
        e = r.pop("_e")
        key = bypass_report.attempt_key(e)
        sess = by_sid.get(e.get("sid")) or [e]
        idx = next((i for i, x in enumerate(sess) if x is e), None)
        if idx is None:
            sess, idx = [e], 0
        r["key"] = key
        r["report"] = bypass_report.build_report(e, sess, idx, wf, notes.get(key))
    return {"count": total, "blocked": blocked, "recent": recent}


def compute(week: str | None = None) -> dict:
    mon = week_monday(week)
    label = week_label(mon)
    wf = query.load_workflows()
    tasks = query.all_tasks()
    closed = _closed(tasks)
    since = (mon - dt.timedelta(weeks=12)).isoformat()
    events = store.read_events(day_from=since)
    costs = _cost_events(events)
    cost_by_week: dict[dt.date, float] = {}
    for d, _h, _t, w in costs:
        cost_by_week[_mon(d)] = cost_by_week.get(_mon(d), 0) + w

    kpis = {}
    keys = ("tasks_closed", "first_pass_ready_pct", "caught", "rework_refused", "weighted_usage")
    series = [_kpi_values(closed, cost_by_week, mon - dt.timedelta(weeks=11 - i)) for i in range(12)]
    prev = _kpi_values(closed, cost_by_week, mon - dt.timedelta(weeks=1))
    cur = series[-1]
    for k in keys:
        kpis[k] = {"value": cur[k], "prev": prev[k], "spark": [s[k] for s in series]}
    kpis["rework_refused"]["split"] = cur["_split"]

    end_day = min(dt.date.today(), mon + dt.timedelta(days=6))
    days = [end_day - dt.timedelta(days=13 - i) for i in range(14)]
    ubd = {d: {"day": d.isoformat(), "opus": 0, "sonnet": 0, "haiku": 0} for d in days}
    for d, _h, tr, w in costs:
        if d in ubd:
            ubd[d][tr] += int(w)

    wk_end = mon + dt.timedelta(days=6)
    area_secs: dict[str, int] = {}
    for t in _in_week(closed, mon):
        a = t.get("area") or "unassigned"
        area_secs[a] = area_secs.get(a, 0) + int(t.get("duration_s") or 0)
    time_by_area = sorted(({"area": a, "hours": round(s / 3600, 2)} for a, s in area_secs.items()),
                          key=lambda x: -x["hours"])

    hist = {"0": 0, "1": 0, "2": 0, "refused": 0}
    for d, t in closed:
        if not (mon - dt.timedelta(weeks=3) <= d <= wk_end):
            continue
        if any(r.get("guard") == "rework" and not r.get("notice") for r in t.get("refusals") or []):
            hist["refused"] += 1
        else:
            hist[str(min(2, (t.get("rounds") or {}).get("needs_changes", 0)))] += 1

    by_helper: dict[tuple, float] = {}
    for d, h, tr, w in costs:
        if mon <= d <= wk_end:
            by_helper[(h, tr)] = by_helper.get((h, tr), 0) + w
    usage_by_helper = sorted(({"helper": h, "tier": tr, "weighted": int(w)} for (h, tr), w in by_helper.items()),
                             key=lambda x: -x["weighted"])

    fpw = []
    for i in range(8):
        m = mon - dt.timedelta(weeks=7 - i)
        fpw.append({"week": week_label(m), "pct": _first_pass(_in_week(closed, m))})

    four = [t for d, t in closed if mon - dt.timedelta(weeks=3) <= d <= wk_end]
    ladder = []
    for name, info in (wf.get("types") or {}).items():
        ts = [t for t in four if t.get("type") == name]
        ladder.append({"type": name, "label": info.get("label", name), "rung": info.get("rung"),
                       "budget": info.get("budget"), "tasks_4w": len(ts),
                       "first_pass_pct": _first_pass(ts), "move": info.get("move")})

    return {
        "ok": True, "week": label, "kpis": kpis,
        "usage_by_day": [ubd[d] for d in days], "time_by_area": time_by_area, "rounds_hist": hist,
        "usage_by_helper": usage_by_helper, "first_pass_by_week": fpw, "ladder": ladder,
        "unverified": sum(1 for t in four if t.get("unverified_flag")),
        "usage_by_type": _usage_by_type(events, wf, mon, wk_end),
        "bypass": _bypass(events, mon, wk_end, wf),
        "integrity": integrity(),
    }


def _refresh(key: str, week: str | None) -> None:
    try:
        payload = compute(week)
        with _lock:
            _cache[key] = (time.time(), payload)
    finally:
        with _lock:
            _running.discard(key)


def get_stats(week: str | None = None) -> dict:
    """Cached payload. First call computes inline; later calls serve the cache and refresh it in a
    background thread once it is older than 60 s."""
    key = f"{store.ledger_dir()}|{week_label(week_monday(week))}"
    with _lock:
        hit = _cache.get(key)
        stale = hit is not None and time.time() - hit[0] > TTL_S
        start = stale and key not in _running
        if start:
            _running.add(key)
    if hit is None:
        payload = compute(week)
        with _lock:
            _cache[key] = (time.time(), payload)
        return payload
    if start:
        threading.Thread(target=_refresh, args=(key, week), daemon=True).start()
    return hit[1]


def clear_cache() -> None:
    with _lock:
        _cache.clear()
