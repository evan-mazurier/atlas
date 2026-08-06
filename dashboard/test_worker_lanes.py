"""
Multi-worker dispatch: plan parsing + lane construction.

Companion to test_worker_gate.py (which pins the pre-fan-out serial engine). The load-bearing
property here is the DEGRADATION one: a job with no plan — or with a plan the scoper mangled —
must produce exactly one lane that behaves like the old single-worker job. Fan-out is opt-in,
declared, and never inferred.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker  # noqa: E402


def step(sid, brief="do it", paths=None, deps=None):
    return {"id": sid, "brief": brief, "paths": paths or [], "depends_on": deps or []}


# ---------------------------------------------------------------------------------------
# normalize_plan — the scoper's output is untrusted input
# ---------------------------------------------------------------------------------------

class TestNormalizePlan:
    def test_a_well_formed_plan_survives_intact(self):
        raw = [step("s1", "backend", ["api/**"]), step("s2", "mobile", ["app/**"], ["s1"])]
        out = worker.normalize_plan(raw)
        assert [s["id"] for s in out] == ["s1", "s2"]
        assert out[1]["depends_on"] == ["s1"]
        assert out[0]["paths"] == ["api/**"]

    @pytest.mark.parametrize("raw", [None, [], {}, "s1, s2", 7, [None, 3, "x"]])
    def test_unusable_plans_degrade_to_empty(self, raw):
        assert worker.normalize_plan(raw) == []

    def test_steps_without_a_brief_are_dropped(self):
        out = worker.normalize_plan([step("s1"), {"id": "s2", "brief": "   "}, {"id": "s3"}])
        assert [s["id"] for s in out] == ["s1"]

    def test_missing_ids_are_generated_positionally(self):
        out = worker.normalize_plan([{"brief": "a"}, {"brief": "b"}])
        assert [s["id"] for s in out] == ["s1", "s2"]

    def test_duplicate_ids_are_disambiguated(self):
        out = worker.normalize_plan([step("s1", "a"), step("s1", "b")])
        assert len({s["id"] for s in out}) == 2

    def test_dangling_dependencies_are_dropped_not_honoured(self):
        """A scoper typo naming a step that doesn't exist must not deadlock the scheduler."""
        out = worker.normalize_plan([step("s1"), step("s2", deps=["s1", "nope"])])
        assert out[1]["depends_on"] == ["s1"]

    def test_self_dependency_is_dropped(self):
        out = worker.normalize_plan([step("s1", deps=["s1"])])
        assert out[0]["depends_on"] == []

    def test_values_are_coerced_to_strings(self):
        out = worker.normalize_plan([{"id": 1, "brief": "a", "paths": [2], "depends_on": []}])
        assert out[0]["id"] == "1" and out[0]["paths"] == ["2"]

    def test_blank_paths_are_dropped(self):
        out = worker.normalize_plan([step("s1", paths=["a.py", "", "  "])])
        assert out[0]["paths"] == ["a.py"]


class TestPlanCycle:
    def test_acyclic_plan_is_schedulable(self):
        assert worker.plan_cycle([step("s1"), step("s2", deps=["s1"]),
                                  step("s3", deps=["s1", "s2"])]) is False

    def test_direct_cycle_detected(self):
        assert worker.plan_cycle([step("s1", deps=["s2"]), step("s2", deps=["s1"])]) is True

    def test_long_cycle_detected(self):
        assert worker.plan_cycle([step("s1", deps=["s3"]), step("s2", deps=["s1"]),
                                  step("s3", deps=["s2"])]) is True

    def test_empty_plan_is_not_a_cycle(self):
        assert worker.plan_cycle([]) is False

    def test_a_cycle_alongside_schedulable_work_is_still_a_cycle(self):
        assert worker.plan_cycle([step("ok"), step("a", deps=["b"]), step("b", deps=["a"])]) is True


# ---------------------------------------------------------------------------------------
# contract parsing with a plan
# ---------------------------------------------------------------------------------------

class TestContractPlan:
    def test_contract_carries_a_declared_plan(self):
        c = worker.detect_contract(
            'CONTRACT:\n{"brief": "b", "acceptance_criteria": ["x"], "test_cmd": "pytest -q", '
            '"plan": [{"id": "s1", "brief": "one", "paths": ["a/**"], "depends_on": []}, '
            '{"id": "s2", "brief": "two", "paths": ["b/**"], "depends_on": ["s1"]}]}')
        assert [s["id"] for s in c["plan"]] == ["s1", "s2"]

    def test_a_contract_without_a_plan_gets_an_empty_one(self):
        c = worker.detect_contract('CONTRACT:\n{"brief": "b", "acceptance_criteria": []}')
        assert c["plan"] == []

    def test_a_mangled_plan_does_not_break_contract_parsing(self):
        c = worker.detect_contract('CONTRACT:\n{"brief": "b", "plan": "s1 then s2"}')
        assert c is not None and c["brief"] == "b" and c["plan"] == []


