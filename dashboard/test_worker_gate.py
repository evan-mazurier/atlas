"""
CHARACTERIZATION tests for the Stage-4 dispatch engine (worker.py).

These pin the SERIAL engine's behaviour as it stands *before* the multi-worker refactor.
They are deliberately behavioural, not structural: they assert the state machine's decisions
(which state a job lands in, whether it commits, whether the reviewer is even consulted),
not how the code is arranged. That's what makes them a safety net — the lane refactor may
move every line of run_gate, and these must still pass unchanged.

No claude, no network. The two claude seams (`run_reviewer`, `_gate_invoke`) and the two
slow seams (`run_tests`, `do_commit`) are monkeypatched; the git-touching helpers are
exercised for real against a throwaway repo in tmp_path.
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


# ---------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------

@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    """Point the worker's job store at a temp dir so save()/_log_append() are harmless."""
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr(worker, "JOBS", d)
    return d


def mkjob(**over):
    """A minimal coding job as dispatcher.py would write it, mid-flight (post-coding)."""
    job = {
        "id": "job_test_0001",
        "project": "demo",
        "cwd": r"D:\fake\repo",
        "is_git": True,
        "sandbox": r"D:\fake\worktree",
        "branch": "atlas/job_test_0001",
        "task": "do the thing",
        "status": "coding",
        "model": "sonnet",
        "effort": "medium",
        "test_cmd": "pytest -q",
        "session_id": "sess-abc",
        "rework_count": 0,
        "tokens": 0,
        "contract": {"brief": "b", "acceptance_criteria": ["c1"], "test_cmd": "pytest -q"},
    }
    job.update(over)
    return job


@pytest.fixture
def gate(monkeypatch):
    """Stub every expensive seam run_gate reaches for; record the calls.

    Tests drive the engine by setting `g['tests']` (the run_tests return) and `g['verdicts']`
    (a queue the reviewer pops), then assert on the resulting job state + `g['calls']`.
    """
    g = {
        "tests": (True, "3 passed"),      # (passed, tail) — passed True/False/None
        "verdicts": [],                   # popped left-to-right by run_reviewer
        "rework_ok": True,                # whether the rework session succeeds
        "rework_text": "done",            # the rework session's result text
        "calls": [],
    }

    def fake_run_tests(job):
        g["calls"].append("tests")
        return g["tests"]

    def fake_run_reviewer(job, diff, test_summary):
        g["calls"].append("review")
        return g["verdicts"].pop(0) if g["verdicts"] else None

    def fake_do_commit(job, summary):
        g["calls"].append("commit")
        job["commit"] = "deadbee"
        job["diffstat"] = " f.py | 1 +"

    def fake_gate_invoke(job, label, task, model, effort, resume=None, extra=None, escalation=True):
        g["calls"].append(f"invoke:{label}")
        return g["rework_ok"], g["rework_text"], {"session_id": "sess-next"}

    monkeypatch.setattr(worker, "run_tests", fake_run_tests)
    monkeypatch.setattr(worker, "run_reviewer", fake_run_reviewer)
    monkeypatch.setattr(worker, "do_commit", fake_do_commit)
    monkeypatch.setattr(worker, "_gate_invoke", fake_gate_invoke)
    monkeypatch.setattr(worker, "get_diff", lambda job: "diff --git a/f.py b/f.py")
    monkeypatch.setattr(worker, "diffstat", lambda job: " f.py | 1 +")
    return g


def verdict(v, summary="s", findings=None):
    return {"verdict": v, "summary": summary, "findings": findings or []}


# ---------------------------------------------------------------------------------------
# pure detectors — the session<->worker text protocol
# ---------------------------------------------------------------------------------------

