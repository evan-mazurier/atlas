"""Parallel planning (contracts.md 5.3): event attention, the overlap guard, MAX_PARALLEL.
Temp copies only (conftest). Areas come from the test-owned fixture: md shared, pers full, train full + fixed."""
import re

from plan.test_api import stored
from plan.test_rules import add_task, post_ev

D2 = "2027-06-07"


def sh(api, title, start, end, **kw):
    kw.setdefault("area", "md")                       # md is a shared area
    return post_ev(api, title=title, day=D2, start=start, end=end, **kw)


def events_of(api, day=D2):
    return {e["title"]: e for e in api.get(**{"from": day, "to": day})[1]["events"]}


def test_two_and_three_shared_overlap_fourth_refused_with_the_stretch(api):
    for t in "ABC":
        assert sh(api, t, "09:00", "11:00")[0] == 200
    code, out = sh(api, "D", "10:00", "10:30")
    assert code == 400 and out["error"] == "can't save 'D': 3 sessions already run in parallel 09:00-11:00", out
    assert sh(api, "D", "11:00", "12:00")[0] == 200            # touching edges are fine


def test_staggered_shared_stays_within_three_at_once(api):
    assert sh(api, "A", "09:00", "12:00")[0] == 200
    assert sh(api, "B", "09:00", "10:00")[0] == 200
    assert sh(api, "C", "10:00", "11:00")[0] == 200
    assert sh(api, "D", "10:00", "11:00")[0] == 200
    code, out = sh(api, "E", "10:30", "11:00")
    assert code == 400 and out["error"].endswith("3 sessions already run in parallel 10:00-11:00"), out


def test_full_and_shared_refuse_each_other_both_ways(api):
    assert sh(api, "Shared", "09:00", "11:00")[0] == 200
    code, out = sh(api, "Solo", "10:00", "12:00", area="pers")
    assert code == 400 and out["error"] == "can't save 'Solo': overlaps 'Shared' (09:00-11:00); 'Solo' needs full attention", out
    assert sh(api, "Solo", "11:00", "12:00", area="pers")[0] == 200
    code, out = sh(api, "Late", "11:30", "12:30")
    assert code == 400 and out["error"] == "can't save 'Late': overlaps 'Solo' (11:00-12:00), which needs full attention", out


def test_training_is_fixed_to_full(api):
    code, out = sh(api, "Lab session", "09:00", "10:00", area="train", attention="shared")
    assert code == 400 and out["error"] == "can't save 'Lab session': Learning is always full", out
    assert sh(api, "Lab session", "09:00", "10:00", area="train", attention="full")[0] == 200
    assert sh(api, "Study", "09:30", "10:30")[0] == 400         # keyword-matched Learning, no explicit area
    ev = events_of(api)["Lab session"]
    assert (ev["attention"], ev["attention_source"]) == ("full", "fixed")


def test_invalid_attention_is_400(api):
    code, out = sh(api, "X", "09:00", "10:00", attention="both")
    assert code == 400 and "attention" in out["error"]


def test_event_attention_overrides_the_area_and_a_no_area_event_is_full(api, env):
    assert sh(api, "Solo md", "09:00", "10:00", attention="full")[0] == 200
    code, out = sh(api, "Other", "09:30", "10:30")
    assert code == 400 and "which needs full attention" in out["error"]
    assert post_ev(api, title="Noarea", day=D2, start="13:00", end="14:00")[0] == 200
    code, out = sh(api, "Other", "13:30", "14:30")
    assert code == 400 and "overlaps 'Noarea'" in out["error"]
    evs = events_of(api)
    assert (evs["Solo md"]["attention"], evs["Solo md"]["attention_source"]) == ("full", "event")
    assert (evs["Noarea"]["attention"], evs["Noarea"]["attention_source"]) == ("full", "default")
    assert [e for e in stored(env, "events") if e["title"] == "Solo md"][0]["attention"] == "full"
    assert "attention" not in [e for e in stored(env, "events") if e["title"] == "Noarea"][0]   # only when set explicitly


def test_get_shows_area_attention_and_source(api):
    assert sh(api, "Shared one", "09:00", "10:00")[0] == 200
    got = api.get(**{"from": D2, "to": D2})[1]
    ev = [e for e in got["events"] if e["title"] == "Shared one"][0]
    assert (ev["attention"], ev["attention_source"]) == ("shared", "area")
    areas = {a["id"]: a for a in got["areas"]}
    assert (areas["md"]["attention"], areas["md"]["attention_fixed"], areas["md"]["short"]) == ("shared", False, "PROD")
    assert (areas["train"]["attention"], areas["train"]["attention_fixed"]) == ("full", True)


