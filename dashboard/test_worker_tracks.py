"""
Part 1 of the multi-worker GLOBAL engine: the two-track concurrent drain + the per-JOB stop
signal (docs/agentic-structure.md § "Multi-worker global engine").

These pin the behaviour that makes "ask the planner" instant even mid-dispatch:
  - the LIGHT control-plane track runs concurrently with a HEAVY dispatch (never blocked),
  - heavy dispatch stays SERIAL in Part 1 (HEAVY_CONCURRENCY=1),
  - the light track is capped at MAX_LIGHT,
  - re-draining is idempotent (the in-flight registry),
  - the stop signal is PER-THREAD (a thread-local), so concurrent jobs don't clobber each other.

`process` is faked with a gate so a "running" job can be held in flight while we inspect state —
no claude, no git, no suites. The scheduler is what's under test.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker  # noqa: E402


def wait_until(pred, timeout=5.0, interval=0.01):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return pred()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp jobs dir + freshly reset concurrency globals, torn down after."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(worker, "JOBS", jobs)
    worker._HEAVY_POOL = None
    worker._LIGHT_POOL = None
    worker._INFLIGHT = {}
    worker._INFLIGHT_META = {}
    worker._ROLLOVER = False
    with worker._ACTIVE_LOCK:
        worker._ACTIVE_JIDS.clear()
    yield jobs
    # CRITICAL: wait for pool threads to finish BEFORE monkeypatch restores the real JOBS dir.
    # A thread that outlives this and calls save() would write a stray record into the real queue
    # (`worker.save` reads the module global at call time). The `gated` fixture releases any held
    # gate first (it tears down before this), so wait=True returns promptly.
    for p in (worker._HEAVY_POOL, worker._LIGHT_POOL):
        if p:
            p.shutdown(wait=True)
    worker._HEAVY_POOL = None
    worker._LIGHT_POOL = None
    worker._INFLIGHT = {}
    worker._INFLIGHT_META = {}


def write_job(jobs, jid, kind, project="p", paths=None, status="queued", **extra):
    rec = {"id": jid, "project": project, "cwd": str(jobs), "kind": kind,
           "status": status, "created": jid, "contract_confirmed": True}
    if paths is not None:                       # declare a plan step so job_paths() sees the paths
        rec["contract"] = {"plan": [{"brief": "step", "paths": paths}]}
    rec.update(extra)
    (jobs / f"{jid}.json").write_text(json.dumps(rec), encoding="utf-8")


class _GatedProcess:
    """A fake `process` that signals when a job starts and blocks until the test releases it,
    then marks the job done so it isn't re-drained. Lets a job be held IN FLIGHT on demand."""

    def __init__(self):
        self.started = {}          # jid -> Event (set when the job begins)
        self.gate = {}             # jid -> Event (test sets it to let the job finish)
        self.calls = {}            # jid -> how many times process ran (idempotency check)
        self._lock = threading.Lock()

    def _ev(self, d, jid):
        with self._lock:
            return d.setdefault(jid, threading.Event())

    def __call__(self, job):
        jid = job["id"]
        with self._lock:
            self.calls[jid] = self.calls.get(jid, 0) + 1
        self._ev(self.started, jid).set()
        self._ev(self.gate, jid).wait(timeout=10)
        job["status"] = "done"
        worker.save(job)

    def release(self, jid):
        self._ev(self.gate, jid).set()


@pytest.fixture
def gated(monkeypatch):
    g = _GatedProcess()
    monkeypatch.setattr(worker, "process", g)
    yield g
    # Release every held gate so no fake stays blocked — otherwise a pool thread outlives the
    # fixtures and writes a stray record into the real JOBS dir once monkeypatch is undone.
    for ev in list(g.gate.values()):
        ev.set()


# --------------------------------------------------------------------------- the two tracks

def test_light_runs_concurrently_with_a_heavy_dispatch(env, gated):
    """The daily win: a light control-plane job starts even while a heavy dispatch is in flight."""
    write_job(env, "job_heavy", "exec")
    write_job(env, "job_plan", "planner")
    worker.drain_once()
    assert gated._ev(gated.started, "job_heavy").wait(3), "heavy never started"
    assert gated._ev(gated.started, "job_plan").wait(3), "light was BLOCKED behind the heavy job"
    assert worker._is_inflight("job_heavy") and worker._is_inflight("job_plan")
    gated.release("job_heavy")
    gated.release("job_plan")
    assert wait_until(lambda: not worker._INFLIGHT)


