"""Ledger v2 read side: derived task records + filters (contracts.md §5.2)."""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
from pathlib import Path

try:
    from . import areas as areas_mod, derive, store
except ImportError:  # pragma: no cover
    import areas as areas_mod  # type: ignore
    import derive  # type: ignore
    import store  # type: ignore

REPO = Path(__file__).resolve().parents[2]
_lock = threading.Lock()
TTL_S = 60
_cache: dict = {"sig": None, "tasks": None, "at": 0.0, "running": False}
_yaml_cache: dict = {}


def workflows_path() -> Path:
    return Path(os.environ.get("ATLAS_WORKFLOWS_FILE") or REPO / "workflows.yaml")


def areas_path() -> Path:
    return Path(os.environ.get("ATLAS_AREAS_FILE") or REPO / "areas.yaml")


def _cached_yaml(path: Path):
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _yaml_cache:
        try:
            _yaml_cache[key] = areas_mod.mini_yaml(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _yaml_cache[key] = None
    return _yaml_cache[key]


def load_workflows() -> dict:
    wf = _cached_yaml(workflows_path())
    return wf if isinstance(wf, dict) else {"types": {}, "default_budget": 2}


def load_areas() -> list[dict]:
    return areas_mod.load_areas(str(areas_path()))


def _derive_all(base) -> list[dict]:
    wf, areas = load_workflows(), load_areas()
    ad = store.auto_dir(base)
    markers = {p.stem for p in ad.glob("*.json")} if ad.is_dir() else set()
    tasks = derive.derive_v2(store.read_events(base=base), wf, markers)
    tasks += derive.derive_v1(store.read_v1(base), areas, wf)
    return tasks


def _sig(base):
    wp = workflows_path()
    return (str(base), store.signature(base), wp.stat().st_mtime_ns if wp.exists() else 0)


def _refresh(base, sig) -> None:
    try:
        tasks = _derive_all(base)
        with _lock:
            _cache.update(sig=sig, tasks=tasks, at=time.time())
    finally:
        with _lock:
            _cache["running"] = False


def invalidate() -> None:
    with _lock:
        _cache.update(sig=None, tasks=None, at=0.0)


def all_tasks() -> list[dict]:
    """Every derived task (v2 + v1). The first call derives inline; afterwards the request path only
    serves the cache and, when a ledger file changed or the cache is older than 60 s, starts a
    background re-derivation (never on the request path)."""
    base = store.ledger_dir()
    sig = _sig(base)
    with _lock:
        tasks = _cache.get("tasks")
        same_base = _cache.get("sig") is not None and _cache["sig"][0] == str(base)
        if tasks is not None and same_base:
            stale = _cache["sig"] != sig or time.time() - _cache.get("at", 0) > TTL_S
            if stale and not _cache.get("running"):
                _cache["running"] = True
                threading.Thread(target=_refresh, args=(base, sig), daemon=True).start()
            return tasks
    tasks = _derive_all(base)
    with _lock:
        _cache.update(sig=sig, tasks=tasks, at=time.time(), running=False)
    return tasks


def _day(s):
    if not s:
        return None
    return dt.date.fromisoformat(str(s)[:10])


def _overlaps(t: dict, d_from, d_to) -> bool:
    start = derive.parse_t(t.get("started"))
    end = derive.parse_t(t.get("ended")) or dt.datetime.now().astimezone()
    if start is None:
        return False
    if d_to is not None and start.date() > d_to:
        return False
    if d_from is not None and end.date() < d_from:
        return False
    return True


def tasks(day_from: str | None = None, day_to: str | None = None, area: str | None = None,
          type: str | None = None, status: str | None = None, src: str | None = None,
          limit: int = 200) -> list[dict]:
    d_from, d_to = _day(day_from), _day(day_to)
    out = []
    for t in all_tasks():
        if area and (t.get("area") or "") != area:
            continue
        if type and t.get("type") != type:
            continue
        if status and t.get("status") != status:
            continue
        if src and t.get("src") != src:
            continue
        if (d_from or d_to) and not _overlaps(t, d_from, d_to):
            continue
        out.append(t)
    out.sort(key=lambda t: t.get("started") or "", reverse=True)
    return out[:limit] if limit else out


def auto_runs(day_from, day_to) -> list[dict]:
    res = []
    for t in tasks(day_from, day_to, limit=0):
        if not t.get("auto"):
            continue
        s = t.get("summary") or {}
        res.append({
            "task_id": t["id"], "sid": t["sid"], "title": t.get("session_title") or t.get("title") or t["id"],
            "area": t.get("area"), "start": t.get("started"), "end": t.get("ended"),
            "crew": t.get("crew") or [], "status": t.get("status"),
            "verdict": t.get("final_verdict"), "review_min": s.get("review_min"),
        })
    res.sort(key=lambda r: r["start"] or "")
    return res


def events_for(task_id: str, limit: int = 500) -> list[dict]:
    """Raw events of a task: its sid's events between its start and end, plus events tagged with its id."""
    t = next((x for x in all_tasks() if x["id"] == task_id), None)
    if t is None:
        return []
    if t["src"] == "v1":
        return [e for e in store.read_v1() if e.get("job") == task_id][:limit]
    start, end = t.get("started") or "", t.get("ended") or "9999"
    out = []
    for e in store.read_events():
        if e.get("sid") != t["sid"]:
            continue
        if e.get("task") == task_id or (e.get("task") is None and start <= (e.get("t") or "") <= end):
            out.append(e)
    return out[:limit]
