"""
Atlas assistant — the local Ollama model at the core.

CONTROL PLANE ONLY. It reads the live project state and can open a project's
panel (focus). It does NOT do project work and does NOT launch Claude Code — that
is Stage 3. The tool-calling pattern here is the seed for that dispatcher: Stage 3
just adds a `dispatch_to_claude` tool alongside `focus_project`.

No third-party deps. Model calls go through the substrate seam (dashboard/substrate.py); this module
builds the context/prompt/tools and interprets the reply — it no longer talks to a transport directly.
"""
from __future__ import annotations

import os
import re
import json
import datetime as dt

import dispatcher
import substrate   # the model-agnostic seam (docs/model-agnosticism.md) — ask + ask_vault route through it


def _receptionist() -> dict:
    """The receptionist's definition from agents.yaml (the source of truth for this global agent):
    its model + charter drive this assistant. Defensive — missing file → empty (env/default used)."""
    try:
        import yaml
        from pathlib import Path
        data = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / "agents.yaml").read_text(encoding="utf-8")) or {}
        for a in (data.get("globals") or []):
            if a.get("role") == "receptionist" or a.get("id") == "receptionist":
                return a if isinstance(a, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


_RECEPT = _receptionist()
# Model: ATLAS_MODEL env wins; else the receptionist's model from agents.yaml (if local/ollama);
# else the known default. Charter (its identity) is injected into the system prompt in ask().
MODEL = (os.environ.get("ATLAS_MODEL")
         or (_RECEPT.get("model") if _RECEPT.get("substrate") == "ollama" else None)
         or "qwen2.5-coder:7b")

SYSTEM = """You are Atlas, the assistant at the core of a local mission-control center.

ALWAYS address the user as "you" (or "sir"). NEVER refer to the user by name. Do not say "the operator".

You operate on the CONTROL PLANE. You MAY:
- Answer questions about the projects using ONLY the live state provided below.
- Open a project's panel with focus_project, or close it with clear_focus.
- Launch the NEXT PLANNED task with dispatch_next_task(project): the project's plan has an ordered
  backlog, and each project's `planned_next_task` in the state below is the one queued next. When the
  user wants to work on "the next task", "what's next", "the plan", or says "launch it / start it /
  go ahead" about that task, call dispatch_next_task(project). It pulls the task from the plan itself —
  you do NOT write the brief. You confirm before it runs.
- Dispatch NEW, user-described work with dispatch_to_claude(project, session_type, task): use this ONLY
  when the user describes the work themselves (not "the next task"). Write `task` as a clear optimized
  brief (goal + scope + definition of done) and pick a fitting session_type. You confirm before it runs.

You do NOT do project work yourself and you NEVER claim work is already done — dispatch is how work
happens, and only after you confirm.

You CANNOT write to the user's personal knowledge base, and you must never say or imply that you
have remembered, saved, noted, locked in or will recall something he just told you. Nothing he says
to you is stored by you. When he states a durable fact about himself, simply acknowledge it and use
it in your answer. If it is worth keeping, Atlas offers it to him separately for his approval, and
he can always capture it himself. Claiming to have saved something you did not save is the one
failure that would make him stop trusting what this system knows.

A project's METRICS / KPIs (Instagram followers, paying subscribers, monthly revenue, posts, tests,
etc.) live in its `metrics` and are summarized in the LIVE KPIs list. Answer any KPI/metric question
from those EXACT numbers — never say you don't have the data when the KPI list contains it. A value of
0 (or €0) means the value IS zero (e.g. €0 revenue, 0 subscribers because pre-launch) — report it as
zero, NEVER as "no data", "not reported", or "no metric".

DECIDING WHAT TO DO (important):
- If the user ASKS A QUESTION about a project — status, blockers, next target/milestone, metrics,
  health, risk, "what's the plan / what's next", anything phrased as "what/how/why/when/which/is
  there…" — ANSWER it in plain text from the live state (for "what's next / the plan", use that
  project's `planned_next_task`). Do NOT navigate and do NOT launch anything.
- If the user asks to START / LAUNCH / RUN / "go ahead with" the next task or the plan for a project,
  call dispatch_next_task(project). This is the ONLY thing that launches planned work.
- A project's CURRENT SESSION / "what's running" / "what's in progress" / "anything waiting on me" is
  answered from that project's `active_sessions` array — NOT from its status phase. If the array is
  non-empty, report it (its `state`, what it's `doing`, and if `state` is `needs_input` that the session
  is waiting on you with that `awaiting_input_question`). Only if the array is EMPTY do you say nothing
  is running. A project can be "building" yet have a live session — these are independent.
- ONLY call focus_project when the user EXPLICITLY asks to see/open/show/go to/fly to a project
  (e.g. "show me almanac", "open orchard", "fly to almanac", "take me there"). A question is NOT a
  navigation request.
- Use clear_focus only when asked to go back / home / show the overview.
- Use dispatch_to_claude only when the user wants work DONE.
When unsure whether to answer or navigate, ANSWER.

Style: concise and direct, a sentence or two. Refer to projects by name. Base every factual claim on
the live state; if it's not there, say so. Valid project ids: {ids}.
Project session types (project -> types): {session_types}."""


# ===================== ask-Orchard (vault-grounded mode) =====================
# The SAME local model, put into a read-only "answer from the decision vault" mode. Not a second
# brain — different grounding (retrieved vault evidence) + a tighter charter. Used by /api/ask-orchard.
VAULT_SYSTEM = """You are Orchard's institutional memory — a READ-ONLY assistant that answers questions \
about Orchard's own decisions and strategy, using ONLY the decision-vault evidence provided below.

HARD RULES:
- Answer in 2 to 4 short sentences of plain prose — ONE short paragraph. No bullet points, no lists, \
no headings, no bold. Never longer. If your answer is as long as the documents, you have failed the job.
- Base EVERY claim ONLY on the evidence below. If the evidence doesn't cover the question, say exactly: \
"That's not in the decision vault." Do NOT use outside knowledge and do NOT guess.
- Answer DIRECTLY. NEVER ask the user for clarification or more context, NEVER say you are confused or \
that there is confusion, and NEVER describe what the documents are about instead of answering. Either \
give the answer from the evidence, or say "That's not in the decision vault." — nothing in between. \
Note: "personal brand" is a real Orchard topic in the vault (the operator's personal brand / Instagram / content \
strategy) — answer from those docs, do not treat it as unknown.
- Start with the answer itself. Do NOT preface with "based on the context", "according to the \
information provided", "it seems", or similar — just state the fact.
- Cite your sources inline as [[doc-id]], using the EXACT id shown in [brackets] before each document. \
Never invent an id.
- Each document is tagged with its state. NEVER present a SUPERSEDED document's content as a current \
fact. If the most relevant document is superseded, do not answer from it — say it was superseded and \
point to its replacement (the successor id given). Only when the user explicitly asks about the PAST / \
history may you cite a superseded document, and then you MUST label it as superseded.
- You EXPLAIN decisions. You never make, change, or recommend changing a decision, and you take no action.
- Address the user as "you". Never use a name.

FORMAT (required): write the 2-4 sentence answer, then on a NEW line write "Sources:" followed by the \
[[doc-id]] of EVERY document your answer used, comma-separated, using the exact id from the [brackets]. \
Example:
The app uses 5 bottom tabs to keep mobile navigation clear, and a Schedule tab was rejected as redundant.
Sources: [[decision_app-navigation]]"""


# ask-Almanac persona — the Almanac decision-vault (trading algorithm) variant of the same read-only agent.
ALMANAC_VAULT_SYSTEM = """You are Almanac's institutional memory — a READ-ONLY assistant that answers questions \
about Almanac's own trading-algorithm decisions, strategy, backtesting and risk, using ONLY the decision-vault \
evidence provided below.

HARD RULES:
- Answer in 2 to 4 short sentences of plain prose — ONE short paragraph. No bullet points, no lists, \
no headings, no bold. Never longer. If your answer is as long as the documents, you have failed the job.
- Base EVERY claim ONLY on the evidence below. If the evidence doesn't cover the question, say exactly: \
"That's not in the decision vault." Do NOT use outside knowledge and do NOT guess.
- Answer DIRECTLY. NEVER ask the user for clarification or more context, NEVER say you are confused or \
that there is confusion, and NEVER describe what the documents are about instead of answering. Either \
give the answer from the evidence, or say "That's not in the decision vault." — nothing in between. \
Note: SMC concepts (order blocks, imbalances/FVGs, OTE, liquidity, market structure), strategy specs, \
guardrails, and backtest/paper-trading results are real topics in this vault — answer from those docs.
- Start with the answer itself. Do NOT preface with "based on the context", "according to the \
information provided", "it seems", or similar — just state the fact.
- Cite your sources inline as [[doc-id]], using the EXACT id shown in [brackets] before each document. \
Never invent an id.
- Each document is tagged with its state. NEVER present a SUPERSEDED document's content as a current \
fact. If the most relevant document is superseded, do not answer from it — say it was superseded and \
point to its replacement (the successor id given). Only when the user explicitly asks about the PAST / \
history may you cite a superseded document, and then you MUST label it as superseded.
- You EXPLAIN decisions. You never make, change, or recommend changing a decision, and you take no action.
- STATUS questions (is X live / on paper / done / dropped / retired?): answer from the MOST EXPLICIT and \
MOST RECENT status statement in the evidence — a page's `status:` field, its latest "⟳" update line, or a \
clear "CLEARED / added / live / paper-trading / dropped / retired" statement. An instrument or strategy \
that has been "cleared to paper", "added to paper", or marked "paper-trading" IS on paper, even if its \
first live session is still pending or it runs on a limited schedule. Do NOT infer "not yet", "pending", \
or "still in its original setup" unless a statement explicitly says so.
- Address the user as "you". Never use a name.

FORMAT (required): write the 2-4 sentence answer, then on a NEW line write "Sources:" followed by the \
[[doc-id]] of EVERY document your answer used, comma-separated, using the exact id from the [brackets]. \
Example:
The v1 strategy trades OTE + FVG confluence on gold, and NAS100 was dropped for showing no edge.
Sources: [[strategy_ote-imbalance]], [[decision_instrument-selection]]"""


def _days_old(updated):
    try:
        return (dt.date.today() - dt.date.fromisoformat(str(updated).strip())).days
    except Exception:  # noqa: BLE001
        return None


def _vault_status_brief(statuses: list[dict] | None) -> str:
    """A compact current-status/KPI line per project — extra context the vault alone doesn't hold."""
    if not statuses:
        return ""
    lines = []
    for s in statuses:
        if not s.get("_ok"):
            continue
        nm = s.get("project") or s.get("_id")
        mets = s.get("metrics") or {}
        met = ", ".join(f"{k}={v}" for k, v in list(mets.items())[:6])
        lines.append(f"- {nm}: phase {s.get('phase')}, health {s.get('health')}"
                     + (f"; metrics: {met}" if met else ""))
    return ("SECONDARY CONTEXT — live project KPIs/status. Use ONLY when the question is about current "
            "numbers or status; otherwise IGNORE this entirely and answer from the decision-vault "
            "evidence.\n" + "\n".join(lines)) if lines else ""


def ask_vault(user_msg: str, evidence: dict, statuses: list[dict] | None = None,
              history: list[dict] | None = None, role: str = "ask-orchard", timeout: int = 120,
              persona: str = VAULT_SYSTEM) -> dict:
    """Answer `user_msg` grounded ONLY in vault `evidence` (from vault.search: {live, superseded}).
    Returns {ok, answer, sources:[{id,label,state,successor}], cited:[id...], error}. Read-only —
    no tools, no navigation, no dispatch. `cited` are the doc ids the answer referenced (for the graph).
    The MODEL/substrate is resolved from `role` via the substrate seam (docs/model-agnosticism.md) —
    so ask-Orchard and ask-Almanac are each independently model-swappable by config."""
    live = (evidence or {}).get("live") or []
    sup = (evidence or {}).get("superseded") or []
    if not live and not sup:
        return {"ok": True, "answer": "That's not in the decision vault.",
                "sources": [], "cited": [], "error": None}
    ev = ["DECISION-VAULT EVIDENCE (answer ONLY from this):\n"]
    for d in live:
        ev.append(f'[{d["id"]}] "{d["label"]}" ({d.get("type")} · {d.get("state")} · '
                  f'{d.get("updated") or "n/a"})\n{d.get("markdown", "")}\n---')
    for d in sup:
        ev.append(f'[{d["id"]}] "{d["label"]}" (SUPERSEDED — replaced by '
                  f'{d.get("successor") or "unknown"})\n{d.get("markdown", "")}\n---')
    messages = [{"role": "system", "content": persona}]
    sb = _vault_status_brief(statuses)
    if sb:
        messages.append({"role": "system", "content": sb})       # secondary — placed BEFORE the evidence
    # The decision-vault evidence goes LAST among the system messages: a small model weights the most
    # recent context heaviest, so this stops it anchoring on the KPI block for decision questions.
    messages.append({"role": "system", "content": "\n".join(ev)})
    if history:
        messages += history[-6:]
    messages.append({"role": "user", "content": user_msg})
    # Route through the substrate seam: resolve(role) picks ollama/qwen (today) or claude/haiku (config).
    # max_tokens=200 preserves the old num_predict length cap on the Ollama path (ignored by the CLI,
    # which honors the persona's brevity rule + the post-processing below).
    res = substrate.chat(role, messages, temperature=0.2, max_tokens=200, timeout=timeout)
    if not res.get("ok"):
        return {"ok": False, "answer": "", "sources": [], "cited": [], "error": res.get("error")}
    raw = (res.get("text") or "").strip()
    by_id = {d["id"]: d for d in list(live) + list(sup)}
    cited, sources = [], []
    refused = bool(re.search(r"not in the decision vault", raw, re.I))
    if not refused:
        for m in re.finditer(r"\[\[([^\]|#]+)", raw):          # pull the [[id]] citations it emitted
            cid = m.group(1).strip()
            if cid in by_id and cid not in cited:
                cited.append(cid)
                d = by_id[cid]
                sources.append({"id": cid, "label": d["label"], "state": d["state"],
                                "successor": d.get("successor")})
        # Fallback: the small model doesn't always emit the Sources line. Retrieval ranks the right doc
        # #1, so when it cites nothing, attribute to the top live doc(s) whose title terms appear in the
        # answer, else the single top doc — honest given accurate retrieval; keeps chips + halos working.
        if not cited and live:
            al = raw.lower()
            for d in live[:3]:
                tt = [t for t in re.findall(r"[a-z0-9]+", d["label"].lower())
                      if len(t) > 2 and t not in ("the", "and", "for")]
                if d["label"].lower() in al or (tt and sum(t in al for t in tt) >= max(1, len(tt) // 2)):
                    cited.append(d["id"])
                    sources.append({"id": d["id"], "label": d["label"], "state": d["state"],
                                    "successor": d.get("successor")})
            if not cited:
                d = live[0]
                cited.append(d["id"])
                sources.append({"id": d["id"], "label": d["label"], "state": d["state"],
                                "successor": d.get("successor")})
    # Strip the trailing "Sources:" line, then HARD-GUARANTEE brevity (the small model sometimes ignores
    # "no lists / 2-4 sentences"). Flatten any markdown list/heading structure into plain prose, then cap
    # at 4 sentences / 600 chars — the UI never shows a wall of text, a dangling "intro:", or a bullet dump.
    answer = re.sub(r"\n*\s*sources?\s*:.*$", "", raw, flags=re.I | re.S).strip()
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", ln) for ln in answer.splitlines()]   # drop bullets
    lines = [re.sub(r"^#+\s*", "", ln) for ln in lines]                                    # drop headings
    answer = re.sub(r"\*\*|__|`", "", " ".join(l.strip() for l in lines if l.strip()))     # drop bold/code
    answer = re.sub(r"\s+", " ", answer.replace("�", "")).strip()
    for _ in range(2):                                         # strip hedgy preambles the model tacks on
        answer = re.sub(r"^\s*(based on [^,.:]*|according to [^,.:]*|it (?:seems|appears)(?: that)?|"
                        r"as (?:per|stated)[^,.:]*)[,:.]?\s+", "", answer, flags=re.I)
    answer = (answer[:1].upper() + answer[1:]) if answer else answer
    sents = re.split(r"(?<=[.!?])\s+", answer)[:4]              # at most 4 sentences
    out = ""
    for s in sents:                                            # keep WHOLE sentences up to ~600 chars
        if out and len(out) + len(s) + 1 > 600:
            break
        out = (out + " " + s).strip()
    answer = out or answer[:600]
    if len(answer) > 600:                                      # single mega-sentence → clean word cut
        answer = answer[:600].rsplit(" ", 1)[0].rstrip(",;:—- ") + "…"
    return {"ok": True, "answer": answer, "sources": sources, "cited": cited, "error": None}


# ===================== the Brain: retrieval (docs/brain.md §7) =====================
# Two passes. The FIRST hands the model every note's one-line description and asks which are
# relevant; the SECOND answers with those notes' full text as evidence. The candidate set is
# therefore deterministic (every note is always visible to the index pass) and only the SELECTION
# is model-driven: so retrieval can surface the wrong real note, in front of the operator, but can never
# invent one. Names that come back are checked against the index before anything is opened.
#
# Wired into the receptionist ONLY (§8). It is the visible consumer: a bad retrieval is seen
# immediately and nothing has happened. The autonomous consumers graduate later, one at a time.
BRAIN_MAX_NOTES = 5          # retrieval injects WHOLE notes; five ~1 KB notes is the context budget

_BRAIN_SELECT = """You are choosing which of the operator's personal notes apply to his request.

Each line below is one note: `name` then its description. Reply with ONLY the names that apply, \
comma-separated, most relevant first. If none apply, reply exactly: NONE

Fewer is better. {k} is a CEILING, not a target: an irrelevant note actively degrades the answer, \
so never pad the list to reach it. Two right notes beat five with three wrong ones.

Two kinds of note apply, and the second is the one that gets missed:
1. TOPIC notes, about the subject he is asking about.
2. STANDING RULES about how he wants things written or done: style, tone, formatting, wording, \
working method. If the request involves PRODUCING anything (copy, a document, a plan, a message, \
code) then every standing rule about producing things applies, whatever the topic is. A rule does \
not have to be about the subject to govern the answer.

Do not explain. Do not invent a name. Only names from this list.

NOTES:
{index}

REQUEST: {q}"""


def brain_evidence(user_msg: str, k: int = BRAIN_MAX_NOTES) -> list[dict]:
    """The notes the Brain offers for this question. Never raises: the Brain is an enrichment, and
    a receptionist that fails because the corpus is unreadable is worse than one without it."""
    try:
        import brain
    except Exception:  # noqa: BLE001
        return []
    try:
        idx = brain.index()
        if not idx:
            return []
        listing = "\n".join(f"{n['name']}: {n['description']}" for n in idx)
        known = {n["name"] for n in idx}
        picked: list[str] = []
        try:
            res = substrate.chat("receptionist",
                                 [{"role": "user",
                                   "content": _BRAIN_SELECT.format(k=k, index=listing, q=user_msg)}],
                                 temperature=0, timeout=45)
            if res.get("ok"):
                text = (res.get("text") or "").strip()
                if text.upper() != "NONE":
                    for raw in re.split(r"[,\n]", text):
                        nm = raw.strip().strip("`'\"[]").lower()
                        # the deterministic guard: a name the model made up is simply dropped
                        if nm in known and nm not in picked:
                            picked.append(nm)
        except Exception:  # noqa: BLE001
            picked = []
        if not picked:
            # selection failed or returned nothing usable → degrade to deterministic matching
            # rather than to no Brain at all
            return brain.search(user_msg, limit=k)
        return [n for n in (brain.read_note(nm) for nm in picked[:k]) if n]
    except Exception:  # noqa: BLE001
        return []


# A note's `type` decides how it is injected, and the distinction turned out to matter. Feeding a
# style rule as background produced answers that cited it and violated it in the same breath: the
# model read a formatting rule as something true ABOUT the operator rather than something binding on
# the reply. Rules arrive as constraints; facts arrive as context.
_RULE_TYPES = {"taste", "preference", "constraint", "decision-pattern"}


def brain_block(notes: list[dict]) -> str:
    rules = [n for n in notes if (n.get("type") or "") in _RULE_TYPES]
    facts = [n for n in notes if n not in rules]
    out = []
    if rules:
        out.append(
            "HARD RULES: the user's standing instructions for how you write and work. These are "
            "NOT background information. They bind this reply and they override your own habits, "
            "including your default writing style. Before you send your answer, check it against "
            "every rule here and fix anything that breaks one.\n\n" +
            "\n\n".join(f"RULE [{n['name']}]: {n['description']}\n{n['body']}" for n in rules))
    if facts:
        out.append(
            "WHAT YOU KNOW ABOUT THE USER: context about him, to make the answer specific rather "
            "than generic.\n\n" +
            "\n\n".join(f"[{n['name']}] {n['description']}\n{n['body']}" for n in facts))
    out.append("Never quote a note back at him verbatim, and never mention that you were given notes.")
    return "\n\n".join(out)


def _cited(notes: list[dict]) -> list[dict]:
    """What the client renders as chips: the notes RETRIEVAL SUPPLIED, not the ones the model claims
    it used. That distinction is the whole point of the soak (§11): "wrong note" then measures
    retrieval, which is the thing under test, rather than the model's honesty about its own reasoning.
    """
    return [{"name": n["name"], "description": n["description"]} for n in (notes or [])]


def build_context(statuses: list[dict], jobs: list[dict] | None = None) -> list[dict]:
    """Compact the loaded status sidecars into the state the model reasons over.
    Staleness is precomputed so the model never has to do date math. Live dispatch jobs (a project's
    current SESSIONS) are folded into each project's `active_sessions` — embedded with the project so
    the small model actually uses it for 'what's running / waiting on me' (a separate block was ignored)."""
    by_proj: dict[str, list[dict]] = {}
    for j in (jobs or []):
        by_proj.setdefault(j.get("project"), []).append({
            "state": j.get("status"),
            "doing": j.get("task"),
            "awaiting_input_question": j.get("question"),
        })
    out = []
    for s in statuses:
        if not s.get("_ok"):
            out.append({"id": s.get("_id"), "error": "status file unreadable"})
            continue
        pid = s.get("_id")
        out.append({
            "id": pid,
            "name": s.get("project"),
            "kind": s.get("kind"),
            "status": s.get("status"),
            "phase": s.get("phase"),
            "health": s.get("health"),
            "updated": str(s.get("updated")),
            "days_since_update": _days_old(s.get("updated")),
            "next_target": s.get("next_target"),
            "blockers": s.get("blockers") or [],
            "metrics": s.get("metrics") or {},
            "tracks": s.get("tracks") or [],
            "active_sessions": by_proj.get(pid, []),   # [] = nothing running for this project
            # The next task on this project's PLAN (docs/planner.md). Lets the receptionist answer
            # "what's next / what's the plan" from the plan itself, and know which task to LAUNCH.
            "planned_next_task": _plan_next_brief(pid),
        })
    return out


def _plan_next_brief(pid: str) -> dict | None:
    """Compact the plan's next task for the model's context (None = nothing queued on the plan)."""
    try:
        nt = dispatcher.next_planned_task(pid)
    except Exception:  # noqa: BLE001
        return None
    if not nt:
        return None
    return {
        "title": nt.get("title"),
        "type": nt.get("type"),               # dispatch (a worker can do it) | manual (you do it)
        "priority": nt.get("priority"),
        "milestone": nt.get("milestone_title"),
        "summary": (nt.get("notes") or "")[:240],
    }


def _sessions_brief(jobs: list[dict] | None) -> str:
    """A pre-digested, plain-English summary of the live sessions. The small local model relays a
    ready-made sentence reliably, where it ignores the same facts buried in JSON. Authoritative for
    'what's running / waiting on me' questions."""
    # The small model is unreliable at reasoning over these facts, so we hand it an explicit verdict
    # AND a ready-to-speak sentence. Strong/directive (it obeys "YES"/"NO"), but phrased to address "you"
    # and read naturally if spoken — NO raw labels, never the user's name.
    if not jobs:
        return ('If asked whether anything is waiting on you, or what is running/pending, the answer is NO. '
                'Say exactly: "Nothing is waiting on you right now."')
    lines, waiting_on = [], []
    for j in jobs:
        p, st = j.get("project"), j.get("status")
        if st == "needs_input":
            q = (j.get("question") or "").strip() or "needs your input"
            lines.append(f'- {p}: a session is waiting for you — it asks: "{q}"')
            waiting_on.append(p)
        elif st == "ready":
            lines.append(f"- {p}: a session is ready to merge and waiting for you.")
            waiting_on.append(p)
        else:
            lines.append(f'- {p}: a session is running (state: {st}) — {(j.get("task") or "work in progress")}')
    names = ", ".join(waiting_on) if waiting_on else ""
    verb = "has" if len(waiting_on) == 1 else "have"
    verdict = (f'If asked whether anything is waiting on you, or what is running/pending: the answer is YES. '
               f'Say: "Yes — {names} {verb} a session waiting for you," then add its question.'
               if waiting_on else
               'If asked whether anything is waiting on you, the answer is NO. Say nothing is.')
    return (verdict + "\n\nLive sessions (use these EXACT facts; speak naturally and address the user as "
            "'you', never by name):\n" + "\n".join(lines))


_KPI_LABELS = {"instagram_followers": "Instagram followers", "posts_published": "posts published",
               "paying_subs": "paying app subscribers", "monthly_net_eur": "monthly net revenue (EUR)",
               "pilot_coaches": "pilot coaches (B2B)", "content_docs": "knowledge-base docs",
               "tests_passing": "tests passing"}


def _kpi_brief(statuses: list[dict] | None) -> str:
    """A pre-digested, plain-English list of each project's live METRICS/KPIs. The small local model
    reliably relays a ready-made line but ignores the same numbers buried in the JSON state (same reason
    _sessions_brief exists). Authoritative for 'how many followers / subs / revenue / KPIs' questions."""
    lines = []
    for s in (statuses or []):
        if not s.get("_ok"):
            continue
        m = s.get("metrics") or {}
        if not m:
            continue
        name = s.get("project") or s.get("_id")
        parts = [f"{_KPI_LABELS.get(k, k.replace('_', ' '))}: "
                 f"{'€' + str(v) if k == 'monthly_net_eur' else v}" for k, v in m.items()]
        lines.append(f"- {name}: " + "; ".join(parts))
    if not lines:
        return ""
    return ("LIVE KPIs / METRICS per project — use these EXACT numbers to answer ANY question about "
            "metrics, KPIs, followers, subscribers, revenue, posts, or tests. Address the user as 'you', "
            "never by name:\n" + "\n".join(lines))


def _tools(ids: list[str], allow_nav: bool = True, allow_dispatch: bool = True,
           allow_next: bool = True) -> list[dict]:
    # Navigation tools are OFFERED only when the caller allows it. The control center Ask box passes
    # allow_nav=False so the (small, flaky) model can't wrongly "fly you somewhere" on a question
    # or a work request — navigation is handled deterministically client-side instead.
    # allow_dispatch=False (the VOICE path) drops the free-form dispatch tool so the small model can't
    # auto-draft arbitrary dispatches from plain spoken questions (over-triggering the tool).
    # allow_next governs dispatch_next_task SEPARATELY — it's the SAFE, deterministic "launch the
    # planned task" bridge (no hand-written brief; the plan supplies it; you still confirm), so it can
    # stay on even for voice.
    nav = [
        {"type": "function", "function": {
            "name": "focus_project",
            "description": "Open/focus a project's panel in the dashboard so the user can see its full detail.",
            "parameters": {"type": "object", "properties": {
                "id": {"type": "string", "enum": ids,
                       "description": "The project id to open."}},
                "required": ["id"]}}},
        {"type": "function", "function": {
            "name": "clear_focus",
            "description": "Close any open project panel and return to the full overview of all projects.",
            "parameters": {"type": "object", "properties": {}}}},
    ] if allow_nav else []
    dispatch = [
        {"type": "function", "function": {
            "name": "dispatch_to_claude",
            "description": "Hand an autonomous Claude Code session a task brief to execute in a "
                           "project's repo. The user confirms the brief before it runs. Use this ONLY "
                           "when the user DESCRIBES the work themselves — not for 'the next task'.",
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string", "enum": ids,
                            "description": "The project id to do the work in."},
                "session_type": {"type": "string",
                                 "description": "The kind of session; pick from the project's listed types."},
                "task": {"type": "string",
                         "description": "A clear, optimized brief: goal, scope, definition of done."}},
                "required": ["project", "task"]}}},
    ] if allow_dispatch else []
    nextt = [
        {"type": "function", "function": {
            "name": "dispatch_next_task",
            "description": "Launch the NEXT PLANNED task for a project — what the user means by "
                           "'the next task', 'what's next', 'launch it', 'start the next one', 'work "
                           "on the plan'. It reads the project's plan, takes the task queued next, and "
                           "starts a dispatch session (scoped, waiting for the user's confirmation). "
                           "Do NOT write a brief yourself — the plan supplies it. Prefer this over "
                           "dispatch_to_claude whenever the user refers to the planned/next task "
                           "rather than describing new work.",
            "parameters": {"type": "object", "properties": {
                "project": {"type": "string", "enum": ids,
                            "description": "The project id whose next planned task to launch."}},
                "required": ["project"]}}},
    ] if allow_next else []
    return nav + dispatch + nextt


