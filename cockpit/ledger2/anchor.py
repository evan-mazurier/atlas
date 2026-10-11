"""Outside anchor for the ledger hash chain (docs/v2/contracts.md §1 "Outside anchor", docs/v2/anchor-setup.md).

Stdlib only. The anchor set is NOT read from the local file alone: the pinned remote (anchor.json, versioned in the Atlas
repo) is fetched, in a background thread (cached, TTL 10 minutes, never on a request path), into a FRESH temporary bare
repo (so nothing in the local clone's config can redirect or rewrite the fetch), and the whole remote history of
anchors.jsonl is checked: no merge commits, and every version must be a byte-prefix of the next one. The ledger is then
compared with the anchors. Once the tool has pushed for the first time it writes an `anchor_configured` event to the
ledger; from then on a missing clone, file or config, an empty anchor set, or a disabled check is itself a finding.

Config (anchor.json at the Atlas root, or the file named by ATLAS_ANCHOR_CONFIG): {"url", "branch", "clone"}. The local
clone path can be overridden with ATLAS_ANCHOR_REPO. ATLAS_ANCHOR_DISABLE=1 turns the check off (tests); once
`anchor_configured` exists, a disabled or overridden check shows as amber "check disabled".

This raises the bar against accidental or clumsy tampering. It is not proof against an agent that edits Atlas itself, the
SSH configuration, or drives a browser logged in to GitHub (docs/v2/anchor-setup.md "Limits")."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat as _stat
import subprocess
import tempfile
import threading
from pathlib import Path

try:
    from . import ledger_chain
except ImportError:  # pragma: no cover
    import ledger_chain  # type: ignore

ANCHOR_FILE = "anchors.jsonl"
ATTR_FILE = ".gitattributes"
ATTR_LINE = "anchors.jsonl -text"
STATUS_FILE = ".anchor-status.json"       # written by the tool inside the clone: {"last_push_ok_at": ISO}
SEEN_FILE = ".anchor-remote-seen.json"    # written by the cockpit check: {"last_remote_ok_at": ISO}
EVENT = "anchor_configured"
TTL_SECONDS = 600
GIT_TIMEOUT = 60
MAX_PROBLEMS = 50
ALLOW_FILE_PROTOCOL = False               # tests only: lets the pinned "url" be a local bare repo path

_DAY_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_lock = threading.Lock()
_state: dict = {"gen": 0, "checked_at": None, "running": False, "result": None, "unreachable_since": None,
                "head": None, "url_rewrite": False}


# ------------------------------------------------------------------ config and git

def disabled() -> bool:
    return os.environ.get("ATLAS_ANCHOR_DISABLE") == "1"


def weakened() -> bool:
    """The check is switched off or pointed at another config through the environment."""
    return disabled() or bool(os.environ.get("ATLAS_ANCHOR_CONFIG"))


def config_path() -> Path:
    return Path(os.environ.get("ATLAS_ANCHOR_CONFIG") or (Path(__file__).resolve().parents[2] / "anchor.json"))


def load_config() -> dict | None:
    """{"url", "branch", "clone"} or None when the config file is missing or unusable."""
    try:
        c = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(c, dict) or not isinstance(c.get("url"), str) or not c["url"]:
        return None
    clone = os.environ.get("ATLAS_ANCHOR_REPO") or c.get("clone") or r"D:\AI\atlas-anchor"
    return {"url": c["url"], "branch": str(c.get("branch") or "main"), "clone": Path(clone)}


def git_env() -> dict:
    """No system or global git config (no url rewrites, no helpers), never a prompt, and a FORCED non-interactive ssh."""
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def git_prefix() -> list[str]:
    p = ["git", "-c", "protocol.allow=never", "-c", "protocol.ssh.allow=always", "-c", "core.autocrlf=false"]
    if ALLOW_FILE_PROTOCOL:
        p += ["-c", "protocol.file.allow=always"]
    return p


def run_git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT, input: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(git_prefix() + list(args), cwd=str(cwd), capture_output=True, env=git_env(),
                          timeout=timeout, input=input)


def has_url_rewrite(clone: Path) -> bool:
    """Any `url.*` key in the clone's own config (insteadOf / pushInsteadOf) could redirect a push: treated as tampering."""
    r = run_git(clone, "config", "--local", "--get-regexp", r"^url\.")
    return r.returncode == 0 and bool(r.stdout.strip())


