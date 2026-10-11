"""Atlas v2 hook: ONE script for every Claude Code hook event (docs/v2/contracts.md §6).

Logger + verdict capture + guards (rework budget, done-lock, quick-question lock, publish gate) + grant-rework +
allow-publish + cost capture + areas/look check. Registered SYNC so a guard can deny. Reads `atlas_config.json` next to
itself (override the directory with ATLAS_HOOKS_DIR), never imports the Atlas repo.

Fail open, always: any problem ends in os._exit(0) with an empty stdout. Stdout carries only the
documented JSON (PreToolUse permissionDecision, Stop decision, PostToolUse additionalContext).
Metadata only: never prompt text, never tool input/output text beyond the fields the contract lists.
"""
import datetime as dt
from collections import Counter
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.environ.get("ATLAS_HOOKS_DIR") or HERE
DEFAULT_LEDGER = r"D:\AI\atlas-data\ledger"
CHECK_FILES = ("areas.yaml", "look.css")
MAX_STR = 200

DEFAULT_WORKFLOWS = {
    "default_budget": 2, "builders": ["implementer", "drafter", "researcher", "sweeper"],
    "reviewers": ["reviewer", "senior-reviewer"], "types": {}, "aliases": {}, "playbooks": [],
}


# ------------------------------------------------------------------ config, paths

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return default


def load_config():
    cfg = _load_json(os.path.join(CONFIG_DIR, "atlas_config.json"), {})
    return cfg if isinstance(cfg, dict) else {}


def _areas_mod():
    for p in (HERE, os.path.join(HERE, "..", "cockpit", "ledger2")):
        if os.path.isfile(os.path.join(p, "areas.py")):
            if p not in sys.path:
                sys.path.insert(0, p)
            break
    import areas  # noqa: PLC0415
    return areas


def _chain_mod():
    for p in (HERE, os.path.join(HERE, "..", "cockpit", "ledger2")):
        if os.path.isfile(os.path.join(p, "ledger_chain.py")):
            if p not in sys.path:
                sys.path.insert(0, p)
            break
    import ledger_chain  # noqa: PLC0415
    return ledger_chain


def norm_path(p):
    return os.path.normcase(os.path.abspath(str(p)))


def under(path, root):
    path, root = norm_path(path), norm_path(root)
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def clip(o):
    if isinstance(o, str):
        return o[:MAX_STR]
    if isinstance(o, list):
        return [clip(x) for x in o]
    if isinstance(o, dict):
        return {k: clip(v) for k, v in o.items()}
    return o


def type_info(wf, typ):
    t = (wf.get("types") or {}).get(typ)
    if isinstance(t, dict):
        return {"budget": t.get("budget"), "needs_check": bool(t.get("needs_check", True))}
    return {"budget": wf.get("default_budget", 2), "needs_check": True}


def min_budget(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def task_resolved(S):
    """The task's most recent reviewer verdict is READY, it covers the latest main-session version (helpers edit in
    their own worktrees, so `version` counts only edits made in the main session), and that reviewer was LAUNCHED after
    the latest NEEDS CHANGES arrived (`nc_at` = how many helpers had been launched when it arrived; a reviewer started
    in parallel with the failing one cannot clear it). A task opened by a carry has no verdict yet, so it stays
    unresolved (the double-declaration escape stays closed). Honest limits: a READY on an unrelated artifact, or from a
    reviewer told to answer READY, still counts."""
    if S.get("last_verdict") != "READY" or S.get("last_verified", 0) < S.get("version", 0):
        return False
    nc_at, seq = S.get("nc_at", 0), S.get("last_verdict_seq")
    return nc_at == 0 or (seq is not None and seq > nc_at)


def nc_count(S):
    """NEEDS CHANGES rounds: distinct failing versions of this task plus rounds carried over a type switch."""
    return S.get("carried_nc", 0) + len(S["nc_versions"])


class Ctx:
    """Everything one hook run needs."""

    def __init__(self, d):
        self.d = d
        self.cfg = load_config()
        self.mode = "enforce" if self.cfg.get("mode") == "enforce" else "record"
        self.ledger = os.environ.get("ATLAS_LEDGER2_DIR") or self.cfg.get("ledger_dir") or DEFAULT_LEDGER
        self.ev = d.get("hook_event_name") or ""
        self.sid = str(d.get("session_id") or "")
        self.cwd = d.get("cwd") or ""
        self.agent_id = d.get("agent_id") or None
        self.agent_type = d.get("agent_type") or None
        self.tool = d.get("tool_name") or ""
        ti = d.get("tool_input")
        self.ti = ti if isinstance(ti, dict) else {}
        self.entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        self._wf = None
        self._areas = None

    @property
    def wf(self):
        if self._wf is None:
            wf = None
            try:
                a = _areas_mod()
                p = self.cfg.get("workflows_file")
                if p and os.path.isfile(p):
                    with open(p, "r", encoding="utf-8") as fh:
                        wf = a.mini_yaml(fh.read())
                if not (isinstance(wf, dict) and isinstance(wf.get("types"), dict)):
                    wf = None
            except Exception:  # noqa: BLE001
                wf = None
            if wf is None:
                wf = _load_json(os.path.join(CONFIG_DIR, "workflows.json"), None)
            if not (isinstance(wf, dict) and isinstance(wf.get("types"), dict)):
                wf = dict(DEFAULT_WORKFLOWS)
            base = dict(DEFAULT_WORKFLOWS)
            base.update({k: v for k, v in wf.items() if v is not None or k == "types"})
            self._wf = base
        return self._wf

    @property
    def areas(self):
        if self._areas is None:
            try:
                a = _areas_mod()
                p = self.cfg.get("areas_file")
                lst = a.load_areas(p) if p and os.path.isfile(p) else []
                if not lst:
                    j = _load_json(os.path.join(CONFIG_DIR, "areas.json"), None)
                    lst = j.get("areas") if isinstance(j, dict) else (j or [])
                self._areas = lst or []
            except Exception:  # noqa: BLE001
                self._areas = []
        return self._areas

    def area_for(self, area_arg=None, playbook=None):
        try:
            return _areas_mod().area_for(self.areas, self.cwd, area_arg, playbook)
        except Exception:  # noqa: BLE001
            return None

    @property
    def builders(self):
        return [str(x).lower() for x in self.wf.get("builders") or []]

    @property
    def reviewers(self):
        return [str(x).lower() for x in self.wf.get("reviewers") or []]

    def playbook_names(self):
        names = set(self.wf.get("playbooks") or [])
        for a in self.areas:
            names.update(a.get("playbooks") or [])
        return names

    def norm_type(self, t):
        t = (t or "").lower()
        return (self.wf.get("aliases") or {}).get(t, t)

    def is_exempt(self, path):
        p = norm_path(path)
        base = os.path.basename(p)
        if base.endswith("build-log.md") or base.endswith(".notes.md"):
            return True
        dirs = list(self.cfg.get("exempt_dirs") or [])
        dirs += [os.path.join(os.path.expanduser("~"), ".claude"), r"D:\AI\atlas-data"]
        if self.cfg.get("exempt_temp", True):
            dirs.append(tempfile.gettempdir())
        return any(under(p, r) for r in dirs if r)

    def is_protected(self, path):
        anchors = [r"D:\AI\atlas-anchor", os.environ.get("ATLAS_ANCHOR_REPO"), self.cfg.get("anchor_repo")]
        return (under(path, self.ledger) or under(path, CONFIG_DIR) or under(path, HERE)
                or any(under(path, a) for a in anchors if a))


# ------------------------------------------------------------------ lock, state, events

class LockBusy(Exception):
    """The ledger lock could not be taken in time (Txn strict mode)."""


class Txn:
    """read state -> emit events -> write state, all under the ledger's append lock (contracts §1)."""

    def __init__(self, H, tries=60, strict=False, light=False):
        """tries x 50 ms = how long to wait for the lock; strict = raise LockBusy instead of going on without it;
        light = never rebuild a missing state cache (a fresh, unsaved state is used) and skip the tamper check."""
        self.H = H
        self.events = []
        self.fh = None
        self.got = False
        self.state = None
        self.tries = tries
        self.strict = strict
        self.light = light
        self.nosave = False

    # lock ---------------------------------------------------------
    def __enter__(self):
        H = self.H
        try:
            os.makedirs(H.ledger, exist_ok=True)
            self.fh = open(os.path.join(H.ledger, ".lock"), "a+b")
            self.fh.seek(0)
            import msvcrt  # noqa: PLC0415
            for _ in range(self.tries):  # x 50 ms (60 = ~3 s), then proceed unlocked (lines then carry "nolock")
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    self.got = True
                    break
                except OSError:
                    time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        if self.strict and not self.got:
            if self.fh is not None:
                self.fh.close()
            raise LockBusy()
        if self.light:
            S = cached_state(H)
            self.nosave = S is None
            self.state = fresh_state(H.sid)
            if S is not None:
                self.state.update(S)
            prune_pending(self.state)
            return self
        self.state = load_state(H)
        prune_pending(self.state)
        self._tamper_check()
        return self

    def __exit__(self, *exc):
        try:
            if self.events:
                append_lines(self.H, self.events, nolock=not self.got)
            if self.state is not None and not self.nosave:
                prune_pending(self.state)
                save_state(self.H, self.state)
        finally:
            if self.fh is not None:
                if self.got:
                    try:
                        import msvcrt  # noqa: PLC0415
                        self.fh.seek(0)
                        msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:  # noqa: BLE001
                        pass
                self.fh.close()
        return False

    # events -------------------------------------------------------
    def emit(self, e):
        H = self.H
        rec = {"v": 2, "t": now_iso(), "ev": e["ev"], "sid": H.sid, "cwd": H.cwd}
        if H.agent_id:
            rec["agent_id"] = H.agent_id
        if H.agent_type:
            rec["agent_type"] = H.agent_type
        rec["mode"] = H.mode
        for k, v in e.items():
            if k != "ev":
                rec[k] = v
        self.events.append(rec)
        apply_event(self.state, rec, H.wf)
        return rec

    def _tamper_check(self):
        H = self.H
        p = os.path.join(H.ledger, "state", "last_mode.json")
        lm = _load_json(p, None)
        if not isinstance(lm, dict):
            mode = last_mode_event(H) or H.mode      # no mode event yet: baseline = the current mode
            lm = {"mode": mode, "tamper_for": None}
            _write_json(p, lm)
        if lm.get("mode") != H.mode and lm.get("tamper_for") != H.mode:
            self.emit({"ev": "tamper", "found": H.mode, "expected": lm.get("mode")})
            lm["tamper_for"] = H.mode
            _write_json(p, lm)


PAIR_MAX_AGE_S = 30 * 60


def prune_pending(S):
    """Drop unclaimed pending launches that are stale (> 30 min) or belong to another task."""
    now = dt.datetime.now().astimezone()
    for k, p in list(S["pending"].items()):
        if p.get("agent_id"):
            continue
        try:
            age = (now - dt.datetime.fromisoformat(p["t"])).total_seconds()
        except Exception:  # noqa: BLE001
            age = 0
        if age > PAIR_MAX_AGE_S or (p.get("task") != S.get("task")):
            S["pending"].pop(k, None)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    for _ in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)


def _sha(line_bytes):
    return hashlib.sha256(line_bytes.rstrip(b"\r\n")).hexdigest()


def _last_line(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    parts = data.split(b"\n")
    if not data.endswith(b"\n") and len(parts) > 1:
        parts = parts[:-1]                        # a line still being written by another process
    lines = [x for x in parts if x.strip()]
    return lines[-1].rstrip(b"\r") if lines else None


def _prev_hash(H, day):
    """Hash chain (contracts §6.4 rule A): sha256 of the day file's last line, else of the nearest earlier
    day file's last line, else "genesis". Same rule as cockpit/ledger2/store.py."""
    ln = _last_line(os.path.join(H.ledger, f"events-{day}.jsonl"))
    if ln is not None:
        return _sha(ln)
    try:
        names = sorted(n for n in os.listdir(H.ledger) if re.fullmatch(r"events-\d{4}-\d\d-\d\d\.jsonl", n))
    except OSError:
        names = []
    for n in reversed([n for n in names if n[len("events-"):-len(".jsonl")] < day]):
        ln = _last_line(os.path.join(H.ledger, n))
        if ln is not None:
            return _sha(ln)
    return "genesis"


def append_lines(H, events, nolock=False):
    for e in events:
        day = str(e["t"])[:10]
        e = dict(e, prev=_prev_hash(H, day))
        if nolock:
            e["nolock"] = True
        line = json.dumps(clip(e), ensure_ascii=False)
        if len(line.encode("utf-8")) >= 4000:
            line = json.dumps({k: v for k, v in clip(e).items() if not isinstance(v, (list, dict))},
                              ensure_ascii=False)
        fd = os.open(os.path.join(H.ledger, f"events-{day}.jsonl"),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)


def safe_sid(sid):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:80] or "unknown"


def state_path(H):
    return os.path.join(H.ledger, "state", safe_sid(H.sid) + ".json")


def fresh_state(sid):
    return {"sid": sid, "task": None, "n": 0, "type": None, "area": None, "open": False, "needs_check": True,
            "version": 0, "last_verified": 0, "other_v": 0, "edits": 0, "nc_versions": [], "carried_nc": 0,
            "needs_changes": 0, "tuid_aid": {}, "budget": None, "escalated": False, "granted": 0, "pending": {},
            "agents": {}, "captured": [], "ended_ids": [], "sent_back_for": None, "playbook_last": None,
            "task_t": None, "spawns": 0, "reminded": None, "plan_ref": None, "last_verdict": None,
            "last_verdict_seq": None, "nc_at": 0, "spawn_seq": 0, "pub_n": 0, "pub_exp": None}


def day_files_desc(H, max_days=30):
    try:
        names = sorted((n for n in os.listdir(H.ledger) if re.fullmatch(r"events-\d{4}-\d\d-\d\d\.jsonl", n)),
                       reverse=True)
    except OSError:
        return []
    return [os.path.join(H.ledger, n) for n in names[:max_days]]


