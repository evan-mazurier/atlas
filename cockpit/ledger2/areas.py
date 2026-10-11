"""Area resolution, shared by the hooks, the ledger and the Plan (contracts.md §1.3, §6).

Stdlib-only: the installer copies this file verbatim into ~/.claude/hooks/atlas/. It also carries a
tiny YAML reader (`mini_yaml`) good enough for areas.yaml and workflows.yaml (block maps, block lists,
flow lists/maps, quoted strings, comments), so the hooks never need PyYAML.
"""
from __future__ import annotations

import json
import os
import re

# ---------------------------------------------------------------- tiny YAML reader


def _strip_comment(s: str) -> str:
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i]
    return s


def _bare(tok: str):
    t = tok.strip()
    if t in ("", "~", "null", "Null", "NULL"):
        return None
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?\d+", t):
        return int(t)
    if re.fullmatch(r"[-+]?\d*\.\d+", t):
        return float(t)
    return t


class _Flow:
    def __init__(self, s: str):
        self.s, self.i = s, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def value(self):
        self.ws()
        c = self.s[self.i] if self.i < len(self.s) else ""
        if c == "[":
            self.i += 1
            out = []
            while True:
                self.ws()
                if self.i >= len(self.s):
                    return out
                if self.s[self.i] == "]":
                    self.i += 1
                    return out
                start = self.i
                out.append(self.value())
                self.ws()
                if self.i < len(self.s) and self.s[self.i] == ",":
                    self.i += 1
                if self.i == start:
                    raise ValueError("flow sequence: unexpected character")
        if c == "{":
            self.i += 1
            out = {}
            while True:
                self.ws()
                if self.i >= len(self.s):
                    return out
                if self.s[self.i] == "}":
                    self.i += 1
                    return out
                start = self.i
                if self.s[self.i] == "]":
                    raise ValueError("flow mapping: unexpected ]")
                k = self.token(":,}")
                self.ws()
                v = None
                if self.i < len(self.s) and self.s[self.i] == ":":
                    self.i += 1
                    v = self.value()
                out[str(k)] = v
                self.ws()
                if self.i < len(self.s) and self.s[self.i] == ",":
                    self.i += 1
                if self.i == start:
                    raise ValueError("flow mapping: unexpected character")
        return self.token(",]}")

    def token(self, stops: str):
        self.ws()
        if self.i < len(self.s) and self.s[self.i] in ("'", '"'):
            q = self.s[self.i]
            j = self.i + 1
            buf = []
            while j < len(self.s):
                ch = self.s[j]
                if q == "'" and ch == "'" and self.s[j + 1:j + 2] == "'":
                    buf.append("'")
                    j += 2
                    continue
                if q == '"' and ch == "\\" and j + 1 < len(self.s):
                    nxt = self.s[j + 1]
                    buf.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
                    j += 2
                    continue
                if ch == q:
                    break
                buf.append(ch)
                j += 1
            self.i = j + 1
            return "".join(buf)
        j = self.i
        while j < len(self.s) and self.s[j] not in stops:
            if self.s[j] == ":" and ":" in stops and not (self.s[j + 1:j + 2] in ("", " ", ",", "}")):
                j += 1
                continue
            j += 1
        raw = self.s[self.i:j]
        self.i = j
        return _bare(raw)


def _scalar(text: str):
    t = text.strip()
    if not t:
        return None
    if t[0] in "[{" or t[0] in "'\"":
        return _Flow(t).value()          # a malformed flow value raises ValueError (callers fall back)
    return _bare(t)


_KEY_RE = re.compile(r"^([^\s'\"\[\]{},#:][^:]*?|'[^']*'|\"[^\"]*\")\s*:(?:\s+(.*)|$)")


def _split_key(content: str):
    m = _KEY_RE.match(content)
    if not m:
        return None
    k = m.group(1)
    if k[:1] in ("'", '"'):
        k = k[1:-1]
    return k, (m.group(2) or "")


def mini_yaml(text: str):
    rows: list[list] = []
    for raw in text.splitlines():
        c = _strip_comment(raw).rstrip()
        if not c.strip() or c.strip() in ("---", "..."):
            continue
        rows.append([len(c) - len(c.lstrip(" ")), c.strip()])
    if not rows:
        return None
    val, _ = _block(rows, 0, rows[0][0])
    return val


