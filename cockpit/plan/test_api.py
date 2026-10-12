import datetime as dt
import json
import re
import threading
import time

import pytest
import yaml

from plan import model, store


def today_iso(offset=0):
    return (dt.date.today() + dt.timedelta(days=offset)).isoformat()

FAR = "2027-03-01"       # nothing in the real schedule snapshot reaches this far


def ev_req(title="Test event", day=FAR, start="10:00", end="11:00", **kw):
    return {"title": title, "day": day, "start": start, "end": end, **kw}


def create_event(api, base=None, **kw):
    code, out = api.post("event", {"base": api.version() if base is None else base, "event": ev_req(**kw)})
    assert code == 200, out
    return out["changed"], out["version"]


def stored(env, section):
    return yaml.safe_load(env.sched.read_text(encoding="utf-8"))[section]


def sections(text):
    """top-level section texts of the file, for byte comparison."""
    out = {}
    for k in ("availability", "placements", "events", "work_blocks"):
        s, e = store.section_region(text, k)
        out[k] = text[s:e]
    return out


# ── GET ────────────────────────────────────────────────────────────────────────────────────────
def test_get_shape_and_recurring_expansion(api):
    code, out = api.get(**{"from": "2026-08-01", "to": "2026-08-12"})
    assert code == 200 and out["ok"]
    for k in ("version", "today", "areas", "events", "placements", "work_blocks", "availability",
              "agents", "review_tray", "tasks"):
        assert k in out
    assert [a["id"] for a in out["areas"]][:2] == ["md", "job"]
    selfcare = [e for e in out["events"] if e["id"] == "evt_6f1a9d34"]
    assert len(selfcare) == 12                                        # daily Aug 1..12
    by_day = {e["occurrence"]: e for e in selfcare}
    assert by_day["2026-08-01"]["start"] == "08:30"                   # unquoted time normalised
    assert (by_day["2026-08-03"]["start"], by_day["2026-08-03"]["end"]) == ("07:00", "08:00")  # override
    lunch = {e["occurrence"] for e in out["events"] if e["id"] == "evt_c47e2085"}
    assert "2026-08-02" not in lunch and "2026-08-09" not in lunch   # skips
    assert len(lunch) == 10
    hike = [e for e in out["events"] if e["id"] == "evt_5d2a8c41"]
    assert hike == []                                                 # all of its days are skipped


def test_get_default_range_and_bad_range(api):
    code, out = api.get()
    assert code == 200
    code, out = api.get(**{"from": "nope"})
    assert code == 400 and not out["ok"]
    code, out = api.get(**{"from": "2026-10-09", "to": "2026-10-01"})
    assert code == 400


def test_get_placements_blocks_area_mapping(api):
    code, out = api.get(**{"from": "2026-09-15", "to": "2026-09-15"})
    assert any(b["id"] == "blk_f10c93d7" for b in out["work_blocks"])
    plc = [p for p in out["placements"] if p["session"] == "sess_2f619cea"][0]
    assert plc["project"] == "client-app" and plc["area"] == "admin"     # areas.yaml projects
    assert plc["start"] == "08:00" and plc["end"] == "09:45"


def test_event_area_resolution(api):
    e1, _ = create_event(api, title="Product: kickoff")
    e2, _ = create_event(api, title="Product: other", day="2027-03-02", area="atlas")
    e3, _ = create_event(api, title="Dentist", day="2027-03-03")
    code, out = api.get(**{"from": FAR, "to": "2027-03-03"})
    by = {e["id"]: e for e in out["events"]}
    assert (by[e1["id"]]["area"], by[e1["id"]]["area_source"]) == ("md", "match")
    assert (by[e2["id"]]["area"], by[e2["id"]]["area_source"]) == ("atlas", "explicit")
    assert (by[e3["id"]]["area"], by[e3["id"]]["area_source"]) == (None, "none")
    assert by[e1["id"]]["title"] == "Product: kickoff"          # titles never rewritten