def last_mode_event(H):
    for p in day_files_desc(H):
        last = None
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"ev": "mode"' in line:
                        try:
                            last = json.loads(line).get("to")
                        except Exception:  # noqa: BLE001
                            pass
        except OSError:
            continue
        if last:
            return last
    return None


def rebuild_state(H):
    """Replay this session's events from the day files (<= 30 days back) into a fresh state."""
    evs = []
    needle = '"sid": ' + json.dumps(H.sid)
    for p in reversed(day_files_desc(H)):
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if needle in line:
                        try:
                            evs.append(json.loads(line))
                        except Exception:  # noqa: BLE001
                            pass
        except OSError:
            continue
    S = fresh_state(H.sid)
    for e in evs:
        if isinstance(e, dict):
            apply_event(S, e, H.wf)
    return S


def load_state(H):
    S = _load_json(state_path(H), None)
    if isinstance(S, dict) and S.get("sid") == H.sid:
        base = fresh_state(H.sid)
        base.update(S)
        return base
    try:
        return rebuild_state(H)
    except Exception:  # noqa: BLE001
        return fresh_state(H.sid)


def save_state(H, S):
    S["needs_changes"] = nc_count(S)
    for k in ("captured", "ended_ids"):
        S[k] = S[k][-200:]
    if len(S["pending"]) > 100:
        for k in list(S["pending"])[:-100]:
            S["pending"].pop(k, None)
    _write_json(state_path(H), S)


def apply_event(S, e, wf):
    ev = e.get("ev")
    if ev in ("work_type", "playbook"):
        typ = "playbook" if ev == "playbook" else (e.get("type") or "undeclared")
        info = type_info(wf, typ)
        S["task"] = e.get("task")
        try:
            S["n"] = int(str(e.get("task")).rsplit("-", 1)[1])
        except Exception:  # noqa: BLE001
            S["n"] = S.get("n", 0) + 1
        S.update(type=typ, area=e.get("area"), open=True, version=0, last_verified=0, other_v=0, edits=0,
                 sent_back_for=None, needs_check=info["needs_check"], task_t=e.get("t"), spawns=0, reminded=None,
                 plan_ref=e.get("plan_ref"), last_verdict=None, last_verdict_seq=None)
        if e.get("carried"):
            S["carried_nc"] = nc_count(S)
            S["nc_versions"] = []
            S["budget"] = min_budget(S.get("budget"), info["budget"])
        else:
            S.update(nc_versions=[], carried_nc=0, escalated=False, granted=0, budget=info["budget"], nc_at=0)
        if ev == "playbook":
            S["playbook_last"] = {"cmd": e.get("cmd"), "t": e.get("t")}
    elif ev == "task_done":
        if e.get("task") == S.get("task"):
            S["open"] = False
    elif ev == "edit":
        if e.get("task") == S.get("task"):
            S["version"] = int(e.get("version") or S["version"] + 1)
            S["edits"] += 1
            if os.path.basename(str(e.get("path") or "")).lower() not in CHECK_FILES:
                S["other_v"] = S["version"]
    elif ev == "verdict":
        if e.get("agent_id") and e["agent_id"] not in S["captured"]:
            S["captured"].append(e["agent_id"])
        if e.get("task") == S.get("task"):
            v, ver = e.get("verdict"), e.get("version")
            key = e.get("agent_id")
            who = (S["agents"].get(key) or S["agents"].get(S["tuid_aid"].get(key)) or S["pending"].get(key) or {})
            S["last_verdict"], S["last_verdict_seq"] = v, who.get("seq")
            if v == "NEEDS_CHANGES":
                S["nc_at"] = S.get("spawn_seq", 0)
            if v == "READY":
                if ver is not None:      # unknown version never satisfies the done-lock
                    S["last_verified"] = max(S["last_verified"], int(ver))
            elif v == "NEEDS_CHANGES":
                key = int(ver) if ver is not None else "?" + str(e.get("agent_id"))
                if key not in S["nc_versions"]:
                    S["nc_versions"].append(key)
            elif v == "ESCALATE":
                S["escalated"] = True
    elif ev == "check":
        if e.get("task") == S.get("task") and e.get("ok") and S["other_v"] <= S["last_verified"]:
            S["last_verified"] = max(S["last_verified"], int(e.get("version") or 0))
    elif ev == "rework_granted":
        S["granted"] += int(e.get("n") or 1)
        S["escalated"] = False
    elif ev == "reminder":
        S["reminded"] = e.get("task")
    elif ev == "helper_start":
        if e.get("task") == S.get("task"):
            S["spawns"] = S.get("spawns", 0) + 1
        S["spawn_seq"] = S.get("spawn_seq", 0) + 1
        S["pending"][e.get("tuid") or ("h%d" % len(S["pending"]))] = {"seq": S["spawn_seq"],
            "helper": e.get("helper"), "version": int(e.get("version") or 0), "t": e.get("t"),
            "bg": bool(e.get("bg")), "task": e.get("task"), "agent_id": None,
            "label": str(e.get("label") or "")[:60]}
    elif ev == "helper_paired":
        aid, tuid = e.get("agent_id"), e.get("tuid")
        if tuid in S["pending"]:
            S["pending"][tuid]["agent_id"] = aid
        p = S["pending"].get(tuid) or {}
        S["agents"][aid] = {"helper": e.get("helper"), "version": p.get("version"), "tuid": tuid,
                            "task": p.get("task", S.get("task")), "seq": p.get("seq")}
        if tuid:
            S["tuid_aid"][tuid] = aid
        S.setdefault("helper_of", {})[aid] = e.get("helper")
        for k in ("helper_of", "agents", "tuid_aid"):
            while len(S[k]) > 200:
                S[k].pop(next(iter(S[k])))
    elif ev == "helper_end":
        aid = e.get("agent_id")
        if aid and aid not in S["ended_ids"]:
            S["ended_ids"].append(aid)
        info = S["agents"].get(aid)          # the map is never emptied by helper_end
        if info and info.get("tuid"):
            S["pending"].pop(info["tuid"], None)
        else:
            for k, p in list(S["pending"].items()):
                if p.get("agent_id") == aid:
                    S["pending"].pop(k, None)
    elif ev == "sendback":
        S["sent_back_for"] = e.get("version")
    elif ev == "publish_grant":
        try:
            live = S.get("pub_n", 0) if dt.datetime.fromisoformat(S["pub_exp"]) > dt.datetime.fromisoformat(e["t"]) else 0
        except Exception:  # noqa: BLE001
            live = 0
        S["pub_n"] = min(PUBLISH_GRANT_MAX, live + int(e.get("n") or 1))
        S["pub_exp"] = e.get("expires")
    elif ev == "publish_allowed":
        S["pub_n"] = max(0, S.get("pub_n", 0) - 1)
    elif ev == "session_end":
        S["pub_n"] = 0                  # a grant never outlives its session


# ------------------------------------------------------------------ outputs

def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def refuse(H, tx, guard, reason, **who):
    """Log a refusal; deny only in enforce mode. `who` (helper, label, tuid) records which spawn was refused."""
    would = H.mode != "enforce"
    rec = {"ev": "refusal", "task": tx.state.get("task"), "guard": guard, "would": would,
           "detail": reason[:MAX_STR]}
    rec.update({k: v for k, v in who.items() if v})
    tx.emit(rec)
    return None if would else deny(reason)


def tripped(S):
    b = S.get("budget")
    if not S.get("open"):
        return False
    if S.get("escalated"):
        return True
    return b is not None and nc_count(S) > b + S.get("granted", 0)


# ------------------------------------------------------------------ patterns

CLAUDE_PATH_RE = re.compile(r"[\\/]claude(?:\.exe)?(?=[\s\"']|$)", re.I)
CLAUDE_CMD_RE = re.compile(r"(?:^|[;&|(]\s*|\s)(?<![.\/\\\w])claude(?:\.exe)?(?=\s)", re.I)
CLAUDE_FLAG_RE = re.compile(r"\s(-p|--print|--resume)\b")
TEAM_RE = re.compile(r"^\s*:\s*team-([a-z]+)\b(.*)$", re.I | re.S)
TYPE_RE = re.compile(r"^\s*([a-z][a-z0-9-]{0,30})(?:\s+([a-z][a-z0-9-]{0,15}))?\s*$", re.I)
# optional trailing `task:<id>` (a Plan item: calendar event, series occurrence, or task id). Strict format, no free text.
# Parsed by splitting words (never a backtracking regex: this runs under the ledger lock); stored exactly as validated.
MAX_TEAM_TYPE_ARGS = 120
PLAN_REF_RE = re.compile(r"^(?:evt_[0-9a-f]{8}(?:@\d{4}-\d\d-\d\d)?|t_[0-9a-f]{8}|[A-Za-z0-9][A-Za-z0-9._-]{0,40}/[A-Za-z0-9][A-Za-z0-9._-]{0,60})$")
VERDICT_RE = re.compile(r"^\W*(?:verdict\W*)?(READY|NEEDS[ _-]CHANGES|ESCALATE(?: TO OPERATOR)?)\b", re.I)
GRANT_PROMPT_RE = re.compile(r"^\s*/grant-rework\b(?:\s+(\d{1,2}))?")
ACK_PROMPT_RE = re.compile(r"^\s*/ack-break(?:\s+([A-Za-z0-9_.:-]{1,80}))?\s*$")
USER_ONLY_RE = re.compile(r"^\s*/(?:ack-break|grant-rework|allow-publish)(?![\w-])", re.I)
FORWARD_TOOLS = ("CronCreate", "ScheduleWakeup", "SendMessage")
FORWARD_KEYS = ("prompt", "message", "content", "text", "body", "summary")
SLASH_RE = re.compile(r"^/([A-Za-z0-9:_-]{1,60})")


def forged_grant(cmd):
    """A command that invokes claude with -p/--print/--resume (by name or by path). Mentions of
    /grant-rework alone (git messages, greps) pass. Linear: the last flag is found once, then the first
    claude call is compared with it."""
    last_flag = -1
    for m in CLAUDE_FLAG_RE.finditer(cmd):
        last_flag = m.start()
    if last_flag < 0:
        return False
    for rx in (CLAUDE_CMD_RE, CLAUDE_PATH_RE):
        m = rx.search(cmd)
        if m and m.end() <= last_flag:
            return True
    return False


def parse_verdict(text):
    if not isinstance(text, str):
        return None
    lines = [ln for ln in text.splitlines()]
    first = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first is None:
        return None
    m = VERDICT_RE.match(lines[first].strip())
    if not m:
        return None
    word = m.group(1).upper()
    verdict = "READY" if word == "READY" else ("ESCALATE" if word.startswith("ESCALATE") else "NEEDS_CHANGES")
    findings = sum(1 for ln in lines[first + 1:] if re.match(r"^(?:[-*]\s|\d+\.)", ln))
    return verdict, findings


def split_plan_ref(rest):
    """`<type> [area] [task:<id>]` arguments -> (the arguments without the task token, plan_ref or None, problem or None).
    Linear: the length is capped first, then the words are split (no backtracking regex under the ledger lock).
    The id is stored exactly as validated; a malformed one is dropped (problem "team-type: bad plan ref")."""
    if len(rest) > MAX_TEAM_TYPE_ARGS:
        return rest, None, "team-type: bad arguments"
    words = rest.split()
    if words and words[-1][:5].lower() == "task:":
        cand = words.pop()[5:]
        rest = " ".join(words)
        if PLAN_REF_RE.match(cand):
            return rest, cand, None
        return rest, None, "team-type: bad plan ref"
    return rest, None, None


# ------------------------------------------------------------------ task opening

def open_task(H, tx, kind, typ, area_arg=None, cmd=None, implicit=False, plan_ref=None):
    S = tx.state
    carried = False
    if S["open"]:
        b = S.get("budget")
        # Carries only when escalated, or when the rework budget is spent and the task is not resolved (its latest
        # verdict is not a READY covering the latest version). Under budget every declaration starts fresh.
        if (b is not None and nc_count(S) > 0 and nc_count(S) >= b and not task_resolved(S)) or S.get("escalated"):
            carried = True
    tid = "%s-%d" % (H.sid[:8], S["n"] + 1)
    e = {"ev": kind, "task": tid, "area": H.area_for(area_arg, cmd if kind == "playbook" else None)}
    if kind == "playbook":
        e["cmd"] = cmd
    else:
        e["type"] = typ
    if plan_ref:
        e["plan_ref"] = plan_ref
    if carried:
        e["carried"] = True
    if implicit:
        e["implicit"] = True
    tx.emit(e)
    if carried:
        # not a denial in either mode: logged as would:true so the "enforced" count stays honest
        tx.emit({"ev": "refusal", "task": tid, "guard": "rework", "would": True,
                 "detail": "carry-over, not a denial: budget carried over a type switch (%d NEEDS CHANGES)"
                           % nc_count(S)})


def _just_opened(S, seconds=60):
    """True when the open task was opened less than `seconds` ago (an accidental double declaration)."""
    try:
        a = dt.datetime.fromisoformat(S["task_t"])
        return 0 <= (dt.datetime.now().astimezone() - a).total_seconds() <= seconds
    except Exception:  # noqa: BLE001
        return False


def open_playbook(H, tx, cmd):
    S = tx.state
    last = S.get("playbook_last")
    if last and last.get("cmd") == cmd and S.get("open"):
        try:
            a = dt.datetime.fromisoformat(last["t"])
            if (dt.datetime.now().astimezone() - a).total_seconds() <= 10:
                return
        except Exception:  # noqa: BLE001
            pass
    open_task(H, tx, "playbook", "playbook", cmd=cmd)


# ------------------------------------------------------------------ handlers

def h_session_start(H):
    with Txn(H) as tx:
        tx.emit({"ev": "session_start", "source": H.d.get("source"), "entry": H.entry,
                 "model": H.d.get("model"), "transcript": H.d.get("transcript_path")})
    return None


