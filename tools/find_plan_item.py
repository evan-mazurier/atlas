"""Find a Plan item id by name (contracts.md section 1.2, task-opening rules, `plan_ref`).

    py -3.12 tools/find_plan_item.py "weekly report" [--day YYYY-MM-DD]

Prints one line per match, best first, tab separated:
    <id> <score> <kind> <when> <area> <title>
The id goes straight into a session declaration: `: team-type <type> [area] task:<id>`.

Candidates: the calendar events of the day (a series occurrence is `evt_xxxxxxxx@YYYY-MM-DD`) and the open
tasks (not done) that are either unplanned or planned within one day of the day. Matching ignores case and
accents: exact title 100, one contains the other 80, otherwise the share of the query words found in the title
(a word counts when a title word starts with it) scaled to 60. Score 0 is never listed.

Source: the live cockpit (GET /api/v2/plan on 127.0.0.1:8765, override with --url or ATLAS_COCKPIT_URL); when
the server is down the same payload is built read-only from schedule.yaml / tasks.yaml with the plan engine
(nothing is written, not even the version file). Exit 0 whether or not anything matched (no match = no output).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))

DEFAULT_URL = "http://127.0.0.1:8765"


def fold(text) -> str:
    s = unicodedata.normalize("NFKD", str(text or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def score(query: str, title: str) -> int:
    q, t = fold(query), fold(title)
    if not q or not t:
        return 0
    if q == t:
        return 100
    if q in t or t in q:
        return 80
    qw, tw = q.split(), t.split()
    hit = sum(1 for w in qw if any(x == w or x.startswith(w) for x in tw))
    return round(60 * hit / len(qw)) if hit else 0


def _when(day, start, end) -> str:
    if not day:
        return "-"
    return f"{day} {start}-{end}" if start and end else str(day)


def candidates(payload: dict, day: str) -> list[dict]:
    """Flatten a /api/v2/plan payload into {id, kind, title, when, area} candidates for `day`."""
    d0 = dt.date.fromisoformat(day)
    near = {(d0 + dt.timedelta(days=k)).isoformat() for k in (-1, 0, 1)}
    out = []
    for ev in payload.get("events") or []:
        if ev.get("day") != day:
            continue
        eid = ev["id"] + ("@" + ev["occurrence"] if ev.get("recur") and ev.get("occurrence") else "")
        out.append({"id": eid, "kind": "event", "title": ev.get("title"), "area": ev.get("area"),
                    "when": _when(ev.get("day"), ev.get("start"), ev.get("end"))})
    for area, rows in (payload.get("tasks") or {}).items():
        for r in rows:
            if r.get("state") == "done":
                continue
            pl = r.get("planned") if isinstance(r.get("planned"), dict) else None
            if pl and pl.get("day") not in near:
                continue
            out.append({"id": r["id"], "kind": "task", "title": r.get("title"), "area": area or None,
                        "when": _when(pl.get("day"), pl.get("start"), pl.get("end")) if pl else "-"})
    return out


def find(payload: dict, query: str, day: str) -> list[dict]:
    hits = []
    for c in candidates(payload, day):
        s = score(query, c["title"])
        if s > 0:
            hits.append(dict(c, score=s))
    hits.sort(key=lambda h: (-h["score"], h["kind"] != "event", h["when"], str(h["title"])))
    return hits


def fetch_live(base: str, frm: str, to: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/api/v2/plan?from={frm}&to={to}", timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return body if isinstance(body, dict) and body.get("ok") else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def load_offline(frm: str, to: str) -> dict:
    """The plan payload built read-only from the files (the same builder the server uses, no version write)."""
    from plan import model, store
    sched = store.parse(store.read_text(store.schedule_path()), "schedule.yaml")
    tdoc = store.parse(store.read_text(store.tasks_path()), "tasks.yaml")
    return model.build(sched, tdoc, dt.date.fromisoformat(frm), dt.date.fromisoformat(to), 0, [], [])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find Plan items (events, open tasks) by name.")
    ap.add_argument("name", nargs="+", help="words of the item's title")
    ap.add_argument("--day", default=dt.date.today().isoformat(), help="YYYY-MM-DD (default today)")
    ap.add_argument("--url", default=os.environ.get("ATLAS_COCKPIT_URL") or DEFAULT_URL)
    ap.add_argument("--offline", action="store_true", help="skip the server and read the files")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")        # titles hold accents and symbols; a pipe defaults to cp1252
    try:
        d0 = dt.date.fromisoformat(a.day)
    except ValueError:
        print("--day must be YYYY-MM-DD", file=sys.stderr)
        return 2
    frm, to = (d0 - dt.timedelta(days=1)).isoformat(), (d0 + dt.timedelta(days=1)).isoformat()
    payload = None if a.offline else fetch_live(a.url, frm, to)
    if payload is None:
        try:
            payload = load_offline(frm, to)
        except Exception as exc:  # noqa: BLE001
            print(f"cannot read the Plan: {exc}", file=sys.stderr)
            return 1
    hits = find(payload, " ".join(a.name), a.day)
    if not hits:
        print("no match", file=sys.stderr)
    for h in hits:
        print("\t".join([h["id"], str(h["score"]), h["kind"], h["when"], h["area"] or "-", str(h["title"])]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
