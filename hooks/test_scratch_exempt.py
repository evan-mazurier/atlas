"""Scratch exemption: scratch-*.md, *.scratch.md and .md/.txt files in a _scratch folder record no edit."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_atlas_hook import env  # noqa: E402,F401
import atlas_hook  # noqa: E402


@pytest.fixture
def hook():
    H = atlas_hook.Ctx.__new__(atlas_hook.Ctx)
    H.cfg = {"exempt_temp": False}
    return H


EXEMPT = [
    r"D:\AI\proj\scratch-x.md",
    "D:/AI/proj/scratch-x.md",
    r"D:\AI\proj\SCRATCH-X.MD",
    r"D:\AI\proj\x.scratch.md",
    r"D:\AI\proj\_scratch\scope.md",
    "D:/AI/proj/_scratch/deep/log.txt",
    r"D:\AI\proj\_SCRATCH\Log.TXT",
    r"D:\AI\proj\x.build-log.md",
    r"D:\AI\proj\a.notes.md",
]
NOT_EXEMPT = [
    # the old notes rules are gone
    r"D:\AI\proj\notes-x.md",
    r"D:\AI\proj\notes-2026-10-12-b.md",
    r"D:\AI\proj\notes\scope.md",
    r"D:\Documents\Reading List\notes\drafts\notes-2026-10-12.md",
    # near misses
    r"D:\AI\proj\scratchpad.md",
    r"D:\AI\proj\my-scratch-x.md",
    r"D:\AI\proj\scratch-x.txt",
    r"D:\AI\proj\scratch.md",
    r"D:\AI\proj\scratch\x.md",
    r"D:\AI\proj\_scratch",
    r"D:\AI\proj\_scratch\x.py",
    r"D:\AI\proj\_scratch\x.json",
    r"D:\AI\app\src\_scratch\models.py",
    # dot folders and well-known files
    r"D:\AI\repo\.claude\skills\_scratch\SKILL.md",
    r"D:\AI\repo\_scratch\CLAUDE.md",
    r"D:\AI\repo\_scratch\AGENTS.md",
    r"D:\AI\repo\_scratch\skill.md",
    r"D:\AI\repo\_scratch\GEMINI.md",
    r"D:\AI\repo\.github\_scratch\x.md",
    r"D:\AI\repo\.github\scratch-x.md",
    r"D:\AI\repo\_scratch\requirements.txt",
    r"D:\AI\repo\_scratch\CMakeLists.txt",
    # normalisation
    r"D:\AI\proj\_scratch\..\src\main.py",
    r"D:\AI\proj\_scratch\..\src\x.md",
    "//?/D:/AI/proj/_scratch/../src/x.py",
    "//?/D:/AI/proj/_scratch/../src/x.md",
    r"D:\AI\repo\.git\..\_scratch\..\.claude\x.md",
]


@pytest.mark.parametrize("path", EXEMPT)
def test_exempt(hook, path):
    assert hook.is_exempt(path)


@pytest.mark.parametrize("path", NOT_EXEMPT)
def test_not_exempt(hook, path):
    assert not hook.is_exempt(path)


def test_end_to_end_scratch_edit_records_nothing_and_stop_passes(env):
    env.set_mode("enforce")
    env.bash(": team-type coding")
    env.edit("scratch-x.md")
    assert not env.events("edit")
    assert env.run("Stop", stop_hook_active=False) is None
    assert not env.events("sendback")
    env.edit("letter.md")
    assert env.events("edit")
    out = env.run("Stop", stop_hook_active=False)
    assert out["decision"] == "block"


def test_scratch_folder_edit_records_nothing_only_for_md_txt(env):
    (env.work / "_scratch").mkdir()
    env.edit("_scratch/anything.md")
    assert not env.events("edit")
    env.edit("_scratch/anything.py")
    assert env.events("edit")


def test_notes_files_are_work_again(env):
    env.edit("notes-x.md")
    assert env.events("edit")


def test_protected_path_in_scratch_folder_still_denied(env):
    out = env.run("PreToolUse", tool_name="Write", tool_input={"file_path": str(env.hooks / "_scratch" / "x.md")})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