def h_session_end(H):
    try:
        cost_pass(H)
    except Exception:  # noqa: BLE001
        log_error()
    with Txn(H) as tx:
        tx.emit({"ev": "session_end", "reason": H.d.get("reason")})
    return None


def h_prompt(H):
    p = (H.d.get("prompt") or "").lstrip()
    m = SLASH_RE.match(p)
    a = ACK_PROMPT_RE.match(p) if not H.agent_id else None
    # the chain scan reads the whole ledger: do it BEFORE the lock is taken; only the emit happens under it
    verdict, target = (None, None)
    if a and entry_interactive(H):
        verdict, target = ack_decision(H, a.group(1))
    out = None
    with Txn(H) as tx:
        e = {"ev": "prompt"}
        if m:
            e["cmd"] = m.group(1)
        tx.emit(e)
        pg = PUBLISH_PROMPT_RE.match(p)
        if pg and not H.agent_id:
            if not entry_interactive(H):
                tx.emit({"ev": "ignored", "what": "publish grant from non-interactive entry"})
            else:
                n = min(PUBLISH_GRANT_MAX, max(1, int(pg.group(1) or 1)))
                exp = (dt.datetime.now().astimezone() + dt.timedelta(minutes=PUBLISH_GRANT_MINUTES)).isoformat(timespec="seconds")
                tx.emit({"ev": "publish_grant", "n": n, "expires": exp, "entry": H.entry})
        g = GRANT_PROMPT_RE.match(p)
        if g and not H.agent_id:
            if not entry_interactive(H):                  # UserPromptSubmit also fires for scheduled/forwarded prompts
                tx.emit({"ev": "ignored", "what": "grant from non-interactive entry"})
            elif not tx.state["open"]:
                tx.emit({"ev": "ignored", "what": "grant: no open task"})
            else:
                n = max(1, int(g.group(1) or 1))
                tx.emit({"ev": "rework_granted", "task": tx.state["task"], "n": n, "entry": H.entry})
        if a:
            if not entry_interactive(H):
                tx.emit({"ev": "ignored", "what": "ack-break from non-interactive entry"})
            elif target:
                tx.emit({"ev": "integrity_ack", "break": target, "by": "operator", "entry": H.entry})
            else:
                tx.emit({"ev": "ignored", "what": verdict[:60]})
                msg = ACK_NEEDS_ID if verdict == "ack-break: id needed" else "No integrity break was acknowledged (" + verdict + ")."
                out = {"systemMessage": msg, "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": msg}}
    return out


DEFAULT_INTERACTIVE = ("cli", "claude-desktop", "claude-vscode")
ACK_NEEDS_ID = ("/ack-break needs the break id shown on the Ledger screen (Ledger integrity details). "
                "Nothing was acknowledged.")


def entry_interactive(H):
    """Grants and acknowledgements come only from an interactive entry. An empty or missing `interactive_entries`
    falls back to a fixed set (never "any entry"): scheduled tasks and forwarded prompts also fire UserPromptSubmit."""
    allowed = [str(x) for x in H.cfg.get("interactive_entries") or []] or list(DEFAULT_INTERACTIVE)
    return H.entry in allowed


def ack_decision(H, arg):
    """(reason, None) when nothing is acknowledged, or (None, break id). The id is REQUIRED."""
    if not arg:
        return "ack-break: id needed", None
    try:
        todo = _chain_mod().unacknowledged(H.ledger)
    except Exception:  # noqa: BLE001
        log_error()
        return "ack-break: chain check unavailable", None
    if arg not in [b["id"] for b in todo]:
        return "ack-break: no such unacknowledged break", None
    return None, arg


def h_prompt_expansion(H):
    cmd = H.d.get("command_name") or ""
    cmd = cmd.split(":")[-1]
    if cmd == "grant-rework" or cmd not in H.playbook_names():
        return None
    with Txn(H) as tx:
        open_playbook(H, tx, cmd)
    return None


# ------------------------------------------------------------------ ledger bypass guard (contracts §6.4 rule B)

# Tools that legitimately name the ledger or hooks folder. A command segment that mentions either folder
# must START with one of these (env assignments, optional python launcher, optional path, then the tool) and
# carry no redirection or substitution. No other exception exists (a pytest run is not one).
# The folder must be EXACTLY the Atlas checkout or one of its .claude\worktrees\<name> (no look-alike folders). A relative
# `tools/x.py` is accepted only when the session's cwd is such a checkout (see ATLAS_CWD_RE); otherwise the absolute path.
_ENV_LAUNCH = r"(?:[A-Za-z_]\w*=\S*\s+)*(?:(?:py|python[\d.]*)(?:\.exe)?(?:\s+-[\d.]+)?\s+)?"
_ATLAS_ROOT = (r"(?:[A-Za-z]:[\\/]+AI[\\/]+Atlas|/mnt/[a-z]/AI/Atlas|/[a-z]/AI/Atlas)"
               r"(?:[\\/]+\.claude[\\/]+worktrees[\\/]+[\w-]+)?")
_HEAD = _ENV_LAUNCH + _ATLAS_ROOT + r"[\\/]+tools[\\/]+"          # absolute path
_HEAD_REL = _ENV_LAUNCH + r"tools[\\/]+"                           # relative path
ATLAS_CWD_RE = re.compile(r"^" + _ATLAS_ROOT + r"[\\/]*$", re.I)
_TOOL_TAILS = [
    r"launch_session\.py\b.*--record-session\b",
    r"hooks_mode\.py\b",
    r"install_hooks\.py\b",
    r"import_v1_ledger\.py\b",
    r"weekly_report\.py\b",
    r"anchor_ledger\.py\b",
]
ALLOWED_TOOL_RES = [re.compile(_HEAD + t, re.I) for t in _TOOL_TAILS]
ALLOWED_TOOL_REL_RES = [re.compile(_HEAD_REL + t, re.I) for t in _TOOL_TAILS]
ANCHOR_TOOL_RES = [re.compile(_HEAD + _TOOL_TAILS[-1], re.I)]
ANCHOR_TOOL_REL_RES = [re.compile(_HEAD_REL + _TOOL_TAILS[-1], re.I)]
SEGMENT_SPLIT_RE = re.compile(r"[;&|\r\n]+")
SMUGGLE_RE = re.compile(r"[<>`]|\$\(|\$\{")
DOT_SEG_RE = re.compile(r"[\\/]\.(?=[\\/])")
BYPASS_MATCH_MAX = 60
BYPASS_DENY = ("Protected Atlas ledger/hooks folder. To look, use the Read or Grep tools; "
               "to change it, use the documented Atlas tools (tools/*.py).")
END = r"(?![\w-])(?!\.+[\w-])"   # a dot may only start a new name: ledger.bak passes, ledger.\ (Windows strips it) does not


def _sep_re(path):
    parts = [p for p in re.split(r"[\\/]+", path.strip()) if p]
    return r"[\\/]+".join(re.escape(p) for p in parts)


def anchor_res(H):
    """The local clone of the outside-anchor repo: only tools/anchor_ledger.py may name it."""
    res = [re.compile(r"ai[\\/]+atlas-anchor" + END, re.I)]
    extra = os.environ.get("ATLAS_ANCHOR_REPO") or (getattr(H, "cfg", None) or {}).get("anchor_repo")
    if extra and len(norm_path(extra)) > 8:
        res.append(re.compile(_sep_re(extra) + END, re.I))
    return res


def protected_res(H):
    res = [re.compile(r"ai[\\/]+atlas-data[\\/]+ledger" + END, re.I),
           re.compile(r"\.claude[\\/]+hooks[\\/]+atlas" + END, re.I)]
    for extra in (H.ledger,):
        if extra and len(norm_path(extra)) > 8:
            res.append(re.compile(_sep_re(extra) + END, re.I))
    return res + anchor_res(H)


def _normalise(text):
    """Quotes removed and `/./` collapsed, so quoting or dot segments cannot hide a protected path."""
    text = text.replace('"', "").replace("'", "")
    prev = None
    while prev != text:
        prev = text
        text = DOT_SEG_RE.sub("", text)
    return text


def ledger_bypass(H, cmd):
    """The first reference to the ledger or the installed hooks folder in a command segment that does not
    start with a documented tool (or the cwd, when the session sits inside either folder), else None."""
    pats = protected_res(H)
    cwd = _normalise(H.cwd or "")
    if cwd:
        for rx in pats:
            hit = rx.search(cwd)
            if hit:
                return ("cwd:" + hit.group(0))[:BYPASS_MATCH_MAX]
        if H.ledger and under(H.cwd, H.ledger):
            return "cwd:ledger"
    for seg in SEGMENT_SPLIT_RE.split(_normalise(cmd)):
        hit = None
        for rx in pats:
            hit = rx.search(seg)
            if hit:
                break
        if not hit:
            continue
        s = seg.lstrip()
        rel_ok = bool(cwd) and bool(ATLAS_CWD_RE.match(cwd.strip()))
        abs_res, rel_res = ALLOWED_TOOL_RES, ALLOWED_TOOL_REL_RES
        if any(rx.search(seg) for rx in anchor_res(H)):          # the anchor clone: the anchor tool only
            abs_res, rel_res = ANCHOR_TOOL_RES, ANCHOR_TOOL_REL_RES
        if not SMUGGLE_RE.search(s) and (any(rx.match(s) for rx in abs_res)
                                         or (rel_ok and any(rx.match(s) for rx in rel_res))):
            continue
        return hit.group(0)[:BYPASS_MATCH_MAX]
    return None


# ------------------------------------------------------------------ bypass enrichment (contracts §1, bypass_attempt)
# Runs AFTER the deny decision, on a clipped copy of the command, with linear patterns only (no unbounded spans),
# and outside the ledger lock: a crafted command must never make the hook slow enough to time out.

BYPASS_DESC_MAX = 160
ENRICH_IN_MAX = 4096
_SECRET_RES = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+|token\s+)?[^\s\"']+"), r"\1***"),
    (re.compile(r"(\s-u\s+)([^\s:]{1,100}:)\S+"), r"\1\2***"),
    (re.compile(r"(?i)((?:^|\s)(?:set|export|-e)\s+[A-Za-z0-9_]{0,40}(?:password|passwd|token|secret|api[-_]?key|access[-_]?key|_key)\s+)"
                r"(\"[^\"]*\"|'[^']*'|\S+)"), r"\1***"),
    (re.compile(r"(?i)\bbearer\s+[^\s\"']+"), "Bearer ***"),
    (re.compile(r"(?i)(--?(?:password|passwd|token|secret|api[-_]?key|access[-_]?key)[= ])(\"[^\"]*\"|'[^']*'|\S+)"), r"\1***"),
    (re.compile(r"(?i)((?:password|passwd|pwd|token|secret|api[-_]?key|apikey|access[-_]?key|_key|\bkey)\s*[=:]\s*)"
                r"(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"), r"\1***"),
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,15}://[^/\s:@]{1,200}:)[^@\s]{1,200}@"), r"\1***@"),
    (re.compile(r"(?i)(sshpass\s+-p\s*)\S+"), r"\1***"),
    (re.compile(r"(\s-p)(\S+)"), r"\1***"),
    (re.compile(r"\b(?:sk|pk|glpat|xox[a-z])-[A-Za-z0-9_-]{8,}"), "***"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat|npm|hf)_[A-Za-z0-9_-]{8,}"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{8,}"), "***"),
]
_LONG_RUN_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_PATH_PIECE_RE = re.compile(r"[a-z0-9._-]*|[A-Z0-9._-]*")


def _mask_run(m):
    s = m.group(0)
    if re.fullmatch(r"[0-9a-fA-F]+", s):
        return "***"
    if not (re.search(r"\d", s) and re.search(r"[A-Za-z]", s)):
        return s
    if "/" not in s:
        return "***"
    # a run with slashes is kept only when every piece looks like a path name (one case, no base64 symbols)
    return s if all(_PATH_PIECE_RE.fullmatch(p) for p in s.split("/")) else "***"


def mask_secrets(text):
    text = str(text)[:20000]
    for rx, rep in _SECRET_RES:
        text = rx.sub(rep, text)
    return _LONG_RUN_RE.sub(_mask_run, text)


_READ_TOOLS = {"cat", "type", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "findstr", "get-content",
               "gc", "select-string", "sls", "ls", "dir", "wc", "get-childitem", "gci", "stat", "file", "sort", "uniq",
               "jq", "diff", "cmp", "tree", "find", "awk", "ack", "get-item", "gi", "test-path", "measure-object"}
_DELETE_TOOLS = {"rm", "del", "erase", "rmdir", "rd", "remove-item", "ri", "unlink", "shred"}
_CHANGE_TOOLS = {"mv", "move", "move-item", "mi", "ren", "rename", "rename-item", "set-content", "sc", "out-file",
                 "add-content", "ac", "tee", "truncate", "chmod", "attrib", "icacls", "chown", "touch", "new-item", "ni",
                 "clear-content", "set-itemproperty", "ln", "mklink", "dd", "patch", "tee-object", "install"}
_COPY_TOOLS = {"cp", "copy", "copy-item", "cpi", "xcopy", "robocopy", "rsync"}
_WRAPPERS = {"sudo", "time", "nohup", "command", "call", "env", "start", "start-process"}
_INTERPS = {"python", "python3", "py", "node", "perl", "ruby", "bash", "sh", "zsh", "cmd", "powershell", "pwsh"}
_GIT_CHANGE = {"checkout", "restore", "reset", "clean", "mv", "apply", "stash", "switch", "revert"}
_STRONG_DELETE_RE = re.compile(
    r"(?i)\bRemove-Item\b|\bunlink\s*\(|\bos\.(?:remove|unlink|rmdir)\b|\brmtree\b|\.unlink\b")
