"""Daily check: an end-of-day summary of the work done and the health of Atlas. Facts only, no advice
(lessons and suggestions are the weekly report's job).

    py -3.12 tools/daily_check.py [--day YYYY-MM-DD] [--json]

Prints markdown (or JSON with --json) to stdout. Read-only: writes no file and nothing to the ledger.

Part 1, the day: one row per task with activity on the day (same selection as the Ledger day view, `query.tasks`
over the one day; a task still open must also have an event of its session on that day, so a stale open task does not
show on every later day). Task = the label the Ledger screen shows (linked Plan title, else the task's own name),
clipped to 50 characters. Active time is the task's full `duration_s` from derive.py (active time over the whole
task, not only the part on the day: the per-day split is not kept by the derivation). Cost = price-weighted tokens
(Sonnet = 1), the figure the Ledger and the Today panel show. Result follows `verdictParts` in
cockpit/web2/screens/ledger-model.js.

Part 2, Atlas health: one line per check (red, amber, green; red first). All green prints `All green.`.

Environment: ATLAS_DAILY_SERVER_URL overrides the cockpit URL probed for the server check (empty = skip the check).
The ledger directory follows ATLAS_LEDGER2_DIR like the rest of Atlas.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))

from ledger2 import query, stats, store  # noqa: E402

LABEL_MAX = 50
ANCHOR_HOUR = 21
DEFAULT_SERVER_URL = "http://127.0.0.1:8765/api/v2/areas"
GUARDS = ("rework", "publish", "config", "done_lock", "quick_question")
ORDER = {"red": 0, "amber": 1, "green": 2}


# ---------------------------------------------------------------- formatting

def fmt_active(secs) -> str:
    s = int(secs or 0)
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    return f"{round(s / 60)} min"


def fmt_cost(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{round(n / 1000)}k"
    return str(n)


def clip(s: str, n: int = LABEL_MAX) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


# ---------------------------------------------------------------- the day

def _plan_titles(tasks: list[dict]) -> dict:
    refs = {t.get("plan_ref") for t in tasks if t.get("plan_ref")}
    if not refs:
        return {}
    try:
        from plan import refs as plan_refs
        return plan_refs.resolve_many(refs) or {}
    except Exception:  # noqa: BLE001
        return {}


def task_label(t: dict, plans: dict | None = None) -> str:
    """The Ledger screen's name for the task (ledger-model.js taskCellParts): the Plan title when linked, else
    taskTitle: title, summary goal, first helper label, session title, id."""
    pt = ((plans or {}).get(t.get("plan_ref")) or {}).get("title") if t.get("plan_ref") else None
    helpers = t.get("helpers") or []
    first = (helpers[0].get("labels") or [None])[0] if helpers else None
    name = pt or t.get("title") or (t.get("summary") or {}).get("goal") or first or t.get("session_title") or t.get("id")
    return clip(name)


def result_of(t: dict) -> str:
    """Same priority as verdictParts in ledger-model.js."""
    real = [r for r in (t.get("refusals") or []) if r.get("would") is False and not r.get("notice")]
    refused = next((r for r in real if r.get("guard") == "rework"), real[0] if real else None)
    fv = t.get("final_verdict")
    if refused and fv != "READY":
        return f"refused ({refused.get('guard') or 'guard'})"
    if t.get("unverified_flag"):
        return "closed, unverified"
    if fv == "READY":
        return "READY, closed" if t.get("status") == "done" else "READY, open"
    if fv == "NEEDS_CHANGES":
        return "NEEDS CHANGES"
    if fv == "ESCALATE":
        return "ESCALATED"
    return "open, unchecked" if t.get("status") == "open" else "closed"


def _local(s):
    return stats._local_date(s)


def day_events(day: dt.date) -> list[dict]:
    evs = store.read_events(day_from=(day - dt.timedelta(days=1)).isoformat(),
                            day_to=(day + dt.timedelta(days=1)).isoformat())
    return [e for e in evs if _local(e.get("t")) == day]


def day_tasks(day: dt.date, events: list[dict]) -> list[dict]:
    query.invalidate()
    out = []
    for t in query.tasks(day.isoformat(), day.isoformat(), limit=0):
        if not t.get("ended") and t.get("status") == "open":
            started = t.get("started") or ""
            active = _local(started) == day or any(
                e.get("sid") and e.get("sid") == t.get("sid") and (e.get("t") or "") >= started for e in events)
            if not active:
                continue
        out.append(t)
    out.sort(key=lambda t: t.get("started") or "")
    return out


def area_names() -> dict:
    return {a["id"]: (a.get("name") or a.get("short") or a["id"]) for a in query.load_areas()}


def build_rows(tasks: list[dict]) -> list[dict]:
    plans, names = _plan_titles(tasks), area_names()
    rows = []
    for t in tasks:
        a = t.get("area")
        cost = int((t.get("cost") or {}).get("weighted") or 0)
        secs = int(t.get("duration_s") or 0)
        rows.append({
            "id": t["id"], "started": t.get("started"), "task": task_label(t, plans),
            "area": names.get(a, a) if a else "unassigned", "type": t.get("type") or "undeclared",
            "active_s": secs, "active": fmt_active(secs), "cost": cost, "cost_fmt": fmt_cost(cost),
            "result": result_of(t),
        })
    return rows


def totals(rows: list[dict]) -> dict:
    by_area: dict[str, int] = {}
    for r in rows:
        by_area[r["area"]] = by_area.get(r["area"], 0) + r["active_s"]
    top = max(rows, key=lambda r: r["cost"], default=None)
    return {
        "tasks": len(rows),
        "active_by_area": [{"area": a, "active_s": s, "active": fmt_active(s)}
                           for a, s in sorted(by_area.items(), key=lambda x: -x[1])],
        "cost": sum(r["cost"] for r in rows), "cost_fmt": fmt_cost(sum(r["cost"] for r in rows)),
        "most_expensive": {"task": top["task"], "cost": top["cost"], "cost_fmt": top["cost_fmt"]} if top and top["cost"] else None,
    }


# ---------------------------------------------------------------- health

def _line(name, status, fact):
    return {"check": name, "status": status, "fact": fact}


def check_chain(integ: dict) -> dict:
    if integ.get("intact"):
        return _line("Hash chain", "green", "Intact" if integ.get("status") in (None, "intact") else str(integ["status"]).capitalize())
    n = sum(1 for b in integ.get("breaks") or [] if not b.get("acknowledged"))
    anchor_bad = bool((integ.get("anchor") or {}).get("mismatches"))
    return _line("Hash chain", "red", f"Broken: {n} break(s) not acknowledged" + (", anchor mismatch" if anchor_bad else ""))


def check_bypass(events: list[dict]) -> dict:
    n = sum(1 for e in events if e.get("ev") == "bypass_attempt")
    return _line("Bypass attempts", "red" if n else "green", f"{n} on the day")


def real_refusals(events: list[dict]) -> dict:
    """Enforced refusals per guard (record-mode would-refusals and carry-over notices are not counted)."""
    c = {g: 0 for g in GUARDS}
    would = 0
    for e in events:
        if e.get("ev") != "refusal" or str(e.get("detail") or "").startswith("carry-over"):
            continue
        if e.get("would"):
            would += 1
            continue
        g = e.get("guard")
        if g in c:
            c[g] += 1
    c["_would"] = would
    return c


def check_refusals(events: list[dict]) -> list[dict]:
    c = real_refusals(events)
    out = []
    for g in ("rework", "done_lock"):
        if c[g]:
            out.append(_line(f"Refusals: {g}", "amber", f"{c[g]} on the day"))
    info = [f"{g} {c[g]}" for g in ("publish", "config", "quick_question") if c[g]]
    if c["_would"]:
        info.append(f"would-have (record mode) {c['_would']}")
    if info:
        out.append(_line("Refusals: normal flow", "green", ", ".join(info)))
    if not out:
        out.append(_line("Refusals", "green", "none on the day"))
    return out


def check_send_backs(tasks: list[dict], day: dt.date) -> dict:
    hot = []
    for t in tasks:
        n = sum(1 for v in t.get("verdicts") or [] if v.get("verdict") == "NEEDS_CHANGES" and _local(v.get("t")) == day)
        if n >= 3:
            hot.append(f"{task_label(t)} ({n})")
    if hot:
        return _line("Send-backs", "amber", "3 or more in one task: " + "; ".join(hot))
    return _line("Send-backs", "green", "none at 3 or more in one task")


def check_unverified(tasks: list[dict], day: dt.date) -> dict:
    bad = [task_label(t) for t in tasks if t.get("unverified_flag") and t.get("ended") and _local(t["ended"]) == day]
    if bad:
        return _line("Closed without a passing review", "amber", f"{len(bad)}: " + "; ".join(bad))
    return _line("Closed without a passing review", "green", "0")


def open_at_end(tasks: list[dict], day: dt.date) -> list[dict]:
    return [t for t in tasks if not t.get("ended") or (_local(t["ended"]) or day) > day]


def check_open(tasks: list[dict], day: dt.date, is_today: bool) -> dict:
    op = open_at_end(tasks, day)
    when = "now" if is_today else "at the end of the day"
    if op:
        return _line("Open tasks", "amber", f"{len(op)} still open {when}: " + "; ".join(task_label(t) for t in op))
    return _line("Open tasks", "green", f"0 open {when}")


def check_hook_errors(day: dt.date) -> dict:
    n = 0
    try:
        text = (store.ledger_dir() / "hook-errors.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for ln in text.splitlines():
        if _local(ln.split(" ", 1)[0]) == day:
            n += 1
    return _line("Hook errors", "amber" if n else "green", f"{n} on the day")


def check_anchor(anchor: dict, day: dt.date, now: dt.datetime) -> dict:
    if not (anchor.get("configured") or anchor.get("expected")):
        return _line("Anchor", "green", "not set up")
    if anchor.get("missing"):
        return _line("Anchor", "amber", "outside anchor missing")
    last = anchor.get("last_anchored_day")
    if last and str(last) >= day.isoformat():
        return _line("Anchor", "green", f"ran (last anchored day {last})")
    due = day < now.date() or (day == now.date() and now.hour >= ANCHOR_HOUR)
    if due:
        return _line("Anchor", "amber", f"did not run (last anchored day {last or 'never'})")
    return _line("Anchor", "green", f"not due yet (runs at {ANCHOR_HOUR}:00)")


def check_server(day: dt.date, now: dt.datetime) -> dict | None:
    url = os.environ.get("ATLAS_DAILY_SERVER_URL", DEFAULT_SERVER_URL)
    if not url or day != now.date():
        return None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=1) as r:
            ok = 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        ok = False
    return _line("Cockpit server", "green", "up") if ok else _line("Cockpit server", "red", "down (no answer within 1 s)")


def health(day: dt.date, tasks: list[dict], events: list[dict], now: dt.datetime) -> list[dict]:
    integ = stats.integrity()
    lines = [check_chain(integ), check_bypass(events), *check_refusals(events), check_send_backs(tasks, day),
             check_unverified(tasks, day), check_open(tasks, day, day == now.date()), check_hook_errors(day),
             check_anchor(integ.get("anchor") or {}, day, now)]
    srv = check_server(day, now)
    if srv:
        lines.append(srv)
    return sorted(lines, key=lambda x: ORDER[x["status"]])      # stable: check order kept inside a status


# ---------------------------------------------------------------- build and render

def compute(day: dt.date | None = None, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now().astimezone()
    day = day or now.date()
    events = day_events(day)
    tasks = day_tasks(day, events)
    rows = build_rows(tasks)
    h = health(day, tasks, events, now)
    return {"day": day.isoformat(), "rows": rows, "totals": totals(rows), "health": h,
            "all_green": all(x["status"] == "green" for x in h)}


def _cell(s) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def render(r: dict) -> str:
    L = [f"# Daily check {r['day']}", "", "## The day", ""]
    if r["rows"]:
        L += ["| Task | Area | Type | Active time | Cost | Result |", "| --- | --- | --- | --- | --- | --- |"]
        L += [f"| {_cell(x['task'])} | {_cell(x['area'])} | {_cell(x['type'])} | {x['active']} | {x['cost_fmt']} | {x['result']} |"
              for x in r["rows"]]
    else:
        L.append("No task worked.")
    t = r["totals"]
    top = t["most_expensive"]
    L += ["", f"Totals: {t['tasks']} task(s); active "
          + (", ".join(f"{a['area']} {a['active']}" for a in t["active_by_area"]) or "none")
          + f"; cost {t['cost_fmt']}"
          + (f"; most expensive: {top['task']} ({top['cost_fmt']})" if top else ""), "", "## Atlas health", ""]
    if r["all_green"]:
        L.append("All green.")
    else:
        L += [f"- {x['status']}: {x['check']}: {x['fact']}" for x in r["health"]]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", help="local day YYYY-MM-DD (default: today)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    a = ap.parse_args(argv)
    try:
        day = dt.date.fromisoformat(a.day) if a.day else None
    except ValueError:
        print("daily_check: --day must look like 2026-10-13", file=sys.stderr)
        return 2
    r = compute(day)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(render(r), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
