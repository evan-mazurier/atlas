"""Plan a day from a chat: show the calendar, apply a list of edits (contracts.md section 5.3).

    py -3.12 tools/plan_day.py show [--day YYYY-MM-DD] [--days N] [--url URL]
    py -3.12 tools/plan_day.py apply <ops.json|-> [--dry-run]

Talks to the live cockpit (`--url` / ATLAS_COCKPIT_URL, default http://127.0.0.1:8765) through the Plan API;
it never writes a file itself, so every guard of the Plan engine (window, all-day rule, attention) applies.

show   one line per event, tab separated: day, start-end (or all-day), area short, attention, id, title.
       A recurring occurrence has the id `evt_xxxxxxxx@YYYY-MM-DD`. A done event gets a final column `done`.
apply  a JSON list of ops, applied in order, each with a fresh base (the version is read before each write):
         {"op":"add","title","day","start","end","area"?,"attention"?,"notes"?}
         {"op":"move","id","day","start","end","occurrence"?}
         {"op":"delete","id"}            (an id `evt_x@DAY` skips that one occurrence)
         {"op":"retitle","id","title"}
         {"op":"done","id"}              (manual mark, own tracking only; `evt_x@DAY` marks one occurrence)
         {"op":"undone","id"}            (removes the mark)
       An id written `evt_xxxxxxxx@YYYY-MM-DD` is split into id + occurrence. Stops at the first refusal,
       prints which ops were applied and the server's reason verbatim; exit 1 then, 0 when all applied.
       --dry-run prints the requests that would be sent and touches nothing (no network).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"
ADD_FIELDS = ("title", "day", "start", "end", "area", "attention", "notes")


class PlanDayError(Exception):
    pass


def split_id(raw) -> tuple[str, str | None]:
    s = str(raw or "").strip()
    if "@" in s:
        eid, occ = s.split("@", 1)
        return eid, occ
    return s, None


def build_request(op: dict) -> tuple[str, dict]:
    """One op -> (route, body without base). Raises PlanDayError on a malformed op."""
    kind = op.get("op") if isinstance(op, dict) else None
    if kind == "add":
        missing = [k for k in ("title", "day", "start", "end") if not op.get(k)]
        if missing:
            raise PlanDayError(f"add needs {', '.join(missing)}")
        return "event", {"event": {k: op[k] for k in ADD_FIELDS if op.get(k) not in (None, "")}}
    if kind == "move":
        eid, occ = split_id(op.get("id"))
        occ = op.get("occurrence") or occ
        if not eid or not op.get("day"):
            raise PlanDayError("move needs id and day")
        body = {"id": eid, "day": op["day"]}
        for k in ("start", "end"):
            if op.get(k):
                body[k] = op[k]
        if occ:
            body["occurrence"] = occ
        return "event/move", body
    if kind == "delete":
        eid, occ = split_id(op.get("id"))
        if not eid:
            raise PlanDayError("delete needs id")
        if occ:
            return "event/skip", {"id": eid, "occurrence": occ}
        return "event/delete", {"id": eid}
    if kind == "retitle":
        eid, _ = split_id(op.get("id"))
        if not eid or not str(op.get("title") or "").strip():
            raise PlanDayError("retitle needs id and title")
        return "event", {"event": {"id": eid, "title": op["title"]}}
    if kind in ("done", "undone"):
        eid, occ = split_id(op.get("id"))
        if not eid:
            raise PlanDayError(f"{kind} needs id")
        body = {"id": eid, "done": kind == "done"}
        if op.get("occurrence") or occ:
            body["occurrence"] = op.get("occurrence") or occ
        return "event/done", body
    raise PlanDayError(f"unknown op {kind!r} (add, move, delete, retitle, done, undone)")


def _call(url: str, method: str, route: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + route, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return exc.code, {"ok": False, "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        raise PlanDayError(f"cannot reach the cockpit at {url}: {exc}") from exc


def get_plan(url: str, frm: str, to: str) -> dict:
    code, out = _call(url, "GET", f"/api/v2/plan?from={frm}&to={to}")
    if code != 200 or not out.get("ok"):
        raise PlanDayError(f"cannot read the Plan: {out.get('error') or code}")
    return out


def show_lines(payload: dict) -> list[str]:
    short = {a["id"]: a.get("short") or a["id"].upper() for a in payload.get("areas") or []}
    lines = []
    for e in payload.get("events") or []:
        eid = e["id"] + ("@" + e["occurrence"] if e.get("recur") and e.get("occurrence") else "")
        when = f"{e['start']}-{e['end']}" if e.get("start") and e.get("end") else "all-day"
        cols = [str(e.get("day")), when, short.get(e.get("area"), "-"),
                str(e.get("attention") or "full"), eid, str(e.get("title"))]
        if e.get("done"):
            cols.append("done")
        lines.append("\t".join(cols))
    return lines


def cmd_show(url: str, day: str, days: int) -> int:
    d0 = dt.date.fromisoformat(day)
    payload = get_plan(url, day, (d0 + dt.timedelta(days=max(1, days) - 1)).isoformat())
    for ln in show_lines(payload):
        print(ln)
    return 0


def cmd_apply(url: str, source: str, dry: bool) -> int:
    text = sys.stdin.read() if source == "-" else open(source, encoding="utf-8").read()
    ops = json.loads(text)
    if not isinstance(ops, list):
        raise PlanDayError("the ops file must hold a JSON list")
    reqs = [build_request(op) for op in ops]            # a malformed op fails before anything is sent
    if dry:
        for i, (route, body) in enumerate(reqs, 1):
            print(f"{i}. POST /api/v2/plan/{route} {json.dumps({'base': '<fresh>', **body}, ensure_ascii=False)}")
        return 0
    today = dt.date.today().isoformat()
    done = 0
    for i, (route, body) in enumerate(reqs, 1):
        base = get_plan(url, today, today)["version"]
        code, out = _call(url, "POST", "/api/v2/plan/" + route, {"base": base, **body})
        if code != 200 or not out.get("ok"):
            print(f"applied {done} of {len(reqs)}; op {i} ({ops[i - 1].get('op')}) refused: {out.get('error') or code}")
            return 1
        done += 1
        ch = out.get("changed") or {}
        print(f"{i}. {ops[i - 1].get('op')} ok {ch.get('id', '')} {ch.get('title', '')}".rstrip())
    print(f"applied {done} of {len(reqs)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Show and edit the Plan calendar through the cockpit API.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=os.environ.get("ATLAS_COCKPIT_URL") or DEFAULT_URL)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", parents=[common])
    s.add_argument("--day", default=dt.date.today().isoformat())
    s.add_argument("--days", type=int, default=1)
    a = sub.add_parser("apply", parents=[common])
    a.add_argument("file", help="ops JSON file, or - for stdin")
    a.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        if args.cmd == "show":
            dt.date.fromisoformat(args.day)
            return cmd_show(args.url, args.day, args.days)
        return cmd_apply(args.url, args.file, args.dry_run)
    except (PlanDayError, ValueError, OSError) as exc:
        print(f"plan_day: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