class TestDetectors:
    def test_needs_input_last_occurrence_wins(self):
        txt = "NEEDS_INPUT: first question\nblah\nNEEDS_INPUT: real question"
        assert worker.detect_needs_input(txt) == "real question"

    def test_needs_input_absent(self):
        assert worker.detect_needs_input("all done, no questions") is None
        assert worker.detect_needs_input("") is None
        assert worker.detect_needs_input(None) is None

    def test_contract_parsed_with_all_fields(self):
        txt = ('CONTRACT:\n{"intent": "Build X. Not Y.", "brief": "the brief", '
               '"acceptance_criteria": ["a", "b"], "test_cmd": "pytest -q"}')
        c = worker.detect_contract(txt)
        assert c["intent"] == "Build X. Not Y."
        assert c["brief"] == "the brief"
        assert c["acceptance_criteria"] == ["a", "b"]
        assert c["test_cmd"] == "pytest -q"

    def test_contract_intent_falls_back_to_brief(self):
        c = worker.detect_contract('CONTRACT:\n{"brief": "just a brief", "acceptance_criteria": []}')
        assert c["intent"] == "just a brief"

    def test_contract_requires_a_brief(self):
        assert worker.detect_contract('CONTRACT:\n{"acceptance_criteria": ["a"]}') is None
        assert worker.detect_contract('CONTRACT:\n{"brief": "   "}') is None

    def test_contract_absent_or_malformed(self):
        assert worker.detect_contract("no contract here") is None
        assert worker.detect_contract("CONTRACT:\n{not json}") is None
        assert worker.detect_contract(None) is None

    def test_contract_survives_surrounding_prose(self):
        txt = ('Here is my contract.\n\nCONTRACT:\n{"brief": "b", "acceptance_criteria": ["x"], '
               '"test_cmd": null}\n')
        c = worker.detect_contract(txt)
        assert c and c["brief"] == "b" and c["test_cmd"] is None

    def test_contract_criteria_are_stringified_and_blanks_dropped(self):
        c = worker.detect_contract('CONTRACT:\n{"brief": "b", "acceptance_criteria": ["a", "", "  ", 7]}')
        assert c["acceptance_criteria"] == ["a", "7"]


# ---------------------------------------------------------------------------------------
# run_gate — the state machine. This is the contract the refactor must preserve.
# ---------------------------------------------------------------------------------------

class TestGateHappyPath:
    def test_tests_pass_and_commit_ready_commits_and_goes_ready(self, jobs_dir, gate):
        gate["tests"] = (True, "881 passed")
        gate["verdicts"] = [verdict("commit-ready", "looks good")]
        job = mkjob()
        worker.run_gate(job)
        assert job["status"] == "ready"
        assert job["commit"] == "deadbee"
        assert job["question"] is None
        assert gate["calls"] == ["tests", "review", "commit"]

    def test_review_verdict_and_notes_recorded(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("commit-ready", "clean", ["minor nit"])]
        job = mkjob()
        worker.run_gate(job)
        assert job["review_verdict"] == "commit-ready"
        assert "clean" in job["review_notes"] and "minor nit" in job["review_notes"]

    def test_test_output_is_recorded_and_capped(self, jobs_dir, gate):
        gate["tests"] = (True, "x" * 9000)
        gate["verdicts"] = [verdict("commit-ready")]
        job = mkjob()
        worker.run_gate(job)
        assert len(job["test_output"]) == 4000