def _is_item(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _block(rows, i, indent):
    if _is_item(rows[i][1]):
        return _list(rows, i, indent)
    return _map(rows, i, indent)


def _list(rows, i, indent):
    out = []
    while i < len(rows) and rows[i][0] == indent and _is_item(rows[i][1]):
        item = rows[i][1][1:].strip()
        if item == "":
            if i + 1 < len(rows) and rows[i + 1][0] > indent:
                v, i = _block(rows, i + 1, rows[i + 1][0])
                out.append(v)
            else:
                out.append(None)
                i += 1
        elif _split_key(item) and item[0] not in "[{'\"":
            offset = len(rows[i][1]) - len(item)
            rows[i] = [indent + offset, item]
            v, i = _map(rows, i, indent + offset)
            out.append(v)
        else:
            out.append(_scalar(item))
            i += 1
    return out, i


def _map(rows, i, indent):
    out = {}
    while i < len(rows) and rows[i][0] == indent and not _is_item(rows[i][1]):
        kv = _split_key(rows[i][1])
        if not kv:
            i += 1
            continue
        k, rest = kv
        if rest.strip() == "":
            if i + 1 < len(rows) and (rows[i + 1][0] > indent
                                      or (rows[i + 1][0] == indent and _is_item(rows[i + 1][1]))):
                v, i = _block(rows, i + 1, rows[i + 1][0])
                out[k] = v
            else:
                out[k] = None
                i += 1
        else:
            out[k] = _scalar(rest)
            i += 1
    return out, i


# ---------------------------------------------------------------- areas

_HERE = os.path.dirname(os.path.abspath(__file__))


def default_areas_path() -> str:
    """Repo-root areas.yaml when this file sits in cockpit/ledger2/; else areas.yaml/areas.json beside it."""
    repo = os.path.normpath(os.path.join(_HERE, "..", "..", "areas.yaml"))
    if os.path.isfile(repo):
        return repo
    for n in ("areas.json", "areas.yaml"):
        p = os.path.join(_HERE, n)
        if os.path.isfile(p):
            return p
    return repo


def _read_any(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.lower().endswith(".json"):
        return json.loads(text)
    return mini_yaml(text)


def load_areas(path=None) -> list[dict]:
    """The areas from areas.yaml (repo root by default), in file order. Falls back to a sibling
    areas.json when the YAML is missing or does not parse; [] when nothing works."""
    cands = [str(path)] if path else [default_areas_path()]
    cands.append(os.path.join(_HERE, "areas.json"))
    for p in cands:
        try:
            data = _read_any(p)
        except Exception:  # noqa: BLE001
            continue
        lst = data.get("areas") if isinstance(data, dict) else data
        if isinstance(lst, list):
            out = []
            for a in lst:
                if isinstance(a, dict) and a.get("id"):
                    a = dict(a)
                    for key in ("folders", "playbooks", "projects"):
                        if key in a and not isinstance(a[key], list):
                            a[key] = [a[key]] if a[key] else []
                    out.append(a)
            if out:
                return out
    return []


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").rstrip("/").lower()


def area_for(areas: list[dict], cwd: str | None = None, area_arg: str | None = None,
             playbook: str | None = None) -> str | None:
    """Explicit area arg → playbook → longest folder prefix of cwd → None."""
    ids = {a.get("id") for a in areas}
    arg = (area_arg or "").strip().lower()
    if arg and arg in ids:
        return arg
    pb = (playbook or "").strip().lstrip("/").lower()
    if pb:
        for a in areas:
            if pb in [str(x).lstrip("/").lower() for x in (a.get("playbooks") or [])]:
                return a.get("id")
    c = _norm(cwd or "")
    if c:
        best, best_len = None, -1
        for a in areas:
            for f in a.get("folders") or []:
                nf = _norm(f)
                if nf and (c == nf or c.startswith(nf + "/")) and len(nf) > best_len:
                    best, best_len = a.get("id"), len(nf)
        if best:
            return best
    return None
