"""Tests for tools/daily_check.py on a fixture ledger. Temp dirs only, no live data, no network."""
import datetime as dt
import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_check as dc  # noqa: E402
from ledger2 import query, stats, store  # noqa: E402

DAY = dt.date(2026, 10, 7)
BASE = "2026-10-07T09:00:00+02:00"
NOW_PAST = dt.datetime.fromisoformat("2026-10-09T10:00:00+02:00")
NOW_EVE = dt.datetime.fromisoformat("2026-10-07T22:00:00+02:00")
NOW_MORNING = dt.datetime.fromisoformat("2026-10-07T12:00:00+02:00")


def sid(n):
    return f"{n:08d}-0000-4000-8000-000000000001"


def tid(n, k=1):
    return f"{n:08d}-{k}"


def T(minutes, base=BASE):
    return (dt.datetime.fromisoformat(base) + dt.timedelta(minutes=minutes)).isoformat(timespec="seconds")


def E(ev, m, s, **kw):
    return dict({"v": 2, "t": T(m), "ev": ev, "sid": sid(s), "cwd": "D:\\AI\\Atlas", "mode": "record"}, **kw)


def session(n, start, typ="coding", area="atlas"):
    return [E("session_start", start, n, source="startup"), E("work_type", start, n, type=typ, area=area, task=tid(n))]


def cost(m, n, w):
    return E("cost", m, n, scope="main", model="claude-sonnet-5-5",
             **{"in": 1, "out": 1, "cache_read": 0, "cache_write": 0, "weighted": w})


def fixture_events():
    ev = []
    # 8: a task of another day, must not show on the 7th (first: day files chain in date order)
    other = "2026-10-05T09:00:00+02:00"
    ev += [dict(session(8, 0)[0], t=T(0, other)), dict(session(8, 0)[1], t=T(0, other)),
           dict(E("task_done", 1, 8, task=tid(8)), t=T(1, other))]
    # 1: READY and closed, 60 min, area atlas, cost 514000
    ev += session(1, 0) + [E("helper_start", 1, 1, helper="implementer", label="Build the thing", tuid="u", bg=False, version=0),
                          E("edit", 5, 1, path="x.py", tool="Edit", version=1, task=tid(1)),
                          E("verdict", 20, 1, agent_id="r1", helper="reviewer", verdict="READY", version=1, findings=0, task=tid(1)),
                          cost(21, 1, 514000), E("task_done", 60, 1, task=tid(1)), E("session_end", 61, 1, reason="other")]
    # 2: closed, edited, never checked -> unverified, job area
    ev += session(2, 70, area="job") + [E("edit", 71, 2, path="y.py", tool="Edit", version=1, task=tid(2)), cost(72, 2, 1500),
                                       E("task_done", 80, 2, task=tid(2)), E("session_end", 81, 2, reason="other")]
    # 3: three send-backs on the day, still open
    ev += session(3, 90) + [E("edit", 91, 3, path="z.py", tool="Edit", version=1, task=tid(3))]
    for i in range(3):
        ev.append(E("verdict", 92 + i, 3, agent_id=f"n{i}", helper="reviewer", verdict="NEEDS_CHANGES",
                    version=1 + i, findings=1, task=tid(3)))
    # 4: open, unchecked
    ev += session(4, 100) + [E("edit", 101, 4, path="w.py", tool="Edit", version=1, task=tid(4))]
    # 5: enforced rework refusal; also publish, config, done_lock, quick_question, a notice and a would-have
    ev += session(5, 110) + [E("edit", 111, 5, path="v.py", tool="Edit", version=1, task=tid(5)),
                            E("refusal", 112, 5, guard="rework", would=False, task=tid(5), detail="over budget"),
                            E("refusal", 113, 5, guard="publish", would=False, task=tid(5), detail="push"),
                            E("refusal", 113, 5, guard="config", would=False, task=tid(5), detail="claude-md"),
                            E("refusal", 114, 5, guard="done_lock", would=False, task=tid(5), detail="unchecked"),
                            E("refusal", 114, 5, guard="quick_question", would=False, task=tid(5), detail="q"),
                            E("refusal", 115, 5, guard="rework", would=False, task=tid(5), detail="carry-over notice"),
                            E("refusal", 115, 5, guard="rework", would=True, task=tid(5), detail="x"),
                            E("task_done", 116, 5, task=tid(5))]
    # 6: escalated
    ev += session(6, 120) + [
                            E("verdict", 122, 6, agent_id="e", helper="reviewer", verdict="ESCALATE", version=1, findings=0, task=tid(6)),
                            E("task_done", 123, 6, task=tid(6))]
    # 7: READY, left open
    ev += session(7, 130) + [E("edit", 131, 7, path="t.py", tool="Edit", version=1, task=tid(7)),
                            E("verdict", 132, 7, agent_id="r7", helper="reviewer", verdict="READY", version=1, findings=0, task=tid(7))]
    return ev


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(d))
    monkeypatch.setenv("ATLAS_ANCHOR_DISABLE", "1")
    monkeypatch.setenv("ATLAS_DAILY_SERVER_URL", "")
    stats.clear_cache()
    stats._integrity_cache.clear()
    query.invalidate()
    store.append_events(fixture_events(), base=d)
    return d


