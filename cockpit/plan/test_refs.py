"""plan.refs: a session's declared Plan item id resolves to a title (read-only), and flows to the Ledger
tasks payload and the Team payload."""
import sys
from pathlib import Path

from plan import refs

COCKPIT = Path(__file__).resolve().parents[1]


def _seed(env):
    env.sched.write_text("events:\n- {id: evt_0a1b2c3d, title: 'Open weekly report', day: '2026-10-12',"
                         " start: '15:45', end: '16:30', type: errand}\n", encoding="utf-8")
    env.tasks.write_text("tasks:\n- {id: t_deadbeef, area: md, title: Write outreach, state: todo, owner: me}\n"
                         "reviews: []\n", encoding="utf-8")


def test_valid_ref_format():
    for ok in ("evt_0a1b2c3d", "evt_0a1b2c3d@2026-10-12", "t_deadbeef", "learning-app/week-view"):
        assert refs.valid_ref(ok), ok
    for bad in ("", "open the payment", "evt_xyz", "evt_0a1b2c3d@tomorrow", "t_DEADBEEF", "a/", None, "../x/y z"):
        assert not refs.valid_ref(bad), bad


def test_resolve_event_series_occurrence_task_and_plan_yaml(env):
    _seed(env)
    got = refs.resolve_many(["evt_0a1b2c3d", "evt_0a1b2c3d@2026-10-19", "t_deadbeef", "learning-app/week-view",
                             "t_00000000", "free text"])
    assert got["evt_0a1b2c3d"]["title"] == "Open weekly report"
    assert got["evt_0a1b2c3d@2026-10-19"]["title"] == "Open weekly report"
    assert got["evt_0a1b2c3d@2026-10-19"]["day"] == "2026-10-19"
    assert got["t_deadbeef"]["title"] == "Write outreach"
    assert got["learning-app/week-view"]["title"] == "Week view"
    assert set(got) == {"evt_0a1b2c3d", "evt_0a1b2c3d@2026-10-19", "t_deadbeef", "learning-app/week-view"}


def test_resolve_never_raises_on_missing_files(env):
    env.sched.unlink()
    env.tasks.write_text(":::not yaml", encoding="utf-8")
    assert refs.resolve("evt_0a1b2c3d") is None


def test_ledger_tasks_payload_carries_plan_ref_and_title(env, monkeypatch):
    _seed(env)
    sys.path.insert(0, str(COCKPIT))
    from ledger2 import api as lapi
    out = lapi.with_plan_titles([{"id": "a-1", "plan_ref": "evt_0a1b2c3d"}, {"id": "a-2", "plan_ref": "t_00000000"},
                                 {"id": "a-3", "plan_ref": None}])
    assert out[0]["plan_title"] == "Open weekly report"
    assert out[1]["plan_ref"] == "t_00000000" and out[1]["plan_title"] is None
    assert out[2]["plan_title"] is None


def test_team_payload_resolves_the_linked_title(env, monkeypatch):
    _seed(env)
    sys.path.insert(0, str(COCKPIT))
    import shell_api
    snap = {"ok": True, "sessions": [{"sid": "s1", "type": "Coding", "project": "x", "status": "working",
                                      "plan_ref": "evt_0a1b2c3d"},
                                     {"sid": "s2", "type": "Coding", "project": "x", "status": "idle"}]}
    monkeypatch.setattr(shell_api, "_snapshot", lambda: snap)
    code, out = shell_api.team_payload()
    assert code == 200
    assert out["sessions"][0]["plan_title"] == "Open weekly report"
    assert out["sessions"][1]["plan_title"] is None


def test_resolve_includes_done_project_plan_tasks(env):
    assert refs.resolve("learning-app/old")["title"] == "Old thing"
