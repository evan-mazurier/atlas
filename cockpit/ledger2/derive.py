"""Task records, derived on read from the event stream (contracts.md §1.7). Never reads the hooks' cache."""
from __future__ import annotations

import datetime as dt
import os
from collections import OrderedDict

try:
    from . import store
except ImportError:  # pragma: no cover - flat import
    import store  # type: ignore

CHECK_FILES = ("areas.yaml", "look.css")
IDLE_GAP_S = 15 * 60  # a gap between two events of a task counts as work only up to this long, unless a helper ran across it
VERDICT_MAP = {"commit-ready": "READY", "needs-changes": "NEEDS_CHANGES", "escalate": "ESCALATE"}


def parse_t(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.astimezone()
    return d


def _secs(a, b) -> int:
    da, db = parse_t(a), parse_t(b)
    if not da or not db:
        return 0
    return max(0, int((db - da).total_seconds()))


def type_info(wf: dict, typ: str) -> dict:
    """budget (None = no rework), needs_check for a type; unknown type = default budget, needs_check."""
    t = (wf.get("types") or {}).get(typ)
    if isinstance(t, dict):
        return {"budget": t.get("budget"), "needs_check": bool(t.get("needs_check", True))}
    return {"budget": wf.get("default_budget", 2), "needs_check": True}


def _min_budget(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _active_secs(started, end, times: list, helpers: list) -> int:
    """Active seconds: union of helper intervals and of event gaps <= IDLE_GAP_S, inside [started, end]."""
    a, b = parse_t(started), parse_t(end)
    if not a or not b or b <= a:
        return 0
    pts = sorted({min(max(x, a), b) for x in (parse_t(t) for t in times) if x} | {a, b})
    iv = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1) if (pts[i + 1] - pts[i]).total_seconds() <= IDLE_GAP_S]
    for h in helpers:
        s0 = parse_t(h["start"])
        if not s0:
            continue
        e0 = parse_t(h["end"]) if h["end"] else b
        s0, e0 = min(max(s0, a), b), min(max(e0 or b, a), b)
        if e0 > s0:
            iv.append((s0, e0))
    iv.sort()
    total, cur_s, cur_e = 0.0, None, None
    for x, y in iv:
        if cur_e is None or x > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = x, y
        elif y > cur_e:
            cur_e = y
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return int(total)


def _tier(helper) -> str:
    """The rework tier of a reviewer: senior-reviewer is `senior`, every other reviewer `routine`."""
    return "senior" if str(helper or "").strip().lower() == "senior-reviewer" else "routine"


def _new_task(e: dict, wf: dict, prev: dict | None) -> dict:
    typ = "playbook" if e["ev"] == "playbook" else (e.get("type") or "undeclared")
    info = type_info(wf, typ)
    t = {
        "id": e.get("task"), "src": "v2", "sid": e.get("sid"), "session_title": None,
        "area": e.get("area"), "type": typ, "playbook": e.get("cmd") if e["ev"] == "playbook" else None,
        "plan_ref": e.get("plan_ref"), "plan_auto": bool(e.get("plan_auto")),
        "auto": False, "started": e.get("t"), "ended": None, "duration_s": 0, "span_s": 0, "status": "open",
        "_times": [], "_hiv": [],
        "_budget": info["budget"], "_nc": set(), "_nc_n": 0, "_nc_t": {"routine": 0, "senior": 0}, "_esc": False, "_granted": 0,
        "_carried": 0, "_nc_at": 0, "_vseq": None, "_version": 0, "_lv": -1, "_other_v": -1, "_edits": 0, "_needs_check": info["needs_check"],
        "helpers": OrderedDict(), "verdicts": [], "_seen_agents": set(), "refusals": [],
        "caught": 0, "summary": None,
        "cost": {"weighted": 0, "by_model": {}, "by_helper": {}},
    }
    if prev is not None and prev["status"] == "switched":
        b = prev["_budget"]
        total = prev["_carried"] + prev["_nc_n"]
        # resolved = the latest verdict is a READY that covers the latest main-session version (the hook's task_resolved);
        # a task opened by a carry has no verdict yet, so it stays unresolved
        real = [x for x in prev["verdicts"] if not x.get("late")]
        last_v = real[-1]["verdict"] if real else None
        resolved = (last_v == "READY" and max(prev["_lv"], 0) >= prev["_version"]
                    and (prev["_nc_at"] == 0 or (prev["_vseq"] is not None and prev["_vseq"] > prev["_nc_at"])))
        spent = (b is not None and total > 0 and total >= b and not resolved) or prev["_esc"]
        if e.get("carried") is not None:
            spent = bool(e.get("carried"))            # the hook recorded its own decision; recompute only legacy events
        if spent:
            t["_carried"] = total          # carried rounds never collide with the new task's version numbers
            t["_nc_at"] = prev["_nc_at"]
            t["_esc"] = prev["_esc"]
            t["_granted"] = prev["_granted"]
            t["_budget"] = _min_budget(b, info["budget"])
    return t