# ---------------------------------------------------------------------------------------
# make_lanes — the degradation property
# ---------------------------------------------------------------------------------------

def mkjob(**over):
    job = {
        "id": "job_x", "project": "demo", "cwd": r"D:\repo", "is_git": True,
        "task": "the whole job", "model": "sonnet", "effort": "medium",
        "test_cmd": "pytest -q", "sandbox": r"D:\wt", "branch": "atlas/job_x",
        "session_id": "sess-1", "rework_count": 1, "tokens": 500,
        "contract": {"brief": "b", "acceptance_criteria": ["c1"], "plan": []},
    }
    job.update(over)
    return job


class TestMakeLanesSerialCase:
    def test_no_plan_yields_exactly_one_implicit_lane(self):
        lanes = worker.make_lanes(mkjob())
        assert len(lanes) == 1 and lanes[0]["implicit"] is True

    def test_the_implicit_lane_adopts_the_jobs_own_sandbox_and_session(self):
        """This is what makes the serial path bit-for-bit unchanged: the single lane does not
        get a NEW worktree, it continues in the one the coding session already ran in."""
        lane = worker.make_lanes(mkjob())[0]
        assert lane["sandbox"] == r"D:\wt"
        assert lane["branch"] == "atlas/job_x"
        assert lane["session_id"] == "sess-1"
        assert lane["rework_count"] == 1          # a resumed job keeps its spent budget

    def test_the_implicit_lanes_brief_is_the_whole_task(self):
        assert worker.make_lanes(mkjob())[0]["task"] == "the whole job"

    def test_a_contract_with_no_plan_key_at_all_still_degrades(self):
        job = mkjob(contract={"brief": "b"})
        assert len(worker.make_lanes(job)) == 1

    def test_no_contract_at_all_still_degrades(self):
        job = mkjob(contract=None)
        lanes = worker.make_lanes(job)
        assert len(lanes) == 1 and lanes[0]["task"] == "the whole job"


class TestMakeLanesFanOut:
    def test_a_plan_yields_one_lane_per_step(self):
        job = mkjob(contract={"brief": "b", "plan": [step("s1", "one"), step("s2", "two")]})
        lanes = worker.make_lanes(job)
        assert [x["lane_id"] for x in lanes] == ["s1", "s2"]
        assert [x["task"] for x in lanes] == ["one", "two"]
        assert all(x["implicit"] is False for x in lanes)

    def test_fanned_lanes_start_with_no_sandbox_of_their_own(self):
        """Each lane gets a FRESH worktree from the scheduler — it must not inherit the job's."""
        job = mkjob(contract={"brief": "b", "plan": [step("s1"), step("s2")]})
        for lane in worker.make_lanes(job):
            assert lane["sandbox"] is None
            assert lane["branch"] is None
            assert lane["session_id"] is None
            assert lane["rework_count"] == 0       # each lane gets its own full rework budget

    def test_lanes_inherit_the_gate_configuration(self):
        job = mkjob(contract={"brief": "b", "acceptance_criteria": ["c1"],
                              "plan": [step("s1")]})
        lane = worker.make_lanes(job)[0]
        assert lane["test_cmd"] == "pytest -q"     # same objective gate per lane
        assert lane["is_git"] is True
        assert lane["cwd"] == r"D:\repo"
        assert lane["id"] == "job_x"               # logs + commit messages stay job-scoped
        assert lane["model"] == "sonnet"

    def test_dependencies_are_carried_onto_the_lane(self):
        job = mkjob(contract={"brief": "b", "plan": [step("s1"), step("s2", deps=["s1"])]})
        lanes = worker.make_lanes(job)
        assert lanes[1]["depends_on"] == ["s1"]

    def test_a_single_step_plan_is_still_an_explicit_lane(self):
        job = mkjob(contract={"brief": "b", "plan": [step("only", "just this")]})
        lanes = worker.make_lanes(job)
        assert len(lanes) == 1 and lanes[0]["implicit"] is False
        assert lanes[0]["sandbox"] is None          # explicit → gets its own fresh worktree


# ---------------------------------------------------------------------------------------
# mirror_lane / fold_lane_tokens
# ---------------------------------------------------------------------------------------

