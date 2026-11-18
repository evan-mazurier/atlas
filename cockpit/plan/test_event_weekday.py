"""Events with the monthly weekday rule (last Saturday, nth weekday): shared with deadlines in schedmath."""
import datetime as dt

from plan.test_api import create_event, ev_req, stored

LAST_SAT = {"freq": "monthly", "weekday": "sat", "week": "last"}
EXPECTED = ["2026-11-28", "2026-12-26", "2027-01-30", "2027-02-27", "2027-03-27"]


def _series(api, **kw):
    kw.setdefault("recur", LAST_SAT)
    kw.setdefault("area", "pers")
    return create_event(api, title="Last Saturday", day="2026-11-28", start="10:00", end="11:00", **kw)[0]


def _occ(api, eid, a="2026-11-01", b="2027-03-31"):
    code, g = api.get(**{"from": a, "to": b})
    assert code == 200
    return sorted(e["occurrence"] for e in g["events"] if e["id"] == eid)


def test_last_saturday_expands(api, env):
    s = _series(api)
    assert s["recur"]["weekday"] == "sat" and s["recur"]["week"] == "last"
    assert _occ(api, s["id"]) == EXPECTED


def test_nth_weekday_interval_until_count(api, env):
    s = _series(api, recur={"freq": "monthly", "weekday": "sat", "week": "last", "interval": 2, "until": "2027-02-28"})
    assert _occ(api, s["id"]) == ["2026-11-28", "2027-01-30"]
    s2 = create_event(api, title="First Monday", day="2026-11-02", recur={"freq": "monthly", "weekday": "mon", "week": 1, "count": 3},
                      start="12:00", end="13:00", area="pers")[0]
    assert _occ(api, s2["id"]) == ["2026-11-02", "2026-12-07", "2027-01-04"]


def test_collision_guard_sees_occurrences(api, env):
    _series(api)
    for day in ("2026-12-26", "2027-02-27"):
        code, out = api.post("event", {"base": api.version(), "event": ev_req(title="Clash", day=day, start="10:30", end="11:30", area="pers")})
        assert code == 400 and "overlaps 'Last Saturday'" in out["error"], (day, out)
    code, out = api.post("event", {"base": api.version(), "event": ev_req(title="Free", day="2026-12-19", start="10:30", end="11:30", area="pers")})
    assert code == 200, out


def test_skip_and_override(api, env):
    s = _series(api)
    assert api.post("event/skip", {"base": api.version(), "id": s["id"], "occurrence": "2026-12-26"})[0] == 200
    assert _occ(api, s["id"]) == [d for d in EXPECTED if d != "2026-12-26"]
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-01-30",
                                        "day": "2027-01-30", "start": "14:00", "end": "15:00"})
    assert code == 200
    code, g = api.get(**{"from": "2027-01-30", "to": "2027-01-30"})
    row = [e for e in g["events"] if e["id"] == s["id"]][0]
    assert row["start"] == "14:00"
    # a skipped occurrence frees its slot
    code, out = api.post("event", {"base": api.version(), "event": ev_req(title="Now free", day="2026-12-26", start="10:00", end="11:00", area="pers")})
    assert code == 200, out
    ev = [e for e in stored(env, "events") if e["id"] == s["id"]][0]
    assert ev["recur"]["weekday"] == "sat"


def test_write_400s(api, env):
    bad = [
        ("2026-11-21", LAST_SAT),                                       # not the last Saturday
        ("2026-11-28", {**LAST_SAT, "week": 2}),
        ("2026-11-28", {**LAST_SAT, "week": 5}),
        ("2026-11-28", {**LAST_SAT, "week": True}),
        ("2026-11-28", {**LAST_SAT, "weekday": "xyz"}),
        ("2026-11-28", {"freq": "weekly", "weekday": "sat", "week": "last"}),
        ("2026-11-28", {"freq": "monthly", "week": "last"}),            # week without weekday
        ("2026-11-28", {**LAST_SAT, "week": None}),
    ]
    for day, rec in bad:
        code, out = api.post("event", {"base": api.version(), "event": ev_req(title="X", day=day, area="pers", recur=rec)})
        assert code == 400, (day, rec, out)
    s = _series(api)
    code, out = api.post("event", {"base": api.version(), "event": {"id": s["id"], "day": "2026-11-27"}})
    assert code == 400


def test_edit_keeps_weekday_rule(api, env):
    s = _series(api)
    code, out = api.post("event", {"base": api.version(), "event": {"id": s["id"], "title": "Renamed"}})
    assert code == 200
    ev = [e for e in stored(env, "events") if e["id"] == s["id"]][0]
    assert ev["recur"]["weekday"] == "sat" and ev["recur"]["week"] == "last" and ev["title"] == "Renamed"


def test_whole_series_move_checks_the_rule(api, env):
    s = _series(api)
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "day": "2026-11-20"})
    assert code == 400 and "is not the last sat of its month" in out["error"] and out["error"].startswith("can't move")
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "day": "2026-12-26"})
    assert code == 200, out
    # an occurrence move is unaffected by the series rule
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "occurrence": "2027-01-30",
                                        "day": "2027-01-30", "start": "14:00", "end": "15:00"})
    assert code == 200, out


def test_count_must_be_an_integer(api, env):
    for c in ("3", 1.5, True, 0):
        code, out = api.post("event", {"base": api.version(), "event": ev_req(
            title="X", day="2026-11-28", area="pers", recur={**LAST_SAT, "count": c})})
        assert code == 400, (c, out)
    s = _series(api, recur={**LAST_SAT, "count": 2})
    assert _occ(api, s["id"]) == ["2026-11-28", "2026-12-26"]


def test_schedmath_coerces_a_string_count():
    from schedmath import _occurs_on
    d0 = dt.date(2026, 11, 28)
    days = [d0 + dt.timedelta(days=i) for i in range(0, 130)]
    got = [d for d in days if _occurs_on(d0, {**LAST_SAT, "count": "3"}, d)]
    assert len(got) == 3


def test_hand_edited_weekday_rule_without_week_is_400_not_500(api, env):
    import yaml
    s = _series(api)
    d = yaml.safe_load(env.sched.read_text(encoding="utf-8"))
    for e in d["events"]:
        if e["id"] == s["id"]:
            e["recur"].pop("week")
    env.sched.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    code, out = api.post("event/move", {"base": api.version(), "id": s["id"], "day": "2026-12-26"})
    assert code == 400 and "can't move" in out["error"]