def rows_by_id(r):
    return {x["id"]: x for x in r["rows"]}


def test_row_selection_and_order(ledger):
    r = dc.compute(DAY, NOW_PAST)
    assert [x["id"] for x in r["rows"]] == [tid(n) for n in range(1, 8)]   # sorted by start; the 5 Oct task is out
    assert [x["id"] for x in dc.compute(dt.date(2026, 10, 5), NOW_PAST)["rows"]] == [tid(8)]
    assert dc.compute(dt.date(2026, 10, 6), NOW_PAST)["rows"] == []


def test_stale_open_task_not_on_later_days(ledger):
    # task 4 is open (no end); it must not show on a day with no event of its session
    assert tid(4) not in [x["id"] for x in dc.compute(dt.date(2026, 10, 8), NOW_PAST)["rows"]]


def test_each_result_value(ledger):
    rb = rows_by_id(dc.compute(DAY, NOW_PAST))
    assert rb[tid(1)]["result"] == "READY, closed"
    assert rb[tid(2)]["result"] == "closed, unverified"
    assert rb[tid(3)]["result"] == "NEEDS CHANGES"
    assert rb[tid(4)]["result"] == "open, unchecked"
    assert rb[tid(5)]["result"] == "refused (rework)"
    assert rb[tid(6)]["result"] == "ESCALATED"
    assert rb[tid(7)]["result"] == "READY, open"


def test_result_priority_unit():
    base = {"status": "done", "final_verdict": "READY", "refusals": [{"guard": "publish", "would": False}]}
    assert dc.result_of(base) == "READY, closed"                       # a refusal does not hide a READY
    assert dc.result_of(dict(base, final_verdict=None)) == "refused (publish)"
    assert dc.result_of(dict(base, final_verdict=None, unverified_flag=True,
                             refusals=[{"guard": "rework", "would": False, "notice": True}])) == "closed, unverified"
    assert dc.result_of({"status": "open", "final_verdict": None,
                         "refusals": [{"guard": "rework", "would": True}]}) == "open, unchecked"


def test_label_area_type_and_formatting(ledger):
    rb = rows_by_id(dc.compute(DAY, NOW_PAST))
    r1 = rb[tid(1)]
    assert r1["task"] == "Build the thing" and r1["area"] == "Ops" and r1["type"] == "coding"
    assert r1["active"] == "1.0 h" and r1["cost_fmt"] == "514k" and r1["cost"] == 514000
    assert rb[tid(2)]["area"] == "Research"
    assert dc.fmt_active(35 * 60) == "35 min" and dc.fmt_active(4320) == "1.2 h" and dc.fmt_active(0) == "0 min"
    assert dc.fmt_cost(999) == "999" and dc.fmt_cost(1_234_000) == "1.2M" and dc.fmt_cost(514_000) == "514k"
    assert len(dc.clip("x" * 80)) == 50 and dc.clip("x" * 80).endswith("...")


