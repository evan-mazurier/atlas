"""Deadlines and the Brief (design notes, not in this copy): writes, recurrence, builder, GET. Neutral titles, temp copies only."""
import datetime as dt

import pytest
import yaml

from plan import brief, store
from plan.test_api import sections, stored

TODAY = dt.date(2026, 11, 16)


def dl_req(**kw):
    return {"title": "Tax declaration", "day": "2026-12-30", **kw}


def post(api, route, body, base=None):
    return api.post(route, {"base": api.version() if base is None else base, **body})


def make(api, **kw):
    code, out = post(api, "deadline", {"deadline": dl_req(**kw)})
    assert code == 200, out
    return out["changed"]


def build(deadlines, day=TODAY, **extra):
    return brief.build(day, {"deadlines": deadlines, **extra}, None, [])


def D(i, day, **kw):
    return {"id": f"dl_{i:08x}", "title": f"Item {i}", "day": day, **kw}


# ── store round trip and versions ──────────────────────────────────────────────────────────────
def test_round_trip_leaves_other_sections_byte_identical(api, env):
    before = sections(env.sched.read_text(encoding="utf-8"))
    d = make(api, remind_days=7)
    after_text = env.sched.read_text(encoding="utf-8")
    assert sections(after_text) == before
    assert stored(env, "deadlines")[0]["id"] == d["id"] and d["id"].startswith("dl_") and len(d["id"]) == 11
    assert d["kind"] == "deadline" and d["priority"] == "normal"
    # a second write only touches the deadlines section
    make(api, title="Second")
    assert sections(env.sched.read_text(encoding="utf-8")) == before
    # a plain re-splice of the parsed section is stable
    doc = store.parse(after_text, "schedule.yaml")
    assert store.splice(after_text, {"deadlines": doc["deadlines"]}) == after_text


def test_versions_and_409(api):
    d = make(api)
    stale = api.version()
    assert post(api, "deadline/done", {"id": d["id"]}, base=stale)[0] == 200
    code, out = post(api, "deadline/move", {"id": d["id"], "to_day": "2027-01-05"}, base=stale)
    assert code == 409 and out["current"]["id"] == d["id"]
    assert post(api, "deadline/move", {"id": d["id"], "to_day": "2027-01-05"})[0] == 200
    assert api.post("deadline", {"deadline": dl_req()})[0] == 400          # base required


def test_hand_edit_bumps_version(api, env):
    d = make(api)
    v = api.version()
    text = env.sched.read_text(encoding="utf-8").replace("Tax declaration", "Tax declaration x")
    env.sched.write_text(text, encoding="utf-8")
    assert api.version() == v + 1
    assert post(api, "deadline/done", {"id": d["id"]}, base=v)[0] == 409


# ── writes ─────────────────────────────────────────────────────────────────────────────────────
def test_upsert_merge_null_and_bad_values(api, env):
    d = make(api, notes="n", area="admin")
    code, out = post(api, "deadline", {"deadline": {"id": d["id"], "title": "Renamed", "notes": None, "priority": "high"}})
    c = out["changed"]
    assert code == 200 and c["title"] == "Renamed" and "notes" not in c and c["priority"] == "high"
    assert c["area"] == "admin"
    assert post(api, "deadline", {"deadline": {"id": d["id"], "custom_field": "x"}})[0] == 400      # closed field set
    for bad in ({"kind": "other"}, {"priority": "urgent"}, {"day": "2026-13-01"}, {"area": "nope"},
                {"remind_days": -1}, {"remind_days": 367}, {"remind_days": 800000}, {"remind_days": True}, {"remind_days": "3"}, {"title": " "},
                {"recur": {"freq": "yearly"}}, {"recur": {"freq": "daily", "interval": 0}},
                {"recur": {"freq": "daily", "count": 0}}, {"recur": {"freq": "daily", "until": "bad"}},
                {"skips": ["2026-12-30"]}, {"done_on": []}, {"overrides": {}}, {"done": "yes"}):
        code, out = post(api, "deadline", {"deadline": {"id": d["id"], **bad}})
        assert code == 400, (bad, out)
    assert post(api, "deadline", {"deadline": {"id": "dl_00000000", "title": "x"}})[0] == 404
    assert post(api, "deadline", {"deadline": dl_req(title="")})[0] == 400
    assert post(api, "deadline", {"deadline": dl_req(day=None)})[0] == 400
    assert post(api, "deadline", {"deadline": "x"})[0] == 400


