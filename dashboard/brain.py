"""
Atlas — the Brain: the personal knowledge layer's store (docs/brain.md).

The deterministic reader + WRITER for `D:\\work\\brain` — the curated corpus of atomic facts
about the operator. THE rule of the whole design (§6): **a model never writes a file.** Models
may propose; only this module commits. Every write goes through `validate()` → `write_note()`,
so the corpus cannot silently fill with model-invented facts. That property is structural, not
behavioural — there is no code path from a model to the disk that skips this module.

Files are the source of truth and there is NO derived index (§3.2): 2,000 notes at the ~1 KB
cap is ~2 MB, so the frontmatter scan that builds the description index runs per request. When
that stops being fast enough, the fix is a cache keyed on mtime — not a second source of truth.

The corpus lives OUTSIDE the Atlas repo (§4) so personal data never enters this codebase's git
history. It is not a project: registry.yaml gets a top-level `brain:` key, never an entry under
`projects:` (`state._load_registry()` reads `projects` only, so a Brain key cannot render as a
project card).

Stdlib + pyyaml. READ paths are defensive — an unreadable note degrades to a minimal record and
never raises, so one bad file can't blank the panel. WRITE paths raise `BrainError` with a
human-readable message: a rejected write must fail loudly and say why, inline, where it was typed.
"""
from __future__ import annotations

import os
import re
import datetime as dt
import subprocess
from pathlib import Path

# ---- the note contract (docs/brain.md §5) -----------------------------------------------
TYPES = ("taste", "preference", "decision-pattern", "constraint", "person", "fact")
CONFIDENCE = ("high", "medium", "low")
# The atomic cap. §5: "one fact per file, hard-capped (~150 words / ~1 KB)". Retrieval injects
# WHOLE notes, so a long note is a bad retrieval unit — the cap is what makes the corpus usable,
# not a storage concern. ~1000 chars is ~150 words of ordinary prose; ONE hard rule, measured in
# characters so the error can name an exact number.
BODY_MAX_CHARS = 1000
DESC_MAX_CHARS = 160          # the retrieval key (§7) — one line, must stand alone
# §11: staleness is a low-priority queue INSIDE the panel, never the Pulse rail. Six months.
STALE_DAYS = 180

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.S)


class BrainError(Exception):
    """A write was rejected. The message is shown to the user verbatim, inline."""


def _norm(p: str) -> str:
    """Windows path → posix when running under WSL. Mirrors state.py / vault.py."""
    if os.name != "nt":
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
        if m:
            return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}"
    return p


_ATLAS_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY = _ATLAS_ROOT / "registry.yaml"