class TestGateNoTestCommand:
    """POLICY: with no objective gate, the REVIEWER's verdict commits onto the disposable
    atlas/<id> branch and the job parks at `ready` — the human still gates the MERGE, which is
    the only thing that reaches the base branch.

    This replaces the older 'never auto-commit, route to the operator' rule. That rule withheld the
    COMMIT rather than the MERGE, which left the work as loose worktree state and made `ready`'s
    one-click merge impossible for any project without a test_cmd (orchard-journal, orchard-ops, the
    vaults). Worse, it was a dead end: a merge request ran `git merge` against a branch carrying
    no commit -> "Already up to date" -> exit 0 -> cleanup_job --force deleted the reviewed work.
    """

    def test_no_test_cmd_commit_ready_commits_and_parks_at_ready(self, jobs_dir, gate):
        gate["tests"] = (None, "")
        gate["verdicts"] = [verdict("commit-ready", "fine")]
        job = mkjob(test_cmd=None)
        worker.run_gate(job)
        assert job["status"] == "ready"          # -> the control center's one-click merge button
        assert "commit" in gate["calls"]         # committed onto atlas/<id>, NOT onto the base
        assert not job.get("question")           # no dead-end escalation to answer

    def test_no_test_cmd_ready_is_flagged_as_reviewer_only(self, jobs_dir, gate):
        """The flag is set on the LANE, so it only reaches the control center if mirror_lane carries
        it up — without that the badge silently never renders and a reviewer-only job looks
        identical to a tests-green one."""
        gate["tests"] = (None, "")
        gate["verdicts"] = [verdict("commit-ready", "fine")]
        job = mkjob(test_cmd=None)
        worker.run_gate(job)
        assert job.get("no_test_gate") is True

    def test_no_test_cmd_keeps_the_diffstat_captured_before_the_commit(self, jobs_dir, gate):
        """do_commit captures the diffstat BEFORE committing, because `git diff HEAD` goes empty
        afterwards. Re-capturing it after the commit would report '(no file changes)' on a job
        that changed plenty."""
        gate["tests"] = (None, "")
        gate["verdicts"] = [verdict("commit-ready", "fine")]
        job = mkjob(test_cmd=None)
        worker.run_gate(job)
        assert job["diffstat"] and "no file changes" not in job["diffstat"]

    def test_a_genuine_escalation_still_reaches_the_human(self, jobs_dir, gate):
        """Guard the thing this change must NOT break: `escalate` still parks at needs_input."""
        gate["tests"] = (None, "")
        gate["verdicts"] = [verdict("escalate", "needs a human call")]
        job = mkjob(test_cmd=None)
        worker.run_gate(job)
        assert job["status"] == "needs_input"
        assert job["question"]


class TestGateFailingTests:
    def test_failing_tests_skip_the_reviewer_and_go_straight_to_rework(self, jobs_dir, gate):
        """Cost discipline: a red gate is already decisive — don't pay for a review."""
        gate["tests"] = (False, "1 failed")
        gate["verdicts"] = [verdict("commit-ready")]   # would pass IF consulted
        job = mkjob(rework_count=worker.REWORK_BUDGET)  # at budget → park, one pass only
        worker.run_gate(job)
        assert "review" not in gate["calls"]
        assert job["status"] == "needs_input"

    def test_red_then_green_reworks_once_then_commits(self, jobs_dir, gate, monkeypatch):
        seq = [(False, "1 failed"), (True, "all green")]

        def stepping_tests(job):
            gate["calls"].append("tests")
            return seq.pop(0) if seq else (True, "all green")

        monkeypatch.setattr(worker, "run_tests", stepping_tests)
        gate["verdicts"] = [verdict("commit-ready", "fixed")]
        job = mkjob()
        worker.run_gate(job)
        assert job["rework_count"] == 1
        assert job["status"] == "ready"
        rework = next(c for c in gate["calls"] if "REWORK #1" in c)
        assert gate["calls"].index("commit") > gate["calls"].index(rework)


class TestGateReviewOutcomes:
    def test_escalate_parks_with_the_reviewer_summary(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("escalate", "architectural concern")]
        job = mkjob()
        worker.run_gate(job)
        assert job["status"] == "needs_input"
        assert job["question"] == "architectural concern"
        assert "commit" not in gate["calls"]

    def test_unparseable_verdict_fails_safe_to_escalation(self, jobs_dir, gate):
        gate["verdicts"] = []                    # reviewer returns None
        job = mkjob()
        worker.run_gate(job)
        assert job["status"] == "needs_input"
        assert "no usable verdict" in job["question"]
        assert "commit" not in gate["calls"]

    def test_needs_changes_reworks_then_re_reviews(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("needs-changes", "fix it", ["do x"]),
                            verdict("commit-ready", "now good")]
        job = mkjob()
        worker.run_gate(job)
        assert job["rework_count"] == 1
        assert job["status"] == "ready"
        assert gate["calls"].count("review") == 2

    def test_rework_carries_the_coder_session_forward(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("needs-changes"), verdict("commit-ready")]
        job = mkjob()
        worker.run_gate(job)
        assert job["session_id"] == "sess-next"


