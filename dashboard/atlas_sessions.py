"""
Atlas — the session ids of Atlas's OWN Claude CLI calls (docs/brain.md §14.10).

Every `claude` call Atlas makes returns a `session_id`, and every Claude Code memory file records
the `originSessionId` that produced it. Recording ours is what lets brain-keeper tell "the operator said
this to a control center agent" from "the operator said this in a terminal session", which is the difference
between the two readers' territories.

Without it the readers overlap: reader 2 judges a turn immediately, and reader 1 later finds the
memory file that same turn produced, so one fact arrives as two proposals with different source
ids and dedup cannot see they are the same. With it, every turn is judged exactly once by exactly
one reader, and which one is predictable rather than a race.

Deliberately its own module and deliberately tiny: substrate, the brainstormer and the worker all
record into it, and brain-keeper reads it. A shared leaf keeps that from becoming an import cycle
between layers that otherwise do not know about each other.
"""
from __future__ import annotations

import json
from pathlib import Path

# Beside the other brain-keeper ledgers: local operational state, gitignored, and losing it costs
# nothing worse than a few duplicate proposals until the ids age out anyway.
SESSIONS = Path(__file__).resolve().parent.parent / "brain_atlas_sessions.json"
KEEP = 2000          # ids age out; a memory file is judged within days of being written


def known() -> set[str]:
    """Every session id Atlas produced. Absent or corrupt reads as empty, which degrades to the
    old overlapping behaviour rather than to an error."""
    try:
        data = json.loads(SESSIONS.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:  # noqa: BLE001
        return set()


def record(session_id: str | None) -> None:
    """Note that this session was Atlas talking to itself. Never raises: it runs off the back of
    every agent call, and a bookkeeping failure must not touch the call's result."""
    try:
        sid = (session_id or "").strip()
        if not sid:
            return
        cur = []
        try:
            data = json.loads(SESSIONS.read_text(encoding="utf-8"))
            cur = data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            cur = []
        if sid in cur:
            return
        cur.append(sid)
        SESSIONS.write_text(json.dumps(cur[-KEEP:], indent=0), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
