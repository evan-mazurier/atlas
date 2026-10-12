"""Plan engine read model (contracts.md section 5.3): areas, recurring expansion, tasks, agents lanes,
review tray. Pure functions over already-parsed documents, plus the few cached file reads."""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import yaml

from . import store

_DASH = Path(__file__).resolve().parents[1] / "core"
if str(_DASH) not in sys.path:
    sys.path.append(str(_DASH))
import schedmath  # noqa: E402  (read-only helpers: _occurs_on, collision guards)

TASK_STATES = ("todo", "next", "doing", "review", "done")
OWNERS = ("me", "agents")
EVENT_TYPES = ("appointment", "errand", "personal", "free")
HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
TRAY_DAYS = 30
DAY_START_MIN = 7 * 60          # the Plan day: 07:00 to 22:00 (sleep hours are not planned)
DAY_END_MIN = 22 * 60
MAX_AGENT_LANES = 3
MAX_PARALLEL = 3                # shared (parallel-session) events that may overlap at any instant
ATTENTIONS = ("shared", "full")


# ── areas ──────────────────────────────────────────────────────────────────────────────────────
def areas_path() -> Path:
    return Path(os.environ.get("ATLAS_AREAS_FILE") or store.ROOT / "areas.yaml")


def load_areas() -> list[dict]:
    """The one reader: `ledger2.areas.load_areas`, given ATLAS_AREAS_FILE when set."""
    try:
        from ledger2.areas import load_areas as _la
        return list(_la(os.environ.get("ATLAS_AREAS_FILE") or None) or [])
    except Exception:  # noqa: BLE001
        return []


def project_areas(areas: list[dict]) -> dict:
    out = {}
    for a in areas:
        for p in (a.get("projects") or []):
            out.setdefault(str(p), a["id"])
    return out


def _fold(text: str) -> str:
    """Lower-case, accents removed (Impots matches Invoïce)."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "").lower()) if not unicodedata.combining(c))


def _as_list(v) -> list:
    if isinstance(v, str):
        return [v]
    return [x for x in (v or []) if x] if isinstance(v, (list, tuple)) else []


def event_area(ev: dict, areas: list[dict]) -> tuple[str | None, str]:
    """Explicit area wins; else the first area (file order) whose `match.title_prefix` starts the title;
    else the first area whose `match.keywords` has a whole word anywhere in the title (case and accents
    ignored); else none."""
    explicit = ev.get("area")
    if explicit:
        return str(explicit), "explicit"
    title = _fold(ev.get("title"))
    for a in areas:
        m = a.get("match")
        for p in _as_list(m.get("title_prefix") if isinstance(m, dict) else None):
            if title.startswith(_fold(p)):
                return a["id"], "match"
    for a in areas:
        m = a.get("match")
        for k in _as_list(m.get("keywords") if isinstance(m, dict) else None):
            if re.search(r"(?<!\w)" + re.escape(_fold(k)) + r"(?!\w)", title):
                return a["id"], "match"
    return None, "none"


def area_attention(a: dict | None) -> str:
    v = (a or {}).get("attention")
    return v if v in ATTENTIONS else "full"


def effective_attention(ev: dict, areas: list[dict]) -> tuple[str, str]:
    """(attention, source). The area's value when it is fixed; else the event's own; else the area's;
    else full. Source: fixed | event | area | default. Same area resolution as the read path."""
    aid, _ = event_area(ev, areas)
    a = next((x for x in areas if x.get("id") == aid), None) if aid else None
    if a and a.get("attention_fixed") is True:
        return area_attention(a), "fixed"
    if ev.get("attention") in ATTENTIONS:
        return ev["attention"], "event"
    if a and a.get("attention") in ATTENTIONS:
        return a["attention"], "area"
    return "full", "default"


# ── time helpers ───────────────────────────────────────────────────────────────────────────────
def valid_hhmm(v) -> str | None:
    m = HHMM_RE.match(str(v).strip()) if v is not None else None
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def valid_day(v) -> str | None:
    try:
        return dt.date.fromisoformat(str(v).strip()).isoformat()
    except (ValueError, TypeError):
        return None


def parse_estimate_min(v) -> int | None:
    """'2h' | '2h30' | '45m' | '1.5h' | 90 -> minutes."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v) * 60))          # plan.yaml bare numbers are hours
    s = str(v).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)h(?:\s*(\d{1,2}))?$", s)
    if m:
        return int(round(float(m.group(1)) * 60 + (int(m.group(2)) if m.group(2) else 0)))
    m = re.match(r"^(\d+(?:\.\d+)?)m(?:in)?$", s)
    if m:
        return int(round(float(m.group(1))))
    m = re.match(r"^\d+(?:\.\d+)?$", s)
    return int(round(float(s) * 60)) if m else None


