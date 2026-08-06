"""
approve_planner (PLAN mode) must not let a planner proposal silently drop Atlas's own bookkeeping.

A plan approval is a WHOLE-FILE overwrite — the planner authors plan.yaml and Atlas writes it. So
until this pass existed, the machine-written task fields survived only if the planner happened to
echo them back, which is neither guaranteed nor enforced. The most consequential one is `dispatch:`,
the backlink `worker._advance_plan_after_merge` matches on: lose it and merging the job no longer
advances the plan.

The SCHEDULE branch already had this guarantee (pinned placements + life-events, see
test_placement_api.py). These tests give the PLAN branch the same one.

Ownership under test (dispatcher._ATLAS_TASK_FIELDS):
  Atlas-owned   — dispatch, sessions_done, review_pending, done_date  (disk is the sole truth)
  planner-owned — title, state, priority, type, estimate, remaining, target_date, notes, ordering

Run:  py -3.12 -m pytest dashboard/test_planner_preserve.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dispatcher  # noqa: E402
import worker  # noqa: E402

# On disk: task-one has been dispatched and half-worked; task-two is untouched.
DISK = """project: demo
# a header comment on the live file

milestones:
  - id: m1
    title: "Milestone one"
    status: active

tasks:
  - id: task-one
    title: "First task"
    type: dispatch
    state: in_progress
    estimate: 2h
    remaining: 1h
    dispatch: job_live
    sessions_done: [sess_a]
    review_pending: true
  - id: task-two
    title: "Second task"
    type: manual
    state: next
    estimate: 30m
"""

# What the planner proposes back: it re-titles task-one, re-estimates it, adds a task — and (the
# whole point) echoes NONE of the Atlas fields.
PROPOSAL = """project: demo

milestones:
  - id: m1
    title: "Milestone one"
    status: active

tasks:
  - id: task-one
    title: "First task, re-scoped"
    type: dispatch
    state: next
    estimate: 3h
    remaining: 3h
  - id: task-two
    title: "Second task"
    type: manual
    state: next
    estimate: 30m
  - id: task-three
    title: "Brand new task"
    type: dispatch
    state: todo
    estimate: 1h
"""


@pytest.fixture()
def approve(tmp_path, monkeypatch):
    """A planner job whose cwd is tmp_path, with DISK already in place. Returns a callable that
    approves a proposal and hands back (parsed plan, raw text, note)."""
    (tmp_path / "plan.yaml").write_text(DISK, encoding="utf-8")
    (tmp_path / "status.yaml").write_text('next_target: "First task"\n', encoding="utf-8")
    jobs = {"job_plan": {"id": "job_plan", "kind": "planner", "mode": "plan",
                         "project": "demo", "cwd": str(tmp_path)}}
    monkeypatch.setattr(dispatcher, "get_job", lambda jid: jobs.get(jid))
    monkeypatch.setattr(dispatcher, "_write", lambda job: None)
    monkeypatch.setattr(dispatcher, "_ensure_plan_file_registry", lambda pid, path: None)
    monkeypatch.setattr(dispatcher, "_to_local", lambda p: p)

    def _go(proposal=PROPOSAL):
        res = dispatcher.approve_planner("job_plan", proposal)
        raw = (tmp_path / "plan.yaml").read_text(encoding="utf-8")
        return yaml.safe_load(raw), raw, res.get("note", "")
    _go.dir = tmp_path
    return _go


def _task(plan, tid):
    return next(t for t in plan["tasks"] if t["id"] == tid)


# ── the Atlas-owned fields survive ────────────────────────────────────────────────────────────────

def test_the_dispatch_backlink_survives_a_proposal_that_omits_it(approve):
    plan, _, _ = approve()
    assert _task(plan, "task-one")["dispatch"] == "job_live"


def test_the_surviving_link_is_still_in_the_shape_the_merge_hook_matches(approve):
    """The two halves are coupled only by worker's regex — assert against the real matcher, not the
    parsed value, so a formatting change can't quietly break the merge hook."""
    _, raw, _ = approve()
    tasks_text = raw[raw.index("tasks:"):]
    hit = worker._find_task_block(tasks_text, "job_live")
    assert hit and "task-one" in hit[2]