def _coerce(args):
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:  # noqa: BLE001
            return {}
    return args or {}


def _strip_md(s: str) -> str:
    """Flatten the light markdown a frontier model emits (**bold**, __underline__, `code`). The control center
    reply box and TTS render plain text, so the raw markers would show as literal ** / ` (and be spoken).
    ask_vault does the same inline; the receptionist answer needs it now that it runs on Haiku."""
    return re.sub(r"\*\*|__|`", "", s or "")


def _match(name, args, ids):
    if name == "focus_project" and args.get("id") in ids:
        return ("focus", args["id"])
    if name == "clear_focus":
        return ("clear", None)
    if name == "dispatch_to_claude" and args.get("project") in ids and args.get("task"):
        return ("dispatch", {"project": args["project"],
                             "session_type": args.get("session_type"),
                             "task": args.get("task")})
    if name == "dispatch_next_task" and args.get("project") in ids:
        return ("next", {"project": args["project"]})   # resolved to a concrete dispatch in ask()
    return None


# The small local model over-triggers tool calls — it will call dispatch_next_task even on a plain
# QUESTION ("what's the next task?"). So the tool call only means "this is about project X's next
# task"; whether we LAUNCH or just DESCRIBE it is decided HERE, deterministically, from the user's
# actual words. A real imperative launches; anything else (a question) is answered.
_LAUNCH_RE = re.compile(
    r"\b(launch|start|run|kick[\s-]?off|go ahead|do it|dispatch|begin|fire|proceed|execute|"
    r"work on|tackle|take on|get (?:it )?(?:going|started)|let'?s (?:do|go|start)|"
    # affirmations — the user confirming after the receptionist offered to dispatch ("yes please")
    r"yes|yeah|yep|yup|sure|okay|ok|affirmative|absolutely|please do|go for it|sounds good|"
    r"confirm(?:ed)?)\b", re.I)
