"""Integrity break triage and acknowledgement (contracts.md §1 "Hash chain", §5.2 integrity). Synthetic temp ledgers only."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "cockpit"))

from ledger2 import ledger_chain, stats, store  # noqa: E402

T0 = dt.datetime(2026, 10, 9, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def tt(i):
    return (T0 + dt.timedelta(seconds=5 * i)).isoformat(timespec="seconds")


@pytest.fixture
def led(tmp_path, monkeypatch):
    d = tmp_path / "ledger"
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(d))
    stats._integrity_cache.clear()
    stats.clear_cache()
    return d


def make(led, n=25):
    store.append_events([{"ev": "prompt", "sid": "s", "t": tt(i)} for i in range(n)], led)
    return led / "events-2026-10-09.jsonl"


def raw_lines(f):
    return f.read_bytes().split(b"\n")[:-1]


def append_raw(f, obj, newline=True):
    data = (json.dumps(obj) if isinstance(obj, dict) else obj).encode("utf-8") + (b"\n" if newline else b"")
    with open(f, "ab") as fh:
        fh.write(data)


def only_break(led):
    stats._integrity_cache.clear()
    res = stats.integrity()
    assert len(res["breaks"]) == 1, res["breaks"]
    return res, res["breaks"][0]


def test_clean_ledger_is_intact(led):
    make(led)
    res = stats.integrity()
    assert res["intact"] is True and res["status"] == "intact" and res["breaks"] == []


def test_nolock_line_at_end_of_file_is_accidental(led):
    f = make(led)
    stale = ledger_chain.line_hash(raw_lines(f)[3])         # a stale tail far outside the window of 8
    append_raw(f, {"v": 2, "t": tt(40), "ev": "prompt", "sid": "s", "prev": stale, "nolock": True})
    res, b = only_break(led)
    assert b["verdict"] == "accidental"
    assert b["summary"].startswith("Looks accidental: ") and " of 6 signs" in b["summary"]
    assert any("without the lock" in s for s in b["signs"]["accidental"])
    assert b["acknowledged"] is False and res["status"] == "broken" and res["intact"] is False


def test_truncated_last_line_is_accidental(led):
    f = make(led)
    append_raw(f, '{"v": 2, "t": "2026-10-09T11:00:00+02:00", "ev": "pro', newline=False)
    _res, b = only_break(led)
    assert b["verdict"] == "accidental"
    assert any("cut off" in s for s in b["signs"]["accidental"])


def test_middle_line_edited_is_tampering(led):
    f = make(led)
    ls = raw_lines(f)
    ls[9] = ls[9].replace(b'"prompt"', b'"promptX"')
    f.write_bytes(b"\n".join(ls) + b"\n")
    _res, b = only_break(led)
    assert b["line"] == 11
    assert b["verdict"] == "tampering"
    assert b["summary"].startswith("Looks like tampering: ")
    assert any("removed or edited" in s for s in b["signs"]["tampering"])
    assert any("middle of the history" in s for s in b["signs"]["tampering"])


def test_middle_line_deleted_is_tampering(led):
    f = make(led)
    ls = raw_lines(f)
    del ls[9]
    f.write_bytes(b"\n".join(ls) + b"\n")
    _res, b = only_break(led)
    assert b["verdict"] == "tampering"


def test_story_next_to_the_break_is_named(led):
    f = make(led)
    ls = raw_lines(f)
    ls[11] = json.dumps({"v": 2, "t": tt(11), "ev": "bypass_attempt", "sid": "s", "prev": json.loads(ls[11])["prev"]}).encode()
    f.write_bytes(b"\n".join(ls) + b"\n")
    _res, b = only_break(led)
    assert b["verdict"] == "tampering"
    assert any("bypass attempt" in s for s in b["signs"]["tampering"])


def test_ack_changes_status_and_a_new_break_turns_it_red_again(led):
    f = make(led)
    ls = raw_lines(f)
    del ls[9]
    f.write_bytes(b"\n".join(ls) + b"\n")
    _res, b = only_break(led)
    store.append_events([{"ev": "prompt", "cmd": "ack-break", "sid": "s", "t": tt(99)}, {"ev": "integrity_ack", "break": b["id"], "by": "operator", "sid": "s", "t": tt(100)}], led)
    stats._integrity_cache.clear()
    res = stats.integrity()
    assert res["status"] == "intact, 1 acknowledged" and res["intact"] is True
    assert res["breaks"][0]["acknowledged"] is True and res["breaks"][0]["ack_t"] == tt(100)
    # a second, new break
    ls = raw_lines(f)
    del ls[16]
    f.write_bytes(b"\n".join(ls) + b"\n")
    stats._integrity_cache.clear()
    res = stats.integrity()
    assert res["status"] == "broken" and res["intact"] is False
    assert sorted(x["acknowledged"] for x in res["breaks"]) == [False, True]


def test_ack_of_an_unknown_id_acknowledges_nothing(led):
    f = make(led)
    ls = raw_lines(f)
    del ls[9]
    f.write_bytes(b"\n".join(ls) + b"\n")
    store.append_events([{"ev": "prompt", "cmd": "ack-break", "sid": "s"}, {"ev": "integrity_ack", "break": "2026-10-09:99:deadbeef:00000000", "by": "operator", "sid": "s"}], led)
    stats._integrity_cache.clear()
    assert stats.integrity()["status"] == "broken"


def test_verify_chain_keeps_its_shape(led):
    f = make(led)
    ls = raw_lines(f)
    del ls[9]
    f.write_bytes(b"\n".join(ls) + b"\n")
    r = store.verify_chain()
    assert r["intact"] is False and r["breaks"][0]["file"] == f.name and r["breaks"][0]["line"] == 10
    assert len(ledger_chain.scan(led)["breaks"][0]["id"].split(":")) == 4
