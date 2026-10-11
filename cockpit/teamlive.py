"""
The Claude Code team, live — which Claude Code DESKTOP sessions are working, on what kind of work,
and which named helper is running (GET /api/team-live; the page is cockpit/web/team.html).

Source: a hook outside this repo appends one JSON line per event to
`~/.claude/team-live/events-YYYY-MM-DD.jsonl` (override the directory with `ATLAS_TEAMLIVE_DIR`).
This module only READS that file plus the Claude Code transcripts (for cost). It writes nothing.

Control plane only: it shows what the work plane is doing. It is NOT about Atlas's own dispatches —
those are excluded (their session ids live in `jobs/*.json`; see `EXCLUDE_PROJECT_DIRS` too).

Pure and testable: `build_state(events, now, exclude_sids, cost_fn)` takes plain data. `snapshot()`
is the thin wrapper the server calls.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ATLAS_ROOT = HERE.parent
DASHBOARD = HERE / "core"
if str(DASHBOARD) not in sys.path:      # `usage.model_weight`, cockpit/core
    sys.path.insert(0, str(DASHBOARD))

# --- exclusions: this panel is Claude Code DESKTOP sessions only ------------------------------------
# Sessions whose transcript folder is one of these are not desktop work: the Inbox summarizer runs from
# ~/.atlas/inbox, other tools and ad-hoc `claude -p` CLI runs land in Temp / system32. Compared case-insensitively; a folder nested under one (`<dir>-...`) counts too.
EXCLUDE_PROJECT_DIRS = {"C--Users-user--atlas-inbox", "C--Users-user-AppData-Local-Temp", "C--WINDOWS-system32"}
# Folders that contain one of these are Atlas's own dispatch sandboxes (worktrees/job-<id>) — also not
# desktop work. Belt and braces for the `jobs/*.json` session-id check, which can lag a job's first minute.
EXCLUDE_FOLDER_MARKERS = ("-worktrees-job-",)

IDLE_HIDE = dt.timedelta(hours=4)       # an idle session older than this leaves the live view (stays in Today)
STALE_WORKING = dt.timedelta(minutes=30)  # "working" with no event at all for this long = stopped without a Stop
STALE_SILENT_HELPER = dt.timedelta(minutes=10)  # a helper with no event this long, in a session that has moved on (new prompt), is gone
STALE_HELPER = dt.timedelta(hours=3)    # a helper with a Start and never a Stop, this old, is dead
# A helper with a Start and no Stop is LIVE while its own transcript (`<sid>/subagents/agent-<id>.jsonl`) was
# written within this window, and a ghost once it was not (a lost SubagentStop must not leave a session "working"
# forever). The hook log cannot tell: a helper's Edit / Read / test calls are not logged, so a helper that is
# busy can be silent in the event log for many minutes. Measured on 44k consecutive records of helper
# transcripts: median gap 0.6 s, 99th percentile 57 s. But the full test suite runs ~5 min inside ONE Bash call
# (no transcript write meanwhile), so the window is 8 min: it covers that call without keeping a dead helper
# alive for long.
HELPER_FRESH = dt.timedelta(minutes=8)
PAIR_WINDOW = 120   # seconds: a helper's Start may come this long after its launch when several are sent at once
MIN_LIFETIME = dt.timedelta(seconds=30)  # a session this short with no helper / playbook / WorkType is a CLI call, not a task
WAITING_WINDOW = dt.timedelta(hours=2)  # idle after a Stop this recently = "waiting" for the person (v2 attention bar)
MAX_STEPS = 12

# --- vocabulary --------------------------------------------------------------------------------------
# display id -> (name, tier). Ids are the org chart's creature ids; `op` is the human.
AGENTS = {
    "op": ("Operator", "human"), "chief": ("orchestrator", "opus"), "scout": ("scout", "haiku"),
    "clerk": ("sweeper", "haiku"), "analyst": ("researcher", "sonnet"), "builder": ("implementer", "sonnet"),
    "writer": ("drafter", "sonnet"), "labtech": ("test-runner", "haiku"), "inspector": ("reviewer", "sonnet"),
    "partner": ("senior-reviewer", "opus"),
}
# helper `agent_type` (the subagent name) -> display id. `explore` is Claude Code's built-in recon helper.
AGENT_TYPE_ID = {
    "scout": "scout", "explore": "scout", "sweeper": "clerk", "researcher": "analyst",
    "implementer": "builder", "drafter": "writer", "test-runner": "labtech",
    "reviewer": "inspector", "senior-reviewer": "partner",
}

# playbook command -> (type label, route). The route is the org chart's, in display ids.
PLAYBOOKS = {
    "daily-review": ("Research", ["chief", "chief", "op", "writer", "inspector", "chief", "op"]),
    "start-of-day": ("Product morning",
                                   ["clerk", "chief", "op", "writer", "analyst", "builder", "inspector", "partner", "op"]),
    "end-of-day": ("Product evening", ["clerk", "chief", "inspector", "chief", "op"]),
    "optimize-prompt": ("Sharpen an idea", ["chief", "scout", "chief", "op"]),
    "tune-up": ("Monthly tune-up", ["clerk", "chief", "chief", "op", "writer", "inspector"]),
    "personal-brand-advisor": ("Brand advice", []),
}
WORKTYPE_LABEL = {"coding": "Coding", "research": "Research", "writing": "Writing",
                  "legal": "Legal or money", "quick-question": "Quick question"}
# inferred from the helpers used, first match wins (agent_type -> label); "only scout" handled apart.
INFER_ORDER = [("implementer", "Coding"), ("researcher", "Research"), ("drafter", "Writing"),
               ("senior-reviewer", "Legal or money")]

_PROMPT_EVS = ("UserPromptSubmit", "UserPromptExpansion")
_CMD_EVS = ("UserPromptSubmit", "UserPromptExpansion", "PreToolUse")
# Activity after a Stop means the session is working again even when no prompt event announced it.
_ACTIVITY_EVS = ("PreToolUse", "PostToolUse", "SubagentStart", "WorkType")


# --- small helpers -----------------------------------------------------------------------------------
def _ts(s) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(s)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def _hhmm(t: dt.datetime | None) -> str:
    return t.strftime("%H:%M") if t else ""


def _norm_cmd(cmd) -> str:
    c = str(cmd or "").strip().lstrip("/").lower()
    return c.split(":")[-1] if ":" in c else c


def _encode_dir(path: str) -> str:
    """Claude Code's transcript folder name for a working directory: every non-alphanumeric -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path or "")


