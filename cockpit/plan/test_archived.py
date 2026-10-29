"""Archived areas (contracts.md section 3): kept for history, closed to new work. Temp copies only."""
import pytest
import yaml

from plan import model
from plan.test_api import FAR, create_event, ev_req

ARCHIVED = """  - id: old
    name: Old area
    color: "#8a9a1a"
    attention: shared
    short: OLD
    archived: true
    match: {title_prefix: ["Legacy"], keywords: [Zorblat]}
"""


@pytest.fixture
def arch(env, api):
    env.areas.write_text(env.areas.read_text(encoding="utf-8").rstrip(chr(10)) + chr(10) + ARCHIVED, encoding="utf-8")
    model._plan_cache.clear()
    return api




def test_loader_keeps_archived_area_with_flag(arch):
    areas = {a["id"]: a for a in model.load_areas()}
    assert areas["old"]["archived"] is True
    assert not areas["atlas"].get("archived")
    code, out = arch.get(**{"from": FAR, "to": FAR})
    row = next(a for a in out["areas"] if a["id"] == "old")
    assert row["archived"] is True and row["name"] == "Old area" and row["color"] == "#8a9a1a"
    assert next(a for a in out["areas"] if a["id"] == "atlas")["archived"] is False


def test_new_event_in_archived_area_is_refused(arch):
    code, out = arch.post("event", {"base": arch.version(), "event": ev_req(area="old")})
    assert code == 400 and out["error"] == "area archived: old", out


def test_setting_an_existing_event_to_an_archived_area_is_refused(arch):
    e, _ = create_event(arch, area="atlas")
    code, out = arch.post("event", {"base": arch.version(), "event": {"id": e["id"], "area": "old"}})
    assert code == 400 and out["error"] == "area archived: old", out


def test_new_task_in_archived_area_is_refused(arch):
    code, out = arch.post("task", {"base": arch.version(), "task": {"area": "old", "title": "A memo"}})
    assert code == 400 and out["error"] == "area archived: old", out
    code, out = arch.post("task", {"base": arch.version(), "task": {"area": "atlas", "title": "A memo"}})
    assert code == 200, out
    code, out = arch.post("task", {"base": arch.version(), "task": {"id": out["changed"]["id"], "area": "old"}})
    assert code == 400 and out["error"] == "area archived: old", out


def test_existing_items_in_archived_area_keep_working(env, api):
    # created while the area is still open, then archived
    e, _ = create_event(api, title="Keep me", area="atlas")
    code, out = api.post("task", {"base": api.version(), "task": {"area": "atlas", "title": "Old memo"}})
    assert code == 200
    tid = out["changed"]["id"]
    for path in (env.sched, env.tasks):
        path.write_text(path.read_text(encoding="utf-8").replace("area: atlas", "area: old"), encoding="utf-8")
    env.areas.write_text(env.areas.read_text(encoding="utf-8").rstrip(chr(10)) + chr(10) + ARCHIVED, encoding="utf-8")
    model._plan_cache.clear()
    code, out = api.get(**{"from": FAR, "to": FAR})
    assert code == 200 and next(x for x in out["events"] if x["id"] == e["id"])["area"] == "old"
    code, out = api.post("event/move", {"base": api.version(), "id": e["id"], "day": "2027-03-02", "start": "10:00", "end": "11:00"})
    assert code == 200, out
    code, out = api.post("event", {"base": api.version(), "event": {"id": e["id"], "title": "Retitled", "area": "old"}})
    assert code == 200 and out["changed"]["title"] == "Retitled", out
    code, out = api.post("event/done", {"base": api.version(), "id": e["id"], "done": True})
    assert code == 200, out
    code, out = api.post("task/state", {"base": api.version(), "id": tid, "state": "done"})
    assert code == 200, out
    code, out = api.post("event/delete", {"base": api.version(), "id": e["id"]})
    assert code == 200, out


def test_matching_skips_archived_areas(arch):
    areas = model.load_areas()
    assert model.event_area({"title": "Legacy plan"}, areas) == (None, "none")
    assert model.event_area({"title": "Fix a Zorblat"}, areas) == (None, "none")
    assert model.event_area({"title": "x", "area": "old"}, areas) == ("old", "explicit")
    assert model.event_area({"title": "Atlas fix"}, areas) == ("atlas", "match")