# A bare question NEVER launches, even if it happens to contain an affirmation-ish word.
_QUESTION_RE = re.compile(r"\b(what|which|when|why|how|whats|what's|is there|are there|tell me|"
                          r"list|show me)\b", re.I)


def _is_launch_intent(msg: str) -> bool:
    """True only if the user gave a launch imperative OR a bare affirmation (confirming a just-offered
    dispatch) — and did NOT phrase it as a question. Deterministic gate over the flaky small model,
    which over-calls the launch tool even on plain questions."""
    m = msg or ""
    if _QUESTION_RE.search(m):
        return False
    return bool(_LAUNCH_RE.search(m))


_NEXT_EXPLICIT_RE = re.compile(r"\b(next task|next one|next slice|planned task|the plan|"
                               r"what'?s next|what is next)\b", re.I)
_BARE_AFFIRM_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok|okay|absolutely|affirmative|please do|go for it|go ahead|"
    r"do it|confirm(?:ed)?|sounds good)[\s.!,]*(?:please)?[\s.!,]*$", re.I)
_NEXT_QUERY_RE = re.compile(
    r"(\bnext task\b|\bnext one\b|\bnext slice\b|what'?s next|what is next|\bnext on\b|\bnext for\b|"
    r"\bthe plan\b|\bplanned task\b|what should i (?:do|work on|tackle|start|build)|"
    r"what do i (?:do|work on|tackle)|what'?s on (?:the plan|my plate|the deck|for)|"
    r"whats up next|what'?s (?:the )?next)", re.I)