def test_archived_area_refused_for_new_item(api, env):
    env.areas.write_text(env.areas.read_text(encoding="utf-8").rstrip("\n") + "\n  - id: old\n    name: Old\n"
                         "    color: \"#8a9a1a\"\n    attention: shared\n    short: OLD\n    archived: true\n", encoding="utf-8")
    code, out = post(api, "deadline", {"deadline": dl_req(area="old")})
    assert code == 400 and out["error"] == "area archived: old"


def test_monthly_anchor_over_28_is_400(api):
    code, out = post(api, "deadline", {"deadline": dl_req(day="2026-12-30", recur={"freq": "monthly"})})
    assert code == 400 and out["error"] == "can't save 'Tax declaration': monthly anchor day must be 28 or less"
    assert post(api, "deadline", {"deadline": dl_req(day="2026-12-28", recur={"freq": "monthly"})})[0] == 200


def test_done_move_delete_single(api, env):
    d = make(api)
    code, out = post(api, "deadline/move", {"id": d["id"], "to_day": "2027-01-02"})
    assert code == 200 and out["changed"]["day"] == "2027-01-02"
    code, out = post(api, "deadline/done", {"id": d["id"]})
    assert out["changed"]["done"] is True
    assert post(api, "deadline/done", {"id": d["id"]})[0] == 200                       # idempotent
    for route, extra in (("done", {}), ("move", {"to_day": "2027-01-03"}), ("delete", {})):
        assert post(api, "deadline/" + route, {"id": d["id"], "occurrence_key": "2027-01-02", **extra})[0] == 400
    assert post(api, "deadline/move", {"id": d["id"], "to_day": "bad"})[0] == 400
    assert post(api, "deadline/done", {"id": "dl_00000000"})[0] == 404
    assert post(api, "deadline/delete", {"id": d["id"]})[0] == 200
    assert stored(env, "deadlines") == []


def test_series_done_move_skip(api, env):
    d = make(api, day="2026-11-15", recur={"freq": "monthly"})
    i = d["id"]
    assert post(api, "deadline/done", {"id": i})[0] == 400                              # key required
    assert post(api, "deadline/done", {"id": i, "occurrence_key": "2026-12-16"})[0] == 400   # not an occurrence
    for k in ("2027-01-15", "2026-12-15", "2026-12-15"):
        code, out = post(api, "deadline/done", {"id": i, "occurrence_key": k})
        assert code == 200
    assert out["changed"]["done_on"] == ["2026-12-15", "2027-01-15"] and "done" not in out["changed"]
    code, out = post(api, "deadline/move", {"id": i, "to_day": "2027-02-20", "occurrence_key": "2027-02-15"})
    assert out["changed"]["overrides"] == {"2027-02-15": {"day": "2027-02-20"}} and out["changed"]["day"] == "2026-11-15"
    code, out = post(api, "deadline/move", {"id": i, "to_day": "2027-02-15", "occurrence_key": "2027-02-15"})
    assert "overrides" not in out["changed"]                                            # back on its day
    post(api, "deadline/move", {"id": i, "to_day": "2027-02-20", "occurrence_key": "2027-02-15"})
    code, out = post(api, "deadline/delete", {"id": i, "occurrence_key": "2027-02-15"})
    assert code == 200 and out["changed"]["skips"] == ["2027-02-15"] and "overrides" not in out["changed"]
    assert post(api, "deadline/done", {"id": i, "occurrence_key": "2027-02-15"})[0] == 400   # skipped
    assert post(api, "deadline/move", {"id": i, "to_day": "2027-03-01", "occurrence_key": "2027-02-15"})[0] == 400
    assert post(api, "deadline/delete", {"id": i, "occurrence_key": "2027-02-15"})[0] == 200  # idempotent
    post(api, "deadline/delete", {"id": i, "occurrence_key": "2026-12-15"})
    assert "2026-12-15" not in (stored(env, "deadlines")[0].get("done_on") or [])
    assert post(api, "deadline/delete", {"id": i})[0] == 200 and stored(env, "deadlines") == []


