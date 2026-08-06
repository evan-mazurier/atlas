"""
Taste-rubric injection — the deterministic design-standards inject that makes the design loop
taste-aware without relying on an agent choosing to invoke the design-taste-frontend skill
("invoke skill X" in a charter is a soft instruction LLMs follow unreliably).

Two seams, one shared standards block:
  - REVIEWER (item 1): keys off the DIFF's file headers (UI files → judge the look).
  - CODER (§5): no diff exists yet at coding-start, so it keys off the WORKER the scoper recorded
    (the lane's step agent, else the contract's top-level agent from §4b).
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import worker  # noqa: E402


# ---- reviewer-side detection (item 1) -------------------------------------------------
def test_is_design_review_matches_ui_diff():
    diff = "diff --git a/src/components/Card.astro b/src/components/Card.astro\n+<div>hi</div>\n"
    assert worker._is_design_review({}, diff) is True


def test_is_design_review_matches_css():
    diff = "diff --git a/src/global.css b/src/global.css\n+.x{color:red}\n"
    assert worker._is_design_review({}, diff) is True


def test_is_design_review_ignores_non_ui_diff():
    diff = "diff --git a/src/lib/calc.py b/src/lib/calc.py\n+def f():\n+    return 1\n"
    assert worker._is_design_review({}, diff) is False
    assert worker._is_design_review({}, "") is False
    assert worker._is_design_review({}, None) is False


# ---- coder-side detection (§5) --------------------------------------------------------
def test_is_design_job_from_contract_agent():
    assert worker._is_design_job({"contract": {"agent": "design-engineer"}}) is True


def test_is_design_job_from_lane_agent_wins():
    # a fanned lane names its own step agent
    assert worker._is_design_job({"contract": {"agent": "backend-engineer"}},
                                 {"agent": "design-engineer"}) is True


def test_is_design_job_false_for_non_design():
    assert worker._is_design_job({"contract": {"agent": "backend-engineer"}}) is False
    assert worker._is_design_job({}) is False
    assert worker._is_design_job({"contract": {}}, {}) is False
    assert worker._is_design_job({"contract": {"agent": None}}, {"agent": None}) is False


# ---- the rubrics themselves -----------------------------------------------------------
def test_both_rubrics_carry_the_shared_standards():
    for rubric in (worker.TASTE_RUBRIC, worker.TASTE_RUBRIC_BUILD):
        assert "Anti-default" in rubric
        assert "Consistency locks" in rubric
        assert "WCAG AA" in rubric


def test_rubrics_are_framed_for_their_role():
    # reviewer JUDGES the result; coder BUILDS to the standards
    assert "judge" in worker.TASTE_RUBRIC.lower()
    assert "build to these" in worker.TASTE_RUBRIC_BUILD.lower()
    # the coder rubric points at the installed skill for depth
    assert "design-taste-frontend" in worker.TASTE_RUBRIC_BUILD