_STRONG_CHANGE_RE = re.compile(
    r"(?i)\b(?:Set-Content|Add-Content|Out-File|Clear-Content)\b|\.write(?:_text|_bytes|lines)?\s*\("
    r"|\bos\.(?:rename|replace|truncate|chmod)\b|\bshutil\.(?:move|copy\w*)\b|\bfs\.(?:write|append|unlink|rm|rename)\w*"
    r"|\bwriteFile\w*\b|\bopen\s*\([^)]{0,200}?[,=]\s*[\"']?(?:[wax]|r\+)")
_REDIRECT_RE = re.compile(r"(?<![<>])>>?\|?\s*([^\s<>|&;]+)")
_TOKEN_SPLIT_RE = re.compile(r"[\s()\[\]{}\"',]+")
_FIND_WRITE = {"-fprint", "-fprint0", "-fls", "-fprintf"}


def _tool_in(tokens, names):
    for t in tokens:
        low = os.path.basename(t.replace("\\", "/")).lower()
        low = re.sub(r"\.(exe|cmd|bat|ps1)$", "", low)
        if low in names:
            return True
    return False


def _head_of(tokens):
    for i, t in enumerate(tokens):
        low = os.path.basename(t.replace("\\", "/")).lower()
        low = re.sub(r"\.(exe|cmd|bat|ps1)$", "", low)
        if re.fullmatch(r"[A-Za-z_]\w*=\S*", t) or low in _WRAPPERS:
            continue
        return low, tokens[i + 1:]
    return "", []


def _copy_dest(head, rest, args):
    for i, a in enumerate(rest):
        al = a.lower()
        if al in ("-t", "--target-directory", "-destination", "-dest", "-d") and i + 1 < len(rest):
            return rest[i + 1]
        if al.startswith("--target-directory="):
            return a.split("=", 1)[1]
    pos = [a for a in rest if not a.startswith("-") and not re.fullmatch(r"/[A-Za-z?]{1,8}(?::\S*)?", a)]
    if head in ("xcopy", "robocopy"):
        return pos[1] if len(pos) > 1 else ""
    return pos[-1] if pos else ""


_GREP_FAMILY = {"grep", "egrep", "fgrep", "rg", "ack"}
_OUT_LONG = ("--output", "--log-file", "--pre", "--open-files-in-pager")


def _out_flag(head, rest, hit):
    """A read tool run with a flag that writes a file or runs a program: 'change' when the flag's value names a
    protected path, else 'unclear'; None when no such flag. (-o means only-matching for the grep family.)"""
    for i, f in enumerate(rest):
        val = None
        if f.startswith(_OUT_LONG):
            val = f.split("=", 1)[1] if "=" in f else (rest[i + 1] if i + 1 < len(rest) else "")
        elif f.startswith("-") and not f.startswith("--"):
            if (f.startswith("-o") and head not in _GREP_FAMILY) or (f.startswith("-O") and head in ("git", "rg", "ack")):
                takes = head in ("less", "sort", "tree", "git", "rg", "ack", "diff", "jq")
                val = f[2:] or ((rest[i + 1] if i + 1 < len(rest) else "") if takes else "")
        if val is not None:
            return "change" if hit(val) else "unclear"
    return None


def _segment_effect(seg_raw, pats):
    """'read' | 'change' | 'delete' | 'unclear' for ONE command segment."""
    norm = _normalise(seg_raw)
    if not norm.strip():
        return None
    hit = lambda s: any(rx.search(s) for rx in pats)  # noqa: E731
    for m in _REDIRECT_RE.finditer(norm):
        if hit(m.group(1)):
            return "change"
    if _STRONG_DELETE_RE.search(seg_raw):
        return "delete"
    if _STRONG_CHANGE_RE.search(seg_raw):
        return "change" if hit(norm) else "unclear"
    tokens = norm.split()
    head, rest = _head_of(tokens)
    flags = [a for a in rest if a.startswith("-")]
    args = [a for a in rest if not a.startswith("-")]
    low_flags = [a.lower() for a in flags]
    if head in _DELETE_TOOLS:
        return "delete"
    if head in _CHANGE_TOOLS:
        return "change" if hit(norm) else "unclear"
    smuggle = bool(re.search(r"`|\$\(|\$\{", norm))
    if head == "xargs" or "-exec" in low_flags or "-execdir" in low_flags or "-ok" in low_flags:
        words = _TOKEN_SPLIT_RE.split(norm)
        if _tool_in(words, _DELETE_TOOLS):
            return "delete"
        if _tool_in(words, _CHANGE_TOOLS):
            return "change"
        return "unclear"
    if head in _COPY_TOOLS:
        if head in ("xcopy", "robocopy") or "/mir" in [a.lower() for a in rest] or "/purge" in [a.lower() for a in rest]:
            lower = [a.lower() for a in rest]
            if ("/mir" in lower or "/purge" in lower) and hit(_copy_dest(head, rest, args)):
                return "delete"
        return "change" if hit(_copy_dest(head, rest, args)) else ("unclear" if smuggle else "read")
    if head == "git":
        sub = args[0].lower() if args else ""
        if sub == "rm":
            return "delete"
        if sub in _GIT_CHANGE:
            return "change"
        of = _out_flag("git", rest, hit)
        if of:
            return of
        return "unclear" if smuggle or sub not in ("log", "show", "diff", "status", "blame", "grep", "ls-files") else "read"
    if head in _INTERPS:
        words = _TOKEN_SPLIT_RE.split(norm)
        if _tool_in(words[1:], _DELETE_TOOLS):
            return "delete"
        if _tool_in(words[1:], _CHANGE_TOOLS):
            return "change"
        return "unclear"
    if head == "sed":                           # a sed script can write (w), run (e) or edit in place: never "read"
        if "--in-place" in low_flags or any(f.startswith("--in-place=") for f in low_flags) \
                or any(re.match(r"-[A-Za-z]*i", f) and not f.startswith("--") for f in flags):
            return "change"
        for rx in pats:
            if re.search(r"(?:^|[/;{\s])[wW]\s*\S{0,40}?(?:" + rx.pattern + ")", norm, re.I):
                return "change"
        return "unclear"
    if head in _READ_TOOLS or head == "git":
        of = _out_flag(head, rest, hit)
        if of and head != "git":
            return of
    if head in _READ_TOOLS:
        if "--in-place" in low_flags or any(f.startswith("--in-place=") for f in low_flags):
            return "change"
        if head == "awk":
            if "system(" in norm.replace(" ", "") or "inplace" in norm.lower():
                return "delete" if re.search(r"\b(?:rm|del)\b", norm) else "change"
            if "|&" in norm or re.search(r"print[^>|]{0,80}[>|]", norm):
                return "unclear"
        if head == "sort" and any(f == "-o" or f.startswith("-o") or f.startswith("--output") for f in flags):
            return "change"
        if head == "tree" and any(f == "-o" or f.startswith("-o") for f in flags):
            return "change"
        if head == "uniq" and len(args) >= 2:
            return "change"
        if head == "find":
            if "-delete" in low_flags:
                return "delete"
            if any(f.lower() in _FIND_WRITE for f in flags):
                return "change"
        return "unclear" if smuggle else "read"
    return "unclear"


def classify_effect(H, cmd):
    """'read' | 'change' | 'delete' | 'unclear' for the whole command (every pipe / ; / && segment).
    'read' only when EVERY segment has a read-tool head and no change signal. Input over ENRICH_IN_MAX = 'unclear'."""
    if len(cmd) > ENRICH_IN_MAX:
        return "unclear"
    cmd = re.sub(r"\d?>&\d?-?", " ", cmd).replace(">|", ">")
    pats = protected_res(H)
    res = []
    for seg in SEGMENT_SPLIT_RE.split(cmd):
        r = _segment_effect(seg, pats)
        if r:
            res.append(r)
    if "delete" in res:
        return "delete"
    if "change" in res:
        return "change"
    if res and all(r == "read" for r in res):
        return "read"
    return "unclear"


def _actor_of(H, S):
    if not H.agent_id and not H.agent_type:
        return "main", None, None
    helper = (H.agent_type or "").lower()
    label = None
    info = (S.get("agents") or {}).get(H.agent_id) if H.agent_id else None
    if info:
        helper = helper or str(info.get("helper") or "").lower()
        p = (S.get("pending") or {}).get(info.get("tuid")) or {}
        label = p.get("label")
    return helper or "unknown", H.agent_id, label


_DAY_FILE_RE = re.compile(r"events-(\d{4}-\d\d-\d\d)")


def _target_label(c):
    """A short label of what the command names (no command text): ledger-day:YYYY-MM-DD, hooks-config,
    hooks-program, hooks-folder or ledger."""
    low = c.lower()
    if re.search(r"\.claude[\\/]+hooks[\\/]+atlas", low):
        if "atlas_config" in low:
            return "hooks-config"
        if "atlas_hook" in low:
            return "hooks-program"
        return "hooks-folder"
    m = _DAY_FILE_RE.search(low)
    return "ledger-day:" + m.group(1) if m else "ledger"


def bypass_extra(H, cmd):
    """The parts of the enrichment that need no ledger state; computed before the lock is taken."""
    out = {}
    c = str(cmd)[:ENRICH_IN_MAX]
    try:
        desc = H.ti.get("description")
        if desc:
            out["desc"] = mask_secrets(str(desc)[:ENRICH_IN_MAX])[:BYPASS_DESC_MAX]
    except Exception:  # noqa: BLE001
        pass
    try:                                        # the command itself is NEVER stored; only this derived label
        out["target"] = _target_label(c)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["effect"] = classify_effect(H, cmd)
    except Exception:  # noqa: BLE001
        out["effect"] = "unclear"
    return out


def bypass_actor(H, tx):
    """actor / agent_id / actor_task, read from the locked state (cheap)."""
    out = {}
    try:
        actor, aid, label = _actor_of(H, tx.state)
        out["actor"] = actor
        if aid:
            out["agent_id"] = aid
        if label:
            out["actor_task"] = str(label)[:60]
    except Exception:  # noqa: BLE001
        pass
    return out


# a segment RUNS the note tool: optional wrapper shell, env assignments, launcher, then a path ending in the file
LEDGER_NOTE_RUN_RE = re.compile(
    r"^\s*(?:(?:bash|sh|zsh|cmd|powershell|pwsh)(?:\.exe)?\s+(?:-\w+\s+|/c\s+)*)?(?:[A-Za-z_]\w*=\S*\s+)*"
    r"(?:(?:py|python[\d.]*)(?:\.exe)?(?:\s+-[\d.]+)?\s+)?(?:\S*[\\/])?ledger_note\.py(?![\w.-])", re.I)
NOTE_BY_HELPER = "ledger note by a helper"


def runs_ledger_note(cmd):
    if "ledger_note" not in cmd.lower():
        return False
    return any(LEDGER_NOTE_RUN_RE.match(seg) for seg in SEGMENT_SPLIT_RE.split(_normalise(cmd)))


# ------------------------------------------------------------------ publish gate (contracts §6.4b)
# A Bash/PowerShell command that PUBLISHES (pushes, releases, deploys, uploads, opens or changes something on a
# hosting service) is denied in EVERY mode unless the operator typed /allow-publish in this session. Only the families
# in PUBLISH_RULES / PUBLISH_SPECIAL count: an unknown command is NOT publish (false positives stay near zero). The
# parse is one linear pass per level (no backtracking regex on the command text) and runs only on the command's first
# PUBLISH_MAX_CHARS characters; a longer command is refused outright (fail closed). One grant = one publish COMMAND:
# the scan collects every family in the whole command and a chain of three needs three allowances. Honest limits:
# aliases, scripts that publish internally (`make deploy`, `npm run release`), `python -c`, `echo "git push" | sh`,
# run-time string building, non-shell tools (MCP servers, browser automation) and rsync/scp/curl -T are not seen.

PUBLISH_MAX_CHARS = 200000
PUBLISH_WORK_MAX = 4 * PUBLISH_MAX_CHARS      # characters scanned over every wrapper level and reading; more = refused
PUBLISH_DEPTH = 4                      # nested wrappers (bash -c "bash -c '...'") deeper than this are refused
PUBLISH_DENY = "Publishing needs the operator's OK: ask the operator to type /allow-publish."
PUBLISH_GRANT_MAX = 5                  # the most one /allow-publish can leave unused
PUBLISH_GRANT_MINUTES = 30
PUBLISH_LOCK_TRIES = 10                # x 50 ms: taking a grant waits at most ~0.5 s for the ledger lock, then denies
PUBLISH_LOG_TRIES = 4                  # x 50 ms: logging a refusal waits at most ~0.2 s, then writes unlocked
PUBLISH_PROMPT_RE = re.compile(r"^\s*/allow-publish(?![\w-])(?:\s+(\d{1,2})(?!\d))?")