def test_tasks_per_area_include_read_only_plan_yaml(api):
    code, out = api.post("task", {"base": api.version(), "task": {"area": "md", "title": "Draft FAQ", "estimate_min": 45}})
    assert code == 200
    t = out["changed"]
    assert t["id"].startswith("t_") and t["state"] == "todo" and t["owner"] == "me" and t["reversible"] is True
    code, out = api.get()
    assert [r["title"] for r in out["tasks"]["md"]] == ["Draft FAQ"]
    train = out["tasks"]["train"]
    assert [(r["id"], r["source"], r["owner"], r["estimate_min"], r["state"]) for r in train] == [
        ("learning-app/week-view", "plan.yaml", "agents", 120, "next"),
        ("learning-app/zones", "plan.yaml", "me", 90, "todo")]            # done one left out
    assert train[0]["project"] == "learning-app"


# ── event writes ───────────────────────────────────────────────────────────────────────────────
def test_event_create_assigns_id_and_persists(api, env):
    ev, ver = create_event(api)
    assert re.fullmatch(r"evt_[0-9a-f]{8}", ev["id"]) and ev["type"] == "personal"
    assert ver > 1
    assert any(e["id"] == ev["id"] and e["start"] == "10:00" for e in stored(env, "events"))


def test_event_merge_null_removes_and_unknown_fields_survive(api, env):
    ev, v = create_event(api, notes="n1", location="Paris", remind=15, busy=False, wibble={"a": 1})
    eid = ev["id"]
    code, out = api.post("event", {"base": v, "event": {"id": eid, "title": "Renamed", "notes": None}})
    assert code == 200
    now = out["changed"]
    assert now["title"] == "Renamed" and "notes" not in now                # null removes
    assert now["location"] == "Paris" and now["remind"] == 15 and now["busy"] is False   # kept
    assert now["wibble"] == {"a": 1}                                       # unknown kept
    assert now["start"] == "10:00"                                         # fields not sent kept
    on_disk = [e for e in stored(env, "events") if e["id"] == eid][0]
    assert on_disk == now


def test_event_validation(api):
    base = api.version()
    assert api.post("event", {"base": base, "event": ev_req(area="nope")})[0] == 400
    assert api.post("event", {"base": base, "event": ev_req(title=" ")})[0] == 400
    assert api.post("event", {"base": base, "event": ev_req(day="2027-13-40")})[0] == 400
    assert api.post("event", {"base": base, "event": ev_req(start="25:00")})[0] == 400
    assert api.post("event", {"base": base, "event": {"title": "x", "day": FAR, "start": "10:00"}})[0] == 400
    assert api.post("event", {"base": base, "event": "str"})[0] == 400
    assert api.post("event", {"event": ev_req()})[0] == 400                # base missing
    code, out = api.post("event", {"base": base, "event": ev_req(area="")})
    assert code == 200 and "area" not in out["changed"]
    assert api.post("event", {"base": True, "event": ev_req()})[0] == 400


def test_event_delete_and_404(api, env):
    ev, v = create_event(api)
    code, out = api.post("event/delete", {"base": v, "id": ev["id"]})
    assert code == 200
    assert all(e["id"] != ev["id"] for e in stored(env, "events"))
    code, out = api.post("event/delete", {"base": out["version"], "id": ev["id"]})
    assert code == 404 and not out["ok"]


def test_collision_guard_400_with_v1_reason(api, env):
    a, v = create_event(api, title="First", start="10:00", end="11:00")
    code, out = api.post("event", {"base": v, "event": ev_req(title="Second", start="10:30", end="11:30")})
    assert code == 400
    assert out["error"] == "can't save 'Second': overlaps 'First' (10:00-11:00); 'Second' needs full attention"
    assert len(stored(env, "events")) == len([e for e in stored(env, "events")])
    # touching edges are fine; busy:false is never a candidate
    assert api.post("event", {"base": v, "event": ev_req(title="Next", start="11:00", end="12:00")})[0] == 200
    assert api.post("event", {"base": v, "event": ev_req(title="Free", start="10:15", end="10:45", busy=False)})[0] == 200
    # an all-day event is always allowed, even on a busy day (no busy check against timed events)
    code, out = api.post("event", {"base": v, "event": {"title": "All day", "day": FAR}})
    assert code == 200
    # a notes-only edit on an event is not re-guarded
    assert api.post("event", {"base": api.version(), "event": {"id": a["id"], "notes": "x"}})[0] == 200


def test_placement_collision_message_matches_v1(api):
    code, out = api.post("placement/add", {"base": api.version(), "project": "p", "task": "t", "day": "2026-09-15",
                                           "start": "08:30", "end": "09:00"})
    assert code == 400 and out["error"] == "can't add 't': collides with review-demo (08:00–09:45)"