def _registry() -> dict:
    import yaml
    try:
        return yaml.safe_load(_REGISTRY.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — an unreadable registry falls back to defaults
        return {}


def brain_root() -> Path:
    """Where the corpus lives. ATLAS_BRAIN wins (tests + a relocated corpus), then registry.yaml's
    top-level `brain:` key (a mapping with `path:`, or a bare string), then the documented default."""
    raw = os.environ.get("ATLAS_BRAIN")
    if not raw:
        b = _registry().get("brain")
        if isinstance(b, dict):
            raw = b.get("path")
        elif isinstance(b, str):
            raw = b
    return Path(_norm(raw or r"D:\work\brain"))


def scopes() -> list[str]:
    """The allowed `scope` vocabulary: `global`, `atlas`, every registry project id, every umbrella
    parent. Constrained rather than free-form (decided at build time, §13.1) so a typo cannot
    create a scope that silently filters to nothing — the panel renders these as fixed segments.

    `atlas` is listed EXPLICITLY: Atlas is the control plane, so it has no entry under `projects:`,
    yet a large share of the corpus is taste about how Atlas itself gets built (§5's own example
    note is exactly that). Deriving the vocabulary from the registry alone would have made those
    facts unscopeable."""
    out = ["global", "atlas"]
    for e in (_registry().get("projects") or []):
        if not isinstance(e, dict):
            continue
        pid = (e.get("id") or "").strip()
        if pid and pid not in out:
            out.append(pid)
        par = (e.get("parent") or "").strip()
        if par and par not in out:
            out.append(par)
    return out


# ---- slugs ------------------------------------------------------------------------------
_STOPWORDS = {"a", "an", "the", "of", "to", "and", "or", "in", "on", "at", "for", "is", "it",
              "by", "as", "so", "but", "that", "than", "with", "not", "was", "are", "be", "its",
              "their", "his", "my", "your", "over", "into", "from", "when", "which", "all",
              "any", "every", "this", "these"}
NAME_MAX_CHARS = 40      # derived names; an explicit name may run to 80 (validate())


def slugify(text: str) -> str:
    """Normalize an id: kebab-case, nothing else. NO truncation — this is the LOOKUP path
    (`read_note`, `delete_note`, an explicit `name`), and a lookup that silently shortens its
    argument cannot find a file whose name is longer than the cap."""
    return _SLUG_RE.sub("-", (text or "").lower()).strip("-") or "note"


def _drop_trailing_stopwords(s: str) -> str:
    parts = s.split("-")
    while len(parts) > 1 and parts[-1] in _STOPWORDS:
        parts.pop()
    return "-".join(parts) or "note"


def derive_name(description: str) -> str:
    """The naming convention: turn a description into the name a human would have picked.

    Used ONLY when no explicit name is given — i.e. the capture path, which by design asks for
    nothing but the text (§11). Three deterministic steps, each applied only if still too long:

      1. keep the FIRST CLAUSE — the head of a sentence carries the fact, the tail qualifies it
         ("Adds an index at the point of need, never ahead of…" → "adds-an-index-at-the-point-of-need");
      2. drop stop-words anywhere, keeping content words in order;
      3. truncate on a word boundary.

    This exists so renaming never has to. A name is permanent once written — it is the [[wikilink]]
    target, and a writer that renamed files would leave every inbound link dangling. When a note
    later deserves a shorter handle, the answer is an `aliases:` entry (resolved by `resolve_link`
    and by Obsidian natively), which ADDS a target instead of moving one.
    """
    head = re.split(r"[,;:—(]", description or "")[0].strip() or (description or "")
    s = slugify(head)
    if len(s) <= NAME_MAX_CHARS:
        return _drop_trailing_stopwords(s)
    s = "-".join(p for p in s.split("-") if p not in _STOPWORDS) or s
    if len(s) <= NAME_MAX_CHARS:
        return _drop_trailing_stopwords(s)
    return _drop_trailing_stopwords(s[:NAME_MAX_CHARS].rsplit("-", 1)[0] or s[:NAME_MAX_CHARS])


def _unique_slug(base: str, root: Path) -> str:
    """`base`, or `base-2`, `base-3`… — never silently overwrite a different fact."""
    name, n = base, 2
    while (root / f"{name}.md").exists():
        name = f"{base}-{n}"
        n += 1
    return name


# ---- validation -------------------------------------------------------------------------
def validate(fields: dict) -> dict:
    """Normalize + check a candidate note. Returns the clean field dict; raises BrainError with a
    message written for a human reading it inline. This is the ONLY gate on the corpus's shape —
    markdown's one real weakness is frontmatter drift, and this is the fix (§5)."""
    out: dict = {}

    desc = (fields.get("description") or "").strip().replace("\n", " ")
    if not desc:
        raise BrainError("A note needs a one-line description — it's how retrieval finds it.")
    if len(desc) > DESC_MAX_CHARS:
        raise BrainError(f"Description is {len(desc)} characters; keep it under {DESC_MAX_CHARS}.")
    out["description"] = desc

    body = (fields.get("body") or "").strip()
    if not body:
        raise BrainError("A note needs a body — the fact itself.")
    if len(body) > BODY_MAX_CHARS:
        raise BrainError(
            f"That's {len(body)} characters; the cap is {BODY_MAX_CHARS} (~150 words). "
            "If it wants to be longer it's two notes — or it belongs in a vault.")
    out["body"] = body

    typ = (fields.get("type") or "").strip().lower()
    if typ and typ not in TYPES:
        raise BrainError(f"Unknown type '{typ}'. Use one of: {', '.join(TYPES)}.")
    if typ:
        out["type"] = typ

    scope = (fields.get("scope") or "").strip().lower()
    if scope:
        allowed = scopes()
        if scope not in allowed:
            raise BrainError(f"Unknown scope '{scope}'. Use one of: {', '.join(allowed)}.")
        out["scope"] = scope

    conf = (fields.get("confidence") or "high").strip().lower()
    if conf not in CONFIDENCE:
        raise BrainError(f"Confidence must be one of: {', '.join(CONFIDENCE)}.")
    out["confidence"] = conf

    src = (fields.get("source") or "capture").strip().replace("\n", " ")
    out["source"] = src

    name = (fields.get("name") or "").strip().lower()
    if name:
        name = slugify(name)
        if len(name) > 80:
            raise BrainError(f"That name is {len(name)} characters; keep it under 80.")
    out["name"] = name

    # `aliases` — additional [[wikilink]] targets for the same note (§5's links, Obsidian's own
    # idiom). This is what makes RENAME unnecessary rather than merely deferred: a note that
    # deserves a shorter handle GAINS one, so no inbound link can ever be left dangling.
    raw = fields.get("aliases") or []
    if isinstance(raw, str):
        raw = [a for a in re.split(r"[,\n]", raw)]
    seen, aliases = set(), []
    for a in raw:
        a = slugify(str(a).strip())
        if a in ("", "note") or a in seen:
            continue
        seen.add(a)
        aliases.append(a)
    if aliases:
        taken = {}
        for rec in list_notes():
            if rec["name"] == out["name"]:
                continue
            taken[rec["name"]] = rec["name"]
            for al in rec.get("aliases") or []:
                taken[al] = rec["name"]
        for a in aliases:
            if a in taken:
                raise BrainError(f"The alias '{a}' already points at '{taken[a]}'.")
        out["aliases"] = aliases
    return out


# ---- serialization ----------------------------------------------------------------------
def _render(fm: dict, body: str) -> str:
    """Frontmatter written BY HAND, in a fixed key order — not yaml.dump. Stable ordering keeps
    git diffs readable (the whole point of the repo being git), and the file matches §5 exactly."""
    lines = ["---"]
    for key in ("name", "aliases", "description", "type", "scope", "confidence", "source",
                "created", "reviewed"):
        if key not in fm or fm[key] in (None, "", []):
            continue
        val = fm[key]
        if key == "aliases":                       # Obsidian's own list form, so it resolves natively
            lines.append("aliases: [" + ", ".join(val) + "]")
            continue
        # quote anything that could confuse a YAML parser; leave plain slugs/dates bare
        if key in ("description", "source") or re.search(r"[:#\"']", str(val)):
            val = '"' + str(val).replace('"', "'") + '"'
        lines.append(f"{key}: {val}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.strip() + "\n"


def _parse(text: str, name: str) -> dict:
    """One note file → a record. Defensive: no/garbled frontmatter still yields a usable record
    (description falls back to the first body line) so a hand-edited file never blanks the panel."""
    import yaml
    m = _FM_RE.match(text)
    fm, body = {}, text
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:  # noqa: BLE001
            fm = {}
        if not isinstance(fm, dict):
            fm = {}
        body = m.group(2)
    body = body.strip()
    desc = str(fm.get("description") or "").strip()
    if not desc:
        desc = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")[:DESC_MAX_CHARS]
    al = fm.get("aliases") or []
    if isinstance(al, str):
        al = re.split(r"[,\n]", al)
    rec = {
        "name": str(fm.get("name") or name).strip() or name,
        "aliases": [slugify(str(a)) for a in al if str(a).strip()],
        "description": desc,
        "type": (str(fm.get("type")).strip().lower() if fm.get("type") else ""),
        "scope": (str(fm.get("scope")).strip().lower() if fm.get("scope") else ""),
        "confidence": (str(fm.get("confidence")).strip().lower() if fm.get("confidence") else ""),
        "source": str(fm.get("source") or "").strip(),
        "created": _isodate(fm.get("created")),
        "reviewed": _isodate(fm.get("reviewed")),
        "body": body,
        "links": sorted(set(re.findall(r"\[\[([^\]|#]+)", body))),
    }
    rec["stale"] = _is_stale(rec)
    # §11: the panel's cheap batch pass — a note with no type OR no scope needs classifying.
    rec["unclassified"] = not (rec["type"] and rec["scope"])
    return rec


def _isodate(v) -> str:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    s = str(v or "").strip()
    try:
        return dt.date.fromisoformat(s).isoformat()
    except Exception:  # noqa: BLE001
        return ""


def _is_stale(rec: dict) -> bool:
    """`reviewed` + `confidence` drive the re-confirm queue (§9). A belief not re-confirmed in six
    months is surfaced to the operator rather than quietly steering agents forever."""
    d = rec.get("reviewed") or rec.get("created")
    if not d:
        return True
    try:
        return (dt.date.today() - dt.date.fromisoformat(d)).days > STALE_DAYS
    except Exception:  # noqa: BLE001
        return False


# ---- git (history + undo, §4) -----------------------------------------------------------
def _git(args: list[str], root: Path | None = None) -> bool:
    """Best-effort git. The repo is what gives 'delete = gone, but recoverable by you' its
    meaning (§9), so every write commits — but a missing/broken git NEVER fails a write. The
    corpus on disk is the source of truth; git is the safety net."""
    try:
        r = subprocess.run(["git", "-C", str(root or brain_root())] + args,
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _commit(msg: str) -> None:
    root = brain_root()
    if not (root / ".git").exists():
        return
    _git(["add", "-A"], root)
    _git(["commit", "-m", msg, "--no-gpg-sign"], root)


README = """# Brain

Atlas's personal knowledge layer — a **curated** corpus of atomic facts about the operator
(taste, preferences, decision patterns, constraints). Design of record: `Atlas/docs/brain.md`.

- **One fact per file**, ~1 KB cap. If it wants to be longer, it's two notes.
- **The membership test:** *if every project were cancelled tomorrow, would this still be true?*
  Yes → here. No → `status.yaml` / `plan.yaml` / the decision vaults.
- **Nothing here is written by a model.** Agents propose; Atlas's deterministic writer commits
  after you accept.
- Obsidian opens this folder directly. Editing a note by hand is a supported path.

Private and local. This repo never leaves the machine.
"""


def init_repo() -> dict:
    """Create the corpus dir + git repo if absent. Idempotent — safe to call on every server boot."""
    root = brain_root()
    root.mkdir(parents=True, exist_ok=True)
    created = False
    if not (root / ".git").exists():
        created = _git(["init"], root)
    gi = root / ".gitignore"
    if not gi.exists():
        # Obsidian's workspace files churn on every pane move — noise in a history meant for facts.
        gi.write_text(".obsidian/\n.trash/\n", encoding="utf-8")
    rd = root / "README.md"
    if not rd.exists():
        rd.write_text(README, encoding="utf-8")
    if created:
        _commit("brain: initialize the corpus")
    return {"ok": True, "root": str(root), "initialized": created}


# ---- read -------------------------------------------------------------------------------
def list_notes() -> list[dict]:
    """Every note, newest-reviewed first. This IS the description index of §7 — the candidate set
    is deterministic (every note is always visible to the index pass), so retrieval can surface the
    wrong real note but can never invent one."""
    root = brain_root()
    if not root.exists():
        return []
    out = []
    for p in sorted(root.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        try:
            out.append(_parse(p.read_text(encoding="utf-8"), p.stem))
        except Exception:  # noqa: BLE001 — one unreadable file must not blank the library
            out.append({"name": p.stem, "description": "(unreadable note)", "type": "", "scope": "",
                        "confidence": "", "source": "", "created": "", "reviewed": "", "body": "",
                        "links": [], "stale": True, "unclassified": True, "error": True})
    out.sort(key=lambda r: (r.get("reviewed") or r.get("created") or "", r.get("name") or ""), reverse=True)
    return out


def read_note(name: str) -> dict | None:
    """By name, else by alias — the same resolution Obsidian does, so a [[wikilink]] that points
    at an alias opens the right note here too."""
    p = brain_root() / f"{slugify(name)}.md"
    if p.exists():
        try:
            return _parse(p.read_text(encoding="utf-8"), p.stem)
        except Exception:  # noqa: BLE001
            return None
    target = resolve_link(name)
    return read_note(target) if target else None


def resolve_link(target: str) -> str | None:
    """A [[wikilink]] target → the note's name, resolving through `aliases`. Returns None for a
    dangling link (rendered as a dead chip rather than silently dropped)."""
    t = slugify(target)
    for rec in list_notes():
        if rec["name"] == t or t in (rec.get("aliases") or []):
            return rec["name"]
    return None


_STOPQ = _STOPWORDS | {"what", "how", "why", "when", "where", "who", "do", "does", "did", "should",
                       "would", "can", "could", "i", "you", "he", "me", "my", "is", "am", "about"}


def search(query: str, limit: int = 5) -> list[dict]:
    """Description-matching retrieval (§7 v1), deterministic. Scores every note by word overlap,
    description first because that is the retrieval key.

    This is the stable `brain.search()` interface the design promises will survive the move to
    embeddings: v2 replaces the scoring, not the signature. It is also the FALLBACK for the
    model-driven selection in assistant.py — when that call fails, retrieval degrades to this
    rather than to nothing.
    """
    terms = {w for w in re.split(r"[^a-z0-9]+", (query or "").lower()) if len(w) > 2 and w not in _STOPQ}
    if not terms:
        return []
    scored = []
    for rec in list_notes():
        desc = (rec.get("description") or "").lower()
        body = (rec.get("body") or "").lower()
        hits_d = sum(1 for t in terms if t in desc)
        hits_b = sum(1 for t in terms if t in body)
        score = hits_d * 3 + hits_b          # a description hit outweighs a body hit
        if score:
            scored.append((score, rec))
    scored.sort(key=lambda x: (-x[0], x[1].get("name") or ""))
    return [r for _, r in scored[:limit]]


def index() -> list[dict]:
    """The retrieval index: name + description only (§7 — 2,000 notes ≈ 200 KB). Slice 2 hands
    this to the selection pass; the panel uses list_notes() for the full records."""
    return [{"name": r["name"], "description": r["description"],
             "type": r.get("type", ""), "scope": r.get("scope", "")} for r in list_notes()]


# ---- write (the deterministic path — §6) ------------------------------------------------
def write_note(description: str, body: str, name: str = "", type: str = "", scope: str = "",
               confidence: str = "high", source: str = "capture", aliases=None,
               overwrite: bool = False) -> dict:
    """Validate → write → commit. The ONLY way a note reaches disk.

    `overwrite=False` (capture) never clobbers: a colliding slug becomes `slug-2`. `overwrite=True`
    (edit) requires `name` and rewrites that note in place, preserving its `created` date and
    stamping `reviewed` — editing a note IS re-confirming it (§9)."""
    fields = validate({"description": description, "body": body, "name": name, "type": type,
                       "scope": scope, "confidence": confidence, "source": source,
                       "aliases": aliases})
    root = brain_root()
    root.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()

    if overwrite:
        if not fields["name"]:
            raise BrainError("Editing a note needs its name.")
        slug = fields["name"]
        prev = read_note(slug)
        if prev is None:
            raise BrainError(f"No note named '{slug}' to edit.")
        created = prev.get("created") or today
    else:
        # no explicit name → the naming convention, so the capture path still produces a name a
        # human would have picked (derive_name); _unique_slug then guarantees it clobbers nothing.
        slug = _unique_slug(fields["name"] or derive_name(fields["description"]), root)
        created = today

    fm = {"name": slug, "aliases": fields.get("aliases") or [], "description": fields["description"],
          "type": fields.get("type", ""), "scope": fields.get("scope", ""),
          "confidence": fields["confidence"], "source": fields["source"],
          "created": created, "reviewed": today}
    path = root / f"{slug}.md"
    path.write_text(_render(fm, fields["body"]), encoding="utf-8")
    _commit(f"brain: {'update' if overwrite else 'add'} {slug}")
    return {"ok": True, "name": slug, "path": str(path)}


def capture(text: str, source: str = "capture") -> dict:
    """The ⌘K / voice path (§11): asks for NOTHING but the text. `description` is derived from what
    was typed; `type` and `scope` are left unset deliberately — retrieval v1 matches on description,
    so an unclassified note is fully retrievable the moment it is written. An unclassified note
    beats an uncaptured one; the panel carries a 'needs classifying' filter for a cheap batch pass."""
    text = (text or "").strip()
    if not text:
        raise BrainError("Nothing to remember — type the fact after `remember`.")
    first = re.split(r"(?<=[.!?])\s+", text)[0].strip() or text
    desc = first if len(first) <= DESC_MAX_CHARS else first[:DESC_MAX_CHARS - 1].rstrip() + "…"
    return write_note(description=desc, body=text, source=source)


def delete_note(name: str) -> dict:
    """Delete = gone (§9). No tombstone, no archive-by-default — this is personal data, and a
    'forget' that half-remembers is a broken promise. Git history is the only recoverability."""
    slug = slugify(name)
    p = brain_root() / f"{slug}.md"
    if not p.exists():
        raise BrainError(f"No note named '{slug}'.")
    p.unlink()
    _commit(f"brain: forget {slug}")
    return {"ok": True, "name": slug}


def reconfirm(name: str) -> dict:
    """Stamp `reviewed` = today: 'yes, this is still true'. The cheap answer to the staleness
    queue (§9) — one click, no edit."""
    rec = read_note(name)
    if rec is None:
        raise BrainError(f"No note named '{name}'.")
    return write_note(description=rec["description"], body=rec["body"], name=rec["name"],
                      type=rec.get("type", ""), scope=rec.get("scope", ""),
                      confidence=rec.get("confidence") or "high",
                      source=rec.get("source") or "capture",
                      aliases=rec.get("aliases") or [], overwrite=True)


# The soak's ledger (§8). Deliberately NOT in the corpus: these are observations about how
# retrieval performed, not facts about the operator, and mixing them would break the one rule that makes
# the Brain trustworthy. Lives beside Atlas's other operational state, gitignored.
FEEDBACK_LOG = _ATLAS_ROOT / "brain_soak.jsonl"
# §8's "roughly 30 real questions". A reference point for reading the panel, NOT a threshold the
# code acts on: graduation stays the operator's judgement call, and a number that promoted itself would be
# exactly the kind of silent decision the Brain is built to avoid.
SOAK_TARGET = 30


def log_feedback(kind: str, question: str, notes=None, note: str = "") -> dict:
    """Record a retrieval judgement: 'wrong' (it surfaced junk) or 'missing' (it should have known).

    §8 sets graduation at "over roughly 30 real questions, retrieval surfaces the right notes."
    That is only checkable if the misses are counted rather than remembered vaguely, so every
    answer carries the two controls and every click lands here."""
    import json
    if kind not in ("wrong", "missing"):
        raise BrainError(f"Unknown feedback kind '{kind}'.")
    rec = {"at": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind,
           "question": (question or "").strip()[:500],
           "notes": [str(n) for n in (notes or [])], "note": (note or "").strip()[:200]}
    with FEEDBACK_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, **soak_stats()}


def log_answer(notes=None) -> dict:
    """Record that the receptionist answered a question, and what retrieval supplied for it.

    §8's criterion is "over roughly 30 real questions" — a DENOMINATOR. Without it a count of
    misfires is unreadable: two wrong notes out of five answers and two out of fifty are opposite
    verdicts, and the soak cannot be read at a glance without one.

    Only the note names are kept, never the question text. The denominator needs a count, and when
    something did go wrong the judgement record already captures the question. Every answer counts,
    including those where retrieval supplied nothing — a question it should have known about but
    stayed silent on is exactly a 'should have known', and dropping it would flatter the numbers.
    """
    import json
    # Retrieval hands back whole note RECORDS, not names. Stringifying those would write a
    # kilobyte of dict repr per answer into a file whose only job is to be countable.
    names = [n.get("name", "") if isinstance(n, dict) else str(n) for n in (notes or [])]
    rec = {"at": dt.datetime.now().isoformat(timespec="seconds"), "kind": "answer",
           "notes": [n for n in names if n]}
    try:
        with FEEDBACK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:      # the soak's bookkeeping must never break a reply
        return {"ok": False}
    return {"ok": True}


def soak_stats() -> dict:
    """Answers-with-retrieval vs. judged misfires, for reading the soak at a glance."""
    import json
    answers = with_notes = wrong = missing = 0
    if FEEDBACK_LOG.exists():
        for line in FEEDBACK_LOG.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            k = rec.get("kind")
            if k == "answer":
                answers += 1
                if rec.get("notes"):
                    with_notes += 1
            elif k == "wrong":
                wrong += 1
            elif k == "missing":
                missing += 1
    return {"answers": answers, "with_notes": with_notes, "wrong": wrong, "missing": missing,
            "target": SOAK_TARGET}


def stats() -> dict:
    notes = list_notes()
    return {"ok": True, "root": str(brain_root()), "count": len(notes),
            "unclassified": sum(1 for n in notes if n.get("unclassified")),
            "stale": sum(1 for n in notes if n.get("stale")),
            "soak": soak_stats(),
            "types": list(TYPES), "scopes": scopes()}