# THE LIST. One row per family: (what, programs, words, gap, dry_flags, need_flags, anywhere)
#   what       label logged on refusal (the matched family, never the command)
#   programs   executable names (lower case, .exe/.cmd/.bat/.ps1 and any path removed)
#   words      the non-option arguments that must follow, in order, e.g. ("pages", "deploy")
#   gap        how many leading non-option arguments may come first (an option's value, e.g. `npm --prefix x publish`)
#   dry_flags  a dry-run flag that makes the command a non-publish
#   need_flags the command is a publish only when one of these flags is present (`vercel --prod`)
#   anywhere   `words[0]` may be ANY non-option argument (`gcloud run deploy`, `az webapp deploy`)
# Families with their own parsing (git push and subtree push, gh api and repo edit --visibility, aws s3) are
# PUBLISH_SPECIAL below.
PUBLISH_RULES = (
    ("gh release create", ("gh",), ("release", "create"), 0, (), (), False),
    ("gh release upload", ("gh",), ("release", "upload"), 0, (), (), False),
    ("gh release edit", ("gh",), ("release", "edit"), 0, (), (), False),
    ("gh release delete", ("gh",), ("release", "delete"), 0, (), (), False),
    ("gh pr merge", ("gh",), ("pr", "merge"), 0, (), (), False),
    ("gh pr create", ("gh",), ("pr", "create"), 0, (), (), False),
    ("gh pr comment", ("gh",), ("pr", "comment"), 0, (), (), False),
    ("gh pr review", ("gh",), ("pr", "review"), 0, (), (), False),
    ("gh issue create", ("gh",), ("issue", "create"), 0, (), (), False),
    ("gh issue comment", ("gh",), ("issue", "comment"), 0, (), (), False),
    ("gh workflow run", ("gh",), ("workflow", "run"), 0, (), (), False),
    ("gh repo create", ("gh",), ("repo", "create"), 0, (), (), False),
    ("gh repo delete", ("gh",), ("repo", "delete"), 0, (), (), False),
    ("gh repo archive", ("gh",), ("repo", "archive"), 0, (), (), False),
    ("gh gist create --public", ("gh",), ("gist", "create"), 0, (), ("--public", "-p"), False),
    ("npm publish", ("npm", "pnpm", "yarn", "bun"), ("publish",), 1, ("--dry-run",), (), False),
    ("yarn npm publish", ("yarn",), ("npm", "publish"), 1, ("--dry-run",), (), False),
    ("twine upload", ("twine",), ("upload",), 0, (), (), False),
    ("uv publish", ("uv",), ("publish",), 1, ("--dry-run",), (), False),
    ("poetry publish", ("poetry",), ("publish",), 1, ("--dry-run",), (), False),
    ("hatch publish", ("hatch",), ("publish",), 1, (), (), False),
    ("flit publish", ("flit",), ("publish",), 1, (), (), False),
    ("cargo publish", ("cargo",), ("publish",), 1, ("--dry-run",), (), False),
    ("docker push", ("docker",), ("push",), 0, (), (), False),
    ("docker push", ("docker",), ("image", "push"), 0, (), (), False),
    ("docker compose push", ("docker",), ("compose", "push"), 0, (), (), False),
    ("docker compose push", ("docker-compose",), ("push",), 1, (), (), False),
    ("docker build --push", ("docker",), ("build",), 0, (), ("--push",), False),
    ("docker build --push", ("docker",), ("buildx", "build"), 0, (), ("--push",), False),
    ("docker build --push", ("docker",), ("buildx", "bake"), 0, (), ("--push",), False),
    ("wrangler deploy", ("wrangler",), ("deploy",), 1, ("--dry-run",), (), False),
    ("wrangler deploy", ("wrangler",), ("publish",), 1, ("--dry-run",), (), False),
    ("wrangler deploy", ("wrangler",), ("pages", "deploy"), 1, (), (), False),
    ("wrangler deploy", ("wrangler",), ("pages", "publish"), 1, (), (), False),
    ("vercel --prod", ("vercel", "vc"), (), 0, (), ("--prod", "--production"), False),
    ("netlify deploy --prod", ("netlify",), ("deploy",), 1, (), ("--prod", "-p", "--prodiflocked"), False),
    ("firebase deploy", ("firebase",), ("deploy",), 1, (), (), False),
    ("fly deploy", ("flyctl", "fly"), ("deploy",), 1, (), (), False),
    ("gcloud deploy", ("gcloud",), ("deploy",), 0, (), (), True),
    ("az deploy", ("az",), ("deploy",), 0, (), (), True),
)

_ENV_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PLAIN_RUN_RE = re.compile(r"[^'\"\\ \t\r#;&|()`\n<]+")
_PLAIN_RUN_PS_RE = re.compile(r"[^'\"\\ \t\r#;&|()`\n<{}]+")      # the same, stopping at braces (PowerShell-braces reading)
_PUB_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
# prefixes that run the command after them, with the options that take a value (so the value is not read as the command)
_PREFIX_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-r", "-t", "-U", "--user", "--group", "--host",
             "--prompt", "--chdir", "--chroot"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "--unset", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},     # plus one positional DURATION
    "stdbuf": {"-i", "-o", "-e"},
    "time": {"-f", "-o", "--format", "--output"},
    "exec": {"-a"},
}
_PUB_PREFIX = set(_PREFIX_VALUE_OPTS) | {"nohup", "command", "call", "builtin", "setsid", "unbuffer",
                                         "then", "do", "else", "elif", "if", "while", "until", "!", "{", "}"}
_PKG_ALIASES = {"netlify-cli": "netlify", "firebase-tools": "firebase"}
_RUN_VALUE_OPTS = {"-p", "--package", "--with", "--python", "--from", "--spec", "--directory", "--project",
                   "--with-requirements", "--index-url", "--extra-index-url", "--index"}
_PM_OWN = {"add", "install", "i", "remove", "rm", "uninstall", "update", "up", "upgrade", "run", "test", "t", "start",
           "exec", "dlx", "x", "pack", "link", "unlink", "list", "ls", "outdated", "audit", "why", "store", "config",
           "init", "create", "import", "rebuild", "prune", "fetch", "patch", "env", "setup", "root", "bin", "cache",
           "doctor", "info", "version", "login", "logout", "whoami", "help", "workspaces", "workspace", "set", "get",
           "node", "plugin", "dedupe", "explain", "pm", "repl", "publish", "npm"}
_XARGS_VALUE_OPTS = {"-I", "-n", "-L", "-P", "-d", "-E", "-s", "-a", "--max-args", "--max-lines", "--max-procs",
                     "--delimiter", "--eof", "--max-chars", "--arg-file"}
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
_PS_VALUE_PARAMS = {"-workingdirectory", "-verb", "-windowstyle", "-redirectstandardinput", "-redirectstandardoutput",
                    "-redirectstandarderror", "-credential"}
_GIT_OPT_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
_AWS_BOOL_FLAGS = {"--recursive", "--dryrun", "--delete", "--quiet", "--only-show-errors", "--no-progress",
                   "--follow-symlinks", "--no-follow-symlinks", "--exact-timestamps", "--size-only", "--no-sign-request",
                   "--ignore-glacier-warnings", "--force-glacier-transfer", "--debug", "--no-paginate",
                   "--no-verify-ssl", "--human-readable", "--summarize", "--expected-size", "--no-guess-mime-type"}


def _prog(tok):
    """Executable name of a token: path and .exe/.cmd/.bat/.ps1 removed, lower case."""
    name = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _pkg_head(tok):
    """A package-runner spec as a program token: `wrangler@latest` -> `wrangler`, `@scope/pkg@1` -> `@scope/pkg`
    (the scope's own @ is kept), then the npm package aliases (`netlify-cli` -> `netlify`)."""
    k = tok.find("@", 1) if tok.startswith("@") else tok.find("@")
    if k > 0:
        tok = tok[:k]
    return _PKG_ALIASES.get(_prog(tok), tok)


def _shell_split(text, psb=False):
    """One linear pass: (segments, heredoc bodies, double-quoted strings holding $( or a backtick). Segments are token
    lists split on unquoted ; & | ( ) backtick and newline; quotes are removed, `\\"` is unescaped inside double quotes,
    a backslash before a space keeps the space in the token, `#` at a token start comments out the line, a
    backslash-newline continues the line, and `<<WORD` bodies are collected apart (never read as commands unless a shell
    reads them). Blocks are decided here, where quoting is known: an UNQUOTED `{` that starts a token ends the segment
    and is dropped, and an unquoted `}` that ends a token (next char is a space, a line break, the end, or one of `;&|()`)
    ends the token and the segment and is dropped, unless it balances a `{` inside the same token. Braces inside a token
    (`HEAD@{1}`, `${x}`, `@{u}`, `a.{txt,bak}`, `@{`) and every quoted brace stay plain text. An unquoted `{#` stays text
    (so the `#` is not read as a comment) and an unquoted `{}` at a word start (`find -exec rm {} ;`, `{}/..`) is kept as text.
    `psb=True` is the PowerShell-braces reading: EVERY unquoted `{` and `}` is a boundary (`%{git push}`, `try{...}catch{}`),
    except the `${name}` variable syntax; the caller unions it with the plain reading."""
    segs, docs, nested = [], [], []
    toks, cur = [], []
    have = False
    bal = 0
    no_close = False                            # no `}` left in the text (so a `${` is not searched again: stays linear)
    pend = []
    heredocs = True
    n, i = len(text), 0
    while i < n:
        c = text[i]
        if not have:
            bal = 0                             # a new word starts: no brace of it is open yet
        if c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            cur.append(text[i + 1:j])
            have = True
            i = j + 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n:
                d = text[j]
                if d == "\\" and j + 1 < n and text[j + 1] == '"':
                    buf.append('"')
                    j += 2
                elif d == '"':
                    break
                else:
                    buf.append(d)
                    j += 1
            s = "".join(buf)
            if "$(" in s or "`" in s:
                nested.append(s)
            cur.append(s)
            have = True
            i = j + 1
        elif c == "\\" and text[i + 1:i + 2] == " ":
            cur.append(" ")                     # an escaped space stays inside the token (/my\ dir/git)
            have = True
            i += 2
        elif c in " \t\r" or (c == "\\" and text[i + 1:i + 2] in ("\n", "\r")):
            if have:
                toks.append("".join(cur))
                cur, have = [], False
            if c != "\\":                             # a backslash before a line break continues the line
                i += 1
            else:
                i += 3 if text.startswith("\r\n", i + 1) else 2
        elif c == "#" and not have and not (psb and i > 0 and text[i - 1] in "{}"):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in ";&|()`\n":
            if have:
                toks.append("".join(cur))
                cur, have = [], False
            if toks:
                segs.append(toks)
                toks = []
            i += 1
            if c == "\n" and pend:
                for delim, strip in pend:
                    start, body, closed = i, [], False
                    while i < n:
                        e = text.find("\n", i)
                        line = text[i:n if e < 0 else e]
                        i = n if e < 0 else e + 1
                        chk = line.rstrip("\r")
                        if (chk.lstrip("\t") if strip else chk) == delim:
                            closed = True
                            break
                        body.append(line)
                    if not closed:          # no closing line (for example `$((1<<2))`): read the rest as plain commands
                        i, heredocs = start, False       # (once: later `<<` are plain too, so the pass stays linear)
                        break
                    docs.append("\n".join(body))
                pend = []
        elif c == "<" and text.startswith("<<<", i):
            cur.append("<<<")                   # a here-string: its word is read as an argument
            have = True
            i += 3
        elif c == "<" and heredocs and text.startswith("<<", i):
            if have:
                toks.append("".join(cur))
                cur, have = [], False
            j, strip = i + 2, False
            if j < n and text[j] == "-":
                strip, j = True, j + 1
            while j < n and text[j] in " \t":
                j += 1
            word = []
            while j < n and text[j] not in " \t\r\n;&|()<>":
                if text[j] not in "'\"":
                    word.append(text[j])
                j += 1
            if word:
                pend.append(("".join(word), strip))
            i = j
        elif psb and c in "{}":
            if c == "{" and have and cur and cur[-1].endswith("$"):          # ${name}: a variable, not a block
                j = -1 if no_close else text.find("}", i)
                if j < 0:
                    no_close, j = True, n - 1
                cur.append(text[i:j + 1])
                i = j + 1
            else:
                if have:
                    toks.append("".join(cur))
                    cur, have = [], False
                if toks:
                    segs.append(toks)
                    toks = []
                i += 1
        elif c == "{" and not have and text[i + 1:i + 2] != "#":
            if text[i + 1:i + 2] == "}":
                cur.append("{}")                # the find placeholder stays text, whatever follows ({}/.. {}.d)
                have = True
                i += 2
                continue
            if toks:                            # an unquoted { that starts a token opens a block: it ends the segment
                segs.append(toks)
                toks = []
            i += 1
        else:
            m = (_PLAIN_RUN_PS_RE if psb else _PLAIN_RUN_RE).match(text, i)   # a run of ordinary characters at once (one class, no backtracking)
            if m is None:
                cur.append(c)
                have = True
                i += 1
                continue
            run, end = m.group(), m.end()
            cut = 0                             # (bal: braces opened and not yet closed inside the word being read)
            if run.endswith("}") and (end >= n or text[end] in " \t\r\n;&|()"):
                # trailing } of a token closes a block, unless it balances a { of the same token (${x}, HEAD@{1}, @{u})
                excess = run.count("}") - run.count("{") - bal
                cut = min(len(run) - len(run.rstrip("}")), excess) if excess > 0 else 0
            kept = run[:len(run) - cut]
            bal = max(0, bal + kept.count("{") - kept.count("}"))
            if kept:
                cur.append(kept)
                have = True
            if cut:                             # the block ends: close the token, then the segment
                if have:
                    toks.append("".join(cur))
                    cur, have = [], False
                if toks:
                    segs.append(toks)
                    toks = []
            i = end
    if have:
        toks.append("".join(cur))
    if toks:
        segs.append(toks)
    return segs, docs, nested


_PS_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "??="}


def _strip_prefix(toks):
    """(the command after any environment assignments and prefixes such as sudo/env/timeout/nice, texts to scan as
    commands). `env -S "git push"` hands its string back as a text."""
    i, n, texts = 0, len(toks), []
    while i < n:
        t = toks[i]
        if _ENV_ASSIGN_RE.match(t):
            i += 1
            continue
        if t.startswith("$"):                   # PowerShell assignment: `$x = git push`, `$null = ...`, `$x=git push`
            if "=" in t:
                val = t.split("=", 1)[1]
                if val:
                    toks = toks[:i] + [val] + toks[i + 1:]
                else:
                    i += 1
                continue
            if i + 1 < n and toks[i + 1] in _PS_ASSIGN_OPS:
                i += 2
                continue
            break
        p = _prog(t)
        if p not in _PUB_PREFIX:
            break
        i += 1
        valued = _PREFIX_VALUE_OPTS.get(p, ())
        while i < n and toks[i].startswith("-"):
            o = toks[i]
            if p == "env" and (o in ("-S", "--split-string") or o.startswith("--split-string=")
                               or (o.startswith("-S") and len(o) > 2)):
                if o in ("-S", "--split-string"):
                    val, first = (toks[i + 1] if i + 1 < n else ""), i + 2
                else:
                    val, first = (o.split("=", 1)[1] if "=" in o else o[2:]), i + 1
                texts.append(" ".join([val] + toks[first:]))
                return [], texts
            i += 2 if o in valued else 1
        if p == "timeout" and i < n:
            i += 1                              # the DURATION
    return toks[i:], texts


