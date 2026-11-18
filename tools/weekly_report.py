"""Weekly report: the objective numbers (scope.html "Weekly report"; contracts.md §5.2).

    py -3.12 tools/weekly_report.py [--week YYYY-Www] [--out DIR]

Reads the ledger (read-only) through cockpit/ledger2 and writes `<out>/YYYY-Www.md` (numbers only,
narrative sections left as marked placeholders for the /weekly-report skill) plus `YYYY-Www.json`.
Re-running refreshes only the numbers block: a narrative the skill already wrote is kept.
Default out dir: ATLAS_REPORTS_DIR or ~/.atlas/reports.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))

from ledger2 import derive, query, stats, store  # noqa: E402

N_START, N_END = "<!-- numbers:start -->", "<!-- numbers:end -->"
PLACEHOLDER = "<!-- FILL: /weekly-report writes this section -->"
CACHE_NA = "not measured"
BASELINE_DAY = "2026-10-12"          # first day of W42: ledger data before it is build noise (the comparison starts here)
ADVISOR = "advisor"
ADVISOR_KINDS = ("scope", "unstick", "design", "unlabelled")
# task id -> reason: left out of every advisor trial figure (consult counts, cost and label split still include it)
ADVISOR_TRIAL_EXCLUDED = {"2bc16666-3": "Gate 0 test consult, 2026-11-13"}


def default_out() -> Path:
    return Path(os.environ.get("ATLAS_REPORTS_DIR") or os.path.join(os.path.expanduser("~"), ".atlas", "reports"))


def _add(d: dict, k, v) -> None:
    d[k] = d.get(k, 0) + v


def _rank(d: dict, nd: int = 0) -> list[dict]:
    return [{"key": k, "value": round(v, nd) if nd else int(v)} for k, v in sorted(d.items(), key=lambda x: -x[1])]


def _cache_ratio(tasks: list[dict]):
    cr = tot = 0
    for t in tasks:
        for m in (t.get("cost") or {}).get("by_model", {}).values():
            cr += int(m.get("cache_read") or 0)
            tot += int(m.get("cache_read") or 0) + int(m.get("in") or 0) + int(m.get("cache_write") or 0)
    return round(100.0 * cr / tot, 1) if tot else None


def _edit_times(events: list[dict], builders: set):
    """Edit times per task: (builder edits, main-session edits). Agent ids map to helpers via the pairing events."""
    agent_helper: dict[str, str] = {}
    for e in events:
        if e.get("ev") in ("helper_paired", "helper_end") and e.get("agent_id") and e.get("helper"):
            agent_helper[e["agent_id"]] = e["helper"]
    builder_times: dict[str, list] = {}
    main_times: dict[str, list] = {}
    for e in events:
        if e.get("ev") != "edit" or not e.get("task"):
            continue
        when = derive.parse_t(e.get("t"))
        if when is None:
            continue
        aid = e.get("agent_id")
        if not aid:
            main_times.setdefault(e["task"], []).append(when)
        elif agent_helper.get(aid) in builders:
            builder_times.setdefault(e["task"], []).append(when)
    return builder_times, main_times


def _hidden_rework(ts: list[dict], events: list[dict], builders: set) -> list[str]:
    """Ids of tasks with verdicts where a main-session edit fell after the first builder edit and before the first verdict."""
    builder_times, main_times = _edit_times(events, builders)
    out = []
    for t in ts:
        if not t.get("verdicts") or t["id"] not in builder_times:
            continue
        fv = derive.parse_t(t["verdicts"][0].get("t"))
        if fv is None:
            continue
        first_builder = min(builder_times[t["id"]])
        if any(first_builder < m < fv for m in main_times.get(t["id"], [])):
            out.append(t["id"])
    return out


BUILT_BY = ("orchestrator only", "helper only", "both", "no file edits")


def _rate_rows(counts: dict) -> list[dict]:
    rows = [{"key": k, "ready": r, "reviewed": n, "pct": round(100.0 * r / n, 1)} for k, (r, n) in counts.items() if n]
    rows.sort(key=lambda x: (-x["reviewed"], x["key"]))
    return rows


def _first_pass_breakdown(ts: list[dict], events: list[dict], builders: set):
    """First-pass READY per type and per who built (edits strictly before the first verdict)."""
    builder_times, main_times = _edit_times(events, builders)
    by_type: dict[str, list] = {}
    by_built: dict[str, list] = {}
    for t in ts:
        if not t.get("verdicts"):
            continue
        ready = 1 if t["verdicts"][0]["verdict"] == "READY" else 0
        c = by_type.setdefault(t.get("type") or "undeclared", [0, 0])
        c[0] += ready
        c[1] += 1
        fv = derive.parse_t(t["verdicts"][0].get("t"))
        has_m = fv is not None and any(m < fv for m in main_times.get(t["id"], []))
        has_b = fv is not None and any(b < fv for b in builder_times.get(t["id"], []))
        key = BUILT_BY[2] if has_m and has_b else BUILT_BY[0] if has_m else BUILT_BY[1] if has_b else BUILT_BY[3]
        c = by_built.setdefault(key, [0, 0])
        c[0] += ready
        c[1] += 1
    return _rate_rows(by_type), _rate_rows(by_built)


def _senior_after_routine_ready(ts: list[dict]) -> list[str]:
    """Task ids where a senior-reviewer returned NEEDS CHANGES after a routine reviewer (any other reviewer) had
    returned READY on the same task. Read off the task's verdicts in ledger order; late verdicts are not this task's."""
    out = []
    for t in ts:
        routine_ready = False
        for v in t.get("verdicts") or []:
            if v.get("late"):
                continue
            senior = str(v.get("helper") or "").strip().lower() == "senior-reviewer"
            if not senior and v.get("verdict") == "READY":
                routine_ready = True
            elif senior and v.get("verdict") == "NEEDS_CHANGES" and routine_ready:
                out.append(t["id"])
                break
    return out


def _strict_first_pass(ts: list[dict], hidden: list[str]):
    with_v = [t for t in ts if t.get("verdicts")]
    if not with_v:
        return None
    ok = sum(1 for t in with_v if t["verdicts"][0]["verdict"] == "READY" and t["id"] not in hidden)
    return round(100.0 * ok / len(with_v), 1)


def _mean(xs: list) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


def _label_kind(label) -> str:
    low = str(label or "").strip().lower()
    for k in ADVISOR_KINDS[:3]:
        if low.startswith(k + ":"):
            return k
    return "unlabelled"


def _advisor(events: list[dict], mon: dt.date, sun: dt.date, closed_ids: set | None = None) -> dict:
    """The advisor trial numbers (spec design notes, not in this copy, section 12). `events` are the raw events
    from the ledger baseline to the end of the week; counts and cost are for the week, the troubled-against-troubled
    comparison covers every task since the baseline up to the end of the week (a task already in trouble: it had a
    reviewer round). Order within a task is the event time (file order breaks ties), because the task-level verdict
    list and `rounds.own` carry no order against a consult. `closed_ids`: only closed tasks enter the comparison
    (None = all tasks count as closed); a consulted task still open is counted apart. A consult needs a helper_paired."""
    is_closed = (lambda tid: True) if closed_ids is None else (lambda tid: tid in closed_ids)
    def when(e):
        return derive.parse_t(e.get("t"))

    seq = sorted(((when(e), i, e) for i, e in enumerate(events) if when(e) is not None), key=lambda x: (x[0], x[1]))
    rounds: dict[str, list] = {}         # task -> [(t, i)] of rounds (the hook's `round` field, or the legacy rule)
    consults: dict[str, list] = {}       # task -> [(t, i, event)] of advisor spawns
    legacy_seen: dict[str, set] = {}     # task -> failing versions already counted (verdicts with no `round` field)
    # a consult counts only once the spawn happened: a helper_start whose spawn another hook then denied has no helper_paired
    paired = {e.get("tuid") for _t, _i, e in seq if e.get("ev") == "helper_paired" and e.get("tuid")}
    for t, i, e in seq:
        tid = e.get("task")
        if not tid:
            continue
        if e.get("ev") == "verdict" and e.get("verdict") == "NEEDS_CHANGES" and not e.get("counted_on"):
            if "round" in e:
                if e["round"]:
                    rounds.setdefault(tid, []).append((t, i))
            else:                       # legacy (contracts §1.2, same rule as derive.py): one round per distinct failing version
                ver = e.get("version")
                key = int(ver) if ver is not None else "?" + str(e.get("agent_id"))
                if key not in legacy_seen.setdefault(tid, set()):
                    legacy_seen[tid].add(key)
                    rounds.setdefault(tid, []).append((t, i))
        elif e.get("ev") == "helper_start" and e.get("helper") == ADVISOR and e.get("tuid") in paired:
            consults.setdefault(tid, []).append((t, i, e))

    in_week = lambda e: (d := stats._local_date(e.get("t"))) is not None and mon <= d <= sun
    wk_consults = [c for cs in consults.values() for c in cs if in_week(c[2])]
    kinds = {k: {"count": 0, "weighted": 0} for k in ADVISOR_KINDS}
    # labels come from every helper_start since the baseline: a consult started last week still has its cost this week
    label_of = {c[2].get("tuid"): c[2].get("label") for cs in consults.values() for c in cs}
    helper_of = {e.get("agent_id"): e.get("helper") for _t, _i, e in seq if e.get("ev") == "helper_paired" and e.get("agent_id")}
    tuid_of = {e.get("agent_id"): e.get("tuid") for _t, _i, e in seq if e.get("ev") == "helper_paired" and e.get("agent_id")}
    for c in wk_consults:
        kinds[_label_kind(c[2].get("label"))]["count"] += 1
    total_w = 0.0
    for e in events:
        if e.get("ev") != "cost" or not in_week(e):
            continue
        scope = e.get("scope") or "main"
        helper = e.get("helper") or helper_of.get(scope)
        if scope == "main" or helper != ADVISOR:
            continue
        w = float(e.get("weighted") or 0)
        total_w += w
        kinds[_label_kind(label_of.get(tuid_of.get(scope)))]["weighted"] += w

    consult_tasks, consult_group, other_group = [], [], []
    for tid, cs in consults.items():
        first = cs[0]
        before = sum(1 for r in rounds.get(tid, []) if (r[0], r[1]) < (first[0], first[1]))
        after = sum(1 for r in rounds.get(tid, []) if (r[0], r[1]) > (first[0], first[1]))
        row = {"task": tid, "consults": len(cs), "rounds_before_first": before, "rounds_after_first": after,
               "in_week": any(in_week(c[2]) for c in cs), "open": not is_closed(tid)}
        consult_tasks.append(row)
        if tid in ADVISOR_TRIAL_EXCLUDED:
            row["excluded"] = True
            continue
        if row["open"]:
            continue
        (consult_group if before >= 1 else other_group).append(row)
    open_consult = sum(1 for r in consult_tasks if r["open"] and not r.get("excluded"))
    excluded = [{"task": r["task"], "reason": ADVISOR_TRIAL_EXCLUDED[r["task"]]}
                for r in sorted(consult_tasks, key=lambda r: r["task"]) if r.get("excluded") and r["in_week"]]
    # matched on k = rounds before the first consult: a no-consult task that reached k rounds, outcome = rounds after its k-th
    by_k, pooled_c, pooled_x, pooled_n = [], 0.0, 0.0, 0
    comp_ids: set = set()
    for k in sorted({r["rounds_before_first"] for r in consult_group}):
        cons = [r["rounds_after_first"] for r in consult_group if r["rounds_before_first"] == k]
        comp_t = {tid: len(rs) - k for tid, rs in rounds.items() if tid not in consults and is_closed(tid) and len(rs) >= k}
        comp = list(comp_t.values())
        by_k.append({"k": k, "consult_tasks": len(cons), "consult_mean_after": _mean(cons),
                     "comparison_tasks": len(comp), "comparison_mean_after": _mean(comp)})
        if comp:
            pooled_n += len(cons)
            pooled_c += sum(cons)
            pooled_x += len(cons) * sum(comp) / len(comp)
            comp_ids |= set(comp_t)
    pooled = {"consult_tasks": pooled_n, "consult_mean_after": round(pooled_c / pooled_n, 2) if pooled_n else None,
              "comparison_mean_after": round(pooled_x / pooled_n, 2) if pooled_n else None}
    wk_tasks = {c[2].get("task") for c in wk_consults}
    return {
        "consults": len(wk_consults), "weighted_fable_cost": int(total_w), "tasks_with_consult": len(wk_tasks),
        "consults_per_task": round(len(wk_consults) / len(wk_tasks), 2) if wk_tasks else None,
        "by_kind": [{"kind": k, "count": kinds[k]["count"], "weighted": int(kinds[k]["weighted"])} for k in ADVISOR_KINDS],
        "since": BASELINE_DAY,
        "excluded_from_trial": excluded,
        # matched figures: the consult tasks whose k has a comparison (pooled); the rest is reported apart, descriptive only
        "consult_group": {"tasks": pooled_n, "mean_rounds_after": pooled["consult_mean_after"]},
        "unmatched_consult_tasks": len(consult_group) - pooled_n,
        "consult_tasks_open": open_consult,
        "comparison_group": {"tasks": len(comp_ids), "mean_rounds_after": pooled["comparison_mean_after"]},
        "matched": {"by_k": by_k, "pooled": pooled},
        "other_consult_tasks": {"tasks": len(other_group), "mean_rounds_after": _mean([r["rounds_after_first"] for r in other_group])},
        "consult_tasks": sorted(consult_tasks, key=lambda r: r["task"]),
    }


MAIN_EDIT_STOP = 6                              # mirrors hooks/atlas_hook.py (read-only copy; the hooks are frozen)
STOP_RESET = ("implementer", "drafter")         # mirrors the hook's BUILDER_RESET
STOP_REVIEWERS = ("reviewer", "senior-reviewer")


def _has_builder(task_type, playbook_cmd, wf: dict) -> bool:
    """Mirror of the hook's crew_has_builder: False only when the type's crew is a list with no implementer/drafter.
    `undeclared` counts as having a builder. A playbook uses its own crew (playbook_crews by command, taken from the
    task's `playbook` event); if that command is not in the events or the workflows data, a builder is assumed."""
    if task_type == "undeclared":
        return True
    spec = (wf.get("types") or {}).get(task_type)
    if task_type == "playbook":
        pcs = wf.get("playbook_crews") or {}
        spec = pcs.get(playbook_cmd) if playbook_cmd else None
        if not isinstance(spec, dict):
            return True
    crew = spec.get("crew") if isinstance(spec, dict) else None
    if isinstance(crew, list):
        return bool({str(x).lower() for x in crew} & set(STOP_RESET))
    return True


def _edit_runs(events: list[dict], ids: set, task_types: dict | None = None, wf: dict | None = None) -> dict:
    """Longest run of main-session edits between builder launches, per task. The start of the task counts as a reset.
    Main-session = an `edit` event without `agent_id` (same test as the helper-sized-work signal). Memory files never
    reach here: the hook emits no `edit` event for them (memory_path is exempt). Like the hook's 6-edit stop, only tasks
    whose type's crew has a builder are counted (see _has_builder) when task_types and wf are given."""
    pb_cmd = {e["task"]: e.get("cmd") for e in events if e.get("ev") == "playbook" and e.get("task")}
    cur: dict[str, int] = {}
    best: dict[str, int] = {}
    for e in events:
        tid = e.get("task")
        if tid not in ids:
            continue
        if task_types is not None and not _has_builder(task_types.get(tid), pb_cmd.get(tid), wf or {}):
            continue
        if e.get("ev") == "edit" and not e.get("agent_id"):
            cur[tid] = cur.get(tid, 0) + 1
            best[tid] = max(best.get(tid, 0), cur[tid])
        elif e.get("ev") == "helper_start" and e.get("helper") in STOP_RESET:
            cur[tid] = 0
    return best


def _test_runner_reminders(events: list[dict], ids: set, task_types: dict) -> dict:
    """Derived (the reminder emits no event): on a coding task, a reviewer launched after an implementer returned with no
    test-runner launch since that return. One reminder per such reviewer launch (the state only changes on a
    test-runner launch or the next implementer return). Returns {task: count}."""
    armed: dict[str, bool] = {}
    out: dict[str, int] = {}
    for e in events:
        tid = e.get("task")
        if tid not in ids or task_types.get(tid) != "coding":
            continue
        ev, h = e.get("ev"), e.get("helper")
        if ev == "helper_end" and h == "implementer":
            armed[tid] = True
        elif ev == "helper_start" and h == "test-runner":
            armed[tid] = False
        elif ev == "helper_start" and h in STOP_REVIEWERS and armed.get(tid):
            out[tid] = out.get(tid, 0) + 1      # stateless like the hook: every reviewer launch while armed counts
    return out


def _rework_concentration(ts: list[dict]) -> dict:
    """NEEDS CHANGES rounds per closed task (same set and same `own` count as review.needs_changes_rounds), and how much
    of it one session holds. Session = task id up to the last `-`."""
    per_sess: dict[str, list] = {}
    for t in ts:
        rr = t.get("rounds") or {}
        n = rr.get("own", rr.get("needs_changes", 0))
        a = per_sess.setdefault(str(t["id"]).rsplit("-", 1)[0], [0, 0])
        a[0] += n
        a[1] += 1
    rounds = sum(a[0] for a in per_sess.values())
    tasks = len(ts)
    res = {"rounds": rounds, "closed_tasks": tasks,
           "per_task": round(rounds / tasks, 2) if tasks else None,
           "top_session": None, "top_rounds": 0, "top_pct": None, "top_tasks": 0, "per_task_without_top": None}
    if not rounds:
        return res
    top = max(per_sess, key=lambda k: (per_sess[k][0], k))
    tr, tt = per_sess[top]
    res.update({"top_session": top, "top_rounds": tr, "top_tasks": tt, "top_pct": round(100.0 * tr / rounds, 1),
                "per_task_without_top": round((rounds - tr) / (tasks - tt), 2) if tasks > tt else None})
    return res


def compute(week: str | None = None) -> dict:
    mon = stats.week_monday(week)
    sun = mon + dt.timedelta(days=6)
    label = stats.week_label(mon)
    query.invalidate()
    wf = query.load_workflows()
    types = wf.get("types") or {}
    builders = set(wf.get("builders") or [])
    all_t = query.all_tasks()
    closed = stats._closed(all_t)
    ts = stats._in_week(closed, mon)
    prev_ts = stats._in_week(closed, mon - dt.timedelta(weeks=1))
    ids = {t["id"] for t in ts}

    # tasks closed this week may have started earlier: read from the earliest local start date among them
    starts = [d - dt.timedelta(days=1) for t in ts if (d := stats._local_date(t.get("started")))]
    ev_from = min([mon - dt.timedelta(days=1)] + starts)
    events = store.read_events(day_from=ev_from.isoformat(),
                               day_to=(sun + dt.timedelta(days=1)).isoformat())
    wk_events = [e for e in events if (d := stats._local_date(e.get("t"))) and mon <= d <= sun]

    hidden = _hidden_rework(ts, events, builders)
    strict_pct = _strict_first_pass(ts, hidden)
    fp_type, fp_built = _first_pass_breakdown(ts, events, builders)
    senior_catch = _senior_after_routine_ready(ts)

    time_area, time_type = {}, {}
    use_area, use_type, use_model, use_tier, use_helper = {}, {}, {}, {}, {}
    use_hat: dict = {}
    senior_tasks = []
    for t in ts:
        a, ty = t.get("area") or "unassigned", t.get("type") or "undeclared"
        _add(time_area, a, (t.get("duration_s") or 0) / 3600)
        _add(time_type, ty, (t.get("duration_s") or 0) / 3600)
        c = t.get("cost") or {}
        _add(use_area, a, c.get("weighted") or 0)
        _add(use_type, ty, c.get("weighted") or 0)
        for m, v in (c.get("by_model") or {}).items():
            _add(use_model, m, v.get("weighted") or 0)
            _add(use_tier, store.tier(m), v.get("weighted") or 0)
        for h, w in (c.get("by_helper") or {}).items():
            _add(use_helper, h, w)
            _add(use_hat, (h, a, ty), w)
        sw = (c.get("by_helper") or {}).get("senior-reviewer") or 0
        if sw > 0:
            senior_tasks.append({"id": t["id"], "area": a, "type": ty, "weighted": int(sw)})
    by_helper_area_type = [{"helper": h, "area": a, "type": ty, "weighted": int(w)}
                           for (h, a, ty), w in sorted(use_hat.items(), key=lambda x: -x[1]) if w > 0]
    senior_tasks.sort(key=lambda x: -x["weighted"])
    senior_top_tasks = senior_tasks[:5]

    # every cost event of the week (open tasks included), the same figure as the Ledger KPI
    cost_total = sum(w for d, _h, _t, w in stats._cost_events(events) if mon <= d <= sun)

    enforced, would = stats._refusals(ts)
    hist = {"0": 0, "1": 0, "2+": 0}
    for t in ts:
        rr = t.get("rounds") or {}
        n = rr.get("own", rr.get("needs_changes", 0))          # own rounds: a carried count belongs to the task that earned it
        hist["2+" if n >= 2 else str(n)] += 1
    with_v = [t for t in ts if t.get("verdicts")]

    # waste signals
    main_edits: dict[str, int] = {}
    for e in wk_events:
        if e.get("ev") == "edit" and not e.get("agent_id") and e.get("task") in ids:
            _add(main_edits, e["task"], 1)
    by_id = {t["id"]: t for t in ts}
    chief_work = []
    for tid, n in main_edits.items():
        t = by_id[tid]
        crew = set((types.get(t["type"]) or {}).get("crew") or [])
        if crew & builders:
            chief_work.append({"task": tid, "type": t["type"], "area": t.get("area"), "main_edits": n})
    chief_work.sort(key=lambda x: -x["main_edits"])
    undeclared = [t["id"] for t in ts if t.get("type") == "undeclared"]
    groups: dict[tuple, list] = {}
    for t in ts:
        key = (t.get("area"), t.get("type"), (t.get("title") or t.get("session_title") or "").strip().lower())
        if key[2]:
            groups.setdefault(key, []).append(t["id"])
    reruns = [{"area": k[0], "type": k[1], "title": k[2], "tasks": v} for k, v in groups.items() if len(v) > 1]
    cache, cache_prev = _cache_ratio(ts), _cache_ratio(prev_ts)
    runs = _edit_runs(events, ids, {t["id"]: t.get("type") for t in ts}, wf)
    over = sorted(k for k, v in runs.items() if v > MAIN_EDIT_STOP)
    top_run = max(runs.items(), key=lambda x: (x[1], x[0])) if runs else None
    stop_enf = sum(1 for t in ts for x in t.get("refusals") or [] if x.get("guard") == "edit_limit" and not x.get("would"))
    stop_would = sum(1 for t in ts for x in t.get("refusals") or [] if x.get("guard") == "edit_limit" and x.get("would"))
    rem = _test_runner_reminders(events, ids, {t["id"]: t.get("type") for t in ts})

    playbooks: dict[str, int] = {}
    skills: dict[str, int] = {}
    for e in wk_events:
        if e.get("ev") == "playbook" and e.get("cmd"):
            _add(playbooks, e["cmd"], 1)
        elif e.get("ev") == "prompt" and e.get("cmd"):
            _add(skills, e["cmd"], 1)

    adv_events = (store.read_events(day_from=BASELINE_DAY, day_to=(sun + dt.timedelta(days=1)).isoformat())
                  if BASELINE_DAY <= sun.isoformat() else [])
    # the extra day is read only for the local-date conversion; nothing after the report week counts for the Advisor
    adv_events = [e for e in adv_events if not ((d := stats._local_date(e.get("t"))) and d > sun)]
    adv_closed = {t["id"] for t in derive.derive_v2(adv_events, wf) if t.get("status") != "open"}
    advisor = _advisor(adv_events, mon, sun, adv_closed)
    consulted = {r["task"] for r in advisor["consult_tasks"]}

    autos = [r for r in query.auto_runs(mon.isoformat(), sun.isoformat())
             if (d := stats._local_date(r.get("end") or r.get("start"))) and mon <= d <= sun]

    return {
        "week": label, "from": mon.isoformat(), "to": sun.isoformat(),
        "tasks": {
            "closed": len(ts), "done": sum(1 for t in ts if t["status"] == "done"),
            "by_status": _rank_count(ts, "status"), "by_src": _rank_count(ts, "src"),
            "first_pass_ready_pct": stats._first_pass(ts), "first_pass_ready_strict_pct": strict_pct, "checked": sum(1 for t in ts if t.get("checked")),
            "with_consult": [t["id"] for t in ts if t["id"] in consulted],
        },
        "time_hours": {"total": round(sum(time_area.values()), 2),
                       "by_area": _rank(time_area, 2), "by_type": _rank(time_type, 2)},
        "usage_weighted": {"total_task_cost": int(sum(use_area.values())), "total_cost_events": int(cost_total),
                           "by_area": _rank(use_area), "by_type": _rank(use_type),
                           "by_model": _rank(use_model), "by_tier": _rank(use_tier), "by_helper": _rank(use_helper),
                           "by_helper_area_type": by_helper_area_type, "senior_top_tasks": senior_top_tasks},
        "review": {"needs_changes_rounds": sum((t.get("rounds") or {}).get("own", (t.get("rounds") or {}).get("needs_changes", 0))
                                                 for t in ts),
                   "abandoned": sum(1 for t in ts if t.get("status") == "abandoned"),
                   "tasks_by_rounds": hist, "tasks_with_verdicts": len(with_v),
                   "caught": sum(t.get("caught") or 0 for t in ts), "hidden_rework_tasks": hidden,
                   "senior_after_routine_ready": senior_catch,
                   "rework_concentration": _rework_concentration(ts),
                   "first_pass_by_type": fp_type, "first_pass_by_builder": fp_built},
        "refusals": {"enforced": enforced, "would": would},
        "unverified": [t["id"] for t in ts if t.get("unverified_flag")],
        "waste": {
            "chief_helper_sized_work": chief_work, "undeclared_tasks": undeclared, "reruns": reruns,
            "main_edit_run": {"max": top_run[1] if top_run else 0, "max_task": top_run[0] if top_run else None,
                              "stop": MAIN_EDIT_STOP, "tasks_above_stop": over},
            "edit_limit_refusals": {"enforced": stop_enf, "would": stop_would},
            "test_runner_reminders": {"total": sum(rem.values()), "tasks": sorted(rem)},
            "cache_reuse_pct": cache if cache is not None else CACHE_NA,
            "cache_reuse_pct_prev_week": cache_prev if cache_prev is not None else CACHE_NA,
        },
        "advisor": advisor,
        "playbooks": _rank(playbooks), "skills": _rank(skills),
        "auto": {"runs": len(autos), "review_min": sum(int(r.get("review_min") or 0) for r in autos),
                 "titles": [r["title"] for r in autos]},
    }


def _rank_count(ts: list[dict], field: str) -> list[dict]:
    c: dict = {}
    for t in ts:
        _add(c, t.get(field) or "none", 1)
    return _rank(c)


# ---------------------------------------------------------------- markdown


def _table(head: list[str], rows: list[list]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def _kv(items: list[dict], unit: str = "", fmt=lambda v: f"{v:,}") -> list[list]:
    return [[i["key"], fmt(i["value"]) + unit] for i in items]


def _fp_rows(items: list[dict]) -> list[list]:
    return [[i["key"], i["reviewed"], i["ready"], f"{i['pct']} %"] for i in items]


def _na(v) -> str:
    return "n/a" if v is None else str(v)


def _advisor_md(a: dict, with_consult: list) -> list:
    def grp(g):
        return f"{g['tasks']} task(s), mean rounds after: {'n/a' if g['mean_rounds_after'] is None else g['mean_rounds_after']}"
    ex = a.get("excluded_from_trial") or []
    return [
        "### Advisor", "",
        f"- Consults this week: {a['consults']}; weighted Fable cost: {a['weighted_fable_cost']:,}; "
        f"tasks with a consult: {a['tasks_with_consult']}; consults per such task: "
        f"{'n/a' if a['consults_per_task'] is None else a['consults_per_task']}",
        f"- Tasks closed this week that had a consult (judge the W44 changes without them): {len(with_consult)}"
        + (f" ({', '.join(with_consult)})" if with_consult else ""),
        f"- Consult group (closed tasks, k >= 1 rounds before the first consult, k has a comparison; rounds after it), since {a['since']}: {grp(a['consult_group'])}",
        f"- Consult tasks with k >= 1 but no comparison (unmatched, description only): {a['unmatched_consult_tasks']}",
        f"- Consult tasks still open (not yet compared): {a['consult_tasks_open']}",
        *([f"- Excluded from the trial: {len(ex)} ({'; '.join(x['task'] + ': ' + x['reason'] for x in ex)})"] if ex else []),
        f"- Comparison group (no consult, reached k rounds; rounds after the k-th, weighted by the consult tasks' k), since {a['since']}: {grp(a['comparison_group'])}",
        f"- Pooled over the k with a comparison: consult mean {_na(a['matched']['pooled']['consult_mean_after'])} "
        f"(n={a['matched']['pooled']['consult_tasks']}), comparison mean {_na(a['matched']['pooled']['comparison_mean_after'])}",
        f"- Other consult tasks (no round before the first consult; description only, never compared): {grp(a['other_consult_tasks'])}",
        "", _table(["k (rounds before consult)", "Consult tasks", "Mean after", "Comparison tasks", "Mean after"],
                   [[x["k"], x["consult_tasks"], _na(x["consult_mean_after"]), x["comparison_tasks"], _na(x["comparison_mean_after"])]
                    for x in a["matched"]["by_k"]]),
        _table(["Kind", "Consults", "Weighted"], [[k["kind"], k["count"], f"{k['weighted']:,}"] for k in a["by_kind"]]),
        _table(["Task with a consult", "Consults", "Rounds before first", "Rounds after first"],
               [[x["task"], x["consults"], x["rounds_before_first"], x["rounds_after_first"]]
                for x in a["consult_tasks"] if x["in_week"]]),
    ]


def _rework_md(c: dict) -> str:
    if not c["rounds"]:
        return "- Rework per closed task: n/a (no NEEDS CHANGES rounds)"
    wo = "n/a" if c["per_task_without_top"] is None else c["per_task_without_top"]
    return (f"- Rework per closed task: {c['per_task']} ({c['rounds']}/{c['closed_tasks']}); top session {c['top_session']} "
            f"has {c['top_rounds']} of {c['rounds']} rounds ({c['top_pct']} %); without it: {wo} "
            f"({c['rounds'] - c['top_rounds']} / {c['closed_tasks'] - c['top_tasks']})")


def numbers_md(r: dict) -> str:
    t, u, rv, w = r["tasks"], r["usage_weighted"], r["review"], r["waste"]
    fp = t["first_pass_ready_pct"]
    sfp = t["first_pass_ready_strict_pct"]
    hid = rv["hidden_rework_tasks"]
    L = [
        N_START, "",
        "## Numbers", "",
        f"Week {r['week']} ({r['from']} to {r['to']}). Tasks counted when they closed in the week; "
        "usage is weighted (price-weighted tokens), never money.", "",
        "### Tasks", "",
        f"- Closed: {t['closed']} (done: {t['done']}); first-pass READY: {'n/a' if fp is None else f'{fp} %'}; "
        f"checked at the final version: {t['checked']}",
        f"- First-pass READY, strict (no main-session edits on a builder's work before the first verdict): "
        f"{'n/a' if sfp is None else f'{sfp} %'}; hidden rework: {len(hid)} task(s)"
        + (f" ({', '.join(hid)})" if hid else ""),
        f"- Automatic sessions: {r['auto']['runs']}, estimated review time {r['auto']['review_min']} min", "",
        "### Time by area (hours)", "", _table(["Area", "Hours"], _kv(r["time_hours"]["by_area"], "", lambda v: f"{v:.2f}")),
        "### Time by type (hours)", "", _table(["Type", "Hours"], _kv(r["time_hours"]["by_type"], "", lambda v: f"{v:.2f}")),
        f"### Weighted usage (tasks {u['total_task_cost']:,}; all cost events in the week {u['total_cost_events']:,})", "",
        "By area", "", _table(["Area", "Weighted"], _kv(u["by_area"])),
        "By type", "", _table(["Type", "Weighted"], _kv(u["by_type"])),
        "By model", "", _table(["Model", "Weighted"], _kv(u["by_model"])),
        "By helper", "", _table(["Helper", "Weighted"], _kv(u["by_helper"])),
        "By helper, area and type", "",
        _table(["Helper", "Area", "Type", "Weighted"],
               [[x["helper"], x["area"], x["type"], f"{x['weighted']:,}"] for x in u["by_helper_area_type"]]),
        "Most expensive senior-reviewer tasks", "",
        _table(["Task", "Area", "Type", "Weighted"],
               [[x["id"], x["area"], x["type"], f"{x['weighted']:,}"] for x in u["senior_top_tasks"]]),
        "### Review and gates", "",
        f"- NEEDS CHANGES rounds: {rv['needs_changes_rounds']}; tasks by rounds (0 / 1 / 2+): "
        f"{rv['tasks_by_rounds']['0']} / {rv['tasks_by_rounds']['1']} / {rv['tasks_by_rounds']['2+']}; "
        f"abandoned with open findings: {rv['abandoned']}",
        _rework_md(rv["rework_concentration"]),
        f"- Findings caught by the gates: {rv['caught']}",
        f"- Senior review caught what the routine review passed (NEEDS CHANGES from senior-reviewer after a READY from "
        f"another reviewer on the same task): {len(rv['senior_after_routine_ready'])} task(s)"
        + (f" ({', '.join(rv['senior_after_routine_ready'])})" if rv["senior_after_routine_ready"] else ""),
        f"- Budget refusals: {r['refusals']['enforced']} enforced, {r['refusals']['would']} would-have (record mode)",
        f"- Unverified tasks (edited, never checked): {len(r['unverified'])}"
        + (f" ({', '.join(r['unverified'])})" if r["unverified"] else ""), "",
        "First-pass READY by type", "",
        _table(["Type", "Reviewed", "READY first time", "%"], _fp_rows(rv["first_pass_by_type"])),
        "First-pass READY by who built (edits before the first review)", "",
        _table(["Built by", "Reviewed", "READY first time", "%"], _fp_rows(rv["first_pass_by_builder"])),
        "### Waste signals", "",
        f"- Orchestrator doing helper-sized work (main-session edits on tasks that had builders available): "
        f"{len(w['chief_helper_sized_work'])} task(s)"
        + "".join(f"\n  - {c['task']} ({c['type']}, {c['area']}): {c['main_edits']} main-session edits"
                  for c in w["chief_helper_sized_work"]),
        f"- Main-session edits since the last builder launch ({w['main_edit_run']['stop']}-edit stop): max "
        f"{w['main_edit_run']['max']}" + (f" ({w['main_edit_run']['max_task']})" if w["main_edit_run"]["max_task"] else "")
        + f"; tasks above {w['main_edit_run']['stop']}: {len(w['main_edit_run']['tasks_above_stop'])}"
        + (f" ({', '.join(w['main_edit_run']['tasks_above_stop'])})" if w["main_edit_run"]["tasks_above_stop"] else ""),
        f"- {w['main_edit_run']['stop']}-edit stop refusals: {w['edit_limit_refusals']['enforced']} enforced, "
        f"{w['edit_limit_refusals']['would']} would-have (record mode)",
        f"- Test-runner reminders (derived: reviewer launched on a coding task after an implementer returned, no "
        f"test-runner since): {w['test_runner_reminders']['total']} across {len(w['test_runner_reminders']['tasks'])} task(s)",
        f"- Tasks with no declared type (`undeclared`): {len(w['undeclared_tasks'])}"
        + (f" ({', '.join(w['undeclared_tasks'])})" if w["undeclared_tasks"] else ""),
        f"- Reruns (same area, type and title closed more than once): {len(w['reruns'])}"
        + "".join(f"\n  - {x['type']} / {x['title']}: {len(x['tasks'])} tasks" for x in w["reruns"]),
        f"- Cache-reuse ratio: {w['cache_reuse_pct']}"
        + ("" if w["cache_reuse_pct"] == CACHE_NA else " %") + f" (previous week: {w['cache_reuse_pct_prev_week']}"
        + ("" if w["cache_reuse_pct_prev_week"] == CACHE_NA else " %") + ")", "",
        *_advisor_md(r["advisor"], t["with_consult"]),
        "### Playbooks and skills used", "",
        _table(["Playbook", "Runs"], _kv(r["playbooks"], "", str)),
        _table(["Slash command", "Uses"], _kv(r["skills"], "", str)),
        N_END,
    ]
    return "\n".join(L)


def narrative_md() -> str:
    parts = ["## Lessons: three observations", "", PLACEHOLDER, "",
             "## Three changes (prompts, skills, routing)", "", PLACEHOLDER, "",
             "## Post ideas (numbers only, never client details; the operator writes the post)", "", PLACEHOLDER, ""]
    return "\n".join(parts)


def render(r: dict, existing: str | None = None) -> str:
    block = numbers_md(r)
    if existing and N_START in existing and N_END in existing:
        a, b = existing.index(N_START), existing.index(N_END) + len(N_END)
        return existing[:a] + block + existing[b:]
    return f"# Weekly report {r['week']}\n\n" + block + "\n\n" + narrative_md()


def write(r: dict, out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / f"{r['week']}.md", out / f"{r['week']}.json"
    prev = md.read_text(encoding="utf-8") if md.exists() else None
    md.write_text(render(r, prev), encoding="utf-8")
    js.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    return md, js


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--week", help="ISO week, e.g. 2026-W41 (default: the current week)")
    ap.add_argument("--out", help="output dir (default: ATLAS_REPORTS_DIR or ~/.atlas/reports)")
    a = ap.parse_args(argv)
    try:
        r = compute(a.week)
    except (ValueError, KeyError) as e:
        print(f"weekly_report: bad --week ({e})", file=sys.stderr)
        return 2
    md, js = write(r, Path(a.out) if a.out else default_out())
    print(json.dumps({"ok": True, "week": r["week"], "md": str(md), "json": str(js),
                      "tasks_closed": r["tasks"]["closed"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