def test_no_plan_file_is_503(api, env):
    env.sched.unlink()
    assert post(api, "deadline", {"deadline": dl_req()}, base=1)[0] == 503


# ── recurrence ─────────────────────────────────────────────────────────────────────────────────
def keys(dl, hi):
    return [k.isoformat() for k, _, _ in brief._occurrences(dl, dt.date.fromisoformat(hi))]


def test_monthly_interval_until_count():
    base = {"id": "dl_1", "title": "T", "day": "2026-11-15"}
    assert keys({**base, "recur": {"freq": "monthly"}}, "2027-02-28") == ["2026-11-15", "2026-12-15", "2027-01-15", "2027-02-15"]
    assert keys({**base, "recur": {"freq": "monthly", "interval": 2}}, "2027-03-31") == ["2026-11-15", "2027-01-15", "2027-03-15"]
    assert keys({**base, "recur": {"freq": "monthly", "until": "2027-01-15"}}, "2027-06-01") == ["2026-11-15", "2026-12-15", "2027-01-15"]
    assert keys({**base, "recur": {"freq": "monthly", "count": 2}}, "2027-06-01") == ["2026-11-15", "2026-12-15"]


def test_skips_and_overrides_in_occurrences():
    dl = {"id": "dl_1", "title": "T", "day": "2026-11-15", "recur": {"freq": "monthly"}, "skips": ["2026-12-15"],
          "overrides": {"2027-01-15": {"day": "2027-01-20"}, "2027-05-15": {"day": "2027-05-01"}}}
    got = [(k.isoformat(), s.isoformat()) for k, s, _ in brief._occurrences(dl, dt.date(2027, 2, 1))]
    assert got == [("2026-11-15", "2026-11-15"), ("2027-01-15", "2027-01-20"), ("2027-05-15", "2027-05-01")]


LAST_SAT = {"freq": "monthly", "weekday": "sat", "week": "last"}


def test_last_saturday_series():
    dl = {"id": "dl_1", "title": "Monthly review, last Saturday", "day": "2026-11-28", "recur": LAST_SAT}
    assert keys(dl, "2027-03-31") == ["2026-11-28", "2026-12-26", "2027-01-30", "2027-02-27", "2027-03-27"]


def test_first_monday_interval_and_skips():
    dl = {"id": "dl_1", "title": "T", "day": "2026-11-02", "recur": {"freq": "monthly", "weekday": "mon", "week": 1}}
    assert keys(dl, "2027-02-28") == ["2026-11-02", "2026-12-07", "2027-01-04", "2027-02-01"]
    dl["recur"] = {**dl["recur"], "interval": 2}
    assert keys(dl, "2027-03-31") == ["2026-11-02", "2027-01-04", "2027-03-01"]
    dl["skips"] = ["2027-01-04"]
    assert keys(dl, "2027-03-31") == ["2026-11-02", "2027-03-01"]
    dl["recur"]["count"] = 2
    dl["skips"] = []
    assert keys(dl, "2027-12-31") == ["2026-11-02", "2027-01-04"]
    dl["recur"] = {"freq": "monthly", "weekday": "mon", "week": 1, "until": "2026-12-31"}
    assert keys(dl, "2027-12-31") == ["2026-11-02", "2026-12-07"]