def _flags_of(args):
    return {a.lower().split("=", 1)[0] for a in args if a.startswith("-")}


def _is_anchor_tool(H, toks):
    """The documented anchor tool (its push is part of the approved anchor design), by the bypass guard's own path rules."""
    s = " ".join(toks)
    if "anchor_ledger" not in s.lower() or SMUGGLE_RE.search(s):
        return False
    if any(rx.match(s) for rx in ANCHOR_TOOL_RES):
        return True
    cwd = _normalise(H.cwd or "").strip()
    return bool(cwd) and bool(ATLAS_CWD_RE.match(cwd)) and any(rx.match(s) for rx in ANCHOR_TOOL_REL_RES)


_PUSH_SHORT = frozenset("fnuqvd46")      # git push's own one-letter flags (no value)


def _push_dry_cluster(r):
    """`-n`, `-nu`, `-vn`: a short-flag cluster of git push's own letters that holds n; never a `-Word` parameter."""
    return len(r) >= 2 and r[0] == "-" and "n" in r[1:] and all(c in _PUSH_SHORT for c in r[1:])


def _pub_git(args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_OPT_VALUE else 1
    if i >= len(args):
        return None
    if args[i] == "subtree":
        rest = [a for a in args[i + 1:] if not a.startswith("-")]
        return "git subtree push" if "push" in rest[:2] else None
    if args[i] != "push":
        return None
    for r in args[i + 1:]:
        if r == "--dry-run" or _push_dry_cluster(r):
            return None
    return "git push"


def _pub_gh(args):
    pos = [a for a in args if not a.startswith("-")]
    if pos[:2] == ["repo", "edit"]:             # making a repository public
        for k, a in enumerate(args):
            if a == "--visibility" and k + 1 < len(args) and args[k + 1].lower() == "public":
                return "gh repo edit --visibility public"
            if a.lower() == "--visibility=public":
                return "gh repo edit --visibility public"
        return None
    if pos[:1] != ["api"]:
        return None
    method, fields = None, False
    for k, a in enumerate(args):
        low = a.lower()
        if a in ("-X", "--method") and k + 1 < len(args):
            method = args[k + 1].upper()
        elif a.startswith("--method="):
            method = a[9:].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
        elif a in ("-f", "-F") or low.startswith(("--field", "--raw-field", "--input")) or (
                a.startswith(("-f", "-F")) and not a.startswith("--") and len(a) > 2):
            fields = True
    if method is None and fields:
        method = "POST"                 # gh api turns into a POST as soon as it is given fields
    return "gh api " + method if method in ("POST", "PUT", "PATCH", "DELETE") else None


def _pub_aws(args):
    paths, skip = [], False
    seen = []                           # the words before the paths: s3, then sync|cp|mv
    for a in args:
        if skip:
            skip = False
        elif a.startswith("--"):
            skip = "=" not in a and a.lower() not in _AWS_BOOL_FLAGS
        elif a.startswith("-"):
            continue
        elif len(seen) < 2:
            seen.append(a.lower())
        else:
            paths.append(a)
    if seen[:1] != ["s3"] or seen[1:2] not in (["sync"], ["cp"], ["mv"]):
        return None
    if "--dryrun" in _flags_of(args):
        return None
    return "aws s3 " + seen[1] if len(paths) >= 2 and paths[1].lower().startswith("s3://") else None


PUBLISH_SPECIAL = {"git": _pub_git, "gh": _pub_gh, "aws": _pub_aws}
_RULE_PROGS = {p for row in PUBLISH_RULES for p in row[1]}
# every program name the segment reader does anything with; any other head is skipped at once (speed only)
_KNOWN_HEADS = (_RULE_PROGS | set(PUBLISH_SPECIAL) | _PUB_SHELLS | _PUB_PREFIX
                | {"cmd", "powershell", "pwsh", "iex", "invoke-expression", "invoke-command", "icm", "start-process",
                   "saps", "start", "eval", "wsl", "xargs", "find", "py", "npx", "pnpx", "bunx", "uvx", "npm", "pnpm", "yarn",
                   "bun", "uv", "pipx", "poetry"})


def _words_match(pos, words, gap):
    n = len(words)
    return any(pos[s:s + n] == list(words) for s in range(gap + 1))


def _deeper(H, toks, depth, ctx):
    return _publish_segment(H, toks, depth + 1, ctx) if depth < PUBLISH_DEPTH else ["nested wrappers"]


def _from_block(args):
    """PowerShell script text: from the first `{` argument on, else every non-option argument."""
    for k, a in enumerate(args):
        if a.startswith("{"):
            return " ".join(args[k:])
    return " ".join(a for a in args if not a.startswith("-"))


def _ps_start_process(args):
    """Start-Process / saps / start -> the command tokens: FilePath, then ArgumentList split on commas (or on spaces
    when there is no comma)."""
    path, arglist, rest, i = None, None, [], 0
    while i < len(args):
        a, low = args[i], args[i].lower()
        name, _sep, attached = low.partition(":")
        if a.startswith("-") and len(name) >= 2:
            val = args[i + 1] if not _sep and i + 1 < len(args) else (a.split(":", 1)[1] if _sep else "")
            adv = 1 if _sep else 2
            if "-filepath".startswith(name):
                path = val
            elif "-argumentlist".startswith(name):
                arglist = val
            elif name in _PS_VALUE_PARAMS:
                pass
            else:
                adv = 1
            i += adv
        else:
            if a and not (a.startswith("/") and a[1:].isalpha()):
                rest.append(a)
            i += 1
    if path is None:
        if not rest:
            return []
        path, rest = rest[0], rest[1:]
    items = []
    if arglist:
        items = [x.strip() for x in arglist.split(",")] if "," in arglist else arglist.split()
    return [path] + items + rest


def _find_rest(toks):
    """True for the rest of a find after an unquoted backslash-semicolon cut it: starts with -, ! or , and holds an exec
    flag (`-exec git push`, `-print -exec ...`, `! -name x -exec ...`, `, -exec ...`, `-o -exec ...`)."""
    return toks[0][:1] in ("-", "!", ",") and any(t in _FIND_EXEC for t in toks)


def _segment_once(H, toks, depth, ctx):
    if _find_rest(toks):
        toks = ["find"] + toks
    head = _prog(toks[0])
    out = []
    if "=" in toks[0] or toks[0].startswith("$") or head in _PUB_PREFIX:
        toks, texts = _strip_prefix(toks)
        for t in texts:
            out += _publish_scan(H, t, depth + 1, ctx)
        if not toks:
            return out
        head = _prog(toks[0])
    args = toks[1:]
    if head in _PUB_SHELLS:
        k = next((j for j, a in enumerate(args) if a.startswith("-") and not a.startswith("--") and "c" in a[1:]), None)
        if k is not None:
            j = k + 1
            while j < len(args) and args[j].startswith("-"):
                j += 1
                if args[j - 1] == "--":
                    break
            return out + _publish_scan(H, args[j] if j < len(args) else "", depth + 1, ctx)
        ctx["shell_stdin"] = True
        for k, a in enumerate(args):
            if a.startswith("<<<"):
                return out + _publish_scan(H, a[3:] or (args[k + 1] if k + 1 < len(args) else ""), depth + 1, ctx)
        return out
    if head == "cmd":
        k = next((j for j, a in enumerate(args) if a.lower() in ("/c", "/k", "/r")), None)
        return out + (_publish_scan(H, " ".join(args[k + 1:]), depth + 1, ctx) if k is not None else [])
    if head in ("powershell", "pwsh"):
        for k, a in enumerate(args):
            low = a.lower()
            if not low.startswith("-") or len(low) < 2:
                continue
            if "-command".startswith(low):
                return out + _publish_scan(H, " ".join(args[k + 1:]), depth + 1, ctx)
            if "-file".startswith(low):
                return out
            if low in ("-e", "-ec") or ("-encodedcommand".startswith(low) and len(low) >= 3):
                try:
                    import base64  # noqa: PLC0415
                    inner = base64.b64decode(args[k + 1] if k + 1 < len(args) else "").decode("utf-16-le")
                except Exception:  # noqa: BLE001
                    return out
                return out + _publish_scan(H, inner, depth + 1, ctx)
        return out + _publish_scan(H, _from_block(args), depth + 1, ctx)   # `pwsh { git push }`, `pwsh "git push"`
    if head in ("iex", "invoke-expression", "invoke-command", "icm"):
        return out + _publish_scan(H, _from_block(args), depth + 1, ctx)
    if head in ("start-process", "saps", "start"):
        return out + _deeper(H, _ps_start_process(args), depth, ctx)
    if head == "eval":
        return out + _publish_scan(H, " ".join(args), depth + 1, ctx)
    if head == "wsl":
        j = 0
        while j < len(args) and args[j].startswith("-") and args[j] != "--":
            j += 2 if args[j] in ("-d", "--distribution", "-u", "--user", "--cd") else 1
        if j < len(args) and args[j] == "--":
            j += 1
        return out + _deeper(H, args[j:], depth, ctx)
    if head == "xargs":
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if args[j] in _XARGS_VALUE_OPTS else 1
        return out + _deeper(H, args[j:], depth, ctx)
    if head == "find":                          # -exec/-execdir/-ok/-okdir <command> ... ; or +: the command is checked
        k = 0
        while k < len(args):
            if args[k] in _FIND_EXEC:
                j = k + 1
                while j < len(args) and args[j] not in (";", "+", "\\"):
                    j += 1
                if j > k + 1:
                    out += _deeper(H, args[k + 1:j], depth, ctx)
                k = j
            k += 1
        return out
    if head.startswith("python") or head == "py":
        if "-m" in args and args.index("-m") + 1 < len(args):
            return out + _deeper(H, args[args.index("-m") + 1:], depth, ctx)
        return out
    inner = None
    if head in ("npx", "pnpx", "bunx", "uvx"):
        inner = args
    elif head in ("npm", "pnpm", "yarn", "bun") and args and args[0] in ("dlx", "exec", "x"):
        inner = args[1:]
    elif head == "uv" and args[:1] == ["run"]:
        inner = args[1:]
    elif head == "uv" and args[:2] == ["tool", "run"]:
        inner = args[2:]
    elif head in ("pipx", "poetry") and args[:1] == ["run"]:
        inner = args[1:]
    if inner is not None:
        j = 0
        while j < len(inner) and inner[j].startswith("-"):
            if inner[j] in ("-c", "--call") and j + 1 < len(inner):        # npx -c "<shell command>"
                out += _publish_scan(H, inner[j + 1], depth + 1, ctx)
            j += 2 if (inner[j] in _RUN_VALUE_OPTS or inner[j] in ("-c", "--call")) else 1
        rest = inner[j:]
        if rest:
            rest = [_pkg_head(rest[0])] + rest[1:]
        return out + _deeper(H, rest, depth, ctx)
    special = PUBLISH_SPECIAL.get(head)
    if special:
        what = special(args)
        if what:
            return out + [what]
    if head in _RULE_PROGS:
        pos = [a.lower() for a in args if not a.startswith("-")]
        flags = _flags_of(args)
        for what, progs, words, gap, dry, need, anywhere in PUBLISH_RULES:
            if head not in progs:
                continue
            if words and not ((words[0] in pos) if anywhere else _words_match(pos, words, gap)):
                continue
            if (dry and flags & set(dry)) or (need and not flags & set(need)):
                continue
            return out + [what]
    if head in ("pnpm", "yarn", "bun") and args and not args[0].startswith("-") and args[0].lower() not in _PM_OWN:
        return out + _deeper(H, args, depth, ctx)           # `pnpm wrangler deploy` runs the local binary
    return out


def _publish_segment(H, toks, depth, ctx):
    """Every publish family one command segment runs (a list; a wrapper can hold several)."""
    if not toks:
        return []
    h0 = toks[0]
    if ("=" not in h0 and "\\" not in h0 and not h0.startswith("$") and (_prog(h0) not in _KNOWN_HEADS and not _prog(h0).startswith("python"))
            and not _find_rest(toks)):
        return []
    if _is_anchor_tool(H, toks):
        return []
    out = _segment_once(H, toks, depth, ctx)
    if not out and any("\\" in t for t in toks):       # g\it push: the same segment with the escapes removed
        out = _segment_once(H, [t.replace("\\", "") if "\\" in t else t for t in toks], depth, ctx)
    return out


def _publish_scan(H, text, depth, ctx):
    """The publishes in a text. The brace reading (ctx["psb"]: plain, or the PowerShell-braces reading where every unquoted
    brace is a boundary) is chosen once by publish_families and inherited by every nested text. The characters scanned
    over all levels are summed in ctx["work"]; past PUBLISH_WORK_MAX the scan stops and refuses (fail closed, so a
    command built to be slow cannot outlast the hook's timeout)."""
    if depth > PUBLISH_DEPTH:
        return ["nested wrappers"]
    work = ctx.get("work")
    if work is not None:
        work[0] += len(text)
        if work[0] > PUBLISH_WORK_MAX:
            return ["unreadable command"]
    segs, docs, nested = _shell_split(text, ctx.get("psb", False))
    out = []
    for toks in segs:
        out += _publish_segment(H, toks, depth, ctx)
    for s in nested:
        out += _publish_scan(H, s, depth + 1, ctx)
    if ctx.get("shell_stdin"):          # a heredoc body fed to a shell is a script
        for d in docs:
            out += _publish_scan(H, d, depth + 1, ctx)
    return out


def _merge_hits(a, b):
    """Union of two readings' publish lists by family: each family counts as often as the reading that saw it most."""
    ca, cb = Counter(a), Counter(b)
    return list((ca | cb).elements()) if (ca and cb) else (a or b)


def publish_families(H, cmd):
    """Every publish family the command runs, one entry per publish (short labels, never the command); [] = none.
    Readings: the text as written, and without backticks (PowerShell's escape: gi`t push); each plainly and, when the text
    holds a brace, also in the PowerShell-braces reading (`%{git push}`). The hits are merged by family."""
    if len(cmd) > PUBLISH_MAX_CHARS:
        return ["oversized command"]
    work = [0]
    texts = [cmd] + ([cmd.replace("`", "")] if "`" in cmd else [])
    best = []
    for text in texts:
        for psb in ((False, True) if ("{" in text or "}" in text) else (False,)):
            best = _merge_hits(best, _publish_scan(H, text, 0, {"psb": psb, "work": work}))
    return best


def cached_state(H):
    """The session's state cache read WITHOUT the ledger lock (written atomically); None when missing or not this session."""
    S = _load_json(state_path(H), None)
    return S if isinstance(S, dict) and S.get("sid") == H.sid else None


def publish_grant_open(S, n=1):
    """True while the session holds at least `n` unused allowances that have not expired."""
    try:
        return S.get("pub_n", 0) >= n and dt.datetime.fromisoformat(S["pub_exp"]) > dt.datetime.now().astimezone()
    except Exception:  # noqa: BLE001
        return False


def publish_gate(H, whats):
    """Deny a publish command unless the MAIN session holds enough allowances (one per publish in the command; they are
    all used up together). The DENY is decided before the ledger lock is taken, from the state cache: no grant, a helper,
    a cold cache or a busy lock all deny at once (a hook that waits out its timeout lets the command through). The lock
    is taken only to use up a grant, with a short wait. The refusal is logged afterwards, best effort."""
    count = len(whats)
    S = cached_state(H)
    actor, _aid, _label = _actor_of(H, S or {})
    if actor == "main" and S is not None and publish_grant_open(S, count):
        try:
            with Txn(H, tries=PUBLISH_LOCK_TRIES, strict=True, light=True) as tx:
                if publish_grant_open(tx.state, count):
                    for w in whats:
                        tx.emit({"ev": "publish_allowed", "what": w})
                    return None
        except BaseException:  # noqa: BLE001 - lock busy or log unwritable: deny
            log_error()
    try:
        with Txn(H, tries=PUBLISH_LOG_TRIES, light=True) as tx:
            tx.nosave = True                    # the refusal changes nothing in state: never write the cache here
            tx.emit({"ev": "refusal", "task": tx.state.get("task"), "guard": "publish", "would": False,
                     "what": whats[0], "count": count, "actor": actor, "detail": PUBLISH_DENY})
    except BaseException:  # noqa: BLE001
        log_error()
    return deny(PUBLISH_DENY)


def h_pre_bash(H):
    cmd = str(H.ti.get("command") or "")
    try:
        matched = ledger_bypass(H, cmd)
    except Exception:  # noqa: BLE001 - a bug in the matcher must not open the door
        matched = "unreadable command"
    if not matched and H.agent_id and runs_ledger_note(cmd):
        matched = NOTE_BY_HELPER                # notes are the main session's only (contracts 1.2 bypass_note)
    if matched:
        try:
            extra = bypass_extra(H, cmd)        # after the decision, on a clipped copy, outside the lock
        except BaseException:  # noqa: BLE001
            extra = {}
        try:                                    # the deny never depends on the log being writable
            with Txn(H) as tx:
                rec = {"ev": "bypass_attempt", "tool": H.tool, "matched": matched, "denied": True}
                rec.update(extra)
                rec.update(bypass_actor(H, tx))
                tx.emit(rec)
        except BaseException:  # noqa: BLE001
            log_error()
        return deny(BYPASS_DENY)
    try:
        whats = publish_families(H, cmd)
    except BaseException:  # noqa: BLE001 - a bug in the matcher must not open the door
        log_error()
        whats = ["unreadable command"]
    if whats:
        return publish_gate(H, whats)
    team = TEAM_RE.match(cmd.split("\n", 1)[0]) if ":" in cmd[:8] else None
    forged = forged_grant(cmd) if "claude" in cmd.lower() else False
    if not team and not forged:
        return None
    if forged:
        with Txn(H) as tx:
            tx.emit({"ev": "ignored", "what": "forged grant command"})
        return deny("Only the operator can grant rework rounds, by typing /grant-rework. "
                    "This command was refused.")
    kind, rest = team.group(1).lower(), team.group(2)
    with Txn(H) as tx:
        if H.agent_id:
            tx.emit({"ev": "ignored", "what": "team-" + kind + " from a helper"})
            return None
        S = tx.state
        if kind == "type":
            rest, plan_ref, problem = split_plan_ref(rest)
            if problem:
                tx.emit({"ev": "ignored", "what": problem})
                if problem == "team-type: bad arguments":
                    return None
            m = TYPE_RE.match(rest)
            if not m:
                tx.emit({"ev": "ignored", "what": "team-type: bad arguments"})
                return None
            typ = H.norm_type(m.group(1))
            if (S["open"] and S["type"] == typ and _just_opened(S)
                    and (plan_ref is None or plan_ref == S.get("plan_ref"))):
                tx.emit({"ev": "ignored", "what": "team-type: duplicate within 60 s, no-op"})
                return None
            open_task(H, tx, "work_type", typ, area_arg=(m.group(2) or "").lower() or None, plan_ref=plan_ref)
        elif kind == "summary":
            raw = rest.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                raw = raw[1:-1]
            try:
                obj = json.loads(raw)
                assert isinstance(obj, dict)
            except Exception:  # noqa: BLE001
                tx.emit({"ev": "ignored", "what": "team-summary: bad json"})
                return None
            if not S["open"]:
                tx.emit({"ev": "ignored", "what": "team-summary: no open task"})
                return None
            e = {"ev": "summary", "task": S["task"]}
            for k in ("goal", "produced", "decisions", "open"):
                if obj.get(k) is not None:
                    e[k] = str(obj[k])[:MAX_STR]
            try:
                if obj.get("review_min") is not None:
                    e["review_min"] = int(obj["review_min"])
            except (TypeError, ValueError):
                pass
            tx.emit(e)
        elif kind == "done":
            if not S["open"]:
                tx.emit({"ev": "ignored", "what": "team-done: no open task"})
                return None
            ok = ((not S["needs_check"] and S["edits"] == 0) or S["version"] == 0
                  or S["last_verified"] >= S["version"])
            if not ok:
                reason = ("Not closed: version %d has no passing review (last READY at version %d). "
                          "Run the reviewer on the final version, then close."
                          % (S["version"], S["last_verified"]))
                out = refuse(H, tx, "done_lock", reason)
                if out is not None:
                    return out
            tx.emit({"ev": "task_done", "task": S["task"], "version": S["version"]})
        elif kind == "auto":
            marker = rest.strip().split()[0] if rest.strip() else ""
            if marker:
                tx.emit({"ev": "auto", "marker": marker[:80]})
    return None


FORWARD_DEPTH = 6
FORWARD_NODES = 2000
FORWARD_TOO_BIG = "/grant-rework (forward input too large to inspect)"   # matches USER_ONLY_RE: the hook refuses


def _forward_texts(ti, depth=0, budget=None):
    """Every text a forwarded tool input could carry: strings under FORWARD_KEYS and strings directly inside lists,
    recursing through dicts and lists (depth <= FORWARD_DEPTH, <= FORWARD_NODES nodes). FAILS CLOSED: input that
    exceeds either bound yields a sentinel that USER_ONLY_RE matches, so it is refused rather than half-read."""
    budget = budget if budget is not None else [FORWARD_NODES]
    if not isinstance(ti, (dict, list)):
        return
    if depth > FORWARD_DEPTH:
        yield FORWARD_TOO_BIG
        return
    items = ti.items() if isinstance(ti, dict) else enumerate(ti)
    listed = isinstance(ti, list)
    for k, v in items:
        budget[0] -= 1
        if budget[0] < 0:
            yield FORWARD_TOO_BIG
            return
        if isinstance(v, str):
            if listed or k in FORWARD_KEYS:
                yield v
        elif isinstance(v, (dict, list)):
            yield from _forward_texts(v, depth + 1, budget)


def h_pre_forward(H):
    """A scheduled prompt (CronCreate, ScheduleWakeup) or a message to another session (SendMessage) that STARTS with
    /ack-break or /grant-rework would later arrive as a UserPromptSubmit: refuse it here, in every mode."""
    if not any(USER_ONLY_RE.match(t) for t in _forward_texts(H.ti)):
        return None
    with Txn(H) as tx:
        tx.emit({"ev": "ignored", "what": ("forged user-only command via " + H.tool)[:60]})
    return deny("/ack-break, /grant-rework and /allow-publish are typed by the operator only; this scheduled prompt "
                "or message was refused.")


def h_pre_skill(H):
    name = str(H.ti.get("skill") or H.ti.get("name") or "").split(":")[-1].strip()
    if name == "grant-rework":
        with Txn(H) as tx:
            tx.emit({"ev": "ignored", "what": "model invoked grant-rework skill"})
        return deny("grant-rework is user-only: only the operator can grant another round by typing /grant-rework.")
    if name == "ack-break":
        with Txn(H) as tx:
            tx.emit({"ev": "ignored", "what": "model invoked ack-break skill"})
        return deny("ack-break is user-only: only the operator can acknowledge an integrity break by typing /ack-break.")
    if name == "allow-publish":
        with Txn(H) as tx:
            tx.emit({"ev": "ignored", "what": "model invoked allow-publish skill"})
        return deny("allow-publish is user-only: only the operator can allow a publish, by typing /allow-publish.")
    if name and name in H.playbook_names():
        with Txn(H) as tx:
            open_playbook(H, tx, name)
    return None


REMIND_OPEN_S = 3 * 3600
REMIND_SPAWNS = 5


def reminder_for(H, S, helper, tripped_now):
    """Why the main session should be reminded to declare a new piece of work (None = no reminder). Once per task."""
    if H.agent_id or not S.get("open") or S.get("reminded") == S.get("task"):
        return None
    if helper in H.builders and tripped_now and H.mode != "enforce":
        return "is over its rework budget"
    try:
        age = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(S["task_t"])).total_seconds()
    except Exception:  # noqa: BLE001
        return None
    if age > REMIND_OPEN_S and S.get("spawns", 0) + 1 >= REMIND_SPAWNS:
        return "has been open a long time"
    return None


