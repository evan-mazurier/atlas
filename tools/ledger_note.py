"""Attach a human note to a bypass attempt (contracts.md §1.2 `bypass_note`).

    py -3.12 tools/ledger_note.py --at <t of the attempt> --sid <session id> --text "..."

Appends {"ev":"bypass_note","ref":"<t>|<sid>","text":...} to the ledger through the store's locked, chained
append (ATLAS_LEDGER2_DIR overrides the folder). The attempt must exist; the note shows under its report in the
Ledger screen. The ledger is never edited: every note for an attempt is shown, oldest first, with its time and author. Only the
main session may run this tool (the hook refuses it to helpers and logs the try as a bypass attempt).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))
from ledger2 import store  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Attach a note to a bypass attempt in the ledger.")
    ap.add_argument("--at", required=True, help="the attempt's `t` exactly as the Ledger shows it")
    ap.add_argument("--sid", required=True, help="the attempt's session id")
    ap.add_argument("--text", required=True, help="the note (kept to 200 characters)")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    text = a.text.strip()
    if not text:
        print("the note is empty")
        return 2
    day = str(a.at)[:10]
    found = any(e.get("ev") == "bypass_attempt" and e.get("t") == a.at and e.get("sid") == a.sid
                for e in store.read_events(day_from=day, day_to=day))
    if not found:
        print(f"no bypass attempt at {a.at} in session {a.sid}")
        return 1
    # `by`: the hook refuses this tool to helpers (contracts 1.2), so a note is always the main session's
    store.append({"ev": "bypass_note", "ref": f"{a.at}|{a.sid}", "sid": a.sid, "by": "main", "text": text[:store.MAX_STR]})
    print("note added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