def test_weekday_rule_writes(api, env):
    d = make(api, title="Monthly review, last Saturday", day="2026-11-28", recur=LAST_SAT)
    assert d["recur"] == {"freq": "monthly", "interval": 1, "weekday": "sat", "week": "last"}   # no anchor-day rule
    for day, rec in (("2026-11-21", LAST_SAT), ("2026-11-28", {**LAST_SAT, "week": 2}),
                     ("2026-11-28", {**LAST_SAT, "week": 5}), ("2026-11-28", {**LAST_SAT, "weekday": "xyz"}),
                     ("2026-11-28", {"freq": "weekly", "weekday": "sat", "week": "last"}),
                     ("2026-11-28", {"freq": "monthly", "week": "last"}), ("2026-11-28", {**LAST_SAT, "week": True})):
        assert post(api, "deadline", {"deadline": dl_req(day=day, recur=rec)})[0] == 400, (day, rec)
    assert post(api, "deadline/done", {"id": d["id"], "occurrence_key": "2026-12-26"})[0] == 200
    assert post(api, "deadline/done", {"id": d["id"], "occurrence_key": "2026-12-19"})[0] == 400
    code, out = post(api, "deadline", {"deadline": {"id": d["id"], "day": "2026-11-27"}})   # anchor no longer matches
    assert code == 400


# ── builder ────────────────────────────────────────────────────────────────────────────────────
def names(out, block="do_not_miss"):
    return [(e["title"], e.get("group")) for e in out[block]]


def test_groups_order_and_windows():
    out = build([
        D(1, "2026-11-10"),                                       # overdue
        D(2, "2026-11-16"),                                       # due today
        D(3, "2026-11-30", priority="high"),                      # in window (14 default) and high
        D(4, "2026-11-25"),                                       # in window
        D(5, "2026-12-10"),                                       # window starts 11-26: next
        D(6, "2026-11-17", kind="follow-up"),                     # follow-up remind 0: not yet, next
        D(7, "2026-11-16", kind="follow-up"),                     # follow-up on its day: today group
        D(8, "2027-03-01", remind_days=7),                        # beyond 30 days: nowhere
        D(9, "2026-12-01", priority="high", remind_days=3),       # high but outside window: next
    ])
    assert [e["id"] for e in out["do_not_miss"]] == [f"dl_{i:08x}" for i in (1, 2, 7, 3, 4)]
    assert [e["group"] for e in out["do_not_miss"]] == ["overdue", "today", "today", "high", "window"]
    assert [e["id"] for e in out["next"]] == [f"dl_{i:08x}" for i in (6, 9, 5)]
    assert [e["week"] for e in out["next"]] == ["2026-11-16", "2026-11-30", "2026-12-07"]
    c = out["counts"]
    assert (c["do_not_miss"], c["next"], c["overdue"], c["act"]) == (5, 3, 1, 0)
    assert c["deadlines_30"] == 7                       # overdue excluded; 12-10 and 11-30 included, 2027-03-01 not
    assert out["act"] == [] and out["act_status"] == {}


def test_edge_30_days_and_done_hidden():
    out = build([D(1, "2026-12-16", remind_days=0), D(2, "2026-12-17", remind_days=0), D(3, "2026-11-20", done=True)])
    assert [e["id"] for e in out["next"]] == ["dl_00000001"] and out["counts"]["deadlines_30"] == 1
    assert out["do_not_miss"] == []


def test_wording():
    assert [brief.days_text(n) for n in (0, 1, 2, -1, -3)] == ["today", "in 1 day", "in 2 days", "1 day overdue", "3 days overdue"]
    e = build([D(1, "2026-11-13")])["do_not_miss"][0]
    assert (e["days_left"], e["days_text"], e["overdue"]) == (-3, "3 days overdue", True)


def test_series_latest_overdue_only_and_next():
    s = D(1, "2026-08-15", recur={"freq": "monthly"})
    out = build([s])
    assert [(e["occurrence_key"], e["group"]) for e in out["do_not_miss"]] == [("2026-11-15", "overdue")]
    assert [(e["occurrence_key"], e["day"]) for e in out["next"]] == [("2026-12-15", "2026-12-15")]
    s["done_on"] = ["2026-11-15"]
    out = build([s])        # 11-15 done: the latest NOT-done missed one (10-15) is listed
    assert [e["occurrence_key"] for e in out["do_not_miss"]] == ["2026-10-15"] and [e["occurrence_key"] for e in out["next"]] == ["2026-12-15"]
    s["done_on"] = ["2026-08-15", "2026-09-15", "2026-10-15", "2026-11-15"]
    assert build([s])["do_not_miss"] == []


