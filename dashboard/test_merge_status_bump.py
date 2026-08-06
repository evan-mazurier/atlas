"""
Field ownership in the post-merge status.yaml bump (_update_status_after_merge).

CLAUDE.md's inputs-vs-outputs rule splits status.yaml in two: `updated` and `next_target` are
DERIVED, so the rung-0 merge hook owns them; `last_session`, `phase`, `health` and `blockers` are
JUDGMENT, so the scribe's session-end procedure owns them. Every project's procedures.yaml already
states exactly that ("Leave `updated` and `next_target` for the merge hook").

The hook used to write `last_session` too, with the job's task line truncated at 80 chars. That
made a considered end-of-session summary unsurvivable: any later dispatch merge replaced it. These
tests pin the boundary so the write is not helpfully reintroduced.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker  # noqa: E402

HAND_WRITTEN = "closed the first-reel milestone and merged both hardening lanes"

STATUS = """project: "Test Project"
kind: app
status: building
phase: "Phase 2"
health: green
updated: 2026-01-01
last_session: "{last}"
next_target: "something"
detail: "progress.md"
blockers: []
"""


def _project(tmp_path, last=HAND_WRITTEN):
    (tmp_path / "status.yaml").write_text(STATUS.format(last=last), encoding="utf-8")
    return {"id": "job_abc123", "cwd": str(tmp_path),
            "task": "Reset the content queue around one next post — a long brief that would be "
                    "truncated mid-sentence at eighty characters if it were written to the file"}


def _fields(tmp_path):
    out = {}
    for line in (tmp_path / "status.yaml").read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip('"')
    return out


def test_merge_bumps_updated(tmp_path):
    worker._update_status_after_merge(_project(tmp_path))
    assert _fields(tmp_path)["updated"] == dt.date.today().isoformat()


def test_merge_leaves_last_session_to_the_scribe(tmp_path):
    """The judgment summary survives a merge — this is the regression the whole file exists for."""
    worker._update_status_after_merge(_project(tmp_path))
    assert _fields(tmp_path)["last_session"] == HAND_WRITTEN


def test_merge_never_writes_the_job_task_into_status(tmp_path):
    """Belt-and-braces: no part of the dispatch brief leaks into the sidecar under any key."""
    job = _project(tmp_path)
    worker._update_status_after_merge(job)
    text = (tmp_path / "status.yaml").read_text(encoding="utf-8")
    assert "Reset the content queue" not in text
    assert job["id"] not in text


def test_merge_leaves_the_other_judgment_fields_alone(tmp_path):
    worker._update_status_after_merge(_project(tmp_path))
    f = _fields(tmp_path)
    assert (f["phase"], f["health"], f["status"]) == ("Phase 2", "green", "building")


def test_merge_is_a_noop_when_there_is_no_status_file(tmp_path):
    """A project without a sidecar must never crash the merge."""
    worker._update_status_after_merge({"id": "job_x", "cwd": str(tmp_path), "task": "x"})
    assert not (tmp_path / "status.yaml").exists()
