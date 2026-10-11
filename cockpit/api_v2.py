"""Atlas v2 API seam (docs/v2/contracts.md §5, §9).

server.py hands every /api/v2/* request here. Routing is by prefix: /api/v2/ledger/ → ledger2.api,
/api/v2/plan → plan.api, everything else → shell_api. Each module answers (code, payload) or None
("not mine" → 404). A module that fails to import, or raises, is a 500 for its own paths only.
"""
from __future__ import annotations

import importlib
import traceback


def _module_for(path: str) -> str:
    if path.startswith("/api/v2/ledger/"):
        return "ledger2.api"
    if path == "/api/v2/plan" or path.startswith("/api/v2/plan/"):
        return "plan.api"
    return "shell_api"


def _call(path: str, method: str, arg):
    name = _module_for(path)
    try:
        mod = importlib.import_module(name)
        fn = getattr(mod, method, None)
        return fn(path, arg) if fn else None
    except Exception as exc:  # noqa: BLE001 — one lane's failure never reaches another lane's paths
        tb = traceback.extract_tb(exc.__traceback__)
        where = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else "?"
        return 500, {"ok": False, "error": f"{name}: {type(exc).__name__} at {where}"}


def handle_get(path: str, query: dict):
    return _call(path, "get", query)


def handle_post(path: str, body: dict):
    return _call(path, "post", body)
