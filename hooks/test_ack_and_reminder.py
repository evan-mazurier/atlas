"""/ack-break (operator-only, honoured from UserPromptSubmit) and the once-per-task "declare new work" reminder.
Temp ledgers only (ATLAS_LEDGER2_DIR / ATLAS_HOOKS_DIR via the Env fixture)."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "cockpit"))

from ledger2 import ledger_chain, store  # noqa: E402
from test_atlas_hook import Env, SID, trip  # noqa: E402


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


T0 = dt.datetime(2026, 10, 9, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def seed(env, n=25):
    store.append_events([{"ev": "prompt", "sid": "old", "t": (T0 + dt.timedelta(seconds=5 * i)).isoformat(timespec="seconds")}
                         for i in range(n)], env.ledger)
    return env.ledger / "events-2026-10-09.jsonl"


def delete_line(f, i):
    ls = f.read_bytes().split(b"\n")[:-1]
    del ls[i]
    f.write_bytes(b"\n".join(ls) + b"\n")


def acks(env):
    return env.events("integrity_ack")


# ------------------------------------------------------------------ reminder

def ctx(out):
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext")


def test_reminder_when_builder_spawned_over_budget_once_per_task(env):
    trip(env)
    out = env.agent("implementer", "b1")
    assert "over its rework budget" in ctx(out) and ": team-type <type> [area]" in ctx(out)
    rem = env.events("reminder")
    assert len(rem) == 1 and rem[0]["task"] == "s1abcdef-1"
    assert env.agent("implementer", "b2") is None            # once per task
    assert len(env.events("reminder")) == 1
    env.bash(": team-type research")                          # a new task gets its own reminder
    assert env.state()["reminded"] is None


def test_no_reminder_for_reviewers_or_in_budget(env):
    env.bash(": team-type coding")
    env.edit("a.py")
    assert env.agent("implementer", "b1") is None
    assert env.agent("reviewer", "r1") is None
    assert not env.events("reminder")


def test_reminder_when_open_over_3h_with_5_spawns(env):
    env.bash(": team-type coding")
    for i in range(4):
        assert env.agent("scout", "s%d" % i) is None          # 4 spawns, task is young: nothing
    p = env.ledger / "state" / (SID + ".json")
    st = json.loads(p.read_text(encoding="utf-8"))
    st["task_t"] = (dt.datetime.now().astimezone() - dt.timedelta(hours=4)).isoformat(timespec="seconds")
    p.write_text(json.dumps(st), encoding="utf-8")
    out = env.agent("scout", "s9")                            # the 5th spawn of a task open for 4 h
    assert "open a long time" in ctx(out)
    assert env.events("reminder")[0]["why"] == "has been open a long time"
    assert env.agent("scout", "s10") is None


def test_no_reminder_when_young_or_few_spawns(env):
    env.bash(": team-type coding")
    for i in range(6):
        assert env.agent("scout", "s%d" % i) is None          # many spawns but only seconds old
    p = env.ledger / "state" / (SID + ".json")
    st = json.loads(p.read_text(encoding="utf-8"))
    st.update(task_t=(dt.datetime.now().astimezone() - dt.timedelta(hours=4)).isoformat(timespec="seconds"), spawns=2)
    p.write_text(json.dumps(st), encoding="utf-8")
    assert env.agent("scout", "x") is None                    # old but only 3 spawns
    assert not env.events("reminder")


def test_enforce_refusal_records_who_was_refused_and_no_reminder(env):
    trip(env)
    env.set_mode("enforce")
    out = env.agent("drafter", "d1", label="Write the letter")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    r = env.events("refusal")[-1]
    assert r["helper"] == "drafter" and r["label"] == "Write the letter" and r["would"] is False
    assert not env.events("reminder")