def _folder_of(transcript: str | None, cwd: str | None) -> str:
    if transcript:
        parts = re.split(r"[\\/]", str(transcript).rstrip("\\/"))
        if len(parts) >= 2 and parts[-2]:
            return parts[-2]
    return _encode_dir(cwd or "")


def project_label(folder: str, cwd: str | None = None) -> str:
    """`D--AI-Atlas` -> Atlas; `D--AI-product` -> product; `D--Personal-Reading-List` -> Reading List."""
    m = re.match(r"^[A-Za-z]--(.+)$", folder or "")
    if m:
        rest = m.group(1)
        if rest.startswith("AI-"):
            rest = rest[3:]
        elif rest.startswith("Personal-"):
            rest = rest[len("Personal-"):].replace("-", " ")
        rest = re.split(r"--|-worktrees-", rest)[0]
        if rest:
            return rest
    if cwd:
        base = re.split(r"[\\/]", str(cwd).rstrip("\\/"))[-1]
        if base:
            return base
    return folder or "unknown"


def worktype_label(t: str) -> str:
    t = str(t or "").strip().lower()
    return WORKTYPE_LABEL.get(t) or t.replace("-", " ").replace("_", " ").title() or "Orchestrator working"


def _excluded_folder(folder: str) -> bool:
    f = (folder or "").lower()
    # a dir and anything nested under it (Temp holds pytest-of-*/… sub-folders, each its own transcript dir)
    if any(f == d.lower() or f.startswith(d.lower() + "-") for d in EXCLUDE_PROJECT_DIRS):
        return True
    return any(mk in f for mk in EXCLUDE_FOLDER_MARKERS)


# --- loading -----------------------------------------------------------------------------------------
def team_dir() -> Path:
    env = os.environ.get("ATLAS_TEAMLIVE_DIR")
    return Path(env) if env else Path(os.path.expanduser("~")) / ".claude" / "team-live"


_EVENT_CACHE: dict[str, dict] = {}


