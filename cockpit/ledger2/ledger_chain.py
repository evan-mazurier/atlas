"""Hash-chain scan, break triage and acknowledgements (contracts.md §1 "Hash chain", §5.2 integrity, §6.4 rule A).

Stdlib only, no repo imports: the installer copies this file next to the hook (like areas.py) so the hook can list the
breaks when the operator types /ack-break. Everything here is deterministic, no model.

A break is identified by a stable id: `<day>:<line>:<first 8 hex of the broken line's hash>` (for example
`2026-10-09:120:ab12cd34`). The line number is the raw line number in the day file."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

GENESIS = "genesis"
CHAIN_WINDOW = 8          # a prev equal to the hash of any of the previous 8 lines is accepted
NEAR_END = 8              # "at/near the end of a day file" = within its last 8 lines
MAX_BREAKS = 50
ACCIDENT_SIGNS = 6        # how many accident signs the triage looks at

_DAY_RE = re.compile(r"events-(\d{4}-\d\d-\d\d)\.jsonl$")


def line_hash(line: bytes) -> str:
    """sha256 (hex) of one line's bytes, without its line ending."""
    return hashlib.sha256(line.rstrip(b"\r\n")).hexdigest()


def day_files(base) -> list[Path]:
    try:
        return sorted(Path(base).glob("events-????-??-??.jsonl"))
    except OSError:
        return []


def break_id(file_name: str, line: int, h: str, prev_h: str = GENESIS) -> str:
    """`<day>:<line>:<hash8 of the broken line>:<hash8 of the previous non-blank line in scan order>`: the id binds
    the predecessor, so editing that line after an acknowledgement makes the break a new, unacknowledged one."""
    m = _DAY_RE.search(file_name)
    return f"{m.group(1) if m else file_name}:{line}:{h[:8]}:{prev_h[:8]}"


