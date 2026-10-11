"""Plan item references (contracts.md section 6, `plan_ref`): resolve an id that a session declared with
`: team-type <type> [area] [task:<id>]` to the Plan item's title. Read-only; never raises.

A ref is a calendar event id (`evt_xxxxxxxx`, or a series occurrence `evt_xxxxxxxx@YYYY-MM-DD`), a tasks.yaml
id (`t_xxxxxxxx`) or a project plan.yaml id (`<project>/<task id>`)."""
from __future__ import annotations

import re

try:
    from . import model, store
except ImportError:  # pragma: no cover
    import model  # type: ignore
    import store  # type: ignore

REF_RE = re.compile(r"^(?:evt_[0-9a-f]{8}(?:@\d{4}-\d\d-\d\d)?|t_[0-9a-f]{8}|"
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,40}/[A-Za-z0-9][A-Za-z0-9._-]{0,60})$")


def valid_ref(ref) -> bool:
    return isinstance(ref, str) and bool(REF_RE.match(ref))


def _index() -> dict:
    """ref base id -> {title, kind, day, start, end}; empty on any trouble."""
    idx: dict = {}
    try:
        sched = store.parse(store.read_text(store.schedule_path()), "schedule.yaml")
        for e in sched.get("events") or []:
            if isinstance(e, dict) and e.get("id"):
                idx[str(e["id"])] = {"title": e.get("title"), "kind": "event", "day": e.get("day"),
                                     "start": e.get("start"), "end": e.get("end")}
    except Exception:  # noqa: BLE001
        pass
    try:
        tdoc = store.parse(store.read_text(store.tasks_path()), "tasks.yaml")
        for t in tdoc.get("tasks") or []:
            if isinstance(t, dict) and t.get("id"):
                idx[str(t["id"])] = {"title": t.get("title"), "kind": "task"}
    except Exception:  # noqa: BLE001
        pass
    try:      # every project plan.yaml task, DONE ones included: a finished task still names the work that was linked
        for pid, t in model.registry_plan_tasks():
            if t.get("id") and t.get("title"):
                idx[f"{pid}/{t['id']}"] = {"title": t.get("title"), "kind": "task"}
    except Exception:  # noqa: BLE001
        pass
    return idx


def resolve_many(refs) -> dict:
    """{ref: {title, kind, ...}} for the refs that resolve; unknown or malformed refs are left out."""
    wanted = {r for r in refs if valid_ref(r)}
    if not wanted:
        return {}
    idx = _index()
    out = {}
    for r in wanted:
        base, _, occ = r.partition("@")
        hit = idx.get(base)
        if hit and hit.get("title"):
            out[r] = dict(hit, **({"day": occ} if occ else {}))
    return out


def resolve(ref) -> dict | None:
    return resolve_many([ref]).get(ref)
