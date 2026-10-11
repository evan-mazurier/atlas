"""Install the repo's skills/<name>/ folders into the user's Claude skills dir.

    py -3.12 tools/install_skills.py [--dry-run] [--only name ...]

Target: ATLAS_SKILLS_DIR or ~/.claude/skills. A skill that already exists and differs is first
copied to <target>/.atlas-backup/<name>-<timestamp>/ (never inside the skills dir proper, so it is
not loaded as a skill). Identical skills are left alone: running twice changes nothing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "skills"


def target_dir() -> Path:
    return Path(os.environ.get("ATLAS_SKILLS_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "skills"))


def _same(a: Path, b: Path) -> bool:
    fa = sorted(p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file())
    fb = sorted(p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file())
    return fa == fb and all(filecmp.cmp(a / f, b / f, shallow=False) for f in fa)


def install(src: Path = SRC, dst: Path | None = None, dry_run: bool = False, only=None) -> list[dict]:
    dst = dst or target_dir()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    res = []
    for d in sorted(p for p in src.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()):
        if only and d.name not in only:
            continue
        t = dst / d.name
        if t.exists() and _same(d, t):
            res.append({"skill": d.name, "action": "unchanged"})
            continue
        action, backup = ("replace" if t.exists() else "install"), None
        if t.exists():
            backup = dst / ".atlas-backup" / f"{d.name}-{stamp}"
        res.append({"skill": d.name, "action": action, "backup": str(backup) if backup else None})
        if dry_run:
            continue
        if backup:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(t, backup)
            shutil.rmtree(t)
        shutil.copytree(d, t)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args(argv)
    for r in install(dry_run=a.dry_run, only=a.only):
        print(("[dry-run] " if a.dry_run else "") + f"{r['skill']}: {r['action']}"
              + (f" (backup {r['backup']})" if r.get("backup") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
