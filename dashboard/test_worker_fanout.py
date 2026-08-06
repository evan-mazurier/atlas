"""
Multi-worker dispatch: the deterministic orchestrator (scheduling, conflicts, JOIN).

The properties that matter here are safety properties, and they're asserted as such:
  - two workers never run concurrently against overlapping paths
  - a lane never starts before its dependencies are ready
  - never more than MAX_LANES workers at once
  - a blocked lane stops NEW work but never discards in-flight work
  - lanes that merge cleanly are still re-gated as a whole before the operator sees them

The git-touching JOIN is exercised for real against throwaway repos — merge conflicts and
semantic breakage are exactly the things a mock would paper over.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker  # noqa: E402


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr(worker, "JOBS", d)
    return d


def lane(lid, deps=None, paths=None, state="queued"):
    return {"lane_id": lid, "task": f"do {lid}", "paths": paths or [f"{lid}/**"],
            "depends_on": list(deps or []), "state": state, "tokens": 0,
            "id": "job_f", "cwd": r"D:\repo", "is_git": True}


def mkjob(plan, **over):
    job = {
        "id": "job_f", "project": "demo", "cwd": r"D:\repo", "is_git": True,
        "task": "overall", "model": "sonnet", "effort": "medium", "test_cmd": "pytest -q",
        "tokens": 0, "contract": {"brief": "b", "acceptance_criteria": [], "plan": plan},
        "contract_confirmed": True,
    }
    job.update(over)
    return job


def step(sid, brief=None, paths=None, deps=None):
    return {"id": sid, "brief": brief or f"do {sid}", "paths": paths or [f"{sid}/**"],
            "depends_on": deps or []}


# ---------------------------------------------------------------------------------------
# path conflict analysis
# ---------------------------------------------------------------------------------------

class TestPathPrefix:
    @pytest.mark.parametrize("pat,expected", [
        ("backend/api/**", "backend/api"),
        ("app/src/*.ts", "app/src"),
        ("backend/api/routes.py", "backend/api/routes.py"),
        ("./backend/api/**", "backend/api"),
        ("backend\\api\\**", "backend/api"),
        ("*.py", ""),
        ("**", ""),
        ("src", "src"),
    ])
    def test_prefix_extraction(self, pat, expected):
        assert worker.path_prefix(pat) == expected


class TestPathsConflict:
    def test_disjoint_directories_do_not_conflict(self):
        assert worker.paths_conflict(["backend/**"], ["mobile/**"]) is False

    def test_identical_paths_conflict(self):
        assert worker.paths_conflict(["backend/**"], ["backend/**"]) is True

    def test_nested_paths_conflict(self):
        assert worker.paths_conflict(["backend/**"], ["backend/api/routes.py"]) is True
        assert worker.paths_conflict(["backend/api/routes.py"], ["backend/**"]) is True

    def test_a_repo_wide_glob_conflicts_with_everything(self):
        assert worker.paths_conflict(["**"], ["mobile/**"]) is True

    def test_undeclared_paths_conflict_with_everything(self):
        """Unknown blast radius must be serialized, never raced."""
        assert worker.paths_conflict([], ["mobile/**"]) is True
        assert worker.paths_conflict(["backend/**"], []) is True

    def test_sibling_prefixes_are_not_confused(self):
        """'backend' must not swallow 'backend2' on a plain string prefix check."""
        assert worker.paths_conflict(["backend/**"], ["backend2/**"]) is False

    def test_one_shared_path_among_many_is_enough_to_conflict(self):
        assert worker.paths_conflict(["a/**", "shared/**"], ["b/**", "shared/x.py"]) is True


class TestApplyPathConflicts:
    def test_overlapping_lanes_are_serialized_in_plan_order(self):
        lanes = [lane("s1", paths=["backend/**"]), lane("s2", paths=["backend/api/**"])]
        worker.apply_path_conflicts(lanes)
        assert lanes[1]["depends_on"] == ["s1"]
        assert lanes[1]["serialized_after"] == ["s1"]

    def test_disjoint_lanes_stay_parallel(self):
        lanes = [lane("s1", paths=["a/**"]), lane("s2", paths=["b/**"])]
        worker.apply_path_conflicts(lanes)
        assert lanes[1]["depends_on"] == []

    def test_declared_dependencies_are_preserved(self):
        lanes = [lane("s1", paths=["a/**"]), lane("s2", deps=["s1"], paths=["b/**"])]
        worker.apply_path_conflicts(lanes)
        assert lanes[1]["depends_on"] == ["s1"]

    def test_conflict_edges_never_create_a_cycle(self):
        """Edges only point backwards in plan order — a scheduler deadlock is impossible."""
        lanes = [lane(f"s{i}", paths=["same/**"]) for i in range(5)]
        worker.apply_path_conflicts(lanes)
        graph = [{"id": x["lane_id"], "depends_on": x["depends_on"]} for x in lanes]
        assert worker.plan_cycle(graph) is False


# ---------------------------------------------------------------------------------------
# derive_job_state
# ---------------------------------------------------------------------------------------

class TestDeriveJobState:
    def test_all_ready_is_ready(self):
        assert worker.derive_job_state([lane("a", state="ready"), lane("b", state="ready")]) == "ready"

    def test_needs_input_outranks_everything(self):
        lanes = [lane("a", state="ready"), lane("b", state="needs_input"), lane("c", state="failed")]
        assert worker.derive_job_state(lanes) == "needs_input"

    def test_failed_outranks_active_work(self):
        assert worker.derive_job_state([lane("a", state="failed"),
                                        lane("b", state="coding")]) == "failed"

    def test_active_work_is_reported_over_queued(self):
        assert worker.derive_job_state([lane("a", state="review"),
                                        lane("b", state="queued")]) == "review"


# ---------------------------------------------------------------------------------------
# the scheduler
# ---------------------------------------------------------------------------------------

class Recorder:
    """Stands in for run_lane; records concurrency + ordering, so the scheduler's safety
    properties can be asserted rather than assumed."""

    def __init__(self, outcome=None, hold=0.05):
        self.outcome = outcome or {}          # lane_id -> terminal state (default 'ready')
        self.hold = hold
        self.lock = threading.Lock()
        self.live = 0
        self.peak = 0
        self.order = []

    def __call__(self, job, lane, persist):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.order.append(lane["lane_id"])
            self._live_ids = getattr(self, "_live_ids", set()) | {lane["lane_id"]}
        time.sleep(self.hold)
        with self.lock:
            lane["state"] = self.outcome.get(lane["lane_id"], "ready")
            if lane["state"] == "needs_input":
                lane["question"] = f"{lane['lane_id']} needs a decision"
            self.live -= 1
            self._live_ids = self._live_ids - {lane["lane_id"]}
        persist()

    _live_ids = set()


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(worker, "run_lane", r)
    return r


class TestScheduler:
    def test_disjoint_lanes_run_concurrently(self, jobs_dir, rec):
        job = mkjob([step("s1", paths=["a/**"]), step("s2", paths=["b/**"])])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        assert rec.peak == 2
        assert all(x["state"] == "ready" for x in lanes)

    def test_conflicting_lanes_never_overlap(self, jobs_dir, rec):
        """The core safety property: two workers must never be live on the same paths."""
        job = mkjob([step("s1", paths=["backend/**"]), step("s2", paths=["backend/api/**"])])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        assert rec.peak == 1
        assert rec.order == ["s1", "s2"]

    def test_dependencies_are_respected(self, jobs_dir, rec):
        job = mkjob([step("s1", paths=["a/**"]),
                     step("s2", paths=["b/**"], deps=["s1"]),
                     step("s3", paths=["c/**"], deps=["s2"])])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        assert rec.order == ["s1", "s2", "s3"]
        assert rec.peak == 1

    def test_concurrency_is_capped(self, jobs_dir, rec, monkeypatch):
        monkeypatch.setattr(worker, "MAX_LANES", 2)
        job = mkjob([step(f"s{i}", paths=[f"d{i}/**"]) for i in range(6)])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        assert rec.peak <= 2
        assert all(x["state"] == "ready" for x in lanes)

    def test_a_diamond_runs_its_middle_in_parallel(self, jobs_dir, rec):
        job = mkjob([step("top", paths=["t/**"]),
                     step("l", paths=["l/**"], deps=["top"]),
                     step("r", paths=["r/**"], deps=["top"]),
                     step("bot", paths=["b/**"], deps=["l", "r"])])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        assert rec.order[0] == "top" and rec.order[-1] == "bot"
        assert rec.peak == 2                     # l and r together, nothing else


class TestSchedulerFailure:
    def test_a_blocked_lane_stops_new_work(self, jobs_dir, monkeypatch):
        rec = Recorder(outcome={"s1": "needs_input"})
        monkeypatch.setattr(worker, "run_lane", rec)
        monkeypatch.setattr(worker, "MAX_LANES", 1)
        job = mkjob([step("s1", paths=["a/**"]), step("s2", paths=["b/**"]),
                     step("s3", paths=["c/**"])])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        assert rec.order == ["s1"]               # s2/s3 never launched
        assert worker.derive_job_state(lanes) == "needs_input"

    def test_in_flight_lanes_are_never_discarded_when_a_sibling_blocks(self, jobs_dir, monkeypatch):
        """LOCKED RULE: green lanes keep their work. A sibling parking must not cancel them."""
        rec = Recorder(outcome={"s1": "needs_input"}, hold=0.05)
        monkeypatch.setattr(worker, "run_lane", rec)
        monkeypatch.setattr(worker, "MAX_LANES", 3)
        job = mkjob([step("s1", paths=["a/**"]), step("s2", paths=["b/**"]),
                     step("s3", paths=["c/**"])])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        by = {x["lane_id"]: x for x in lanes}
        assert by["s1"]["state"] == "needs_input"
        assert by["s2"]["state"] == "ready" and by["s3"]["state"] == "ready"

    def test_a_failing_lane_parks_the_session(self, jobs_dir, monkeypatch):
        rec = Recorder(outcome={"s2": "failed"})
        monkeypatch.setattr(worker, "run_lane", rec)
        job = mkjob([step("s1", paths=["a/**"]), step("s2", paths=["b/**"])])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        assert worker.derive_job_state(lanes) == "failed"

    def test_the_blocking_lanes_question_reaches_the_job(self, jobs_dir, monkeypatch):
        rec = Recorder(outcome={"s2": "needs_input"})
        monkeypatch.setattr(worker, "run_lane", rec)
        job = mkjob([step("s1", paths=["a/**"]), step("s2", paths=["b/**"])])
        lanes = worker.load_lanes(job)
        worker.run_lanes(job, lanes)
        assert job["question"] == "s2 needs a decision"
        assert job["needs_input_lane"] == "s2"   # so a resume routes to the right worker

    def test_lane_state_is_persisted_for_the_control_center(self, jobs_dir, rec):
        job = mkjob([step("s1", paths=["a/**"]), step("s2", paths=["b/**"])])
        worker.run_lanes(job, worker.load_lanes(job))
        assert [x["lane_id"] for x in job["lanes"]] == ["s1", "s2"]
        assert all("state" in x for x in job["lanes"])


class TestResumeUnblocks:
    """REGRESSION (live, 2026-07-20). Both lanes escalated; answering s1 ran NOTHING, because
    the 'a parked lane stops new work' rule also stopped the lane that had just been answered.
    Answering was a no-op whenever more than one lane was parked at once."""

    def test_an_answered_lane_runs_even_though_a_sibling_is_still_parked(self, jobs_dir, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(worker, "run_lane", rec)
        job = mkjob([step("s1", paths=["a/**"]), step("s2", paths=["b/**"])])
        lanes = worker.load_lanes(job)
        by = {x["lane_id"]: x for x in lanes}
        by["s1"].update({"state": "queued", "resuming": True})    # the operator just answered s1
        by["s2"]["state"] = "needs_input"                          # still parked
        worker.run_lanes(job, lanes)
        assert rec.order == ["s1"]                                 # it ran
        assert by["s1"]["state"] == "ready"
        assert by["s2"]["state"] == "needs_input"                  # untouched

    def test_a_parked_sibling_still_holds_back_UNanswered_work(self, jobs_dir, monkeypatch):
        rec = Recorder()
        monkeypatch.setattr(worker, "run_lane", rec)
        job = mkjob([step("s1", paths=["a/**"]), step("s2", paths=["b/**"]),
                     step("s3", paths=["c/**"])])
        lanes = worker.load_lanes(job)
        by = {x["lane_id"]: x for x in lanes}
        by["s1"].update({"state": "queued", "resuming": True})
        by["s2"]["state"] = "needs_input"
        worker.run_lanes(job, lanes)
        assert rec.order == ["s1"]                                 # s3 must NOT start
        assert by["s3"]["state"] == "queued"


class TestTokenAccounting:
    """REGRESSION (live, 2026-07-20): a resume that ran nothing still added 2.3M tokens. The
    delta-fold marker lived only in memory, so rehydrated lanes refolded their whole totals."""

    def test_the_total_is_the_base_plus_every_lane(self, jobs_dir):
        job = mkjob([step("s1"), step("s2")], tokens=500)
        lanes = [dict(lane("s1"), tokens=100), dict(lane("s2"), tokens=250)]
        worker.fold_all_lane_tokens(job, lanes)
        assert job["tokens_base"] == 500
        assert job["tokens"] == 850

    def test_refolding_does_not_double_count(self, jobs_dir):
        job = mkjob([step("s1")], tokens=500)
        lanes = [dict(lane("s1"), tokens=100)]
        worker.fold_all_lane_tokens(job, lanes)
        worker.fold_all_lane_tokens(job, lanes)
        worker.fold_all_lane_tokens(job, lanes)
        assert job["tokens"] == 600

    def test_survives_a_rehydration_that_loses_in_memory_state(self, jobs_dir):
        """The exact live failure: lanes come back from the job record with no fold marker."""
        job = mkjob([step("s1"), step("s2")], tokens=500)
        lanes = [dict(lane("s1"), tokens=100), dict(lane("s2"), tokens=250)]
        worker.fold_all_lane_tokens(job, lanes)
        # load_lanes() restores only LANE_PERSIST fields, so any in-memory bookkeeping is gone
        rehydrated = [worker.public_lane(x) for x in lanes]
        assert not any(k.startswith("_") for x in rehydrated for k in x)
        worker.fold_all_lane_tokens(job, rehydrated)
        assert job["tokens"] == 850                                  # not 1200

    def test_further_spending_after_a_resume_still_accrues(self, jobs_dir):
        job = mkjob([step("s1")], tokens=500)
        lanes = [dict(lane("s1"), tokens=100)]
        worker.fold_all_lane_tokens(job, lanes)
        lanes[0]["tokens"] = 175
        worker.fold_all_lane_tokens(job, lanes)
        assert job["tokens"] == 675


class TestDiskReclaim:
    """A fanned job holds one worktree PER LANE. The retention sweep gated on the job's scalar
    `sandbox`, which is only set once the JOIN runs — so a job that died before integrating
    leaked every lane worktree until its record aged out."""

    def test_the_sweep_reclaims_lane_worktrees_with_no_job_level_sandbox(self, fanrepo, jobs_dir,
                                                                         monkeypatch):
        lanes = [make_lane_branch(fanrepo, "s1", {"a.txt": "A\n"}),
                 make_lane_branch(fanrepo, "s2", {"b.txt": "B\n"})]
        job = mkjob([step("s1"), step("s2")], cwd=str(fanrepo),
                    status="failed", sandbox=None, lanes=lanes)   # died before the JOIN
        (worker.JOBS / f"{job['id']}.json").write_text(json.dumps(job), encoding="utf-8")

        worker.sweep_worktrees(keep=0)

        for rec in lanes:
            assert not Path(rec["sandbox"]).exists()
        saved = json.loads((worker.JOBS / f"{job['id']}.json").read_text(encoding="utf-8"))
        assert saved["worktree_cleaned"] is True      # record kept, disk reclaimed

    def test_a_job_with_nothing_on_disk_is_left_alone(self, fanrepo, jobs_dir):
        job = mkjob([], cwd=str(fanrepo), status="failed", sandbox=None, lanes=[])
        (worker.JOBS / f"{job['id']}.json").write_text(json.dumps(job), encoding="utf-8")
        worker.sweep_worktrees(keep=0)
        saved = json.loads((worker.JOBS / f"{job['id']}.json").read_text(encoding="utf-8"))
        assert not saved.get("worktree_cleaned")


class TestFanoutRouting:
    def test_a_single_step_plan_does_not_fan_out(self):
        assert worker.is_fanned(mkjob([step("s1")])) is False

    def test_no_plan_does_not_fan_out(self):
        assert worker.is_fanned(mkjob([])) is False

    def test_a_multi_step_plan_fans_out(self):
        assert worker.is_fanned(mkjob([step("s1"), step("s2")])) is True

    def test_a_cyclic_plan_parks_instead_of_deadlocking(self, jobs_dir, monkeypatch):
        monkeypatch.setattr(worker, "run_lane", lambda *a: pytest.fail("must not schedule"))
        job = mkjob([])
        job["lanes"] = [{"lane_id": "a", "depends_on": ["b"], "state": "queued", "task": "a"},
                        {"lane_id": "b", "depends_on": ["a"], "state": "queued", "task": "b"}]
        worker.process_fanout(job)
        assert job["status"] == "needs_input"
        assert "cycle" in job["question"]

    def test_resume_reopens_only_the_lane_that_asked(self, jobs_dir, monkeypatch):
        seen = {}

        def fake_run_lanes(job, lanes):
            seen.update({x["lane_id"]: dict(x) for x in lanes})
            return lanes

        monkeypatch.setattr(worker, "run_lanes", fake_run_lanes)
        job = mkjob([step("s1"), step("s2")], answer="use postgres", needs_input_lane="s2")
        job["lanes"] = [
            {"lane_id": "s1", "depends_on": [], "state": "ready", "task": "a", "commit": "abc"},
            {"lane_id": "s2", "depends_on": [], "state": "needs_input", "task": "b",
             "question": "which db?", "session_id": "sess-2"},
        ]
        worker.process_fanout(job, resuming=True)
        assert seen["s1"]["state"] == "ready"            # untouched, keeps its commit
        assert seen["s2"]["state"] == "queued"           # reopened
        assert seen["s2"]["resuming"] is True
        assert seen["s2"]["answer"] == "use postgres"
        assert seen["s2"]["session_id"] == "sess-2"      # resumes the SAME worker session


# ---------------------------------------------------------------------------------------
# JOIN — real git
# ---------------------------------------------------------------------------------------

def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def fanrepo(tmp_path, monkeypatch):
    """A repo with a base commit, plus a helper to lay down a committed lane branch."""
    r = tmp_path / "repo"
    r.mkdir()
    git("init", "-b", "main", cwd=r)
    git("config", "user.email", "t@t.t", cwd=r)
    git("config", "user.name", "t", cwd=r)
    (r / "base.txt").write_text("base\n", encoding="utf-8")
    git("add", "-A", cwd=r)
    git("commit", "-m", "init", cwd=r)
    monkeypatch.setattr(worker, "WORKTREES", tmp_path / "wt")
    monkeypatch.setattr(worker, "BACKUPS", tmp_path / "bk")
    return r


def make_lane_branch(repo, lane_id, files, job_id="job_f"):
    """Simulate a lane that coded + committed in its own worktree."""
    wt = worker.WORKTREES / f"{job_id}_{lane_id}"
    worker.WORKTREES.mkdir(exist_ok=True)
    branch = f"atlas/{job_id}-{lane_id}"
    git("worktree", "add", "-b", branch, str(wt), "HEAD", cwd=repo)
    for name, content in files.items():
        p = Path(wt) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    git("add", "-A", cwd=wt)
    git("commit", "-m", f"lane {lane_id}", cwd=wt)
    sha = git("rev-parse", "--short", "HEAD", cwd=wt).stdout.strip()
    return {"lane_id": lane_id, "branch": branch, "commit": sha, "sandbox": str(wt),
            "state": "ready", "depends_on": [], "task": f"do {lane_id}"}


class TestIntegrate:
    def test_disjoint_lanes_integrate_into_one_branch(self, fanrepo, jobs_dir):
        lanes = [make_lane_branch(fanrepo, "s1", {"a.txt": "A\n"}),
                 make_lane_branch(fanrepo, "s2", {"b.txt": "B\n"})]
        job = mkjob([step("s1"), step("s2")], cwd=str(fanrepo), test_cmd="exit 0")
        worker.integrate_lanes(job, lanes)
        assert job["status"] == "ready"
        assert job["branch"] == "atlas/job_f"
        assert job["commit"]
        wt = Path(job["sandbox"])
        assert (wt / "a.txt").read_text(encoding="utf-8") == "A\n"
        assert (wt / "b.txt").read_text(encoding="utf-8") == "B\n"

    def test_the_real_branch_is_untouched_by_the_join(self, fanrepo, jobs_dir):
        lanes = [make_lane_branch(fanrepo, "s1", {"a.txt": "A\n"})]
        job = mkjob([step("s1")], cwd=str(fanrepo), test_cmd="exit 0")
        worker.integrate_lanes(job, lanes)
        assert not (fanrepo / "a.txt").exists()
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=fanrepo).stdout.strip() == "main"

    def test_a_merge_conflict_parks_for_the_operator_and_names_the_file(self, fanrepo, jobs_dir):
        lanes = [make_lane_branch(fanrepo, "s1", {"shared.txt": "from one\n"}),
                 make_lane_branch(fanrepo, "s2", {"shared.txt": "from two\n"})]
        job = mkjob([step("s1"), step("s2")], cwd=str(fanrepo), test_cmd="exit 0")
        worker.integrate_lanes(job, lanes)
        assert job["status"] == "needs_input"
        assert "shared.txt" in job["question"]

    def test_lanes_green_apart_but_red_together_park_for_the_operator(self, fanrepo, jobs_dir):
        """The whole point of the integration re-gate: a clean textual merge can still be
        semantically broken, and only tests on the combined tree catch it."""
        lanes = [make_lane_branch(fanrepo, "s1", {"a.txt": "A\n"}),
                 make_lane_branch(fanrepo, "s2", {"b.txt": "B\n"})]
        job = mkjob([step("s1"), step("s2")], cwd=str(fanrepo), test_cmd="exit 1")
        worker.integrate_lanes(job, lanes)
        assert job["status"] == "needs_input"
        assert "INTEGRATED" in job["question"]
        assert not job.get("commit")             # nothing is presented as mergeable

    def test_a_lane_that_changed_nothing_is_skipped(self, fanrepo, jobs_dir):
        lanes = [make_lane_branch(fanrepo, "s1", {"a.txt": "A\n"}),
                 {"lane_id": "s2", "branch": None, "commit": None, "state": "ready"}]
        job = mkjob([step("s1"), step("s2")], cwd=str(fanrepo), test_cmd="exit 0")
        worker.integrate_lanes(job, lanes)
        assert job["status"] == "ready"

    def test_cleanup_reclaims_every_lane_worktree_and_branch(self, fanrepo, jobs_dir):
        lanes = [make_lane_branch(fanrepo, "s1", {"a.txt": "A\n"}),
                 make_lane_branch(fanrepo, "s2", {"b.txt": "B\n"})]
        job = mkjob([step("s1"), step("s2")], cwd=str(fanrepo), test_cmd="exit 0")
        worker.integrate_lanes(job, lanes)
        job["lanes"] = lanes
        worker.cleanup_job(job)
        for rec in lanes:
            assert not Path(rec["sandbox"]).exists()
        remaining = git("branch", "--list", "atlas/job_f*", cwd=fanrepo).stdout.strip()
        assert remaining == ""
