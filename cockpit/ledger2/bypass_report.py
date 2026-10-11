"""Plain-language report for one bypass_attempt (contracts.md §5.2, §6.4 rule B).

Built only from what the ledger holds. Old attempts (before 10 Oct 2026) lack actor/desc/target/effect and still
render, with "Not recorded" where a fact is missing. No model is involved: every sentence comes from a rule."""
from __future__ import annotations

import datetime as dt
import re

try:
    from . import derive
except ImportError:  # pragma: no cover
    import derive  # type: ignore

REFUSAL_WINDOW_S = 30 * 60
AFTER_WINDOW_S = 5 * 60
REPEAT_WINDOW_S = 10 * 60
NOT_RECORDED = "Not recorded."
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

CREATURES = {
    "implementer": "Builder", "test-runner": "Lab Tech", "reviewer": "Inspector", "senior-reviewer": "Senior Partner",
    "scout": "Scout", "sweeper": "Clerk", "researcher": "Analyst", "drafter": "Writer",
}

ATTEMPT_KEYS = ("actor", "desc", "target", "effect")


def attempt_key(e: dict) -> str:
    return f"{e.get('t')}|{e.get('sid')}"


def actor_words(e: dict) -> str:
    a = e.get("actor") or e.get("agent_type") or (None if e.get("agent_id") else "main")
    if not a:
        return "An unknown agent"
    a = str(a).lower()
    if a == "main":
        return "The Chief"
    if a == "unknown":
        return "An unknown agent"
    if a in CREATURES:
        return "The " + CREATURES[a]
    return "An agent (" + a + ")"


def target_words(e: dict) -> str:
    matched = str(e.get("matched") or "")
    label = str(e.get("target") or "")          # derived by the hook; the command text is not stored
    if matched.startswith("ledger note"):
        return "the ledger notes"
    if label == "hooks-config":
        return "the hooks' settings"
    if label == "hooks-program":
        return "the hook program"
    if label == "hooks-folder" or re.search(r"\.claude[\\/]+hooks[\\/]+atlas", matched, re.I):
        return "the hooks folder"
    m = re.fullmatch(r"ledger-day:(\d{4})-(\d\d)-(\d\d)", label)
    if m:
        mo = int(m.group(2))
        if 1 <= mo <= 12:
            return f"the ledger record of {int(m.group(3))} {MONTHS[mo - 1]}"
    return "the ledger record"


def _verb(effect) -> str:
    return {"read": "read", "change": "change", "delete": "delete"}.get(effect, "use")


def _secs(a, b):
    ta, tb = derive.parse_t(a), derive.parse_t(b)
    if ta is None or tb is None:
        return None
    return (tb - ta).total_seconds()


def _mins(s) -> str:
    m = max(1, int(round(s / 60)))
    return f"{m} min"


def _actor_key(e: dict) -> str:
    a = e.get("actor") or e.get("agent_type") or (None if e.get("agent_id") else "main")
    return str(a or "unknown").lower()


def _same_helper(x: dict, e: dict, before: list[dict]) -> bool:
    """A rework refusal concerned the attempt's actor: same helper type, or (when the refusal kept the spawn's
    tool-use id) the helper that spawn became."""
    helper = str(x.get("helper") or "").lower()
    if helper and helper == _actor_key(e):
        return True
    tuid, aid = x.get("tuid"), e.get("agent_id")
    if tuid and aid:
        return any(p.get("ev") == "helper_paired" and p.get("tuid") == tuid and p.get("agent_id") == aid
                   for p in before)
    return False


def _context(e: dict, before: list[dict], wf: dict):
    """(sentence, matching_refusal_or_None) from the session's events up to the attempt. The refusal is returned
    only when the attempt's actor is a builder and the refusal concerned that same helper (reading rule a)."""
    recent = []
    for x in before:
        if x.get("ev") != "refusal" or x.get("guard") != "rework":
            continue
        if str(x.get("detail") or "").startswith("carry-over"):
            continue
        d = _secs(x.get("t"), e.get("t"))
        if d is not None and 0 <= d <= REFUSAL_WINDOW_S:
            recent.append((x, d))
    builders = {str(b).lower() for b in (wf.get("builders") or [])}
    refusal = None
    if _actor_key(e) in builders:
        refusal = next(((x, d) for x, d in reversed(recent) if _same_helper(x, e, before)), None)
    tasks = []
    try:
        tasks = derive._derive_session(e.get("sid") or "", before, wf, set()) if before else []
    except Exception:  # noqa: BLE001
        tasks = []
    tk = tasks[-1] if tasks and tasks[-1].get("status") == "open" else None
    parts = []
    title = (tasks[-1].get("session_title") if tasks else None) or None
    parts.append(f'Session "{title}".' if title else "Session title not recorded.")
    if tk is None:
        parts.append("No task was open.")
    else:
        typ = tk.get("type") or "undeclared"
        r = tk.get("rounds") or {}
        parts[-1] = parts[-1][:-1] + f", type {typ}."
        budget = r.get("budget")
        if budget is None:
            parts.append("No rework budget for this type.")
        else:
            total = budget + int(r.get("granted") or 0)
            granted = f" ({r.get('granted')} granted)" if r.get("granted") else ""
            used = int(r.get("needs_changes") or 0)
            if used > total:
                tail = "; rounds beyond it were logged, not enforced" if e.get("mode") != "enforce" else ""
                parts.append(f"{used} review rounds asked for changes on this task (budget {total}{granted}{tail}).")
            else:
                parts.append(f"{used} of {total} rework used{granted}.")
    if e.get("actor_task"):
        parts.append(f'Helper task: "{e["actor_task"]}".')
    if recent:
        x, d = recent[-1]
        helper = str(x.get("helper") or "").strip()
        who = f", for the {helper} helper" if helper else ""
        if not helper:
            parts.append(f"A rework round was over budget for this task (helper not recorded), {_mins(d)} before.")
        elif x.get("would"):
            parts.append(f"A rework round was over budget (logged, not enforced) {_mins(d)} before{who}.")
        else:
            parts.append(f"A new rework round was refused {_mins(d)} before{who}.")
    else:
        parts.append("No rework refusal in the 30 minutes before.")
    return " ".join(parts), refusal


