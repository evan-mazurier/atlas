"""
Atlas — read-only decision-vault reader.

Scans the Obsidian vault (markdown frontmatter + `[[wikilinks]]`) and returns a graph
(nodes + edges) plus per-document content, for the control center's Decision-Vault overview. This
is CONTROL-PLANE READ ONLY — Atlas renders/navigates the vault, it never edits it (edits stay
in Obsidian or a dispatched work-plane session). The vault is company-wide (all of the umbrella's
decisions live here), so the overview is an umbrella-level surface, not a sub-project one.

Stdlib + pyyaml. Defensive: an unreadable file degrades to a minimal node, never raises.
"""
from __future__ import annotations

import os
import re
import math
import datetime as dt
from pathlib import Path

# Vault location (override via ATLAS_VAULT). Windows→/mnt posix mapping.
_RAW_ROOT = os.environ.get("ATLAS_VAULT", r"D:\work\orchard-vault")
TODAY = dt.date.today()

_SKIP_DIRS = {".obsidian", ".git", ".claude", ".trash", "assets"}
_LINK_RE = re.compile(r"\[\[([^\]|#]+)")          # [[target]], [[target|alias]], [[target#heading]]
# filename-prefix → node type (used when frontmatter has no explicit `type:`)
_TYPE_PREFIX = {
    "decision": "decision", "concept": "concept", "entity": "entity", "src": "src",
    "synth": "synthesis", "reference": "reference", "index": "index",
}
_LOG_FILES = {"progress", "progress-archive", "log", "benchmark-findings", "content_queue"}


def _norm(p: str) -> str:
    if os.name != "nt":
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
        if m:
            return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}"
    return p


VAULT_ROOT = Path(_norm(_RAW_ROOT))

# ---- multi-vault resolution — serve a SECOND vault by id; the umbrella vault stays default.
_ENV_ROOT = os.environ.get("ATLAS_VAULT")     # explicit override still wins for the default vault


def _registry_entry(pid: str) -> dict | None:
    """Look a project up in registry.yaml. Read-only and defensive: a missing or malformed
    registry degrades to None (→ the default vault), never raises."""
    try:
        import yaml
        reg = Path(__file__).resolve().parent.parent / "registry.yaml"
        projects = (yaml.safe_load(reg.read_text(encoding="utf-8")) or {}).get("projects", []) or []
        return next((e for e in projects if e.get("id") == pid), None)
    except Exception:  # noqa: BLE001
        return None


def _vault_root(vault_id: str = "orchard") -> Path:
    """Resolve a vault's wiki root by id. Default (the umbrella vault) = VAULT_ROOT, unchanged; any other id
    reads its `vault_path` from registry.yaml (e.g. almanac → D:\\work\\almanac\\wiki)."""
    if vault_id in (None, "orchard"):
        return VAULT_ROOT
    raw = (_registry_entry(vault_id) or {}).get("vault_path")
    return Path(_norm(raw)) if raw else VAULT_ROOT


def _days_since(d) -> int | None:
    if not d:
        return None
    try:
        if isinstance(d, dt.datetime):
            d = d.date()
        elif isinstance(d, str):
            d = dt.date.fromisoformat(d.strip()[:10])
        return (TODAY - d).days
    except Exception:  # noqa: BLE001
        return None


def _iter_md(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".md"):
                yield Path(dirpath) / fn


def _lenient_frontmatter(block: str) -> dict:
    """Fallback when YAML parse fails (e.g. an unquoted URL in a `sources: [...]` flow list): pull the
    simple top-level `key: value` scalar lines so ONE bad line doesn't discard the whole block (which
    would otherwise drop the doc's title → the node renders as its raw slug)."""
    import re
    fm = {}
    for ln in block.splitlines():
        m = re.match(r'^([A-Za-z_][\w-]*):\s*(.*)$', ln)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if not v or v[0] in '[{|>#':          # skip lists / maps / block scalars — keep simple scalars only
            continue
        fm[k] = v.strip().strip('"\'')
    return fm


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            import yaml
            try:
                fm = yaml.safe_load(text[3:end]) or {}
            except Exception:  # noqa: BLE001
                fm = _lenient_frontmatter(text[3:end])   # a bad line shouldn't nuke title/type/dates
            return (fm if isinstance(fm, dict) else {}), text[end + 4:]
    return {}, text