def _own_title(t: dict) -> str | None:
    """Summary goal, else the description given to the task's first helper, else the session title (the API puts the
    linked Plan item's title in front of all three)."""
    goal = (t["summary"] or {}).get("goal") if t["summary"] else None
    if goal:
        return goal
    for h, v in t["helpers"].items():
        if h != "advisor" and v["labels"]:          # an advisor label ("scope: ...") describes the consult, not the task
            return v["labels"][0]
    return t["session_title"] or None


def _finish(t: dict, last_t: str | None, wf: dict) -> dict:
    ended = t["ended"]
    rec_end = ended or last_t or t["started"]
    t["span_s"] = _secs(t["started"], rec_end)
    t["duration_s"] = _active_secs(t["started"], rec_end, t["_times"], t["_hiv"])
    budget = t["_budget"]
    nc = t["_carried"] + t["_nc_n"]
    top = max(t["_nc_t"].values())                      # the busiest tier's own rounds (contracts §6.3)
    used = t["_carried"] + top
    left = None if budget is None else max(0, budget + t["_granted"] - used)
    verdicts = t["verdicts"]
    real = [x for x in verdicts if not x.get("late")]
    final = real[-1]["verdict"] if real else None
    checked = t["_lv"] >= t["_version"] and t["_lv"] >= 0
    unverified = (t["_needs_check"] and t["status"] in ("done", "switched", "ended", "abandoned")
                  and t["_edits"] > 0 and not checked)
    helpers = [{"helper": h, "count": v["count"], "labels": v["labels"]} for h, v in t["helpers"].items()]
    out = {
        "id": t["id"], "src": "v2", "sid": t["sid"], "session_title": t["session_title"],
        "title": _own_title(t),
        "area": t["area"], "type": t["type"], "playbook": t["playbook"], "plan_ref": t.get("plan_ref"), "plan_auto": t.get("plan_auto", False), "auto": t["auto"],
        "started": t["started"], "ended": ended, "duration_s": t["duration_s"], "span_s": t["span_s"], "status": t["status"],
        "crew": list(t["helpers"].keys()), "helpers": helpers,
        "rounds": {"needs_changes": nc, "budget": budget, "granted": t["_granted"], "left": left,
                   "own": t["_nc_n"], "carried": t["_carried"],
                   "own_routine": t["_nc_t"]["routine"], "own_senior": t["_nc_t"]["senior"], "used": used},
        "verdicts": verdicts, "final_verdict": final, "checked": checked,
        "unverified_flag": bool(unverified), "caught": t["caught"], "edits": t["_edits"],
        "refusals": t["refusals"], "cost": t["cost"], "summary": t["summary"],
    }
    out["cost"]["weighted"] = int(out["cost"]["weighted"])
    for v in out["cost"]["by_model"].values():
        v["weighted"] = int(v["weighted"])
    out["cost"]["by_helper"] = {k: int(v) for k, v in out["cost"]["by_helper"].items()}
    return out


