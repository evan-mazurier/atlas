"""Automatic Plan link at task open (contracts 1.2). The hook runs as a subprocess; the Plan is a tiny local server."""
import datetime as dt
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from test_atlas_hook import env  # noqa: F401  (fixture)


class PlanServer:
    def __init__(self):
        self.events = []
        self.hits = 0
        self.lock_free = []
        self.ledger = None
        self.raw = None
        self.delay = 0
        self.redirect_to = None
        self.redirect_code = 302
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                outer.lock_free.append(outer.probe_lock())
                if outer.redirect_to:
                    self.send_response(outer.redirect_code)
                    self.send_header("Location", outer.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if outer.delay:
                    time.sleep(outer.delay)
                body = outer.raw if outer.raw is not None else json.dumps({"ok": True, "events": outer.events}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.srv.server_port
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def probe_lock(self):
        """True when the ledger's append lock is NOT held by anyone, i.e. the GET is outside the Txn."""
        import msvcrt
        p = os.path.join(str(self.ledger), ".lock")
        if not os.path.exists(p):
            return True
        fh = open(p, "a+b")
        try:
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return True
        finally:
            fh.close()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def plan(env):
    s = PlanServer()
    s.ledger = env.ledger
    env.plan_env = {"ATLAS_PLAN_URL": s.url}
    yield s
    s.close()


def at(d0, d1):
    now = dt.datetime.now()
    a, b = now + dt.timedelta(minutes=d0), now + dt.timedelta(minutes=d1)
    if a.date() != now.date() or b.date() != now.date():
        pytest.skip("too close to midnight")
    return now.date().isoformat(), a.strftime("%H:%M"), b.strftime("%H:%M")


def ev(area, d0, d1, eid="evt_0a1b2c3d", **kw):
    day, a, b = at(d0, d1)
    row = {"id": eid, "occurrence": day, "day": day, "start": a, "end": b, "area": area, "recur": None}
    row.update(kw)
    return row


def declare(env, text=": team-type research job"):
    env.bash(text, extra_env=env.plan_env)
    return env.events("work_type")[-1]


def test_running_single_event_links(env, plan):
    plan.events = [ev("job", -10, 50)]
    w = declare(env)
    assert w["plan_ref"] == "evt_0a1b2c3d" and w["plan_auto"] is True


def test_series_occurrence_links_with_day(env, plan):
    e = ev("job", -10, 50, recur={"freq": "daily"})
    plan.events = [e]
    w = declare(env)
    assert w["plan_ref"] == "evt_0a1b2c3d@" + e["occurrence"] and w["plan_auto"] is True


def test_starting_in_ten_minutes_links(env, plan):
    plan.events = [ev("job", 10, 70)]
    assert declare(env)["plan_ref"] == "evt_0a1b2c3d"


def test_starting_in_twenty_minutes_does_not(env, plan):
    plan.events = [ev("job", 20, 80)]
    w = declare(env)
    assert "plan_ref" not in w and "plan_auto" not in w


def test_two_matches_no_link(env, plan):
    plan.events = [ev("job", -10, 50), ev("job", 5, 60, eid="evt_11111111")]
    assert "plan_ref" not in declare(env)


def test_other_area_no_link(env, plan):
    plan.events = [ev("atlas", -10, 50)]
    assert "plan_ref" not in declare(env)


def test_all_day_and_done_ignored(env, plan):
    plan.events = [ev("job", 0, 0, start=None, end=None, eid="evt_22222222"),
                   ev("job", -10, 50, eid="evt_33333333", done=True)]
    assert "plan_ref" not in declare(env)
    plan.events.append(ev("job", -10, 50, eid="evt_44444444"))
    assert declare(env, ": team-type writing job")["plan_ref"] == "evt_44444444"


def test_explicit_task_wins_without_plan_auto(env, plan):
    plan.events = [ev("job", -10, 50)]
    w = declare(env, ": team-type research job task:t_aaaaaaaa")
    assert w["plan_ref"] == "t_aaaaaaaa" and "plan_auto" not in w
    assert plan.hits == 0


def test_server_down_task_still_opens(env, plan):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env.plan_env = {"ATLAS_PLAN_URL": "http://127.0.0.1:%d" % port}
    w = declare(env)
    assert w["type"] == "research" and "plan_ref" not in w


def test_non_json_body_no_link(env, plan):
    plan.raw = b"<html>not json</html>"
    w = declare(env)
    assert w["type"] == "research" and "plan_ref" not in w
    assert plan.hits == 1


def test_non_dict_rows_are_skipped(env, plan):
    plan.events = [1, None, "x", ["evt_0a1b2c3d"], ev("job", -10, 50)]
    assert declare(env)["plan_ref"] == "evt_0a1b2c3d"


def test_non_string_id_ignored(env, plan):
    plan.events = [ev("job", -10, 50, eid=12345678)]
    assert "plan_ref" not in declare(env)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_is_not_followed(env, plan, code):
    other = PlanServer()
    other.ledger = env.ledger
    try:
        other.events = [ev("job", -10, 50)]
        plan.redirect_code = code
        plan.redirect_to = other.url + "/api/v2/plan"
        w = declare(env)
        assert w["type"] == "research" and "plan_ref" not in w
        assert plan.hits == 1 and other.hits == 0
    finally:
        other.close()


def test_slow_server_task_opens_without_link(env, plan):
    plan.events = [ev("job", -10, 50)]
    plan.delay = 1.5
    w = declare(env)
    assert w["type"] == "research" and "plan_ref" not in w


def test_trailing_newline_in_id_no_link(env, plan):
    plan.events = [ev("job", -10, 50, eid="evt_0a1b2c3d\n")]
    assert "plan_ref" not in declare(env)


def test_skill_path_links(env, plan):
    plan.events = [ev("job", -10, 50)]
    env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "daily-review"}, extra_env=env.plan_env)
    p = env.events("playbook")[-1]
    assert p["plan_ref"] == "evt_0a1b2c3d" and p["plan_auto"] is True and plan.lock_free == [True]