def test_totals(ledger):
    t = dc.compute(DAY, NOW_PAST)["totals"]
    assert t["tasks"] == 7 and t["cost"] == 515500 and t["cost_fmt"] == "516k"
    assert t["active_by_area"][0]["area"] == "Ops"
    assert {a["area"] for a in t["active_by_area"]} == {"Ops", "Research"}
    assert t["most_expensive"] == {"task": "Build the thing", "cost": 514000, "cost_fmt": "514k"}
    md = dc.render(dc.compute(DAY, NOW_PAST))
    assert "Totals: 7 task(s); active Ops" in md and "most expensive: Build the thing (514k)" in md


def test_empty_day_totals(ledger):
    r = dc.compute(dt.date(2026, 10, 6), NOW_PAST)
    assert r["totals"]["most_expensive"] is None
    assert "No task worked." in dc.render(r) and "Totals: 0 task(s); active none; cost 0" in dc.render(r)


def H(r):
    return {x["check"]: x for x in r["health"]}


def test_health_statuses_on_a_busy_day(ledger):
    h = H(dc.compute(DAY, NOW_PAST))
    assert h["Hash chain"]["status"] == "green"
    assert h["Bypass attempts"]["status"] == "green"
    assert h["Refusals: rework"]["status"] == "amber" and h["Refusals: rework"]["fact"] == "1 on the day"   # notice, would-have excluded
    assert h["Refusals: done_lock"]["status"] == "amber"
    flow = h["Refusals: normal flow"]
    assert flow["status"] == "green"
    assert "publish 1" in flow["fact"] and "config 1" in flow["fact"] and "quick_question 1" in flow["fact"]
    assert "would-have (record mode) 1" in flow["fact"]
    assert h["Send-backs"]["status"] == "amber" and "(3)" in h["Send-backs"]["fact"]
    assert h["Closed without a passing review"]["status"] == "amber"
    assert h["Open tasks"]["status"] == "amber" and "at the end of the day" in h["Open tasks"]["fact"]
    assert h["Hook errors"]["status"] == "green"
    assert h["Anchor"] == {"check": "Anchor", "status": "green", "fact": "not set up"}
    assert "Cockpit server" not in h


def test_ordering_red_amber_green(ledger):
    store.append_events([E("bypass_attempt", 140, 3, tool="Bash", matched="x", denied=True)], base=ledger)
    r = dc.compute(DAY, NOW_PAST)
    order = [x["status"] for x in r["health"]]
    assert order == sorted(order, key=dc.ORDER.get) and order[0] == "red" and order[-1] == "green"
    assert r["health"][0]["check"] == "Bypass attempts" and not r["all_green"]
    md = dc.render(r)
    assert md.index("- red:") < md.index("- amber:") < md.index("- green:")


def test_hash_chain_red_when_tampered(ledger):
    f = store.day_file("2026-10-07", ledger)
    f.write_bytes(f.read_bytes().replace(b'"x.py"', b'"q.py"', 1))
    stats._integrity_cache.clear()
    h = H(dc.compute(DAY, NOW_PAST))
    assert h["Hash chain"]["status"] == "red" and "not acknowledged" in h["Hash chain"]["fact"]


def test_hook_errors(ledger):
    (ledger / "hook-errors.log").write_text(
        "2026-10-07T10:00:00+02:00 KeyError atlas_hook.py:12\n2026-10-07T11:00:00+02:00 OSError atlas_hook.py:30\n"
        "2026-10-06T11:00:00+02:00 OSError atlas_hook.py:30\ngarbage\n", encoding="utf-8")
    assert dc.check_hook_errors(DAY) == {"check": "Hook errors", "status": "amber", "fact": "2 on the day"}
    assert dc.check_hook_errors(dt.date(2026, 10, 9))["status"] == "green"


def test_open_tasks_today_uses_now(ledger):
    h = H(dc.compute(DAY, NOW_EVE))
    assert "still open now" in h["Open tasks"]["fact"] and h["Open tasks"]["status"] == "amber"
    tasks = [t for t in query.all_tasks() if t["id"] in (tid(1), tid(2))]       # both ended on the day
    assert dc.check_open(tasks, DAY, False)["status"] == "green"