def _parse(ln: bytes):
    try:
        obj = json.loads(ln.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def scan(base) -> dict:
    """Walk every day file in order. Returns {"breaks": [{file, line, id}], "acks": {break id: time}, "files", "lines"}.
    Lines written before the chain existed (no `prev`) are accepted until the first chained line."""
    breaks: list[dict] = []
    acks: dict[str, str] = {}
    recent: list[str] = []
    last_prompt: dict[str, str | None] = {}      # sid -> cmd of its latest (non-helper) prompt event
    chained = False
    n_files = n_lines = 0
    for p in day_files(base):
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        n_files += 1
        first = True
        for i, ln in enumerate(raw.split(b"\n"), 1):
            ln = ln.rstrip(b"\r")
            if not ln.strip():
                continue
            n_lines += 1
            h = line_hash(ln)
            obj = _parse(ln)
            prev = obj.get("prev") if obj else None
            bad = False
            if isinstance(prev, str):
                # exact link, or genesis; the window of 8 is tolerated ONLY for a line flagged nolock (a racing append
                # chained to a slightly stale tail), the first chained line after legacy lines, and the first line of
                # a day file (backfill shape). Anything else that links to an older line is a break.
                exact = (prev == GENESIS and not recent) or (bool(recent) and prev == recent[-1])
                tolerated = bool(recent) and prev in recent and (obj.get("nolock") is True or first or not chained)
                chained = True
                bad = not (exact or tolerated)
            elif chained:
                bad = True
            if bad:
                breaks.append({"file": p.name, "line": i,
                               "id": break_id(p.name, i, h, recent[-1] if recent else GENESIS)})
            if obj:
                sid = str(obj.get("sid") or "")
                if obj.get("ev") == "prompt" and not obj.get("agent_id"):
                    last_prompt[sid] = obj.get("cmd")
                elif (obj.get("ev") == "integrity_ack" and isinstance(obj.get("break"), str) and not bad
                      and not obj.get("agent_id") and obj.get("by") == "operator"
                      and last_prompt.get(sid) == "ack-break"):
                    acks.setdefault(obj["break"], str(obj.get("t") or ""))
            first = False
            recent.append(h)
            del recent[:-CHAIN_WINDOW]
    return {"breaks": breaks[:MAX_BREAKS], "acks": acks, "files": n_files, "lines": n_lines}


# ------------------------------------------------------------------ triage

def _load_file(p: Path) -> list[dict]:
    """Non-blank lines of a day file: [{no, raw, obj, hash}]."""
    out = []
    try:
        raw = p.read_bytes()
    except OSError:
        return out
    for i, ln in enumerate(raw.split(b"\n"), 1):
        ln = ln.rstrip(b"\r")
        if ln.strip():
            out.append({"no": i, "obj": _parse(ln), "hash": line_hash(ln)})
    return out


def _secs(a, b):
    try:
        ta, tb = dt.datetime.fromisoformat(str(a)), dt.datetime.fromisoformat(str(b))
        return abs((ta - tb).total_seconds())
    except Exception:  # noqa: BLE001
        return None


def _story(window: list[dict]) -> bool:
    for x in window:
        o = x["obj"] or {}
        if o.get("ev") in ("refusal", "bypass_attempt"):
            return True
        if o.get("ev") == "verdict" and str(o.get("verdict")) == "NEEDS_CHANGES":
            return True
    return False


ACC_WORDS = {
    "near_end": "the break sits within the last 8 lines of its day file",
    "nolock": "the line is flagged as written without the lock (two writers raced)",
    "truncated": "the line is cut off or unreadable (a crash while writing)",
    "same_second": "its time is within one second of a neighbouring line (two writers at once)",
    "out_of_order": "its link points to another line that exists, so lines are out of order rather than missing",
    "older_copy": "the previous day file ends earlier than this line expects (an older copy may have been restored)",
}
TAM_WORDS = {
    "mid_unflagged": "the break is in the middle of the history and the line is not flagged",
    "no_match": "its link matches no line in the file, so a line was removed or edited",
    "story": "a refusal, a NEEDS CHANGES verdict or a bypass attempt sits right next to the break",
}


def triage_break(files: list[Path], idx: int, line_no: int, cache: dict) -> dict:
    """Signs and a plain-words verdict for the break at `line_no` of `files[idx]`."""
    def lines_of(j):
        if j not in cache:
            cache[j] = _load_file(files[j])
        return cache[j]

    lines = lines_of(idx)
    k = next((n for n, x in enumerate(lines) if x["no"] == line_no), None)
    if k is None:
        return {"verdict": "unclear", "summary": "Unclear: the line could not be read again.",
                "signs": {"accidental": [], "tampering": [], "of": ACCIDENT_SIGNS}}
    cur = lines[k]
    obj = cur["obj"]
    prev = obj.get("prev") if obj else None
    prev_hashes = {x["hash"] for x in lines_of(idx - 1)} if idx > 0 else set()
    all_hashes = {x["hash"] for n, x in enumerate(lines) if n != k} | prev_hashes
    matches = isinstance(prev, str) and prev in all_hashes

    near_end = len(lines) - k <= NEAR_END
    nolock = bool(obj and obj.get("nolock") is True)
    truncated = obj is None
    same_second = False
    for n in (k - 1, k + 1):
        if 0 <= n < len(lines) and obj:
            s = _secs(obj.get("t"), (lines[n]["obj"] or {}).get("t"))
            if s is not None and s <= 1:
                same_second = True
    older_copy = (k == 0 and idx > 0 and not matches and prev != GENESIS)
    acc = {"near_end": near_end, "nolock": nolock, "truncated": truncated, "same_second": same_second,
           "out_of_order": matches, "older_copy": older_copy}
    story = _story(lines[max(0, k - 3):k + 3])
    mid_unflagged = (not near_end) and (not nolock) and k != 0
    tam = {"mid_unflagged": mid_unflagged, "no_match": not matches, "story": story}

    # nolock and same_second are signs only, never sufficient. The verdict needs a structural explanation.
    nolock_near = any((x["obj"] or {}).get("nolock") is True for x in lines[max(0, k - CHAIN_WINDOW):k + 1])
    backfill = k == 0 and idx > 0 and isinstance(prev, str) and prev in prev_hashes
    summary = ""
    if truncated:
        tam["no_match"] = False                     # a cut-off line has no link to judge
        nxt = lines[k + 1] if k + 1 < len(lines) else (lines_of(idx + 1)[0] if idx + 1 < len(files) and lines_of(idx + 1) else None)
        before = lines[k - 1]["hash"] if k > 0 else (lines_of(idx - 1)[-1]["hash"] if idx > 0 and lines_of(idx - 1) else None)
        np_ = (nxt["obj"] or {}).get("prev") if nxt else None
        if nxt is None or (np_ is not None and np_ in (cur["hash"], before)):
            verdict = "accidental"                  # the last line of the ledger, or the next line continues from it
        else:
            verdict = "unclear"
            summary = "Unclear: the line is cut off, but the line after it does not continue from it or from the line before it."
    elif not matches:
        if nolock or older_copy:
            verdict = "unclear"
            summary = ("Unclear: the link matches no line; a race, an older copy restored or a removed line cannot be "
                       "told apart here.")
        else:
            verdict = "tampering"
    elif nolock_near or backfill:
        before_nolock = k > 0 and bool((lines[k - 1]["obj"] or {}).get("nolock") is True)
        newest = idx == len(files) - 1
        if before_nolock and not (newest and len(lines) - k <= NEAR_END):
            verdict = "unclear"                     # a nolock line mid-file followed by a locked one: could be an insert
            summary = ("Unclear: the line before it is flagged nolock and sits in the middle of the history, "
                       "which an inserted line would also look like.")
        else:
            verdict = "accidental"
    else:
        verdict = "unclear"
        summary = "Unclear: the line links to another line of the file, but no nolock flag or backfill explains it."
    acc_list = [ACC_WORDS[a] for a, on in acc.items() if on]
    tam_list = [TAM_WORDS[a] for a, on in tam.items() if on]
    if verdict == "accidental":
        summary = f"Looks accidental: {len(acc_list)} of {ACCIDENT_SIGNS} signs: " + "; ".join(acc_list) + "."
    elif verdict == "tampering":
        summary = "Looks like tampering: " + "; ".join(tam_list) + "."
    return {"verdict": verdict, "summary": summary, "signs": {"accidental": acc_list, "tampering": tam_list,
                                                              "of": ACCIDENT_SIGNS}}


def status_of(breaks: list[dict]) -> str:
    if not breaks:
        return "intact"
    return "broken" if any(not b.get("acknowledged") for b in breaks) else "intact, %d acknowledged" % len(breaks)


def build(base, anchor_fn=None) -> dict:
    """The integrity payload: breaks with verdict, signs and acknowledgement; `intact` is false only while a break is
    unacknowledged; `status` is "intact", "intact, N acknowledged" or "broken"."""
    res = scan(base)
    files = day_files(base)
    index = {p.name: n for n, p in enumerate(files)}
    cache: dict = {}
    out = []
    for b in res["breaks"]:
        try:
            tri = triage_break(files, index[b["file"]], b["line"], cache)
        except Exception:  # noqa: BLE001
            tri = {"verdict": "unclear", "summary": "Unclear: the triage failed for this break.",
                   "signs": {"accidental": [], "tampering": [], "of": ACCIDENT_SIGNS}}
        ack_t = res["acks"].get(b["id"])
        out.append(dict(b, **tri, acknowledged=ack_t is not None, ack_t=ack_t))
    status = status_of(out)
    anchor = anchor_fn(base) if anchor_fn else {"configured": False, "last_anchored_day": None, "mismatches": []}
    if anchor["mismatches"]:
        status = "broken"            # an anchor mismatch cannot be acknowledged and is never accidental
    return {"intact": status != "broken", "status": status, "breaks": out,
            "acknowledged": sum(1 for b in out if b["acknowledged"]),
            "files": res["files"], "lines": res["lines"], "anchor": anchor}


def unacknowledged(base) -> list[dict]:
    res = scan(base)
    return [b for b in res["breaks"] if b["id"] not in res["acks"]]
