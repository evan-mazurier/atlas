"""Static server for cockpit/web2 with automatic cache-busting (W1 Lane 2, contracts.md §5 intro, §8.1).

    serve(route, if_none_match) -> (code, content_type, body_bytes, headers) | None

* `/` is `index.html`; the resolved path must stay inside cockpit/web2 (else 404).
* While `cockpit/web2/index.html` does not exist, returns None (server.py answers 404);
  once it exists a missing file is a 404, never a silent v1 fallback.
* `index.html` is served with every LOCAL href/src given `?v=<first 10 hex of sha1(file bytes)>` and
  an injected `<script type="importmap">` that maps every `.js` under web2 to its `?v=` URL, so ES
  module imports are versioned too. Nothing is hand-bumped.
* Every file: `Cache-Control: no-cache` + `ETag`; a matching `If-None-Match` answers 304.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent

_TYPES = {".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8",
          ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
          ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
_HASH_CACHE: dict[str, tuple[int, int, str]] = {}
_ATTR = re.compile(r'(\b(?:href|src)\s*=\s*)(["\'])([^"\']*)\2', re.I)


def web2_dir() -> Path:
    env = os.environ.get("ATLAS_WEB2_DIR")
    return Path(env).resolve() if env else HERE / "web2"


def _digest(p: Path) -> str:
    """First 10 hex of sha1(file bytes), cached by (mtime, size)."""
    st = p.stat()
    hit = _HASH_CACHE.get(str(p))
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    h = hashlib.sha1(p.read_bytes()).hexdigest()[:10]
    _HASH_CACHE[str(p)] = (st.st_mtime_ns, st.st_size, h)
    return h


def _inside(root: Path, p: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _local_target(root: Path, url: str) -> Path | None:
    """The file a local href/src names (relative to index.html's folder), or None if not local."""
    if not url or url.startswith(("#", "//", "data:", "mailto:", "javascript:")) or re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
        return None
    path = url.split("?")[0].split("#")[0]
    p = (root / path.lstrip("/")).resolve()
    return p if _inside(root, p) and p.is_file() else None


def render_index(root: Path) -> bytes:
    html = (root / "index.html").read_text(encoding="utf-8")

    def sub(m: re.Match) -> str:
        url = m.group(3)
        t = _local_target(root, url)
        if t is None or "?" in url:
            return m.group(0)
        frag = ""
        if "#" in url:
            url, frag = url.split("#", 1)
            frag = "#" + frag
        return f"{m.group(1)}{m.group(2)}{url}?v={_digest(t)}{frag}{m.group(2)}"

    html = _ATTR.sub(sub, html)
    imports = {}
    for f in sorted(root.rglob("*.js")):
        rel = "/" + f.relative_to(root).as_posix()
        imports[rel] = f"{rel}?v={_digest(f)}"
    tag = '<script type="importmap">' + json.dumps({"imports": imports}, indent=1) + "</script>"
    m = re.search(r"<head[^>]*>", html, re.I)
    html = html[:m.end()] + "\n" + tag + html[m.end():] if m else tag + html
    return html.encode("utf-8")


def _etag_ok(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    for part in if_none_match.split(","):
        part = part.strip()
        if part == "*" or part.removeprefix("W/") == etag:
            return True
    return False


def _not_found():
    return 404, "text/plain; charset=utf-8", b"not found", {"Cache-Control": "no-cache"}


def serve(route: str, if_none_match: str | None = None):
    root = web2_dir()
    if not (root / "index.html").is_file():
        return None
    try:
        rel = unquote((route or "/").split("?")[0].split("#")[0])
    except Exception:  # noqa: BLE001
        return _not_found()
    if "\x00" in rel or "\\" in rel:
        return _not_found()
    rel = rel.lstrip("/") or "index.html"
    p = (root / rel).resolve()
    if not _inside(root, p) or not p.is_file():
        return _not_found()
    if p == (root / "index.html").resolve():
        body = render_index(root)
    else:
        body = p.read_bytes()
    etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
    ctype = _TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    if _etag_ok(if_none_match, etag):
        return 304, ctype, b"", headers
    return 200, ctype, body, headers
