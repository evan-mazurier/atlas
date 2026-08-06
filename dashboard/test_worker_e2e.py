"""
END-TO-END multi-worker dispatch against a real repository.

Everything here is real except the model: real git worktrees, real threads running
concurrently, a real test command executed as a real subprocess, real commits, a real merge,
and the real integration re-gate. Only `_gate_invoke` is stubbed — it stands in for the
claude session by actually writing files into the lane's sandbox, which is what a coding
session does.

This is the test that would catch the things unit tests structurally cannot: two workers
racing on the same worktree, a lane committing to the wrong branch, the join losing a lane's
work, or the engine leaving the operator's real branch modified.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker  # noqa: E402

# A real gate: every .py in the tree must compile. Green at base, and any lane that writes
# a syntactically broken module turns it red — for real, in a subprocess.
GATE = ('py -3.12 -c "import pathlib,py_compile,sys;'
        '[py_compile.compile(str(p),doraise=True) for p in pathlib.Path(\'.\').rglob(\'*.py\')];'
        'sys.exit(0)"')

VERDICT_OK = 'VERDICT: {"verdict": "commit-ready", "summary": "looks good", "findings": []}'


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "app"
    r.mkdir()
    git("init", "-b", "main", cwd=r)
    git("config", "user.email", "t@t.t", cwd=r)
    git("config", "user.name", "t", cwd=r)
    (r / "core.py").write_text("VERSION = 1\n", encoding="utf-8")
    # Real projects ignore their build artifacts, and it matters more than it looks: the worker
    # commits with `git add -A`, so anything the TEST RUN leaves behind gets committed too.
    # Under fan-out that bites harder than it does serially: two lanes on completely
    # disjoint source still write the SAME artifact paths, and the join can then hit a
    # merge conflict that has nothing to do with their actual work. The engine is right
    # not to second-guess what a project tracks, so the requirement lands on the project:
    # anything dispatched with fan-out must gitignore its build artifacts.
    (r / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    git("add", "-A", cwd=r)
    git("commit", "-m", "init", cwd=r)
    monkeypatch.setattr(worker, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(worker, "BACKUPS", tmp_path / "backups")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(worker, "JOBS", jobs)
    return r


def mkjob(repo, plan, jid="job_e2e"):
    return {
        "id": jid, "project": "app", "cwd": str(repo), "is_git": True, "kind": "exec",
        "task": "overall task", "model": "sonnet", "effort": "medium",
        "test_cmd": GATE, "tokens": 0, "status": "queued", "contract_confirmed": True,
        "contract": {"brief": "b", "acceptance_criteria": ["compiles"], "plan": plan},
    }


def step(sid, paths, deps=None, agent=None):
    return {"id": sid, "brief": f"write {sid}", "paths": paths,
            "depends_on": deps or [], "agent": agent}


class Model:
    """Stands in for the claude session. Writes real files into the lane's real worktree."""

    def __init__(self, writes, broken_first=(), escalate=()):
        self.writes = writes                  # lane_id -> {filename: content}
        self.broken_first = set(broken_first)  # lanes that emit broken code on the first pass
        self.escalate = dict(escalate)         # lane_id -> question
        self.calls = []

    def __call__(self, lane, label, task, model, effort, resume=None, extra=None, escalation=True):
        self.calls.append(label)
        if label == "REVIEW":
            return True, VERDICT_OK, {"session_id": "rev"}
        lid = label.split()[0]
        if lid in self.escalate:
            q = self.escalate.pop(lid)
            return True, f"working\nNEEDS_INPUT: {q}", {"session_id": f"sess-{lid}"}
        sandbox = Path(lane["sandbox"])
        for name, content in self.writes.get(lid, {}).items():
            p = sandbox / name
            p.parent.mkdir(parents=True, exist_ok=True)
            if lid in self.broken_first and "REWORK" not in label:
                content = content + "\ndef oops(   # deliberately unparseable\n"
            p.write_text(content, encoding="utf-8")
        return True, "done", {"session_id": f"sess-{lid}"}


