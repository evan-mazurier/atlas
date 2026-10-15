"""Tests for tools/launch_session.py. Temp dirs only."""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_session as ls  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_LEDGER2_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("ATLAS_LAUNCH_NOW", "2026-10-10T08:30:00+02:00")
    return tmp_path


def brief(tmp_path, text="Build the Product page.\nPark it on branch atlas/m2.\n"):
    p = tmp_path / "brief.md"
    p.write_text(text, encoding="utf-8")
    return str(p)


def go(tmp_path, **kw):
    a = dict(area="job", typ="coding", model="claude-sonnet-5-5", title="Product page", review_min=20)
    a.update(kw)
    if "brief_path" not in a:
        a["brief_path"] = brief(tmp_path)
    return ls.prepare(**a)


def test_output_shape_and_marker_file(tmp_path):
    out = go(tmp_path)
    assert set(out) == {"marker", "area", "group", "model", "title", "prompt"}
    assert out["group"] == "Research" and out["model"] == "claude-sonnet-5-5"
    f = tmp_path / "ledger" / "auto" / f"{out['marker']}.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    assert set(rec) == {"marker", "area", "type", "model", "title", "brief_sha", "started"}
    assert rec["type"] == "coding" and len(rec["brief_sha"]) == 64 and rec["started"].startswith("2026-10-10T08:30")


def test_prompt_shape(tmp_path):
    out = go(tmp_path)
    lines = out["prompt"].splitlines()
    assert lines[0] == f"Run this first, on its own: : team-auto {out['marker']}"
    assert lines[1] == "Then run this, on its own: : team-type coding job"
    p = out["prompt"]
    assert "Build the Product page." in p
    for word in ("publish", "send", "pay", "sign", "merge to main", "push", "contact"):
        assert word in p
    assert "implementer, test-runner, reviewer" in p and "2 NEEDS CHANGES" in p
    assert "reviewer must pass the FINAL version" in p
    assert p.index(": team-summary") < p.index(": team-done")
    assert '"review_min": 20' in p
    assert p.index("BRIEF") < p.index("BOUNDARY") < p.index("CLOSING RITUAL")


def test_chore_budget_and_default_review_min(tmp_path):
    p = go(tmp_path, typ="chore", review_min=None)["prompt"]
    assert "1 NEEDS CHANGES" in p and "estimate" in p


def test_dry_run_writes_nothing(tmp_path):
    out = go(tmp_path, dry_run=True)
    assert out["marker"] and not (tmp_path / "ledger").exists()


def test_deterministic(tmp_path):
    assert go(tmp_path, dry_run=True) == go(tmp_path, dry_run=True)


@pytest.mark.parametrize("typ", ["legal", "quick-question", "quick", "money", "undeclared", "playbook", "nope"])
def test_refused_types(tmp_path, typ):
    with pytest.raises(ls.LaunchError):
        go(tmp_path, typ=typ)
    assert not (tmp_path / "ledger").exists()


def test_unknown_area(tmp_path):
    with pytest.raises(ls.LaunchError, match="unknown area"):
        go(tmp_path, area="nope")


@pytest.mark.parametrize("line", ["/grant-rework 5", "   /grant-rework", "/GRANT-REWORK 1"])
def test_grant_rework_refused(tmp_path, line):
    with pytest.raises(ls.LaunchError, match="grant-rework"):
        go(tmp_path, brief_path=brief(tmp_path, f"Do the thing.\n{line}\nmore\n"))
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize("line", ["/ack-break", "   /ack-break 2026-10-09:1:ab:cd", "/ACK-BREAK"])
def test_ack_break_refused(tmp_path, line):
    with pytest.raises(ls.LaunchError, match="ack-break"):
        go(tmp_path, brief_path=brief(tmp_path, f"Do the thing.\n{line}\nmore\n"))
    assert not (tmp_path / "ledger").exists()


@pytest.mark.parametrize("line", ["/allow-publish", "  /allow-publish 3", "/ALLOW-CONFIG", "/allow-config now"])
def test_allow_commands_refused(tmp_path, line):
    with pytest.raises(ls.LaunchError, match="allow-"):
        go(tmp_path, brief_path=brief(tmp_path, f"Do the thing.\n{line}\nmore\n"))
    assert not (tmp_path / "ledger").exists()


def test_grant_rework_mentioned_midline_is_fine(tmp_path):
    go(tmp_path, brief_path=brief(tmp_path, "Never type /grant-rework yourself.\n"))


def test_empty_and_missing_brief(tmp_path):
    with pytest.raises(ls.LaunchError):
        go(tmp_path, brief_path=brief(tmp_path, "  \n"))
    with pytest.raises(ls.LaunchError):
        go(tmp_path, brief_path=str(tmp_path / "missing.md"))


def test_cli_exit_codes_and_single_json(tmp_path, capsys):
    b = brief(tmp_path)
    assert ls.main(["--area", "job", "--type", "writing", "--model", "m", "--title", "t", "--brief", b,
                    "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["group"] == "Research"
    assert ls.main(["--area", "job", "--type", "legal", "--model", "m", "--title", "t", "--brief", b]) == 2


def test_summary_line_rule_in_prompt(tmp_path):
    p = go(tmp_path)["prompt"]
    assert "ONE line: no apostrophes or line breaks inside the JSON, each field 200 characters or fewer" in p


def test_record_session(tmp_path):
    out = go(tmp_path)
    f = tmp_path / "ledger" / "auto" / f"{out['marker']}.json"
    assert ls.main(["--record-session", out["marker"], "sess-123"]) == 0
    rec = json.loads(f.read_text(encoding="utf-8"))
    assert rec["session_id"] == "sess-123" and rec["area"] == "job" and rec["marker"] == out["marker"]
    assert not list(f.parent.glob("*.tmp"))
    assert ls.main(["--record-session", "nope-1", "x"]) == 2
    assert ls.main(["--record-session", "../evil", "x"]) == 2


def test_playbook_registered():
    t = (Path(ls.REPO) / "workflows.yaml").read_text(encoding="utf-8")
    assert re.search(r"^playbooks:.*launch-auto", t, re.M)
