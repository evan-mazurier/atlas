"""Switch the installed Atlas hooks between record-only and enforce (contracts.md §6.7).

    py -3.12 tools/hooks_mode.py record|enforce

Edits the installed `atlas_config.json` (ATLAS_HOOKS_DIR, default ~/.claude/hooks/atlas) and appends a
`mode` event to the ledger (ATLAS_LEDGER2_DIR) so the hooks' tamper check knows the change was made here.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cockpit"))
from ledger2 import store  # noqa: E402


def hooks_dir() -> Path:
    return Path(os.environ.get("ATLAS_HOOKS_DIR")
                or Path(os.path.expanduser("~")) / ".claude" / "hooks" / "atlas")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in ("record", "enforce"):
        print("usage: hooks_mode.py record|enforce")
        return 2
    new = args[0]
    cfg_path = hooks_dir() / "atlas_config.json"
    if not cfg_path.is_file():
        print(f"no installed config at {cfg_path} (run tools/install_hooks.py first)")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    old = cfg.get("mode", "record")
    cfg["mode"] = new
    # the record first, the config second: a hook that sees the new mode always finds the matching event
    base = store.ledger_dir()
    with store.locked(base):          # the hook's tamper check runs under the same lock
        sd = base / "state"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "last_mode.json").write_text(json.dumps({"mode": new, "tamper_for": None}), encoding="utf-8")
        store.append_events([{"ev": "mode", "sid": "hooks_mode", "cwd": "", "from": old, "to": new, "mode": new}], base)
        tmp = cfg_path.with_name(cfg_path.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, cfg_path)
    print(f"hooks mode: {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