def _read_events_file(f: Path) -> list[dict]:
    """Parsed events of one log file. Cached by size + mtime, and incremental: a file that only GREW is
    parsed from where the last call stopped, so a 2 s poll never re-parses an unchanged (or old) log."""
    try:
        st = f.stat()
    except Exception:  # noqa: BLE001
        _EVENT_CACHE.pop(str(f), None)
        return []
    c = _EVENT_CACHE.get(str(f))
    if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime_ns:
        return c["events"]
    if not c or st.st_size < c["off"]:
        c = {"off": 0, "events": []}
    events = list(c["events"])
    off = c["off"]
    try:
        with open(f, "rb") as fh:
            fh.seek(off)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break                                  # a half-written last line: read it next time
                off += len(raw)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(e, dict) and e.get("sid") and _ts(e.get("ts")):
                    e["_t"] = _ts(e["ts"])
                    events.append(e)
    except Exception:  # noqa: BLE001
        return c["events"]
    _EVENT_CACHE[str(f)] = {"size": st.st_size, "mtime": st.st_mtime_ns, "off": off, "events": events}
    return events


def load_events(day_dir, now: dt.datetime) -> list[dict]:
    """Today's events plus yesterday's (a session can span midnight), in time order. Bad lines are skipped."""
    d = Path(day_dir)
    out: list[dict] = []
    for day in (now.date() - dt.timedelta(days=1), now.date()):
        out.extend(_read_events_file(d / f"events-{day.isoformat()}.jsonl"))
    out.sort(key=lambda e: e["_t"])      # stable: same-second events keep file order
    return out


_JOB_SID_CACHE: dict[str, tuple[float, set]] = {}