def _wants_launch_next(msg: str) -> bool:
    """True when the user wants the NEXT PLANNED task launched: an explicit 'next task' launch, a bare
    affirmation ('yes please' — confirming a just-offered dispatch), or a SHORT pronoun launch
    ('dispatch it', 'launch it please'). Long messages that describe their own work are NOT this — those
    are free-form dispatches the model handles."""
    m = (msg or "").strip()
    if not _is_launch_intent(m):
        return False
    if _NEXT_EXPLICIT_RE.search(m) or _BARE_AFFIRM_RE.match(m):
        return True
    words = re.findall(r"[a-z']+", m.lower())
    return len(words) <= 5 and bool(re.search(r"\b(it|this|that|the task|the one)\b", m, re.I))


def _asks_next_task(msg: str) -> bool:
    """True when the user asks what the next/planned task is (a describe, not a launch)."""
    return bool(_NEXT_QUERY_RE.search(msg or ""))


def _find_project_in(text: str, ctx: list, ids: list) -> str | None:
    t = (text or "").lower()
    for pid in ids:
        if re.search(r"\b" + re.escape((pid or "").lower()) + r"\b", t):
            return pid
    for p in ctx:                                   # else by display name ("Orchard Ops")
        name = (p.get("name") or "").strip().lower()
        if name and name in t:
            return p.get("id")
    return None


