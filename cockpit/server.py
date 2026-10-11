"""
Atlas cockpit: local server.

A tiny stdlib HTTP server that:
  - serves the v2 front end (cockpit/web2, via web2_static)
  - routes /api/v2/* to api_v2 (plan, team, ledger, attention, areas)

Run:
    py -3.12 D:\\AI\\Atlas\\cockpit\\server.py            # -> http://localhost:8765
    py -3.12 D:\\AI\\Atlas\\cockpit\\server.py --port 9000
"""
from __future__ import annotations

import os
import sys
import json
import argparse
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = Path(__file__).resolve().parent
WEB2 = HERE / "web2"

if str(HERE) not in sys.path:  # so `api_v2`/`web2_static`/`teamlive` resolve
    sys.path.insert(0, str(HERE))  # whether launched as a script or as `cockpit.server`

class Handler(SimpleHTTPRequestHandler):
    # Static files come from web2_static (the v2 front end); nothing else is served.
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB2), **kw)

    def log_message(self, *args):  # quiet console; errors still surface via exceptions
        pass

    def handle(self):
        # A live-polled dashboard drops connections routinely (tab close, refresh mid-request).
        # Swallow the resulting client-disconnect errors so they don't spam tracebacks.
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    def end_headers(self):
        # never cache: the cockpit is iterated live, stale app.js/styles.css = confusion.
        # v2 static files set their own Cache-Control (no-cache + ETag, contracts.md §8.1).
        if not getattr(self, "_own_cache_header", False):
            self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    # --- v2 seam (docs/v2/contracts.md §5, §9): /api/v2/* goes to api_v2; static to web2_static ---
    def _v2_api(self, method: str) -> bool:
        route = self.path.split("?")[0]
        if not route.startswith("/api/v2/"):
            return False
        try:
            import api_v2
            if method == "GET":
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                res = api_v2.handle_get(route, q)
            else:
                try:
                    body = self._body()
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    self._json({"ok": False, "error": "body must be a JSON object"}, code=400)
                    return True
                res = api_v2.handle_post(route, body)
            if res is None:
                self._json({"ok": False, "error": "not found"}, code=404)
            else:
                code, payload = res
                self._json(payload, code=code)
        except Exception as exc:  # noqa: BLE001 — the API never takes the server down
            self._json({"ok": False, "error": str(exc)}, code=500)
        return True

    def _v2_static(self) -> bool:
        route = self.path.split("?")[0]
        try:
            import web2_static
            res = web2_static.serve(route, self.headers.get("If-None-Match"))
        except Exception:  # noqa: BLE001 — a static failure is a 404, never a crash
            res = None
        if res is None:
            return False
        code, ctype, body, headers = res
        self._own_cache_header = True
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _json(self, payload: dict, code: int = 200):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self._v2_api("GET"):
            return
        if not self.path.startswith("/api/") and self._v2_static():
            return
        self._json({"ok": False, "error": "not found"}, code=404)

    def _body(self) -> dict:
        ln = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(ln) or b"{}")

    def do_POST(self):
        if self._v2_api("POST"):
            return
        self._json({"ok": False, "error": "not found"}, code=404)


def _open_cockpit(url: str) -> None:
    """Open the cockpit in a dedicated CHROME app window (lean: no tab bar, own minimal profile) —
    Preferred over the OS-default browser. Falls back to Edge's app mode, then
    the OS default. `--app` + an isolated `--user-data-dir` keep it a minimal, single-purpose window."""
    import os, shutil
    prof = os.path.join(os.environ.get("LOCALAPPDATA", ""), "AtlasCockpitProfile")
    chrome_candidates = [
        os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    chrome = next((c for c in chrome_candidates if c and os.path.isfile(c)), None) or shutil.which("chrome")
    try:
        exe = chrome or shutil.which("msedge")          # Chrome preferred; Edge is the app-mode fallback
        if exe:
            import subprocess
            subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={prof}"])
            return
    except Exception:  # noqa: BLE001 — never let the browser-open crash the server
        pass
    webbrowser.open(url)                                 # last resort: the OS default browser


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    if not WEB2.exists():
        print(f"[cockpit] missing web dir: {WEB2}", file=sys.stderr)
        sys.exit(1)

    url = f"http://localhost:{args.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    # Also serve on IPv6 loopback (::1): Windows resolves "localhost" to ::1 first, so an IPv4-only
    # bind makes every localhost request wait out an IPv6 SYN timeout before falling back to 127.0.0.1
    # (a per-request lag). Both listeners are loopback-only — no network exposure. Skip if IPv6 is off.
    import socket as _socket
    class _V6Server(ThreadingHTTPServer):
        address_family = _socket.AF_INET6
    try:
        _srv6 = _V6Server(("::1", args.port), Handler)
        threading.Thread(target=_srv6.serve_forever, daemon=True).start()
    except OSError:
        pass
    print(f"[cockpit] Atlas cockpit on {url}  (Ctrl-C to stop)")
    if not args.no_open:
        _open_cockpit(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[cockpit] stopped")
        srv.server_close()


if __name__ == "__main__":
    main()
