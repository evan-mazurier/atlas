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
        cands.append(Path("/mnt~/.claude/projects"))
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
# The subscription limit is rationed by COST, not by raw tokens: a Haiku token spends a small
# fraction of what an Opus token does, so counting every fresh token the same made Haiku work read
# like Opus and held back jobs that fit. Anthropic does not publish the exact weighting, so the API
# price ratios are the best available proxy -- and the cap itself is learned from refusals
# (watcher.observed_cap), so a proportional error in the weights washes out. The unit is
# "Sonnet-equivalent fresh tokens" (Sonnet 5.5 = 1.0). Only the DECISION figures use it
# (`window_spend`, `worker._add_tokens`); `collect()` stays raw, it is a display meter.
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


# ═══ the five-hour window (docs/monitor.md §3.8, contract W5; rework-findings #11) ═══════════════
# `collect()` is day-granular, which is right for the seven-day meter and useless for a limit that
# runs on FIVE HOURS. The watcher needs two numbers: how much has been spent inside the current
# window, and when that window frees.
#
# The window is NOT a trailing sum — that was the model until 2026-09-16, and it held two jobs in
# one afternoon that the real window would have admitted (finding #11). Anthropic's window STARTS
# at the first message and EXPIRES five hours later; the next one starts at the next message after
# that expiry. So the transcripts are walked as a chain of windows: the first message after a gap
# of five hours or more opens a window that nothing before it can still belong to (any earlier
# window has expired inside the gap), and from there each window's end is `start + 5h`, the next
# start is the first message at or after it. The current window is the last one in the chain; a
# message-free stretch past its end means the budget is FULL again and the spend is zero.
#
# It reads the SAME local transcripts as `collect()`, and that matters: The operator's own interactive
# sessions consume the same ceiling as Atlas's dispatches, so a window computed from the ledger
# alone (which only knows about dispatched work) would read low exactly when it matters most.
#
# The override door: `jobs/_window.json` with `{"window_start": "<local ISO>"}` pins the anchor —
# for the day the model is wrong and the app on the operator's phone is right. It is read on every call
# (no worker restart), and it is ignored once it is more than a window old, so a stale pin cannot
# quietly misprice the next day.
#
# Cached for 30 seconds. This is called at job admission, a handful of times an hour, and an rglob
# over the transcripts is not free.
_WINDOW_CACHE: dict = {"at": None, "hours": None, "value": None}
LOOKBACK_HOURS = 36            # long enough to hold a night's gap, which is what re-anchors the chain
WINDOW_PIN = Path(__file__).resolve().parents[2] / "jobs" / "_window.json"


def _window_pin(now: dt.datetime, hours: int) -> dt.datetime | None:
    """The hand-set window start, if one is pinned and still inside a window's reach."""
    try:
        raw = json.loads(WINDOW_PIN.read_text(encoding="utf-8")).get("window_start")
        t = dt.datetime.fromisoformat(str(raw))
        if t.tzinfo is not None:
            t = t.astimezone().replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None
    if t > now + dt.timedelta(minutes=5) or now - t >= dt.timedelta(hours=hours):
        return None
    return t


def _usage_records(root: Path, since: dt.datetime, now: dt.datetime) -> list[tuple[dt.datetime, int]]:
    """Every `(local time, fresh tokens)` usage record stamped in `[since, now]`, unsorted.

    Fresh tokens are price-weighted (`model_weight`): Sonnet-equivalent input+output."""
    mtime_cut = since - dt.timedelta(hours=1)
    out: list[tuple[dt.datetime, int]] = []
    for f in root.rglob("*.jsonl"):
        try:
            if dt.datetime.fromtimestamp(f.stat().st_mtime) < mtime_cut:
                continue                      # the file itself is older than the lookback
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
            try:
                # transcripts stamp UTC with a trailing Z; compare in local time, as the window is
                # experienced locally
                t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                t = t.astimezone().replace(tzinfo=None)
            except Exception:  # noqa: BLE001
                continue
            if t < since or t > now + dt.timedelta(minutes=5):
                continue
            fresh = int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0)
            prompt = (int(u.get("input_tokens", 0) or 0) + int(u.get("cache_read_input_tokens", 0) or 0)
                      + int(u.get("cache_creation_input_tokens", 0) or 0))
            out.append((t, round(model_weight(msg.get("model"), prompt) * fresh)))
    return out


def window_chain(times: list[dt.datetime], hours: int, anchor: dt.datetime | None = None) -> list[dt.datetime]:
    """The window starts implied by a sorted list of message times.

    Pure, so the model can be tested without transcripts. The chain begins at `anchor` when one is
    given, else at the first message after the last gap of `hours` or more (else at the first
    message at all — the unanchored case, which the caller reports). From the start, each window
    ends `hours` later and the next begins at the first message at or after that end.
    """
    span = dt.timedelta(hours=hours)
    if anchor is not None:
        times = [t for t in times if t >= anchor]
        starts = [anchor]
    else:
        if not times:
            return []
        first = times[0]
        for prev, cur in zip(times, times[1:]):
            if cur - prev >= span:
                first = cur
        starts = [first]
        times = [t for t in times if t >= first]
    end = starts[-1] + span
    for t in times:
        if t >= end:
            starts.append(t)
            end = t + span
    return starts


