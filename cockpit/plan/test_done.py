"""The manual 'done' mark on events (single: done, series: done_on)."""
from plan.test_api import create_event, make_series, stored


def _get(api, a, b):
    code, g = api.get(**{"from": a, "to": b})
    assert code == 200
    return g["events"]


def test_single_mark_unmark_and_expansion(api, env):
    a, v = create_event(api, title="Solo", day="2027-05-03")
    code, out = api.post("event/done", {"base": v, "id": a["id"], "done": True})
    assert code == 200 and out["changed"]["done"] is True
    assert [e for e in stored(env, "events") if e["id"] == a["id"]][0]["done"] is True
    row = [e for e in _get(api, "2027-05-03", "2027-05-03") if e["id"] == a["id"]][0]
    assert row["done"] is True
    # idempotent
    code, out = api.post("event/done", {"base": api.version(), "id": a["id"], "done": True})
    assert code == 200 and out["changed"]["done"] is True
    code, out = api.post("event/done", {"base": api.version(), "id": a["id"], "done": False})
    assert code == 200 and "done" not in out["changed"]
    row = [e for e in _get(api, "2027-05-03", "2027-05-03") if e["id"] == a["id"]][0]
    assert "done" not in row


def test_series_occurrence_mark_unmark(api, env):
    s = make_series(api)
    for occ in ("2027-04-08", "2027-04-06", "2027-04-06"):
        code, out = api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": occ, "done": True})
        assert code == 200
    assert out["changed"]["done_on"] == ["2027-04-06", "2027-04-08"] and "done" not in out["changed"]
    rows = {e["occurrence"]: e for e in _get(api, "2027-04-05", "2027-04-09") if e["id"] == s["id"]}
    assert {k for k, e in rows.items() if e.get("done")} == {"2027-04-06", "2027-04-08"}
    code, out = api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-06", "done": False})
    assert out["changed"]["done_on"] == ["2027-04-08"]
    code, out = api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-08", "done": False})
    assert code == 200 and "done_on" not in out["changed"]
    assert "done_on" not in [e for e in stored(env, "events") if e["id"] == s["id"]][0]


def test_errors(api):
    s = make_series(api)
    a, v = create_event(api, title="Solo2", day="2027-05-04")
    assert api.post("event/done", {"base": api.version(), "id": s["id"], "done": True})[0] == 400
    assert api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": "2027-06-01", "done": True})[0] == 400
    api.post("event/skip", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07"})
    assert api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07", "done": True})[0] == 400
    assert api.post("event/done", {"base": api.version(), "id": "evt_zzzz", "done": True})[0] == 404
    assert api.post("event/done", {"base": api.version(), "id": a["id"]})[0] == 400


def test_stale_base_is_409(api):
    a, v = create_event(api, title="Stale", day="2027-05-05")
    stale = api.version()
    assert api.post("event/done", {"base": stale, "id": a["id"], "done": True})[0] == 200
    code, out = api.post("event/done", {"base": stale, "id": a["id"], "done": False})
    assert code == 409


def test_upsert_keeps_done(api, env):
    a, v = create_event(api, title="Keeper", day="2027-05-06")
    api.post("event/done", {"base": api.version(), "id": a["id"], "done": True})
    code, out = api.post("event", {"base": api.version(), "event": {"id": a["id"], "title": "Renamed"}})
    assert code == 200 and out["changed"]["done"] is True and out["changed"]["title"] == "Renamed"
    s = make_series(api)
    api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-06", "done": True})
    code, out = api.post("event", {"base": api.version(), "event": {"id": s["id"], "title": "Series2"}})
    assert code == 200 and out["changed"]["done_on"] == ["2027-04-06"]


def test_cross_day_move_carries_done(api, env):
    s = make_series(api)
    api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07", "done": True})
    api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-08", "done": True})
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07",
                                        "day": "2027-04-10", "start": "10:00", "end": "11:00"})
    assert code == 200 and out["changed"]["done"] is True
    assert out["series"]["done_on"] == ["2027-04-08"]
    # an undone occurrence moved: the new event is not done
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-09",
                                        "day": "2027-04-11", "start": "10:00", "end": "11:00"})
    assert "done" not in out["changed"]


def test_same_day_move_keeps_done(api, env):
    s = make_series(api)
    api.post("event/done", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07", "done": True})
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-04-07",
                                        "day": "2027-04-07", "start": "18:00", "end": "19:00"})
    assert code == 200 and out["changed"]["done_on"] == ["2027-04-07"]