# ── versions / 409 ─────────────────────────────────────────────────────────────────────────────
def test_stale_base_is_409_and_other_objects_do_not_conflict(api):
    a, v1 = create_event(api, title="A", day="2027-03-02")
    b, v2 = create_event(api, title="B", day="2027-03-03", base=v1)
    assert v2 > v1
    # edit A with a base that predates A's last write: conflict
    code, out = api.post("event", {"base": v1 - 1, "event": {"id": a["id"], "notes": "late"}})
    assert code == 409 and not out["ok"]
    assert out["current"]["id"] == a["id"] and out["version"] == v2 and "error" in out
    # B was written at v2; a client at v1 editing B conflicts, but editing A (v1) with base v1 does not
    code, out = api.post("event", {"base": v1, "event": {"id": b["id"], "notes": "x"}})
    assert code == 409
    code, out = api.post("event", {"base": v1, "event": {"id": a["id"], "notes": "ok"}})
    assert code == 200
    # an unrelated placement never conflicts, whatever the base
    code, out = api.post("placement/delete", {"base": 1, "session": "sess_2f619cea"})
    assert code == 200


def test_version_increases_on_every_write_and_get_agrees(api):
    v0 = api.version()
    _, v1 = create_event(api)
    assert v1 > v0 and api.version() == v1


def test_external_hand_edit_is_detected_and_bumps(api, env):
    ev, v = create_event(api, title="Hand")
    assert api.version() == v
    text = env.sched.read_text(encoding="utf-8").replace("title: Hand\n", "title: Hand edited\n")
    env.sched.write_text(text, encoding="utf-8")
    v_after = api.version()
    assert v_after == v + 1
    assert api.version() == v_after                                   # stable once seen
    code, out = api.post("event", {"base": v, "event": {"id": ev["id"], "notes": "late"}})
    assert code == 409 and out["current"]["title"] == "Hand edited"
    code, out = api.post("event", {"base": v_after, "event": {"id": ev["id"], "notes": "ok"}})
    assert code == 200


def test_external_delete_is_detected(api, env):
    ev, v = create_event(api)
    lines = yaml.safe_load(env.sched.read_text(encoding="utf-8"))
    events = [e for e in lines["events"] if e["id"] != ev["id"]]
    text = env.sched.read_text(encoding="utf-8")
    env.sched.write_text(store.splice(text, {"events": events}), encoding="utf-8")
    assert api.version() == v + 1


def test_first_run_baselines_without_bump(api, env):
    assert not env.ver.exists()
    v = api.version()
    assert v == 1 and env.ver.exists()
    saved = json.loads(env.ver.read_text())
    assert saved["version"] == 1 and "evt:evt_d82b64e1" in saved["objects"]


# ── parse failure / missing files ──────────────────────────────────────────────────────────────
def test_parse_failure_refuses_and_leaves_file_unchanged(api, env):
    v = api.version()
    env.sched.write_text("events:\n- id: x\n  title: [unclosed\nplacements: ::\n", encoding="utf-8")
    before = env.sched.read_bytes()
    code, out = api.post("event", {"base": v, "event": ev_req()})
    assert code == 500 and not out["ok"] and "does not parse" in out["error"]
    assert api.get()[0] == 500
    assert env.sched.read_bytes() == before
    assert not (env.tmp / "schedule.yaml.tmp").exists()
    # tasks.yaml likewise
    env.tasks.write_text("tasks: [\n", encoding="utf-8")
    b2 = env.tasks.read_bytes()
    code, out = api.post("task", {"base": v, "task": {"area": "md", "title": "t"}})
    assert code == 500
    assert env.tasks.read_bytes() == b2


def test_missing_files_read_empty_and_writes_are_503(api, env):
    env.sched.unlink()
    env.tasks.unlink()
    code, out = api.get()
    assert code == 200 and out["events"] == [] and out["placements"] == [] and out["work_blocks"] == []
    assert out["availability"] == {"blocks": [], "full_days": []}
    assert out["review_tray"] == [] and all(v == [] for v in out["tasks"].values() if v is not out["tasks"].get("train"))
    for route, body in [("event", {"event": ev_req()}), ("block", {"day": FAR, "start": "09:00", "end": "10:00"}),
                        ("placement/add", {"project": "p", "task": "t", "day": FAR, "start": "09:00", "end": "10:00"}),
                        ("task", {"task": {"area": "md", "title": "t"}}),
                        ("task/state", {"id": "x", "state": "done"}),
                        ("review/done", {"id": "rv_x"})]:
        code, out = api.post(route, {"base": 1, **body})
        assert (code, out["error"]) == (503, "no plan file"), route
    assert not env.sched.exists() and not env.tasks.exists()