def _target_project(msg: str, history: list | None, ctx: list, ids: list) -> str | None:
    """The project the user means: named in this message, else the most recent one named in the
    conversation — so 'launch it' after 'what's next on orchard-ops' resolves to orchard-ops."""
    p = _find_project_in(msg, ctx, ids)
    if p:
        return p
    for h in reversed(history or []):
        p = _find_project_in(h.get("content", ""), ctx, ids)
        if p:
            return p
    return None


def ask(user_msg: str, statuses: list[dict], history: list[dict] | None = None,
        model: str = MODEL, timeout: int = 120, allow_nav: bool = True,
        allow_dispatch: bool = True, jobs: list[dict] | None = None,
        allow_next: bool = True) -> dict:
    """Returns {answer, action, error}. action is ('focus', id) | ('clear', None) | ('dispatch',..) | None.
    allow_nav=False (the control center Ask box) drops the navigation tools; allow_dispatch=False (the VOICE
    path) drops the free-form dispatch tool; allow_next governs the SAFE planned-task launch separately.
    All are belt-and-suspenders enforced after extraction too, never inferred by the small model."""
    ctx = build_context(statuses, jobs)
    ids = [p["id"] for p in ctx if p.get("id")]
    try:
        st_map = dispatcher.session_types()
    except Exception:  # noqa: BLE001
        st_map = {}
    charter = (_RECEPT.get("charter") or "").strip()
    sys_content = (f"Your charter (from agents.yaml): {charter}\n\n" if charter else "") + \
        SYSTEM.format(ids=", ".join(ids), session_types=json.dumps(st_map))
    messages = [
        {"role": "system", "content": sys_content},
        {"role": "system", "content": "LIVE PROJECT STATE (JSON). Each project's `active_sessions` is "
                                       "its current dispatch session(s); [] means none are running:\n"
                                       + json.dumps(ctx, ensure_ascii=False)},
    ]
    if history:
        messages += history[-8:]
    # KPI + sessions briefs go LAST (freshest = most salient for the small model) so the big JSON state
    # can't drown them — the model ignores numbers buried in JSON but relays a ready-made line reliably.
    kpi = _kpi_brief(statuses)
    if kpi:
        messages.append({"role": "system", "content": kpi})
    messages.append({"role": "system", "content": _sessions_brief(jobs)})
    # The Brain goes in LAST, right before the question: these are standing instructions about the
    # person asking, and the small model weights what is nearest the question most heavily.
    brain_notes = brain_evidence(user_msg)
    if brain_notes:
        messages.append({"role": "system", "content": brain_block(brain_notes)})
    messages.append({"role": "user", "content": user_msg})

    # Route through the substrate seam: resolve("receptionist") → claude/haiku (today) or ollama/qwen
    # (config). Tools work on BOTH — native function-calling on Ollama, a parsed ```action block on the
    # CLI — normalized to [{name, arguments}], so the extraction below is substrate-blind.
    res = substrate.chat("receptionist", messages,
                         tools=_tools(ids, allow_nav, allow_dispatch, allow_next),
                         temperature=0.2, timeout=timeout)
    if not res.get("ok"):
        return {"answer": "", "action": None, "error": res.get("error"),
                "brain_notes": _cited(brain_notes)}
    text = (res.get("text") or "").strip()          # already clean: inline-JSON blanked, action block stripped
    action = None
    for tc in (res.get("tool_calls") or []):
        a = _match(tc.get("name"), _coerce(tc.get("arguments")), ids)
        if a:
            action = a
            break
    if not allow_nav and action and action[0] in ("focus", "clear"):
        action = None     # belt-and-suspenders: never navigate from the Ask box
    if not allow_dispatch and action and action[0] == "dispatch":
        action = None     # never auto-draft a free-form dispatch when disallowed
    if not allow_next and action and action[0] == "next":
        action = None

    # --- PLANNED-TASK intent, resolved DETERMINISTICALLY (never trust the small model's tool call) ---
    # The model's tool call (if any) is just one hint. Whether we LAUNCH the next task, DESCRIBE it, or
    # do neither is decided here from the user's actual words + the recent conversation — so a follow-up
    # like "can you dispatch it please" (after the receptionist named the next task) launches reliably,
    # even when the model answered in plain text and never called the tool.
    resolved_answer = None
    if allow_next:
        model_next_proj = action[1]["project"] if (action and action[0] == "next") else None
        want_launch = _wants_launch_next(user_msg)
        if want_launch or model_next_proj or _asks_next_task(user_msg):
            model_answer = text
            # resolve the project from: the message, the conversation, and the model's OWN answer text
            # (it often names the project — "the next task on the Orchard Ops project…").
            target = (model_next_proj or _target_project(user_msg, history, ctx, ids)
                      or _find_project_in(model_answer, ctx, ids))
            try:
                nt = dispatcher.next_planned_task(target) if target else None
            except Exception:  # noqa: BLE001
                nt = None
            if want_launch:
                if not target:
                    action, resolved_answer = None, "Which project's next task should I launch?"
                elif not nt:
                    action, resolved_answer = None, f"{target} has nothing queued next on its plan."
                elif nt.get("type") == "dispatch":
                    action = ("dispatch", {"project": target, "session_type": None,
                                           "task": dispatcher.brief_from_task(nt), "scope": True})
                    resolved_answer = (f'Launching the next task on {target}: "{nt.get("title")}". '
                                       "Confirm the brief and it goes to scoping.")
                else:
                    action, resolved_answer = None, (
                        f'The next task on {target} is "{nt.get("title")}" — that one is manual, '
                        "you handle it yourself, so there's nothing to dispatch.")
            else:                  # DESCRIBE what's next, and ALWAYS offer the launch
                if target and nt:
                    manual = " — a manual task you handle yourself" if nt.get("type") == "manual" else ""
                    action, resolved_answer = None, (
                        f'The next task on {target} is "{nt.get("title")}"{manual}. '
                        'Say "launch it" to start it.')
                elif model_answer:   # keep the model's own answer, but tack the offer onto the end
                    action, resolved_answer = None, (
                        model_answer.rstrip(" .") + '. Say "launch it" and I\'ll start it.')
                # else: nothing useful to add → leave as-is

    if action and action[0] == "next":            # never leak a raw ('next', …) action downstream
        action = None
    answer = resolved_answer if resolved_answer is not None else text
    return {"answer": _strip_md(answer), "action": action, "error": None,
            "brain_notes": _cited(brain_notes)}
