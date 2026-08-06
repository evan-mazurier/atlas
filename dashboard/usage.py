"""
Atlas usage meter — real-time Claude Code token usage from the LOCAL session logs.

Every Claude Code session (your interactive ones AND Atlas-dispatched ones) writes a
JSONL transcript under ~/.claude/projects/**, with exact per-message token counts.
We aggregate those. This is token CONSUMPTION (the honest local view), not the official
subscription rate-limit %, which Claude Code does not expose programmatically.

claude.exe runs on Windows, so the logs live in the Windows home; from WSL that's
/mnt/c/Users/<you>/.claude/projects.
"""
from __future__ import annotations

import os
import json
import datetime as dt
from pathlib import Path
from collections import defaultdict


def projects_dir() -> Path | None:
    if os.environ.get("ATLAS_CLAUDE_DIR"):
        p = Path(os.environ["ATLAS_CLAUDE_DIR"])
        return p if p.exists() else None
    cands: list[Path] = []
    if os.name == "nt":
        cands.append(Path(os.path.expanduser("~")) / ".claude" / "projects")
    else:
        cands += list(Path("/mnt/c/Users").glob("*/.claude/projects"))   # WSL reading the Windows host
        cands.append(Path(os.path.expanduser("~")) / ".claude" / "projects")
    return next((c for c in cands if c and c.exists()), None)


def human(n: float) -> str:
    n = float(n or 0)
    for u in ("", "K", "M", "B"):
        if abs(n) < 1000:
            return f"{n:.0f}" if u == "" else f"{n:.2f}{u}"
        n /= 1000.0
    return f"{n:.2f}T"


def collect(days: int = 7) -> dict:
    """Aggregate token usage over the last `days` days (by UTC date on the record)."""
    root = projects_dir()
    if not root:
        return {"ok": False, "error": "Couldn't find ~/.claude/projects (Claude Code logs)."}

    cutoff = dt.date.today() - dt.timedelta(days=days - 1)
    mtime_cut = dt.datetime.now() - dt.timedelta(days=days + 1)
    by_day: dict[str, dict] = defaultdict(lambda: {"in": 0, "out": 0, "cache_r": 0, "cache_c": 0})
    by_model: dict[str, int] = defaultdict(int)

    for f in root.rglob("*.jsonl"):
        try:
            if dt.datetime.fromtimestamp(f.stat().st_mtime) < mtime_cut:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        for line in text.splitlines():
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            msg = rec.get("message") or {}
            u = msg.get("usage") or {}
            ts = rec.get("timestamp")
            if not u or not ts:
                continue
            day = ts[:10]
            try:
                if dt.date.fromisoformat(day) < cutoff:
                    continue
            except Exception:  # noqa: BLE001
                continue
            i = u.get("input_tokens", 0) or 0
            o = u.get("output_tokens", 0) or 0
            cr = u.get("cache_read_input_tokens", 0) or 0
            cc = u.get("cache_creation_input_tokens", 0) or 0
            d = by_day[day]
            d["in"] += i; d["out"] += o; d["cache_r"] += cr; d["cache_c"] += cc
            by_model[(msg.get("model") or "?").replace("claude-", "")] += i + o   # fresh in+out only (cache excluded, to match the subscription)

    days_sorted = sorted(by_day.keys())
    today = dt.date.today().isoformat()
    t = by_day.get(today, {"in": 0, "out": 0, "cache_r": 0, "cache_c": 0})
    week_in = sum(v["in"] for v in by_day.values())
    week_out = sum(v["out"] for v in by_day.values())
    week_cc = sum(v["cache_c"] for v in by_day.values())
    series = [{"day": d, "total": by_day[d]["in"] + by_day[d]["out"]}
              for d in days_sorted]
    top_models = sorted(by_model.items(), key=lambda kv: kv[1], reverse=True)[:3]

    return {
        "ok": True,
        "today_total": t["in"] + t["out"],            # fresh input+output only — matches the subscription usage panel
        "today_in": t["in"], "today_out": t["out"], "today_cached": t["cache_c"],
        "week_total": week_in + week_out,             # (cache_creation was ~99% of the old inflated number)
        "week_in": week_in, "week_out": week_out, "week_cached": week_cc,
        "series": series,
        "top_models": top_models,
        "days": days,
    }