def test_same_project_undeclared_paths_serialize(env, gated):
    """Two heavy jobs on the SAME project that declare no paths have unknown blast radius, so the
    overlap check conservatively serializes them (disjoint=parallel, overlap/unknown=queue)."""
    write_job(env, "job_h1", "exec", project="alpha")     # no paths → conflicts with everything
    write_job(env, "job_h2", "exec", project="alpha")
    worker.drain_once()
    assert gated._ev(gated.started, "job_h1").wait(3)
    assert not gated._ev(gated.started, "job_h2").wait(0.3), "same-project sibling ran despite unknown paths"
    gated.release("job_h1")
    assert wait_until(lambda: not worker._is_inflight("job_h1"))
    worker.drain_once()                       # sibling gone → the second is admitted
    assert gated._ev(gated.started, "job_h2").wait(3)
    gated.release("job_h2")
    assert wait_until(lambda: not worker._INFLIGHT)


def test_different_projects_run_in_parallel(env, gated):
    """Heavy jobs on DIFFERENT projects never conflict → they run concurrently (Part 2)."""
    write_job(env, "job_a", "exec", project="alpha")
    write_job(env, "job_b", "exec", project="beta")
    worker.drain_once()
    assert gated._ev(gated.started, "job_a").wait(3)
    assert gated._ev(gated.started, "job_b").wait(3)
    assert wait_until(lambda: sum(1 for v in worker._INFLIGHT.values() if v == "heavy") == 2)
    for j in ("job_a", "job_b"):
        gated.release(j)
    assert wait_until(lambda: not worker._INFLIGHT)


def test_same_project_disjoint_paths_run_in_parallel(env, gated):
    """Same project, DISJOINT declared paths → parallel (the whole point of the overlap check)."""
    write_job(env, "job_a", "exec", project="alpha", paths=["backend/api/"])
    write_job(env, "job_b", "exec", project="alpha", paths=["frontend/ui/"])
    worker.drain_once()
    assert gated._ev(gated.started, "job_a").wait(3)
    assert gated._ev(gated.started, "job_b").wait(3), "disjoint same-project jobs did NOT parallelize"
    for j in ("job_a", "job_b"):
        gated.release(j)
    assert wait_until(lambda: not worker._INFLIGHT)


def test_same_project_overlapping_paths_serialize(env, gated):
    """Same project, OVERLAPPING declared paths → the second queues behind the first."""
    write_job(env, "job_a", "exec", project="alpha", paths=["backend/api/"])
    write_job(env, "job_b", "exec", project="alpha", paths=["backend/api/users.py"])  # inside job_a
    worker.drain_once()
    assert gated._ev(gated.started, "job_a").wait(3)
    assert not gated._ev(gated.started, "job_b").wait(0.3), "overlapping same-project jobs raced"
    gated.release("job_a")
    assert wait_until(lambda: not worker._is_inflight("job_a"))
    worker.drain_once()
    assert gated._ev(gated.started, "job_b").wait(3)
    gated.release("job_b")
    assert wait_until(lambda: not worker._INFLIGHT)


def test_heavy_dispatch_capped_at_max_jobs(env, gated):
    """No more than MAX_JOBS heavy dispatches run at once, even across disjoint projects."""
    n = worker.MAX_JOBS
    for i in range(n + 1):
        write_job(env, f"job_h{i}", "exec", project=f"proj{i}")   # all different projects
    worker.drain_once()
    for i in range(n):
        assert gated._ev(gated.started, f"job_h{i}").wait(3), f"job_h{i} did not start"
    assert not gated._ev(gated.started, f"job_h{n}").wait(0.3), "exceeded MAX_JOBS"
    assert wait_until(lambda: sum(1 for v in worker._INFLIGHT.values() if v == "heavy") == n)
    for i in range(n + 1):
        gated.release(f"job_h{i}")
    assert wait_until(lambda: not worker._INFLIGHT)


def test_rollover_flag_stops_admission(env, gated):
    """During a hot-reload rollover, drain_once admits nothing new (in-flight is left to finish)."""
    worker._ROLLOVER = True
    write_job(env, "job_h1", "exec", project="alpha")
    worker.drain_once()
    assert not gated._ev(gated.started, "job_h1").wait(0.3), "admitted work while rolling over"
    assert not worker._INFLIGHT
    worker._ROLLOVER = False


def test_light_track_default_is_three(env):
    """planner + architect + vault-keeper can all run at once (the confirmed MAX_LIGHT=3)."""
    assert worker.MAX_LIGHT == 3