def test_missing_tasks_file_only_blocks_task_writes(api, env):
    env.tasks.unlink()
    assert api.get()[0] == 200
    assert api.post("event", {"base": api.version(), "event": ev_req()})[0] == 200
    assert api.post("task", {"base": api.version(), "task": {"area": "md", "title": "t"}})[0] == 503
    assert not env.tasks.exists()


# ── section isolation ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("route,body_fn,written", [
    ("event", lambda: {"event": ev_req()}, {"events"}),
    ("placement/add", lambda: {"project": "p", "task": "t", "day": FAR, "start": "09:00", "end": "10:00"}, {"placements"}),
    ("placement/move", lambda: {"session": "sess_2f619cea", "day": FAR, "start": "09:00", "end": "10:00"}, {"placements"}),
    ("block", lambda: {"day": FAR, "start": "09:00", "end": "10:00", "label": "L"}, {"work_blocks"}),
    ("event/delete", lambda: {"id": "evt_3c81f5a2"}, {"events"}),
])
def test_other_sections_stay_byte_identical_on_the_fixture_schedule(api, env, route, body_fn, written):
    before = env.sched.read_text(encoding="utf-8")
    code, out = api.post(route, {"base": api.version(), **body_fn()})
    assert code == 200, out
    after = env.sched.read_text(encoding="utf-8")
    b, a = sections(before), sections(after)
    for name in b:
        if name in written:
            assert a[name] != b[name]
        else:
            assert a[name] == b[name], name
    # and everything OUTSIDE the written sections, header comments included, is the same bytes
    regs = sorted(store.section_region(before, k) for k in written)
    regs_a = sorted(store.section_region(after, k) for k in written)
    pre_b, pre_a = before[:regs[0][0]], after[:regs_a[0][0]]
    assert pre_b == pre_a
    post_b, post_a = before[regs[-1][1]:], after[regs_a[-1][1]:]
    assert post_b == post_a
    assert after.startswith("# ATLAS GLOBAL SCHEDULE")
    assert yaml.safe_load(after) is not None


# ── recurring moves ────────────────────────────────────────────────────────────────────────────
def make_series(api, title="Series", start="16:00", end="17:00", day="2027-04-05", **extra):
    code, out = api.post("event", {"base": api.version(), "event": ev_req(
        title=title, day=day, start=start, end=end, area="atlas",
        recur={"freq": "daily", "interval": 1, "until": "2027-04-12"}, **extra)})
    assert code == 200, out
    return out["changed"]


def test_recurring_move_same_day_is_an_override(api, env):
    s = make_series(api)
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07",
                                        "day": "2027-04-07", "start": "18:00", "end": "19:00"})
    assert code == 200
    ev = [e for e in stored(env, "events") if e["id"] == s["id"]][0]
    assert ev["overrides"] == {"2027-04-07": {"start": "18:00", "end": "19:00"}}
    assert "skips" not in ev and ev["start"] == "16:00"
    code, g = api.get(**{"from": "2027-04-06", "to": "2027-04-08"})
    times = {e["occurrence"]: (e["start"], e["end"]) for e in g["events"] if e["id"] == s["id"]}
    assert times == {"2027-04-06": ("16:00", "17:00"), "2027-04-07": ("18:00", "19:00"),
                     "2027-04-08": ("16:00", "17:00")}


