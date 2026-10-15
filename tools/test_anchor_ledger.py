"""Tests for tools/anchor_ledger.py, cockpit/ledger2/anchor.py and the integrity anchor check.
Temp dirs only; a temp bare git repo is the pinned "remote" (its path is the pinned url)."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import anchor_ledger as al  # noqa: E402
from ledger2 import anchor, ledger_chain, stats, store  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
NOW = dt.datetime(2026, 10, 10, 23, 55, tzinfo=dt.timezone(dt.timedelta(hours=2)))


def g(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert r.returncode == 0, r.stderr
    return r.stdout


class Env:
    def write_cfg(self, url=None):
        self.cfg_file.write_text(json.dumps({"url": url or str(self.remote), "branch": "main", "clone": str(self.clone)}))

    def cfg(self):
        return anchor.load_config()

    def run(self, **kw):
        kw.setdefault("now", NOW)
        return al.run(self.cfg(), self.led, **kw)

    def check(self):
        """Refresh the remote state synchronously, then the integrity payload."""
        anchor.refresh_remote(NOW)
        stats._integrity_cache.clear()
        return stats.integrity()


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env()
    e.led, e.remote, e.clone = tmp_path / "ledger", tmp_path / "remote.git", tmp_path / "anchor"
    e.cfg_file = tmp_path / "anchor.json"
    e.tmp = tmp_path
    subprocess.run(["git", "init", "--bare", "-b", "main", str(e.remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(e.remote), str(e.clone)], check=True, capture_output=True)
    g(e.clone, "checkout", "-B", "main")
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(e.led))
    monkeypatch.delenv("ATLAS_ANCHOR_CONFIG", raising=False)       # an env override reads as "check disabled" after setup
    monkeypatch.setattr(anchor, "config_path", lambda: e.cfg_file)
    monkeypatch.setattr(anchor, "ALLOW_FILE_PROTOCOL", True)       # the pinned "url" is a local bare repo here
    monkeypatch.setenv("ATLAS_ANCHOR_REPO", str(e.clone))
    monkeypatch.setenv("ATLAS_ANCHOR_DISABLE", "0")
    e.write_cfg()
    anchor.reset_state()
    stats._integrity_cache.clear()
    stats.clear_cache()
    for day, n in (("2026-10-08", 4), ("2026-10-09", 3), ("2026-10-10", 2)):
        store.append_events([{"ev": "prompt", "sid": "s", "t": f"{day}T10:00:{i:02d}+02:00"} for i in range(n)], e.led)
    (e.clone / "README.md").write_text("anchors\n")
    g(e.clone, "add", "README.md")
    g(e.clone, "commit", "-m", "init")
    g(e.clone, "push", "-u", "origin", "main")
    yield e
    anchor.reset_state()


def anchors(env):
    return [json.loads(x) for x in (env.clone / "anchors.jsonl").read_text().splitlines()]


def last_hash(f):
    return ledger_chain.line_hash([x for x in f.read_bytes().split(b"\n") if x.strip()][-1])


def rechain(env, edit_day="2026-10-08"):
    """A clean re-chain of the whole ledger from an edit in a day: the chain itself sees no break."""
    prev = ledger_chain.GENESIS
    for f in ledger_chain.day_files(env.led):
        objs = [json.loads(x) for x in f.read_bytes().split(b"\n") if x.strip()]
        if f.name == f"events-{edit_day}.jsonl":
            objs[-1]["sid"] = "forged"
        out = []
        for o in objs:
            o["prev"] = prev
            raw = json.dumps(o, separators=(",", ":")).encode()
            prev = ledger_chain.line_hash(raw)
            out.append(raw)
        f.write_bytes(b"\n".join(out) + b"\n")


# ------------------------------------------------------------------ the tool

def test_first_run_anchors_every_day_commits_pushes_and_records_configured(env):
    assert not anchor.configured_event(env.led)
    assert env.run() == 0
    a = anchors(env)
    assert [(x["day"], x["lines"]) for x in a] == [("2026-10-08", 4), ("2026-10-09", 3), ("2026-10-10", 2)]
    assert a[0]["last_hash"] == last_hash(env.led / "events-2026-10-08.jsonl")
    assert "head" not in a[0] and a[2]["head"] is True
    assert g(env.clone, "log", "-1", "--format=%s").strip() == "anchor 2026-10-10"
    assert g(env.remote, "log", "-1", "--format=%s", "main").strip() == "anchor 2026-10-10"
    assert anchor.configured_event(env.led)
    assert anchor.last_push_ok_at(env.clone) == NOW.isoformat(timespec="seconds")
    assert ledger_chain.build(env.led)["intact"]                # the event is chained like any other line


def test_configured_event_written_once(env):
    env.run()
    store.append_events([{"ev": "prompt", "sid": "s", "t": "2026-10-10T12:00:00+02:00"}], env.led)
    env.run()
    n = sum(b'"anchor_configured"' in ln for f in env.led.glob("events-*.jsonl") for ln in f.read_bytes().split(b"\n"))
    assert n == 1


def test_append_only_second_run_adds_only_new_entries(env):
    env.run()
    before = (env.clone / "anchors.jsonl").read_bytes()
    store.append_events([{"ev": "prompt", "sid": "s", "t": "2026-10-10T11:00:00+02:00"}], env.led)
    assert env.run() == 0
    after = (env.clone / "anchors.jsonl").read_bytes()
    assert after.startswith(before) and after != before
    assert env.run() == 0                                        # the event line is new: one more head entry, then stable
    again = (env.clone / "anchors.jsonl").read_bytes()
    assert env.run() == 0
    assert (env.clone / "anchors.jsonl").read_bytes() == again


def test_mismatch_exits_nonzero_and_never_rewrites(env, capsys):
    env.run()
    before = (env.clone / "anchors.jsonl").read_bytes()
    f = env.led / "events-2026-10-08.jsonl"
    lines = f.read_bytes().split(b"\n")
    lines[3] = lines[3].replace(b'"prompt"', b'"promp2"')
    f.write_bytes(b"\n".join(lines))
    assert env.run() == 2
    assert "ANCHOR MISMATCH" in capsys.readouterr().err
    assert (env.clone / "anchors.jsonl").read_bytes() == before


def test_tool_refuses_when_the_remote_history_was_rewritten(env, capsys):
    env.run()
    force_rewrite(env)
    assert env.run() == 2
    assert "rewritten" in capsys.readouterr().err


def test_dry_run_writes_nothing(env, capsys):
    assert env.run(dry_run=True) == 0
    assert "would append" in capsys.readouterr().out
    assert not (env.clone / "anchors.jsonl").exists()
    assert g(env.clone, "status", "--porcelain").strip() == ""
    assert not anchor.configured_event(env.led)


def test_push_failure_exits_3_keeps_commit_and_next_run_pushes_it(env):
    env.write_cfg(url=str(env.tmp / "gone.git"))
    assert env.run() == 3
    assert g(env.clone, "log", "-1", "--format=%s").strip() == "anchor 2026-10-10"
    assert not anchor.configured_event(env.led)                  # no successful push yet
    env.write_cfg()
    assert env.run() == 0                                        # nothing new, but the pending commit goes out
    assert g(env.remote, "log", "-1", "--format=%s", "main").strip() == "anchor 2026-10-10"
    assert anchor.configured_event(env.led)


def test_leftover_uncommitted_anchor_file_is_committed(env):
    assert env.run(push=False) == 0
    g(env.clone, "reset", "--soft", "HEAD~1")                    # as if the commit step had failed after the append
    g(env.clone, "reset", "-q")
    assert g(env.clone, "status", "--porcelain", "--", "anchors.jsonl").strip()
    assert env.run() == 0
    assert g(env.clone, "status", "--porcelain", "--", "anchors.jsonl").strip() == ""
    assert g(env.remote, "log", "-1", "--format=%s", "main").strip() == "anchor 2026-10-10"


def test_setup_errors_exit_1(env, tmp_path, monkeypatch):
    cfg = env.cfg()
    cfg["clone"] = tmp_path / "nope"
    assert al.run(cfg, env.led, now=NOW) == 1
    cfg["clone"] = env.clone

    def no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(anchor, "run_git", no_git)
    assert al.run(cfg, env.led, now=NOW) == 1


def test_git_never_prompts_and_ignores_outside_config(monkeypatch):
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o BatchMode=no -v")        # a pre-set command is overridden, not kept
    e = anchor.git_env()
    assert e["GIT_TERMINAL_PROMPT"] == "0" and e["GCM_INTERACTIVE"] == "never"
    assert e["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert e["GIT_CONFIG_NOSYSTEM"] == "1" and e["GIT_CONFIG_GLOBAL"] == os.devnull
    p = anchor.git_prefix()
    assert "protocol.allow=never" in p and "protocol.ssh.allow=always" in p and "core.autocrlf=false" in p


def test_global_url_rewrite_cannot_redirect_the_remote_check(env, tmp_path, monkeypatch):
    env.run()
    cfg = tmp_path / "gitconfig"
    cfg.write_text(f'[url "{tmp_path / "evil.git"}"]\n\tinsteadOf = {env.remote}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))                       # ignored: git_env() overrides it
    assert env.check()["anchor"]["remote"] == "ok"


def test_url_rewrite_in_the_clone_config_is_tampering_and_stops_the_tool(env, capsys):
    env.run()
    g(env.clone, "config", f"url.{env.tmp / 'evil.git'}.insteadOf", str(env.remote))
    s = env.check()
    assert s["status"] == "broken"
    assert any("rewrites URLs" in m["summary"] for m in s["anchor"]["mismatches"])
    assert env.run() == 2
    assert "url.*" in capsys.readouterr().err


def test_merge_commit_in_remote_history_is_tampering(env):
    env.run()
    other = env.tmp / "attacker2"
    subprocess.run(["git", "clone", str(env.remote), str(other)], check=True, capture_output=True)
    g(other, "checkout", "-B", "main")
    g(other, "checkout", "-b", "side")
    (other / "x.txt").write_text("x")
    g(other, "add", "x.txt")
    g(other, "commit", "-m", "side")
    g(other, "checkout", "main")
    (other / "y.txt").write_text("y")
    g(other, "add", "y.txt")
    g(other, "commit", "-m", "main")
    g(other, "merge", "--no-ff", "-m", "merge", "side")
    g(other, "push", "origin", "main")
    s = env.check()
    assert s["status"] == "broken"
    assert any("was rewritten" in m["summary"] for m in s["anchor"]["mismatches"])


def test_crlf_checkout_is_not_an_edit(env):
    env.run()
    assert (env.clone / ".gitattributes").read_text().splitlines() == ["anchors.jsonl -text"]
    again = env.tmp / "clone-crlf"
    subprocess.run(["git", "clone", "-c", "core.autocrlf=true", str(env.remote), str(again)], check=True, capture_output=True)
    assert b"\r\n" not in (again / "anchors.jsonl").read_bytes()
    os.environ["ATLAS_ANCHOR_REPO"] = str(again)
    s = env.check()
    assert s["status"] == "intact" and s["anchor"]["mismatches"] == [] and s["anchor"]["remote"] == "ok"
    raw = (again / "anchors.jsonl").read_bytes()                          # even a CRLF working file (older clone)
    (again / "anchors.jsonl").write_bytes(raw.replace(b"\n", b"\r\n"))
    assert env.check()["anchor"]["mismatches"] == []


def test_failed_background_check_always_clears_running(env, monkeypatch):
    def boom(*a, **k):
        raise KeyError("unexpected")
    monkeypatch.setattr(anchor, "fetch_and_analyse", boom)
    anchor._state["running"] = True
    with pytest.raises(KeyError):
        anchor.refresh_remote(NOW)
    assert anchor._state["running"] is False and anchor._state["gen"] == 1


def test_git_timeout_exits_3(env, monkeypatch):
    real = anchor.run_git

    def slow(cwd, *args, **kw):
        if "push" in args:
            raise subprocess.TimeoutExpired("git push", 60)
        return real(cwd, *args, **kw)
    monkeypatch.setattr(anchor, "run_git", slow)
    assert env.run() == 3


def test_unreachable_since_survives_a_restart_of_the_check(env):
    env.run()
    env.check()                                                            # reachable: records the time
    env.remote.rename(env.tmp / "moved.git")
    a = env.check()["anchor"]
    anchor.reset_state()                                                   # as after a cockpit restart
    b = env.check()["anchor"]
    assert a["remote"] == b["remote"] == "unreachable"
    assert a["unreachable_since"] == b["unreachable_since"] and a["unreachable_since"]


def test_check_disabled_after_setup_is_reported(env, monkeypatch):
    env.run()
    monkeypatch.setenv("ATLAS_ANCHOR_DISABLE", "1")
    a = env.check()["anchor"]
    assert a["check_disabled"] is True and a["configured"] is True and a["mismatches"] == []
    monkeypatch.setenv("ATLAS_ANCHOR_DISABLE", "0")
    assert env.check()["anchor"]["check_disabled"] is False
    monkeypatch.setenv("ATLAS_ANCHOR_CONFIG", str(env.cfg_file))
    assert env.check()["anchor"]["check_disabled"] is True


def test_partial_tail_is_not_anchored_and_completing_it_stays_intact(env):
    f = env.led / "events-2026-10-10.jsonl"
    full = f.read_bytes()
    obj = json.loads(full.split(b"\n")[-2])
    obj2 = dict(obj, t="2026-10-10T10:05:00+02:00", prev=ledger_chain.line_hash(full.split(b"\n")[-2]))
    raw = json.dumps(obj2, separators=(",", ":")).encode()
    f.write_bytes(full + raw)                                    # a write in progress: no newline yet
    assert env.run() == 0
    assert [(x["day"], x["lines"]) for x in anchors(env)][-1] == ("2026-10-10", 2)
    f.write_bytes(full + raw + b"\n")                            # completed later
    s = env.check()
    assert s["status"] == "intact" and s["anchor"]["mismatches"] == []
    assert env.run() == 0
    assert [(x["day"], x["lines"]) for x in anchors(env)][-1] == ("2026-10-10", 3)


# ------------------------------------------------------------------ the integrity check

def test_not_set_up_without_config_or_anchors(env):
    env.cfg_file.unlink()
    s = env.check()
    assert s["anchor"]["configured"] is False and s["status"] == "intact"
    env.write_cfg()
    s = env.check()                                              # config but nothing anchored yet
    assert s["anchor"]["configured"] is False and s["anchor"]["mismatches"] == [] and s["status"] == "intact"
    assert ledger_chain.build(env.led)["anchor"]["configured"] is False


def test_all_match_after_anchoring_with_remote_times(env):
    env.run()
    a = env.check()["anchor"]
    assert a["configured"] and a["remote"] == "ok" and a["mismatches"] == []
    assert a["last_anchored_day"] == "2026-10-10"
    assert a["last_remote_commit_at"] and a["last_remote_commit_at"] != a["last_anchored_day"]
    assert a["last_remote_commit_at"] == g(env.remote, "log", "-1", "--format=%cI", "main").strip()
    assert a["last_push_ok_at"] == NOW.isoformat(timespec="seconds")


def test_rechained_ledger_is_caught_by_the_remote_anchors(env):
    env.run()
    assert env.check()["status"] == "intact"
    rechain(env)
    s = env.check()
    assert s["breaks"] == [] and s["status"] == "broken" and s["intact"] is False
    m = s["anchor"]["mismatches"]
    assert [x["day"] for x in m] == ["2026-10-08", "2026-10-09", "2026-10-10"]
    assert all(x["verdict"] == "tampering" for x in m)
    assert m[0]["summary"] == ("Looks like tampering: the record of 2026-10-08 no longer matches its outside anchor of "
                               "2026-10-10.")


def test_rechain_plus_rewriting_the_local_anchor_file_is_still_caught(env):
    env.run()
    rechain(env)
    new = [json.dumps({"day": x["day"], "lines": x["lines"], "last_hash": last_hash(env.led / f"events-{x['day']}.jsonl"),
                       "anchored_at": x["anchored_at"]}) for x in anchors(env)]
    (env.clone / "anchors.jsonl").write_text("\n".join(new) + "\n")        # attacker also forges the local clone
    s = env.check()
    assert s["status"] == "broken"
    assert any("differs from" in m["summary"] for m in s["anchor"]["mismatches"])


def force_rewrite(env):
    """Rewrite the remote history so an earlier version of anchors.jsonl is no prefix of the later one."""
    other = env.tmp / "attacker"
    subprocess.run(["git", "clone", str(env.remote), str(other)], check=True, capture_output=True)
    g(other, "checkout", "-B", "main")
    raw = (other / "anchors.jsonl").read_bytes()
    (other / "anchors.jsonl").write_bytes(raw.replace(b'"lines":4', b'"lines":5'))
    g(other, "add", "anchors.jsonl")
    g(other, "commit", "-m", "tidy")
    g(other, "push", "origin", "main")
    (other / "anchors.jsonl").write_bytes(raw)                    # the history now has a non-append change
    g(other, "add", "anchors.jsonl")
    g(other, "commit", "-m", "tidy 2")
    g(other, "push", "origin", "main")


def test_rewritten_remote_history_is_tampering_even_if_it_looks_consistent_now(env):
    env.run()
    force_rewrite(env)
    s = env.check()
    assert s["status"] == "broken"
    assert any(m["summary"] == "Looks like tampering: the outside anchor file was rewritten." for m in s["anchor"]["mismatches"])


def test_local_file_extended_by_unpushed_appends_is_fine(env):
    env.run()
    with open(env.clone / "anchors.jsonl", "ab") as fh:
        fh.write(b'{"day":"2026-10-10","lines":3,"last_hash":"%s","anchored_at":"2026-10-10T23:56:00+02:00"}\n'
                 % last_hash(env.led / "events-2026-10-10.jsonl").encode())
    assert env.check()["anchor"]["mismatches"] == []


def test_remote_unreachable_is_amber_never_a_silent_local_fallback(env):
    env.run()
    env.remote.rename(env.tmp / "moved.git")
    s = env.check()
    a = s["anchor"]
    assert a["remote"] == "unreachable" and a["unreachable_since"] and a["last_remote_commit_at"] is None
    assert s["status"] == "intact"                                # not an alarm by itself
    first = a["unreachable_since"]
    assert env.check()["anchor"]["unreachable_since"] == first    # "since" stays at the first failure


def test_configured_only_grows_missing_pieces_are_broken(env):
    env.run()
    assert env.check()["status"] == "intact"
    (env.clone / "anchors.jsonl").unlink()
    s = env.check()
    assert s["status"] == "broken" and s["anchor"]["missing"] is True
    assert any("Outside anchor missing" in m["summary"] for m in s["anchor"]["mismatches"])
    g(env.clone, "checkout", "--", "anchors.jsonl")
    (env.clone / "anchors.jsonl").write_bytes(b"")                 # emptied
    assert env.check()["status"] == "broken"
    shutil.rmtree(env.clone, ignore_errors=True)                   # the clone is gone
    assert env.check()["anchor"]["missing"] is True
    env.cfg_file.unlink()                                          # the config is gone
    s = env.check()
    assert s["status"] == "broken" and s["anchor"]["missing"] is True


@pytest.mark.parametrize("line", [
    b"not json",
    b'["a"]',
    b'{"day":"2026-10-08","lines":true,"last_hash":"%s","anchored_at":"2026-10-10T23:55:00+02:00"}' % (b"a" * 64),
    b'{"day":"2026-10-08","lines":0,"last_hash":"%s","anchored_at":"2026-10-10T23:55:00+02:00"}' % (b"a" * 64),
    b'{"day":"2026-10-08","lines":"4","last_hash":"%s","anchored_at":"2026-10-10T23:55:00+02:00"}' % (b"a" * 64),
    b'{"day":"2026-13-08","lines":4,"last_hash":"%s","anchored_at":"2026-10-10T23:55:00+02:00"}' % (b"a" * 64),
    b'{"day":"2026-10-08","lines":4,"last_hash":"abc","anchored_at":"2026-10-10T23:55:00+02:00"}',
    b'{"day":"2026-10-08","lines":4,"last_hash":"%s","anchored_at":"yesterday"}' % (b"a" * 64),
])
def test_unreadable_or_ill_typed_entries_are_a_mismatch_never_dropped(env, line):
    env.run()
    with open(env.clone / "anchors.jsonl", "ab") as fh:
        fh.write(line + b"\n")
    s = env.check()
    assert s["status"] == "broken"
    assert any(m["summary"] == "Looks like tampering: an anchor entry is unreadable." for m in s["anchor"]["mismatches"])


def test_unterminated_last_anchor_line_is_unreadable(env):
    env.run()
    with open(env.clone / "anchors.jsonl", "ab") as fh:
        fh.write(b'{"day":"2026-10-10"')
    assert any("unreadable" in m["summary"] for m in env.check()["anchor"]["mismatches"])


# ------------------------------------------------------------------ background fetch

def test_fetch_runs_in_the_background_and_is_cached(env, monkeypatch):
    env.run()
    gate, calls = threading.Event(), []

    def slow(now=None):
        calls.append(1)
        gate.wait(5)
        with anchor._lock:
            anchor._state.update({"gen": anchor._state["gen"] + 1, "checked_at": dt.datetime.now().astimezone(),
                                  "running": False})
    monkeypatch.setattr(anchor, "refresh_remote", slow)
    t0 = time.time()
    anchor.ensure_fresh()
    anchor.ensure_fresh()                                         # a second call while one runs spawns nothing
    stats.integrity()                                             # and a request never waits for the fetch
    assert time.time() - t0 < 2
    gate.set()
    for _ in range(100):
        if not anchor._state["running"]:
            break
        time.sleep(0.02)
    assert len(calls) == 1
    anchor.ensure_fresh()                                         # inside the TTL: no new fetch
    time.sleep(0.1)
    assert len(calls) == 1
    # past the TTL: one more. Measured from the real clock, like checked_at above (a fixed date stops working once it is past)
    anchor.ensure_fresh(dt.datetime.now().astimezone() + dt.timedelta(seconds=anchor.TTL_SECONDS + 60))
    for _ in range(100):
        if len(calls) == 2:
            break
        time.sleep(0.02)
    assert len(calls) == 2
