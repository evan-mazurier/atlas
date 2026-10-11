"""
The Claude Code team panel's state (cockpit/teamlive.py) and its endpoint.

Fixtures are the hook's real event shapes (see teamlive.py). Every test builds events with `ev()` and
calls the pure `build_state` — no clock, no transcripts, no network.

Run:  py -3.12 -m pytest cockpit/test_teamlive.py -q
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import teamlive  # noqa: E402

NOW = dt.datetime(2026, 10, 8, 12, 30, 0)
ATLAS_T = r"~\.claude\projects\D--AI-Atlas\{sid}.jsonl"


@pytest.fixture(autouse=True)
def _no_min_lifetime(monkeypatch):
    """Most fixtures are a bare prompt; the few-seconds-session rule has its own test."""
    monkeypatch.setattr(teamlive, "MIN_LIFETIME", dt.timedelta(0))


def ev(t: str, name: str, sid: str = "s1", **kw) -> dict:
    d = {"ts": f"2026-10-08T{t}", "ev": name, "sid": sid, "cwd": r"D:\AI\Atlas"}
    d.update(kw)
    d["_t"] = dt.datetime.fromisoformat(d["ts"])
    return d


def prompt(t, sid="s1", folder="D--AI-Atlas", **kw):
    return ev(t, "UserPromptSubmit", sid, transcript=rf"~\.claude\projects\{folder}\{sid}.jsonl", **kw)


def helper(t0, t1, atype, label, sid="s1", aid="a1", tuid="tu1", bg=False, post=True):
    """PreToolUse(Agent) + SubagentStart, and (t1 given) the stop events."""
    out = [ev(t0, "PreToolUse", sid, tool="Agent", agent_type=atype, label=label, bg=bg, tuid=tuid),
           ev(t0, "SubagentStart", sid, in_agent=aid, agent_id=aid, agent_type=atype)]
    if t1 and not bg:
        out += [ev(t1, "SubagentStop", sid, in_agent=aid, agent_id=aid, agent_type=atype),
                ev(t1, "PostToolUse", sid, tool="Agent", tuid=tuid, status="completed", agent_id=aid)]
    elif bg and post:
        out.append(ev(t0, "PostToolUse", sid, tool="Agent", tuid=tuid, status="async_launched", agent_id=aid))
        if t1:
            out.append(ev(t1, "SubagentStop", sid, in_agent=aid, agent_id=aid, agent_type=atype))
    return out


def build(events, exclude=(), cost=None):
    return teamlive.build_state(sorted(events, key=lambda e: e["_t"]), NOW, exclude, cost)


def lane(state, sid="s1"):
    return next(s for s in state["sessions"] if s["sid"] == sid)


def agents(s):
    return [(x["agent"], x["state"]) for x in s["steps"]]


# --- helpers working / done ---------------------------------------------------------------------------
def test_helper_working_then_done():
    evs = [prompt("12:00:00"), ev("12:00:30", "WorkType", type="coding")]
    evs += helper("12:01:00", "12:05:00", "researcher", "Look it up", aid="a1", tuid="t1")
    evs += helper("12:06:00", None, "implementer", "Build it", aid="a2", tuid="t2")
    s = lane(build(evs))
    assert s["status"] == "working"
    assert agents(s) == [("op", "done"), ("chief", "working"), ("analyst", "done"), ("builder", "working")]
    assert [x["what"] for x in s["steps"][2:]] == ["Look it up", "Build it"]
    assert (s["type"], s["tag"]) == ("Coding", "just ask · type: coding")


def test_background_helper_completes_only_through_subagent_stop():
    evs = [prompt("12:00:00"), ev("12:00:10", "WorkType", type="research")]
    evs += helper("12:01:00", None, "researcher", "Long dig", bg=True)
    evs.append(ev("12:02:00", "Stop"))                       # the turn ends; the background helper does not
    st = build(evs)
    s = lane(st)
    assert s["status"] == "working"                          # a running helper keeps the lane open
    assert agents(s)[1:] == [("chief", "done"), ("analyst", "working")]
    assert st["counts"] == {"analyst": 1}                    # chief is not working, the helper is
    evs.append(ev("12:20:00", "SubagentStop", in_agent="a1", agent_id="a1", agent_type="researcher"))
    st = build(evs)
    assert lane(st)["status"] == "idle" and agents(lane(st)) == [("op", "done"), ("chief", "done"), ("analyst", "done")]
    assert "partner" not in st["counts"]


# --- labels --------------------------------------------------------------------------------------------
def test_worktype_label_and_title_case():
    s = lane(build([prompt("12:00:00"), ev("12:00:05", "WorkType", type="quick-question")]))
    assert s["type"] == "Quick question"
    s = lane(build([prompt("12:00:00"), ev("12:00:05", "WorkType", type="data-cleanup")]))
    assert s["type"] == "Data Cleanup"
    s = lane(build([prompt("12:00:00"), ev("12:00:05", "WorkType", type="legal")]))
    assert s["type"] == "Legal or money"


def test_playbook_label_and_remaining_route_as_next_steps():
    evs = [prompt("12:00:00", cmd="daily-review")]
    evs += helper("12:02:00", None, "drafter", "Draft the letter")
    s = lane(build(evs))
    assert (s["type"], s["tag"], s["ask"]) == ("Research", "/daily-review", False)
    assert agents(s) == [("op", "done"), ("chief", "working"), ("writer", "working"),
                         ("inspector", "wait"), ("chief", "wait"), ("op", "wait")]
    assert s["steps"][0]["what"] == "ran /daily-review"
    assert s["steps"][3]["what"] == ""


def test_playbook_beats_worktype_and_skill_tool_counts():
    evs = [prompt("12:00:00"), ev("12:00:01", "PreToolUse", tool="Skill", cmd="optimize-prompt"),
           ev("12:00:09", "WorkType", type="writing")]
    assert lane(build(evs))["type"] == "Sharpen an idea"
    evs = [prompt("12:00:00"), ev("12:00:01", "UserPromptExpansion", cmd="/start-of-day", kind="slash")]
    assert lane(build(evs))["type"] == "Product morning"


def test_inferred_labels():
    for atype, label in [("implementer", "Coding"), ("researcher", "Research"), ("drafter", "Writing"),
                         ("senior-reviewer", "Legal or money")]:
        s = lane(build([prompt("12:00:00")] + helper("12:01:00", None, atype, "x")))
        assert (s["type"], s["tag"]) == (label, "just ask · inferred")
    s = lane(build([prompt("12:00:00")] + helper("12:01:00", "12:01:20", "scout", "x")))
    assert s["type"] == "Quick question"
    s = lane(build([prompt("12:00:00")] + helper("12:01:00", None, "test-runner", "x")))
    assert s["type"] == "Chief working"
    assert lane(build([prompt("12:00:00")]))["type"] == "Chief working"


def test_current_task_starts_at_latest_worktype():
    evs = [prompt("11:00:00"), ev("11:00:05", "WorkType", type="coding")]
    evs += helper("11:01:00", "11:03:00", "implementer", "Old task", tuid="t1", aid="a1")
    evs += [ev("11:10:00", "Stop"), prompt("12:00:00"), ev("12:00:05", "WorkType", type="research")]
    evs += helper("12:01:00", None, "researcher", "New task", tuid="t2", aid="a2")
    s = lane(build(evs))
    assert s["type"] == "Research" and s["since"] == "12:00"
    assert [x["what"] for x in s["steps"][2:]] == ["New task"]


def test_steps_capped_to_last_12():
    evs = [prompt("12:00:00"), ev("12:00:01", "WorkType", type="coding")]
    for i in range(15):
        evs += helper(f"12:01:{i:02d}", f"12:01:{i:02d}", "scout", f"h{i}", aid=f"a{i}", tuid=f"t{i}")
    steps = lane(build(evs))["steps"]
    assert len(steps) == 12 and steps[-1]["what"] == "h14"


# --- exclusions ----------------------------------------------------------------------------------------
def test_atlas_job_sessions_excluded():
    evs = [prompt("12:00:00", sid="job1"), prompt("12:00:00", sid="mine")]
    st = build(evs, exclude={"job1"})
    assert [s["sid"] for s in st["sessions"]] == ["mine"]
    assert [r["sid"] for r in st["timeline"]["rows"]] == ["mine"]


def test_exclude_project_dirs():
    evs = [prompt("12:00:00", sid="inbox", folder="C--Users-user--atlas-inbox"),
           prompt("12:00:00", sid="tmp", folder="C--Users-user-AppData-Local-Temp"),
           prompt("12:00:00", sid="sys", folder="C--WINDOWS-system32"),
           prompt("12:00:00", sid="nested", folder="C--Users-user-AppData-Local-Temp-pytest-of-user-pytest-1374-x0"),
           prompt("12:00:00", sid="job", folder="D--AI-Atlas-worktrees-job-20260924-101010-abcd"),
           prompt("12:00:00", sid="desk", folder="D--AI-product")]
    st = build(evs)
    assert [s["sid"] for s in st["sessions"]] == ["desk"]
    assert st["sessions"][0]["project"] == "product"


def test_excluded_folder_from_cwd_when_no_transcript():
    e = ev("12:00:00", "PreToolUse", sid="x", tool="Bash")
    e["cwd"] = r"C:\WINDOWS\system32"
    assert build([e])["sessions"] == []


def test_project_labels():
    assert teamlive.project_label("D--AI-Atlas") == "Atlas"
    assert teamlive.project_label("D--AI-product") == "product"
    assert teamlive.project_label("D--Personal-Reading-List") == "Reading List"
    assert teamlive.project_label("D--AI-Atlas--claude-worktrees-agent-a4d7c") == "Atlas"
    assert teamlive.project_label("", r"D:\Work\Thing") == "Thing"


# --- idle / ended ---------------------------------------------------------------------------------------
def test_idle_collapsed_and_idle_since():
    evs = [prompt("12:00:00"), ev("12:00:05", "WorkType", type="quick-question"), ev("12:10:00", "Stop")]
    st = build(evs)
    s = lane(st)
    assert (s["status"], s["idle_since"], s["type"]) == ("idle", "12:10", "Quick question")
    assert st["working"] == 0 and st["counts"] == {}


def test_idle_over_four_hours_leaves_live_view_but_stays_in_timeline():
    evs = [prompt("07:00:00"), ev("07:30:00", "Stop")]
    st = build(evs)
    assert st["sessions"] == []
    assert len(st["timeline"]["rows"]) == 1


def test_session_end_hides_but_a_resume_revives():
    evs = [prompt("12:00:00"), ev("12:10:00", "Stop"), ev("12:11:00", "SessionEnd", reason="exit")]
    st = build(evs)
    assert st["sessions"] == [] and len(st["timeline"]["rows"]) == 1
    st = build(evs + [ev("12:20:00", "SessionStart", source="resume"), prompt("12:21:00")])
    assert lane(st)["status"] == "working"


def test_working_with_no_events_for_half_an_hour_reads_idle():
    s = lane(build([prompt("11:40:00")]))                  # NOW is 12:30
    assert s["status"] == "idle"


# --- counts, timeline, cost -----------------------------------------------------------------------------
def test_team_counts_two_sessions_same_helper():
    evs = [prompt("12:00:00", sid="s1"), prompt("12:00:00", sid="s2")]
    evs += helper("12:05:00", None, "implementer", "a", sid="s1", aid="a1", tuid="t1")
    evs += helper("12:06:00", None, "implementer", "b", sid="s2", aid="a2", tuid="t2")
    evs += helper("12:07:00", None, "scout", "c", sid="s2", aid="a3", tuid="t3")
    st = build(evs)
    assert st["counts"] == {"chief": 2, "builder": 2, "scout": 1}
    assert st["working"] == 2 and st["active"] == 5


def test_timeline_segments_are_tiered_and_cost_summed():
    evs = [prompt("12:00:00"), ev("12:00:02", "WorkType", type="coding")]
    evs += helper("12:10:00", "12:20:00", "implementer", "x")
    evs += [ev("12:25:00", "Stop")]
    st = build(evs, cost=lambda sid, tr: 1234)
    row = st["timeline"]["rows"][0]
    tiers = [g[2] for g in row["segs"]]
    assert "opus" in tiers and "sonnet" in tiers
    sonnet = next(g for g in row["segs"] if g[2] == "sonnet")
    assert abs(sonnet[0] - 12.1667) < 0.01 and abs(sonnet[1] - 12.3333) < 0.01
    assert row["cost"] == 1234 and st["cost"] == 1234 and lane(st)["cost"] == 1234
    assert st["timeline"]["start"] <= 11 and st["timeline"]["end"] >= 13


def test_cost_fn_failure_does_not_break_the_panel():
    def boom(sid, tr):
        raise RuntimeError("unreadable")
    assert lane(build([prompt("12:00:00")], cost=boom))["cost"] == 0


def test_tolerates_missing_keys_and_unknown_events():
    evs = [ev("12:00:00", "SessionStart", source="startup"), ev("12:00:01", "Mystery"),
           ev("12:00:02", "SubagentStart", agent_id="zz"), ev("12:00:03", "UserPromptSubmit")]
    st = build(evs)
    assert st["ok"] and lane(st)["status"] == "working"


# --- cost reader ----------------------------------------------------------------------------------------
def test_session_cost_reads_main_and_subagent_transcripts(tmp_path):
    today = dt.datetime.now().astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = "2020-01-01T00:00:00.000Z"

    def rec(model, i, o, ts=today):
        return json.dumps({"timestamp": ts, "message": {"model": model, "usage": {"input_tokens": i, "output_tokens": o}}})
    d = tmp_path / "D--AI-Atlas"
    (d / "sid9" / "subagents").mkdir(parents=True)
    main = d / "sid9.jsonl"
    main.write_text(rec("claude-sonnet-5", 100, 50) + "\n" + rec("claude-sonnet-5", 999, 999, old) + "\n", encoding="utf-8")
    (d / "sid9" / "subagents" / "agent-1.jsonl").write_text(rec("claude-sonnet-5", 10, 5) + "\n", encoding="utf-8")
    assert teamlive.session_cost("sid9", str(main)) == 165
    with open(main, "a", encoding="utf-8") as f:                 # incremental: only the new tail is read
        f.write(rec("claude-sonnet-5", 1, 1) + "\n")
    assert teamlive.session_cost("sid9", str(main)) == 167


# --- loading + endpoint ----------------------------------------------------------------------------------
def test_load_events_reads_yesterday_and_today_and_skips_bad_lines(tmp_path):
    (tmp_path / "events-2026-10-07.jsonl").write_text(
        json.dumps({"ts": "2026-10-07T23:59:00", "ev": "UserPromptSubmit", "sid": "s1"}) + "\n", encoding="utf-8")
    (tmp_path / "events-2026-10-08.jsonl").write_text(
        "not json\n" + json.dumps({"ts": "2026-10-08T00:01:00", "ev": "Stop", "sid": "s1"}) + "\n\n", encoding="utf-8")
    (tmp_path / "events-2026-10-06.jsonl").write_text(
        json.dumps({"ts": "2026-10-06T10:00:00", "ev": "Stop", "sid": "old"}) + "\n", encoding="utf-8")
    evs = teamlive.load_events(tmp_path, NOW)
    assert [e["ev"] for e in evs] == ["UserPromptSubmit", "Stop"]


def test_atlas_job_sids_reads_nested_lane_sessions(tmp_path):
    (tmp_path / "job_1.json").write_text(json.dumps(
        {"session_id": "top", "lanes": [{"session_id": "lane1"}, {"x": {"session_id": "lane2"}}]}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert teamlive.atlas_job_sids(tmp_path) == {"top", "lane1", "lane2"}


def test_snapshot_with_an_empty_dir_is_ok(tmp_path):
    out = teamlive.snapshot(tmp_path, tmp_path / "jobs", now=NOW)
    assert out["ok"] is True and out["sessions"] == [] and out["counts"] == {}


# --- review fixes -----------------------------------------------------------------------------------
def _stop(t, aid, atype, sid="s1"):
    return ev(t, "SubagentStop", sid, in_agent=aid, agent_id=aid, agent_type=atype)


def _start(t, aid, atype, sid="s1"):
    return ev(t, "SubagentStart", sid, in_agent=aid, agent_id=aid, agent_type=atype)


def test_helper_restarted_under_the_same_id_is_one_running_step():
    # live shape: senior-reviewer Start 12:23, Stop 12:26, Start again 12:28 (resumed), Stop 12:28:21
    evs = [prompt("12:20:00"), ev("12:20:05", "WorkType", type="legal")]
    evs += helper("12:23:06", None, "senior-reviewer", "Privacy questions", aid="aX", tuid="t1")
    evs += [_stop("12:26:44", "aX", "senior-reviewer"), _start("12:28:06", "aX", "senior-reviewer")]
    s = lane(build(evs))
    partner = [x for x in s["steps"] if x["agent"] == "partner"]
    assert len(partner) == 1                                   # repeats are one step
    assert partner[0]["state"] == "working" and partner[0]["what"] == "Privacy questions (resumed)"
    st = build(evs + [_stop("12:28:21", "aX", "senior-reviewer")])
    assert [x["state"] for x in lane(st)["steps"] if x["agent"] == "partner"] == ["done"]
    assert "partner" not in st["counts"]


def test_earlier_run_of_a_restarted_id_is_never_running():
    evs = [prompt("12:20:00"), _start("12:21:00", "aX", "scout"), _start("12:25:00", "aX", "scout")]
    runs = teamlive._helpers(sorted(evs, key=lambda e: e["_t"]), NOW)
    assert runs[0]["stop"] is not None and runs[1]["stop"] is None


def test_atlas_conversational_session_ids_are_excluded(tmp_path, monkeypatch):
    import atlas_sessions
    f = tmp_path / "sessions.json"
    f.write_text(json.dumps(["conv1"]), encoding="utf-8")
    monkeypatch.setattr(atlas_sessions, "SESSIONS", f)
    d = tmp_path / "tl"
    d.mkdir()
    rows = [prompt("12:25:00", sid="conv1"), prompt("12:25:00", sid="real")]
    (d / "events-2026-10-08.jsonl").write_text(
        "\n".join(json.dumps({k: v for k, v in e.items() if k != "_t"}) for e in rows) + "\n", encoding="utf-8")
    out = teamlive.snapshot(d, tmp_path / "jobs", now=NOW)
    assert [s["sid"] for s in out["sessions"]] == ["real"]


def test_short_session_with_no_task_signal_is_hidden(monkeypatch):
    monkeypatch.setattr(teamlive, "MIN_LIFETIME", dt.timedelta(seconds=30))
    evs = [prompt("12:20:00", sid="cli"), ev("12:20:08", "Stop", sid="cli"),                 # 8 s, nothing else
           prompt("12:20:00", sid="typed"), ev("12:20:03", "WorkType", sid="typed", type="coding"),
           prompt("12:20:00", sid="helped")] + helper("12:20:01", "12:20:05", "scout", "x", sid="helped", aid="h1", tuid="h1")
    assert sorted(s["sid"] for s in build(evs)["sessions"]) == ["helped", "typed"]
    longer = [prompt("12:10:00", sid="cli"), ev("12:11:00", "Stop", sid="cli")]              # a minute: a real exchange
    assert [s["sid"] for s in build(longer)["sessions"]] == ["cli"]


def test_parallel_same_type_helpers_keep_their_own_labels():
    # b (second Start) is tuid-linked to the FIRST Pre; a (first Start, still running) has no Post yet and
    # must not steal that Pre through the same-type fallback.
    evs = [prompt("12:00:00"), ev("12:00:01", "WorkType", type="research"),
           ev("12:01:00", "PreToolUse", tool="Agent", agent_type="scout", label="first", bg=False, tuid="t1"),
           ev("12:01:00", "PreToolUse", tool="Agent", agent_type="scout", label="second", bg=False, tuid="t2"),
           _start("12:01:01", "a", "scout"), _start("12:01:01", "b", "scout"),
           _stop("12:01:10", "b", "scout"),
           ev("12:01:10", "PostToolUse", tool="Agent", tuid="t1", status="completed", agent_id="b")]
    steps = [(x["what"], x["state"]) for x in lane(build(evs))["steps"] if x["agent"] == "scout"]
    assert steps == [("second", "working"), ("first", "done")]


def test_cost_fn_only_called_for_sessions_active_today():
    called = []
    old = ev("10:00:00", "UserPromptSubmit", sid="old")
    old["ts"], old["_t"] = "2026-10-07T10:00:00", dt.datetime(2026, 10, 7, 10, 0, 0)
    old_stop = dict(old, ev="Stop", ts="2026-10-07T10:05:00", _t=dt.datetime(2026, 10, 7, 10, 5, 0))
    build([old, old_stop, prompt("12:00:00", sid="new")], cost=lambda sid, tr: called.append(sid) or 1)
    assert called == ["new"]


def test_cost_reader_streams_and_waits_for_a_complete_last_line(tmp_path):
    today = dt.datetime.now().astimezone().astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    line = json.dumps({"timestamp": today, "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 5}}})
    f = tmp_path / "t.jsonl"
    f.write_bytes((line + "\n" + line[:40]).encode())           # second record is half-written
    assert teamlive._file_cost_today(f, dt.date.today()) == 15
    f.write_bytes((line + "\n" + line + "\n").encode())
    assert teamlive._file_cost_today(f, dt.date.today()) == 30


def test_load_events_cache_skips_unchanged_files_and_reads_only_the_tail(tmp_path, monkeypatch):
    f = tmp_path / "events-2026-10-08.jsonl"

    def row(t):
        return json.dumps({"ts": f"2026-10-08T{t}", "ev": "Stop", "sid": "s1"}) + "\n"
    f.write_text(row("12:00:00"), encoding="utf-8")
    assert len(teamlive.load_events(tmp_path, NOW)) == 1
    calls = []
    real = json.loads
    monkeypatch.setattr(json, "loads", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert len(teamlive.load_events(tmp_path, NOW)) == 1 and calls == []     # unchanged: nothing parsed
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(row("12:01:00"))
    assert len(teamlive.load_events(tmp_path, NOW)) == 2 and len(calls) == 1   # only the new line


def test_any_activity_after_a_stop_reads_working_again():
    base = [prompt("12:20:00"), ev("12:21:00", "Stop")]
    assert lane(build(base))["status"] == "idle"
    for e in (ev("12:25:00", "PreToolUse", tool="Bash"), ev("12:25:00", "WorkType", type="coding"),
              _start("12:25:00", "z", "scout"), ev("12:25:00", "PostToolUse", tool="Bash")):
        assert lane(build(base + [e]))["status"] == "working", e["ev"]
    inner = ev("12:25:00", "PreToolUse", tool="Bash", in_agent="z")         # a helper's own tool use is not the Chief's
    assert lane(build(base + [inner]))["status"] == "idle"


def test_foreground_helper_without_a_stop_stays_done_across_later_turns():
    evs = [prompt("12:00:00"), ev("12:00:02", "WorkType", type="coding")]
    evs += helper("12:01:00", None, "implementer", "Build", aid="f1", tuid="t1", post=False)[:2]   # no SubagentStop, no Post
    evs += [ev("12:05:00", "Stop"), prompt("12:20:00")]                      # a later turn is running
    s = lane(build(evs))
    assert s["status"] == "working"
    assert [x["state"] for x in s["steps"] if x["agent"] == "builder"] == ["done"]
    assert "builder" not in build(evs)["counts"]


def test_working_session_is_shown_even_when_young_and_plain(monkeypatch):
    monkeypatch.setattr(teamlive, "MIN_LIFETIME", dt.timedelta(seconds=30))     # the real rule, not the fixture's 0
    for first in ("12:29:55", "12:28:00"):                                       # +5 s and +120 s before NOW
        evs = [ev(first, "SessionStart", source="startup"), prompt(first)]
        s = lane(build(evs))
        assert s["status"] == "working", first
    done = [ev("12:29:50", "SessionStart", source="startup"), prompt("12:29:50"), ev("12:29:55", "Stop")]
    assert build(done)["sessions"] == []                                         # finished + short + no signal: hidden


def test_resumed_background_helper_keeps_running_past_a_session_stop():
    evs = [prompt("12:20:00"), ev("12:20:01", "WorkType", type="legal")]
    evs += helper("12:23:00", None, "senior-reviewer", "Review", aid="bg1", tuid="t1", bg=True)
    evs += [_stop("12:24:00", "bg1", "senior-reviewer"), _start("12:25:00", "bg1", "senior-reviewer"),   # resumed: no Pre/Post
            ev("12:26:00", "Stop")]
    st = build(evs)
    s = lane(st)
    assert s["status"] == "working"
    assert [x["state"] for x in s["steps"] if x["agent"] == "partner"] == ["working"]
    assert st["counts"] == {"partner": 1}


# ---- session titles (the name in the Claude sidebar) ------------------------------------------------

def test_title_comes_from_the_latest_custom_title_record(tmp_path):
    tr = tmp_path / "s1.jsonl"
    lines = [
        {"type": "custom-title", "customTitle": "First name", "sessionId": "s1"},
        # a message that merely MENTIONS custom-title is not a title, and its tokens still count
        {"type": "assistant", "timestamp": dt.datetime.now().astimezone().isoformat(),
         "message": {"model": "claude-sonnet-5-5", "content": "look for \"custom-title\" records",
                     "usage": {"input_tokens": 100, "output_tokens": 0}}},
        {"type": "custom-title", "customTitle": "Product Work", "sessionId": "s1"},
    ]
    tr.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    teamlive._COST_CACHE.clear()
    assert teamlive.session_title("s1", str(tr)) == "Product Work"
    assert teamlive.session_cost("s1", str(tr)) == 100


def test_lane_and_timeline_carry_the_title_and_fall_back_without_one():
    evs = [prompt("12:00:00", sid="s1"), ev("12:00:05", "WorkType", sid="s1", type="coding"),
           prompt("12:01:00", sid="s2")]
    titles = {"s1": "Product Work"}
    st = teamlive.build_state(sorted(evs, key=lambda e: e["_t"]), NOW, (), None,
                              lambda sid, tr: titles.get(sid))
    by = {s["sid"]: s for s in st["sessions"]}
    assert by["s1"]["title"] == "Product Work" and by["s1"]["type"] == "Coding"
    assert by["s2"]["title"] is None


def test_batch_launch_with_a_lost_post_still_pairs_label_and_background():
    # Seen live 2026-10-08: six helpers launched in one message; their Starts came ~20 s after the launches,
    # and one PostToolUse (the researcher's) never reached the log. It must keep its label and stay background.
    evs = [prompt("12:00:00"),
           ev("12:00:06", "PreToolUse", tool="Agent", agent_type="researcher", label="Distil Atlas design docs",
              bg=True, tuid="tR"),
           ev("12:00:13", "PreToolUse", tool="Agent", agent_type="scout", label="Map the code", bg=True, tuid="tS"),
           ev("12:00:26", "SubagentStart", agent_id="aR", agent_type="researcher", in_agent="aR"),
           ev("12:00:26", "SubagentStart", agent_id="aS", agent_type="scout", in_agent="aS"),
           ev("12:00:26", "PostToolUse", tool="Agent", tuid="tS", status="async_launched", agent_id="aS"),
           ev("12:00:36", "Stop")]
    st = build(evs)
    steps = {x["agent"]: x for x in lane(st)["steps"]}
    assert steps["analyst"]["what"] == "Distil Atlas design docs" and steps["analyst"]["state"] == "working"
    assert st["counts"].get("analyst") == 1 and st["counts"].get("scout") == 1


# --- v2 additions: cwd per session, `waiting` (contracts.md §5.1) ------------------------------------------
def test_v2_session_carries_cwd():
    s = lane(build([prompt("12:00:00")]))
    assert s["cwd"] == r"D:\AI\Atlas"


def test_v2_waiting_is_idle_after_a_stop_within_two_hours():
    s = lane(build([prompt("12:00:00"), ev("12:10:00", "Stop")]))
    assert (s["status"], s["waiting"]) == ("idle", True)
    s = lane(build([prompt("12:00:00")]))                     # still working: not waiting
    assert (s["status"], s["waiting"]) == ("working", False)


def test_v2_waiting_ends_after_two_hours_and_ignores_helper_events():
    # NOW is 12:30: a Stop at 10:00 is 2.5 h old, so the session is idle but no longer "waiting"
    s = lane(build([prompt("09:50:00"), ev("10:00:00", "Stop")]))
    assert (s["status"], s["waiting"]) == ("idle", False)
    # a late helper stop after the Stop is not a main-session event
    evs = [prompt("12:00:00"), ev("12:10:00", "Stop"), ev("12:11:00", "SubagentStop", in_agent="a1", agent_id="a1", agent_type="scout")]
    assert lane(build(evs))["waiting"] is True


def test_v2_area_arg_from_the_work_type_event():
    s = lane(build([prompt("12:00:00"), ev("12:00:05", "WorkType", type="coding", area="Job")]))
    assert s["area_arg"] == "job"
    assert lane(build([prompt("12:00:00")]))["area_arg"] is None


def test_launched_but_never_stopped_helper_is_closed_once_the_session_moved_on():
    # a background helper killed by TaskStop: no SubagentStop and no Post status, ever
    evs = [prompt("12:00:00"), ev("12:00:02", "WorkType", type="coding")]
    evs += helper("12:01:00", None, "implementer", "Build", aid="k1", tuid="t1", bg=True)
    evs += [ev("12:05:00", "Stop"), prompt("12:10:00")]
    s = lane(build(evs))
    assert [x["state"] for x in s["steps"] if x["agent"] == "builder"] == ["done"]
    assert "builder" not in build(evs)["counts"]


def test_silent_helper_is_kept_while_the_session_has_not_moved_on_or_it_is_recent():
    evs = [prompt("12:00:00"), ev("12:00:02", "WorkType", type="coding")]
    evs += helper("12:01:00", None, "implementer", "Build", aid="k1", tuid="t1", bg=True)
    evs += [ev("12:05:00", "Stop")]                       # no new prompt, 29 min of silence: still shown working
    assert [x["state"] for x in lane(build(evs))["steps"] if x["agent"] == "builder"] == ["working"]
    evs += [prompt("12:10:00"), ev("12:25:00", "PreToolUse", in_agent="k1", tool="Skill", cmd="x")]
    assert [x["state"] for x in lane(build(evs))["steps"] if x["agent"] == "builder"] == ["working"]   # recent helper event


def test_timeline_row_carries_title_and_type_without_the_project():
    # Team rows read "title · type of work · area chip" (docs/v2/review-changes.md): the folder is not in them.
    evs = [prompt("12:00:00"), ev("12:00:02", "WorkType", type="coding")]
    evs += helper("12:10:00", "12:20:00", "implementer", "x")
    evs += [ev("12:25:00", "Stop")]
    titled = teamlive.build_state(sorted(evs, key=lambda e: e["_t"]), NOW, (), lambda sid, tr: 5, lambda sid, tr: "Product Work")
    row = titled["timeline"]["rows"][0]
    assert row["label"] == "Product Work" and row["type"] == "Coding"
    untitled = build(evs, cost=lambda sid, tr: 5)["timeline"]["rows"][0]
    assert untitled["label"] == "Coding" and untitled["type"] == "Coding"          # no folder text, no " · "
    assert "project" in lane(build(evs))                                            # still sent, only not shown


def test_work_type_plan_ref_is_carried_on_the_session():
    s = lane(build([prompt("12:00:00"), ev("12:00:30", "WorkType", type="coding", plan_ref="t_deadbeef")]))
    assert s["plan_ref"] == "t_deadbeef"
    assert lane(build([prompt("12:00:00"), ev("12:00:30", "WorkType", type="coding")]))["plan_ref"] is None


def test_plan_ref_from_a_helper_is_ignored():
    evs = [prompt("12:00:00"), ev("12:00:30", "WorkType", type="coding", plan_ref="t_deadbeef"),
           ev("12:01:00", "WorkType", type="research", area="md", plan_ref="t_aaaaaaaa", in_agent="A1")]
    s = lane(build(evs))
    assert s["plan_ref"] == "t_deadbeef" and s["type"] == "Coding" and s["area_arg"] is None


# ---- live helpers: the helper's own transcript is the evidence (docs/v2/contracts.md 5.1) -------------

def _bg_session(stop_at=None):
    """A main conversation that handed work to a background helper and finished its turn at 12:02."""
    evs = [prompt("12:00:00"), ev("12:00:02", "WorkType", type="coding")]
    evs += helper("12:01:00", stop_at, "implementer", "Build the thing", aid="bg1", tuid="t1", bg=True)
    evs += [ev("12:02:00", "Stop")]
    return evs


def _build_mt(events, mtimes):
    """build_state with a stand-in for the helper-transcript lookup: {agent_id: last write | None}."""
    return teamlive.build_state(sorted(events, key=lambda e: e["_t"]), NOW, (), None, None,
                                lambda sid, transcript, aid: mtimes.get(aid))


def test_live_background_helper_keeps_the_session_working_after_the_turn_ends():
    evs = _bg_session()
    evs += [prompt("12:03:00"), ev("12:03:10", "Stop")]                   # the session has moved on, helper silent in the log
    fresh = NOW - dt.timedelta(seconds=30)                                # ... but its transcript is being written
    st = _build_mt(evs, {"bg1": fresh})
    s = lane(st)
    assert s["status"] == "working" and s["waiting"] is False
    assert s["via_helpers"] is True
    assert s["live_helpers"] == [{"agent": "builder", "name": "Builder", "label": "Build the thing", "bg": True}]
    assert ("builder", "working") in agents(s) and ("chief", "done") in agents(s)
    assert st["working"] == 1 and st["counts"] == {"builder": 1}
    # without transcript evidence the old rule (session moved on + 10 min of log silence) would have closed it
    assert lane(build(evs))["status"] == "idle"


def test_stopped_background_helper_returns_the_session_to_waiting():
    evs = _bg_session(stop_at="12:10:00")
    st = _build_mt(evs, {"bg1": NOW - dt.timedelta(seconds=5)})           # transcript fresh, but the Stop is logged: done
    s = lane(st)
    assert s["status"] == "idle" and s["waiting"] is True and s["via_helpers"] is False and s["live_helpers"] == []
    assert ("builder", "done") in agents(s)


def test_helper_start_without_stop_and_a_stale_transcript_is_a_ghost():
    evs = _bg_session()
    just_over = NOW - teamlive.HELPER_FRESH - dt.timedelta(seconds=1)
    s = lane(_build_mt(evs, {"bg1": just_over}))
    assert s["status"] == "idle" and s["waiting"] is True and s["via_helpers"] is False
    assert ("builder", "done") in agents(s)
    just_under = NOW - teamlive.HELPER_FRESH + dt.timedelta(seconds=1)
    assert lane(_build_mt(evs, {"bg1": just_under}))["status"] == "working"
    assert teamlive.HELPER_FRESH == dt.timedelta(minutes=8)
    # a full test suite runs ~5 min in one Bash call with no transcript write: still working
    assert lane(_build_mt(evs, {"bg1": NOW - dt.timedelta(minutes=5, seconds=30)}))["status"] == "working"


def test_unknown_transcript_falls_back_to_the_event_log():
    evs = _bg_session()
    for mt in ({}, {"bg1": None}):
        assert lane(_build_mt(evs, mt))["status"] == "working"            # same as before: no Stop, session not moved on
    assert teamlive.helper_transcript_mtime("s1", r"Z:\nowhere\s1.jsonl", "bg1") is None


def test_foreground_helper_is_unchanged_by_the_transcript_check():
    evs = [prompt("12:00:00"), ev("12:00:02", "WorkType", type="coding")]
    evs += helper("12:01:00", "12:05:00", "implementer", "Build", aid="f1", tuid="t1")      # finished, Stop logged
    evs += [ev("12:06:00", "Stop")]
    stale = {"f1": NOW - dt.timedelta(hours=1)}
    s = lane(_build_mt(evs, stale))
    assert s["status"] == "idle" and s["via_helpers"] is False and ("builder", "done") in agents(s)
    running = [prompt("12:29:00"), ev("12:29:01", "WorkType", type="coding")]
    running += helper("12:29:10", None, "implementer", "Build", aid="f2", tuid="t2", post=False)[:2]
    s = lane(_build_mt(running, {"f2": NOW - dt.timedelta(seconds=10)}))
    assert s["status"] == "working" and s["via_helpers"] is False         # the conversation itself is working too
    assert s["live_helpers"][0]["bg"] is False


def test_helper_transcript_mtime_reads_the_subagents_folder(tmp_path):
    import os
    main = tmp_path / "proj" / "sid1.jsonl"
    sub = tmp_path / "proj" / "sid1" / "subagents"
    sub.mkdir(parents=True)
    main.write_text("{}\n", encoding="utf-8")
    f = sub / "agent-h1.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    t = dt.datetime(2026, 10, 8, 12, 29, 0)
    os.utime(f, (t.timestamp(), t.timestamp()))
    assert teamlive.helper_transcript_mtime("sid1", str(main), "h1") == t
    assert teamlive.helper_transcript_mtime("sid1", str(main), "missing") is None
