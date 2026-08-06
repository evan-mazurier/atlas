"""
Substrate seam tests — deterministic, no Ollama / no CLI.

Covers resolve() precedence, the message-flattening for the CLI adapter, the normalized result
contract, and (the key Phase-0 property) that ask_vault behaves identically when routed through the
seam onto Ollama. The transports themselves (HTTP / subprocess) are not exercised here; they're thin
and integration-checked live.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import substrate  # noqa: E402


# ---- resolve() -------------------------------------------------------------------------

def test_resolve_defaults_after_phase2():
    # Phase 2: all three conversational front-door agents now default to Haiku on the CLI.
    for role in ("receptionist", "ask-orchard", "ask-almanac"):
        spec = substrate.resolve(role)
        assert spec["substrate"] == "claude" and spec["model"] == "haiku"


def test_vault_agents_revert_to_local_by_env(monkeypatch):
    # the reversibility guarantee: one env value returns a vault agent to the local model
    monkeypatch.setenv("ATLAS_ASK_ORCHARD_SUBSTRATE", "ollama")
    monkeypatch.setenv("ATLAS_ASK_ORCHARD_MODEL", "qwen2.5:3b-instruct")
    assert substrate.resolve("ask-orchard")["substrate"] == "ollama"


def test_resolve_env_override_wins(monkeypatch):
    monkeypatch.setenv("ATLAS_ASK_ORCHARD_SUBSTRATE", "claude")
    monkeypatch.setenv("ATLAS_ASK_ORCHARD_MODEL", "sonnet")
    monkeypatch.setenv("ATLAS_ASK_ORCHARD_EFFORT", "high")
    spec = substrate.resolve("ask-orchard")
    assert spec == {"substrate": "claude", "model": "sonnet", "effort": "high"}


def test_resolve_unknown_role_safe_fallback():
    spec = substrate.resolve("does-not-exist")
    assert spec["substrate"] == "ollama" and spec["model"]  # never raises, always a usable spec


def test_resolve_role_to_envkey_mapping(monkeypatch):
    # hyphens in the role map to underscores in the env key
    monkeypatch.setenv("ATLAS_ASK_ALMANAC_MODEL", "haiku")
    assert substrate.resolve("ask-almanac")["model"] == "haiku"


# ---- chat() dispatch + result contract -------------------------------------------------

def test_chat_unwired_substrate_returns_error_not_raise():
    res = substrate.chat("x", [{"role": "user", "content": "hi"}],
                         _spec={"substrate": "grok", "model": "m"})
    assert res["ok"] is False and "not wired" in res["error"]
    assert res["substrate"] == "grok"


def test_chat_routes_by_spec_substrate(monkeypatch):
    seen = {}
    def fake_claude(model, effort, messages, timeout, tools):
        seen.update(model=model, effort=effort, n=len(messages), tools=tools)
        return {"ok": True, "text": "hello", "tool_calls": [], "error": None,
                "substrate": "claude", "model": model}
    monkeypatch.setattr(substrate, "_claude_chat", fake_claude)
    res = substrate.chat("ask-orchard", [{"role": "user", "content": "q"}],
                         _spec={"substrate": "claude", "model": "haiku", "effort": None})
    assert res["text"] == "hello" and seen["model"] == "haiku"


# ---- CLI tool-calling: action protocol + action-block parse (Phase 2) ------------------

_TOOLS = [
    {"type": "function", "function": {
        "name": "focus_project", "description": "Open a project's panel.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "enum": ["almanac", "orchard-api"]}}, "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "dispatch_next_task", "description": "Launch the next planned task.",
        "parameters": {"type": "object", "properties": {
            "project": {"type": "string", "enum": ["almanac"]}}, "required": ["project"]}}},
]


def test_tools_to_protocol_lists_names_and_enums():
    proto = substrate._tools_to_protocol(_TOOLS)
    assert "ACTION PROTOCOL" in proto and "```action" in proto
    assert "focus_project(id*" in proto and "almanac" in proto      # required marker + enum hint
    assert "dispatch_next_task(project*" in proto


def test_parse_action_block_extracts_and_strips():
    reply = ('Opening Almanac for you.\n\n```action\n'
             '{"name": "focus_project", "arguments": {"id": "almanac"}}\n```')
    text, calls = substrate._parse_action_block(reply)
    assert calls == [{"name": "focus_project", "arguments": {"id": "almanac"}}]
    assert text == "Opening Almanac for you." and "```" not in text


def test_parse_action_block_none_when_absent():
    text, calls = substrate._parse_action_block("Just an answer, no action.")
    assert calls == [] and text == "Just an answer, no action."


def test_claude_chat_parses_tool_call(monkeypatch):
    # stub the subprocess so no CLI runs; assert the action block becomes a normalized tool_call
    class P:
        returncode = 0
        stdout = json.dumps({"result": 'Done.\n```action\n{"name":"dispatch_next_task",'
                                       '"arguments":{"project":"almanac"}}\n```'})
        stderr = ""
    monkeypatch.setattr(substrate.subprocess, "run", lambda *a, **k: P())
    res = substrate._claude_chat("haiku", None, [{"role": "user", "content": "launch it"}], 30, _TOOLS)
    assert res["ok"] and res["text"] == "Done."
    assert res["tool_calls"] == [{"name": "dispatch_next_task", "arguments": {"project": "almanac"}}]


def test_ollama_tool_calls_normalized(monkeypatch):
    class R:
        def read(self): return json.dumps({"message": {"content": "",
            "tool_calls": [{"function": {"name": "focus_project",
                                         "arguments": {"id": "almanac"}}}]}}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(substrate.urllib.request, "urlopen", lambda *a, **k: R())
    res = substrate._ollama_chat("qwen", [{"role": "user", "content": "open almanac"}], 0.2, None, 30, _TOOLS)
    assert res["tool_calls"] == [{"name": "focus_project", "arguments": {"id": "almanac"}}]


def test_flatten_overflows_large_system_into_stdin():
    big = "X" * (substrate._SYS_ARG_BUDGET + 100)
    system, stdin = substrate._flatten_for_cli(
        [{"role": "system", "content": big}, {"role": "user", "content": "q"}], effort=None)
    assert len(system) < 500 and "SYSTEM CONTEXT" in stdin and big in stdin   # moved out of argv


# ---- CLI message flattening ------------------------------------------------------------

def test_flatten_for_cli_folds_system_and_history():
    msgs = [
        {"role": "system", "content": "PERSONA"},
        {"role": "system", "content": "EVIDENCE"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "prior answer"},
        {"role": "user", "content": "the question"},
    ]
    system, user = substrate._flatten_for_cli(msgs, effort=None)
    assert "PERSONA" in system and "EVIDENCE" in system
    assert "User: earlier" in user and "You: prior answer" in user
    assert user.strip().endswith("the question")   # current turn last, unlabeled


def test_flatten_prepends_effort_directive():
    system, _ = substrate._flatten_for_cli([{"role": "system", "content": "S"}], effort="high")
    assert system.startswith(substrate._EFFORT_DIRECTIVE["high"])


def test_cli_result_text_unwraps_envelope_and_falls_back():
    assert substrate._cli_result_text('{"result": "answer"}') == "answer"
    assert substrate._cli_result_text("raw text") == "raw text"


# ---- Phase-0 behavior preservation: ask_vault routed through the seam onto Ollama -------

def test_ask_vault_routes_through_substrate_ollama(monkeypatch):
    """The vault answer path must produce the same shape when the seam resolves to ollama — the
    Phase-0 guarantee. We stub substrate.chat to return a fixed Ollama-style answer and assert the
    citation + post-processing pipeline still runs."""
    import assistant

    captured = {}
    def fake_chat(role, messages, temperature=0.2, max_tokens=None, timeout=120, _spec=None):
        captured.update(role=role, max_tokens=max_tokens, has_evidence=any("EVIDENCE" in m["content"] for m in messages))
        return {"ok": True, "text": "The decision was to combine uncorrelated edges [[d1]].",
                "error": None, "substrate": "ollama", "model": "qwen2.5:3b-instruct"}
    monkeypatch.setattr(assistant.substrate, "chat", fake_chat)

    evidence = {"live": [{"id": "d1", "label": "Instrument selection", "type": "decision",
                          "state": "live", "updated": "2026-01-01", "markdown": "combine edges"}],
                "superseded": []}
    out = assistant.ask_vault("why combine instruments?", evidence, role="ask-almanac")
    assert out["ok"] is True
    assert "combine uncorrelated edges" in out["answer"]
    assert out["cited"] == ["d1"] and out["sources"][0]["id"] == "d1"
    assert captured["role"] == "ask-almanac"          # the role is threaded to resolve()
    assert captured["max_tokens"] == 200            # the old num_predict length cap is preserved


def test_ask_vault_empty_evidence_short_circuits_without_model_call(monkeypatch):
    import assistant
    called = []
    monkeypatch.setattr(assistant.substrate, "chat", lambda *a, **k: called.append(1) or {"ok": True, "text": "x"})
    out = assistant.ask_vault("q", {"live": [], "superseded": []}, role="ask-orchard")
    assert out["ok"] is True and out["answer"] == "That's not in the decision vault."
    assert not called                                # no evidence → never hits the model


def test_managed_roles_are_the_seam_agents():
    # brain-keeper joined the seam (docs/brain.md §14.6): the model behind a judgment call that no
    # test can gate should be one config value, not a hardcoded choice. It carries no agents.yaml
    # entry, which is harmless: _agent_substrate only OVERLAYS a live label onto roster agents that
    # already exist, so membership here never injects a card.
    assert substrate.MANAGED_ROLES == frozenset({"receptionist", "ask-orchard", "ask-almanac",
                                                 "brain-keeper"})


def test_receptionist_answer_strips_markdown(monkeypatch):
    import assistant
    monkeypatch.setattr(assistant.substrate, "chat",
        lambda *a, **k: {"ok": True, "text": "Equity is **€50,073.28** with `471` tests.",
                         "tool_calls": []})
    statuses = [{"_ok": True, "_id": "almanac", "project": "Almanac", "kind": "trading", "status": "p",
                 "phase": "p", "health": "green", "updated": "2026-07-13", "next_target": "x",
                 "blockers": [], "metrics": {}}]
    out = assistant.ask("almanac equity?", statuses, allow_nav=False, allow_dispatch=False, allow_next=False)
    assert "**" not in out["answer"] and "`" not in out["answer"]
    assert "€50,073.28" in out["answer"] and "471" in out["answer"]


def test_receptionist_ask_maps_tool_call_to_action(monkeypatch):
    """ask() must turn the substrate's normalized tool_call into its action tuple, substrate-blind."""
    import assistant
    monkeypatch.setattr(assistant.substrate, "chat",
                        lambda *a, **k: {"ok": True, "text": "Opening Almanac.",
                                         "tool_calls": [{"name": "focus_project", "arguments": {"id": "almanac"}}]})
    statuses = [{"_ok": True, "_id": "almanac", "project": "Almanac", "kind": "trading",
                 "status": "paper", "phase": "p", "health": "green", "updated": "2026-07-13",
                 "next_target": "x", "blockers": [], "metrics": {}}]
    out = assistant.ask("open almanac", statuses, allow_nav=True, allow_dispatch=False, allow_next=False)
    assert out["action"] == ("focus", "almanac") and out["answer"] == "Opening Almanac."