def derive_v2(events: list[dict], wf: dict, markers: set[str] | None = None) -> list[dict]:
    """TaskRecords from v2 events (any order across sessions; order within a session = list order)."""
    markers = markers or set()
    by_sid: "OrderedDict[str, list]" = OrderedDict()
    for e in events:
        if e.get("v") == 1 or e.get("src") == "v1":
            continue
        sid = e.get("sid")
        if sid:
            by_sid.setdefault(sid, []).append(e)
    out = []
    for sid, evs in by_sid.items():
        out.extend(_derive_session(sid, evs, wf, markers))
    return out


def _derive_session(sid: str, evs: list[dict], wf: dict, markers: set[str]) -> list[dict]:
    tasks: "OrderedDict[str, dict]" = OrderedDict()
    cur = None       # the open task
    last = None      # the most recently opened task
    title = None
    auto = False
    agent_helper: dict[str, str] = {}
    lock: dict | None = None             # the last abandoned task's values, read by legacy rework_recalled / for_task events
    late_nc, late_esc = 0, False         # late verdicts that arrived with no task open: they count on the next task
    spawn_n = 0                          # helpers launched so far in the session (the hook's spawn_seq)
    seq_of: dict[str, int] = {}          # launch id -> launch order; agent id -> launch order (via helper_paired)
    last_t = None
    for e in evs:
        ev, t = e.get("ev"), e.get("t")
        last_t = t or last_t
        if e.get("title"):
            title = e["title"]
        if ev in ("work_type", "playbook"):
            if not e.get("task"):
                continue
            prev = None
            if cur is not None:
                cur["status"], cur["ended"] = "switched", t
                prev = cur
            nt = _new_task(e, wf, prev)
            if late_nc or late_esc:
                nt["_carried"] += late_nc
                nt["_nc_at"] = spawn_n
                nt["_esc"] = nt["_esc"] or late_esc
                late_nc, late_esc = 0, False
            tasks[nt["id"]] = nt
            cur = last = nt
            continue
        if ev == "task_abandoned":
            old = tasks.get(e.get("task"))
            if old is not None and old["status"] == "open":
                old["status"], old["ended"] = "abandoned", t
                lock = {"task": old["id"], "nc": int(e.get("needs_changes") or 0),
                        "granted": int(e["granted"]) if e.get("granted") is not None else old["_granted"],
                        "budget": old["_budget"]}
                if cur is old:
                    cur = None                  # the next declaration opens fresh (no carry)
            continue
        if ev == "auto":
            auto = auto or (e.get("marker") in markers)
            continue
        if ev == "session_start" and e.get("source") == "resume" and cur is None:
            ended = [x for x in tasks.values() if x["status"] == "ended"]
            if ended:                           # a resumed session reopens its most recent ended task
                cur = last = ended[-1]
                cur["status"], cur["ended"] = "open", None
            continue
        if ev == "session_end":
            if cur is not None:
                cur["status"], cur["ended"] = "ended", t
                cur = None
            continue
        if ev == "helper_resume" and e.get("agent_id") in agent_helper:
            # the hook's resume: a new launch of that agent for sequencing; its next verdict is a new one
            spawn_n += 1
            seq_of[e["agent_id"]] = spawn_n
            for x in tasks.values():
                x["_seen_agents"].discard(e["agent_id"])
        if ev == "helper_paired" and e.get("agent_id"):
            if e.get("tuid") is None and e["agent_id"] in agent_helper:      # a resumed run starting again
                for x in tasks.values():
                    x["_seen_agents"].discard(e["agent_id"])
            agent_helper[e["agent_id"]] = e.get("helper")
            if e.get("tuid") in seq_of:
                seq_of[e["agent_id"]] = seq_of[e["tuid"]]
        if ev == "helper_start":
            spawn_n += 1
            if e.get("tuid"):
                seq_of[e["tuid"]] = spawn_n
        if ev in ("helper_paired", "helper_end") and e.get("agent_id"):
            for x in tasks.values():
                _track_helper(x, e, t)
        tid = e.get("task")
        tk = tasks.get(tid) if tid else (last)
        if tk is None:
            if ev == "verdict" and e.get("counted_on") == "next":       # a late verdict before any task exists
                if e.get("verdict") == "NEEDS_CHANGES":
                    late_nc += 1
                elif e.get("verdict") == "ESCALATE":
                    late_esc = True
            continue
        if t:
            tk["_times"].append(t)
        if ev == "helper_start":
            tk["_hiv"].append({"start": t, "end": None, "tuid": e.get("tuid"), "aid": None})
        elif ev == "helper_resume" and e.get("agent_id"):
            tk["_hiv"].append({"start": t, "end": None, "tuid": None, "aid": e["agent_id"]})
        if ev == "task_done":
            if tk["status"] == "open":
                tk["status"], tk["ended"] = "done", t
                if cur is tk:
                    cur = None
        elif ev == "edit":
            tk["_edits"] += 1
            tk["_version"] = max(tk["_version"], int(e.get("version") or 0))
            if os.path.basename(str(e.get("path") or "")).lower() not in CHECK_FILES:
                tk["_other_v"] = max(tk["_other_v"], int(e.get("version") or 0))
        elif ev == "helper_start":
            h = e.get("helper") or "unknown"
            slot = tk["helpers"].setdefault(h, {"count": 0, "labels": []})
            slot["count"] += 1
            lab = e.get("label")
            if lab and lab not in slot["labels"] and len(slot["labels"]) < 10:
                slot["labels"].append(lab)
        elif ev == "verdict":
            aid = e.get("agent_id")
            if aid and aid in tk["_seen_agents"]:
                continue
            if aid:
                tk["_seen_agents"].add(aid)
            v = e.get("verdict")
            ver = int(e["version"]) if e.get("version") is not None else None
            co = e.get("counted_on")
            is_late = bool(co) and v in ("NEEDS_CHANGES", "ESCALATE")
            entry = {"t": t, "helper": e.get("helper"), "verdict": v, "version": ver,
                     "findings": int(e.get("findings") or 0)}
            if is_late:
                entry["late"] = True                 # counted on another task; never this task's final verdict
            tk["verdicts"].append(entry)
            if not is_late:
                tk["_vseq"] = seq_of.get(aid)
            if co and v in ("NEEDS_CHANGES", "ESCALATE"):
                # a late verdict: it counts on the open task (or the next one), never as an own round of its launch task
                target = tasks.get(co) if co != "next" else None
                if target is None:
                    if v == "NEEDS_CHANGES":
                        late_nc += 1
                    else:
                        late_esc = True
                elif v == "NEEDS_CHANGES":
                    target["_carried"] += 1
                    target["_nc_at"] = spawn_n
                else:
                    target["_esc"] = True
            elif v == "NEEDS_CHANGES":
                if "round" in e:                # the hook's own decision (contracts §1.2)
                    if e["round"]:
                        tk["_nc_n"] += 1
                        tk["_nc_t"][_tier(e.get("helper"))] += 1
                else:                           # legacy verdict: one round per distinct failing version
                    key = ver if ver is not None else "?" + str(aid)
                    if key not in tk["_nc"]:
                        tk["_nc"].add(key)
                        tk["_nc_n"] += 1
                        tk["_nc_t"][_tier(e.get("helper"))] += 1
                tk["_nc_at"] = spawn_n
            if v == "READY":
                if ver is not None:            # unknown version never counts as a check
                    tk["_lv"] = max(tk["_lv"], ver)
            elif v == "NEEDS_CHANGES":
                tk["caught"] += int(e.get("findings") or 0)
            elif v == "ESCALATE" and not (co and v in ("NEEDS_CHANGES", "ESCALATE")):
                tk["_esc"] = True
        elif ev == "check":
            if e.get("ok") and tk["_other_v"] <= tk["_lv"]:
                tk["_lv"] = max(tk["_lv"], int(e.get("version") or 0))
        elif ev == "rework_granted":
            target = tasks.get(e.get("for_task")) if e.get("for_task") else None
            target = target if target is not None else tk       # for_task: the abandoned task's own round
            target["_granted"] += int(e.get("n") or 1)
            if lock is not None and e.get("for_task") and e.get("for_task") == lock["task"]:
                lock["granted"] += int(e.get("n") or 1)
            target["_esc"] = False
        elif ev == "rework_recalled":
            src = tasks.get(e.get("from_task"))
            tk["_carried"] += int(e.get("nc") or 0)              # legacy event: the abandoned task's rounds come back
            tk["_nc_at"] = spawn_n                               # a reviewer launched before the recall cannot resolve it
            if lock is not None and lock["task"] == e.get("from_task"):
                tk["_budget"] = _min_budget(tk["_budget"], lock["budget"])
                tk["_granted"] += lock["granted"]
            elif src is not None:
                tk["_budget"] = _min_budget(tk["_budget"], src["_budget"])
                tk["_granted"] += src["_granted"]
        elif ev == "refusal":
            r = {"guard": e.get("guard"), "would": bool(e.get("would"))}
            if str(e.get("detail") or "").startswith("carry-over"):
                r["notice"] = True            # a carry-over notice, not a denial
            tk["refusals"].append(r)
        elif ev == "summary":
            tk["summary"] = {k: e.get(k) for k in ("goal", "produced", "decisions", "open", "review_min")
                             if e.get(k) is not None}
        elif ev == "cost":
            _add_cost(tk, e, agent_helper)
    res = []
    for tk in tasks.values():
        tk["session_title"] = title
        tk["auto"] = auto
        res.append(_finish(tk, last_t, wf))
    return res


