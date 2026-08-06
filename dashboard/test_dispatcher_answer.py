"""
answer_job — the escalation/resume gate.

REGRESSION (first live fanned dispatch, 2026-07-20): a fanned job has no session of its own
— each LANE owns one — so the `session_id` guard rejected every answer with "has no
session_id to resume", making a parked fan-out unanswerable from the UI. The answer routes
to `needs_input_lane`, so that lane's session is what must be validated.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dispatcher  # noqa: E402


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr(dispatcher, "JOBS_DIR", d)
    return d


def write(jobs_dir, **over):
    job = {"id": "job_a", "project": "demo", "status": "needs_input", "task": "t"}
    job.update(over)
    (jobs_dir / "job_a.json").write_text(json.dumps(job), encoding="utf-8")
    return job


def lane(lid, sid=None, state="needs_input"):
    return {"lane_id": lid, "state": state, "session_id": sid}


class TestSerial:
    def test_a_serial_job_resumes_on_its_own_session(self, jobs_dir):
        write(jobs_dir, session_id="sess-1")
        out = dispatcher.answer_job("job_a", "do it")
        assert out["resume"] is True and out["status"] == "queued"
        assert out["answer"] == "do it"

    def test_a_job_with_no_session_anywhere_is_still_rejected(self, jobs_dir):
        write(jobs_dir)
        with pytest.raises(ValueError, match="no session_id"):
            dispatcher.answer_job("job_a", "do it")

    def test_a_job_not_awaiting_an_answer_is_rejected(self, jobs_dir):
        write(jobs_dir, status="coding", session_id="sess-1")
        with pytest.raises(ValueError, match="not awaiting"):
            dispatcher.answer_job("job_a", "do it")


class TestFanned:
    def test_a_fanned_job_resumes_on_the_asking_lanes_session(self, jobs_dir):
        write(jobs_dir, session_id=None, needs_input_lane="s2",
              lanes=[lane("s1", "sess-1", "ready"), lane("s2", "sess-2")])
        out = dispatcher.answer_job("job_a", "scope is pantry.py only")
        assert out["resume"] is True and out["status"] == "queued"
        assert out["answer"] == "scope is pantry.py only"

    def test_rejected_when_the_asking_lane_has_no_session(self, jobs_dir):
        write(jobs_dir, session_id=None, needs_input_lane="s2",
              lanes=[lane("s1", "sess-1"), lane("s2", None)])
        with pytest.raises(ValueError, match="no session_id"):
            dispatcher.answer_job("job_a", "x")

    def test_a_siblings_session_does_not_authorise_the_answer(self, jobs_dir):
        """The answer must resume the lane that ASKED — not merely prove some lane is live."""
        write(jobs_dir, session_id=None, needs_input_lane="s2",
              lanes=[lane("s1", "sess-1"), lane("s2", None)])
        with pytest.raises(ValueError, match="no session_id"):
            dispatcher.answer_job("job_a", "x")

    def test_rejected_when_no_lane_is_named(self, jobs_dir):
        write(jobs_dir, session_id=None, needs_input_lane=None,
              lanes=[lane("s1", "sess-1")])
        with pytest.raises(ValueError, match="no session_id"):
            dispatcher.answer_job("job_a", "x")