def test_receptionist_ask_nav_gate_drops_focus_from_ask_box(monkeypatch):
    import assistant
    monkeypatch.setattr(assistant.substrate, "chat",
                        lambda *a, **k: {"ok": True, "text": "Almanac is green.",
                                         "tool_calls": [{"name": "focus_project", "arguments": {"id": "almanac"}}]})
    statuses = [{"_ok": True, "_id": "almanac", "project": "Almanac", "kind": "trading", "status": "p",
                 "phase": "p", "health": "green", "updated": "2026-07-13", "next_target": "x",
                 "blockers": [], "metrics": {}}]
    out = assistant.ask("is almanac ok?", statuses, allow_nav=False)   # Ask box: nav is client-side
    assert out["action"] is None and out["answer"] == "Almanac is green."


def test_ask_vault_substrate_error_surfaces(monkeypatch):
    import assistant
    monkeypatch.setattr(assistant.substrate, "chat",
                        lambda *a, **k: {"ok": False, "text": "", "error": "Can't reach Ollama"})
    evidence = {"live": [{"id": "d1", "label": "X", "type": "decision", "state": "live", "markdown": "m"}],
                "superseded": []}
    out = assistant.ask_vault("q", evidence, role="ask-orchard")
    assert out["ok"] is False and "Ollama" in out["error"]