def _infer_type(name: str, fm: dict, folder: str) -> str:
    t = str(fm.get("type") or "").strip().lower()
    if t:
        return t
    prefix = name.split("_", 1)[0]
    if prefix in _TYPE_PREFIX:
        return _TYPE_PREFIX[prefix]
    if folder == "raw":
        return "src"
    if name in _LOG_FILES:
        return "log"
    return "other"


def _state(folder: str, body: str, fm: dict) -> str:
    """archived (in archive/) | superseded (⛔ banner) | fresh|aging|stale (by `updated`)."""
    if folder == "archive":
        return "archived"
    head = body[:500].upper()
    if "⛔" in body[:500] and "SUPERSEDED" in head:
        return "superseded"
    days = _days_since(fm.get("updated") or fm.get("created"))
    if days is None:
        return "current"
    if days <= 30:
        return "fresh"
    if days <= 90:
        return "aging"
    return "stale"


def _summary(body: str) -> str:
    """First real paragraph (skip headings, banners, blank lines) — for the list + detail peek."""
    for raw in body.splitlines():
        s = raw.strip()
        if not s or s.startswith(("#", ">", "---", "|", "!", "```")):
            continue
        return re.sub(r"\s+", " ", s)[:240]
    return ""


def _links(body: str) -> list[str]:
    out, seen = [], set()
    for m in _LINK_RE.finditer(body):
        tgt = m.group(1).strip()
        if tgt and tgt.lower() not in seen:
            seen.add(tgt.lower())
            out.append(tgt)
    return out


def _scan(root: Path) -> list[dict]:
    docs = []
    for f in _iter_md(root):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            text = ""
        fm, body = _split_frontmatter(text)
        rel = f.relative_to(root)
        folder = rel.parts[0] if len(rel.parts) > 1 else "root"
        name = f.stem
        upd = fm.get("updated") or fm.get("created")
        docs.append({
            "id": name,
            "label": str(fm.get("title") or name),
            "type": _infer_type(name, fm, folder),
            "state": _state(folder, body, fm),
            "folder": folder,
            "updated": str(upd)[:10] if upd else "",
            "summary": _summary(body),
            "links": _links(body),
            "path": str(f),
        })
    return docs


def graph(vault_id: str = "orchard") -> dict:
    """Nodes (one per markdown file) + edges (resolved [[wikilinks]]). Never raises."""
    root = _vault_root(vault_id)
    try:
        docs = _scan(root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "nodes": [], "edges": []}
    by_id = {d["id"]: d for d in docs}
    edges, degree = [], {d["id"]: 0 for d in docs}
    for d in docs:
        for tgt in d["links"]:
            if tgt in by_id and tgt != d["id"]:
                edges.append({"source": d["id"], "target": tgt})
                degree[d["id"]] += 1
                degree[tgt] += 1
    nodes = []
    for d in docs:
        nodes.append({k: d[k] for k in ("id", "label", "type", "state", "folder", "updated", "summary")}
                     | {"degree": degree[d["id"]]})
    # stats for the legend / header
    by_state, by_type = {}, {}
    for d in docs:
        by_state[d["state"]] = by_state.get(d["state"], 0) + 1
        by_type[d["type"]] = by_type.get(d["type"], 0) + 1
    return {
        "ok": True,
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "vault_name": root.name,     # for the client's obsidian:// deep link
        "nodes": nodes,
        "edges": edges,
        "stats": {"files": len(nodes), "links": len(edges), "by_state": by_state, "by_type": by_type},
    }


def doc(node_id: str, vault_id: str = "orchard") -> dict:
    """Full markdown of one node — for the detail panel (rendered client-side). Read-only."""
    root = _vault_root(vault_id)
    for f in _iter_md(root):
        if f.stem == node_id:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            fm, _ = _split_frontmatter(text)
            rel = str(f.relative_to(root)).replace("\\", "/")
            if rel.lower().endswith(".md"):
                rel = rel[:-3]
            return {"ok": True, "id": node_id, "label": str(fm.get("title") or node_id),
                    "path": str(f), "rel": rel, "vault_name": root.name, "markdown": text}
    return {"ok": False, "error": "not found"}