def _min(hhmm) -> int | None:
    s = valid_hhmm(hhmm)
    return int(s[:2]) * 60 + int(s[3:]) if s else None


# ── recurring expansion ────────────────────────────────────────────────────────────────────────
def expand_events(events: list, frm: dt.date, to: dt.date, areas: list[dict]) -> list[dict]:
    out = []
    for ev in events:
        if not isinstance(ev, dict) or not ev.get("id"):
            continue
        day0 = schedmath._parse_day(ev.get("day"))
        if day0 is None or day0 > to:
            continue
        recur = ev.get("recur") if isinstance(ev.get("recur"), dict) and ev.get("recur") else None
        area, source = event_area(ev, areas)
        cur = max(frm, day0) if recur else day0
        last = to if recur else day0
        while cur <= last:
            if frm <= cur <= to and (cur == day0 or schedmath._occurs_on(day0, recur, cur)):
                iso = cur.isoformat()
                skips = ev.get("skips") if isinstance(ev.get("skips"), list) else []
                if iso not in [str(s) for s in skips]:
                    start, end = valid_hhmm(ev.get("start")), valid_hhmm(ev.get("end"))
                    ov = (ev.get("overrides") or {}).get(iso) if isinstance(ev.get("overrides"), dict) else None
                    if isinstance(ov, dict):
                        start, end = valid_hhmm(ov.get("start")) or start, valid_hhmm(ov.get("end")) or end
                    att, att_src = effective_attention(ev, areas)
                    row = {"id": ev["id"], "occurrence": iso, "title": ev.get("title"), "day": iso,
                           "start": start, "end": end, "area": area, "area_source": source,
                           "type": ev.get("type") or "personal", "recur": recur,
                           "notes": ev.get("notes"), "attention": att, "attention_source": att_src}
                    done_on = ev.get("done_on") if isinstance(ev.get("done_on"), list) else []
                    if ev.get("done") is True or iso in [str(d) for d in done_on]:
                        row["done"] = True
                    for k in ("busy", "review_of", "from_series", "location", "remind", "task_of"):
                        if k in ev:
                            row[k] = ev[k]
                    out.append(row)
            cur += dt.timedelta(days=1)
    out.sort(key=lambda r: (r["day"], r["start"] or "", str(r["title"])))
    return out


# ── tasks ──────────────────────────────────────────────────────────────────────────────────────
def task_row(t: dict, source: str = "tasks.yaml", project: str | None = None) -> dict:
    state = t.get("state") if t.get("state") in TASK_STATES else "todo"
    row = {"id": t.get("id"), "title": t.get("title"), "state": state,
           "owner": t.get("owner") if t.get("owner") in OWNERS else "me",
           "source": source, "project": project if project else t.get("project"),
           "estimate_min": t.get("estimate_min"),
           "reversible": t.get("reversible") if isinstance(t.get("reversible"), bool) else True}
    if isinstance(t.get("planned"), dict):
        row["planned"] = t["planned"]
    return row


_plan_cache: dict = {}


def _read_plan_yaml(path: Path) -> list:
    try:
        st = path.stat()
    except OSError:
        return []
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _plan_cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=store._LOADER) or {}
        tasks = [t for t in (doc.get("tasks") or []) if isinstance(t, dict) and t.get("id")]
    except Exception:  # noqa: BLE001 — a broken project plan never breaks the Plan screen
        tasks = []
    _plan_cache[str(path)] = (key, tasks)
    return tasks


def registry_path() -> Path:
    return Path(os.environ.get("ATLAS_REGISTRY_FILE") or store.ROOT / "registry.yaml")


_reg_cache: dict = {}


def _registry_plan_files() -> dict:
    p = registry_path()
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _reg_cache.get("key") == key:
        return _reg_cache["val"]
    try:
        doc = yaml.load(p.read_text(encoding="utf-8"), Loader=store._LOADER) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    val = {str(pr["id"]): str(pr["plan_file"]) for pr in (doc.get("projects") or [])
           if isinstance(pr, dict) and pr.get("id") and pr.get("plan_file")}
    _reg_cache.update(key=key, val=val)
    return val


def registry_plan_tasks():
    """Yield `(project_id, task)` for every task, done ones included, in each registry project's plan.yaml.
    Public reader for other plan modules (uses the cached private readers)."""
    for pid, pfile in _registry_plan_files().items():
        for t in _read_plan_yaml(Path(pfile)):
            yield pid, t


