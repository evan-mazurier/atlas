"""Ledger v2 store: paths, locked append, readers (contracts.md §1)."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path

try:
    from . import ledger_chain
except ImportError:  # pragma: no cover
    import ledger_chain  # type: ignore

DEFAULT_DIR = r"D:\AI\atlas-data\ledger"
MAX_STR = 200
MAX_LINE = 4000
LOCK_TRIES = 60          # x 50 ms = ~3 s before proceeding without the lock
CHAIN_WINDOW = 8         # verify_chain accepts a prev equal to the hash of any of the previous 8 lines

# price weights, same table as cockpit/core/usage.py (copied, contracts §1.5)
DEFAULT_WEIGHTS = [["haiku-5", 0.05], ["haiku", 0.5], ["opus", 2.0], ["sonnet", 1.0]]
HAIKU5_TIER_PROMPT = 100_000
HAIKU5_HIGH = 0.25


def ledger_dir() -> Path:
    return Path(os.environ.get("ATLAS_LEDGER2_DIR") or DEFAULT_DIR)


def model_weight(model, prompt_tokens=0, weights=None) -> float:
    m = (model or "").lower()
    for pat, w in (weights or DEFAULT_WEIGHTS):
        if pat in m:
            if pat == "haiku-5" and prompt_tokens > HAIKU5_TIER_PROMPT:
                return HAIKU5_HIGH
            return w
    return 1.0


def tier(model) -> str:
    m = (model or "").lower()
    for t in ("opus", "sonnet", "haiku"):
        if t in m:
            return t
    return "sonnet"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def clip(obj):
    """Truncate every string to 200 chars (recursively)."""
    if isinstance(obj, str):
        return obj[:MAX_STR]
    if isinstance(obj, list):
        return [clip(x) for x in obj]
    if isinstance(obj, dict):
        return {k: clip(v) for k, v in obj.items()}
    return obj


def day_file(day: str, base: Path | None = None) -> Path:
    return (base or ledger_dir()) / f"events-{day}.jsonl"


def encode(ev: dict) -> str:
    line = json.dumps(clip(ev), ensure_ascii=False)
    if len(line.encode("utf-8")) >= MAX_LINE:
        small = {k: v for k, v in clip(ev).items() if not isinstance(v, (list, dict))}
        line = json.dumps(small, ensure_ascii=False)
    return line + "\n"


@contextlib.contextmanager
def locked(base: Path | None = None):
    """Best-effort append lock: 20 tries x 50 ms, then proceed without it (never lose an event)."""
    base = base or ledger_dir()
    fh = None
    got = False
    try:
        base.mkdir(parents=True, exist_ok=True)
        fh = open(base / ".lock", "a+b")
        fh.seek(0)
        try:
            import msvcrt
            for _ in range(LOCK_TRIES):
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    got = True
                    break
                except OSError:
                    time.sleep(0.05)
        except ImportError:  # non-Windows
            try:
                import fcntl
                for _ in range(LOCK_TRIES):
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        got = True
                        break
                    except OSError:
                        time.sleep(0.05)
            except ImportError:
                pass
        yield got
    finally:
        if fh is not None:
            if got:
                try:
                    fh.seek(0)
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:  # noqa: BLE001
                    pass
            fh.close()


GENESIS = "genesis"
_TAIL = 65536


def line_hash(line: bytes) -> str:
    """sha256 (hex) of one line's bytes, without its line ending (contracts §6.4 rule A)."""
    return hashlib.sha256(line.rstrip(b"\r\n")).hexdigest()