def northstar(node_id: str = "concept_orchard-northstar") -> dict:
    """The Orchard north-star doc's frontmatter (mission + guardrails) for the hub strip. Read-only.
    Returns {ok, id, label, mission, guardrails[]}; ok:False if the doc isn't present."""
    for f in _iter_md(VAULT_ROOT):
        if f.stem == node_id:
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": str(exc)}
            fm, _ = _split_frontmatter(text)
            g = fm.get("guardrails")
            g = [str(x) for x in g] if isinstance(g, list) else []
            return {"ok": True, "id": node_id, "label": str(fm.get("title") or "North Star"),
                    "mission": str(fm.get("mission") or ""), "guardrails": g}
    return {"ok": False, "error": "north-star doc not found"}


def lint() -> dict:
    """Deterministic vault check — frontmatter parses, `[[links]]` resolve, files read cleanly. This is
    the rung-1.5 pre-check for a vault write, and doubles as a standalone audit. Never raises.
    Returns {ok, files, issue_count, issues:[{id, kind, detail}]}."""
    try:
        import yaml
        files = list(_iter_md(VAULT_ROOT))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "issues": []}
    ids = {f.stem for f in files}
    _META = {"CLAUDE", "README"}                          # instruction files, not vault knowledge docs
    issues = []
    for f in files:
        if f.stem in _META:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="strict")
        except Exception as exc:  # noqa: BLE001
            issues.append({"id": f.stem, "kind": "read-error", "detail": str(exc)[:120]})
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
        if text.startswith("---"):                       # frontmatter must parse to a mapping
            end = text.find("\n---", 3)
            if end == -1:
                issues.append({"id": f.stem, "kind": "frontmatter", "detail": "opening --- with no closing ---"})
            else:
                try:
                    fm = yaml.safe_load(text[3:end])
                    if fm is not None and not isinstance(fm, dict):
                        issues.append({"id": f.stem, "kind": "frontmatter", "detail": "frontmatter is not a mapping"})
                except Exception as exc:  # noqa: BLE001
                    issues.append({"id": f.stem, "kind": "frontmatter", "detail": f"YAML error: {str(exc)[:100]}"})
        _, body = _split_frontmatter(text)
        for tgt in _links(body):                         # every [[link]] must resolve to a real doc
            if tgt not in ids:
                issues.append({"id": f.stem, "kind": "broken-link", "detail": f"[[{tgt}]] unresolved"})
    return {"ok": True, "files": len(files), "issue_count": len(issues), "issues": issues}


# ===================== retrieval (for the ask-Orchard agent) =====================
# A lightweight, stdlib-only keyword search over the vault — the grounding step for the
# ask-Orchard agent. It returns the most relevant docs as EVIDENCE, split into tiers:
#   live       (fresh/aging/stale/current) — the ONLY basis for a current answer
#   superseded (⛔ banner) — returned separately, each carrying its successor link, so the
#              agent can redirect ("that was superseded by X") instead of answering stale
#   archived   — excluded entirely (retired/out-of-scope; never answers)
# No embeddings dependency (keeps it local-sovereign); embeddings are the future upgrade.
_STOP = set("the a an and or of to in on for is are was were be been being with as at by from "
            "this that these those it its into over under our your you we not no if then than "
            "how what when where why who which do does can could should would will may".split())
_STATE_W = {"fresh": 1.0, "current": 0.95, "aging": 0.85, "stale": 0.7, "superseded": 0.5}
# The vault is a Q&A source about DECISIONS — journals (log/progress/content_queue) are keyword-dense
# noise that would otherwise dominate on raw term counts, so they're heavily de-weighted (not excluded).
_TYPE_W = {"decision": 1.0, "concept": 0.95, "synthesis": 0.95, "entity": 0.85, "reference": 0.85,
           "index": 0.7, "other": 0.6, "src": 0.5, "log": 0.22}


def _tokens(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in _STOP]


def _successor(body: str):
    """For a superseded doc: the replacement named in the ⛔ banner (first [[link]] near the top)."""
    m = _LINK_RE.search(body[:600])
    return m.group(1).strip() if m else None


