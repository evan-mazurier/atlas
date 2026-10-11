"""Plan engine file layer (contracts.md section 5.3): paths, locks, atomic writes, section splicing.

Rules held here:
* a write is read-modify-write under a process lock AND an OS lock on `<file>.lock`;
* the bytes go to `<file>.tmp` then `os.replace`, retried on PermissionError;
* a file that does not parse is never written (v1 returned [] and a save then wiped every event);
* only the touched top-level sections are replaced: every other byte of the file is kept.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import threading
import time
from pathlib import Path

import yaml

try:                                    # the Windows OS lock; absent elsewhere (process lock only)
    import msvcrt
except ImportError:                     # pragma: no cover
    msvcrt = None

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VERSION_FILE = r"D:\AI\atlas-data\plan-version.json"

_PROCESS_LOCK = threading.RLock()
_LOCK_TRIES, _LOCK_WAIT = 20, 0.05
_REPLACE_TRIES, _REPLACE_WAIT = 10, 0.05

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class PlanError(Exception):
    """An error that maps to an HTTP answer: code, one-line message, optional extra payload keys."""

    def __init__(self, code: int, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


# ── paths (read at call time so tests can point the env at temp copies) ─────────────────────────
def schedule_path() -> Path:
    return Path(os.environ.get("ATLAS_SCHEDULE_FILE") or ROOT / "schedule.yaml")


def tasks_path() -> Path:
    return Path(os.environ.get("ATLAS_TASKS_FILE") or ROOT / "tasks.yaml")


def version_path() -> Path:
    return Path(os.environ.get("ATLAS_PLAN_VERSION_FILE") or DEFAULT_VERSION_FILE)


# ── normalisation ──────────────────────────────────────────────────────────────────────────────
def hhmm_from_int(v: int) -> str:
    """YAML 1.1 reads an unquoted `10:00` as the sexagesimal int 600: back to "HH:MM"."""
    return f"{v // 60:02d}:{v % 60:02d}"


def normalise(obj, key: str | None = None):
    """Dates become ISO strings (also as dict keys); unquoted int times under start/end become
    "HH:MM". Returns new containers; leaves everything else as it was."""
    if isinstance(obj, dict):
        return {_nkey(k): normalise(v, _nkey(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalise(v, key) for v in obj]
    if isinstance(obj, dt.datetime):
        return obj.isoformat()
    if isinstance(obj, dt.date):
        return obj.isoformat()
    if key in ("start", "end") and isinstance(obj, int) and not isinstance(obj, bool):
        return hhmm_from_int(obj)
    return obj


def _nkey(k):
    if isinstance(k, dt.date):
        return k.isoformat()
    return k


# ── reading ────────────────────────────────────────────────────────────────────────────────────
def read_text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None


def parse(text: str | None, label: str) -> dict:
    """Parse + normalise. Empty/missing text is an empty document; a parse error or a non-mapping
    top level raises PlanError(500)."""
    if text is None or not text.strip():
        return {}
    try:
        data = yaml.load(text, Loader=_LOADER)
    except yaml.YAMLError as exc:
        raise PlanError(500, f"{label} does not parse; refusing to touch it ({type(exc).__name__})")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise PlanError(500, f"{label} is not a mapping at the top level; refusing to touch it")
    return normalise(data)


def read_doc(path: Path) -> tuple[dict, str | None]:
    text = read_text(path)
    return parse(text, path.name), text


# ── locks ──────────────────────────────────────────────────────────────────────────────────────
@contextlib.contextmanager
def locked(path: Path):
    """Process lock + OS lock on `<path>.lock` (LK_NBLCK, 20 x 50 ms, else 503)."""
    with _PROCESS_LOCK:
        fh = None
        got = False
        if msvcrt is not None:
            lock_path = Path(str(path) + ".lock")
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(lock_path, "a+b")
            except OSError as exc:
                raise PlanError(503, f"cannot open lock file ({type(exc).__name__})")
            for _ in range(_LOCK_TRIES):
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    got = True
                    break
                except OSError:
                    time.sleep(_LOCK_WAIT)
            if not got:
                fh.close()
                raise PlanError(503, "plan file busy (lock not obtained)")
        try:
            yield
        finally:
            if fh is not None:
                with contextlib.suppress(OSError):
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                fh.close()


def atomic_write(path: Path, data: bytes) -> None:
    """`<path>.tmp` then os.replace, retried on PermissionError (another process has the file open)."""
    tmp = Path(str(path) + ".tmp")
    try:
        tmp.write_bytes(data)
    except OSError as exc:
        raise PlanError(503, f"cannot write {path.name} ({type(exc).__name__})")
    for i in range(_REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(_REPLACE_WAIT)
        except OSError as exc:
            raise PlanError(503, f"cannot replace {path.name} ({type(exc).__name__})")
    with contextlib.suppress(OSError):
        tmp.unlink()
    raise PlanError(503, f"{path.name} is locked by another process; nothing written")


# ── section splicing ───────────────────────────────────────────────────────────────────────────
_KEY_RE = re.compile(r"^[A-Za-z_][\w.-]*:", re.MULTILINE)
_COMMENT_OR_BLANK = re.compile(r"^[ \t\r]*(#.*)?\n?$")


def section_region(text: str, key: str) -> tuple[int | None, int | None]:
    """(start, end) offsets of the top-level `key:` section. It ends where the next top-level key's
    leading comment/blank preamble begins (so the explanatory comment above the next key stays with
    that key). (None, None) when the key is absent. Handles `key: []` and the block form."""
    m = re.search(rf"^{re.escape(key)}:.*\n?", text, re.MULTILINE)
    if not m:
        return None, None
    body_start = m.end()
    nxt = _KEY_RE.search(text, body_start)
    end = nxt.start() if nxt else len(text)
    lines = text[body_start:end].splitlines(keepends=True)
    while lines and _COMMENT_OR_BLANK.match(lines[-1]):
        end -= len(lines.pop())
    return m.start(), end


def dump_section(key: str, value, crlf: bool = False) -> str:
    block = yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True)
    return block.replace("\n", "\r\n") if crlf else block


def splice(text: str, sections: dict) -> str:
    """Return `text` with each named top-level section replaced by the re-serialised value; every
    other byte is unchanged. A section not in the file is appended at the end."""
    crlf = "\r\n" in text
    nl = "\r\n" if crlf else "\n"
    for key, value in sections.items():
        block = dump_section(key, value, crlf)
        start, end = section_region(text, key)
        if start is None:
            text = (text.rstrip("\r\n") + nl + nl + block) if text.strip() else block
        else:
            tail = text[end:]
            if tail and not block.endswith("\n"):
                block += nl
            text = text[:start] + block + tail
    return text if text.endswith("\n") else text + nl