def test_recurring_move_cross_day_is_skip_plus_new_event_in_one_write(api, env):
    s = make_series(api, notes="keep me", location="Lab")
    mtime_before = env.sched.stat().st_mtime_ns
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07",
                                        "day": "2027-04-09", "start": "10:00", "end": "11:00"})
    assert code == 200
    new = out["changed"]
    assert new["id"] != s["id"] and new["from_series"] == s["id"]
    assert (new["title"], new["area"], new["type"], new["day"], new["start"]) == ("Series", "atlas", "personal", "2027-04-09", "10:00")
    assert "recur" not in new and out["series"]["skips"] == ["2027-04-07"]
    evs = {e["id"]: e for e in stored(env, "events")}
    assert evs[s["id"]]["skips"] == ["2027-04-07"] and new["id"] in evs
    code, g = api.get(**{"from": "2027-04-06", "to": "2027-04-09"})
    days = sorted(e["occurrence"] for e in g["events"] if e["id"] == s["id"])
    assert days == ["2027-04-06", "2027-04-08", "2027-04-09"]
    assert any(e["id"] == new["id"] and e["day"] == "2027-04-09" for e in g["events"])
    # version: one bump covers both objects
    saved = json.loads(env.ver.read_text())
    assert saved["objects"][f"evt:{s['id']}"]["v"] == saved["objects"][f"evt:{new['id']}"]["v"] == out["version"]


def test_cross_day_move_clears_a_prior_override_and_keeps_times_by_default(api, env):
    s = make_series(api)
    api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07",
                            "day": "2027-04-07", "start": "18:00", "end": "19:00"})
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07", "day": "2027-04-10"})
    assert code == 200
    assert (out["changed"]["start"], out["changed"]["end"]) == ("18:00", "19:00")   # the occurrence's own times
    ev = [e for e in stored(env, "events") if e["id"] == s["id"]][0]
    assert "overrides" not in ev and ev["skips"] == ["2027-04-07"]


def test_move_whole_event_and_collision(api, env):
    a, v = create_event(api, title="Mover", day="2027-05-03")
    code, out = api.post("event/move", {"base": v, "id": a["id"], "day": "2027-05-04", "start": "14:00", "end": "15:00"})
    assert code == 200 and out["changed"]["day"] == "2027-05-04" and out["changed"]["start"] == "14:00"
    b, v = create_event(api, title="Blocker", day="2027-05-05", start="14:30", end="15:30")
    code, out = api.post("event/move", {"base": v, "id": a["id"], "day": "2027-05-05"})
    assert code == 400 and out["error"].startswith("can't move 'Mover': overlaps 'Blocker' (14:30-15:30)")
    assert api.post("event/move", {"base": v, "id": "evt_zzzz", "day": FAR})[0] == 404
    assert api.post("event/move", {"base": v, "id": a["id"], "day": "bad"})[0] == 400