def _scan_full(root: Path) -> list[dict]:
    """Like _scan() but keeps the body + links (needed for scoring + evidence)."""
    docs = []
    for f in _iter_md(root):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            text = ""
        fm, body = _split_frontmatter(text)
        rel = f.relative_to(root)
        folder = rel.parts[0] if len(rel.parts) > 1 else "root"
        name = f.stem
        upd = fm.get("updated") or fm.get("created")
        state = _state(folder, body, fm)
        docs.append({
            "id": name, "label": str(fm.get("title") or name),
            "type": _infer_type(name, fm, folder), "state": state,
            "updated": str(upd)[:10] if upd else "",
            "successor": _successor(body) if state == "superseded" else None,
            "body": body, "links": _links(body),
        })
    return docs


def search(query: str, k: int = 5, per_doc_chars: int = 2200, vault_id: str = "orchard") -> dict:
    """Rank vault docs for `query`. Returns {ok, query, live:[...], superseded:[...]}.

    Each evidence item: {id, label, type, state, updated, successor, score, markdown}.
    `markdown` is the body trimmed to per_doc_chars (bounded context for a small model).
    Archived docs are excluded. Never raises.
    """
    root = _vault_root(vault_id)
    try:
        docs = _scan_full(root)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "query": query, "live": [], "superseded": []}
    by_id = {d["id"]: d for d in docs}
    degree = {d["id"]: 0 for d in docs}
    for d in docs:
        for tgt in d["links"]:
            if tgt in by_id and tgt != d["id"]:
                degree[d["id"]] += 1
                degree[tgt] += 1
    qt = _tokens(query)
    candidates = [d for d in docs if d["state"] != "archived" and d["type"] != "log"]  # retired/journals never answer
    # IDF over the searchable corpus: a term that appears in almost every doc (e.g. "app"/"use" in a
    # orchard-api vault) carries little signal, while a rare term (e.g. "age") should decide the ranking.
    # Without this, generic body hits on common words bury the very doc whose TITLE is the answer — which
    # is exactly why "age limit" failed to retrieve the "Minimum Age" decision until it was named directly.
    N = len(candidates) or 1
    df: dict[str, int] = {}
    for d in candidates:
        for t in set(_tokens(d["label"] + " " + d["id"].replace("_", " "))) | set(_tokens(d["body"])):
            df[t] = df.get(t, 0) + 1
    def _idf(t: str) -> float:
        return math.log(1 + N / (1 + df.get(t, 0)))
    scored = []
    for d in candidates:
        title_terms = set(_tokens(d["label"] + " " + d["id"].replace("_", " ")))
        body_l = d["body"].lower()
        s = 0.0
        for t in qt:
            w = _idf(t)                          # rarer query term → weighs more
            if t in title_terms:
                s += 5.0 * w                     # a title/id hit is worth much more than a body hit
            c = body_l.count(t)
            if c:
                s += 1.6 * math.log1p(c) * w     # saturating TF-IDF — a huge journal can't win on raw counts
        if s <= 0:
            continue
        s *= _STATE_W.get(d["state"], 0.6)       # prefer fresher decisions
        s *= _TYPE_W.get(d["type"], 0.6)         # favour decisions/concepts over journals
        s += min(degree[d["id"]], 20) * 0.15     # load-bearing (well-linked) docs rank up
        scored.append((s, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    live, sup = [], []
    for s, d in scored:
        item = {"id": d["id"], "label": d["label"], "type": d["type"], "state": d["state"],
                "updated": d["updated"], "successor": d["successor"], "score": round(s, 2),
                "markdown": d["body"].strip()[:per_doc_chars]}
        if d["state"] == "superseded":
            if len(sup) < 3:
                sup.append(item)
        elif len(live) < k:
            live.append(item)
        if len(live) >= k and len(sup) >= 3:
            break
    return {"ok": True, "query": query,
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "live": live, "superseded": sup}


if __name__ == "__main__":  # smoke: py -3.12 vault.py ["a query"]
    import json, sys
    if len(sys.argv) > 1:
        r = search(" ".join(sys.argv[1:]))
        print(f"query: {r['query']!r}  →  {len(r['live'])} live, {len(r['superseded'])} superseded")
        for it in r["live"]:
            print(f"  [{it['score']:>6}] {it['state']:<10} {it['id']}  — {it['label']}")
        for it in r["superseded"]:
            print(f"  SUPERSEDED (→ {it['successor']}) {it['id']}")
    else:
        g = graph()
        print(json.dumps(g["stats"], indent=2))
        print("sample nodes:", json.dumps(g["nodes"][:3], indent=2, ensure_ascii=False)[:800])
