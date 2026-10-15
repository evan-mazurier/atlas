"""Claude Code team — live event logger (hook for the Atlas "Claude Code team" panel).

Appends ONE compact JSON line per hook event to ~/.claude/team-live/events-YYYY-MM-DD.jsonl, so the
cockpit can show which sessions are working, on what type of work, and which helper is running.

Privacy by construction: it records metadata only — session id, cwd, event name, helper type, the
helper's short task label, a slash-command NAME, a declared work type. Never prompt text, never tool
input beyond those fields, never tool output.

Never blocks, never prints a decision, fails silently: a broken logger must not disturb a session.
Registered in ~/.claude/settings.json for SessionStart, SessionEnd, UserPromptSubmit,
UserPromptExpansion, Stop, SubagentStart, SubagentStop, PreToolUse (Agent|Skill|Bash|PowerShell)
and PostToolUse (Agent).

Work-type declaration (CLAUDE.md "The team"): the orchestrator runs the no-op shell command
`: team-type <type> [area] [task:<plan id>]` (Bash or PowerShell) at the start of a task; this hook records it.
It closes a finished task with the no-op `: team-done`; this hook records a bare `TeamDone` event (ts/sid/cwd only),
so the panel stops showing the session as "waiting". Neither is ever logged from a helper (agent_id).
"""
import datetime as dt
import json
import os
import re
import sys

LOG_DIR = os.path.join(os.path.expanduser("~"), ".claude", "team-live")
TYPE_RE = re.compile(r"^\s*:\s*team-type\s+([a-z][a-z0-9-]{0,30})(?:\s+([a-z][a-z0-9-]{0,15}))?(?:\s+task:(\S+))?\s*$", re.I)
# like the Atlas hook (TEAM_RE): the FIRST line of the command, `: team-done` then a word boundary, anything may follow
DONE_RE = re.compile(r"^\s*:\s*team-done\b", re.I)
PLAN_REF_RE = re.compile(r"^(?:evt_[0-9a-f]{8}(?:@\d{4}-\d\d-\d\d)?|t_[0-9a-f]{8}|[A-Za-z0-9][A-Za-z0-9._-]{0,40}/[A-Za-z0-9][A-Za-z0-9._-]{0,60})$")


def main() -> None:
    try:
        d = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return
    ev = d.get("hook_event_name") or ""
    rec = {
        "ts": dt.datetime.now().isoformat(timespec="seconds"),
        "ev": ev,
        "sid": d.get("session_id"),
        "cwd": d.get("cwd"),
    }
    if d.get("agent_id"):
        rec["in_agent"] = d.get("agent_id")          # the event fired inside a helper
    tool = d.get("tool_name")
    ti = d.get("tool_input") or {}

    if ev == "SessionStart":
        rec["source"] = d.get("source")
        rec["transcript"] = d.get("transcript_path")
        rec["entry"] = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        if d.get("model"):
            rec["model"] = d.get("model")
    elif ev == "SessionEnd":
        rec["reason"] = d.get("reason")
    elif ev == "UserPromptSubmit":
        p = (d.get("prompt") or "").lstrip()
        m = re.match(r"^/([A-Za-z0-9:_-]{1,60})", p)
        if m:
            rec["cmd"] = m.group(1)                    # the command NAME only, never the text
        rec["transcript"] = d.get("transcript_path")
    elif ev == "UserPromptExpansion":
        rec["cmd"] = d.get("command_name")
        rec["kind"] = d.get("expansion_type")
    elif ev == "Stop":
        pass
    elif ev in ("SubagentStart", "SubagentStop"):
        rec["agent_id"] = d.get("agent_id")
        rec["agent_type"] = d.get("agent_type")
    elif ev == "PreToolUse" and tool == "Agent":
        rec["tool"] = "Agent"
        rec["agent_type"] = ti.get("subagent_type")
        rec["label"] = (ti.get("description") or "")[:60]
        rec["bg"] = ti.get("run_in_background")
        rec["tuid"] = d.get("tool_use_id")
    elif ev == "PostToolUse" and tool == "Agent":
        tr = d.get("tool_response") or {}
        rec["tool"] = "Agent"
        rec["tuid"] = d.get("tool_use_id")
        if isinstance(tr, dict):
            rec["status"] = tr.get("status")
            rec["agent_id"] = tr.get("agentId")
    elif ev == "PreToolUse" and tool == "Skill":
        rec["tool"] = "Skill"
        rec["cmd"] = ti.get("skill") or ti.get("name")
    elif ev == "PreToolUse" and tool in ("Bash", "PowerShell"):
        cmd = ti.get("command") or ""
        m = TYPE_RE.match(cmd) if len(cmd) <= 200 else None
        done = bool(DONE_RE.match(cmd.split("\n", 1)[0][:200]))
        if (not m and not done) or d.get("agent_id"):
            return                                     # ordinary commands, and declarations from a helper, are never logged
        if done:
            rec["ev"] = "TeamDone"                     # nothing else: no type, no text
        else:
            rec["ev"] = "WorkType"
            rec["type"] = m.group(1).lower()
            if m.group(2):
                rec["area"] = m.group(2).lower()
            if m.group(3) and PLAN_REF_RE.match(m.group(3)):
                rec["plan_ref"] = m.group(3)             # a Plan item id, validated; free text is never logged
    else:
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"events-{dt.date.today().isoformat()}.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 — fail silent, never disturb a session
        pass