class TestGateBudget:
    def test_parks_once_the_rework_budget_is_exhausted(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("needs-changes", f"round {i}") for i in range(10)]
        job = mkjob()
        worker.run_gate(job)
        assert job["rework_count"] == worker.REWORK_BUDGET
        assert job["status"] == "needs_input"
        assert f"{worker.REWORK_BUDGET} rework attempts" in job["question"]
        assert "commit" not in gate["calls"]

    def test_budget_counts_reworks_not_total_attempts(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("needs-changes") for _ in range(10)]
        job = mkjob()
        worker.run_gate(job)
        # REWORK_BUDGET reworks == REWORK_BUDGET + 1 review rounds
        assert gate["calls"].count("review") == worker.REWORK_BUDGET + 1


class TestGateReworkFailure:
    def test_a_dead_rework_session_fails_the_job(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("needs-changes")]
        gate["rework_ok"] = False
        job = mkjob()
        worker.run_gate(job)
        assert job["status"] == "failed"
        assert job["error"] == "rework session failed"

    def test_coder_escalating_during_rework_parks_the_job(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("needs-changes")]
        gate["rework_text"] = "working...\nNEEDS_INPUT: which database should I target?"
        job = mkjob()
        worker.run_gate(job)
        assert job["status"] == "needs_input"
        assert job["question"] == "which database should I target?"


class TestGateNonCodePaths:
    def test_read_only_report_parks_immediately_without_touching_the_gate(self, jobs_dir, gate):
        job = mkjob(read_only=True)
        worker.run_gate(job)
        assert job["status"] == "needs_input"
        assert job["report"] is True
        assert gate["calls"] == []

    def test_non_git_vault_write_parks_for_keep_or_revert(self, jobs_dir, gate, monkeypatch):
        monkeypatch.setattr(worker, "vault_write_diff",
                            lambda job: {"files": ["log.md"], "patch": "-a\n+b"})
        gate["tests"] = (None, "")
        job = mkjob(is_git=False, sandbox=r"D:\fake\backup", test_cmd=None)
        worker.run_gate(job)
        assert job["status"] == "needs_input"
        assert job["review"] is True
        assert job["vault_diff"]["files"] == ["log.md"]
        assert "review" not in gate["calls"]     # no LLM reviewer on the vault path


class TestGatePersistence:
    def test_job_is_saved_at_each_transition(self, jobs_dir, gate):
        gate["verdicts"] = [verdict("commit-ready")]
        job = mkjob()
        worker.run_gate(job)
        rec = json.loads((jobs_dir / "job_test_0001.json").read_text(encoding="utf-8"))
        assert rec["id"] == job["id"]            # the dashboard can read live progress

    def test_intermediate_states_are_visible(self, jobs_dir, gate, monkeypatch):
        seen = []
        real_save = worker.save
        monkeypatch.setattr(worker, "save", lambda j: (seen.append(j["status"]), real_save(j))[1])
        gate["verdicts"] = [verdict("needs-changes"), verdict("commit-ready")]
        worker.run_gate(mkjob())
        assert "testing" in seen and "review" in seen and "reworking" in seen


# ---------------------------------------------------------------------------------------
# git-touching helpers — exercised for real against a throwaway repo
# ---------------------------------------------------------------------------------------

def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git("init", "-b", "main", cwd=r)
    git("config", "user.email", "t@t.t", cwd=r)
    git("config", "user.name", "t", cwd=r)
    (r / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "-A", cwd=r)
    git("commit", "-m", "init", cwd=r)
    return r


@pytest.fixture
def sandboxed(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "WORKTREES", tmp_path / "worktrees")
    monkeypatch.setattr(worker, "BACKUPS", tmp_path / "backups")


