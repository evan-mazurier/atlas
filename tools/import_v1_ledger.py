"""Import the v1 dispatch ledger into the v2 ledger (contracts.md §1.6).

    py -3.12 tools/import_v1_ledger.py [--src D:\\AI\\Atlas\\ledger\\events.jsonl] [--dst <ledger>/v1/events.jsonl]

READ-ONLY on the source. The destination is overwritten on each run (idempotent). Only an allowlist of
fields survives: every free-text field (task, notes, q, note, why, error, diffstat, question, answer ...)
is dropped. Verdicts map commit-ready/needs-changes/escalate to READY/NEEDS_CHANGES/ESCALATE.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))
from ledger2 import store  # noqa: E402

DEFAULT_SRC = r"D:\AI\Atlas\ledger\events.jsonl"
ALLOW = ("t ev job project lane phase to was status verdict plan_task branch commit tokens cache_read "
         "cache_write cache_creation fresh input output model effort duration_s attempt rework findings "
         "result kind which action pass estimate type").split()
VERDICT_MAP = {"commit-ready": "READY", "needs-changes": "NEEDS_CHANGES", "escalate": "ESCALATE"}


def convert(line: dict, kept: Counter, dropped: Counter) -> dict:
    out = {"v": 1, "src": "v1"}
    for k, v in line.items():
        if k in ALLOW:
            if k == "verdict" and isinstance(v, str):
                v = VERDICT_MAP.get(v, v)
            if isinstance(v, str):
                v = v[:200]
            out[k] = v
            kept[k] += 1
        else:
            dropped[k] += 1
    return out


def run(src: Path, dst: Path) -> dict:
    kept, dropped = Counter(), Counter()
    n = bad = 0
    rows = []
    with open(src, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:  # noqa: BLE001
                bad += 1
                continue
            if not isinstance(obj, dict):
                bad += 1
                continue
            rows.append(json.dumps(convert(obj, kept, dropped), ensure_ascii=False))
            n += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8", newline="\n")
    os.replace(tmp, dst)
    return {"lines": n, "unparsed": bad, "kept": dict(kept), "dropped": dict(dropped), "dst": str(dst)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--dst", default=None)
    a = ap.parse_args(argv)
    src = Path(a.src)
    if not src.is_file():
        print(f"source not found: {src}")
        return 1
    dst = Path(a.dst) if a.dst else store.v1_file()
    rep = run(src, dst)
    print(f"imported {rep['lines']} lines ({rep['unparsed']} unparsed) -> {rep['dst']}")
    print("kept fields:   ", ", ".join(f"{k}={v}" for k, v in sorted(rep["kept"].items())))
    print("dropped fields:", ", ".join(f"{k}={v}" for k, v in sorted(rep["dropped"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