def test_all_max_light_run_at_once_and_the_rest_wait(env, gated):
    """Exactly MAX_LIGHT light jobs run concurrently; an extra one queues until a slot frees.
    With the default 3, this is the real scenario: planner+architect+vault-keeper together."""
    n = worker.MAX_LIGHT
    kinds = ["planner", "architect", "vault-keeper"]
    ids = [f"job_l{i}" for i in range(n + 1)]
    for i, jid in enumerate(ids):
        write_job(env, jid, kinds[i % len(kinds)])
    worker.drain_once()
    for jid in ids[:n]:                                   # the first MAX_LIGHT all start...
        assert gated._ev(gated.started, jid).wait(3), f"{jid} did not start"
    assert wait_until(lambda: sum(1 for v in worker._INFLIGHT.values() if v == "light") == n), \
        "not all MAX_LIGHT light jobs ran simultaneously"
    assert not gated._ev(gated.started, ids[n]).wait(0.3), "the (MAX_LIGHT+1)th exceeded the cap"
    gated.release(ids[0])                                 # free one slot...
    assert wait_until(lambda: not worker._is_inflight(ids[0]))
    worker.drain_once()                                  # next tick (the watch loop re-drains)
    assert gated._ev(gated.started, ids[n]).wait(3), "freed light slot not reused"
    for jid in ids:
        gated.release(jid)
    assert wait_until(lambda: not worker._INFLIGHT)


def test_redraining_does_not_double_submit(env, gated):
    """The in-flight registry makes drain_once idempotent — a running job isn't resubmitted."""
    write_job(env, "job_heavy", "exec")
    worker.drain_once()
    assert gated._ev(gated.started, "job_heavy").wait(3)
    worker.drain_once()
    worker.drain_once()
    time.sleep(0.2)
    assert gated.calls["job_heavy"] == 1
    gated.release("job_heavy")
    assert wait_until(lambda: not worker._INFLIGHT)


def test_run_job_rechecks_before_processing(env, gated):
    """A job discarded between submit and run is NOT processed (the stale-race re-check)."""
    write_job(env, "job_x", "exec")
    rec = json.loads((env / "job_x.json").read_text())
    rec["discard"] = True
    (env / "job_x.json").write_text(json.dumps(rec), encoding="utf-8")
    worker._INFLIGHT["job_x"] = "heavy"       # pretend drain admitted it
    worker._run_job("job_x")
    assert "job_x" not in gated.calls          # re-check bailed out
    assert "job_x" not in worker._INFLIGHT     # slot released


def test_discard_skips_an_inflight_job(env):
    """handle_discards must not tear down a job that is currently running."""
    worker._INFLIGHT["job_run"] = "heavy"
    write_job(env, "job_run", "exec")
    rec = json.loads((env / "job_run.json").read_text())
    rec["discard"] = True
    (env / "job_run.json").write_text(json.dumps(rec), encoding="utf-8")
    worker.handle_discards()
    assert (env / "job_run.json").exists(), "in-flight job was discarded mid-run"


# --------------------------------------------------------------------------- per-job stop signal

def test_active_jid_is_thread_local(env):
    """Two jobs on two threads never clobber each other's stop identity."""
    worker._enter_active("j1")
    result = {}

    def other():
        result["before"] = worker._current_jid()        # this thread hasn't entered anything
        worker._enter_active("j2")
        result["after"] = worker._current_jid()
        result["active"] = set(worker._active_jids())    # registry is shared across threads
        worker._exit_active("j2")

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert result["before"] is None
    assert result["after"] == "j2"
    assert result["active"] == {"j1", "j2"}
    assert worker._current_jid() == "j1"                 # unchanged in this thread
    worker._exit_active("j1")
    assert worker._current_jid() is None
    assert worker._active_jids() == []


def test_active_registry_is_refcounted_for_shared_jid(env):
    """A fanned job's lanes share one jid — it stays 'active' until the LAST lane exits."""
    worker._enter_active("jx")               # e.g. the process() thread
    worker._enter_active("jx")               # e.g. a lane thread
    assert worker._active_jids() == ["jx"]
    worker._exit_active("jx")
    assert worker._active_jids() == ["jx"]   # one holder left
    worker._exit_active("jx")
    assert worker._active_jids() == []


def test_stop_check_keys_off_the_current_thread(env):
    """A .stop marker aborts only the thread whose current jid it names."""
    (env / "j1.stop").write_text("", encoding="utf-8")
    worker._enter_active("j1")
    try:
        assert worker._stop_requested(worker._current_jid()) is True
        seen = {}

        def other():
            seen["v"] = worker._stop_requested(worker._current_jid())   # current jid is None here
        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert seen["v"] is False
    finally:
        worker._exit_active("j1")
