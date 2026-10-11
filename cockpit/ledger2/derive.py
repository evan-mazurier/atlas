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


def _new_task(e: dict, wf: dict, prev: dict | None) -> dict:
    typ = "playbook" if e["ev"] == "playbook" else (e.get("type") or "undeclared")
    info = type_info(wf, typ)
    t = {
        "id": e.get("task"), "src": "v2", "sid": e.get("sid"), "session_title": None,
        "area": e.get("area"), "type": typ, "playbook": e.get("cmd") if e["ev"] == "playbook" else None,
        "plan_ref": e.get("plan_ref"),
        "auto": False, "started": e.get("t"), "ended": None, "duration_s": 0, "status": "open",
        "_budget": info["budget"], "_nc": set(), "_esc": False, "_granted": 0,
        "_carried": 0, "_nc_at": 0, "_vseq": None, "_version": 0, "_lv": -1, "_other_v": -1, "_edits": 0, "_needs_check": info["needs_check"],
        "helpers": OrderedDict(), "verdicts": [], "_seen_agents": set(), "refusals": [],
        "caught": 0, "summary": None,
        "cost": {"weighted": 0, "by_model": {}, "by_helper": {}},
    }
    if prev is not None and prev["status"] == "switched":
        b = prev["_budget"]
        total = prev["_carried"] + len(prev["_nc"])
        # resolved = the latest verdict is a READY that covers the latest main-session version (the hook's task_resolved);
        # a task opened by a carry has no verdict yet, so it stays unresolved
        last_v = prev["verdicts"][-1]["verdict"] if prev["verdicts"] else None
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


def _finish(t: dict, last_t: str | None, wf: dict) -> dict:
    ended = t["ended"]
    rec_end = ended or last_t or t["started"]
    t["duration_s"] = _secs(t["started"], rec_end)
    budget = t["_budget"]
    nc = t["_carried"] + len(t["_nc"])
    left = None if budget is None else max(0, budget + t["_granted"] - nc)
    verdicts = t["verdicts"]
    final = verdicts[-1]["verdict"] if verdicts else None
    checked = t["_lv"] >= t["_version"] and t["_lv"] >= 0
    unverified = (t["_needs_check"] and t["status"] in ("done", "switched", "ended")
                  and t["_edits"] > 0 and not checked)
    helpers = [{"helper": h, "count": v["count"], "labels": v["labels"]} for h, v in t["helpers"].items()]
    out = {
        "id": t["id"], "src": "v2", "sid": t["sid"], "session_title": t["session_title"],
        "title": (t["summary"] or {}).get("goal") if t["summary"] else None,
        "area": t["area"], "type": t["type"], "playbook": t["playbook"], "plan_ref": t.get("plan_ref"), "auto": t["auto"],
        "started": t["started"], "ended": ended, "duration_s": t["duration_s"], "status": t["status"],
        "crew": list(t["helpers"].keys()), "helpers": helpers,
        "rounds": {"needs_changes": nc, "budget": budget, "granted": t["_granted"], "left": left},
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
            tasks[nt["id"]] = nt
            cur = last = nt
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
        if ev == "helper_paired" and e.get("agent_id"):
            agent_helper[e["agent_id"]] = e.get("helper")
            if e.get("tuid") in seq_of:
                seq_of[e["agent_id"]] = seq_of[e["tuid"]]
        if ev == "helper_start":
            spawn_n += 1
            if e.get("tuid"):
                seq_of[e["tuid"]] = spawn_n
        tid = e.get("task")
        tk = tasks.get(tid) if tid else (last)
        if tk is None:
            continue
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
            tk["verdicts"].append({"t": t, "helper": e.get("helper"), "verdict": v, "version": ver,
                                   "findings": int(e.get("findings") or 0)})
            tk["_vseq"] = seq_of.get(aid)
            if v == "NEEDS_CHANGES":
                tk["_nc_at"] = spawn_n
            if v == "READY":
                if ver is not None:            # unknown version never counts as a check
                    tk["_lv"] = max(tk["_lv"], ver)
            elif v == "NEEDS_CHANGES":
                tk["_nc"].add(ver if ver is not None else "?" + str(aid))
                tk["caught"] += int(e.get("findings") or 0)
            elif v == "ESCALATE":
                tk["_esc"] = True
        elif ev == "check":
            if e.get("ok") and tk["_other_v"] <= tk["_lv"]:
                tk["_lv"] = max(tk["_lv"], int(e.get("version") or 0))
        elif ev == "rework_granted":
            tk["_granted"] += int(e.get("n") or 1)
            tk["_esc"] = False
        elif ev == "refusal":
            tk["refusals"].append({"guard": e.get("guard"), "would": bool(e.get("would"))})
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
        else:
            status, ended = "open", None
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
            "duration_s": _secs(started, ended or last_t), "status": status,
            "crew": crew, "helpers": [{"helper": h, "count": 1, "labels": []} for h in crew],
            "rounds": {"needs_changes": nc, "budget": budget, "granted": 0,
                       "left": None if budget is None else max(0, budget - nc)},
            "verdicts": verdicts, "final_verdict": final, "checked": final == "READY",
            "unverified_flag": False, "caught": sum(v["findings"] for v in verdicts if v["verdict"] == "NEEDS_CHANGES"),
            "edits": 0, "refusals": [], "cost": cost, "summary": None, "project": project,
        })
    return out
