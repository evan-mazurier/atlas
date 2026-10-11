"""plan-version.json: `{version, objects: {key: {v, sha}}}` (contracts.md section 5.3).

Keys: evt:<id>, plc:<session>, blk:<id>, task:<id>, rv:<task_id>. On every load, an object whose
content hash differs from the stored one (an edit from outside) is bumped to `version + 1`.
A write whose object has `v > base` is a 409; base missing is a 400; other objects never conflict.
"""
from __future__ import annotations

import hashlib
import json

from . import store
from .store import PlanError


def sha(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class State:
    def __init__(self, version: int = 1, objects: dict | None = None, fresh: bool = False):
        self.version = version
        self.objects = objects if objects is not None else {}
        self.fresh = fresh

    def to_json(self) -> dict:
        return {"version": self.version, "objects": self.objects}


def load() -> State:
    path = store.version_path()
    text = store.read_text(path)
    if text is not None:
        try:
            raw = json.loads(text)
            objs = raw.get("objects")
            ver = raw.get("version")
            if isinstance(objs, dict) and isinstance(ver, int) and not isinstance(ver, bool):
                clean = {k: {"v": int(o.get("v", 0)), "sha": str(o.get("sha", ""))}
                         for k, o in objs.items() if isinstance(o, dict)}
                return State(ver, clean)
        except (ValueError, AttributeError, TypeError):
            pass
        raise PlanError(500, f"{path.name} does not parse; refusing to reset the plan version")
    return State(1, {}, fresh=True)           # no file yet: first run, baseline silently


def save(state: State) -> None:
    path = store.version_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PlanError(503, f"cannot create the version file folder ({type(exc).__name__})")
    store.atomic_write(path, json.dumps(state.to_json(), ensure_ascii=False).encode("utf-8"))


def detect(state: State, prefixes: tuple, current: dict, quiet_new: tuple = ()) -> bool:
    """Compare the content hash of every `current` object (key -> object) with the stored one, for
    the key namespaces in `prefixes` only. Changed or new objects get `version + 1` (new keys under
    `quiet_new` are stamped with the current version instead: they come from the ledger, not from a
    hand edit); vanished ones are dropped and also bump. A first run baselines everything without a
    bump. Returns True when `state` changed (the caller saves it)."""
    bump = False
    quiet = []
    changed = []
    for key, obj in current.items():
        s = sha(obj)
        have = state.objects.get(key)
        if have is not None and have["sha"] == s:
            continue
        if have is None and (state.fresh or key.startswith(quiet_new)):
            quiet.append((key, s))
        else:
            changed.append((key, s))
    gone = [k for k in state.objects if k.startswith(prefixes) and k not in current]
    if changed or gone:
        bump = not state.fresh
    new_ver = state.version + 1 if bump else state.version
    for key, s in quiet:
        state.objects[key] = {"v": state.version, "sha": s}
    for key, s in changed:
        state.objects[key] = {"v": new_ver, "sha": s}
    for key in gone:
        del state.objects[key]
    was_fresh = state.fresh
    state.version = new_ver
    state.fresh = False
    return bool(quiet or changed or gone or was_fresh)


def base_of(body: dict) -> int:
    base = body.get("base")
    if base is None or isinstance(base, bool) or not isinstance(base, int):
        raise PlanError(400, "base (the version the client last saw) is required")
    return base


def check(state: State, keys, base: int, current: dict) -> None:
    """409 when any of `keys` was changed after `base`."""
    for key in keys:
        o = state.objects.get(key)
        if o is not None and o["v"] > base:
            raise PlanError(409, f"{key} changed since version {base}", current=current.get(key),
                            version=state.version)


def commit(state: State, touched: dict, removed=()) -> int:
    """Bump the global version and stamp the written objects. `touched` = key -> stored object."""
    state.version += 1
    state.fresh = False
    for key, obj in touched.items():
        state.objects[key] = {"v": state.version, "sha": sha(obj)}
    for key in removed:
        state.objects.pop(key, None)
    return state.version