def window_spend(hours: int = 5, now: dt.datetime | None = None) -> dict:
    """`{fresh, oldest, entries, ok, window_start, window_end, anchored, pinned}` for the CURRENT
    `hours`-long window.

    `fresh` is input+output only, PRICE-WEIGHTED into Sonnet-equivalent tokens (`model_weight`) --
    the subscription-relevant figure, the same one `worker._add_tokens` accumulates. Cache reads are
    excluded for the same reason they are excluded everywhere else: they dwarf the real number and
    are not what is being rationed.

    `oldest` is kept for the callers that already read it (`watcher.estimate_reset` adds five
    hours to it): it is now the current window's START, so `oldest + 5h` IS the reset, not a guess
    at it. `window_end` says the same thing without the arithmetic. Both are None when no window
    is open — the next message will open one, and the spend is 0.
    """
    now = now or dt.datetime.now()
    c = _WINDOW_CACHE
    if c["value"] is not None and c["hours"] == hours and c["at"] and \
            (now - c["at"]).total_seconds() < 30:
        return c["value"]

    root = projects_dir()
    if not root:
        out = {"ok": False, "fresh": 0, "oldest": None, "entries": 0, "window_start": None,
               "window_end": None, "anchored": False, "pinned": False,
               "error": "no local Claude Code transcripts to read"}
        _WINDOW_CACHE.update({"at": now, "hours": hours, "value": out})
        return out

    pin = _window_pin(now, hours)
    since = pin if pin else now - dt.timedelta(hours=max(LOOKBACK_HOURS, hours))
    recs = sorted(_usage_records(root, since, now))
    times = [t for t, _ in recs]
    starts = window_chain(times, hours, anchor=pin)
    span = dt.timedelta(hours=hours)
    # anchored = the chain began at something that cannot be wrong: a pin, or a message with a full
    # window of silence before it (a gap between messages, or the lookback's own empty start)
    anchored = bool(pin) or not times or (times[0] - since >= span) or any(
        b - a >= span for a, b in zip(times, times[1:]))

    start = starts[-1] if starts else None
    if start is None or now >= start + span:
        # nothing open: the last window has expired (or nothing was ever sent) — the budget is full
        out = {"ok": True, "fresh": 0, "entries": 0, "oldest": None, "window_start": None,
               "window_end": None, "anchored": anchored, "pinned": bool(pin)}
    else:
        inside = [(t, n) for t, n in recs if t >= start]
        out = {"ok": True,
               "fresh": sum(n for _, n in inside),
               "entries": len(inside),
               "oldest": start.isoformat(timespec="seconds"),
               "window_start": start.isoformat(timespec="seconds"),
               "window_end": (start + span).isoformat(timespec="seconds"),
               "anchored": anchored, "pinned": bool(pin)}
    _WINDOW_CACHE.update({"at": now, "hours": hours, "value": out})
    return out


# ═══ the current window, split by model tier (Atlas v2 attention bar, contracts.md §5.1) ═══════════
def _tier_of(model: str | None) -> str:
    m = (model or "").lower()
    for tier in ("opus", "haiku", "sonnet"):
        if tier in m:
            return tier
    return "sonnet"          # unknown / synthetic models weigh 1.0 in model_weight: count them as Sonnet


def window_by_model(hours: int = 5, now: dt.datetime | None = None) -> dict:
    """The CURRENT window's price-weighted fresh tokens split per model tier, de-duplicated.

    Claude Code writes one transcript line per content block and repeats the message's `usage` on
    each, so `window_spend` over-counts (~3.7x). Here a message is counted ONCE: lines are keyed by
    `(message.id, requestId)` and the largest figure of each field wins. Main-transcript lines flagged
    `isSidechain` are skipped (the helper's own transcript under `subagents/` holds them). The window
    is the one `window_spend` finds. Returns `{ok, window_start, window_end, by_model: {opus, sonnet,
    haiku}, total}`; zeros and None bounds when no window is open.
    """
    now = now or dt.datetime.now()
    zero = {"opus": 0, "sonnet": 0, "haiku": 0}
    base = window_spend(hours, now)
    start_s = base.get("window_start")
    if not base.get("ok") or not start_s:
        return {"ok": bool(base.get("ok")), "window_start": None, "window_end": None,
                "by_model": dict(zero), "total": 0}
    start = dt.datetime.fromisoformat(start_s)
    root = projects_dir()
    msgs: dict = {}
    seq = 0
    for f in (root.rglob("*.jsonl") if root else []):
        try:
            if dt.datetime.fromtimestamp(f.stat().st_mtime) < start - dt.timedelta(hours=1):
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        in_sub = f.parent.name == "subagents"
        for line in text.splitlines():
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("isSidechain") and not in_sub:
                continue
            msg = rec.get("message") or {}
            u = msg.get("usage") or {}
            ts = rec.get("timestamp")
            if not isinstance(u, dict) or not u or not ts:
                continue
            try:
                t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
            except Exception:  # noqa: BLE001
                continue
            if t < start or t > now + dt.timedelta(minutes=5):
                continue
            mid, rid = msg.get("id"), rec.get("requestId")
            if mid and rid:
                key = (mid, rid)
            else:
                seq += 1
                key = ("line", seq)
            cur = msgs.setdefault(key, {"model": msg.get("model"), "in": 0, "out": 0, "cr": 0, "cc": 0})
            cur["in"] = max(cur["in"], int(u.get("input_tokens", 0) or 0))
            cur["out"] = max(cur["out"], int(u.get("output_tokens", 0) or 0))
            cur["cr"] = max(cur["cr"], int(u.get("cache_read_input_tokens", 0) or 0))
            cur["cc"] = max(cur["cc"], int(u.get("cache_creation_input_tokens", 0) or 0))
            if msg.get("model"):
                cur["model"] = msg.get("model")
    by = dict(zero)
    for m in msgs.values():
        prompt = m["in"] + m["cr"] + m["cc"]
        by[_tier_of(m["model"])] += round(model_weight(m["model"], prompt) * (m["in"] + m["out"]))
    return {"ok": True, "window_start": start_s, "window_end": base.get("window_end"),
            "by_model": by, "total": sum(by.values())}