def test_unverified_and_sendbacks_green():
    assert dc.check_unverified([], DAY)["status"] == "green"
    assert dc.check_send_backs([], DAY)["status"] == "green"


ANCHOR_ON = {"configured": True, "expected": True, "missing": False, "last_anchored_day": "2026-10-07"}


@pytest.mark.parametrize("anchor,now,status,word", [
    ({"configured": False, "expected": False}, NOW_PAST, "green", "not set up"),
    (ANCHOR_ON, NOW_PAST, "green", "ran"),
    (dict(ANCHOR_ON, last_anchored_day="2026-10-05"), NOW_PAST, "amber", "did not run"),
    (dict(ANCHOR_ON, last_anchored_day="2026-10-05"), NOW_EVE, "amber", "did not run"),            # after 21:00 on the day
    (dict(ANCHOR_ON, last_anchored_day="2026-10-05"), NOW_MORNING, "green", "not due yet"),
    (dict(ANCHOR_ON, last_anchored_day=None), NOW_PAST, "amber", "never"),
    (dict(ANCHOR_ON, missing=True), NOW_PAST, "amber", "missing"),
])
def test_anchor(anchor, now, status, word):
    r = dc.check_anchor(anchor, DAY, now)
    assert r["status"] == status and word in r["fact"]


class _Stub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def test_server_check(monkeypatch):
    monkeypatch.setenv("ATLAS_DAILY_SERVER_URL", "")
    assert dc.check_server(DAY, NOW_EVE) is None                                  # skipped by an empty value
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port
    try:
        monkeypatch.setenv("ATLAS_DAILY_SERVER_URL", f"http://127.0.0.1:{port}/x")
        assert dc.check_server(DAY, NOW_EVE)["status"] == "green"
        assert dc.check_server(dt.date(2026, 10, 6), NOW_EVE) is None             # not today: skipped
    finally:
        srv.shutdown()
        srv.server_close()
    assert dc.check_server(DAY, NOW_EVE) == {"check": "Cockpit server", "status": "red", "fact": "down (no answer within 1 s)"}


def test_all_green(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(d))
    monkeypatch.setenv("ATLAS_ANCHOR_DISABLE", "1")
    monkeypatch.setenv("ATLAS_DAILY_SERVER_URL", "")
    stats.clear_cache()
    stats._integrity_cache.clear()
    query.invalidate()
    ev = session(1, 0) + [E("edit", 5, 1, path="x.py", tool="Edit", version=1, task=tid(1)),
                          E("verdict", 20, 1, agent_id="r", helper="reviewer", verdict="READY", version=1, findings=0, task=tid(1)),
                          E("refusal", 21, 1, guard="publish", would=False, task=tid(1), detail="push"),
                          E("task_done", 30, 1, task=tid(1)), E("session_end", 31, 1, reason="other")]
    store.append_events(ev, base=d)
    r = dc.compute(DAY, NOW_PAST)
    assert r["all_green"] and all(x["status"] == "green" for x in r["health"])
    assert "All green." in dc.render(r) and "- green" not in dc.render(r)


def test_json_and_cli(ledger, capsys):
    assert dc.main(["--day", "2026-10-07", "--json"]) == 0
    o = json.loads(capsys.readouterr().out)
    assert o["day"] == "2026-10-07" and len(o["rows"]) == 7 and o["totals"]["tasks"] == 7
    assert {"check", "status", "fact"} <= set(o["health"][0])
    assert dc.main(["--day", "2026-10-07"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Daily check 2026-10-07") and "| Task | Area | Type | Active time | Cost | Result |" in out
    assert "\u2014" not in out
    assert dc.main(["--day", "garbage"]) == 2


def test_read_only(ledger):
    before = {p.name: p.read_bytes() for p in ledger.iterdir()}
    dc.compute(DAY, NOW_PAST)
    assert {p.name: p.read_bytes() for p in ledger.iterdir()} == before