def h_pre_agent(H):
    helper = str(H.ti.get("subagent_type") or "").lower()
    why = None
    with Txn(H) as tx:
        S = tx.state
        if S["open"] and S["type"] == "quick-question" and helper != "scout":
            out = refuse(H, tx, "quick_question",
                         "A quick question can't change files: declare a type with a checker first "
                         "(: team-type coding|writing|chore ...).")
            if out is not None:
                return out
        elif helper in H.builders and tripped(S):
            out = refuse(H, tx, "rework",
                         "Rework budget spent for this %s task (%d NEEDS CHANGES, budget %s). Stop and bring "
                         "the operator the blockers; only the operator can grant another round with /grant-rework."
                         % (S["type"], nc_count(S), S["budget"]),
                         helper=helper, label=str(H.ti.get("description") or "")[:60],
                         tuid=H.d.get("tool_use_id"))
            if out is not None:
                return out
        try:
            why = reminder_for(H, S, helper, tripped(S))
        except Exception:  # noqa: BLE001
            why = None
        if why:
            tx.emit({"ev": "reminder", "task": S["task"], "why": why})
        tx.emit({"ev": "helper_start", "task": S["task"], "tuid": H.d.get("tool_use_id") or "t%d" % int(time.time()),
                 "helper": helper, "label": str(H.ti.get("description") or "")[:60],
                 "bg": bool(H.ti.get("run_in_background")), "version": S["version"]})
    if why:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": (
            "This task %s. If this is a new piece of work, declare it: "
            "`: team-type <type> [area]` (same type is fine)." % why)}}
    return None


def edit_path(H):
    return H.ti.get("file_path") or H.ti.get("notebook_path") or H.ti.get("path") or ""


def h_pre_edit(H):
    path = edit_path(H)
    if not path:
        return None
    if H.is_protected(path):
        with Txn(H) as tx:
            tx.emit({"ev": "ignored", "what": "write to protected path"})
        return deny("The ledger and the Atlas hooks folder can't be edited by a session.")
    if H.is_exempt(path):
        return None
    with Txn(H) as tx:
        S = tx.state
        if S["open"] and S["type"] == "quick-question":
            return refuse(H, tx, "quick_question",
                          "A quick question can't change files: declare a type with a checker first "
                          "(: team-type coding|writing|chore ...).")
        if not H.agent_id and tripped(S):
            return refuse(H, tx, "rework",
                          "Rework budget spent for this %s task (%d NEEDS CHANGES, budget %s). Stop and bring "
                          "the operator the blockers; only the operator can grant another round with /grant-rework."
                          % (S["type"], nc_count(S), S["budget"]))
    return None


def find_repo(path):
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(12):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def run_check(path):
    root = find_repo(path)
    if not root:
        return None
    script = os.path.join(root, "tools", "check_look.py")
    if not os.path.isfile(script):
        return None
    try:
        r = subprocess.run([sys.executable, script, "--json", path], capture_output=True, text=True,
                           timeout=3, cwd=root)
        obj = json.loads(r.stdout)
        return bool(obj.get("ok")), [str(p)[:100] for p in (obj.get("problems") or [])[:5]]
    except Exception:  # noqa: BLE001
        return None


def refresh_json_copies(H, path):
    try:
        a = _areas_mod()
        p = norm_path(path)
        for key, out in (("workflows_file", "workflows.json"), ("areas_file", "areas.json")):
            src = H.cfg.get(key)
            if src and norm_path(src) == p and os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as fh:
                    obj = a.mini_yaml(fh.read())
                if isinstance(obj, dict):
                    _write_json(os.path.join(CONFIG_DIR, out), obj)
    except Exception:  # noqa: BLE001
        pass