@pytest.fixture
def model(monkeypatch):
    def install(m):
        monkeypatch.setattr(worker, "_gate_invoke", m)
        return m
    return install


class TestEndToEnd:
    def test_three_parallel_lanes_land_as_one_mergeable_branch(self, repo, model):
        m = model(Model({
            "s1": {"backend/api.py": "def api():\n    return 1\n"},
            "s2": {"mobile/ui.py": "def ui():\n    return 2\n"},
            "s3": {"docs/notes.py": "NOTES = 3\n"},
        }))
        job = mkjob(repo, [step("s1", ["backend/**"]), step("s2", ["mobile/**"]),
                           step("s3", ["docs/**"])])
        worker.process_fanout(job)

        assert job["status"] == "ready", job.get("question") or job.get("error")
        assert job["branch"] == "atlas/job_e2e"
        assert job["commit"]

        # the session footer must describe the integrated change, not "(no file changes)" —
        # the JOIN's edits arrive as merge COMMITS, so `git diff HEAD` cannot see them
        assert "backend/api.py" in job["diffstat"]
        assert "mobile/ui.py" in job["diffstat"]
        assert "docs/notes.py" in job["diffstat"]
        assert "3 files changed" in job["diffstat"]

        # every lane's work is present on the ONE integrated branch
        wt = Path(job["sandbox"])
        assert (wt / "backend" / "api.py").exists()
        assert (wt / "mobile" / "ui.py").exists()
        assert (wt / "docs" / "notes.py").exists()

        # each lane really was gated on its own
        assert m.calls.count("REVIEW") == 3
        assert all(x["commit"] for x in job["lanes"])
        assert len({x["commit"] for x in job["lanes"]}) == 3   # three distinct commits

        # The operator's real branch never moved
        assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip() == "main"
        assert not (repo / "backend").exists()
        assert git("log", "--oneline", "main", cwd=repo).stdout.strip().count("\n") == 0

    def test_a_lane_that_fails_its_gate_reworks_and_still_joins(self, repo, model):
        """The full real loop: broken code -> real subprocess test fails -> rework -> green."""
        m = model(Model({
            "s1": {"backend/api.py": "def api():\n    return 1\n"},
            "s2": {"mobile/ui.py": "def ui():\n    return 2\n"},
        }, broken_first=["s2"]))
        job = mkjob(repo, [step("s1", ["backend/**"]), step("s2", ["mobile/**"])])
        worker.process_fanout(job)

        assert job["status"] == "ready", job.get("question") or job.get("error")
        lanes = {x["lane_id"]: x for x in job["lanes"]}
        assert lanes["s2"]["rework_count"] == 1
        assert lanes["s1"]["rework_count"] == 0        # a sibling's failure is not contagious
        assert any("s2 REWORK #1" in c for c in m.calls)
        assert (Path(job["sandbox"]) / "mobile" / "ui.py").read_text(encoding="utf-8").endswith("return 2\n")

    def test_overlapping_lanes_are_serialized_and_both_survive(self, repo, model):
        """Two lanes on the same directory must not race — and neither may lose its work."""
        m = model(Model({
            "s1": {"shared/one.py": "ONE = 1\n"},
            "s2": {"shared/two.py": "TWO = 2\n"},
        }))
        job = mkjob(repo, [step("s1", ["shared/**"]), step("s2", ["shared/**"])])
        worker.process_fanout(job)

        assert job["status"] == "ready", job.get("question") or job.get("error")
        lanes = {x["lane_id"]: x for x in job["lanes"]}
        assert lanes["s2"]["serialized_after"] == ["s1"]
        wt = Path(job["sandbox"])
        assert (wt / "shared" / "one.py").exists() and (wt / "shared" / "two.py").exists()

    def test_a_lane_escalating_parks_the_session_and_keeps_the_others(self, repo, model):
        m = model(Model({
            "s1": {"backend/api.py": "def api():\n    return 1\n"},
            "s2": {"mobile/ui.py": "def ui():\n    return 2\n"},
        }, escalate=[("s2", "which database?")]))
        job = mkjob(repo, [step("s1", ["backend/**"]), step("s2", ["mobile/**"])])
        worker.process_fanout(job)

        assert job["status"] == "needs_input"
        assert job["question"] == "which database?"
        assert job["needs_input_lane"] == "s2"
        lanes = {x["lane_id"]: x for x in job["lanes"]}
        assert lanes["s1"]["state"] == "ready" and lanes["s1"]["commit"]   # green work kept
        assert not job.get("commit")                                       # nothing to merge yet

    def test_answering_the_parked_lane_resumes_only_it_and_completes_the_join(self, repo, model):
        m = Model({
            "s1": {"backend/api.py": "def api():\n    return 1\n"},
            "s2": {"mobile/ui.py": "def ui():\n    return 2\n"},
        }, escalate=[("s2", "which database?")])
        model(m)
        job = mkjob(repo, [step("s1", ["backend/**"]), step("s2", ["mobile/**"])])
        worker.process_fanout(job)
        assert job["status"] == "needs_input"
        s1_commit = {x["lane_id"]: x for x in job["lanes"]}["s1"]["commit"]

        job["answer"] = "postgres"
        worker.process_fanout(job, resuming=True)

        assert job["status"] == "ready", job.get("question") or job.get("error")
        lanes = {x["lane_id"]: x for x in job["lanes"]}
        assert lanes["s1"]["commit"] == s1_commit          # untouched, not re-run
        assert m.calls.count("s1 CODE") == 1
        wt = Path(job["sandbox"])
        assert (wt / "backend" / "api.py").exists() and (wt / "mobile" / "ui.py").exists()

    def test_the_job_record_stays_loadable_throughout(self, repo, model):
        """The control center polls this file while lanes run in parallel — concurrent writes must
        never leave it half-written or non-JSON."""
        model(Model({f"s{i}": {f"d{i}/m.py": f"X = {i}\n"} for i in range(1, 4)}))
        job = mkjob(repo, [step(f"s{i}", [f"d{i}/**"]) for i in range(1, 4)])
        worker.process_fanout(job)
        rec = json.loads((worker.JOBS / "job_e2e.json").read_text(encoding="utf-8"))
        assert len(rec["lanes"]) == 3
        assert rec["tokens"] >= 0

    def test_a_scoped_job_can_still_join(self, repo, model):
        """REGRESSION (caught on the first live fanned dispatch, 2026-07-20).

        A job that went through SCOPING already owns worktrees/<id> on branch atlas/<id> —
        process() runs the scoping session there before the contract is confirmed. That is
        exactly the path + branch the JOIN wants for its integration worktree, so
        `git worktree add -b` failed and the job died at `failed` *after* every lane had
        finished its real work. The scoper is read-only, so that checkout is disposable.

        The original tests all built jobs synthetically with no scoping phase, which is why
        they never saw it. This one reproduces the real lifecycle.
        """
        job = mkjob(repo, [step("s1", ["a/**"]), step("s2", ["b/**"])])
        # simulate the scoping phase having already claimed the name
        worker.prepare_sandbox(job)
        assert job["branch"] == "atlas/job_e2e"
        assert Path(job["sandbox"]).is_dir()

        model(Model({"s1": {"a/m.py": "A = 1\n"}, "s2": {"b/m.py": "B = 2\n"}}))
        worker.process_fanout(job)

        assert job["status"] == "ready", job.get("question") or job.get("error")
        assert job["branch"] == "atlas/job_e2e"
        wt = Path(job["sandbox"])
        assert (wt / "a" / "m.py").exists() and (wt / "b" / "m.py").exists()

    def test_cleanup_leaves_the_repo_exactly_as_it_was(self, repo, model):
        before = git("branch", "--list", cwd=repo).stdout
        model(Model({"s1": {"a/m.py": "A = 1\n"}, "s2": {"b/m.py": "B = 2\n"}}))
        job = mkjob(repo, [step("s1", ["a/**"]), step("s2", ["b/**"])])
        worker.process_fanout(job)
        worker.cleanup_job(job)
        assert git("branch", "--list", cwd=repo).stdout == before
        assert git("worktree", "list", cwd=repo).stdout.strip().count("\n") == 0