def test_snoozed_occurrence_leaves_overdue_and_series_remind_window():
    s = D(1, "2026-08-15", recur={"freq": "monthly"}, overrides={"2026-11-15": {"day": "2026-11-18"}}, remind_days=3)
    out = build([s])
    # the snoozed 11-15 is no longer overdue: the previous missed one (10-15) takes the overdue slot
    assert [(e["occurrence_key"], e["day"], e["group"]) for e in out["do_not_miss"]] == [
        ("2026-10-15", "2026-10-15", "overdue"), ("2026-11-15", "2026-11-18", "window")]
    assert out["next"] == []
    s["skips"] = ["2026-11-15"]
    out = build([s])
    assert out["do_not_miss"][0]["occurrence_key"] == "2026-10-15" and out["do_not_miss"][0]["overdue"]


def test_one_off_overdue_stays_until_done():
    out = build([D(1, "2026-01-01")])
    assert [e["group"] for e in out["do_not_miss"]] == ["overdue"] and out["do_not_miss"][0]["days_left"] == -319


def test_last_saturday_in_builder():
    s = D(1, "2026-11-28", recur=LAST_SAT, remind_days=14)
    out = build([s])
    assert [(e["occurrence_key"], e["group"]) for e in out["do_not_miss"]] == [("2026-11-28", "window")]
    out = build([s], day=dt.date(2026, 11, 29))
    assert [e["occurrence_key"] for e in out["next"]] == ["2026-12-26"] and out["do_not_miss"][0]["group"] == "overdue"
    out = build([s], day=dt.date(2026, 11, 30))
    assert [(e["occurrence_key"], e["group"]) for e in out["do_not_miss"]] == [("2026-11-28", "overdue")]
    out = build([s], day=dt.date(2026, 12, 14))
    assert [(e["occurrence_key"], e["group"]) for e in out["do_not_miss"]] == [("2026-11-28", "overdue"), ("2026-12-26", "window")]


def test_title_priority_area_notes_and_junk_ignored():
    areas = [{"id": "admin", "match": {}}]
    out = brief.build(TODAY, {"deadlines": [D(1, "2026-11-16", area="admin", notes="who", priority="high", kind="follow-up"),
                                            {"title": "no id", "day": "2026-11-16"}, "junk", D(2, "bad-day")]}, None, areas)
    e = out["do_not_miss"][0]
    assert len(out["do_not_miss"]) == 1 and (e["area"], e["notes"], e["priority"], e["kind"]) == ("admin", "who", "high", "follow-up")


def test_today_items_and_counts():
    sched = {"events": [
        {"id": "evt_a", "title": "Allday", "day": "2026-11-16"},
        {"id": "evt_b", "title": "Late", "day": "2026-11-16", "start": "15:00", "end": "16:00", "done": True},
        {"id": "evt_c", "title": "Weekly", "day": "2026-11-09", "start": "09:00", "end": "10:00",
         "recur": {"freq": "weekly"}, "done_on": ["2026-11-16"]},
        {"id": "evt_d", "title": "Elsewhere", "day": "2026-11-17", "start": "09:00", "end": "10:00"}],
        "work_blocks": [{"id": "blk_1", "day": "2026-11-16", "start": "11:00", "end": "12:00", "label": "Focus"}],
        "placements": [{"session": "sess_1", "project": "p", "task": "Placed", "day": "2026-11-16", "start": "13:00",
                        "end": "14:00", "done": True}]}
    out = brief.build(TODAY, sched, None, [])
    assert [(i["type"], i["title"]) for i in out["today"]] == [
        ("event", "Allday"), ("event", "Weekly"), ("block", "Focus"), ("placement", "Placed"), ("event", "Late")]
    by = {i["title"]: i for i in out["today"]}
    assert by["Weekly"]["occurrence"] == "2026-11-16" and by["Allday"]["occurrence"] is None
    assert by["Weekly"]["done"] and by["Late"]["done"] and not by["Allday"]["done"]
    assert by["Focus"]["can_mark_done"] is False and by["Placed"]["can_mark_done"] is False and by["Placed"]["done"] is True
    assert by["Allday"]["can_mark_done"] is True
    c = out["counts"]
    assert (c["today"], c["events_today"], c["events_done"]) == (5, 3, 2)