def _collect_sids(obj, acc: set) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "session_id" and isinstance(v, str) and v:
                acc.add(v)
            else:
                _collect_sids(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect_sids(v, acc)


def atlas_job_sids(jobs_dir=None) -> set:
    """Every `session_id` recorded in `jobs/*.json` (including per-lane ones) — Atlas's own dispatches."""
    jd = Path(jobs_dir) if jobs_dir else ATLAS_ROOT / "jobs"
    sids: set = set()
    try:
        files = list(jd.glob("*.json"))
    except Exception:  # noqa: BLE001
        return sids
    for f in files:
        try:
            mt = f.stat().st_mtime
            hit = _JOB_SID_CACHE.get(str(f))
            if hit and hit[0] == mt:
                sids |= hit[1]
                continue
            found: set = set()
            _collect_sids(json.loads(f.read_text(encoding="utf-8", errors="ignore")), found)
            _JOB_SID_CACHE[str(f)] = (mt, found)
            sids |= found
        except Exception:  # noqa: BLE001
            continue
    return sids


def atlas_known_sids() -> set:
    """Session ids of Atlas's own conversational-agent / brain-keeper CLI calls (cockpit/core/atlas_sessions.py)."""
    try:
        import atlas_sessions
        return set(atlas_sessions.known())
    except Exception:  # noqa: BLE001
        return set()


# --- cost (price-weighted fresh tokens, today) -------------------------------------------------------
_COST_CACHE: dict[str, dict] = {}


def _file_cost_today(path: Path, today: dt.date) -> int:
    """Today's price-weighted fresh tokens in one transcript. Incremental: only bytes added since the last
    call are parsed, so polling a transcript that is growing costs a read of the new tail, not of the file."""
    import usage  # cockpit/core/usage.py
    try:
        st = path.stat()
    except Exception:  # noqa: BLE001
        return 0
    key = str(path)
    c = _COST_CACHE.get(key)
    if not c or c["day"] != today or st.st_size < c["off"]:
        c = {"day": today, "off": 0, "total": 0, "mtime": 0.0, "title": None}
        _COST_CACHE[key] = c
    if st.st_mtime == c["mtime"] and st.st_size == c["off"]:
        return c["total"]
    total, off, title = c["total"], c["off"], c.get("title")
    try:
        with open(path, "rb") as fh:                       # line by line: never the whole file in memory
            fh.seek(off)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break                                  # half-written last line: next call
                off += len(raw)
                if b'"custom-title"' in raw:               # the session's name in the Claude sidebar (latest wins)
                    try:
                        rec = json.loads(raw)
                        if rec.get("type") == "custom-title":
                            title = (rec.get("customTitle") or "").strip() or title
                            continue
                    except Exception:  # noqa: BLE001
                        pass                               # not a title record: still counted below
                if b'"usage"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                    msg = rec.get("message") or {}
                    u = msg.get("usage") or {}
                    ts = rec.get("timestamp")
                    if not u or not ts:
                        continue
                    t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                    if t.date() != today:
                        continue
                    i, o = int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)
                    prompt = i + int(u.get("cache_read_input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0)
                    total += round(usage.model_weight(msg.get("model"), prompt) * (i + o))
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        return c["total"]
    c.update(off=off, total=total, mtime=st.st_mtime, title=title)
    return total


def session_title(sid: str, transcript: str | None) -> str | None:
    """The session's name in the Claude sidebar (its transcript's latest `custom-title` record), or None.
    Read in the same incremental pass as the cost, so asking for it costs nothing extra."""
    main = _main_transcript(sid, transcript)
    if not main:
        return None
    _file_cost_today(main, dt.date.today())
    return (_COST_CACHE.get(str(main)) or {}).get("title")


def _main_transcript(sid: str, transcript: str | None) -> Path | None:
    main = Path(transcript) if transcript else None
    if main and main.exists():
        return main
    try:
        import usage
        root = usage.projects_dir()
    except Exception:  # noqa: BLE001
        return None
    hits = list(root.glob(f"*/{sid}.jsonl")) if root else []
    return hits[0] if hits else None


def session_cost(sid: str, transcript: str | None) -> int:
    """Today's price-weighted fresh tokens for a session: its transcript + `<sid>/subagents/*.jsonl`."""
    today = dt.date.today()
    main = Path(transcript) if transcript else None
    if not main or not main.exists():
        try:
            import usage
            root = usage.projects_dir()
        except Exception:  # noqa: BLE001
            root = None
        main = None
        if root:
            hits = list(root.glob(f"*/{sid}.jsonl"))
            main = hits[0] if hits else None
    if not main:
        return 0
    total = _file_cost_today(main, today)
    try:
        for f in (main.parent / sid / "subagents").glob("*.jsonl"):
            total += _file_cost_today(f, today)
    except Exception:  # noqa: BLE001
        pass
    return total


def helper_transcript_mtime(sid: str, transcript: str | None, agent_id: str) -> dt.datetime | None:
    """When a helper's own transcript (`<sid>/subagents/agent-<id>.jsonl`, next to the session's) was last
    written, as a naive local time; None when the file cannot be found (the event log then decides alone)."""
    main = _main_transcript(sid, transcript)
    if not main or not agent_id:
        return None
    try:
        return dt.datetime.fromtimestamp((main.parent / sid / "subagents" / f"agent-{agent_id}.jsonl").stat().st_mtime)
    except OSError:
        return None


# --- one session -------------------------------------------------------------------------------------
def _helpers(evs: list[dict], now: dt.datetime, helper_mtime=None) -> list[dict]:
    """Helper RUNS in spawn order. A run is one SubagentStart paired with the first SubagentStop of the same
    agent_id after it and before that id's next Start (a helper can be resumed under the same id); only the
    latest run of an id may still be running. Joins PreToolUse(Agent) -> PostToolUse (tuid) -> SubagentStart
    (agent_id) in two passes: every tuid-linked pair first, then a same-type nearest-in-time fallback for the
    Starts left over, so two parallel helpers of one type keep their own labels.

    `helper_mtime(agent_id) -> datetime | None` is when that helper's own transcript was last written (None =
    unknown). For a run with no Stop it is the liveness evidence (HELPER_FRESH): fresh = running, stale = gone.
    Without it (None) the event log alone decides, as before."""
    pres = [dict(e, _used=False) for e in evs if e.get("ev") == "PreToolUse" and e.get("tool") == "Agent"]
    starts = [(i, e) for i, e in enumerate(evs) if e.get("ev") == "SubagentStart"]
    posts = [(i, e) for i, e in enumerate(evs) if e.get("ev") == "PostToolUse" and e.get("tool") == "Agent" and e.get("agent_id")]
    stop_idx: dict[str, list[tuple[int, dict]]] = {}
    for i, e in enumerate(evs):
        if e.get("ev") == "SubagentStop" and e.get("agent_id"):
            stop_idx.setdefault(e["agent_id"], []).append((i, e))
    session_stops = [(i, e["_t"]) for i, e in enumerate(evs) if e.get("ev") in ("Stop", "SessionEnd")]

    runs: list[dict] = []
    by_aid: dict[str, list[dict]] = {}
    for i, e in starts:
        aid = e.get("agent_id") or e.get("in_agent") or f"anon{i}"
        r = {"id": aid, "type": str(e.get("agent_type") or "").lower(), "start": e["_t"], "idx": i, "pre": None, "post": None}
        r["agent"] = AGENT_TYPE_ID.get(r["type"])
        runs.append(r)
        by_aid.setdefault(aid, []).append(r)
    for rs in by_aid.values():                              # this run's window: from its Start to the id's next Start
        for k, r in enumerate(rs):
            r["next"] = rs[k + 1] if k + 1 < len(rs) else None
            r["post"] = next((e for pi, e in posts if e["agent_id"] == r["id"] and pi > r["idx"]
                              and (r["next"] is None or pi < r["next"]["idx"])), None)
    for r in runs:                                          # pass 1: tuid-linked
        if r["post"] and r["post"].get("tuid"):
            r["pre"] = next((p for p in pres if p.get("tuid") == r["post"]["tuid"] and not p["_used"]), None)
            if r["pre"] is not None:
                r["pre"]["_used"] = True
    for r in runs:                                          # pass 2: same type, oldest unclaimed launch first
        # A launch always precedes its Start, and helpers sent in one message start together only AFTER every
        # launch was written (seen live: 20 s between a researcher's launch and its Start, its Post lost). So:
        # the oldest unclaimed same-type launch from the last PAIR_WINDOW before the Start — FIFO, like spawn order.
        if r["pre"] is None:
            r["pre"] = next((p for p in pres if not p["_used"] and str(p.get("agent_type") or "").lower() == r["type"]
                             and -3 <= (r["start"] - p["_t"]).total_seconds() <= PAIR_WINDOW), None)
            if r["pre"] is not None:
                r["pre"]["_used"] = True
    for r in runs:
        pre, post = r["pre"] or {}, r["post"]
        r["label"] = pre.get("label") or ""
        r["bg"] = bool(pre.get("bg")) or bool(post and post.get("status") == "async_launched")
        # a resumed run has no Pre/Post of its own: it is still the background helper it always was
        r["bg"] = r["bg"] or any(x["bg"] for x in by_aid[r["id"]] if x["idx"] < r["idx"])
        nxt = r["next"]
        stop_t = None
        own = next((e for si, e in stop_idx.get(r["id"], []) if si > r["idx"]
                    and (nxt is None or si < nxt["idx"])), None)
        if own:
            stop_t = own["_t"]
        elif post and post.get("status") not in (None, "async_launched"):
            stop_t = post["_t"]                              # a synchronous helper returned
        elif nxt is not None:
            stop_t = nxt["start"]                            # re-started under the same id: this run had ended
        elif not r["bg"]:
            # A foreground helper cannot outlive its turn: the session's next Stop closes it, however many
            # turns have run since (a new turn must not bring it back to life).
            stop_t = next((t for si, t in session_stops if si > r["idx"]), None)
        mt = None
        if stop_t is None and helper_mtime is not None:
            try:
                mt = helper_mtime(r["id"])
            except Exception:  # noqa: BLE001 — an unreadable transcript falls back to the event log
                mt = None
        if stop_t is None and mt is not None:
            # The helper's own transcript is the evidence. Fresh: it is working, however silent the hook log is
            # (background or foreground alike). Stale: a ghost, closed at its last write.
            if now - mt > HELPER_FRESH:
                stop_t = max(mt, r["start"])
        elif stop_t is None:
            # Launched but never stopped (e.g. killed by TaskStop: no SubagentStop, no Post). Gone when the
            # session moved on (a later UserPromptSubmit) and the helper itself was silent for 10 min, or when it
            # has been silent for longer than a session may stay "working".
            last_h = max([r["start"]] + [e["_t"] for e in evs[r["idx"] + 1:]
                                         if e.get("in_agent") == r["id"] or e.get("agent_id") == r["id"]])
            moved_on = any(i > r["idx"] and e.get("ev") == "UserPromptSubmit" for i, e in enumerate(evs))
            silent = now - last_h
            if (moved_on and silent > STALE_SILENT_HELPER) or silent > STALE_WORKING:
                stop_t = last_h
        if stop_t is None and mt is None and now - r["start"] > STALE_HELPER:
            stop_t = r["start"] + STALE_HELPER
        r["stop"] = stop_t
    return runs


def _group_helpers(runs: list[dict]) -> list[dict]:
    """One entry per agent_id (a resumed helper is ONE step, not two): first run's position and label,
    the latest run's state."""
    groups: dict[str, dict] = {}
    for r in runs:
        g = groups.get(r["id"])
        if g is None:
            groups[r["id"]] = {"id": r["id"], "type": r["type"], "agent": r["agent"], "label": r["label"],
                               "bg": r["bg"], "start": r["start"], "stop": r["stop"], "idx": r["idx"],
                               "last_idx": r["idx"], "resumed": False}
        else:
            g["resumed"] = True
            g["label"] = g["label"] or r["label"]
            g["stop"], g["last_idx"] = r["stop"], r["idx"]
    return sorted(groups.values(), key=lambda g: g["idx"])


def _is_anchor(e: dict) -> bool:
    """An event that starts a new task: a playbook command (typed, expanded, or via the Skill tool) or a WorkType."""
    if e.get("ev") == "WorkType":
        return True
    return e.get("ev") in _CMD_EVS and _norm_cmd(e.get("cmd")) in PLAYBOOKS


def _remaining_route(route: list[str], seen: list[str]) -> list[str]:
    p = 0
    for a in seen:
        for j in range(p, len(route)):
            if route[j] == a:
                p = j + 1
                break
    return route[p:]


def _analyse(sid: str, evs: list[dict], now: dt.datetime, cost_fn, helper_mtime_fn=None) -> dict:
    # a declaration made inside a helper is not the session's (the hook ignores it too): drop its type, area and plan_ref
    evs = [e for e in evs if not (e.get("ev") == "WorkType" and e.get("in_agent"))] or evs
    today0 = dt.datetime.combine(now.date(), dt.time())
    # --- lifecycle: working / idle / ended, and the working intervals for the timeline
    state = None                     # None until a prompt / activity / Stop tells otherwise
    intervals: list[list] = []       # [start, end|None]
    for e in evs:
        ev = e.get("ev")
        if ev in _PROMPT_EVS or (ev in _ACTIVITY_EVS and not (ev in ("PreToolUse", "PostToolUse") and e.get("in_agent"))):
            if state != "working":    # a prompt, or any activity after a Stop (tool use / helper spawn / WorkType)
                intervals.append([e["_t"], None])
            state = "working"
        elif ev in ("Stop", "SessionEnd"):
            if intervals and intervals[-1][1] is None:
                intervals[-1][1] = e["_t"]
            state = "idle"
    last_t = evs[-1]["_t"]
    # ended = the last real event is a SessionEnd (a late SubagentStop does not revive; a resume does)
    ended = next((e.get("ev") for e in reversed(evs) if e.get("ev") != "SubagentStop"), None) == "SessionEnd"
    stale = state == "working" and now - last_t > STALE_WORKING
    chief_working = state == "working" and not stale
    if state == "working" and intervals and intervals[-1][1] is None:
        intervals[-1][1] = last_t if stale else now
    last_end = next((e["_t"] for e in reversed(evs) if e.get("ev") in ("Stop", "SessionEnd")), None)

    # --- identity
    transcript = next((e["transcript"] for e in reversed(evs) if e.get("transcript")), None)
    cwd = next((e["cwd"] for e in evs if e.get("cwd") and not e.get("in_agent")), None) or next(
        (e["cwd"] for e in evs if e.get("cwd")), None)
    folder = _folder_of(transcript, cwd)
    hm = (lambda aid: helper_mtime_fn(sid, transcript, aid)) if helper_mtime_fn else None
    runs = _helpers(evs, now, hm)
    helpers = _group_helpers(runs)

    # --- current task window: since the latest playbook cmd / WorkType, widened back to its prompt run
    anchor = max((i for i, e in enumerate(evs) if _is_anchor(e)), default=-1)
    start_i = 0
    if anchor >= 0:
        start_i = anchor
        j = anchor - 1
        while j >= 0 and evs[j].get("ev") not in ("Stop", "SessionEnd"):
            if evs[j].get("ev") in _PROMPT_EVS:
                start_i = j                       # the prompt that began the run this anchor belongs to
            j -= 1
    win = evs[start_i:]
    win_helpers = [h for h in helpers if h["last_idx"] >= start_i]

    # --- type label
    cmd = None
    for e in reversed(win):
        if e.get("ev") in _CMD_EVS and _norm_cmd(e.get("cmd")) in PLAYBOOKS:
            cmd = _norm_cmd(e["cmd"])
            break
    wtype = next((e.get("type") for e in reversed(win) if e.get("ev") == "WorkType" and e.get("type")), None)
    if cmd:
        label, tag, ask = PLAYBOOKS[cmd][0], f"/{cmd}", False
    elif wtype:
        label, tag, ask = worktype_label(wtype), f"declared: {str(wtype).strip().lower()}", True
    else:
        types = {h["type"] for h in win_helpers}
        inferred = next((lab for t, lab in INFER_ORDER if t in types), None)
        if inferred is None and types and all(AGENT_TYPE_ID.get(t) == "scout" for t in types):
            inferred = "Quick question"
        if inferred:
            label, tag, ask = inferred, "type guessed from activity", True
        else:
            label, tag, ask = "Orchestrator working", "no type declared", True

    area_arg = next((str(e["area"]).strip().lower() for e in reversed(win) if e.get("ev") == "WorkType" and e.get("area")), None)
    plan_ref = next((str(e["plan_ref"]) for e in reversed(win) if e.get("ev") == "WorkType" and e.get("plan_ref")), None)

    # --- steps
    any_helper_working = any(h["stop"] is None and h["agent"] for h in helpers)
    steps = [{"agent": "op", "what": f"ran /{cmd}" if cmd else "asked", "state": "done"}]
    if chief_working:
        steps.append({"agent": "chief", "state": "working",
                      "what": "waiting on helpers" if any(h["stop"] is None for h in win_helpers) else "working"})
    else:
        steps.append({"agent": "chief", "state": "done", "what": "replied"})
    for h in win_helpers:
        if not h["agent"]:
            continue                                  # an agent type with no creature: counted nowhere, drawn nowhere
        steps.append({"agent": h["agent"], "state": "done" if h["stop"] else "working",
                      "what": (h["label"] or AGENTS[h["agent"]][0].lower() + " at work") + (" (resumed)" if h["resumed"] else "")})
    if cmd:
        seen = [s["agent"] for s in steps[1:]]
        for a in _remaining_route(PLAYBOOKS[cmd][1], seen):
            steps.append({"agent": a, "state": "wait", "what": ""})
    steps = steps[-MAX_STEPS:]

    t0 = win[0]["_t"] if win else evs[0]["_t"]
    working_helpers: dict[str, int] = {}
    for h in helpers:
        if h["stop"] is None and h["agent"]:
            working_helpers[h["agent"]] = working_helpers.get(h["agent"], 0) + 1
    live = chief_working or any_helper_working
    # Working ONLY because helpers are: the main conversation finished its turn, helpers carry on (background).
    live_helpers = [{"agent": h["agent"], "name": AGENTS[h["agent"]][0], "label": h["label"], "bg": h["bg"]}
                    for h in helpers if h["stop"] is None and h["agent"]]
    via_helpers = bool(live and not chief_working and live_helpers)
    idle_since = last_end or last_t
    # waiting (v2 attention bar): idle, and the last MAIN-session event is a Stop within WAITING_WINDOW
    # (the session finished its turn and is waiting for the person). Helper events do not count.
    last_main = next((e for e in reversed(evs) if not e.get("in_agent")
                      and e.get("ev") not in ("SubagentStart", "SubagentStop")), None)
    waiting = bool(not live and last_main is not None and last_main.get("ev") == "Stop"
                   and now - last_main["_t"] <= WAITING_WINDOW)
    hidden = ended or (not live and now - last_t > IDLE_HIDE)

    # --- timeline segments (hours since midnight today): Chief time opus, helper intervals their tier
    def hrs(t):
        return max(0.0, (min(t, now) - today0).total_seconds() / 3600.0)
    segs = []
    prev_end = None
    for a, b in intervals:
        b = b or now
        if b <= today0:
            continue
        if prev_end is not None and a > prev_end:
            segs.append([hrs(prev_end), hrs(a), "idle"])
        segs.append([hrs(max(a, today0)), hrs(b), "opus"])
        prev_end = b
    for h in runs:
        if not h["agent"]:
            continue
        b = h["stop"] or now
        if b <= today0:
            continue
        segs.append([hrs(max(h["start"], today0)), hrs(b), AGENTS[h["agent"]][1]])
    segs = [s for s in segs if s[1] > s[0] or s[2] != "idle"]
    return {
        "sid": sid, "project": project_label(folder, cwd), "folder": folder, "transcript": transcript,
        "type": label, "tag": tag, "ask": ask, "playbook": cmd,
        "status": "working" if live else "idle", "chief_working": chief_working,
        "via_helpers": via_helpers, "live_helpers": live_helpers,
        "cwd": cwd, "waiting": waiting, "area_arg": area_arg, "plan_ref": plan_ref,
        "since": _hhmm(t0), "idle_since": _hhmm(idle_since), "first_t": evs[0]["_t"], "last_t": last_t,
        "steps": steps, "working_helpers": working_helpers, "hidden": hidden, "segs": segs,
        "substantive": bool(helpers) or anchor >= 0,
    }


# --- the whole panel ---------------------------------------------------------------------------------
def build_state(events: list[dict], now: dt.datetime, exclude_sids=(), cost_fn=None, title_fn=None, helper_mtime_fn=None) -> dict:
    exclude = set(exclude_sids or ())
    by_sid: dict[str, list[dict]] = {}
    for e in events:
        if e.get("sid") and isinstance(e.get("_t"), dt.datetime):
            by_sid.setdefault(e["sid"], []).append(e)
        elif e.get("sid") and _ts(e.get("ts")):
            e = dict(e, _t=_ts(e["ts"]))
            by_sid.setdefault(e["sid"], []).append(e)
    for evs in by_sid.values():
        evs.sort(key=lambda e: e["_t"])
    cost_fn = cost_fn or (lambda sid, transcript: 0)
    today0 = dt.datetime.combine(now.date(), dt.time())

    sessions, rows = [], []
    counts: dict[str, int] = {}
    total_cost = 0
    for sid, evs in by_sid.items():
        if sid in exclude:
            continue
        s = _analyse(sid, evs, now, cost_fn, helper_mtime_fn)
        if _excluded_folder(s["folder"]):
            continue
        # A `claude -p` call from Atlas's own agents (its session id is recorded only AFTER the call returns)
        # shows up as a few seconds of events with no helper, no playbook and no WorkType. Not a task.
        # Only a FINISHED one is hidden: a session that is working now is always shown (a long plain turn
        # has no such events until its Stop, and event time alone cannot tell it from a CLI call).
        if not s["substantive"] and not s["chief_working"] and s["last_t"] - s["first_t"] < MIN_LIFETIME:
            continue
        cost = 0
        if s["last_t"] >= today0 or s["segs"]:       # only today's sessions are priced (a transcript read costs)
            try:
                cost = int(cost_fn(sid, s["transcript"]) or 0)
            except Exception:  # noqa: BLE001 — a transcript we can't read must not take the panel down
                cost = 0
        s["cost"] = cost
        s["title"] = None
        if title_fn:
            try:
                s["title"] = title_fn(sid, s["transcript"]) or None
            except Exception:  # noqa: BLE001 — a missing title just falls back to type · project
                s["title"] = None
        if s["last_t"] >= today0 or s["segs"]:
            total_cost += cost
            if s["segs"]:
                rows.append({"sid": sid, "label": s["title"] or s["type"], "type": s["type"], "cost": cost, "segs": s["segs"],
                             "start": min(g[0] for g in s["segs"])})
        if s["hidden"]:
            continue
        if s["chief_working"]:
            counts["chief"] = counts.get("chief", 0) + 1
        for a, n in s["working_helpers"].items():
            counts[a] = counts.get(a, 0) + n
        sessions.append(s)

    sessions.sort(key=lambda s: (s["status"] != "working", s["first_t"]))
    rows.sort(key=lambda r: r["start"])
    out_sessions = [{k: s[k] for k in ("sid", "title", "project", "type", "tag", "ask", "playbook", "status", "since",
                                       "idle_since", "cost", "steps", "cwd", "waiting", "area_arg", "plan_ref",
                                       "via_helpers", "live_helpers")} for s in sessions]
    all_segs = [g for r in rows for g in r["segs"]]
    ax_start = int(min([g[0] for g in all_segs] + [now.hour + now.minute / 60.0 - 1]))
    ax_end = max(ax_start + 2, int(now.hour + now.minute / 60.0) + 1)
    return {
        "ok": True,
        "now": now.isoformat(timespec="seconds"),
        "working": sum(1 for s in sessions if s["status"] == "working"),
        "counts": counts,
        "active": sum(counts.get(k, 0) for k in counts if k != "op"),
        "cost": total_cost,
        "sessions": out_sessions,
        "timeline": {"start": ax_start, "end": min(ax_end, 24),
                     "rows": [{k: r[k] for k in ("sid", "label", "type", "cost", "segs")} for r in rows]},
    }


def snapshot(day_dir=None, jobs_dir=None, now: dt.datetime | None = None) -> dict:
    """What GET /api/team-live returns. Never raises."""
    try:
        now = now or dt.datetime.now().replace(microsecond=0)
        events = load_events(day_dir or team_dir(), now)
        exclude = atlas_job_sids(jobs_dir) | atlas_known_sids()
        return build_state(events, now, exclude, session_cost, session_title, helper_transcript_mtime)
    except Exception as exc:  # noqa: BLE001 — a panel never hard-fails the cockpit
        return {"ok": False, "error": str(exc)}