def head_blob(clone: Path) -> bytes | None:
    """anchors.jsonl as committed in the clone's HEAD (None when HEAD has no such file)."""
    r = run_git(clone, "show", f"HEAD:{ANCHOR_FILE}")
    return r.stdout if r.returncode == 0 else None


# ------------------------------------------------------------------ entries

def valid_entry(o) -> bool:
    if not isinstance(o, dict):
        return False
    day, n, h, at = o.get("day"), o.get("lines"), o.get("last_hash"), o.get("anchored_at")
    if not (isinstance(day, str) and _DAY_RE.match(day)):
        return False
    try:
        dt.date.fromisoformat(day)
        dt.datetime.fromisoformat(at)
    except (ValueError, TypeError):
        return False
    return (isinstance(n, int) and not isinstance(n, bool) and n > 0
            and isinstance(h, str) and bool(_HASH_RE.match(h)))


def parse_anchors(raw: bytes) -> tuple[list[dict], list[int]]:
    """(valid entries, line numbers of unreadable lines). Nothing is dropped silently: a line that is not a valid entry,
    or a final line without its newline, is reported."""
    entries, bad = [], []
    parts = raw.split(b"\n")
    for i, ln in enumerate(parts, 1):
        if not ln.strip():
            continue
        try:
            o = json.loads(ln.decode("utf-8"))
        except ValueError:
            o = None
        if valid_entry(o) and i < len(parts):         # the last part has no newline after it
            entries.append(o)
        else:
            bad.append(i)
    return entries, bad


def read_local(clone: Path) -> bytes | None:
    try:
        return (Path(clone) / ANCHOR_FILE).read_bytes()
    except OSError:
        return None


