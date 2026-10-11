"""Contract §6.4 rule A (hash chain in the store) and §5.2 additions (usage_by_type, bypass, integrity)."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "cockpit"))

from ledger2 import query, stats, store  # noqa: E402


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(d))
    monkeypatch.setenv("ATLAS_REPORTS_DIR", str(tmp_path / "reports"))
    stats.clear_cache()
    stats._integrity_cache.clear()
    query.invalidate()
    return d


def ev(day, n, name="x", **kw):
    return dict({"t": "%sT10:%02d:00+02:00" % (day, n), "ev": name, "sid": "s1"}, **kw)


def lines_of(path):
    return path.read_text(encoding="utf-8").splitlines()


# ------------------------------------------------------------------ chain

def test_first_line_is_genesis_and_each_line_hashes_the_previous(ledger):
    store.append([ev("2026-10-07", i) for i in range(4)])
    f = ledger / "events-2026-10-07.jsonl"
    ls = lines_of(f)
    objs = [json.loads(x) for x in ls]
    assert objs[0]["prev"] == "genesis"
    for i in range(1, 4):
        assert objs[i]["prev"] == store.line_hash(ls[i - 1].encode("utf-8"))
    assert store.verify_chain() == {"intact": True, "breaks": [], "files": 1, "lines": 4}


def test_chain_crosses_day_files(ledger):
    store.append([ev("2026-10-07", 1), ev("2026-10-07", 2)])
    store.append([ev("2026-10-09", 1)])                        # a day with no file between
    last_of_7 = lines_of(ledger / "events-2026-10-07.jsonl")[-1]
    first_of_9 = json.loads(lines_of(ledger / "events-2026-10-09.jsonl")[0])
    assert first_of_9["prev"] == store.line_hash(last_of_7.encode("utf-8"))
    assert store.verify_chain()["intact"]


def test_batch_spanning_two_days_is_chained(ledger):
    store.append([ev("2026-10-07", 1), ev("2026-10-08", 1), ev("2026-10-08", 2)])
    assert store.verify_chain() == {"intact": True, "breaks": [], "files": 2, "lines": 3}


def test_deleted_line_is_reported_at_the_next_line(ledger):
    store.append([ev("2026-10-07", i) for i in range(5)])
    f = ledger / "events-2026-10-07.jsonl"
    ls = lines_of(f)
    del ls[2]
    f.write_text("\n".join(ls) + "\n", encoding="utf-8")
    res = store.verify_chain()
    assert not res["intact"] and res["breaks"] == [{"file": f.name, "line": 3}]


def test_edited_line_is_reported_at_the_next_line(ledger):
    store.append([ev("2026-10-07", i) for i in range(5)])
    f = ledger / "events-2026-10-07.jsonl"
    ls = lines_of(f)
    ls[1] = ls[1].replace('"ev": "x"', '"ev": "y"')
    f.write_text("\n".join(ls) + "\n", encoding="utf-8")
    assert store.verify_chain()["breaks"] == [{"file": f.name, "line": 3}]


def test_deleted_first_line_and_deleted_day_file_are_caught(ledger):
    store.append([ev("2026-10-07", 1), ev("2026-10-07", 2)])
    store.append([ev("2026-10-08", 1), ev("2026-10-08", 2)])
    store.append([ev("2026-10-09", 1)])
    f8 = ledger / "events-2026-10-08.jsonl"
    ls = lines_of(f8)
    f8.write_text(ls[1] + "\n", encoding="utf-8")               # first line of the day removed
    assert store.verify_chain()["breaks"] == [{"file": f8.name, "line": 1}]
    f8.write_text("\n".join(ls) + "\n", encoding="utf-8")
    assert store.verify_chain()["intact"]
    f8.unlink()                                                  # a whole day removed
    assert store.verify_chain()["breaks"] == [{"file": "events-2026-10-09.jsonl", "line": 1}]


def test_unchained_legacy_lines_are_accepted_but_a_gap_after_the_chain_starts_is_not(ledger):
    ledger.mkdir()
    f = ledger / "events-2026-10-06.jsonl"
    f.write_text(json.dumps(ev("2026-10-06", 1)) + "\n" + json.dumps(ev("2026-10-06", 2)) + "\n", encoding="utf-8")
    store.append([ev("2026-10-07", 1)])                          # chains from the legacy file's last line
    assert store.verify_chain()["intact"]
    with open(ledger / "events-2026-10-07.jsonl", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(ev("2026-10-07", 2)) + "\n")         # an unchained line after a chained one
    assert store.verify_chain()["breaks"] == [{"file": "events-2026-10-07.jsonl", "line": 2}]


def test_event_over_4kb_keeps_its_prev(ledger):
    store.append([ev("2026-10-07", 1), ev("2026-10-07", 2, problems=["p" * 200] * 40)])
    last = json.loads(lines_of(ledger / "events-2026-10-07.jsonl")[-1])
    assert "problems" not in last and len(last["prev"]) == 64
    assert store.verify_chain()["intact"]


def test_crlf_endings_do_not_change_the_hash(ledger):
    store.append([ev("2026-10-07", i) for i in range(3)])
    f = ledger / "events-2026-10-07.jsonl"
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    assert store.verify_chain()["intact"]


# ------------------------------------------------------------------ stats additions

def now_t(minutes_ago):
    return (dt.datetime.now().astimezone().replace(microsecond=0) - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def cost(sid, m, w):
    return {"v": 2, "t": now_t(m), "ev": "cost", "sid": sid, "scope": "main", "model": "claude-sonnet-5-5", "weighted": w}


def test_usage_by_type_orders_filters_and_counts_tasks(ledger):
    evs = [
        {"v": 2, "t": now_t(60), "ev": "work_type", "sid": "aaaa", "type": "coding", "task": "aaaa-1"},
        cost("aaaa", 59, 300),
        {"v": 2, "t": now_t(50), "ev": "work_type", "sid": "aaaa", "type": "research", "task": "aaaa-2"},
        cost("aaaa", 49, 100),
        {"v": 2, "t": now_t(40), "ev": "work_type", "sid": "aaaa", "type": "coding", "task": "aaaa-3"},
        cost("aaaa", 39, 50),
        {"v": 2, "t": now_t(35), "ev": "work_type", "sid": "aaaa", "type": "writing", "task": "aaaa-4"},   # no cost: not listed
        {"v": 2, "t": now_t(30), "ev": "playbook", "sid": "bbbb", "cmd": "daily-review", "task": "bbbb-1"},
        cost("bbbb", 29, 200),
        {"v": 2, "t": now_t(25), "ev": "work_type", "sid": "cccc", "task": "cccc-1"},               # undeclared type
        cost("cccc", 24, 900),
        cost("dddd", 20, 7),                                                                          # cost with no task
        cost("eeee", 19, 0),                                                                          # zero tokens: ignored
    ]
    store.append(evs)
    ubt = stats.compute()["usage_by_type"]
    assert [x["key"] for x in ubt] == ["coding", "playbook:daily-review", "research", "undeclared"]
    assert [x["weighted"] for x in ubt] == [350, 200, 100, 907]
    assert [x["tasks"] for x in ubt] == [2, 1, 1, 2]
    assert ubt[0]["label"] == "Coding" and ubt[1]["label"] == "/daily-review" and ubt[1]["kind"] == "playbook"
    assert ubt[-1]["kind"] == "undeclared" and ubt[-1]["label"] == "Undeclared"       # last, even though the largest


def test_usage_by_type_ignores_other_weeks(ledger):
    old = (dt.datetime.now().astimezone() - dt.timedelta(days=21)).replace(microsecond=0).isoformat(timespec="seconds")
    store.append([{"v": 2, "t": old, "ev": "work_type", "sid": "aaaa", "type": "coding", "task": "aaaa-1"},
                  dict(cost("aaaa", 0, 500), t=old)])
    assert stats.compute()["usage_by_type"] == []


def test_stats_reports_bypass_attempts_and_integrity(ledger):
    store.append([
        {"v": 2, "t": (dt.datetime.now().astimezone() - dt.timedelta(days=30)).isoformat(timespec="seconds"),
         "ev": "bypass_attempt", "sid": "s1", "tool": "Bash", "matched": "old", "denied": True},
        {"v": 2, "t": now_t(10), "ev": "bypass_attempt", "sid": "s1", "tool": "Bash", "matched": "ai/atlas-data/ledger", "denied": True},
        {"v": 2, "t": now_t(5), "ev": "bypass_attempt", "sid": "s1", "tool": "PowerShell", "matched": ".claude/hooks/atlas", "denied": True},
    ])
    p = stats.compute()
    assert p["bypass"]["count"] == 2 and p["bypass"]["blocked"] == 2
    assert [r["tool"] for r in p["bypass"]["recent"]] == ["PowerShell", "Bash"]          # newest first
    assert p["integrity"]["intact"] is True
    f = max(ledger.glob("events-*.jsonl"))
    ls = lines_of(f)
    assert len(ls) == 2
    f.write_text("\n".join(ls[:-1]) + "\n", encoding="utf-8")        # drop the last line: not detectable by a chain
    stats._integrity_cache.clear()
    assert stats.integrity()["intact"] is True
    store.append([{"v": 2, "t": now_t(1), "ev": "x", "sid": "s1"}])
    ls = lines_of(f)
    f.write_text("\n".join(ls[:-2] + ls[-1:]) + "\n", encoding="utf-8")                   # delete a middle line
    stats._integrity_cache.clear()
    assert stats.integrity()["intact"] is False and stats.integrity()["breaks"][0]["file"] == f.name


# ------------------------------------------------------------------ lock-less appends (review round 1)

def test_appends_while_the_lock_is_held_carry_nolock_and_keep_the_chain_intact(ledger, monkeypatch):
    msvcrt = pytest.importorskip("msvcrt")
    monkeypatch.setattr(store, "LOCK_TRIES", 1)                  # do not wait 3 s per append in the test
    ledger.mkdir(parents=True, exist_ok=True)
    holder = open(ledger / ".lock", "a+b")
    holder.seek(0)
    msvcrt.locking(holder.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        for i in range(10):
            store.append([ev("2026-10-07", i, name="p%d" % i)])
    finally:
        holder.seek(0)
        msvcrt.locking(holder.fileno(), msvcrt.LK_UNLCK, 1)
        holder.close()
    objs = [json.loads(x) for x in lines_of(ledger / "events-2026-10-07.jsonl")]
    assert len(objs) == 10 and all(o.get("nolock") is True for o in objs)
    assert store.verify_chain() == {"intact": True, "breaks": [], "files": 1, "lines": 10}


def test_simulated_parallel_writers_that_read_a_stale_tail_leave_no_breaks(ledger, monkeypatch):
    """Four writers that all computed their prev from the same stale tail (the unlocked race) still verify."""
    store.append([ev("2026-10-07", i) for i in range(3)])
    stale = store.prev_for_day("2026-10-07", ledger)
    real = store.prev_for_day
    calls = {"n": 0}

    def lagging(day, base):
        calls["n"] += 1
        return stale if calls["n"] <= 4 else real(day, base)
    monkeypatch.setattr(store, "prev_for_day", lagging)
    store.append_events([ev("2026-10-07", 10 + i, name="w%d" % i) for i in range(4)], ledger, nolock=True)
    store.append([ev("2026-10-07", 20)])
    res = store.verify_chain()
    assert res["intact"] and res["lines"] == 8


def test_locked_appends_do_not_carry_nolock(ledger):
    store.append([ev("2026-10-07", 1)])
    assert "nolock" not in json.loads(lines_of(ledger / "events-2026-10-07.jsonl")[0])


def test_stale_prev_within_the_window_is_accepted_but_deleted_and_edited_lines_still_break(ledger):
    store.append([ev("2026-10-07", i) for i in range(12)])
    f = ledger / "events-2026-10-07.jsonl"
    ls = lines_of(f)
    # a writer that read the file 3 lines early: its prev is the hash of line 8 (index 8) while it lands after line 11
    stale = json.loads(ls[-1])
    stale["prev"] = store.line_hash(ls[8].encode("utf-8"))
    stale["nolock"] = True
    f.write_text("\n".join(ls[:-1] + [json.dumps(stale)]) + "\n", encoding="utf-8")
    assert store.verify_chain()["intact"]
    # a line deleted in the middle: the next line's prev names a line that is gone
    gone = ls[:5] + ls[6:]
    f.write_text("\n".join(gone) + "\n", encoding="utf-8")
    assert store.verify_chain()["breaks"] == [{"file": f.name, "line": 6}]
    # an edited line
    ed = list(ls)
    ed[4] = ed[4].replace('"ev": "x"', '"ev": "z"')
    f.write_text("\n".join(ed) + "\n", encoding="utf-8")
    assert store.verify_chain()["breaks"] == [{"file": f.name, "line": 6}]


def _break_verdicts(ledger):
    from ledger2 import ledger_chain
    r = ledger_chain.build(ledger)
    return r["status"], [b.get("verdict") for b in r["breaks"]]


def _h(line):
    import hashlib
    return hashlib.sha256(line.encode()).hexdigest()


def _chain(n=40):
    prev, out = "genesis", []
    for i in range(n):
        o = {"v": 2, "t": "2026-10-08T09:%02d:00+02:00" % i, "ev": "edit", "sid": "s", "prev": prev}
        line = json.dumps(o)
        prev = _h(line)
        out.append(line)
    return out


def _write(ledger, lines):
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / "events-2026-10-08.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def test_locked_line_after_an_unlocked_mid_write_is_a_visible_break_triaged_accidental(ledger):
    store.append([ev("2026-10-07", i) for i in range(6)])
    f = ledger / "events-2026-10-07.jsonl"
    ls = lines_of(f)
    mid = json.loads(ls[-1])                      # an unlocked mid-write, linked to an older line
    mid["prev"] = store.line_hash(ls[3].encode("utf-8"))
    mid["nolock"] = True
    locked = json.loads(ls[-1])                   # locked, but linked to the line before the unlocked one
    locked["prev"] = store.line_hash(ls[4].encode("utf-8"))
    locked["name"] = "later"
    f.write_text("\n".join(ls[:-1] + [json.dumps(mid), json.dumps(locked)]) + "\n", encoding="utf-8")
    status, verdicts = _break_verdicts(ledger)
    assert status == "broken" and verdicts == ["accidental"]


def test_inserted_forged_lines_break_the_chain(ledger):
    ls = _chain()
    x = {"v": 2, "t": json.loads(ls[19])["t"], "ev": "verdict", "verdict": "READY", "sid": "s", "prev": _h(ls[19]),
         "nolock": True}
    a = list(ls)
    a.insert(20, json.dumps(x))
    _write(ledger, a)
    assert _break_verdicts(ledger)[0] == "broken"             # one forged nolock line
    x2 = dict(x)
    x2.pop("nolock")
    b = list(ls)
    b.insert(20, json.dumps(x2))
    _write(ledger, b)
    assert _break_verdicts(ledger)[0] == "broken"             # one forged locked line
    prev, ins = _h(ls[19]), []
    for j in range(5):
        o = {"v": 2, "t": json.loads(ls[19])["t"], "ev": "verdict", "verdict": "READY", "sid": "s", "prev": prev}
        if j == 0:
            o["nolock"] = True
        line = json.dumps(o)
        prev = _h(line)
        ins.append(line)
    c = list(ls)
    c[20:20] = ins
    _write(ledger, c)
    assert _break_verdicts(ledger)[0] == "broken"             # five chained forged lines, first nolock
    d = list(ls)
    d[20] = json.dumps({"v": 2, "t": "x", "ev": "edit", "sid": "s", "prev": _h(ls[19]), "nolock": True})
    _write(ledger, d)
    assert _break_verdicts(ledger)[0] == "broken"             # a line replaced by a nolock one


def test_triage_mid_file_nolock_before_a_locked_break_is_unclear_not_accidental(ledger):
    from ledger2 import ledger_chain
    ls = _chain()
    x = {"v": 2, "t": json.loads(ls[19])["t"], "ev": "verdict", "verdict": "READY", "sid": "s", "prev": _h(ls[19]),
         "nolock": True}
    ls.insert(20, json.dumps(x))                  # forged nolock line mid-file; line 21 links past it
    _write(ledger, ls)
    r = ledger_chain.build(ledger)
    assert r["status"] == "broken" and [b["verdict"] for b in r["breaks"]] == ["unclear"]
