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
        cands += list(Path("/mnt/c/Users").glob("*/.claude/projects"))
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


# ═══ model price weights (Sonnet-equivalent fresh tokens) ═══════════════════════════════════════
# Cost is not raw tokens: a Haiku token spends a small fraction of what an Opus token does. Anthropic
# does not publish the exact weighting, so the API price ratios are the proxy. The unit is
# "Sonnet-equivalent fresh tokens" (Sonnet 5.5 = 1.0). Used for relative cost in the Ledger statistics
# and the team view; never to estimate a usage limit (Atlas does not; the Claude app's meter is the
# reference). `collect()` stays raw, it is a display meter.
#
# Prices per million tokens, input/output, 2026-10:
#   Opus 5.5    $4 / $20          -> 2.0
#   Sonnet 5.5  $2 / $10          -> 1.0
#   Haiku 5.5   $0.10 / $0.50 for prompts up to 100k tokens  -> 0.05
#               $0.50 / $2.50 above 100k                      -> 0.25
#   Haiku 4.5   $1 / $5           -> 0.5
# `ATLAS_MODEL_WEIGHTS="haiku-5=0.05,opus=2"` overrides: each pair replaces the flat weight for
# that pattern (no Haiku 5.5 tier then), unknown patterns are added and checked before the defaults.
HAIKU5_TIER_PROMPT = 100_000
_DEFAULT_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("haiku-5", 0.05), ("haiku", 0.5), ("opus", 2.0), ("sonnet", 1.0))


def _env_weights() -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for pair in (os.environ.get("ATLAS_MODEL_WEIGHTS") or "").split(","):
        pat, sep, val = pair.partition("=")
        pat = pat.strip().lower()
        if not sep or not pat:
            continue
        try:
            w = float(val.strip())
        except ValueError:
            continue
        if w != w or w < 0:                 # NaN / negative: malformed
            continue
        out.append((pat, w))
    return out


def model_weight(model: str | None, prompt_tokens: int = 0) -> float:
    """Price weight of one fresh token of `model`, in Sonnet-equivalent tokens (Sonnet = 1.0).

    Unknown / missing / `<synthetic>` models weigh 1.0. `prompt_tokens` only matters for Haiku 5.5,
    whose price steps up for prompts over 100k.
    """
    m = (model or "").lower()
    for pat, w in _env_weights():
        if pat in m:
            return w
    for pat, w in _DEFAULT_WEIGHTS:
        if pat in m:
            if pat == "haiku-5" and prompt_tokens > HAIKU5_TIER_PROMPT:
                return 0.25
            return w
    return 1.0
