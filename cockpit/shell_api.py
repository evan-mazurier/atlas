"""Shell API (W1 Lane 2): areas, attention, team (docs/v2/contracts.md §5.1).

    GET /api/v2/areas       the areas from areas.yaml
    GET /api/v2/attention   sessions working/waiting, pending reviews
    GET /api/v2/team        teamlive.snapshot() plus the area of each session
    GET /api/v2/shell/roster  helpers (agent files), types of work and playbooks (workflows.yaml)
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (HERE, HERE / "core"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# --- areas -----------------------------------------------------------------------------------------
def areas_file() -> Path:
    env = os.environ.get("ATLAS_AREAS_FILE")
    return Path(env) if env else ROOT / "areas.yaml"


def _areas_mod():
    try:
        return importlib.import_module("ledger2.areas")
    except Exception:  # noqa: BLE001
        return None


def load_areas() -> list[dict]:
    """Areas in file order, from the one reader (`ledger2.areas.load_areas`); [] when it is unavailable."""
    mod = _areas_mod()
    if mod is None:
        return []
    try:
        return list(mod.load_areas(str(areas_file())) or [])
    except Exception:  # noqa: BLE001
        return []


def area_of(areas: list[dict], cwd: str | None, area_arg: str | None = None, playbook: str | None = None) -> str | None:
    mod = _areas_mod()
    if mod is None:
        return None
    try:
        return mod.area_for(areas, cwd, area_arg, playbook)
    except Exception:  # noqa: BLE001
        return None


def _session_area(areas: list[dict], s: dict) -> str | None:
    return area_of(areas, s.get("cwd"), s.get("area_arg"), s.get("playbook"))


def areas_payload() -> dict:
    return {"ok": True, "areas": [
        {"id": a["id"], "name": a.get("name") or a["id"], "color": a.get("color"), "group": a.get("group")}
        for a in load_areas()]}


# --- sessions ----------------------------------------------------------------------------------------
def _snapshot() -> dict:
    import teamlive
    return teamlive.snapshot()


_TRAY_LOGGED = False


def _pending_reviews() -> int:
    try:
        mod = importlib.import_module("plan.api")
        return int(mod.review_tray_count())
    except Exception as exc:  # noqa: BLE001
        global _TRAY_LOGGED
        if not _TRAY_LOGGED:
            _TRAY_LOGGED = True
            print(f"shell_api: plan.api.review_tray_count failed: {exc!r}", file=sys.stderr)
        return 0


def _status(s: dict) -> str:
    if s.get("status") == "working":
        return "working"
    return "waiting" if s.get("waiting") else "idle"


def attention_payload() -> dict:
    snap = _snapshot()
    areas = load_areas()
    sessions = []
    team_error = None if snap.get("ok") else (snap.get("error") or "team log unreadable")
    for s in (snap.get("sessions") or []) if snap.get("ok") else []:
        sessions.append({"sid": s.get("sid"), "title": s.get("title") or f"{s.get('type')} · {s.get('project')}",
                         "area": _session_area(areas, s), "status": _status(s)})
    return {"ok": True,
            "working": sum(1 for s in sessions if s["status"] == "working"),
            "waiting": sum(1 for s in sessions if s["status"] == "waiting"),
            "sessions": sessions,
            "pending_reviews": _pending_reviews(),
            "team_error": team_error}


def _plan_titles(sessions: list[dict]) -> dict:
    """plan_ref -> the Plan item's title, for the sessions that declared one (read-only; {} on any trouble)."""
    refs = {s.get("plan_ref") for s in sessions if s.get("plan_ref")}
    if not refs:
        return {}
    try:
        mod = importlib.import_module("plan.refs")
        return {r: v.get("title") for r, v in mod.resolve_many(refs).items()}
    except Exception as exc:  # noqa: BLE001
        print(f"shell_api: plan.refs failed: {exc!r}", file=sys.stderr)
        return {}


def team_payload() -> tuple[int, dict]:
    snap = _snapshot()
    if not snap.get("ok"):
        return 500, {"ok": False, "error": snap.get("error") or "team log unreadable"}
    areas = load_areas()
    out = dict(snap)
    titles = _plan_titles(snap.get("sessions") or [])
    out["sessions"] = [dict(s, area=_session_area(areas, s), plan_title=titles.get(s.get("plan_ref")))
                       for s in snap.get("sessions") or []]
    return 200, out