def test_busy_false_is_ignored_by_the_attention_guard(api):
    assert sh(api, "Solo", "09:00", "10:00", area="pers")[0] == 200
    assert sh(api, "Note", "09:15", "09:45", area="pers", busy=False)[0] == 200
    assert sh(api, "Solo2", "10:00", "11:00", area="pers")[0] == 200
    assert sh(api, "Solo3", "09:15", "09:45", area="pers")[0] == 400


def test_recurring_occurrence_counts_and_a_skip_frees_it(api):
    r = sh(api, "Daily", "09:00", "10:00", area="pers", recur={"freq": "daily", "interval": 1})[1]["changed"]
    code, out = sh(api, "Clash", "09:30", "10:30")
    assert code == 400 and "overlaps 'Daily'" in out["error"]
    code, out = post_ev(api, title="Clash2", day="2027-06-08", start="09:30", end="10:30", area="md")
    assert code == 400
    assert api.post("event/skip", {"base": api.version(), "id": r["id"], "occurrence": D2})[0] == 200
    assert sh(api, "Clash", "09:30", "10:30")[0] == 200


def test_moves_and_retimes_are_guarded(api):
    assert sh(api, "Solo", "09:00", "10:00", area="pers")[0] == 200
    b = sh(api, "B", "11:00", "12:00")[1]["changed"]
    code, out = api.post("event/move", {"base": api.version(), "id": b["id"], "day": D2, "start": "09:30", "end": "10:30"})
    assert code == 400 and out["error"].startswith("can't move 'B': overlaps 'Solo'")
    r = post_ev(api, title="Rec", day="2027-06-14", start="15:00", end="16:00", area="md",
                recur={"freq": "weekly", "interval": 1})[1]["changed"]

    assert post_ev(api, title="Solo14", day="2027-06-21", start="11:00", end="12:00", area="pers")[0] == 200
    # retiming one occurrence onto the full event is refused, a free slot works
    code, out = api.post("event/move", {"base": api.version(), "id": r["id"], "occurrence": "2027-06-21",
                                        "day": "2027-06-21", "start": "11:30", "end": "12:30"})
    assert code == 400 and "overlaps 'Solo14'" in out["error"] and "on 2027-06-21" in out["error"]
    code, out = api.post("event/move", {"base": api.version(), "id": r["id"], "occurrence": "2027-06-21",
                                        "day": "2027-06-21", "start": "13:00", "end": "14:00"})
    assert code == 200, out
    # a cross-day occurrence move is guarded too
    code, out = api.post("event/move", {"base": api.version(), "id": r["id"], "occurrence": "2027-06-28",
                                        "day": "2027-06-21", "start": "11:30", "end": "12:30"})
    assert code == 400 and "overlaps 'Solo14'" in out["error"]


def test_notes_only_edit_on_legacy_overlap_is_not_refused(api, env):
    post_ev(api, title="A", day=D2, start="09:00", end="10:00", area="pers")
    b = sh(api, "B", "11:00", "12:00", area="pers")[1]["changed"]
    text = env.sched.read_text(encoding="utf-8")
    patched = re.sub(r"(id: %s[\s\S]*?start: )['\"]?11:00['\"]?" % b["id"], r"\g<1>'09:30'", text, count=1)
    assert patched != text
    env.sched.write_text(patched, encoding="utf-8")
    code, out = api.post("event", {"base": api.version(), "event": {"id": b["id"], "notes": "kept"}})
    assert code == 200, out
    code, out = api.post("event", {"base": api.version(), "event": {"id": b["id"], "attention": "shared"}})
    assert code == 400                                           # changing a guarded field is re-checked (still overlaps)


def test_task_place_me_uses_the_guard(api):
    assert sh(api, "Solo", "10:00", "12:00", area="pers")[0] == 200
    t = add_task(api)
    code, out = api.post("task/place", {"base": api.version(), "id": t["id"], "target": "me", "day": D2,
                                        "start": "10:30", "end": "11:30"})
    assert code == 400 and "which needs full attention" in out["error"]
    ids = [add_task(api, title=f"S{i}")["id"] for i in range(4)]
    for tid in ids[:3]:
        assert api.post("task/place", {"base": api.version(), "id": tid, "target": "me", "day": "2027-06-09",
                                       "start": "10:00", "end": "11:00"})[0] == 200
    code, out = api.post("task/place", {"base": api.version(), "id": ids[3], "target": "me", "day": "2027-06-09",
                                        "start": "10:30", "end": "11:30"})
    assert code == 400 and "3 sessions already run in parallel 10:00-11:00" in out["error"]