# ── GET ────────────────────────────────────────────────────────────────────────────────────────
def test_get_route(api, env):
    from plan import api as plan_api
    d = make(api, day="2026-11-20", title="Licence renewal", priority="high")
    code, out = plan_api.get("/api/v2/plan/brief", {"day": "2026-11-16"})
    assert code == 200 and out["ok"] and out["day"] == "2026-11-16"
    assert [e["id"] for e in out["do_not_miss"]] == [d["id"]] and out["counts"]["deadlines_30"] == 1
    assert out["version"] == api.version()                   # the base a write needs
    code, out = plan_api.get("/api/v2/plan/brief/", {})
    assert code == 200 and out["day"] == dt.date.today().isoformat()
    assert plan_api.get("/api/v2/plan/brief", {"day": "nope"})[0] == 400
    assert plan_api.get("/api/v2/plan/other", {}) is None
    v = api.version()
    plan_api.get("/api/v2/plan/brief", {})
    assert api.version() == v                                # a read writes nothing
    env.sched.unlink()
    assert plan_api.get("/api/v2/plan/brief", {"day": "2026-11-16"})[1]["do_not_miss"] == []


def test_other_readers_ignore_deadlines(api, env):
    make(api)
    from plan import model, refs
    sched, _ = store.read_doc(env.sched)
    assert "dl:" in "".join(model.schedule_objects(sched))
    payload = model.build(sched, {}, dt.date(2026, 12, 29), dt.date(2026, 12, 31), 1, [])
    assert "deadlines" not in payload and all(e["title"] != "Tax declaration" for e in payload["events"])
    assert refs.resolve("dl_00000000") is None
    assert yaml.safe_load(env.sched.read_text(encoding="utf-8"))["deadlines"][0]["title"] == "Tax declaration"


# ── robustness against hand-edited records ─────────────────────────────────────────────────────
def test_remind_days_cap_366_and_clamp():
    assert brief.remind_days({"remind_days": 800000}) == 366 and brief.remind_days({"remind_days": 5}) == 5


def test_malformed_records_are_skipped_and_counted():
    bad = [D(1, "2026-11-20", remind_days=800000),                 # clamped, still shown
           D(2, "2026-11-20", recur={"freq": "daily"}, skips=5),   # skips not a list: treated as empty
           {"id": [1], "title": "list id", "day": "2026-11-20"},
           {"id": "dl_x", "title": "bad day", "day": "nope"},
           "junk", 7, {"title": "no id", "day": "2026-11-20"},
           {"id": "dl_y", "title": "bad recur", "day": "2026-11-20", "recur": {"freq": "daily", "count": []},
            "overrides": 3, "done_on": "x"}]
    out = build(bad)
    ids = {e["id"] for e in out["do_not_miss"] + out["next"]}
    assert {"dl_00000001", "dl_00000002"} <= ids and out["counts"]["skipped"] >= 4
    assert build([D(1, "2026-11-20")])["counts"]["skipped"] == 0


def test_day_extremes_do_not_break_the_builder():
    out = build([D(1, "0001-01-01", recur={"freq": "daily"}), D(2, "9998-12-31", remind_days=366)], day=dt.date(9998, 12, 31))
    assert out["ok"] and out["counts"]["skipped"] in (0, 1, 2)