def last_line(path: Path) -> bytes | None:
    """The last non-blank line of a file (bytes, no line ending), or None."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL))
            chunk = fh.read()
    except OSError:
        return None
    parts = chunk.split(b"\n")
    if not chunk.endswith(b"\n") and len(parts) > 1:
        parts = parts[:-1]                        # a line still being written by another process
    lines = [x for x in parts if x.strip()]
    if size > _TAIL and len(lines) <= 1:
        try:                                    # a single huge line: read it whole
            lines = [x for x in path.read_bytes().split(b"\n") if x.strip()]
        except OSError:
            return None
    return lines[-1].rstrip(b"\r") if lines else None


def prev_for_day(day: str, base: Path) -> str:
    """The `prev` value for the next line of `day`: hash of that file's last line, else of the last line of the
    nearest earlier day file, else "genesis"."""
    ln = last_line(day_file(day, base))
    if ln is not None:
        return line_hash(ln)
    earlier = [p for p in day_files(base) if p.stem[len("events-"):] < day]
    for p in reversed(earlier):
        ln = last_line(p)
        if ln is not None:
            return line_hash(ln)
    return GENESIS


def append_bytes(path, data: bytes) -> None:
    """ONE write() of the whole line on an O_APPEND handle, so appends that proceed without the lock still land
    at the end of the file instead of overwriting each other."""
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def append_events(events: list[dict], base: Path | None = None, nolock: bool = False) -> None:
    """Append events (each ONE write of the whole line), chaining each line to the previous one
    (`prev`). Caller may already hold the lock."""
    base = base or ledger_dir()
    base.mkdir(parents=True, exist_ok=True)
    for ev in events:
        ev.setdefault("v", 2)
        ev.setdefault("t", now_iso())
        day = str(ev["t"])[:10]
        ev["prev"] = prev_for_day(day, base)
        if nolock:
            ev["nolock"] = True
        append_bytes(day_file(day, base), encode(ev).encode("utf-8"))


def append(events, base: Path | None = None) -> None:
    if isinstance(events, dict):
        events = [events]
    with locked(base) as got:
        append_events(events, base, nolock=not got)


def read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def day_files(base: Path | None = None) -> list[Path]:
    base = base or ledger_dir()
    try:
        return sorted(base.glob("events-????-??-??.jsonl"))
    except OSError:
        return []


def read_events(day_from: str | None = None, day_to: str | None = None,
                base: Path | None = None) -> list[dict]:
    """v2 events from the day files in [day_from, day_to] (inclusive, YYYY-MM-DD), file order."""
    out = []
    for p in day_files(base):
        day = p.stem[len("events-"):]
        if day_from and day < day_from:
            continue
        if day_to and day > day_to:
            continue
        out.extend(read_jsonl(p))
    return out


def verify_chain(base: Path | None = None) -> dict:
    """Walk every day file in order and check each line's `prev` against the hash of the line before it
    (across day files). Lines written before the chain existed (no `prev`) are accepted until the first
    chained line; after that a line without `prev` is a break. Returns
    {"intact": bool, "breaks": [{"file", "line"}], "files": n, "lines": n}; a break is reported at the first
    line that no longer matches (a deleted or edited line shows at the line after it)."""
    res = ledger_chain.scan(base or ledger_dir())
    return {"intact": not res["breaks"], "breaks": [{"file": b["file"], "line": b["line"]} for b in res["breaks"]],
            "files": res["files"], "lines": res["lines"]}


def v1_file(base: Path | None = None) -> Path:
    return (base or ledger_dir()) / "v1" / "events.jsonl"


def read_v1(base: Path | None = None) -> list[dict]:
    return read_jsonl(v1_file(base))


def auto_dir(base: Path | None = None) -> Path:
    return (base or ledger_dir()) / "auto"


def signature(base: Path | None = None) -> tuple:
    """Cheap change detector over every file the readers use."""
    sig = []
    for p in day_files(base) + [v1_file(base)]:
        try:
            st = p.stat()
            sig.append((p.name, st.st_size, st.st_mtime_ns))
        except OSError:
            pass
    ad = auto_dir(base)
    try:
        sig.append(("auto", tuple(sorted(x.name for x in ad.glob("*.json")))))
    except OSError:
        pass
    return tuple(sig)