def test_no_get_from_a_helper(env, plan):
    plan.events = [ev("job", -10, 50)]
    env.run("PreToolUse", tool_name="Skill", tool_input={"skill": "daily-review"}, agent_id="A1",
            agent_type="implementer", extra_env=env.plan_env)
    env.bash(": team-type research job", agent_id="A1", agent_type="implementer", extra_env=env.plan_env)
    assert plan.hits == 0
    assert not any(e.get("plan_auto") for e in env.events())


def test_malformed_task_token_no_link_no_get(env, plan):
    plan.events = [ev("job", -10, 50)]
    w = declare(env, ": team-type research job task:nope!")
    assert "plan_ref" not in w and "plan_auto" not in w
    assert plan.hits == 0


def test_off_switch_no_request(env, plan):
    plan.events = [ev("job", -10, 50)]
    for off in ("", "off", "OFF"):
        env.plan_env = {"ATLAS_PLAN_URL": off}
        assert "plan_ref" not in declare(env)
    env.plan_env = {}                                    # the fixture config says plan_url "" = off
    assert "plan_ref" not in declare(env)
    assert plan.hits == 0


def test_config_plan_url_is_used_and_null_or_off_disables(env, plan):
    plan.events = [ev("job", -10, 50)]
    env.plan_env = {"ATLAS_PLAN_URL": None}              # variable removed from the environment, not just empty
    env.cfg["plan_url"] = plan.url
    env.set_mode("record")
    w = declare(env)
    assert w["plan_ref"] == "evt_0a1b2c3d" and w["plan_auto"] is True and plan.hits == 1
    for off, typ in ((None, "writing"), ("off", "coding")):      # a new type each time: no 60 s duplicate no-op
        env.cfg["plan_url"] = off
        env.set_mode("record")
        assert "plan_ref" not in declare(env, ": team-type %s job" % typ)
    assert plan.hits == 1


def test_non_loopback_url_no_request(env, plan):
    plan.events = [ev("job", -10, 50)]
    for url in ("http://example.com:8765", "http://192.168.1.5:8765", "ftp://127.0.0.1:1"):
        env.plan_env = {"ATLAS_PLAN_URL": url}
        assert "plan_ref" not in declare(env)
    assert plan.hits == 0


def test_default_fixture_makes_no_request(env, plan):
    plan.events = [ev("job", -10, 50)]
    env.bash(": team-type research job")                 # the plan server is NOT configured
    assert "plan_ref" not in env.events("work_type")[-1] and plan.hits == 0


def test_explicit_task_after_auto_is_not_a_duplicate(env, plan):
    plan.events = [ev("job", -10, 50)]
    assert declare(env)["plan_auto"] is True
    w = declare(env, ": team-type research job task:t_aaaaaaaa")
    assert w["plan_ref"] == "t_aaaaaaaa" and "plan_auto" not in w
    assert len(env.events("work_type")) == 2


def test_implicit_undeclared_never_linked(env, plan):
    plan.events = [ev("job", -10, 50)]
    env.run("PostToolUse", tool_name="Edit", tool_input={"file_path": str(env.work / "a.py")},
            extra_env=env.plan_env)
    w = env.events("work_type")[-1]
    assert w["type"] == "undeclared" and "plan_ref" not in w and "plan_auto" not in w
    assert plan.hits == 0


def test_playbook_links(env, plan):
    plan.events = [ev("job", -10, 50)]
    env.run("UserPromptExpansion", command_name="daily-review", extra_env=env.plan_env)
    p = env.events("playbook")[-1]
    assert p["area"] == "job" and p["plan_ref"] == "evt_0a1b2c3d" and p["plan_auto"] is True
    assert plan.lock_free == [True]


def test_get_happens_outside_the_ledger_lock(env, plan):
    plan.events = [ev("job", -10, 50)]
    declare(env)
    assert plan.hits == 1 and plan.lock_free == [True]