def _track_helper(tk: dict, e: dict, t) -> None:
    """Pair a helper interval with its agent id (helper_paired by tuid) and close it (helper_end)."""
    aid = e["agent_id"]
    if e.get("ev") == "helper_paired":
        for h in tk["_hiv"]:
            if h["aid"] is None and h["tuid"] and h["tuid"] == e.get("tuid"):
                h["aid"] = aid
        return
    for h in reversed(tk["_hiv"]):
        if h["aid"] == aid and h["end"] is None:
            h["end"] = t
            break


def _add_cost(tk: dict, e: dict, agent_helper: dict) -> None:
    c = tk["cost"]
    w = float(e.get("weighted") or 0)
    model = e.get("model") or ""
    if model:
        m = c["by_model"].setdefault(model, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "weighted": 0})
        for k in ("in", "out", "cache_read", "cache_write"):
            m[k] += int(e.get(k) or 0)
        m["weighted"] += w
    c["weighted"] += w
    scope = e.get("scope") or "main"
    helper = "chief" if scope == "main" else (e.get("helper") or agent_helper.get(scope) or "unknown")
    c["by_helper"][helper] = c["by_helper"].get(helper, 0) + w


# ---------------------------------------------------------------- v1 jobs


def _phase_helper(phase: str) -> str | None:
    p = (phase or "").lower()
    if "scoping" in p:
        return "scoper"
    if "review" in p:
        return "reviewer"
    if "code" in p or "rework" in p or p == "coding":
        return "implementer"
    return None