def plan_yaml_tasks(areas: list[dict]) -> dict:
    """area id -> read-only rows from each registry project's plan.yaml whose project maps to an area.
    Done tasks are left out. Row id = `<project>/<task id>` (unique next to tasks.yaml ids)."""
    out: dict = {}
    pa = project_areas(areas)
    for pid, pfile in _registry_plan_files().items():
        area = pa.get(pid)
        if not area:
            continue
        for t in _read_plan_yaml(Path(pfile)):
            if t.get("state") == "done":
                continue
            row = task_row({"id": f"{pid}/{t['id']}", "title": t.get("title"), "state": t.get("state"),
                            "owner": "me" if t.get("type") == "manual" else "agents",
                            "estimate_min": parse_estimate_min(t.get("estimate")),
                            "reversible": True}, "plan.yaml", pid)
            out.setdefault(area, []).append(row)
    return out


def plan_task_ids(areas: list[dict]) -> set:
    return {r["id"] for rows in plan_yaml_tasks(areas).values() for r in rows}


# ── automatic runs, agents lanes, review tray ──────────────────────────────────────────────────
def _auto_runs(day_from: str, day_to: str) -> list[dict]:
    """ledger2.query.auto_runs, import guarded (Lane 1 may not have landed)."""
    try:
        from ledger2.query import auto_runs
        return list(auto_runs(day_from, day_to) or [])
    except Exception:  # noqa: BLE001
        return []


def _iso_parts(v):
    """ISO datetime string -> (date iso, minutes) or None."""
    if not v:
        return None
    try:
        d = dt.datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d.date().isoformat(), d.hour * 60 + d.minute


def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def assign_lanes(items: list[dict]) -> list[dict]:
    """Greedy: items sorted by (day, start) go to the first lane free at that moment, so items in
    one lane never overlap."""
    lanes: list[list] = []          # each lane: [items, last_day, last_end_min]
    for it in sorted(items, key=lambda i: (i["day"], i["start"], i["end"])):
        s, e = _min(it["start"]), _min(it["end"])
        for lane in lanes:
            if lane[1] != it["day"] or lane[2] <= s:
                lane[0].append(it)
                lane[1], lane[2] = it["day"], e
                break
        else:
            lanes.append([[it], it["day"], e])
    return [{"lane": i + 1, "items": l[0]} for i, l in enumerate(lanes)]


def agents_lanes(frm: dt.date, to: dt.date, tasks: list[dict], areas: list[dict], runs: list[dict]) -> list[dict]:
    items = []
    lo, hi = frm.isoformat(), to.isoformat()
    now = dt.datetime.now()
    for r in runs:
        a, b = _iso_parts(r.get("start")), _iso_parts(r.get("end"))
        if not a or not (lo <= a[0] <= hi):
            continue
        end_m = b[1] if b and b[0] == a[0] else (
            now.hour * 60 + now.minute if a[0] == now.date().isoformat() and not b else 23 * 60 + 59)
        end_m = max(end_m, a[1] + 1)
        items.append({"task_id": r.get("task_id"), "title": r.get("title"), "area": r.get("area"),
                      "day": a[0], "start": _hhmm(a[1]), "end": _hhmm(min(end_m, 24 * 60 - 1)),
                      "kind": "actual", "crew": list(r.get("crew") or []),
                      "status": r.get("status")})
    for t in tasks:
        pl = t.get("planned")
        if t.get("owner") != "agents" or t.get("state") == "done" or not isinstance(pl, dict):
            continue
        day, s, e = valid_day(pl.get("day")), valid_hhmm(pl.get("start")), valid_hhmm(pl.get("end"))
        if not (day and s and e and lo <= day <= hi and _min(e) > _min(s)):
            continue
        items.append({"task_id": t.get("id"), "title": t.get("title"), "area": t.get("area"),
                      "day": day, "start": s, "end": e, "kind": "planned",
                      "crew": list(t.get("crew") or []), "status": t.get("state") or "todo"})
    return assign_lanes(items)


def review_tray(runs: list[dict], reviews: list, events: list) -> list[dict]:
    reviewed = {r.get("task_id") for r in reviews if isinstance(r, dict)}
    sched_by_task = {}
    for e in events:
        if isinstance(e, dict) and e.get("review_of") and e.get("id"):
            sched_by_task.setdefault(str(e["review_of"]), e)
    tray = []
    for r in runs:
        tid = r.get("task_id")
        if not tid or r.get("status") != "done" or r.get("verdict") != "READY" or tid in reviewed:
            continue
        ev = sched_by_task.get(str(tid))
        tray.append({"id": f"rv_{tid}", "task_id": tid, "title": r.get("title"), "area": r.get("area"),
                     "review_min": r.get("review_min") if isinstance(r.get("review_min"), int) else 20,
                     "verdict": r.get("verdict"), "ready_at": r.get("end"),
                     "scheduled": ({"event": ev["id"], "day": ev.get("day"), "start": ev.get("start"),
                                    "end": ev.get("end")} if ev else None)})
    tray.sort(key=lambda t: str(t["ready_at"] or ""))
    return tray


