"""Screen-review rules (docs/v2/review-changes.md, Plan): day window, all-day rule, area keywords,
task placement, agents' lane cap. Temp copies only (conftest)."""
import yaml

from plan import model
from plan.test_api import FAR, create_event, ev_req, stored

DAY = "2027-04-05"


def post_ev(api, **kw):
    return api.post("event", {"base": api.version(), "event": ev_req(**kw)})


def add_task(api, **kw):
    t = {"area": "md", "title": "Write the pitch", "owner": "me", "estimate_min": 90, **kw}
    code, out = api.post("task", {"base": api.version(), "task": t})
    assert code == 200, out
    return out["changed"]


def stored_tasks(env):
    return yaml.safe_load(env.tasks.read_text(encoding="utf-8"))["tasks"]


# ── day window 07:00-22:00 ─────────────────────────────────────────────────────────────────────
def test_window_refuses_events_outside_07_22(api):
    for s, e in (("06:45", "07:30"), ("21:30", "22:15"), ("05:00", "06:00"), ("22:00", "23:00")):
        code, out = post_ev(api, day=DAY, start=s, end=e)
        assert code == 400 and "outside the 07:00-22:00 day" in out["error"], (s, e, out)
    assert post_ev(api, day=DAY, start="07:00", end="08:00")[0] == 200
    assert post_ev(api, day=DAY, start="21:00", end="22:00")[0] == 200


def test_window_applies_to_moves_and_occurrence_retimes(api):
    a, v = create_event(api, day=DAY, start="09:00", end="10:00")
    code, out = api.post("event/move", {"base": v, "id": a["id"], "day": DAY, "start": "21:30", "end": "22:30"})
    assert code == 400 and "outside" in out["error"]
    r, v = create_event(api, day=DAY, start="12:00", end="13:00", recur={"freq": "daily", "interval": 1})
    code, out = api.post("event/move", {"base": v, "id": r["id"], "occurrence": "2027-04-06", "day": "2027-04-06",
                                        "start": "06:00", "end": "07:00"})
    assert code == 400 and "outside" in out["error"]


def test_existing_event_outside_window_is_not_refused_on_a_notes_edit(api, env):
    # legacy data (06:00 event in the real schedule copy) keeps working
    a, v = create_event(api, day=DAY, start="09:00", end="10:00")
    text = env.sched.read_text(encoding="utf-8").replace("start: '09:00'", "start: '06:00'").replace('start: "09:00"', 'start: "06:00"')
    env.sched.write_text(text, encoding="utf-8")
    code, out = api.post("event", {"base": api.version(), "event": {"id": a["id"], "notes": "x"}})
    assert code == 200, out


# ── all-day rule ───────────────────────────────────────────────────────────────────────────────
def test_all_day_event_is_always_allowed_on_a_busy_day(api):
    assert post_ev(api, title="Timed", day=DAY, start="10:00", end="11:00")[0] == 200
    code, out = api.post("event", {"base": api.version(), "event": {"title": "Trip", "day": DAY}})
    assert code == 200, out


def test_day_with_all_day_event_refuses_new_events_with_a_reason(api):
    assert api.post("event", {"base": api.version(), "event": {"title": "Trip", "day": DAY}})[0] == 200
    code, out = post_ev(api, title="Late call", day=DAY, start="15:00", end="16:00")
    assert code == 400 and "already has the all-day event 'Trip'" in out["error"]
    code, out = api.post("event", {"base": api.version(), "event": {"title": "Second", "day": DAY}})
    assert code == 400 and "already has the all-day event 'Trip'" in out["error"]
    # another day is untouched
    assert post_ev(api, title="Other day", day="2027-04-06", start="15:00", end="16:00")[0] == 200


def test_existing_events_stay_editable_and_a_new_day_is_checked(api):
    t, v = create_event(api, title="Timed", day=DAY, start="10:00", end="11:00")
    assert api.post("event", {"base": v, "event": {"title": "Trip", "day": DAY}})[0] == 200
    # the timed event that was there first keeps its day: a notes edit and its own delete work
    assert api.post("event", {"base": api.version(), "event": {"id": t["id"], "notes": "kept"}})[0] == 200
    # but moving another event onto the day is a new arrival
    o, v = create_event(api, title="Elsewhere", day="2027-04-07", start="10:00", end="11:00")
    code, out = api.post("event/move", {"base": v, "id": o["id"], "day": DAY})
    assert code == 400 and "already has the all-day event" in out["error"]


def test_recurring_all_day_occurrence_counts_and_a_skipped_one_does_not(api):
    r = api.post("event", {"base": api.version(), "event": {"title": "Away", "day": DAY, "recur": {"freq": "daily", "interval": 1}}})[1]["changed"]
    nxt = "2027-04-06"
    code, out = post_ev(api, title="X", day=nxt, start="10:00", end="11:00")
    assert code == 400 and "already has the all-day event 'Away'" in out["error"]
    assert api.post("event/skip", {"base": api.version(), "id": r["id"], "occurrence": nxt})[0] == 200
    assert post_ev(api, title="X", day=nxt, start="10:00", end="11:00")[0] == 200