def derive_v1(events: list[dict], areas: list[dict], wf: dict) -> list[dict]:
    jobs: "OrderedDict[str, list]" = OrderedDict()
    for e in events:
        if e.get("job"):
            jobs.setdefault(e["job"], []).append(e)
    proj_area = {}
    for a in areas:
        for p in a.get("projects") or []:
            proj_area[p] = a.get("id")
    info = type_info(wf, "coding")
    out = []
    for job, evs in jobs.items():
        started_ev = next((e for e in evs if e.get("ev") == "started"), None)
        fin = next((e for e in reversed(evs) if e.get("ev") == "finished"), None)
        merged = next((e for e in evs if e.get("ev") == "merged"), None)
        started = (started_ev or evs[0]).get("t") or (fin or {}).get("created")
        project = next((e.get("project") for e in evs if e.get("project")), None)
        title = next((e["plan_task"] for e in evs if e.get("plan_task")), None) or job
        crew: list[str] = []
        verdicts, nc_pass = [], set()
        cost = {"weighted": 0, "by_model": {}, "by_helper": {}}
        model = (started_ev or {}).get("model") or ""
        phases = [e for e in evs if e.get("ev") == "phase_tokens"]
        real = [e for e in phases if e.get("phase") != "job_total"]
        for e in (real or phases):
            fresh = int(e.get("fresh") or 0)
            w = fresh * store.model_weight(model)
            cost["weighted"] += w
            m = cost["by_model"].setdefault(model or "unknown", {"in": 0, "out": 0, "cache_read": 0,
                                                                  "cache_write": 0, "weighted": 0})
            m["in"] += int(e.get("input") or 0)
            m["out"] += int(e.get("output") or 0)
            m["cache_read"] += int(e.get("cache_read") or 0)
            m["cache_write"] += int(e.get("cache_write") or e.get("cache_creation") or 0)
            m["weighted"] += w
            h = _phase_helper(e.get("phase")) or "chief"
            cost["by_helper"][h] = cost["by_helper"].get(h, 0) + w
        for e in evs:
            if e.get("ev") == "phase_tokens":
                h = _phase_helper(e.get("phase"))
                if h and h not in crew:
                    crew.append(h)
            elif e.get("ev") == "test" and "test-runner" not in crew:
                crew.append("test-runner")
            elif e.get("ev") == "review":
                if "reviewer" not in crew:
                    crew.append("reviewer")
                v = VERDICT_MAP.get(e.get("verdict"), e.get("verdict"))
                verdicts.append({"t": e.get("t"), "helper": "reviewer", "verdict": v,
                                 "version": int(e.get("pass") or 0), "findings": int(e.get("findings") or 0)})
                if v == "NEEDS_CHANGES":
                    nc_pass.add((e.get("lane"), e.get("pass")))
        done = merged is not None
        if done:
            status, ended = "done", merged.get("t")
        elif fin is not None:
            status, ended = "ended", fin.get("finished") or fin.get("t")
        else:                      # v1 is retired: a job without a finish is over, at its last event
            status, ended = "ended", evs[-1].get("t")
        last_t = evs[-1].get("t")
        cost["weighted"] = int(cost["weighted"])
        for m in cost["by_model"].values():
            m["weighted"] = int(m["weighted"])
        cost["by_helper"] = {k: int(v) for k, v in cost["by_helper"].items()}
        nc = len(nc_pass)
        budget = info["budget"]
        final = verdicts[-1]["verdict"] if verdicts else None
        out.append({
            "id": job, "src": "v1", "sid": None, "session_title": None, "title": title,
            "area": proj_area.get(project), "type": "coding", "playbook": None, "auto": False,
            "started": started, "ended": ended,
            "duration_s": _secs(started, ended or last_t), "span_s": _secs(started, ended or last_t),
            "status": status,
            "crew": crew, "helpers": [{"helper": h, "count": 1, "labels": []} for h in crew],
            "rounds": {"needs_changes": nc, "budget": budget, "granted": 0,
                       "left": None if budget is None else max(0, budget - nc), "own": nc, "carried": 0},
            "verdicts": verdicts, "final_verdict": final, "checked": final == "READY",
            "unverified_flag": False, "caught": sum(v["findings"] for v in verdicts if v["verdict"] == "NEEDS_CHANGES"),
            "edits": 0, "refusals": [], "cost": cost, "summary": None, "project": project,
        })
    return out