def h_post_edit(H):
    path = edit_path(H)
    if not path:
        return None
    refresh_json_copies(H, path)
    base = os.path.basename(path).lower()
    check = run_check(path) if base in CHECK_FILES else None
    exempt = H.is_protected(path) or H.is_exempt(path)
    if exempt and check is None:
        return None
    out = None
    with Txn(H) as tx:
        S = tx.state
        if not exempt:
            if not S["open"]:
                open_task(H, tx, "work_type", "undeclared", implicit=True)
            tx.emit({"ev": "edit", "task": S["task"], "path": path, "tool": H.tool,
                     "version": S["version"] + 1})
        if check is not None and S["task"]:
            ok, problems = check
            tx.emit({"ev": "check", "task": S["task"], "file": path, "ok": ok, "problems": problems,
                     "version": S["version"]})
            if not ok:
                out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                              "additionalContext": "Check failed for %s: %s"
                                              % (base, "; ".join(problems)[:900])}}
    return out


def _response_text(tr):
    if isinstance(tr, str):
        return tr
    if isinstance(tr, dict):
        c = tr.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            for item in c:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    return item["text"]
        if isinstance(tr.get("text"), str):
            return tr["text"]
    if isinstance(tr, list):
        for item in tr:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    return ""


def capture_verdict(H, tx, aid, helper, text, tuid=None):
    S = tx.state
    if not aid and tuid:
        aid = S["tuid_aid"].get(tuid)
    key = aid or tuid
    if not key or key in S["captured"]:
        return
    res = parse_verdict(text)
    if res is None:
        return
    verdict, findings = res
    # the version is the one the reviewer was LAUNCHED at; unknown stays unknown (null), never the
    # current version, so it can never satisfy the done-lock
    info = S["agents"].get(aid) if aid else None
    if info is None and tuid in S["pending"]:
        p = S["pending"][tuid]
        info = {"version": p["version"], "task": p.get("task")}
    ver = info.get("version") if info else None
    task = (info or {}).get("task") or S["task"]
    tx.emit({"ev": "verdict", "task": task, "agent_id": key, "helper": helper, "verdict": verdict,
             "version": ver, "findings": findings})


def h_pre_handback(H):
    helper = (H.agent_type or "").lower()
    with Txn(H) as tx:
        info = tx.state["agents"].get(H.agent_id) if H.agent_id else None
        if not helper and info:
            helper = (info.get("helper") or "").lower()
        if helper in H.reviewers and H.agent_id:
            capture_verdict(H, tx, H.agent_id, helper, str(H.ti.get("message") or ""))
    return None


def h_post_agent(H):
    tr = H.d.get("tool_response")
    helper = str(H.ti.get("subagent_type") or "").lower()
    tuid = H.d.get("tool_use_id")
    aid = tr.get("agentId") if isinstance(tr, dict) else None
    with Txn(H) as tx:
        S = tx.state
        if aid is None and tuid in S["pending"]:
            aid = S["pending"][tuid].get("agent_id")
        if helper in H.reviewers and not H.ti.get("run_in_background"):
            capture_verdict(H, tx, aid, helper, _response_text(tr), tuid)
        if not H.ti.get("run_in_background") and tuid in S["pending"]:
            S["pending"].pop(tuid, None)
    return None


def meta_tool_use_id(H, aid):
    tp = H.d.get("transcript_path")
    if not tp:
        return None
    m = _load_json(os.path.join(os.path.dirname(tp), H.sid, "subagents", "agent-%s.meta.json" % aid), None)
    if isinstance(m, dict):
        for k in ("toolUseId", "tool_use_id"):
            if isinstance(m.get(k), str):
                return m[k]
    return None


def h_subagent_start(H):
    aid = H.d.get("agent_id")
    if not aid:
        return None
    helper = str(H.d.get("agent_type") or "").lower()
    with Txn(H) as tx:
        S = tx.state
        cands = [(k, p) for k, p in S["pending"].items()
                 if p.get("helper") == helper and not p.get("agent_id") and p.get("task") == S["task"]]
        tuid = None
        exact = meta_tool_use_id(H, aid)
        if exact and any(k == exact for k, _p in cands):
            tuid = exact                      # exact pairing when the helper's meta file names its launch
        elif cands:
            if len(cands) == 1:
                tuid = cands[0][0]
            else:
                order = {k: i for i, (k, _p) in enumerate(S["pending"].items())}
                tuid = min(cands, key=lambda kp: (kp[1]["version"], order[kp[0]]))[0]
        tx.emit({"ev": "helper_paired", "task": S["task"], "agent_id": aid, "helper": helper, "tuid": tuid})
    return None


def h_subagent_stop(H):
    aid = H.d.get("agent_id")
    if not aid:
        return None
    helper = str(H.d.get("agent_type") or "").lower()
    with Txn(H) as tx:
        S = tx.state
        info = S["agents"].get(aid)
        if not helper and not info:
            return None                       # unknown agent, no type: not ours to log
        helper = helper or str((info or {}).get("helper") or "").lower()
        if aid not in S["ended_ids"]:
            tx.emit({"ev": "helper_end", "task": (info or {}).get("task") or S["task"], "agent_id": aid,
                     "helper": helper, "status": H.d.get("status")})
        if helper in H.reviewers:
            capture_verdict(H, tx, aid, helper, str(H.d.get("last_assistant_message") or ""))
    return None


def h_stop(H):
    if H.agent_id:
        return None
    try:
        cost_pass(H)
    except Exception:  # noqa: BLE001
        log_error()
    out = None
    with Txn(H) as tx:
        S = tx.state
        if not S["open"] or H.d.get("stop_hook_active"):
            return None
        if not (S["needs_check"] or S["edits"] > 0):
            return None
        if S["version"] <= S["last_verified"] or S["sent_back_for"] == S["version"]:
            return None
        now = dt.datetime.now().astimezone()
        for p in S["pending"].values():
            if str(p.get("helper")).lower() in H.builders:
                try:
                    age = (now - dt.datetime.fromisoformat(p["t"])).total_seconds()
                except Exception:  # noqa: BLE001
                    age = 0
                if age < 7200:
                    return None
        would = H.mode != "enforce"
        tx.emit({"ev": "sendback", "task": S["task"], "version": S["version"], "would": would})
        if not would:
            out = {"decision": "block",
                   "reason": "Unchecked changes (version %d, last check at %d). Run the check now, or tell "
                             "the operator plainly that the work is not checked yet." % (S["version"], S["last_verified"])}
    return out


# ------------------------------------------------------------------ cost capture (§1.5)

def weight(H, model, prompt):
    m = (model or "").lower()
    for pat, w in H.cfg.get("weights") or [["haiku-5", 0.05], ["haiku", 0.5], ["opus", 2.0], ["sonnet", 1.0]]:
        if pat in m:
            if pat == "haiku-5" and prompt > int(H.cfg.get("haiku5_tier_prompt", 100000)):
                return float(H.cfg.get("haiku5_high_weight", 0.25))
            return float(w)
    return 1.0


def read_new(path, off, deadline, skip_sidechain):
    """(msgs, new_off, title, size) from byte offset `off`; whole lines only; stops at the deadline."""
    size = os.path.getsize(path)
    if size < off:
        off = 0
    msgs, title = {}, None
    pos = off
    n = 0
    with open(path, "rb") as fh:
        fh.seek(off)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            n += 1
            if n % 200 == 0 and time.time() > deadline:
                break
            pos += len(raw)
            if b'"usage"' not in raw and b"custom-title" not in raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "custom-title":
                t = obj.get("customTitle") or obj.get("title") or obj.get("text")
                if isinstance(t, str) and t.strip():
                    title = t.strip()[:MAX_STR]
                continue
            if skip_sidechain and obj.get("isSidechain"):
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            mid = msg.get("id")
            if not mid:
                continue
            u = msg["usage"]
            vals = [int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                    int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)]
            key = "%s|%s" % (mid, obj.get("requestId") or "")
            cur = msgs.get(key)
            if cur is None:
                msgs[key] = {"model": msg.get("model") or "", "v": vals}
            else:
                cur["v"] = [max(a, b) for a, b in zip(cur["v"], vals)]
                if msg.get("model"):
                    cur["model"] = msg["model"]
    return msgs, pos, title, size


def cost_pass(H):
    tp = H.d.get("transcript_path")
    if not tp or not os.path.isfile(tp):
        return
    deadline = time.time() + 2.0
    opath = os.path.join(H.ledger, "state", "offsets-%s.json" % safe_sid(H.sid))
    offs = _load_json(opath, {})
    files_state = offs.get("files", {}) if isinstance(offs, dict) else {}
    targets = [(tp, "main")]
    sub = os.path.join(os.path.dirname(tp), H.sid, "subagents")
    if os.path.isdir(sub):
        for n in sorted(os.listdir(sub)):
            m = re.fullmatch(r"agent-(.+)\.jsonl", n)
            if m:
                targets.append((os.path.join(sub, n), m.group(1)))
    results = []
    for path, scope in targets:
        if time.time() > deadline:
            break
        key = norm_path(path)
        start = int(files_state.get(key, {}).get("off", 0))
        try:
            msgs, pos, title, _size = read_new(path, start, deadline, scope == "main")
        except OSError:
            continue
        results.append((key, path, scope, start, msgs, pos, title))
    if not results:
        return
    with Txn(H) as tx:
        offs = _load_json(opath, {})
        files_state = offs.get("files", {}) if isinstance(offs, dict) else {}
        last_title = offs.get("title") if isinstance(offs, dict) else None
        new_title = None
        totals = {}
        for key, path, scope, start, msgs, pos, title in results:
            fs = files_state.get(key, {"off": 0, "msgs": {}})
            if int(fs.get("off", 0)) != start:
                continue  # another pass moved this file while we read; the next pass catches up
            seen = fs.setdefault("msgs", {})
            if pos < start or (os.path.getsize(path) < start):
                seen.clear()
            for mk, rec in msgs.items():
                prev = seen.pop(mk, [0, 0, 0, 0, 0.0])
                new = [max(a, b) for a, b in zip(prev[:4], rec["v"])]
                w = (new[0] + new[1]) * weight(H, rec["model"], new[0] + new[2] + new[3])
                delta = [new[i] - prev[i] for i in range(4)] + [w - prev[4]]
                seen[mk] = new + [w]
                if any(delta[:4]) or abs(delta[4]) > 1e-9:
                    t = totals.setdefault((scope, rec["model"]), [0, 0, 0, 0, 0.0])
                    for i in range(5):
                        t[i] += delta[i]
            while len(seen) > 200:
                seen.pop(next(iter(seen)))
            fs["off"] = pos
            files_state[key] = fs
            if scope == "main" and title and title != last_title:
                new_title = title
        for (scope, model), t in totals.items():
            e = {"ev": "cost", "scope": scope, "model": model, "in": t[0], "out": t[1], "cache_read": t[2],
                 "cache_write": t[3], "weighted": int(round(t[4]))}
            if scope != "main":
                info = tx.state["agents"].get(scope) or {}
                helper = (info.get("helper") or tx.state.get("helper_of", {}).get(scope)
                          or helper_from_meta(os.path.join(sub, "agent-%s.meta.json" % scope)))
                if helper:
                    e["helper"] = helper
            if new_title:
                e["title"] = new_title
                new_title = None
            tx.emit(e)
        if new_title:
            tx.emit({"ev": "cost", "scope": "main", "model": "", "in": 0, "out": 0, "cache_read": 0,
                     "cache_write": 0, "weighted": 0, "title": new_title})
        out = {"files": files_state, "title": last_title}
        for e in tx.events:
            if e.get("ev") == "cost" and e.get("title"):
                out["title"] = e["title"]
        _write_json(opath, out)


def helper_from_meta(path):
    m = _load_json(path, None)
    if isinstance(m, dict):
        for k in ("agentType", "agent_type", "subagent_type", "type"):
            if isinstance(m.get(k), str):
                return m[k].lower()
    return None


# ------------------------------------------------------------------ dispatch

def log_error():
    try:
        et, _ev, tb = sys.exc_info()
        frames = traceback.extract_tb(tb) if tb else []
        where = "%s:%s" % (os.path.basename(frames[-1].filename), frames[-1].lineno) if frames else "?"
        led = os.environ.get("ATLAS_LEDGER2_DIR") or load_config().get("ledger_dir") or DEFAULT_LEDGER
        os.makedirs(led, exist_ok=True)
        with open(os.path.join(led, "hook-errors.log"), "a", encoding="utf-8") as fh:
            fh.write("%s %s %s\n" % (now_iso(), getattr(et, "__name__", "?"), where))
    except BaseException:  # noqa: BLE001
        pass


def dispatch(H):
    ev, tool = H.ev, H.tool
    if ev == "PreToolUse":
        if tool in ("Bash", "PowerShell"):
            return h_pre_bash(H)
        if tool == "Agent":
            return h_pre_agent(H)
        if tool in ("Edit", "Write", "NotebookEdit"):
            return h_pre_edit(H)
        if tool == "Skill":
            return h_pre_skill(H)
        if tool == "SubagentHandback":
            return h_pre_handback(H)
        if tool in FORWARD_TOOLS:
            return h_pre_forward(H)
    elif ev == "PostToolUse":
        if tool in ("Edit", "Write", "NotebookEdit"):
            return h_post_edit(H)
        if tool == "Agent":
            return h_post_agent(H)
    elif ev == "SessionStart":
        return h_session_start(H)
    elif ev == "SessionEnd":
        return h_session_end(H)
    elif ev == "UserPromptSubmit":
        return h_prompt(H)
    elif ev == "UserPromptExpansion":
        return h_prompt_expansion(H)
    elif ev == "SubagentStart":
        return h_subagent_start(H)
    elif ev == "SubagentStop":
        return h_subagent_stop(H)
    elif ev == "Stop":
        return h_stop(H)
    return None


def main():
    out = None
    try:
        d = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        if isinstance(d, dict) and d.get("session_id"):
            out = dispatch(Ctx(d))
    except BaseException:  # noqa: BLE001 - fail open, always
        log_error()
        out = None
    try:
        if out:
            sys.stdout.write(json.dumps(out))
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
