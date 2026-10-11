"""Adversarial chain cases from the senior review (A2, B, B2, C, C2, D, E): none may read as accidental or intact.
Synthetic temp ledgers only. Constructions follow the reviewer's script."""
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "cockpit"))

from ledger2 import ledger_chain as lc  # noqa: E402

T0 = dt.datetime(2026, 10, 8, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def H(s):
    return hashlib.sha256(s.encode()).hexdigest()


def mk(days=2, n=40, refusal_at=20):
    prev, files = "genesis", []
    for day in range(days):
        ls = []
        for i in range(n):
            e = {"v": 2, "t": (T0 + dt.timedelta(days=day, seconds=60 * i)).isoformat(timespec="seconds"),
                 "ev": "edit", "sid": "s", "prev": prev}
            if i == refusal_at:
                e.update(ev="refusal", guard="rework", would=False)
            s = json.dumps(e)
            prev = H(s)
            ls.append(s)
        files.append(ls)
    return files


def write(d, files):
    d.mkdir(parents=True, exist_ok=True)
    for day, ls in enumerate(files):
        (d / ("events-%s.jsonl" % (T0 + dt.timedelta(days=day)).date())).write_text("\n".join(ls) + "\n", encoding="utf-8", newline="\n")


def forged(ls, upto, count):
    prev, ins = H(ls[upto]), []
    for _ in range(count):
        s = json.dumps({"v": 2, "t": json.loads(ls[upto])["t"], "ev": "verdict", "verdict": "READY", "sid": "s", "prev": prev})
        prev = H(s)
        ins.append(s)
    return ins


def not_accidental_and_broken(d):
    r = lc.build(d)
    assert r["status"] == "broken" and r["intact"] is False
    assert r["breaks"] and all(b["verdict"] != "accidental" for b in r["breaks"]), [(b["id"], b["verdict"]) for b in r["breaks"]]
    return r


def test_a2_deleted_line_with_nolock_next_is_unclear_not_accidental(tmp_path):
    f = mk()
    ls = f[0]
    del ls[20]
    o = json.loads(ls[20])
    o["nolock"] = True
    ls[20] = json.dumps(o)
    write(tmp_path, f)
    r = not_accidental_and_broken(tmp_path)
    assert r["breaks"][0]["verdict"] == "unclear"


@pytest.mark.parametrize("count", [9, 5])
def test_b_and_b2_inserted_chained_lines_without_nolock_break(tmp_path, count):
    f = mk()
    ls = f[0]
    ls[20:20] = forged(ls, 19, count)
    write(tmp_path, f)
    r = not_accidental_and_broken(tmp_path)
    assert r["breaks"][0]["verdict"] == "unclear"      # links to an older line, no nolock flag explains it


def test_c_truncated_last_line_of_a_day_with_a_later_file_is_unclear(tmp_path):
    f = mk(refusal_at=39)
    f[0][39] = f[0][39][:50]
    write(tmp_path, f)
    r = not_accidental_and_broken(tmp_path)
    assert any(b["verdict"] == "unclear" for b in r["breaks"])


def test_c2_garbled_line_near_the_end_is_not_accidental(tmp_path):
    f = mk(refusal_at=35)
    f[0][35] = f[0][35][:40]
    write(tmp_path, f)
    not_accidental_and_broken(tmp_path)


def test_truncated_last_line_of_the_whole_ledger_is_accidental(tmp_path):
    f = mk(days=1)
    f[0][-1] = f[0][-1][:40]
    write(tmp_path, f)
    r = lc.build(tmp_path)
    assert r["breaks"][0]["verdict"] == "accidental"


def test_truncated_line_continued_by_the_next_line_is_accidental(tmp_path):
    f = mk(days=1)
    ls = f[0]
    garbled = ls[30][:40]
    o = json.loads(ls[31])
    o["prev"] = H(garbled)
    ls[30], ls[31] = garbled, json.dumps(o)
    for k in range(32, len(ls)):                          # re-chain the rest from the new line 31
        o = json.loads(ls[k])
        o["prev"] = H(ls[k - 1])
        ls[k] = json.dumps(o)
    write(tmp_path, f)
    r = lc.build(tmp_path)
    assert [b["verdict"] for b in r["breaks"]] == ["accidental"]


def test_nolock_stale_tail_in_the_window_is_tolerated_without_nolock_it_is_not(tmp_path):
    f = mk(days=1, refusal_at=99)
    ls = f[0]
    o = json.loads(ls[20])
    o["prev"] = H(ls[17])                                # stale by 3 lines
    o["nolock"] = True
    ls[20] = json.dumps(o)
    for k in range(21, len(ls)):
        o = json.loads(ls[k])
        o["prev"] = H(ls[k - 1])
        ls[k] = json.dumps(o)
    write(tmp_path, f)
    assert lc.build(tmp_path)["status"] == "intact"
    o = json.loads(ls[20])
    del o["nolock"]
    ls[20] = json.dumps(o)
    for k in range(21, len(ls)):
        o = json.loads(ls[k])
        o["prev"] = H(ls[k - 1])
        ls[k] = json.dumps(o)
    write(tmp_path, f)
    assert lc.build(tmp_path)["status"] == "broken"


def prompt_ack(prev, bid, **extra):
    p = json.dumps({"v": 2, "t": "2026-10-09T12:00:00+02:00", "ev": "prompt", "cmd": "ack-break", "sid": "op", "prev": prev})
    a = dict({"v": 2, "t": "2026-10-09T12:00:01+02:00", "ev": "integrity_ack", "break": bid, "by": "operator", "sid": "op",
              "prev": H(p)}, **extra)
    return [p, json.dumps(a)]


def test_d_editing_the_predecessor_after_an_ack_turns_it_red_again(tmp_path):
    f = mk()
    ls = f[0]
    o = json.loads(ls[20])
    o["would"] = True
    ls[20] = json.dumps(o)
    write(tmp_path, f)
    bid = lc.build(tmp_path)["breaks"][0]["id"]
    f[1].extend(prompt_ack(H(f[1][-1]), bid))
    write(tmp_path, f)
    assert lc.build(tmp_path)["status"] == "intact, 1 acknowledged"
    o = json.loads(ls[20])
    o["detail"] = "rewritten again"
    ls[20] = json.dumps(o)
    write(tmp_path, f)
    r = lc.build(tmp_path)
    assert r["status"] == "broken" and r["breaks"][0]["acknowledged"] is False


def test_e_forged_acks_do_not_count(tmp_path):
    f = mk()
    ls = f[0]
    o = json.loads(ls[20])
    o["would"] = True
    ls[20] = json.dumps(o)
    write(tmp_path, f)
    bid = lc.build(tmp_path)["breaks"][0]["id"]
    # unchained ack from a helper (itself a break), then chained acks that break the other rules
    f[1].append(json.dumps({"ev": "integrity_ack", "break": bid, "agent_id": "a1", "by": "helper"}))
    write(tmp_path, f)
    assert lc.build(tmp_path)["status"] == "broken"
    f[1].pop()
    last = H(f[1][-1])
    f[1].append(json.dumps({"v": 2, "t": "2026-10-09T12:00:00+02:00", "ev": "integrity_ack", "break": bid, "by": "operator",
                            "sid": "op", "prev": last}))                                  # no preceding ack-break prompt
    write(tmp_path, f)
    assert lc.build(tmp_path)["status"] == "broken"
    f[1].pop()
    for extra in ({"agent_id": "a1"}, {"by": "model"}):
        g = [list(x) for x in f]
        g[1].extend(prompt_ack(last, bid, **extra))
        write(tmp_path, g)
        assert lc.build(tmp_path)["status"] == "broken", extra
    g = [list(x) for x in f]
    g[1].extend(prompt_ack(last, bid))
    write(tmp_path, g)
    assert lc.build(tmp_path)["status"] == "intact, 1 acknowledged"
