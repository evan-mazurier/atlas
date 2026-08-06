"""
The plan<->job link: `dispatch: <job id>` written into plan.yaml when a job is launched FROM a
planned task.

That key is what `worker._advance_plan_after_merge` matches on. Without it a merge cannot tell
which task it completed, so the task never advances and status.yaml's next_target keeps pointing
at finished work. Observed live on orchard-journal 2026-08-05: reel-queue-reset merged cleanly and
still had to be marked done by hand.

Run:  py -3.12 -m pytest dashboard/test_plan_dispatch_link.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dispatcher  # noqa: E402
import worker  # noqa: E402

PLAN = """project: demo

tasks:
  - id: task-one
    title: "First task"
    type: dispatch
    state: next
    estimate: 1h
  - id: task-two
    title: "Second task"
    type: manual
    state: next
    estimate: 30m
"""


@pytest.fixture()
def plan(tmp_path, monkeypatch):
    (tmp_path / "plan.yaml").write_text(PLAN, encoding="utf-8")
    (tmp_path / "status.yaml").write_text('next_target: "First task"\n', encoding="utf-8")
    monkeypatch.setattr(dispatcher, "_plan_file_for", lambda pid: tmp_path / "plan.yaml")
    return tmp_path


def test_link_writes_the_dispatch_key(plan):
    assert dispatcher._link_plan_task("demo", "task-one", "job_abc") is True
    text = (plan / "plan.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s*dispatch: job_abc\s*$", text, re.MULTILINE)


def test_the_key_is_written_in_the_shape_the_merge_hook_matches(plan):
    """The two halves live in different modules and are only coupled by this regex — assert
    against the worker's own matcher rather than eyeballing the format."""
    dispatcher._link_plan_task("demo", "task-one", "job_abc")
    text = (plan / "plan.yaml").read_text(encoding="utf-8")
    tasks_text = text[re.search(r"^tasks:\s*\n", text, re.MULTILINE).end():]
    assert worker._find_task_block(tasks_text, "job_abc") is not None


def test_the_link_lands_on_the_right_task(plan):
    dispatcher._link_plan_task("demo", "task-two", "job_xyz")
    text = (plan / "plan.yaml").read_text(encoding="utf-8")
    tasks_text = text[re.search(r"^tasks:\s*\n", text, re.MULTILINE).end():]
    hit = worker._find_task_block(tasks_text, "job_xyz")
    assert hit and "task-two" in hit[2] and "task-one" not in hit[2]


def test_relaunching_a_task_replaces_its_link(plan):
    """A retried task must point at the NEW job, not accumulate two dispatch keys — otherwise the
    hook matches a stale job and advances on the wrong merge."""
    dispatcher._link_plan_task("demo", "task-one", "job_old")
    dispatcher._link_plan_task("demo", "task-one", "job_new")
    text = (plan / "plan.yaml").read_text(encoding="utf-8")
    assert text.count("dispatch:") == 1
    assert "job_new" in text and "job_old" not in text


def test_an_unknown_task_is_a_no_op_not_a_crash(plan):
    """A dispatch must never fail over bookkeeping — the job is valid without the link."""
    before = (plan / "plan.yaml").read_text(encoding="utf-8")
    assert dispatcher._link_plan_task("demo", "no-such-task", "job_abc") is False
    assert (plan / "plan.yaml").read_text(encoding="utf-8") == before


def test_a_missing_plan_file_is_a_no_op_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "_plan_file_for", lambda pid: tmp_path / "absent.yaml")
    assert dispatcher._link_plan_task("demo", "task-one", "job_abc") is False


def test_the_rest_of_the_plan_survives_the_edit(plan):
    """Text surgery, not a YAML round-trip — comments, order and the other task stay byte-intact."""
    dispatcher._link_plan_task("demo", "task-one", "job_abc")
    text = (plan / "plan.yaml").read_text(encoding="utf-8")
    assert "project: demo" in text
    assert text.index("task-one") < text.index("task-two")
    assert 'title: "Second task"' in text and "estimate: 30m" in text


# ── the seam the control center's launch button rides on ────────────────────────────────────────────────
# `enqueue(plan_task=...)` is the only thing that turns a task_id carried from the UI into a real
# link on disk. Every launch door — the detail card's [launch dispatch] button, the left-rail "up
# next" chip, the receptionist's launch_next path — posts task_id to /api/dispatch, which passes it
# here (the control center server). Tested end-of-chain rather than per-door: the doors are UI, this is the
# contract they all depend on.

@pytest.fixture()
def enqueued(plan, monkeypatch):
    """dispatcher.enqueue against the fixture plan, with the job record kept in memory."""
    written = []
    monkeypatch.setattr(dispatcher, "projects", lambda: {
        "demo": {"cwd": str(plan), "is_git": False, "test_cmd": None, "sidecar_dir": str(plan)}})
    monkeypatch.setattr(dispatcher, "resolve_procedure",
                        lambda project, session_type=None: {"model": "sonnet", "effort": "medium"})
    monkeypatch.setattr(dispatcher, "resolve_review",
                        lambda project: {"model": "opus", "effort": "high"})
    monkeypatch.setattr(dispatcher, "_write", written.append)
    return lambda **kw: dispatcher.enqueue("demo", "a brief", **kw)


def test_a_planned_launch_links_the_job_to_its_task(enqueued, plan):
    """The whole point of the launch button: an arbitrary task can be dispatched with its link
    intact, so the merge hook can find it."""
    job = enqueued(scope=True, plan_task="task-one")
    assert job["plan_task"] == "task-one"
    text = (plan / "plan.yaml").read_text(encoding="utf-8")
    tasks_text = text[re.search(r"^tasks:\s*\n", text, re.MULTILINE).end():]
    hit = worker._find_task_block(tasks_text, job["id"])
    assert hit and "task-one" in hit[2]


def test_a_free_form_dispatch_leaves_the_plan_alone(enqueued, plan):
    before = (plan / "plan.yaml").read_text(encoding="utf-8")
    job = enqueued(scope=True)
    assert job["plan_task"] is None
    assert (plan / "plan.yaml").read_text(encoding="utf-8") == before


def test_launching_never_moves_the_task_state(enqueued, plan):
    """Plan `state` is maintained BY HAND (the operator's explicit decision) — only the merge hook moves it.
    A launch writes the link and nothing else."""
    import yaml
    enqueued(scope=True, plan_task="task-one")
    text = (plan / "plan.yaml").read_text(encoding="utf-8")
    task = next(t for t in yaml.safe_load(text)["tasks"] if t["id"] == "task-one")
    assert task["state"] == "next"          # unchanged
    assert task["estimate"] == "1h"


def test_a_task_can_be_relaunched(enqueued, plan):
    """A failed or abandoned job leaves a stale pointer on a still-open task — the [re-dispatch]
    state. The second launch must repoint the task, not accumulate links."""
    import yaml
    enqueued(scope=True, plan_task="task-one")
    second = enqueued(scope=True, plan_task="task-one")
    text = (plan / "plan.yaml").read_text(encoding="utf-8")
    assert text.count("dispatch:") == 1
    task = next(t for t in yaml.safe_load(text)["tasks"] if t["id"] == "task-one")
    assert task["dispatch"] == second["id"]