# --- roster ------------------------------------------------------------------------------------------
def agents_dir() -> Path:
    env = os.environ.get("ATLAS_AGENTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "agents"


def _frontmatter(text: str) -> tuple[dict, str]:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict = {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        k, sep, v = lines[i].partition(":")
        if sep and k.strip() and not k.startswith(" "):
            meta[k.strip()] = v.strip().strip("\"'")
    return {}, text


def _plain(t: str) -> str:
    """UI copy carries no em-dashes (the agent files do)."""
    import re
    t = t.replace(" — ", ", ").replace("—", ", ")
    return re.sub(r"\bOperator(?:'s)?\b", lambda m: "the operator's" if m.group(0).endswith("s") else "the operator", t)


def _sentences(text: str) -> list[str]:
    import re
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]


def _use_for(desc: str) -> str:
    import re
    desc = re.sub(r"^[^.]{0,40}\((?:Opus|Sonnet|Haiku)\)\.\s*", "", desc, flags=re.I)
    keep = [x for x in _sentences(desc) if not re.match(r"(never|not for)\b", x, re.I)]
    out = " ".join(keep[:2])
    return out if len(out) <= 220 else out[:217].rstrip() + "..."


def _never(body: str) -> str:
    import re
    for line in body.splitlines():
        if re.search(r"\bnever\b", line, re.I):
            t = re.sub(r"^[\s\-*0-9.]+", "", line).strip()
            return t if len(t) <= 160 else t[:157].rstrip() + "..."
    return ""


def team_doc() -> Path:
    env = os.environ.get("ATLAS_TEAM_DOC")
    return Path(env) if env else Path.home() / ".claude" / "CLAUDE.md"


def _cell(c: str) -> str:
    import re
    return re.sub(r"\s+", " ", c.replace("`", "").replace("**", "")).strip()


def load_team_table() -> dict:
    """helper id -> {model, use_for, never} from the `| Helper | Model | Use it for | Never |` table; {} if absent."""
    try:
        lines = team_doc().read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return {}
    out: dict = {}
    on = False
    for ln in lines:
        if not ln.lstrip().startswith("|"):
            if on:
                break
            continue
        import re
        cells = [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", ln.strip()[1:-1] if ln.strip().endswith("|") else ln.strip()[1:])]
        if not on:
            on = [_cell(c).lower() for c in cells[:4]] == ["helper", "model", "use it for", "never"]
            continue
        if len(cells) < 4 or set(cells[0]) <= set("-: "):
            continue
        out[_cell(cells[0])] = {"model": _cell(cells[1]).lower(), "use_for": _plain(_cell(cells[2])), "never": _plain(_cell(cells[3]))}
    return out


def load_helpers() -> list[dict]:
    table = load_team_table()
    d = agents_dir()
    out = []
    try:
        files = sorted(d.glob("*.md"))
    except (OSError, ValueError):
        return []
    for f in files:
        try:
            meta, body = _frontmatter(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hid = meta.get("name") or f.stem
        row = table.get(hid)
        out.append({"id": hid, "model": (meta.get("model") or (row or {}).get("model") or "").lower(),
                    "use_for": row["use_for"] if row else _plain(_use_for(meta.get("description") or "")),
                    "never": row["never"] if row else _plain(_never(body))})
    return out


def roster_payload() -> dict:
    q = importlib.import_module("ledger2.query")
    wf = q.load_workflows()
    types = []
    for tid, t in (wf.get("types") or {}).items():
        if tid == "undeclared" or not isinstance(t, dict):
            continue
        types.append({"id": tid, "label": t.get("label") or tid, "crew": [str(t["crew"])] if isinstance(t.get("crew"), str) else [str(x) for x in (t.get("crew") or [])],
                      "budget": t.get("budget"), "rung": None if t.get("rung") is None else str(t.get("rung")),
                      "move": _plain(t.get("move") or "")})
    areas = load_areas()
    playbooks = []
    for pb in wf.get("playbooks") or []:
        pb = str(pb)
        owner = next((a for a in areas if pb in [str(x).lstrip("/").lower() for x in (a.get("playbooks") or [])]), None)
        spec = (wf.get("playbook_crews") or {}).get(pb)
        spec = spec if isinstance(spec, dict) else {}
        crew = spec.get("crew")
        crew = [crew] if isinstance(crew, str) else [str(x) for x in (crew or [])]
        budget = spec["budget"] if "budget" in spec else ((wf.get("types") or {}).get("playbook") or {}).get("budget", wf.get("default_budget"))
        playbooks.append({"command": "/" + pb.lstrip("/"), "area": (owner or {}).get("name") or (owner or {}).get("id"),
                          "crew": crew, "budget": budget, "per_task": bool(spec.get("per_task"))})
    return {"ok": True, "helpers": load_helpers(), "types": types, "playbooks": playbooks}


# --- routing -----------------------------------------------------------------------------------------
def get(path: str, query: dict):
    if path == "/api/v2/areas":
        return 200, areas_payload()
    if path == "/api/v2/attention":
        return 200, attention_payload()
    if path == "/api/v2/team":
        return team_payload()
    if path == "/api/v2/shell/roster":
        try:
            return 200, roster_payload()
        except Exception as exc:  # noqa: BLE001 - the roster is decoration: never a 500
            print(f"shell_api: roster failed: {exc!r}", file=sys.stderr)
            return 200, {"ok": True, "helpers": [], "types": [], "playbooks": []}
    return None


def post(path: str, body: dict):
    return None
