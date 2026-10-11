"""Ledger v2 API (W1 Lane 1; contracts.md §5.2)."""
from __future__ import annotations

import datetime as dt
import os
import sys
import threading
from pathlib import Path

try:
    from . import query, stats
except ImportError:  # pragma: no cover
    import query  # type: ignore
    import stats  # type: ignore

PREFIX = "/api/v2/ledger/"


def warm() -> "threading.Thread":
    """Derive every task once in a background thread so the first request finds a warm cache."""
    def _run():
        try:
            query.all_tasks()
        except Exception as exc:  # noqa: BLE001
            print(f"ledger2: warm-up failed: {exc!r}", file=sys.stderr)
    t = threading.Thread(target=_run, name="ledger2-warm", daemon=True)
    t.start()
    return t


if "pytest" not in sys.modules:       # at server start (the module is imported once); tests call warm() themselves
    warm()


def _err(code: int, msg: str):
    return code, {"ok": False, "error": msg}


def _week(q: dict):
    w = (q.get("week") or "").strip()
    if not w:
        return None, None
    try:
        mon = stats.week_monday(w)
    except Exception:  # noqa: BLE001
        raise ValueError("week must look like 2026-W41")
    return stats.week_label(mon), mon


def _reports_dir() -> Path:
    return Path(os.environ.get("ATLAS_REPORTS_DIR") or os.path.join(os.path.expanduser("~"), ".atlas", "reports"))


def with_plan_titles(tasks: list[dict]) -> list[dict]:
    """Copies of the tasks, each with `plan_title`: the Plan item's title when its plan_ref resolves, else null (read-only)."""
    refs = {t.get("plan_ref") for t in tasks if t.get("plan_ref")}
    if not refs:
        return [dict(t, plan_title=None) for t in tasks]
    try:
        try:
            from plan import refs as plan_refs
        except ImportError:  # pragma: no cover
            from ..plan import refs as plan_refs
        found = plan_refs.resolve_many(refs)
    except Exception:  # noqa: BLE001
        found = {}
    return [dict(t, plan_title=(found.get(t["plan_ref"]) or {}).get("title") if t.get("plan_ref") else None)
            for t in tasks]


def get(path: str, query_: dict | None = None):
    if not path.startswith(PREFIX):
        return None
    q = query_ or {}
    name = path[len(PREFIX):].strip("/")
    try:
        if name == "tasks":
            label, mon = _week(q)
            if mon is None:
                today = dt.date.today()
                d_from, d_to = (today - dt.timedelta(days=6)).isoformat(), today.isoformat()
                label = stats.week_label(stats.week_monday(None))
            else:
                d_from, d_to = mon.isoformat(), (mon + dt.timedelta(days=6)).isoformat()
            try:
                limit = int(q.get("limit") or 200)
            except ValueError:
                return _err(400, "limit must be an integer")
            ts = query.tasks(d_from, d_to, area=q.get("area") or None, type=q.get("type") or None,
                             status=q.get("status") or None, src=q.get("src") or None, limit=limit)
            return 200, {"ok": True, "week": label, "tasks": with_plan_titles(ts)}
        if name == "task":
            tid = q.get("id") or ""
            task = next((t for t in query.all_tasks() if t["id"] == tid), None)
            if not tid or task is None:
                return _err(404, "unknown task id")
            task = with_plan_titles([task])[0]
            return 200, {"ok": True, "task": task, "events": query.events_for(tid)}
        if name == "stats":
            label, _mon = _week(q)
            return 200, stats.get_stats(label)
        if name == "report":
            label, _mon = _week(q)
            label = label or stats.week_label(stats.week_monday(None))
            p = _reports_dir() / f"{label}.md"
            if not p.is_file():
                return _err(404, f"no saved report for {label}")
            return 200, {"ok": True, "path": str(p), "markdown": p.read_text(encoding="utf-8", errors="replace")}
    except ValueError as e:
        return _err(400, str(e))
    return None


def post(path: str, body: dict):
    return None