def test_the_other_bookkeeping_fields_survive_too(approve):
    plan, _, _ = approve()
    t = _task(plan, "task-one")
    assert t["sessions_done"] == ["sess_a"]
    assert t["review_pending"] is True


def test_a_done_date_survives(approve):
    disk = DISK.replace("    review_pending: true", "    review_pending: false\n    done_date: 2026-08-01")
    (approve.dir / "plan.yaml").write_text(disk, encoding="utf-8")
    plan, _, _ = approve()
    assert str(_task(plan, "task-one")["done_date"]) == "2026-08-01"


# ── the planner still owns the backlog ────────────────────────────────────────────────────────────

def test_the_planner_still_owns_title_state_and_estimates(approve):
    """Preservation must be a bounded pass, not a merge that fights the planner's actual job."""
    plan, _, _ = approve()
    t = _task(plan, "task-one")
    assert t["title"] == "First task, re-scoped"
    assert t["state"] == "next"          # NOT restored to the on-disk in_progress
    assert t["estimate"] == "3h"
    assert t["remaining"] == "3h"        # the reestimate mode's field stays planner-owned


def test_new_tasks_come_through_untouched(approve):
    plan, _, _ = approve()
    assert _task(plan, "task-three")["title"] == "Brand new task"
    assert "dispatch" not in _task(plan, "task-three")


def test_a_task_with_no_atlas_fields_is_left_alone(approve):
    plan, _, _ = approve()
    t = _task(plan, "task-two")
    assert set(t) == {"id", "title", "type", "state", "estimate"}


def test_the_proposals_own_formatting_survives(approve):
    """Text surgery, not a YAML round-trip — the planner's ordering and blank lines stay put."""
    _, raw, _ = approve()
    assert raw.index("task-one") < raw.index("task-two") < raw.index("task-three")
    assert 'title: "Brand new task"' in raw


# ── disk is the SOLE source of truth (an invented field is stripped, not trusted) ─────────────────

def test_an_invented_dispatch_link_is_stripped(approve):
    """A planner echoing a job id it never launched would make a later merge advance the WRONG task.
    Absent on disk => absent after the write, whatever the proposal claims."""
    proposal = PROPOSAL.replace("    id: task-two\n", "    id: task-two\n    dispatch: job_invented\n")
    plan, raw, _ = approve(proposal)
    assert "dispatch" not in _task(plan, "task-two")
    assert "job_invented" not in raw


def test_a_stale_echoed_value_loses_to_disk(approve):
    """The planner echoing an OLD job id is the same failure with a plausible-looking value."""
    proposal = PROPOSAL.replace("    id: task-one\n", "    id: task-one\n    dispatch: job_stale\n")
    plan, raw, _ = approve(proposal)
    assert _task(plan, "task-one")["dispatch"] == "job_live"
    assert "job_stale" not in raw


def test_an_invented_review_pending_is_stripped(approve):
    proposal = PROPOSAL.replace("    id: task-three\n", "    id: task-three\n    review_pending: true\n")
    plan, _, _ = approve(proposal)
    assert "review_pending" not in _task(plan, "task-three")


# ── reporting + the degenerate cases ─────────────────────────────────────────────────────────────

def test_a_dropped_linked_task_is_reported_not_silently_lost(approve):
    """Nothing can preserve a task the planner deleted — but losing a live job link deserves to be
    said out loud, since that job's merge can no longer advance anything."""
    proposal = PROPOSAL.replace("""  - id: task-one
    title: "First task, re-scoped"
    type: dispatch
    state: next
    estimate: 3h
    remaining: 3h
""", "")
    plan, _, note = approve(proposal)
    assert not any(t["id"] == "task-one" for t in plan["tasks"])
    assert "task-one" in note and "linked to a job" in note


def test_the_note_reports_what_was_preserved(approve):
    _, _, note = approve()
    assert "preserved Atlas fields on 1 task(s)" in note


