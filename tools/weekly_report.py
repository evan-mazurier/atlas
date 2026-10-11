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

    events = store.read_events(day_from=(mon - dt.timedelta(days=1)).isoformat(),
                               day_to=(sun + dt.timedelta(days=1)).isoformat())
    wk_events = [e for e in events if (d := stats._local_date(e.get("t"))) and mon <= d <= sun]

    time_area, time_type = {}, {}
    use_area, use_type, use_model, use_tier, use_helper = {}, {}, {}, {}, {}
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

    # every cost event of the week (open tasks included), the same figure as the Ledger KPI
    cost_total = sum(w for d, _h, _t, w in stats._cost_events(events) if mon <= d <= sun)

    enforced, would = stats._refusals(ts)
    hist = {"0": 0, "1": 0, "2+": 0}
    for t in ts:
        n = (t.get("rounds") or {}).get("needs_changes", 0)
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

    playbooks: dict[str, int] = {}
    skills: dict[str, int] = {}
    for e in wk_events:
        if e.get("ev") == "playbook" and e.get("cmd"):
            _add(playbooks, e["cmd"], 1)
        elif e.get("ev") == "prompt" and e.get("cmd"):
            _add(skills, e["cmd"], 1)

    autos = [r for r in query.auto_runs(mon.isoformat(), sun.isoformat())
             if (d := stats._local_date(r.get("end") or r.get("start"))) and mon <= d <= sun]

    return {
        "week": label, "from": mon.isoformat(), "to": sun.isoformat(),
        "tasks": {
            "closed": len(ts), "done": sum(1 for t in ts if t["status"] == "done"),
            "by_status": _rank_count(ts, "status"), "by_src": _rank_count(ts, "src"),
            "first_pass_ready_pct": stats._first_pass(ts), "checked": sum(1 for t in ts if t.get("checked")),
        },
        "time_hours": {"total": round(sum(time_area.values()), 2),
                       "by_area": _rank(time_area, 2), "by_type": _rank(time_type, 2)},
        "usage_weighted": {"total_task_cost": int(sum(use_area.values())), "total_cost_events": int(cost_total),
                           "by_area": _rank(use_area), "by_type": _rank(use_type),
                           "by_model": _rank(use_model), "by_tier": _rank(use_tier), "by_helper": _rank(use_helper)},
        "review": {"needs_changes_rounds": sum((t.get("rounds") or {}).get("needs_changes", 0) for t in ts),
                   "tasks_by_rounds": hist, "tasks_with_verdicts": len(with_v),
                   "caught": sum(t.get("caught") or 0 for t in ts)},
        "refusals": {"enforced": enforced, "would": would},
        "unverified": [t["id"] for t in ts if t.get("unverified_flag")],
        "waste": {
            "chief_helper_sized_work": chief_work, "undeclared_tasks": undeclared, "reruns": reruns,
            "cache_reuse_pct": cache if cache is not None else CACHE_NA,
            "cache_reuse_pct_prev_week": cache_prev if cache_prev is not None else CACHE_NA,
        },
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


def numbers_md(r: dict) -> str:
    t, u, rv, w = r["tasks"], r["usage_weighted"], r["review"], r["waste"]
    fp = t["first_pass_ready_pct"]
    L = [
        N_START, "",
        "## Numbers", "",
        f"Week {r['week']} ({r['from']} to {r['to']}). Tasks counted when they closed in the week; "
        "usage is weighted (price-weighted tokens), never money.", "",
        "### Tasks", "",
        f"- Closed: {t['closed']} (done: {t['done']}); first-pass READY: {'n/a' if fp is None else f'{fp} %'}; "
        f"checked at the final version: {t['checked']}",
        f"- Automatic sessions: {r['auto']['runs']}, estimated review time {r['auto']['review_min']} min", "",
        "### Time by area (hours)", "", _table(["Area", "Hours"], _kv(r["time_hours"]["by_area"], "", lambda v: f"{v:.2f}")),
        "### Time by type (hours)", "", _table(["Type", "Hours"], _kv(r["time_hours"]["by_type"], "", lambda v: f"{v:.2f}")),
        f"### Weighted usage (tasks {u['total_task_cost']:,}; all cost events in the week {u['total_cost_events']:,})", "",
        "By area", "", _table(["Area", "Weighted"], _kv(u["by_area"])),
        "By type", "", _table(["Type", "Weighted"], _kv(u["by_type"])),
        "By model", "", _table(["Model", "Weighted"], _kv(u["by_model"])),
        "By helper", "", _table(["Helper", "Weighted"], _kv(u["by_helper"])),
        "### Review and gates", "",
        f"- NEEDS CHANGES rounds: {rv['needs_changes_rounds']}; tasks by rounds (0 / 1 / 2+): "
        f"{rv['tasks_by_rounds']['0']} / {rv['tasks_by_rounds']['1']} / {rv['tasks_by_rounds']['2+']}",
        f"- Findings caught by the gates: {rv['caught']}",
        f"- Budget refusals: {r['refusals']['enforced']} enforced, {r['refusals']['would']} would-have (record mode)",
        f"- Unverified tasks (edited, never checked): {len(r['unverified'])}"
        + (f" ({', '.join(r['unverified'])})" if r["unverified"] else ""), "",
        "### Waste signals", "",
        f"- Chief doing helper-sized work (main-session edits on tasks that had builders available): "
        f"{len(w['chief_helper_sized_work'])} task(s)"
        + "".join(f"\n  - {c['task']} ({c['type']}, {c['area']}): {c['main_edits']} main-session edits"
                  for c in w["chief_helper_sized_work"]),
        f"- Tasks with no declared type (`undeclared`): {len(w['undeclared_tasks'])}"
        + (f" ({', '.join(w['undeclared_tasks'])})" if w["undeclared_tasks"] else ""),
        f"- Reruns (same area, type and title closed more than once): {len(w['reruns'])}"
        + "".join(f"\n  - {x['type']} / {x['title']}: {len(x['tasks'])} tasks" for x in w["reruns"]),
        f"- Cache-reuse ratio: {w['cache_reuse_pct']}"
        + ("" if w["cache_reuse_pct"] == CACHE_NA else " %") + f" (previous week: {w['cache_reuse_pct_prev_week']}"
        + ("" if w["cache_reuse_pct_prev_week"] == CACHE_NA else " %") + ")", "",
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
