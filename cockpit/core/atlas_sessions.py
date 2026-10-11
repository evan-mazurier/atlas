"""Session ids of Atlas's own v1 Claude CLI calls, excluded from the live team panel.

The v1 recorder is gone; `known()` only reads the leftover json file, and returns an empty set
when it is absent or corrupt, so the exclusion degrades to nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

SESSIONS = Path(__file__).resolve().parent.parent.parent / "brain_atlas_sessions.json"


def known() -> set[str]:
    """Every session id Atlas produced. Absent or corrupt reads as empty."""
    try:
        data = json.loads(SESSIONS.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except Exception:  # noqa: BLE001
        return set()