def test_a_first_ever_plan_writes_the_proposal_unchanged(tmp_path, monkeypatch):
    """No plan.yaml on disk (a project's first planner run) => nothing to preserve, and the pass
    must not get in the way."""
    jobs = {"job_plan": {"id": "job_plan", "kind": "planner", "mode": "plan",
                         "project": "demo", "cwd": str(tmp_path)}}
    monkeypatch.setattr(dispatcher, "get_job", lambda jid: jobs.get(jid))
    monkeypatch.setattr(dispatcher, "_write", lambda job: None)
    monkeypatch.setattr(dispatcher, "_ensure_plan_file_registry", lambda pid, path: None)
    monkeypatch.setattr(dispatcher, "_to_local", lambda p: p)
    dispatcher.approve_planner("job_plan", PROPOSAL)
    # approve_planner strips the proposal and re-terminates it — that is its pre-existing behaviour
    assert (tmp_path / "plan.yaml").read_text(encoding="utf-8") == PROPOSAL.strip() + "\n"


def test_an_empty_task_list_is_not_read_as_a_mass_drop(approve):
    """A proposal with no readable task items must not report every linked task as an orphan."""
    plan, _, note = approve("project: demo\n\ntasks: []\n")
    assert "linked to a job" not in note


# ── a field's value may not be a scalar ──────────────────────────────────────────────────────────

BLOCK_STYLE = """    sessions_done:
      - sess_a
      - sess_b"""


def test_a_block_style_list_on_disk_round_trips(approve):
    """`sessions_done` is a LIST. Atlas always writes it flow-style, but plan.yaml is hand-editable
    and block style is equally valid YAML — so it has to survive. Reading it as a scalar picks up
    only the first item and emits `sessions_done: - sess_a`, which is not valid YAML at all."""
    (approve.dir / "plan.yaml").write_text(
        DISK.replace("    sessions_done: [sess_a]", BLOCK_STYLE), encoding="utf-8")
    plan, _, _ = approve()
    assert _task(plan, "task-one")["sessions_done"] == ["sess_a", "sess_b"]


def test_a_block_style_value_does_not_bleed_into_the_next_field(approve):
    """The span must end where the indentation does — swallowing or orphaning the following line
    corrupts a neighbouring field instead of the one being preserved."""
    (approve.dir / "plan.yaml").write_text(
        DISK.replace("    sessions_done: [sess_a]", BLOCK_STYLE), encoding="utf-8")
    plan, _, _ = approve()
    t = _task(plan, "task-one")
    assert t["review_pending"] is True        # the line right after the block
    assert t["dispatch"] == "job_live"        # the line right before it


def test_a_block_style_value_replaces_a_flow_style_proposal(approve):
    """Disk is the sole source of truth whatever SHAPE each side is in."""
    (approve.dir / "plan.yaml").write_text(
        DISK.replace("    sessions_done: [sess_a]", BLOCK_STYLE), encoding="utf-8")
    proposal = PROPOSAL.replace("    id: task-one\n", "    id: task-one\n    sessions_done: [wrong]\n")
    plan, raw, _ = approve(proposal)
    assert _task(plan, "task-one")["sessions_done"] == ["sess_a", "sess_b"]
    assert "wrong" not in raw


def test_a_block_style_proposal_value_is_fully_stripped_when_absent_on_disk(approve):
    """Removing a field means removing its continuation lines too — dropping only the `key:` line
    would leave orphaned list items behind and corrupt the file."""
    proposal = PROPOSAL.replace(
        "    id: task-three\n", "    id: task-three\n" + BLOCK_STYLE + "\n")
    plan, raw, _ = approve(proposal)
    assert "sessions_done" not in _task(plan, "task-three")
    assert "sess_b" not in raw


def test_a_malformed_on_disk_value_refuses_the_write(approve):
    """plan.yaml is hand-editable and nothing validates it. A value that would corrupt the merged
    file must fail loudly — the on-disk plan is left exactly as it was."""
    (approve.dir / "plan.yaml").write_text(
        DISK.replace("    dispatch: job_live", "    dispatch: job: live"), encoding="utf-8")
    before = (approve.dir / "plan.yaml").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="invalid YAML"):
        approve()
    assert (approve.dir / "plan.yaml").read_text(encoding="utf-8") == before


def test_next_target_is_still_refreshed_from_the_written_plan(approve):
    """The derived next_target must follow the file that actually landed, not the pre-merge text."""
    approve()
    status = (approve.dir / "status.yaml").read_text(encoding="utf-8")
    assert 'next_target: "First task, re-scoped"' in status
