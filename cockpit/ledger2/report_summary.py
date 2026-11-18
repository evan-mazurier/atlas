"""Compact summary of a saved weekly report (contracts.md §5.2, `GET /api/v2/ledger/report`).

Pure and forgiving: unknown structure gives empty lists, a missing or odd json gives no kpis; never raises."""
from __future__ import annotations

import re

_ITEM = re.compile(r"^\s{0,3}(\d{1,2})[.)]\s+(.*)$")
_BOLD = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$")


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _plain(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return s.strip()


def _sections(md: str) -> list[tuple[str, list[str]]]:
    """Level-2 sections: (title, body lines). `###` and deeper stay inside their section."""
    out: list[tuple[str, list[str]]] = []
    for line in md.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m and not line.startswith("###"):
            out.append((m.group(1), []))
        elif out:
            out[-1][1].append(line)
    return out


def _numbered(lines: list[str]) -> list[dict]:
    """Numbered items `1. **Headline.** body`; continuation lines are joined onto the body."""
    items: list[list[str]] = []
    joining = False
    for line in lines:
        m = _ITEM.match(line)
        if m:
            items.append([m.group(2).strip()])
            joining = True
        elif not line.strip():
            joining = False
        elif joining and items and not line.lstrip().startswith(("#", "|")):
            items[-1].append(line.strip())
        else:
            joining = False
    out = []
    for parts in items:
        text = " ".join(p for p in parts if p).strip()
        b = _BOLD.match(text)
        if b:
            head, body = b.group(1).strip(), b.group(2).strip()
        else:
            head, body = text, ""
        head = _plain(head)
        if head and re.search(r"\w", head):
            out.append({"headline": head, "body": _plain(body)})
    return out


def _table_rows(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        t = line.strip()
        if not t.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in t.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c) and any(cells):
            continue
        rows.append(cells)
    return rows


def _kpis(data: dict | None) -> list[dict]:
    if not isinstance(data, dict):
        return []
    tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
    review = data.get("review") if isinstance(data.get("review"), dict) else {}
    hours = data.get("time_hours") if isinstance(data.get("time_hours"), dict) else {}
    usage = data.get("usage_weighted") if isinstance(data.get("usage_weighted"), dict) else {}
    out: list[dict] = []
    closed = _num(tasks.get("closed"))
    if closed is not None:
        out.append({"key": "tasks_closed", "label": "Tasks closed", "value": closed})
    fp = _num(tasks.get("first_pass_ready_pct"))
    if fp is not None:
        out.append({"key": "first_pass_ready_pct", "label": "First-pass READY", "value": fp, "unit": "%"})
    per = None
    conc = review.get("rework_concentration")
    if isinstance(conc, dict):
        per = _num(conc.get("per_task"))
    if per is None:
        rounds = _num(review.get("needs_changes_rounds"))
        if rounds is not None and closed:
            per = round(rounds / closed, 2)
    if per is not None:
        out.append({"key": "rework_per_task", "label": "Rework rounds per task", "value": per})
    th = _num(hours.get("total"))
    if th is not None:
        out.append({"key": "hours", "label": "Hours", "value": th, "unit": "h"})
    w = _num(usage.get("total_task_cost"))
    if w is not None:
        out.append({"key": "weighted_usage", "label": "Weighted usage", "value": w})
    return out


def build_summary(markdown: str, data: dict | None = None) -> dict:
    """`{kpis, lessons, changes, proposed}`. Every part is empty when its source is missing or odd."""
    summary = {"kpis": [], "lessons": [], "changes": [], "proposed": []}
    try:
        summary["kpis"] = _kpis(data)
    except Exception:  # noqa: BLE001
        summary["kpis"] = []
    try:
        secs = _sections(markdown if isinstance(markdown, str) else "")
        for title, lines in secs:
            if title.lower().startswith("lessons") and not summary["lessons"]:
                summary["lessons"] = _numbered(lines)
            elif re.search(r"\bchanges\b", title, re.I) and not title.lower().startswith("lessons"):
                idx = next((i for i, l in enumerate(lines) if re.match(r"^###\s+earlier changes", l, re.I)), None)
                rest = lines
                if idx is not None:
                    tbl = lines[idx + 1:]
                    rows = _table_rows(tbl)
                    if len(rows) >= 2:
                        summary["changes"] = [{"name": _plain(r[0]), "status": _plain(r[-1])}
                                              for r in rows[1:] if len(r) >= 2 and _plain(r[0])]
                    end = next((i for i, l in enumerate(tbl) if l.strip().startswith("|")), None)
                    # items after the table (or after the heading when there is no table)
                    if end is not None:
                        j = end
                        while j < len(tbl) and (tbl[j].strip().startswith("|") or not tbl[j].strip()):
                            j += 1
                        rest = tbl[j:]
                    else:
                        rest = tbl
                else:
                    rest = [l for l in lines]
                summary["proposed"] = [i["headline"] for i in _numbered(rest)]
    except Exception:  # noqa: BLE001
        summary["lessons"], summary["changes"], summary["proposed"] = [], [], []
    return summary