# ── area keywords ──────────────────────────────────────────────────────────────────────────────
def areas():
    return model.load_areas()


def area_of(title, **kw):
    return model.event_area({"title": title, **kw}, areas())


def test_keywords_match_whole_words_anywhere_case_insensitive():
    assert area_of("Morning study") == ("train", "match")
    assert area_of("Easy lecture with a friend") == ("train", "match")
    assert area_of("Workshop") == ("train", "match")
    assert area_of("Call about the paper review") == ("job", "match")
    assert area_of("Atlas review") == ("atlas", "match")
    assert area_of("Declare tax") == ("admin", "match")
    assert area_of("Invoice 2026") == ("admin", "match")
    assert area_of("Invoïce") == ("admin", "match")           # accents ignored
    assert area_of("Bank transfer") == ("admin", "match")


def test_keywords_are_whole_words_only():
    assert area_of("Running late") == (None, "none")
    assert area_of("Jobless") == (None, "none")
    assert area_of("Bikeshed") == (None, "none")
    assert area_of("Dinner") == (None, "none")


def test_prefix_first_then_keywords_file_order_explicit_wins():
    assert area_of("Product study") == ("md", "match")           # prefix beats the Learning keyword
    assert area_of("Atlas paper study") == ("job", "match")              # keywords: first area in file order
    assert area_of("Study", area="pers") == ("pers", "explicit")


def test_api_events_carry_the_keyword_area(api):
    code, out = post_ev(api, title="Lab session", day=DAY, start="08:00", end="09:00")
    assert code == 200
    ev = [e for e in api.get(**{"from": DAY, "to": DAY})[1]["events"] if e["id"] == out["changed"]["id"]][0]
    assert (ev["area"], ev["area_source"]) == ("train", "match")


# ── task placement ─────────────────────────────────────────────────────────────────────────────
def place(api, tid, target, day=DAY, start="10:00", end="11:30", base=None):
    return api.post("task/place", {"base": api.version() if base is None else base, "id": tid, "target": target,
                                   "day": day, "start": start, "end": end})


def test_place_me_creates_a_linked_event_and_sets_planned(api, env):
    t = add_task(api)
    code, out = place(api, t["id"], "me")
    assert code == 200, out
    ev = out["changed"]
    assert (ev["title"], ev["area"], ev["task_of"], ev["day"], ev["start"], ev["end"]) == (
        "Write the pitch", "md", t["id"], DAY, "10:00", "11:30")
    st = [x for x in stored_tasks(env) if x["id"] == t["id"]][0]
    assert st["planned"] == {"day": DAY, "start": "10:00", "end": "11:30"} and st["state"] == "todo"
    # the GET shows the card as planned, state unchanged
    row = [r for r in api.get(**{"from": DAY, "to": DAY})[1]["tasks"]["md"] if r["id"] == t["id"]][0]
    assert row["planned"] == {"day": DAY, "start": "10:00", "end": "11:30", "on": "me"} and row["state"] == "todo"
    # placing again moves the same event
    code, out = place(api, t["id"], "me", start="14:00", end="15:30")
    assert code == 200 and out["changed"]["id"] == ev["id"]
    assert len([e for e in stored(env, "events") if e.get("task_of") == t["id"]]) == 1


def test_planned_carries_done_once_the_linked_event_is_done(api):
    t = add_task(api)
    code, out = place(api, t["id"], "me")
    assert code == 200, out
    row = lambda: [r for r in api.get(**{"from": DAY, "to": DAY})[1]["tasks"]["md"] if r["id"] == t["id"]][0]
    assert "done" not in row()["planned"]
    code, _ = api.post("event/done", {"base": api.version(), "id": out["changed"]["id"], "done": True})
    assert code == 200
    assert row()["planned"] == {"day": DAY, "start": "10:00", "end": "11:30", "on": "me", "done": True}
    code, _ = api.post("event/done", {"base": api.version(), "id": out["changed"]["id"], "done": False})
    assert "done" not in row()["planned"]


def test_place_me_refusals_are_visible_reasons(api):
    t = add_task(api)
    assert place(api, t["id"], "me", start="06:00", end="07:30")[0] == 400
    assert place(api, t["id"], "me", start="11:00", end="10:00")[0] == 400
    assert place(api, t["id"], "nowhere")[0] == 400
    assert place(api, "t_missing", "me")[0] == 404
    post_ev(api, title="Busy", day=DAY, start="10:00", end="12:00")
    code, out = place(api, t["id"], "me")
    assert code == 400 and "overlaps 'Busy' (10:00-12:00)" in out["error"]
    api.post("event", {"base": api.version(), "event": {"title": "Trip", "day": "2027-04-08"}})
    code, out = place(api, t["id"], "me", day="2027-04-08")
    assert code == 400 and "all-day event 'Trip'" in out["error"]