def tray_runs() -> list[dict]:
    today = dt.date.today()
    return _auto_runs((today - dt.timedelta(days=TRAY_DAYS)).isoformat(), today.isoformat())


# ── versioned objects ──────────────────────────────────────────────────────────────────────────
def schedule_objects(sched: dict) -> dict:
    out = {}
    for e in sched.get("events") or []:
        if isinstance(e, dict) and e.get("id"):
            out[f"evt:{e['id']}"] = e
    for p in sched.get("placements") or []:
        if isinstance(p, dict) and p.get("session"):
            out[f"plc:{p['session']}"] = p
    for b in sched.get("work_blocks") or []:
        if isinstance(b, dict) and b.get("id"):
            out[f"blk:{b['id']}"] = b
    return out


def tasks_objects(doc: dict) -> dict:
    out = {}
    for t in doc.get("tasks") or []:
        if isinstance(t, dict) and t.get("id"):
            out[f"task:{t['id']}"] = t
    for r in doc.get("reviews") or []:
        if isinstance(r, dict) and r.get("task_id"):
            out[f"rv:{r['task_id']}"] = r
    return out


def tray_objects(tray: list[dict]) -> dict:
    return {f"rv:{t['task_id']}": {k: t[k] for k in ("task_id", "title", "verdict", "ready_at", "scheduled")}
            for t in tray}


# ── the GET payload ────────────────────────────────────────────────────────────────────────────
def build(sched: dict, tdoc: dict, frm: dt.date, to: dt.date, version: int, runs: list[dict],
          tray_src: list[dict] | None = None) -> dict:
    areas = load_areas()
    lo, hi = frm.isoformat(), to.isoformat()
    events = expand_events(sched.get("events") or [], frm, to, areas)
    pa = project_areas(areas)
    placements = [{"session": p.get("session"), "project": p.get("project"), "task": p.get("task"),
                   "day": p.get("day"), "start": p.get("start"), "end": p.get("end"),
                   "area": pa.get(str(p.get("project"))),
                   **({"pinned": True} if p.get("pinned") else {}), **({"done": True} if p.get("done") else {})}
                  for p in (sched.get("placements") or [])
                  if isinstance(p, dict) and lo <= str(p.get("day")) <= hi]
    placements.sort(key=lambda p: (str(p["day"]), str(p["start"])))
    blocks = [{"id": b.get("id"), "day": b.get("day"), "start": b.get("start"), "end": b.get("end"),
               "label": b.get("label")}
              for b in (sched.get("work_blocks") or [])
              if isinstance(b, dict) and lo <= str(b.get("day")) <= hi]
    blocks.sort(key=lambda b: (str(b["day"]), str(b["start"])))
    av = sched.get("availability") if isinstance(sched.get("availability"), dict) else {}
    availability = {"blocks": [b for b in (av.get("blocks") or []) if isinstance(b, dict) and lo <= str(b.get("day")) <= hi],
                    "full_days": [d for d in (av.get("full_days") or []) if lo <= str(d) <= hi]}
    own = [t for t in (tdoc.get("tasks") or []) if isinstance(t, dict) and t.get("id")]
    tasks = {a["id"]: [] for a in areas}
    for t in own:
        tasks.setdefault(t.get("area") or "", []).append(task_row(t))
    for area, rows in plan_yaml_tasks(areas).items():
        tasks.setdefault(area, []).extend(rows)
    linked = {}
    for e in sched.get("events") or []:
        if isinstance(e, dict) and e.get("task_of") and e.get("day") and e.get("start") and e.get("end"):
            linked.setdefault(str(e["task_of"]), e)
    for rows in tasks.values():
        for r in rows:
            e = linked.get(str(r["id"]))
            if e:             # the event is the truth for "planned in my time"
                r["planned"] = {"day": str(e["day"]), "start": valid_hhmm(e["start"]), "end": valid_hhmm(e["end"]), "on": "me"}
            elif r["owner"] == "agents" and isinstance(r.get("planned"), dict):
                r["planned"] = {**r["planned"], "on": "agents"}
            else:
                r.pop("planned", None)
    tray = review_tray(runs if tray_src is None else tray_src, tdoc.get("reviews") or [], sched.get("events") or [])
    return {"ok": True, "version": version, "today": dt.date.today().isoformat(),
            "areas": [{"id": a["id"], "name": a.get("name"), "color": a.get("color"),
                      "attention": area_attention(a), "attention_fixed": a.get("attention_fixed") is True,
                      "short": a.get("short") or str(a["id"]).upper()[:5]} for a in areas],
            "events": events, "placements": placements, "work_blocks": blocks,
            "availability": availability,
            "agents": agents_lanes(frm, to, own, areas, runs),
            "review_tray": tray, "tasks": tasks}
