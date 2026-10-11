"""Outside anchor for the ledger hash chain (docs/v2/contracts.md §1 "Outside anchor", docs/v2/anchor-setup.md).

    py -3.12 tools/anchor_ledger.py [--repo DIR] [--ledger DIR] [--dry-run] [--no-push]

Once a day (Windows Task Scheduler task AtlasLedgerAnchor), the hash of the last newline-terminated line of each day
file (flagged "head" while the day is still open) is appended to <clone>/anchors.jsonl, committed and pushed to the
pinned remote in anchor.json (url, branch, clone), by `git push <url> HEAD:<branch>`. The remote is a public GitHub
repo whose branch ruleset blocks force pushes and deletions.

Entry: {"day", "lines", "last_hash", "anchored_at"} (+ "head": true while the day is open). The ledger is read; the
only things written are the anchor file, the clone's status file and, on the first successful push, one
`anchor_configured` event in the ledger. Existing anchor lines are never rewritten or "corrected": if a recorded anchor
no longer matches the ledger, or the remote history of the anchor file was rewritten, the tool prints a loud message
and exits 2 without appending. Never prompts (no password, no passphrase).
Exit codes: 0 ok, 1 setup error (config, clone, git missing), 2 mismatch, 3 git commit or push failure (local commit kept)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))

from ledger2 import anchor, ledger_chain, store  # noqa: E402


def compute_new(ledger: Path, anchors: list[dict], today: str, now_iso: str) -> list[dict]:
    """Entries to append: per day file, its current (lines, last hash) unless an identical one is already anchored."""
    have = {(a["day"], a["lines"], a["last_hash"]) for a in anchors}
    out = []
    for p in ledger_chain.day_files(ledger):
        m = ledger_chain._DAY_RE.search(p.name)
        if not m:
            continue
        day = m.group(1)
        n, hs = anchor.day_summary(p)
        if n == 0 or (day, n, hs[-1]) in have:
            continue
        entry = {"day": day, "lines": n, "last_hash": hs[-1], "anchored_at": now_iso}
        if day >= today:
            entry["head"] = True
        out.append(entry)
    return out


def _text(r) -> str:
    return ((r.stderr or b"") + (r.stdout or b"")).decode("utf-8", "replace").strip()


def run(cfg: dict, ledger: Path, dry_run: bool = False, push: bool = True, now: dt.datetime | None = None) -> int:
    try:
        return _run(cfg, ledger, dry_run, push, now)
    except subprocess.SubprocessError as e:                       # includes TimeoutExpired
        print(f"ERROR: git did not finish: {e}", file=sys.stderr)
        return 3
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def _run(cfg: dict, ledger: Path, dry_run: bool, push: bool, now: dt.datetime | None) -> int:
    clone, url, branch = Path(cfg["clone"]), cfg["url"], cfg["branch"]
    now = now or dt.datetime.now().astimezone()
    today = now.date().isoformat()
    now_iso = now.isoformat(timespec="seconds")
    if not clone.is_dir() or not (clone / ".git").exists():
        print(f"ERROR: {clone} is not a git clone (see docs/v2/anchor-setup.md).", file=sys.stderr)
        return 1
    if not ledger.is_dir():
        print(f"ERROR: ledger folder {ledger} not found.", file=sys.stderr)
        return 1
    try:
        anchor.run_git(clone, "--version")
    except FileNotFoundError as e:
        print(f"ERROR: git is not available: {e}", file=sys.stderr)
        return 1
    problems = []
    if anchor.has_url_rewrite(clone):
        problems.append("the anchor clone's git config has a url.* key that could redirect the push")
    wt = anchor.read_local(clone)
    wt_norm = wt.replace(b"\r\n", b"\n") if wt is not None else None
    head = anchor.head_blob(clone)
    if head is not None and wt_norm is not None and not wt_norm.startswith(head):
        problems.append("the local anchor file differs from its committed copy")
    anchors, bad = anchor.parse_anchors(wt_norm) if wt_norm is not None else ([], [])
    problems += [f"anchors.jsonl line {n}: an anchor entry is unreadable" for n in bad]
    problems += [f"{b['day']} line {b['lines']}: {b['reason']}" for b in anchor.compare_ledger(ledger, anchors)]
    if not dry_run:
        # the remote is the source of truth: refuse to build on a rewritten or diverged anchor file
        res = anchor.fetch_and_analyse(url, branch)
        if res is None:
            print("warning: remote not reachable for the check", file=sys.stderr)
        else:
            if res["rewritten"]:
                problems.append("the anchor file was rewritten in the remote history (or the history has a merge)")
            mine = head if head is not None else wt_norm
            if mine is not None and not mine.startswith(res["bytes"]):
                problems.append("the local anchor file differs from the remote copy")
    if problems:
        print("!!! ANCHOR MISMATCH: the ledger or the anchor file no longer matches what was anchored outside. Nothing appended.", file=sys.stderr)
        for p in problems:
            print(f"!!!   {p}", file=sys.stderr)
        return 2
    new = compute_new(ledger, anchors, today, now_iso)
    for e in new:
        print(("would append " if dry_run else "append ") + json.dumps(e, separators=(",", ":")))
    if dry_run:
        print(f"dry run: {len(new)} entries, nothing written.")
        return 0
    if new:
        with open(clone / anchor.ANCHOR_FILE, "ab") as fh:
            fh.write(b"".join(json.dumps(e, separators=(",", ":")).encode("utf-8") + b"\n" for e in new))
    else:
        print("nothing new to anchor.")
    # no line-ending conversion on the anchor file, on any clone (a CRLF checkout would look like an edit)
    attr = clone / anchor.ATTR_FILE
    cur = attr.read_text(encoding="utf-8") if attr.exists() else ""
    if anchor.ATTR_LINE not in cur.splitlines():
        attr.write_text(cur + ("" if not cur or cur.endswith("\n") else "\n") + anchor.ATTR_LINE + "\n", encoding="utf-8")
    paths = [p for p in (anchor.ANCHOR_FILE, anchor.ATTR_FILE) if (clone / p).exists()]
    r = anchor.run_git(clone, "add", "--", *paths)
    if r.returncode == 0:
        st = anchor.run_git(clone, "status", "--porcelain", "--", *paths)
        if st.returncode == 0 and st.stdout.strip():              # also commits leftovers of an earlier failed run
            r = anchor.run_git(clone, "-c", "user.name=atlas-anchor", "-c", "user.email=atlas-anchor@localhost",
                               "commit", "-q", "-m", f"anchor {today}", "--", *paths)
    if r.returncode != 0:
        print(f"ERROR: git commit failed: {_text(r)}", file=sys.stderr)
        return 3
    if push:
        r = anchor.run_git(clone, "-c", "remote.pushDefault=", "push", url, f"HEAD:{branch}")
        if r.returncode != 0:
            print(f"ERROR: git push failed (local commit kept): {_text(r)}", file=sys.stderr)
            return 3
        print("pushed.")
        (clone / anchor.STATUS_FILE).write_text(json.dumps({"last_push_ok_at": now_iso}), encoding="utf-8")
        if not anchor.configured_event(ledger):
            store.append([{"ev": anchor.EVENT, "sid": "anchor-tool", "t": now_iso, "url": url}], ledger)
            print("recorded anchor_configured in the ledger.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", help="local clone of the anchor repo (default: anchor.json / ATLAS_ANCHOR_REPO)")
    ap.add_argument("--ledger", help="ledger folder (default: the Atlas ledger)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be appended, change nothing")
    ap.add_argument("--no-push", action="store_true", help="commit but do not push")
    a = ap.parse_args(argv)
    cfg = anchor.load_config()
    if cfg is None:
        print(f"ERROR: no usable anchor config at {anchor.config_path()}.", file=sys.stderr)
        return 1
    if a.repo:
        cfg["clone"] = Path(a.repo)
    ledger = Path(a.ledger) if a.ledger else store.ledger_dir()
    return run(cfg, ledger, dry_run=a.dry_run, push=not a.no_push)


if __name__ == "__main__":
    sys.exit(main())
