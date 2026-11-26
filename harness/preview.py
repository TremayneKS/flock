"""Serves project folders read-only on their own port (127.0.0.1:PREVIEW_PORT) so a site the agent builds can be opened in the
preview pane or the agent's headless browser with scripts working. A different origin from the app: pages there cannot
reach the harness API. Only folders registered here are served."""
import hashlib
import mimetypes
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

_roots = {}            # key -> folder
_lock = threading.Lock()
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html", ".htm": "text/html",
         ".json": "application/json", ".svg": "image/svg+xml", ".wasm": "application/wasm", ".md": "text/plain",
         ".txt": "text/plain", ".csv": "text/plain", ".py": "text/plain", ".ts": "text/plain"}


def key_for(folder):
    return hashlib.sha1(os.path.normcase(str(folder)).encode()).hexdigest()[:10]


def register(folder):
    folder = Path(folder).resolve()
    k = key_for(folder)
    with _lock:
        _roots[k] = folder
    return k


def url_for(path):
    """http URL for a file: under a registered folder if one holds it, else its own folder is registered."""
    p = Path(path).resolve()
    with _lock:
        roots = sorted(_roots.items(), key=lambda kv: -len(str(kv[1])))
    for k, root in roots:
        try:
            rel = p.relative_to(root)
            return f"http://127.0.0.1:{config.PREVIEW_PORT}/{k}/" + urllib.parse.quote(rel.as_posix())
        except ValueError:
            continue
    folder = p if p.is_dir() else p.parent
    k = register(folder)
    rel = "" if p.is_dir() else p.name
    return f"http://127.0.0.1:{config.PREVIEW_PORT}/{k}/" + urllib.parse.quote(rel)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        segs = parts.path.lstrip("/").split("/", 1)
        root = _roots.get(segs[0])
        if parts.path.endswith("/favicon.ico") and (not root or not (root / (segs[1] if len(segs) > 1 else "")).is_file()):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not root:
            return self._send(404, b"Not found", "text/plain")
        rel = urllib.parse.unquote(segs[1] if len(segs) > 1 else "")
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self._send(403, b"Outside the folder", "text/plain")
        if target.is_dir():
            if not parts.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", parts.path + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            idx = target / "index.html"
            if idx.exists():
                target = idx
            else:
                items = "".join(f'<li><a href="{urllib.parse.quote(e.name)}{"/" if e.is_dir() else ""}">{e.name}{"/" if e.is_dir() else ""}</a></li>'
                                for e in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())))
                return self._send(200, f"<!doctype html><meta charset=utf-8><ul style='font:14px system-ui'>{items}</ul>".encode(), "text/html; charset=utf-8")
        if not target.is_file():
            return self._send(404, b"Not found", "text/plain")
        ctype = TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "text/javascript"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def do_HEAD(self):
        self.do_GET()

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind a port that is already listening, and requests then go to
    # either copy (seen 2026-11-20: two copies on one port, a turn running in the "old" one after it was replaced). Off
    # there, a second copy on a busy port fails to start instead; run.py --after waits for the port to be free.
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        pass                      # browsers drop connections all the time; nothing to report


def serve():
    srv = Server(("127.0.0.1", config.PREVIEW_PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
