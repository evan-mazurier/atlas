"""
Part 2 of the multi-worker GLOBAL engine (docs/agentic-structure.md §Multi-worker global engine):
  - the two GLOBAL permits: MAX_WORKERS (claude sessions) + MAX_SUITES (test suites), spanning
    cross-job AND within-job lanes; light jobs never take the claude permit,
  - the cross-session same-project overlap primitives (job_paths / _heavy_conflict),
  - the rebase-on-merge re-gate (regate_job / _flag_siblings_for_regate / handle_merges skip),
    exercised against a REAL git repo.

Only the claude subprocess is stubbed where needed; git and the test gate are real.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker  # noqa: E402


# =========================================================================== the two global permits

class ConcProbe:
    """Stands in for a subprocess run: tracks how many callers are inside AT ONCE (the peak is what
    a permit is supposed to bound), holding the 'work' briefly so contention actually occurs."""

    def __init__(self, hold=0.08):
        self.cur = 0
        self.peak = 0
        self.hold = hold
        self.lock = threading.Lock()

    def __call__(self, *a, **k):
        with self.lock:
            self.cur += 1
            self.peak = max(self.peak, self.cur)
        time.sleep(self.hold)
        with self.lock:
            self.cur -= 1
        return SimpleNamespace(stdout="{}", stderr="", returncode=0)


def _fan(fn, n):
    ts = [threading.Thread(target=fn) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def test_claude_permit_caps_heavy_sessions(monkeypatch):
    """No more than MAX_WORKERS heavy claude sessions run at once, across every caller."""
    monkeypatch.setattr(worker, "CLAUDE_SEM", threading.BoundedSemaphore(2))
    probe = ConcProbe()
    monkeypatch.setattr(worker, "_run_interruptible", probe)
    _fan(lambda: worker.run_claude("task", ".", "skip"), 6)      # heavy=True by default
    assert probe.peak <= 2, f"peak {probe.peak} exceeded the MAX_WORKERS permit"


def test_light_claude_bypasses_the_heavy_permit(monkeypatch):
    """Light jobs pass heavy=False → they must NOT contend for the heavy permit (else the light
    track could be starved by a saturated heavy pool). So they run fully concurrently."""
    monkeypatch.setattr(worker, "CLAUDE_SEM", threading.BoundedSemaphore(2))
    probe = ConcProbe()
    monkeypatch.setattr(worker, "_run_interruptible", probe)
    _fan(lambda: worker.run_claude("task", ".", "skip", heavy=False), 5)
    assert probe.peak == 5, f"light claude was capped at {probe.peak} (should be unbounded by CLAUDE_SEM)"


def test_test_suite_permit_caps_concurrent_suites(monkeypatch, tmp_path):
    """No more than MAX_SUITES test suites run at once — the real local CPU/RAM guardrail."""
    monkeypatch.setattr(worker, "TEST_SEM", threading.BoundedSemaphore(1))
    probe = ConcProbe()
    monkeypatch.setattr(worker, "_run_interruptible", probe)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(worker, "JOBS", jobs)                    # run_tests logs a tail after the run
    job = {"id": "job_t", "test_cmd": "echo hi", "cwd": ".", "sandbox": "."}
    _fan(lambda: worker.run_tests(job), 4)
    assert probe.peak <= 1, f"peak {probe.peak} exceeded the MAX_SUITES permit"


# =========================================================================== overlap primitives

def test_job_paths_unions_the_plan_steps():
    job = {"contract": {"plan": [{"brief": "a", "paths": ["backend/"]},
                                 {"brief": "b", "paths": ["frontend/x.ts"]}]}}
    assert set(worker.job_paths(job)) == {"backend/", "frontend/x.ts"}


def test_job_paths_empty_when_undeclared():
    assert worker.job_paths({"contract": {}}) == []


def test_heavy_conflict_same_project_overlap(monkeypatch):
    monkeypatch.setattr(worker, "_INFLIGHT_META", {"j1": {"project": "p", "paths": ["backend/"]}})
    assert worker._heavy_conflict("p", ["backend/api.py"]) is True        # inside backend/
    assert worker._heavy_conflict("p", ["frontend/"]) is False            # disjoint
    assert worker._heavy_conflict("other", ["backend/"]) is False         # different project
    assert worker._heavy_conflict("p", []) is True                        # unknown blast radius


# =========================================================================== rebase-on-merge re-gate

def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def regate_repo(tmp_path, monkeypatch):
    """A real repo where a SIBLING already moved `main`, and a job branch (atlas/jobB) sits on the
    old base in its own worktree — exactly the rebase-on-merge situation."""
    monkeypatch.setattr(worker, "TEST_SEM", threading.BoundedSemaphore(2))
    jobs = tmp_path / "jobs"                      # run_tests appends a log tail after the suite
    jobs.mkdir()
    monkeypatch.setattr(worker, "JOBS", jobs)
    r = tmp_path / "repo"
    r.mkdir()
    git("init", "-b", "main", cwd=r)
    git("config", "user.email", "t@t.t", cwd=r)
    git("config", "user.name", "t", cwd=r)
    (r / "a.txt").write_text("base\n", encoding="utf-8")
    (r / "b.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A", cwd=r)
    git("commit", "-m", "base", cwd=r)
    # job B's branch, from the base, in its own worktree — it edits b.txt
    wt = tmp_path / "wtB"
    git("worktree", "add", "-b", "atlas/jobB", str(wt), "HEAD", cwd=r)
    (wt / "b.txt").write_text("base\njobB change\n", encoding="utf-8")
    git("add", "-A", cwd=wt)
    git("commit", "-m", "jobB work", cwd=wt)
    return r, wt


def _jobB(repo, wt, test_cmd):
    return {"id": "job_B", "project": "p", "cwd": str(repo), "is_git": True,
            "branch": "atlas/jobB", "sandbox": str(wt), "status": "ready", "test_cmd": test_cmd}


def test_regate_clean_rebase_and_green_stays_ready(regate_repo):
    repo, wt = regate_repo
    # sibling A merged: main moves by editing a DIFFERENT file (a.txt) → clean rebase for jobB
    (repo / "a.txt").write_text("base\nsiblingA change\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "siblingA merged", cwd=repo)
    job = _jobB(repo, wt, 'py -3.12 -c "import sys; sys.exit(0)"')
    worker.regate_job(job)
    assert job["status"] == "ready"           # green against the new base
    assert job.get("regated_at")
    # jobB's branch now contains siblingA's change (it was rebased onto it)
    assert "siblingA change" in (wt / "a.txt").read_text()


def test_regate_tests_fail_after_rebase_parks(regate_repo):
    repo, wt = regate_repo
    (repo / "a.txt").write_text("base\nsiblingA change\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "siblingA merged", cwd=repo)
    job = _jobB(repo, wt, 'py -3.12 -c "import sys; sys.exit(1)"')   # tests fail on the rebased tree
    worker.regate_job(job)
    assert job["status"] == "needs_input"
    assert "FAIL" in (job.get("question") or "")


def test_regate_rebase_conflict_parks(regate_repo):
    repo, wt = regate_repo
    # sibling A edited the SAME file jobB touched (b.txt) → rebase conflict
    (repo / "b.txt").write_text("base\nsiblingA conflicting change\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "siblingA merged", cwd=repo)
    job = _jobB(repo, wt, 'py -3.12 -c "import sys; sys.exit(0)"')
    worker.regate_job(job)
    assert job["status"] == "needs_input"
    assert "rebase" in (job.get("question") or "").lower()


# =========================================================================== flagging + merge guard

def test_flag_siblings_targets_same_project_ready_jobs(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(worker, "JOBS", jobs)

    def w(jid, project, status, **extra):
        rec = {"id": jid, "project": project, "status": status, "is_git": True,
               "branch": f"atlas/{jid}"}
        rec.update(extra)
        (jobs / f"{jid}.json").write_text(json.dumps(rec), encoding="utf-8")

    w("job_merged", "p", "merged")
    w("job_sib_ready", "p", "ready")            # SAME project, ready → should be flagged
    w("job_other_proj", "q", "ready")           # different project → untouched
    w("job_sib_running", "p", "coding")         # not ready → untouched
    worker._flag_siblings_for_regate(worker.load("job_merged"))
    assert worker.load("job_sib_ready").get("regate") is True
    assert not worker.load("job_other_proj").get("regate")
    assert not worker.load("job_sib_running").get("regate")


def test_handle_merges_skips_a_regate_flagged_job(tmp_path, monkeypatch):
    """A job awaiting a re-gate must NOT merge until the re-gate clears its flag."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(worker, "JOBS", jobs)
    called = []
    monkeypatch.setattr(worker, "do_merge", lambda job: called.append(job["id"]))
    rec = {"id": "job_x", "project": "p", "status": "ready", "merge_requested": True,
           "regate": True, "is_git": True, "branch": "atlas/job_x"}
    (jobs / "job_x.json").write_text(json.dumps(rec), encoding="utf-8")
    worker.handle_merges()
    assert called == [], "merged a job that was still awaiting its rebase-on-merge re-gate"
