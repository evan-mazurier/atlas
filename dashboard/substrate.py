"""
Atlas substrate layer — the ONE seam every conversational agent's model call goes through.

Design of record: docs/model-agnosticism.md. The point: changing an agent's model, or its whole
engine (Ollama ↔ the `claude` CLI ↔ a future local rig), is a CONFIG change, never a code edit — and
the local-first option stays one flip away (the north star in CLAUDE.md).

Two functions:
  resolve(role)            -> {substrate, model, effort} — the single resolution point.
  chat(role, messages, …)  -> {ok, text, tool_calls, error, substrate, model} — dispatches to the adapter.

`resolve` precedence: env override (ATLAS_<ROLE>_SUBSTRATE / _MODEL / _EFFORT) → the central _DEFAULTS
table → a safe fallback. _DEFAULTS is the single place a conversational agent's model lives; wiring it
to also read the distributed agents.yaml files is a documented later enhancement (§2).

Adapters share one normalized result shape so callers are substrate-blind. Two are wired: _ollama_chat
(local HTTP, native tool-calling) and _claude_chat (the subscription CLI). TOOLS work across both — the
Ollama path uses native function-calling; the CLI path uses a parsed ```action block (Phase 2). Either
way `chat` returns tool_calls as a normalized [{name, arguments}], so the caller never sees the seam.
An Anthropic-API adapter (native tool-use) and a llama.cpp adapter slot in with no caller change; those
are NOT built until point-of-need (north-star rule).

The `claude` CLI runs on the operator's SUBSCRIPTION (same path as worker.run_claude / the advisor / the
brainstormer), NOT a metered API key.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("ATLAS_OLLAMA_URL", "http://localhost:11434/api/chat")
_CLAUDE = shutil.which("claude") or os.environ.get("ATLAS_CLAUDE_BIN") or "claude"

# The SINGLE central table of conversational-agent model config. Every value is env-overridable as
# ATLAS_<ROLE>_SUBSTRATE / _MODEL / _EFFORT (role upper-cased, '-' → '_'). Roles absent here fall back
# to a safe local default. The dispatch/gate agents (scoper, coder, reviewer, …) are NOT here — they
# resolve model via procedures.yaml + dispatcher constants and are out of scope for this seam (§5).
_DEFAULTS: dict[str, dict] = {
    # Phase 2: the receptionist moves to Haiku on the subscription — reliable instruction-following
    # (the qwen2.5:3b ceiling produced the "no next target" slip). Its tool-calls (focus/dispatch)
    # ride the CLI action-block path below. Reversible to local with one env value, e.g.
    # ATLAS_RECEPTIONIST_SUBSTRATE=ollama ATLAS_RECEPTIONIST_MODEL=qwen2.5:3b-instruct.
    "receptionist": {"substrate": "claude", "model": "haiku", "effort": None},
    # Phase 1: the two read-only vault lenses on Haiku (reliable grounded recall).
    "ask-orchard": {"substrate": "claude", "model": "haiku", "effort": None},
    "ask-almanac": {"substrate": "claude", "model": "haiku", "effort": None},
    # brain-keeper judges whether a fact is about THE OPERATOR or about the work (docs/brain.md §14.6).
    # That is a judgment call with no test to gate it, and its failure mode is asymmetric: a bad
    # proposal spends the operator's attention permanently while a missed one simply comes back. So it gets
    # Sonnet rather than Haiku, and the seam makes that one value to revisit once precision is known.
    "brain-keeper": {"substrate": "claude", "model": "sonnet", "effort": None},
}

_FALLBACK = {"substrate": "ollama", "model": "qwen2.5:3b-instruct", "effort": None}

# The roles this seam resolves at runtime — so the roster display can show what ACTUALLY runs
# (substrate.py + env) instead of the possibly-stale agents.yaml field for these agents.
MANAGED_ROLES = frozenset(_DEFAULTS)

# Effort is not a CLI flag — it's a prepended system directive (same approach as worker/advisor).
_EFFORT_DIRECTIVE = {
    "low": "Answer directly, with minimal deliberation.",
    "medium": "Think carefully but concisely before you answer.",
    "high": "Reason thoroughly before you answer.",
}

_CLI_TIMEOUT = int(os.environ.get("ATLAS_SUBSTRATE_CLI_TIMEOUT", "120"))
# Keep the CLI --append-system-prompt (which is argv, not stdin) well under Windows' ~32KB command-line
# limit. If the system content is larger (e.g. a big vault-evidence bundle or state JSON), it overflows
# into stdin instead of argv — so a large context can never fail to launch.
_SYS_ARG_BUDGET = 18000


def resolve(role: str) -> dict:
    """The single resolution point for a conversational agent's substrate/model/effort.
    Precedence: env override → central _DEFAULTS → _FALLBACK. Never raises."""
    spec = dict(_DEFAULTS.get(role, _FALLBACK))
    key = (role or "").upper().replace("-", "_")
    spec["substrate"] = os.environ.get(f"ATLAS_{key}_SUBSTRATE", spec["substrate"])
    spec["model"] = os.environ.get(f"ATLAS_{key}_MODEL", spec["model"])
    spec["effort"] = os.environ.get(f"ATLAS_{key}_EFFORT", spec["effort"])
    return spec


def chat(role: str, messages: list[dict], temperature: float = 0.2,
         max_tokens: int | None = None, timeout: int | None = None,
         tools: list[dict] | None = None, _spec: dict | None = None) -> dict:
    """Run a chat completion for `role` through its configured substrate.

    `messages` = OpenAI-style [{role: 'system'|'user'|'assistant', content: str}].
    `tools` = OpenAI-style function tools; if given, the result may carry tool_calls (native on Ollama,
    a parsed ```action block on the CLI).
    Returns {ok, text, tool_calls, error, substrate, model}. Never raises — a transport failure comes
    back as {ok: False, error: …}. `_spec` lets a caller inject a resolved spec (tests); normally omitted.
    """
    spec = _spec or resolve(role)
    sub = spec.get("substrate")
    if sub == "ollama":
        return _ollama_chat(spec["model"], messages, temperature, max_tokens, timeout or 120, tools)
    if sub == "claude":
        return _claude_chat(spec["model"], spec.get("effort"), messages, timeout or _CLI_TIMEOUT, tools)
    return {"ok": False, "text": "", "tool_calls": [], "error": f"substrate '{sub}' not wired",
            "substrate": sub, "model": spec.get("model")}


def _coerce_args(a):
    """Tool arguments as a dict (some models emit them as a JSON string)."""
    if isinstance(a, str):
        try:
            return json.loads(a)
        except Exception:  # noqa: BLE001
            return {}
    return a or {}


# ----- adapters --------------------------------------------------------------------------

def _ollama_chat(model: str, messages: list[dict], temperature: float,
                 max_tokens: int | None, timeout: int, tools: list[dict] | None) -> dict:
    """Local Ollama HTTP chat — native tool-calling (identical behavior to the old assistant._post/tools)."""
    options: dict = {"temperature": temperature}
    if max_tokens:
        options["num_predict"] = max_tokens          # hard length cap (small models over-run)
    payload = {"model": model, "messages": messages, "stream": False, "options": options}
    if tools:
        payload["tools"] = tools
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"ok": False, "text": "", "tool_calls": [], "substrate": "ollama", "model": model,
                "error": f"Can't reach Ollama at {OLLAMA_URL} ({e.reason}). Is `ollama serve` running?"}
    except Exception as e:  # noqa: BLE001 — never crash the caller
        return {"ok": False, "text": "", "tool_calls": [], "substrate": "ollama", "model": model,
                "error": f"Ollama error: {e}"}
    msg = data.get("message") or {}
    text = (msg.get("content") or "").strip()
    calls = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {}) or {}
        if fn.get("name"):
            calls.append({"name": fn["name"], "arguments": _coerce_args(fn.get("arguments"))})
    # Fallback: some models emit the call as JSON in `content` instead of the tool_calls field. Recover
    # it and blank the text (the content WAS the call, not an answer).
    if not calls and text:
        inline = _inline_toolcall(text)
        if inline:
            calls.append(inline)
            text = ""
    return {"ok": True, "text": text, "tool_calls": calls, "error": None,
            "substrate": "ollama", "model": model}


def _inline_toolcall(content: str) -> dict | None:
    c = re.sub(r"</?tool_call>", "", content or "").strip()
    obj = None
    try:
        obj = json.loads(c)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", c, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                obj = None
    if isinstance(obj, dict) and obj.get("name"):
        return {"name": obj["name"], "arguments": _coerce_args(obj.get("arguments") or obj.get("parameters"))}
    return None


def _flatten_for_cli(messages: list[dict], effort: str | None,
                     extra_system: str = "") -> tuple[str, str]:
    """Fold OpenAI-style messages into the CLI's shape: system content → the appended system prompt
    (effort directive + any `extra_system` first); user/assistant turns → a single stdin string. If the
    system content would exceed the argv budget, it overflows into stdin so a large context never fails
    to launch."""
    sys_parts = []
    if effort in _EFFORT_DIRECTIVE:
        sys_parts.append(_EFFORT_DIRECTIVE[effort])
    if extra_system:
        sys_parts.append(extra_system)
    sys_parts += [m["content"] for m in messages if m.get("role") == "system"]
    system = "\n\n".join(p for p in sys_parts if p)

    convo = [m for m in messages if m.get("role") in ("user", "assistant")]
    parts: list[str] = []
    for m in convo[:-1]:                              # prior turns as labeled context
        parts.append(("User: " if m["role"] == "user" else "You: ") + (m.get("content") or ""))
    if parts:
        parts.append("")
    parts.append(convo[-1]["content"] if convo else "")   # the current question, unlabeled
    stdin = "\n".join(parts)

    if len(system) > _SYS_ARG_BUDGET:                # too big for argv → move it into the stdin preamble
        stdin = "SYSTEM CONTEXT (authoritative):\n" + system + "\n\n=====\n\n" + stdin
        system = "Follow the SYSTEM CONTEXT at the top of the message as your system instructions."
    return system, stdin


def _claude_chat(model: str, effort: str | None, messages: list[dict], timeout: int,
                 tools: list[dict] | None) -> dict:
    """One-shot `claude -p` completion on the subscription. Prompt via stdin (avoids the ~32KB Windows
    argv limit); UTF-8 both ways. When `tools` are given, an action-protocol is appended to the system
    prompt and the reply's ```action block (if any) is parsed into tool_calls and stripped from text."""
    protocol = _tools_to_protocol(tools) if tools else ""
    system, user_prompt = _flatten_for_cli(messages, effort, extra_system=protocol)
    cmd = [_CLAUDE, "-p", "--output-format", "json", "--model", model,
           "--append-system-prompt", system]
    try:
        proc = subprocess.run(cmd, input=user_prompt, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:  # noqa: BLE001 — subprocess/timeout → surfaced, never raised
        return {"ok": False, "text": "", "tool_calls": [], "substrate": "claude", "model": model,
                "error": f"claude CLI error: {e}"}
    if proc.returncode != 0:
        return {"ok": False, "text": "", "tool_calls": [], "substrate": "claude", "model": model,
                "error": f"claude CLI failed ({proc.returncode}): {(proc.stderr or '').strip()[:200]}"}
    _record_session(proc.stdout)
    text = _cli_result_text(proc.stdout).strip()
    calls = []
    if tools:
        text, calls = _parse_action_block(text)
    return {"ok": True, "text": text, "tool_calls": calls, "error": None,
            "substrate": "claude", "model": model}


def _record_session(stdout: str) -> None:
    """Note the CLI session this call ran in (docs/brain.md §14.10). A Claude Code session can write
    a memory file, and brain-keeper needs to tell Atlas's own conversations from the operator's terminal
    ones so the same turn is not judged by both readers. Best-effort and silent by design."""
    try:
        import json as _json
        import atlas_sessions
        atlas_sessions.record((_json.loads(stdout) or {}).get("session_id"))
    except Exception:  # noqa: BLE001 — bookkeeping must never affect the call's result
        pass


def _tools_to_protocol(tools: list[dict]) -> str:
    """Render OpenAI-style function tools into a text action-protocol for the CLI (kept in sync with the
    caller's tool list, so allow_* gating carries over — a dropped tool simply isn't described)."""
    lines = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        props = ((fn.get("parameters") or {}).get("properties") or {})
        req = set((fn.get("parameters") or {}).get("required") or [])
        argbits = []
        for pname, pspec in props.items():
            enum = pspec.get("enum")
            hint = f" ∈ [{', '.join(map(str, enum))}]" if enum else ""
            argbits.append(f"{pname}{'*' if pname in req else ''}{hint}")
        sig = f"{name}({', '.join(argbits)})"
        lines.append(f"- {sig}: {fn.get('description', '').strip()}")
    if not lines:
        return ""
    return (
        "ACTION PROTOCOL — the ONLY way to actually perform an action (open a panel, dispatch work) is to "
        "emit a machine-readable block. Prose does NOTHING: the system never reads your sentences as an "
        "action, so NEVER claim you opened/launched/did something unless you emit its block. To act, end "
        "your reply with exactly ONE fenced block on its own lines:\n"
        "```action\n{\"name\": \"<action>\", \"arguments\": { ... }}\n```\n"
        "Put any human-facing sentence FIRST, then the block. Omit the block when no action is needed — "
        "most questions need none. Use ONLY these action names and their listed arguments (a * marks a "
        "required one); never invent an action or an id.\n\n"
        "Example — to open the Almanac panel, your ENTIRE reply is:\n"
        "Opening Almanac.\n```action\n{\"name\": \"focus_project\", \"arguments\": {\"id\": \"almanac\"}}\n```\n\n"
        "Available actions:\n" + "\n".join(lines))


_ACTION_BLOCK_RE = re.compile(r"```(?:action|json)?\s*(\{.*?\})\s*```", re.S)


def _parse_action_block(text: str) -> tuple[str, list[dict]]:
    """Pull a trailing ```action {json} block out of the reply. Returns (text_without_block, [call]).
    Lenient: also accepts a bare ```json block whose object carries a "name"."""
    calls = []
    cleaned = text
    for m in _ACTION_BLOCK_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict) and obj.get("name"):
            calls.append({"name": obj["name"], "arguments": _coerce_args(obj.get("arguments"))})
            cleaned = cleaned.replace(m.group(0), "").strip()
            break                                    # at most one action
    return cleaned, calls


def _cli_result_text(stdout: str) -> str:
    """The assistant text from a `claude -p --output-format json` envelope (raw stdout if not that shape)."""
    try:
        env = json.loads(stdout)
        if isinstance(env, dict) and isinstance(env.get("result"), str):
            return env["result"]
    except json.JSONDecodeError:
        pass
    return stdout
