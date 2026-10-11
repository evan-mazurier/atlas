"""Prepare an automatic Claude Code session (docs/v2/contracts.md §7).

Deterministic half of /launch-auto: validate the request, write the marker file
`<ledger>/auto/<marker>.json`, print ONE JSON object `{marker, area, group, model, title, prompt}`
that the Chief hands to the scheduled-task tool. Starting the session itself is the Chief's job
(Python cannot call the desktop app's tools).

    py -3.12 tools/launch_session.py --area md --type coding --model claude-sonnet-5-5 \
        --title "Product page" --brief brief.md [--review-min 20] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))

from ledger2 import areas as areas_mod, store  # noqa: E402

REFUSED_TYPES = {
    "legal": "legal work never runs alone: it is always decided by hand",
    "quick-question": "a quick question cannot change files, so there is nothing to run alone",
    "undeclared": "'undeclared' is not a type of work: declare a real one",
    "playbook": "playbooks are started by their slash command, not through the launcher",
}


class LaunchError(Exception):
    pass


def _load(name: str):
    env = os.environ.get("ATLAS_AREAS_FILE" if name == "areas.yaml" else "ATLAS_WORKFLOWS_FILE")
    p = Path(env) if env else REPO / name
    return areas_mod.mini_yaml(p.read_text(encoding="utf-8"))


def load_areas() -> list[dict]:
    env = os.environ.get("ATLAS_AREAS_FILE")
    return areas_mod.load_areas(env or str(REPO / "areas.yaml"))


def check_brief(text: str) -> None:
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip().lower().startswith("/grant-rework"):
            raise LaunchError(f"brief line {n} starts with /grant-rework: only the operator can grant a rework round")
        if line.strip().lower().startswith("/ack-break"):
            raise LaunchError(f"brief line {n} starts with /ack-break: only the operator can acknowledge an integrity break")
    if not text.strip():
        raise LaunchError("the brief is empty")


def _now() -> dt.datetime:
    fixed = os.environ.get("ATLAS_LAUNCH_NOW")
    return dt.datetime.fromisoformat(fixed) if fixed else dt.datetime.now().astimezone()


BOUNDARY = """\
BOUNDARY (applies to everything in this session)
- Work stays reversible: branches, drafts, unpublished pages, research notes.
- Never publish, send, pay, sign, merge to main, push, or contact anyone (client, recruiter, anybody).
- Logins, payments and submits are the operator's. If the work needs one, stop and park what you have.
- Do not run /grant-rework and do not start a nested claude: only the operator grants extra rework rounds."""


def build_prompt(marker: str, area: str, typ: str, brief: str, wf: dict, review_min: int | None) -> str:
    info = (wf.get("types") or {}).get(typ) or {}
    crew = info.get("crew") or []
    budget = info.get("budget", wf.get("default_budget", 2))
    crew_s = ", ".join(crew) if crew else "none (work directly)"
    if budget is None:
        rw = "This type has no rework."
    elif budget == 0:
        rw = "Rework budget: 0. A NEEDS CHANGES verdict ends the task: park it and report."
    else:
        rw = (f"Rework budget: {budget} NEEDS CHANGES verdict(s). When it is spent, builders are refused: "
              "park the result with the open findings listed instead of retrying.")
    rm = str(review_min) if review_min is not None else "<your estimate of the operator's review time, whole minutes>"
    summary = ('{"goal": "...", "produced": "branch or file path", "decisions": "...", '
               f'"open": "...", "review_min": {rm}}}')
    return "\n".join([
        f"Run this first, on its own: : team-auto {marker}",
        f"Then run this, on its own: : team-type {typ} {area}",
        "",
        "BRIEF",
        brief.rstrip(),
        "",
        BOUNDARY,
        "",
        "CREW AND GATES",
        f"- Crew for this type ({typ}): {crew_s}. Use named helpers only; you orchestrate, they build.",
        f"- {rw}",
        "- The reviewer must pass the FINAL version (a READY verdict at the last edit). Anything edited "
        "after the verdict needs a new review.",
        "",
        "CLOSING RITUAL (in this order)",
        "1. Park the result: name the branch, or the file path of the draft, in your last message.",
        "2. The summary is ONE line: no apostrophes or line breaks inside the JSON, each field 200 characters or fewer.",
        f"   Run on its own: : team-summary '{summary}'",
        "3. Run on its own: : team-done",
        "",
    ])


def prepare(area: str, typ: str, model: str, title: str, brief_path: str,
            review_min: int | None = None, dry_run: bool = False) -> dict:
    ids = {a["id"]: a for a in load_areas()}
    if area not in ids:
        raise LaunchError(f"unknown area '{area}' (known: {', '.join(ids)})")
    wf = _load("workflows.yaml")
    types = wf.get("types") or {}
    typ = (wf.get("aliases") or {}).get(typ, typ)
    if typ in REFUSED_TYPES:
        raise LaunchError(f"type '{typ}' refused: {REFUSED_TYPES[typ]}")
    if typ not in types:
        raise LaunchError(f"unknown type '{typ}' (known: {', '.join(types)})")
    if not model.strip():
        raise LaunchError("a model id is required: the session's model is always set explicitly")
    if not title.strip():
        raise LaunchError("a title is required")
    if review_min is not None and review_min < 0:
        raise LaunchError("--review-min must be >= 0")
    try:
        raw = Path(brief_path).read_bytes()
    except OSError as e:
        raise LaunchError(f"cannot read the brief: {e}") from e
    brief = raw.decode("utf-8")
    check_brief(brief)
    sha = hashlib.sha256(raw).hexdigest()
    now = _now()
    slug = hashlib.sha256(f"{area}|{typ}|{title}|{sha}".encode()).hexdigest()[:6]
    marker = f"{area}-{now.strftime('%Y%m%d-%H%M%S')}-{slug}"
    started = now.isoformat(timespec="seconds")
    group = ids[area].get("group") or ids[area].get("name") or area
    prompt = build_prompt(marker, area, typ, brief, wf, review_min)
    if not dry_run:
        d = store.auto_dir()
        d.mkdir(parents=True, exist_ok=True)
        rec = {"marker": marker, "area": area, "type": typ, "model": model, "title": title,
               "brief_sha": sha, "started": started}
        (d / f"{marker}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"marker": marker, "area": area, "group": group, "model": model, "title": title, "prompt": prompt}


def record_session(marker: str, session_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", marker or ""):
        raise LaunchError(f"bad marker '{marker}'")
    if not (session_id or "").strip():
        raise LaunchError("a session id is required")
    f = store.auto_dir() / f"{marker}.json"
    if not f.is_file():
        raise LaunchError(f"unknown marker '{marker}'")
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["session_id"] = session_id.strip()
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)
    return rec


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--record-session":
        if len(argv) != 3:
            print("usage: launch_session.py --record-session <marker> <session_id>", file=sys.stderr)
            return 2
        try:
            record_session(argv[1], argv[2])
        except LaunchError as e:
            print(f"launch_session: {e}", file=sys.stderr)
            return 2
        print(json.dumps({"ok": True, "marker": argv[1], "session_id": argv[2]}))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--area", required=True)
    ap.add_argument("--type", required=True, dest="typ")
    ap.add_argument("--model", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--brief", required=True)
    ap.add_argument("--review-min", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="validate and print, write no marker")
    a = ap.parse_args(argv)
    try:
        out = prepare(a.area, a.typ, a.model, a.title, a.brief, a.review_min, a.dry_run)
    except LaunchError as e:
        print(f"launch_session: {e}", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