def _after(e: dict, later: list[dict]) -> str:
    t0 = e.get("t")
    win = []
    for x in later:
        d = _secs(t0, x.get("t"))
        if d is not None and 0 <= d <= AFTER_WINDOW_S:
            win.append(x)
    again = sum(1 for x in win if x.get("ev") == "bypass_attempt")
    out = []
    if again:
        out.append(f"Tried again {again} more time{'s' if again != 1 else ''}.")
    aid = e.get("agent_id")
    if aid and any(x.get("ev") == "helper_end" and x.get("agent_id") == aid for x in win):
        out.append("Then handed back to the Chief.")
    verdicts = [x for x in win if x.get("ev") == "verdict"]
    if verdicts:
        v = str(verdicts[0].get("verdict") or "").replace("_", " ")
        out.append(f"A verdict was then recorded ({v}).")
    if not out:
        others = [x for x in win if x.get("ev") not in ("cost", "bypass_attempt", "bypass_note")]
        if others:
            out.append(f"Work continued in the session ({len(others)} event{'s' if len(others) != 1 else ''} recorded).")
        else:
            out.append("No further activity recorded in the next 5 minutes.")
    return " ".join(out)


def _same_actor(x: dict, e: dict) -> bool:
    """Helpers share the main session's sid, so repeats are counted per actor: the same agent_id when either has one,
    else the same actor/agent type (the main session only matches itself)."""
    ax, ae = x.get("agent_id"), e.get("agent_id")
    if ax or ae:
        return ax == ae
    return _actor_key(x) == _actor_key(e)


def _repeats(e: dict, same_session: list[dict]) -> int:
    n = 0
    for x in same_session:
        if x is e or x.get("ev") != "bypass_attempt" or not _same_actor(x, e):
            continue
        d = _secs(e.get("t"), x.get("t"))
        if d is not None and abs(d) <= REPEAT_WINDOW_S:
            n += 1
    return n


def build_report(e: dict, session_events: list[dict], index: int, wf: dict, notes: list[dict] | None = None) -> dict:
    """`session_events` = every event of the attempt's session in file order, `index` = the attempt's position."""
    effect = e.get("effect")
    target = target_words(e)
    blocked = "Blocked." if e.get("denied", True) else "Not blocked."
    headline = f"{actor_words(e)} tried to {_verb(effect)} {target} with a shell command. {blocked}"
    if effect == "read":
        would = "The command looks read-only."
    elif effect == "change":
        would = f"Yes, it would have changed {target}."
    elif effect == "delete":
        would = f"Yes, it would have deleted {target}."
    elif effect == "unclear":
        would = "Unclear from the command."
    else:
        would = NOT_RECORDED
    context, refusal = _context(e, session_events[:index], wf)
    after = _after(e, session_events[index + 1:])
    repeats = _repeats(e, session_events)
    if refusal:
        reading, level = "After a rework refusal: possible attempt to get past the limit.", "alert"
    elif effect in ("change", "delete"):
        reading, level = "Tried to alter the record or the rules.", "alert"
    elif repeats >= 1:
        reading, level = "Repeated attempts.", "warn"
    elif effect == "read":
        reading, level = "Isolated read, no rework refusal for this helper before: most likely a wrong tool choice.", "info"
    elif effect == "unclear":
        reading, level = "Isolated attempt, no rework refusal for this helper before; the command could not be classified.", "info"
    else:
        reading, level = "No details recorded.", "info"
    note_list = []
    for n in sorted(notes or [], key=lambda x: str(x.get("t"))):
        if n.get("text"):
            note_list.append({"t": n.get("t"), "by": n.get("by") or "unknown", "text": str(n.get("text"))})
    return {
        "headline": headline,
        "own_words": e.get("desc") or NOT_RECORDED,
        "would_change": would,
        "context": context,
        "after": after,
        "reading": reading,
        "level": level,
        "notes": note_list,
        "actor_is_main": actor_words(e) == "The Chief",
    }