def test_skip_occurrence(api, env):
    s = make_series(api)
    code, out = api.post("event/skip", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-06"})
    assert code == 200 and out["changed"]["skips"] == ["2027-04-06"]
    code, g = api.get(**{"from": "2027-04-05", "to": "2027-04-07"})
    assert sorted(e["occurrence"] for e in g["events"] if e["id"] == s["id"]) == ["2027-04-05", "2027-04-07"]
    single, v = create_event(api, day="2027-06-01")
    assert api.post("event/skip", {"base": v, "id": single["id"], "occurrence": "2027-06-01"})[0] == 400


# ── placements and blocks ──────────────────────────────────────────────────────────────────────
def test_placement_add_move_delete(api, env):
    code, out = api.post("placement/add", {"base": api.version(), "project": "learning-app", "task": "week-view",
                                           "day": FAR, "start": "09:00", "end": "10:00"})
    assert code == 200
    sess = out["session"]
    assert re.fullmatch(r"sess_[0-9a-f]{8}", sess) and out["changed"]["pinned"] is True
    code, out = api.post("placement/move", {"base": api.version(), "session": sess, "day": "2027-03-02",
                                            "start": "11:00", "end": "12:30"})
    assert code == 200 and out["changed"]["day"] == "2027-03-02" and out["changed"]["end"] == "12:30"
    code, g = api.get(**{"from": "2027-03-02", "to": "2027-03-02"})
    assert [p["area"] for p in g["placements"]] == ["train"]
    assert api.post("placement/move", {"base": api.version(), "session": "sess_none", "day": FAR, "start": "09:00", "end": "10:00"})[0] == 404
    assert api.post("placement/move", {"base": api.version(), "session": sess, "day": FAR, "start": "05:00", "end": "07:00"})[0] == 400
    code, out = api.post("placement/delete", {"base": api.version(), "session": sess})
    assert code == 200 and all(p["session"] != sess for p in stored(env, "placements"))


def test_blocks(api, env):
    code, out = api.post("block", {"base": api.version(), "day": FAR, "start": "09:00", "end": "12:00", "label": "Morning"})
    assert code == 200
    blk = out["changed"]
    assert re.fullmatch(r"blk_[0-9a-f]{8}", blk["id"]) and blk["label"] == "Morning"
    code, out = api.post("block", {"base": api.version(), "day": FAR, "start": "11:00", "end": "13:00"})
    assert code == 400 and "collides with block Morning (09:00–12:00)" in out["error"]
    code, out = api.post("block/move", {"base": api.version(), "id": blk["id"], "day": "2027-03-02"})
    assert code == 200 and out["changed"]["day"] == "2027-03-02" and out["changed"]["start"] == "09:00"
    code, out = api.post("block", {"base": api.version(), "block": {"id": blk["id"], "day": "2027-03-02",
                                                                    "start": "09:00", "end": "11:00"}})
    assert code == 200 and out["changed"]["end"] == "11:00" and "label" not in out["changed"]
    assert api.post("block", {"base": api.version(), "day": FAR, "start": "9", "end": "10"})[0] == 400
    code, out = api.post("block/delete", {"base": api.version(), "id": blk["id"]})
    assert code == 200
    assert api.post("block/delete", {"base": api.version(), "id": blk["id"]})[0] == 404


# ── tasks ──────────────────────────────────────────────────────────────────────────────────────
def test_task_upsert_state_and_read_only_plan_tasks(api, env):
    code, out = api.post("task", {"base": api.version(), "task": {"id": "t_fixed", "area": "md", "title": "Landing words",
                                                                  "owner": "agents", "estimate_min": 30, "reversible": False,
                                                                  "planned": {"day": FAR, "start": "09:00", "end": "09:30"}}})
    assert code == 200 and out["changed"]["owner"] == "agents" and out["changed"]["reversible"] is False
    for st in ("next", "doing", "review", "done"):
        code, out = api.post("task/state", {"base": api.version(), "id": "t_fixed", "state": st})
        assert code == 200 and out["changed"]["state"] == st
    on_disk = yaml.safe_load(env.tasks.read_text(encoding="utf-8"))
    assert on_disk["tasks"][0]["state"] == "done" and on_disk["reviews"] == []
    assert env.tasks.read_text(encoding="utf-8").startswith("# Atlas v2: area tasks")      # header comment kept
    base = api.version()
    assert api.post("task/state", {"base": base, "id": "t_fixed", "state": "bogus"})[0] == 400
    assert api.post("task/state", {"base": base, "id": "nope", "state": "done"})[0] == 404
    assert api.post("task/state", {"base": base, "id": "learning-app/week-view", "state": "done"})[0] == 400
    assert api.post("task", {"base": base, "task": {"id": "learning-app/week-view", "area": "train", "title": "x"}})[0] == 400
    assert api.post("task", {"base": base, "task": {"area": "zzz", "title": "x"}})[0] == 400
    assert api.post("task", {"base": base, "task": {"area": "md", "title": ""}})[0] == 400
    assert api.post("task", {"base": base, "task": {"area": "md", "title": "x", "owner": "bob"}})[0] == 400
    assert api.post("task", {"base": 1, "task": {"id": "t_fixed", "title": "stale"}})[0] == 409


# ── agents lanes and review tray ───────────────────────────────────────────────────────────────
def runs_fixture(monkeypatch):
    d = today_iso()
    runs = [
        {"task_id": "aaaa1111-1", "sid": "s1", "title": "Weekly report", "area": "atlas", "start": f"{d}T09:00:00+02:00",
         "end": f"{d}T10:30:00+02:00", "crew": ["implementer"], "status": "done", "verdict": "READY", "review_min": 25},
        {"task_id": "bbbb2222-1", "sid": "s2", "title": "Overlapping", "area": "md", "start": f"{d}T10:00:00+02:00",
         "end": f"{d}T11:00:00+02:00", "crew": [], "status": "done", "verdict": "NEEDS_CHANGES", "review_min": None},
        {"task_id": "cccc3333-1", "sid": "s3", "title": "Later", "area": "md", "start": f"{d}T11:00:00+02:00",
         "end": f"{d}T12:00:00+02:00", "crew": [], "status": "done", "verdict": "READY", "review_min": None},
    ]
    monkeypatch.setattr(model, "_auto_runs", lambda a, b: list(runs))
    return d, runs


def test_agents_lanes_never_overlap(api, monkeypatch):
    d, _ = runs_fixture(monkeypatch)
    api.post("task", {"base": api.version(), "task": {"id": "t_plan", "area": "md", "title": "Planned run", "owner": "agents",
                                                       "planned": {"day": d, "start": "09:30", "end": "10:15"}}})
    api.post("task", {"base": api.version(), "task": {"id": "t_me", "area": "md", "title": "Mine", "owner": "me",
                                                       "planned": {"day": d, "start": "09:30", "end": "10:15"}}})
    code, out = api.get(**{"from": d, "to": d})
    lanes = out["agents"]
    items = [i for l in lanes for i in l["items"]]
    assert {i["task_id"] for i in items} == {"aaaa1111-1", "bbbb2222-1", "cccc3333-1", "t_plan"}   # owner=me left out
    assert {i["kind"] for i in items} == {"actual", "planned"}
    for l in lanes:
        its = sorted(l["items"], key=lambda i: i["start"])
        for x, y in zip(its, its[1:]):
            assert x["end"] <= y["start"]
    assert [l["lane"] for l in lanes] == list(range(1, len(lanes) + 1)) and len(lanes) == 3
    a = [i for i in items if i["task_id"] == "aaaa1111-1"][0]
    assert (a["start"], a["end"], a["area"], a["crew"]) == ("09:00", "10:30", "atlas", ["implementer"])


def test_review_tray_schedule_and_done(api, env, monkeypatch):
    d, _ = runs_fixture(monkeypatch)
    code, out = api.get()
    tray = out["review_tray"]
    assert [t["id"] for t in tray] == ["rv_aaaa1111-1", "rv_cccc3333-1"]        # READY only
    assert tray[0]["review_min"] == 25 and tray[1]["review_min"] == 20 and tray[0]["scheduled"] is None
    from plan import api as plan_api
    assert plan_api.review_tray_count() == 2
    # schedule: an event in the operator's time, derived "scheduled"
    code, out = api.post("review/schedule", {"base": api.version(), "id": "rv_aaaa1111-1", "day": FAR, "start": "14:00", "end": "14:25"})
    assert code == 200, out
    ev = out["changed"]
    assert ev["title"] == "Review: Weekly report" and ev["area"] == "atlas" and ev["review_of"] == "aaaa1111-1"
    code, out = api.get(**{"from": FAR, "to": FAR})
    t0 = [t for t in out["review_tray"] if t["task_id"] == "aaaa1111-1"][0]
    assert t0["scheduled"]["day"] == FAR and t0["scheduled"]["start"] == "14:00"
    assert any(e["review_of"] == "aaaa1111-1" for e in out["events"])
    # rescheduling updates the same event, never duplicates it
    code, out2 = api.post("review/schedule", {"base": out["version"], "id": "rv_aaaa1111-1", "day": FAR, "start": "15:00", "end": "15:25"})
    assert code == 200 and out2["changed"]["id"] == ev["id"] and out2["changed"]["start"] == "15:00"
    assert sum(1 for e in stored(env, "events") if e.get("review_of") == "aaaa1111-1") == 1
    assert api.post("review/schedule", {"base": api.version(), "id": "rv_nope", "day": FAR, "start": "16:00", "end": "16:25"})[0] == 404
    assert api.post("review/schedule", {"base": api.version(), "id": "aaaa", "day": FAR, "start": "16:00", "end": "16:25"})[0] == 400
    # done: leaves the tray, recorded in tasks.yaml
    code, out = api.post("review/done", {"base": api.version(), "id": "rv_aaaa1111-1"})
    assert code == 200 and out["changed"]["task_id"] == "aaaa1111-1" and "at" in out["changed"]
    assert yaml.safe_load(env.tasks.read_text(encoding="utf-8"))["reviews"][0]["task_id"] == "aaaa1111-1"
    code, g = api.get()
    assert [t["id"] for t in g["review_tray"]] == ["rv_cccc3333-1"]
    assert api.post("review/done", {"base": api.version(), "id": "rv_aaaa1111-1"})[0] == 200   # idempotent
    assert api.post("review/done", {"base": api.version(), "id": "rv_zzzz"})[0] == 404


def test_tray_changes_from_the_ledger_do_not_bump(api, monkeypatch):
    runs_fixture(monkeypatch)
    v = api.version()
    assert api.version() == v and api.version() == v


# ── concurrency ────────────────────────────────────────────────────────────────────────────────
def test_concurrent_writes_lose_nothing(api, env):
    base = api.version()
    results = []

    def worker(i):
        day = (dt.date(2027, 7, 1) + dt.timedelta(days=i)).isoformat()
        results.append(api.post("event", {"base": base, "event": ev_req(title=f"T{i}", day=day)}))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert [c for c, _ in results] == [200] * 12
    titles = {e["title"] for e in stored(env, "events")}
    assert {f"T{i}" for i in range(12)} <= titles
    versions = sorted(o["version"] for _, o in results)
    assert versions == sorted(set(versions)) and len(versions) == 12      # every write its own version
    assert api.version() == versions[-1]


def test_concurrent_edits_of_one_event_conflict_cleanly(api, env):
    ev, v = create_event(api)
    out = []

    def worker(i):
        out.append(api.post("event", {"base": v, "event": {"id": ev["id"], "notes": f"n{i}"}})[0])

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(out) == [200] + [409] * 5


# ── performance and isolation ──────────────────────────────────────────────────────────────────
def test_get_is_fast_on_the_fixture_schedule_size(api, capsys):
    api.get()
    best = 1e9
    for _ in range(5):
        t = time.perf_counter()
        code, _ = api.get()
        best = min(best, time.perf_counter() - t)
        assert code == 200
    with capsys.disabled():
        print(f"\nGET /api/v2/plan best of 5 on the 2246-line schedule: {best * 1000:.1f} ms")
    assert best < 0.2


def test_get_never_touches_state_snapshot_or_transcripts():
    import pathlib
    src = "".join(p.read_text(encoding="utf-8") for p in pathlib.Path(model.__file__).parent.glob("*.py")
                  if not p.name.startswith("test_") and p.name != "conftest.py")
    assert not re.search(r"^\s*(import|from)\s+state", src, re.M)
    assert "projects" + "/" not in src and ".jsonl" not in src


def test_routing_returns_none_for_foreign_paths(env):
    from plan import api as plan_api
    assert plan_api.get("/api/v2/attention", {}) is None
    assert plan_api.post("/api/v2/plan/nonsense", {"base": 1}) is None
    assert plan_api.post("/api/v2/plan/event", {}) [0] == 400                 # base missing


def test_notes_only_edit_adds_no_field_and_no_type_default(api, env):
    text = env.sched.read_text(encoding="utf-8")
    env.sched.write_text(store.splice(text, {"events": [{"id": "evt_legacy", "title": "Legacy", "day": FAR}]}), encoding="utf-8")
    code, out = api.post("event", {"base": api.version(), "event": {"id": "evt_legacy", "notes": "n"}})
    assert code == 200 and "type" not in out["changed"] and out["changed"]["notes"] == "n"


def test_corrupt_version_file_is_refused_not_reset(api, env):
    api.version()
    env.ver.write_text("{not json", encoding="utf-8")
    code, out = api.get()
    assert code == 500 and "version" in out["error"]
    code, out = api.post("event", {"base": 1, "event": ev_req()})
    assert code == 500
    assert env.ver.read_text(encoding="utf-8") == "{not json"


def test_get_tray_uses_exactly_the_window_of_the_writes(api, monkeypatch):
    """GET with a wide range must not list review items older than tray_runs() (30 days)."""
    d = today_iso()
    old = today_iso(-(model.TRAY_DAYS + 20))
    runs = [
        {"task_id": "new11111-1", "sid": "s1", "title": "New", "area": "md", "start": f"{d}T09:00:00+02:00",
         "end": f"{d}T10:00:00+02:00", "crew": [], "status": "done", "verdict": "READY", "review_min": 10},
        {"task_id": "old22222-1", "sid": "s2", "title": "Old", "area": "md", "start": f"{old}T09:00:00+02:00",
         "end": f"{old}T10:00:00+02:00", "crew": [], "status": "done", "verdict": "READY", "review_min": 10},
    ]
    monkeypatch.setattr(model, "_auto_runs", lambda a, b: [r for r in runs if a <= r["start"][:10] <= b])
    code, out = api.get(**{"from": old, "to": d})
    assert [t["task_id"] for t in out["review_tray"]] == ["new11111-1"]
    from plan import api as plan_api
    assert plan_api.review_tray_count() == 1