def test_place_plan_yaml_task_creates_only_the_event(api, env):
    before = env.tasks.read_text(encoding="utf-8")
    code, out = place(api, "learning-app/zones", "me")
    assert code == 200, out
    assert out["changed"]["task_of"] == "learning-app/zones" and out["changed"]["area"] == "train"
    assert env.tasks.read_text(encoding="utf-8") == before
    row = [r for r in api.get(**{"from": DAY, "to": DAY})[1]["tasks"]["train"] if r["id"] == "learning-app/zones"][0]
    assert row["planned"]["on"] == "me"
    # an agents' lane slot can't be stored for a read-only task
    code, out = place(api, "learning-app/week-view", "agents")
    assert code == 400 and "read-only" in out["error"]


def test_place_agents_sets_planned_slot_only(api, env):
    t = add_task(api, owner="agents", title="Build it")
    sched_before = env.sched.read_text(encoding="utf-8")
    code, out = place(api, t["id"], "agents")
    assert code == 200, out
    assert out["changed"]["planned"] == {"day": DAY, "start": "10:00", "end": "11:30"}
    assert out["changed"]["state"] == "todo"                    # no automatic trigger
    assert env.sched.read_text(encoding="utf-8") == sched_before
    got = api.get(**{"from": DAY, "to": DAY})[1]
    assert [i["task_id"] for ln in got["agents"] for i in ln["items"]] == [t["id"]]
    row = [r for r in got["tasks"]["md"] if r["id"] == t["id"]][0]
    assert row["planned"]["on"] == "agents"
    # a task of mine does not go on an agents' lane
    me = add_task(api, title="Mine")
    code, out = place(api, me["id"], "agents")
    assert code == 400 and "only an agents' task" in out["error"]


def test_place_version_conflict_is_409(api):
    t = add_task(api)
    old = api.version()
    api.post("task", {"base": old, "task": {"id": t["id"], "title": "Renamed"}})
    code, out = place(api, t["id"], "me", base=old)
    assert code == 409


def test_place_missing_files_are_503_and_never_created(api, env):
    t = add_task(api)
    env.tasks.unlink()
    code, out = place(api, t["id"], "agents")
    assert code in (404, 503)
    assert not env.tasks.exists()


# ── agents' lane cap ───────────────────────────────────────────────────────────────────────────
def test_fourth_overlapping_agents_item_is_refused_with_a_reason(api, env):
    ids = [add_task(api, owner="agents", title=f"Run {i}")["id"] for i in range(4)]
    assert place(api, ids[0], "agents", start="09:00", end="11:00")[0] == 200
    assert place(api, ids[1], "agents", start="09:30", end="11:30")[0] == 200
    assert place(api, ids[2], "agents", start="10:00", end="12:00")[0] == 200
    code, out = place(api, ids[3], "agents", start="10:30", end="11:00")
    assert code == 400 and out["error"].endswith("3 agents already busy 10:00-11:00"), out
    # outside the triple overlap it fits; touching edges are not an overlap
    assert place(api, ids[3], "agents", start="12:00", end="13:00")[0] == 200
    assert place(api, ids[3], "agents", start="08:00", end="09:00")[0] == 200
    # re-placing one of the three inside the same busy time is fine (it does not count itself)
    assert place(api, ids[2], "agents", start="10:15", end="11:45")[0] == 200


def test_actual_runs_count_toward_the_cap(api, monkeypatch):
    day = DAY
    runs = [{"task_id": f"r{i}", "title": f"R{i}", "area": "atlas", "start": f"{day}T09:00:00", "end": f"{day}T11:00:00",
             "crew": [], "status": "done"} for i in range(3)]
    monkeypatch.setattr(model, "_auto_runs", lambda a, b: runs)
    t = add_task(api, owner="agents")
    code, out = place(api, t["id"], "agents", start="10:00", end="10:30")
    assert code == 400 and "3 agents already busy 09:00-11:00" in out["error"]


# ── review fixes ───────────────────────────────────────────────────────────────────────────────
def test_existing_timed_event_can_be_retimed_on_an_all_day_day_but_real_overlaps_stay_refused(api):
    a, v = create_event(api, title="A", day=DAY, start="10:00", end="11:00")
    b, v = create_event(api, title="B", day=DAY, start="13:00", end="14:00", base=v)
    assert api.post("event", {"base": v, "event": {"title": "Trip", "day": DAY}})[0] == 200
    code, out = api.post("event/move", {"base": api.version(), "id": a["id"], "day": DAY, "start": "10:30", "end": "11:30"})
    assert code == 200, out
    code, out = api.post("event/move", {"base": api.version(), "id": a["id"], "day": DAY, "start": "13:30", "end": "14:30"})
    assert code == 400 and "overlaps 'B' (13:00-14:00)" in out["error"]


def test_place_refuses_a_done_task(api):
    t = add_task(api)
    api.post("task/state", {"base": api.version(), "id": t["id"], "state": "done"})
    code, out = place(api, t["id"], "me")
    assert code == 400 and "finished task" in out["error"]