class TestLaneAcceptanceCriteria:
    """REGRESSION (first live fanned dispatch, 2026-07-20).

    Both lanes' code was correct and both reviewers escalated anyway: each was handed the
    SESSION's full criteria, so the s1 reviewer was asked to verify pantry.py work against a
    diff that (correctly) only touched briefing.py, and read the contract as self-contradictory.
    A lane's reviewer must be judged against that lane's own definition of done.
    """

    def test_a_step_carries_its_own_criteria(self):
        job = mkjob(contract={"brief": "b", "acceptance_criteria": ["A: x", "B: y"], "plan": [
            dict(step("s1"), acceptance_criteria=["A: x"]),
            dict(step("s2"), acceptance_criteria=["B: y"]),
        ]})
        lanes = worker.make_lanes(job)
        assert lanes[0]["acceptance_criteria"] == ["A: x"]
        assert lanes[1]["acceptance_criteria"] == ["B: y"]

    def test_a_lane_never_inherits_a_siblings_criteria(self):
        job = mkjob(contract={"brief": "b", "acceptance_criteria": ["A: x", "B: y"], "plan": [
            dict(step("s1"), acceptance_criteria=["A: x"]),
            dict(step("s2"), acceptance_criteria=["B: y"]),
        ]})
        for lane in worker.make_lanes(job):
            assert len(lane["acceptance_criteria"]) == 1

    def test_a_step_without_criteria_falls_back_to_the_sessions(self):
        job = mkjob(contract={"brief": "b", "acceptance_criteria": ["A: x"], "plan": [step("s1")]})
        assert worker.make_lanes(job)[0]["acceptance_criteria"] == ["A: x"]

    def test_the_reviewer_is_given_the_lanes_criteria_not_the_jobs(self, monkeypatch):
        seen = {}

        def fake_invoke(lane, label, task, model, effort, **kw):
            seen["prompt"] = task
            return True, 'VERDICT: {"verdict":"commit-ready","summary":"","findings":[]}', {}

        monkeypatch.setattr(worker, "_gate_invoke", fake_invoke)
        job = mkjob(contract={"brief": "b", "acceptance_criteria": ["A: briefing", "B: pantry"],
                              "plan": [dict(step("s1"), acceptance_criteria=["A: briefing"]),
                                       dict(step("s2"), acceptance_criteria=["B: pantry"])]})
        lane = worker.make_lanes(job)[0]
        worker.run_reviewer(lane, "diff", "PASSED")
        assert "A: briefing" in seen["prompt"]
        assert "B: pantry" not in seen["prompt"]        # the sibling's work must not appear

    def test_a_fanned_lane_reviewer_is_told_it_is_one_step_of_a_parallel_plan(self, monkeypatch):
        seen = {}

        def fake_invoke(lane, label, task, model, effort, **kw):
            seen["prompt"] = task
            return True, 'VERDICT: {"verdict":"commit-ready","summary":"","findings":[]}', {}

        monkeypatch.setattr(worker, "_gate_invoke", fake_invoke)
        job = mkjob(contract={"brief": "b", "acceptance_criteria": ["x"],
                              "plan": [step("s1"), step("s2")]})
        worker.run_reviewer(worker.make_lanes(job)[0], "diff", "PASSED")
        assert "IN PARALLEL" in seen["prompt"]
        assert "NOT a finding" in seen["prompt"]

    def test_a_serial_job_reviewer_gets_no_parallel_framing(self, monkeypatch):
        seen = {}

        def fake_invoke(lane, label, task, model, effort, **kw):
            seen["prompt"] = task
            return True, 'VERDICT: {"verdict":"commit-ready","summary":"","findings":[]}', {}

        monkeypatch.setattr(worker, "_gate_invoke", fake_invoke)
        job = mkjob(contract={"brief": "b", "acceptance_criteria": ["x"], "plan": []})
        worker.run_reviewer(worker.make_lanes(job)[0], "diff", "PASSED")
        assert "IN PARALLEL" not in seen["prompt"]


class TestMirrorLane:
    def test_lane_state_becomes_job_status(self):
        job, lane = mkjob(), worker.make_lanes(mkjob())[0]
        lane["state"] = "ready"
        worker.mirror_lane(job, lane)
        assert job["status"] == "ready"

    def test_a_cleared_question_propagates(self):
        """None is meaningful here: a job that parked on a question then passed must stop
        advertising it."""
        job = mkjob(question="an old question")
        lane = worker.make_lanes(job)[0]
        lane["state"], lane["question"] = "ready", None
        worker.mirror_lane(job, lane)
        assert job["question"] is None

    def test_an_unstarted_lane_does_not_wipe_carried_job_state(self):
        job = mkjob(commit="abc1234")
        lane = worker.make_lanes(job)[0]
        lane["commit"] = None
        worker.mirror_lane(job, lane)
        assert job["commit"] == "abc1234"


class TestFoldTokens:
    def test_tokens_fold_up_from_the_lane(self):
        job, lane = mkjob(tokens=100), {"tokens": 30}
        worker.fold_lane_tokens(job, lane)
        assert job["tokens"] == 130

    def test_folding_is_idempotent(self):
        """run_gate persists on every transition — a re-fold must not double-count."""
        job, lane = mkjob(tokens=100), {"tokens": 30}
        worker.fold_lane_tokens(job, lane)
        worker.fold_lane_tokens(job, lane)
        assert job["tokens"] == 130

    def test_only_the_delta_is_added_as_the_lane_spends_more(self):
        job, lane = mkjob(tokens=100), {"tokens": 30}
        worker.fold_lane_tokens(job, lane)
        lane["tokens"] = 75
        worker.fold_lane_tokens(job, lane)
        assert job["tokens"] == 175
