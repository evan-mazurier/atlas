"""Tests for tools/install_skills.py. Temp dirs only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_skills as inst  # noqa: E402


def make_src(tmp_path):
    src = tmp_path / "src"
    (src / "alpha").mkdir(parents=True)
    (src / "alpha" / "SKILL.md").write_text("alpha v1", encoding="utf-8")
    (src / "alpha" / "extra.txt").write_text("x", encoding="utf-8")
    (src / "notaskill").mkdir()
    return src


def test_install_idempotent_and_backup(tmp_path):
    src, dst = make_src(tmp_path), tmp_path / "dst"
    assert inst.install(src, dst, dry_run=True) == [{"skill": "alpha", "action": "install", "backup": None}]
    assert not dst.exists()
    assert inst.install(src, dst)[0]["action"] == "install"
    assert (dst / "alpha" / "extra.txt").read_text() == "x"
    assert inst.install(src, dst) == [{"skill": "alpha", "action": "unchanged"}]
    assert not (dst / ".atlas-backup").exists()
    (src / "alpha" / "SKILL.md").write_text("alpha v2", encoding="utf-8")
    r = inst.install(src, dst)[0]
    assert r["action"] == "replace"
    assert (dst / "alpha" / "SKILL.md").read_text() == "alpha v2"
    backups = list((dst / ".atlas-backup").iterdir())
    assert len(backups) == 1 and (backups[0] / "SKILL.md").read_text() == "alpha v1"


def test_dry_run_replace_changes_nothing(tmp_path):
    src, dst = make_src(tmp_path), tmp_path / "dst"
    inst.install(src, dst)
    (src / "alpha" / "SKILL.md").write_text("alpha v2", encoding="utf-8")
    assert inst.install(src, dst, dry_run=True)[0]["action"] == "replace"
    assert (dst / "alpha" / "SKILL.md").read_text() == "alpha v1"
    assert not (dst / ".atlas-backup").exists()


def test_repo_skills_install_into_temp(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_SKILLS_DIR", str(tmp_path / "skills"))
    res = inst.install()
    assert {r["skill"] for r in res} >= {"launch-auto", "weekly-report"}
    for n in ("launch-auto", "weekly-report"):
        head = (tmp_path / "skills" / n / "SKILL.md").read_text(encoding="utf-8").splitlines()
        assert head[0] == "---" and head[1] == f"name: {n}"


def test_two_replaces_same_second_do_not_collide(tmp_path):
    src, dst = make_src(tmp_path), tmp_path / "dst"
    inst.install(src, dst)
    for v in ("v2", "v3"):
        (src / "alpha" / "SKILL.md").write_text(v, encoding="utf-8")
        inst.install(src, dst)
    assert len(list((dst / ".atlas-backup").iterdir())) == 2