def day_summary(path: Path) -> tuple[int, list[str]]:
    """(count, hashes) of the NEWLINE-TERMINATED non-blank lines of a day file (same rule as store.last_line): a final
    segment without its newline may be a write in progress and is never anchored."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return 0, []
    hs = [ledger_chain.line_hash(ln) for ln in raw.split(b"\n")[:-1] if ln.strip()]
    return len(hs), hs


def compare_ledger(base, anchors: list[dict]) -> list[dict]:
    """Every entry whose line is gone or whose hash differs now. An entry says: the Nth non-blank line of that day file
    had this hash. That stays true for a day that keeps growing, so head entries are checked the same way."""
    hashes: dict[str, list[str]] = {}
    out = []
    for a in anchors:
        day = a["day"]
        if day not in hashes:
            hashes[day] = day_summary(Path(base) / f"events-{day}.jsonl")[1]
        hs = hashes[day]
        n = a["lines"]
        if n > len(hs):
            reason = "the anchored line is gone"
        elif hs[n - 1] != a["last_hash"]:
            reason = "the anchored line was changed"
        else:
            continue
        when = str(a.get("anchored_at") or "")[:10] or "an unknown date"
        out.append({"day": day, "lines": n, "reason": reason, "verdict": "tampering",
                    "summary": f"Looks like tampering: the record of {day} no longer matches its outside anchor of {when}."})
    return out[:MAX_PROBLEMS]


def problem(summary: str, reason: str = "", verdict: str = "tampering") -> dict:
    return {"day": None, "lines": None, "reason": reason, "verdict": verdict, "summary": summary}


# ------------------------------------------------------------------ the remote

def _versions(repo: Path, commits: list[str]) -> list[bytes | None]:
    """anchors.jsonl at each commit (None where the file is absent), read with ONE `git cat-file --batch` process."""
    if not commits:
        return []
    req = "".join(f"{c}:{ANCHOR_FILE}\n" for c in commits).encode()
    r = run_git(repo, "cat-file", "--batch", input=req)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or "git cat-file failed")
    out, buf, pos = [], r.stdout, 0
    for _ in commits:
        nl = buf.index(b"\n", pos)
        head = buf[pos:nl].split()
        pos = nl + 1
        if len(head) == 3 and head[1] == b"blob":
            size = int(head[2])
            out.append(buf[pos:pos + size])
            pos += size + 1
        else:
            out.append(None)
    return out


def analyse_remote(repo: Path) -> dict:
    """After a successful fetch (FETCH_HEAD) in `repo`: the latest anchors.jsonl, the newest commit time, and whether the
    history was rewritten: a merge commit anywhere, a deletion of the file, or a version that is not a byte-prefix of the
    next one."""
    m = run_git(repo, "rev-list", "--merges", "FETCH_HEAD")
    if m.returncode != 0:
        raise RuntimeError(m.stderr.decode("utf-8", "replace").strip() or "git rev-list failed")
    rewritten = bool(m.stdout.strip())
    r = run_git(repo, "rev-list", "--reverse", "--full-history", "FETCH_HEAD", "--", ANCHOR_FILE)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip() or "git rev-list failed")
    vers = _versions(repo, r.stdout.decode().split())
    if any(v is None for v in vers):
        rewritten = True
    vs = [v or b"" for v in vers]
    for a, b in zip(vs, vs[1:]):
        if not b.startswith(a):
            rewritten = True
    t = run_git(repo, "log", "-1", "--format=%cI", "FETCH_HEAD")
    return {"bytes": vs[-1] if vs else b"", "last_commit_at": t.stdout.decode().strip() or None, "rewritten": rewritten}


def fetch_and_analyse(url: str, branch: str) -> dict | None:
    """Fetch the pinned remote into a fresh temporary bare repo and analyse it; None when it cannot be reached."""
    tmp = Path(tempfile.mkdtemp(prefix="atlas-anchor-"))
    try:
        if run_git(tmp, "init", "--bare", "-q", str(tmp)).returncode != 0:
            return None
        if run_git(tmp, "fetch", "--quiet", url, branch).returncode != 0:
            return None
        return analyse_remote(tmp)
    finally:
        def _chmod(fn, p, _exc):
            os.chmod(p, _stat.S_IWRITE)
            fn(p)
        shutil.rmtree(tmp, onerror=_chmod)


def _seen_path(clone: Path) -> Path:
    return Path(clone) / SEEN_FILE


def refresh_remote(now: dt.datetime | None = None) -> None:
    """Fetch and analyse the pinned remote (blocking). Updates the shared state; never raises and always clears
    `running`."""
    now = now or dt.datetime.now().astimezone()
    result = head = None
    url_rewrite = False
    clone = None
    try:
        cfg = load_config()
        if cfg and cfg["clone"].is_dir() and (cfg["clone"] / ".git").exists():
            clone = cfg["clone"]
            try:
                url_rewrite = has_url_rewrite(clone)
                head = head_blob(clone)
            except (OSError, subprocess.SubprocessError):
                pass
            result = fetch_and_analyse(cfg["url"], cfg["branch"])
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        result = None
    finally:
        since_file = None
        if clone is not None and result is not None:
            try:
                _seen_path(clone).write_text(json.dumps({"last_remote_ok_at": now.isoformat(timespec="seconds")}),
                                             encoding="utf-8")
            except OSError:
                pass
        elif clone is not None:
            try:
                since_file = json.loads(_seen_path(clone).read_text(encoding="utf-8")).get("last_remote_ok_at")
            except (OSError, ValueError, AttributeError):
                since_file = None
        with _lock:
            _state["gen"] += 1
            _state["checked_at"] = now
            _state["running"] = False
            _state["result"] = result
            _state["head"] = head
            _state["url_rewrite"] = url_rewrite
            if result is not None:
                _state["unreachable_since"] = None
            elif since_file:
                _state["unreachable_since"] = since_file
            elif _state["unreachable_since"] is None:
                _state["unreachable_since"] = now.isoformat(timespec="seconds")


def ensure_fresh(now: dt.datetime | None = None) -> None:
    """Start a background fetch when the cached result is older than the TTL (or absent). Never blocks the caller."""
    if disabled() or load_config() is None:
        return
    now = now or dt.datetime.now().astimezone()
    with _lock:
        if _state["running"]:
            return
        at = _state["checked_at"]
        if at is not None and (now - at).total_seconds() < TTL_SECONDS:
            return
        _state["running"] = True
    try:
        threading.Thread(target=refresh_remote, args=(now,), daemon=True).start()
    except Exception:  # noqa: BLE001
        with _lock:
            _state["running"] = False


def reset_state() -> None:
    with _lock:
        _state.update({"gen": 0, "checked_at": None, "running": False, "result": None, "unreachable_since": None,
                       "head": None, "url_rewrite": False})


def state_key() -> tuple:
    cfg = load_config()
    local = None
    if cfg:
        try:
            st = (cfg["clone"] / ANCHOR_FILE).stat()
            local = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    with _lock:
        return (str(cfg and cfg["clone"]), local, _state["gen"], disabled(), weakened())


# ------------------------------------------------------------------ the ledger event

def configured_event(base) -> bool:
    """True once the tool recorded its first successful push (an `anchor_configured` event in the ledger)."""
    for p in ledger_chain.day_files(base):
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if EVENT.encode() not in raw:
            continue
        for ln in raw.split(b"\n"):
            if EVENT.encode() in ln:
                o = ledger_chain._parse(ln.strip())
                if o and o.get("ev") == EVENT:
                    return True
    return False


def last_push_ok_at(clone: Path) -> str | None:
    try:
        o = json.loads((Path(clone) / STATUS_FILE).read_text(encoding="utf-8"))
        v = o.get("last_push_ok_at")
        return v if isinstance(v, str) else None
    except (OSError, ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ the payload

def status(base) -> dict:
    """The `anchor` part of the integrity payload. Only reads the cached remote state (see ensure_fresh)."""
    out = {"configured": False, "expected": False, "missing": False, "check_disabled": False,
           "last_anchored_day": None, "last_remote_commit_at": None, "last_push_ok_at": None, "remote": "off",
           "unreachable_since": None, "mismatches": []}
    expected = configured_event(base)
    out["expected"] = expected
    if disabled():
        if expected:                      # set up earlier, now switched off from the environment: say so
            out["configured"] = True
            out["check_disabled"] = True
        return out
    if expected and weakened():
        out["check_disabled"] = True
    cfg = load_config()
    mism: list[dict] = []
    if cfg is None:
        if expected:
            out["configured"] = True
            out["missing"] = True
            mism.append(problem("Outside anchor missing: the anchor configuration cannot be found.", "no anchor.json", "unclear"))
        out["mismatches"] = mism
        return out
    clone = cfg["clone"]
    wt = read_local(clone)
    with _lock:
        res, since, checked = _state["result"], _state["unreachable_since"], _state["checked_at"]
        head, url_rewrite = _state["head"], _state["url_rewrite"]
    out["last_push_ok_at"] = last_push_ok_at(clone)
    if url_rewrite:
        mism.append(problem("Looks like tampering: the anchor clone's git config rewrites URLs.", "url.* key in the clone config"))
    wt_norm = wt.replace(b"\r\n", b"\n") if wt is not None else None     # a CRLF checkout is not an edit
    entries_raw: bytes | None = wt_norm
    if head is not None and wt_norm is not None and not wt_norm.startswith(head):
        mism.append(problem("Looks like tampering: the local anchor file differs from its committed copy.", "working file vs HEAD"))
    if res is not None:
        out["remote"] = "ok"
        out["last_remote_commit_at"] = res["last_commit_at"]
        if res["rewritten"]:
            mism.append(problem("Looks like tampering: the outside anchor file was rewritten.", "history of anchors.jsonl on the remote"))
        remote_raw = res["bytes"]
        # only this tool pushes from the clone, so HEAD is the remote copy plus not-yet-pushed commits
        mine = head if head is not None else wt_norm
        if mine is not None and not mine.startswith(remote_raw):
            mism.append(problem("Looks like tampering: the local anchor file differs from the outside copy.", "local file vs remote"))
        entries_raw = max([remote_raw] + ([wt_norm] if wt_norm is not None else []), key=len)
    else:
        out["remote"] = "unreachable" if checked is not None else "checking"
        out["unreachable_since"] = since
    entries, bad = parse_anchors(entries_raw) if entries_raw is not None else ([], [])
    for n in bad[:MAX_PROBLEMS]:
        mism.append(problem("Looks like tampering: an anchor entry is unreadable.", f"anchors.jsonl line {n}", "unclear"))
    if expected and (not clone.is_dir() or wt is None or (not entries and not bad)):
        out["missing"] = True
        mism.append(problem("Outside anchor missing: the anchor clone or its file cannot be found, or holds no anchors.", "", "unclear"))
    out["configured"] = bool(expected or wt is not None or (res is not None and res["bytes"]))
    out["last_anchored_day"] = max((e["day"] for e in entries), default=None)
    mism.extend(compare_ledger(base, entries))
    out["mismatches"] = mism[:MAX_PROBLEMS]
    return out