class TestSandbox:
    def test_git_job_gets_an_isolated_worktree_on_an_atlas_branch(self, repo, sandboxed, jobs_dir):
        job = mkjob(cwd=str(repo), sandbox=None, branch=None)
        run_dir = worker.prepare_sandbox(job)
        assert Path(run_dir).is_dir()
        assert job["branch"] == "atlas/job_test_0001"
        assert (Path(run_dir) / "a.txt").read_text(encoding="utf-8") == "one\n"
        # the real branch is untouched
        head = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()
        assert head == "main"

    def test_read_only_job_runs_in_place_with_no_sandbox(self, tmp_path, sandboxed, jobs_dir):
        d = tmp_path / "vault"
        d.mkdir()
        job = mkjob(cwd=str(d), is_git=False, sandbox=None, read_only=True)
        assert worker.prepare_sandbox(job) == str(d)
        assert job["sandbox"] is None

    def test_non_git_write_snapshots_a_backup_then_runs_in_place(self, tmp_path, sandboxed, jobs_dir):
        d = tmp_path / "vault"
        d.mkdir()
        (d / "log.md").write_text("hello", encoding="utf-8")
        (d / ".git").mkdir()
        job = mkjob(cwd=str(d), is_git=False, sandbox=None)
        assert worker.prepare_sandbox(job) == str(d)
        bak = Path(job["sandbox"])
        assert (bak / "log.md").read_text(encoding="utf-8") == "hello"
        assert not (bak / ".git").exists()        # ignore patterns applied

    def test_cleanup_removes_the_worktree_and_the_branch(self, repo, sandboxed, jobs_dir):
        job = mkjob(cwd=str(repo), sandbox=None, branch=None)
        wt = worker.prepare_sandbox(job)
        worker.cleanup_job(job)
        assert not Path(wt).exists()
        branches = git("branch", "--list", "atlas/job_test_0001", cwd=repo).stdout.strip()
        assert branches == ""

    def test_cleanup_is_idempotent(self, repo, sandboxed, jobs_dir):
        job = mkjob(cwd=str(repo), sandbox=None, branch=None)
        worker.prepare_sandbox(job)
        worker.cleanup_job(job)
        worker.cleanup_job(job)                   # must not raise


class TestDiffAndCommit:
    def test_untracked_files_appear_in_the_review_diff(self, repo, sandboxed, jobs_dir):
        job = mkjob(cwd=str(repo), sandbox=None, branch=None)
        wt = Path(worker.prepare_sandbox(job))
        (wt / "new.py").write_text("print(1)\n", encoding="utf-8")
        d = worker.get_diff(job)
        assert "new.py" in d                      # intent-to-add surfaced it

    def test_commit_lands_on_the_atlas_branch_and_leaves_main_alone(self, repo, sandboxed, jobs_dir):
        job = mkjob(cwd=str(repo), sandbox=None, branch=None)
        wt = Path(worker.prepare_sandbox(job))
        (wt / "a.txt").write_text("two\n", encoding="utf-8")
        worker.do_commit(job, "summary")
        assert job["commit"]
        assert job["diffstat"] and "a.txt" in job["diffstat"]
        # the dispatched change is NOT on main
        assert (repo / "a.txt").read_text(encoding="utf-8") == "one\n"
        subj = git("log", "-1", "--format=%s", "atlas/job_test_0001", cwd=repo).stdout.strip()
        assert subj.startswith("atlas: do the thing")

    def test_diff_is_capped_for_the_reviewer_prompt(self, repo, sandboxed, jobs_dir, monkeypatch):
        monkeypatch.setattr(worker, "DIFF_CAP", 200)
        job = mkjob(cwd=str(repo), sandbox=None, branch=None)
        wt = Path(worker.prepare_sandbox(job))
        (wt / "big.txt").write_text("line\n" * 500, encoding="utf-8")
        d = worker.get_diff(job)
        assert "truncated" in d
        assert len(d) < 400


class TestRunTests:
    def test_exit_zero_is_a_pass(self, tmp_path, jobs_dir):
        job = mkjob(test_cmd="exit 0", sandbox=str(tmp_path))
        passed, _ = worker.run_tests(job)
        assert passed is True

    def test_nonzero_exit_is_a_fail(self, tmp_path, jobs_dir):
        job = mkjob(test_cmd="exit 3", sandbox=str(tmp_path))
        passed, _ = worker.run_tests(job)
        assert passed is False

    def test_no_test_cmd_returns_none_not_false(self, tmp_path, jobs_dir):
        """None means 'no objective gate' — a DIFFERENT decision from a failing gate."""
        job = mkjob(test_cmd=None, sandbox=str(tmp_path))
        passed, tail = worker.run_tests(job)
        assert passed is None and tail == ""