def test_get_and_writes_survive_malformed_records(api, env):
    from plan import api as plan_api
    good = make(api, day="2026-11-20")
    text = env.sched.read_text(encoding="utf-8")
    extra = ["- {id: [1], title: list id, day: 2026-11-20}",
             "- {id: dl_bad00001, title: bad skips, day: 2026-11-20, skips: 5, done_on: x, overrides: 3,"
             " remind_days: 800000, recur: {freq: daily}}",
             "- junk", "- 7"]
    text = text.rstrip() + chr(10) + chr(10).join(extra) + chr(10)
    env.sched.write_text(text, encoding="utf-8")
    code, out = plan_api.get("/api/v2/plan/brief", {"day": "2026-11-16"})
    assert code == 200 and out["counts"]["skipped"] >= 3
    assert good["id"] in {e["id"] for e in out["do_not_miss"] + out["next"]}
    assert post(api, "deadline", {"deadline": dl_req(title="Another")})[0] == 200          # _ids copes with a list id
    assert post(api, "deadline/done", {"id": good["id"]})[0] == 200
    for route, extra in (("done", {}), ("move", {"to_day": "2026-12-01"}), ("delete", {})):   # on the odd series itself
        code, _ = post(api, "deadline/" + route, {"id": "dl_bad00001", "occurrence_key": "2026-11-21", **extra})
        assert code == 200
    assert plan_api.get("/api/v2/plan/brief", {"day": "1969-12-31"})[0] == 400
    assert plan_api.get("/api/v2/plan/brief", {"day": "9999-01-01"})[0] == 400
    assert plan_api.get("/api/v2/plan/brief", {"day": "9998-12-31"})[0] == 200


def test_absent_private_sources_are_omitted_silently(tmp_path, monkeypatch):
    """The public build has no inbox or finance package: no line and no act_status entry for them."""
    from plan import brief_act
    monkeypatch.setattr(brief_act, "COCKPIT", tmp_path)
    monkeypatch.setattr(brief_act, "FINANCE_DIR", tmp_path / "finance")
    lines, status = brief_act.gather(team_fn=lambda now: [])
    assert lines == [] and status == {"team": "ok"}


def test_blank_title_is_skipped_and_counted():
    ok = D(1, "2026-11-20")
    out, skipped = brief.deadline_entries([ok, D(2, "2026-11-20", title="   "), D(3, "2026-11-20", title=None),
                                           D(4, "2026-11-20", title=5)], TODAY, [])
    assert skipped == 3 and [e["id"] for e in out] == [ok["id"]]


def test_absent_finance_module_is_omitted_silently(tmp_path, monkeypatch):
    """The finance gatherer is a private module: when it cannot be imported the source is absent."""
    import sys
    from plan import brief_act
    (tmp_path / "finance").mkdir()
    monkeypatch.setattr(brief_act, "COCKPIT", tmp_path)
    monkeypatch.setattr(brief_act, "FINANCE_DIR", tmp_path / "finance")
    monkeypatch.setitem(sys.modules, "plan.brief_act_finance", None)      # import raises ImportError
    lines, status = brief_act.gather(team_fn=lambda now: [])
    assert lines == [] and "finance" not in status


def test_broken_finance_import_shows_unavailable_not_absent(tmp_path, monkeypatch):
    """Only a missing brief_act_finance module means 'absent'; any other import failure is visible."""
    import sys
    from plan import brief_act
    (tmp_path / "finance").mkdir()
    monkeypatch.setattr(brief_act, "COCKPIT", tmp_path)
    monkeypatch.setattr(brief_act, "FINANCE_DIR", tmp_path / "finance")
    monkeypatch.delitem(sys.modules, "plan.brief_act_finance", raising=False)
    def boom(_name):
        raise ModuleNotFoundError("No module named 'privmod_x'", name="privmod_x")
    monkeypatch.setattr("importlib.import_module", boom)
    lines, status = brief_act.gather(team_fn=lambda now: [])
    assert status.get("finance") == "unavailable"
    assert [l["text"] for l in lines if l["source"] == "finance"] == ["Finance unavailable"]
