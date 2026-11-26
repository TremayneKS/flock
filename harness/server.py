"""Flock web app: http://127.0.0.1:8780 (window) + http://127.0.0.1:8781 (project previews).

Listens on 127.0.0.1 only. The agent can run commands, so a web page open in a browser must never reach this API: Host and
Origin are checked on every request, and anything that changes state needs the X-Flock-App (or legacy X-Nest-App / X-KC-App) header (a cross-site page cannot
send it without a CORS preflight, which is never granted)."""
import gzip
import json
import mimetypes
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import agent, burn_monitor, chat_import, config, desktop, dictation, fork as chat_fork, groups, imports, llm, mcp, meetings, osutil, presets, preview, providers, sections, selfupdate, skills, store, tools, updates
from . import computer, effort, resume, subagents
from . import vision
from . import runtime as runtime_mod
from .runtime import runtime

WEB = config.APP / "web"
ALLOWED_HOSTS = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}"}


def _remote():
    """Reaching the app from the user's own other devices (a phone) through Tailscale Serve, which forwards a private
    https name on the tailnet to this port and says who is asking. Off unless the profile has
    "remote": {"hosts": ["pc.tailXXXX.ts.net"], "tailscale_logins": ["me@example.com"]}.
    A request for one of those names must carry Tailscale's Tailscale-User-Login header with an allowed login; a
    request to 127.0.0.1 is treated exactly as before."""
    r = config.get().get("remote") or {}
    hosts = {str(h).lower() for h in r.get("hosts") or []}
    return hosts, {str(x).lower() for x in r.get("tailscale_logins") or []}


def remote_ok(headers):
    """(is this a remote request?, is it allowed?)"""
    hosts, logins = _remote()
    host = (headers.get("Host") or "").lower()
    if not hosts or host not in hosts:
        return False, False
    origin = headers.get("Origin")
    if origin and origin.lower() not in {f"https://{host}", f"http://{host}"}:
        return True, False
    return True, (headers.get("Tailscale-User-Login") or "").lower() in logins


def csp():
    frames = " ".join([f"http://127.0.0.1:{config.PREVIEW_PORT}", f"http://localhost:{config.PREVIEW_PORT}", *meetings.origins()])
    return ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            f"frame-src 'self' {frames}; connect-src 'self'; font-src 'self'; media-src 'self' blob:; object-src 'none'; "
            "base-uri 'none'; form-action 'none'")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml", ".woff2": "font/woff2", ".ico": "image/x-icon", ".png": "image/png", ".json": "application/json"}
STARTED = time.time()

# ---- the page must always start. The chat list and the start-up state are built from parts read from files that other
# programs also write (worker chats, imported chats, sections). A part that fails is left out, written to errors.log
# with its trace and named in the answer ("trouble") and in /api/selfcheck, so one bad record never empties the page.
_trouble = {}
_noted = {}
_trouble_lock = threading.Lock()
_slow_lock = threading.Lock()
SLOW_MS = 200.0
SLOW_LOG_MAX = 512 * 1024
_SLOW_SKIP_PREFIXES = ("/api/events", "/api/dictation/", "/api/subagent")


def _log_slow(line):
    """Append one compact slow-timing line to LOGS/slow.log with lightweight rotation (never raises)."""
    try:
        p = config.LOGS / "slow.log"
        with _slow_lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                if p.exists() and p.stat().st_size > SLOW_LOG_MAX:
                    bak = config.LOGS / "slow.log.1"
                    os.replace(p, bak)
            except OSError:
                pass
            with p.open("a", encoding="utf-8") as f:
                f.write(time.strftime("%F %T ") + line.replace("\r", " ").replace("\n", " ")[:420] + "\n")
    except OSError:
        pass


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _part(name, fn, default):
    try:
        v = fn()
        if name in _trouble:
            with _trouble_lock:
                _trouble.pop(name, None)
        return v
    except Exception as e:
        err = f"{e.__class__.__name__}: {e}"[:300]
        with _trouble_lock:
            old = _trouble.get(name) or {}
            same = old.get("error") == err
            fresh = not same or time.time() - old.get("logged", 0) > 600
            _trouble[name] = {"part": name, "error": err, "since": old.get("since") if same else time.time(),
                              "logged": time.time() if fresh else old.get("logged", 0)}
        if fresh:
            try:
                with (config.LOGS / "errors.log").open("a", encoding="utf-8") as f:
                    f.write(time.strftime("%F %T ") + f"left out of the page: {name}\n" + traceback.format_exc() + "\n")
            except OSError:
                pass
        return default() if callable(default) else default


def trouble():
    with _trouble_lock:
        return [{"part": t["part"], "error": t["error"], "since": t["since"]} for t in _trouble.values()]


def _dress(c, st, g_by_id, c_by_id, sync_map):
    c["section"] = sections.chat_section(c, st, g_by_id, c_by_id)
    arch_map = st.get("archived") or {}
    tidy_map = st.get("tidy_titles") or {}
    if c["id"] in arch_map:
        c["archived"] = True
        c["archive_reason"] = arch_map[c["id"]]
    if not c.get("neon") and not c.get("user_renamed") and c["id"] in tidy_map:
        c["raw_title"] = c.get("title") or ""
        c["title"] = tidy_map[c["id"]]
    if c.get("source") in sync_map:
        c["synced_at"] = sync_map[c["source"]]


def chat_list(proj=None, chats=None):
    """What GET /api/chats answers. Every part and every chat is guarded on its own."""
    if chats is None or proj is not None:
        chats = _part("saved chats", lambda: store.list_chats(proj), list)
    else:
        chats = [dict(c) for c in chats if isinstance(c, dict)]
    gs = [g for g in _part("groups", groups.all_groups, list) if isinstance(g, dict) and g.get("id")]
    all_c = [c for c in chats if isinstance(c, dict) and c.get("id")]
    all_c.sort(key=lambda m: -_num(m.get("updated")))
    st = _part("sections", sections.state, dict)
    if not isinstance(st, dict):
        st = {}
    g_by_id = {g["id"]: g for g in gs}
    c_by_id = {c["id"]: c for c in all_c}
    for g in gs:
        g["section"] = _part("group sections", lambda g=g: sections.group_section(g["id"], st, g_by_id), None)
    sync_map = _part("import times", chat_import.last_sync_map, dict)
    def dress_all():
        first = None
        for c in all_c:
            c["archived"] = False
            try:
                _dress(c, st, g_by_id, c_by_id, sync_map)
            except Exception as e:                # that chat is listed plain; the first failure is the one reported
                first = first or e
        if first:
            raise first
    _part("chat details", dress_all, None)
    return {"chats": all_c, "groups": gs, "sections": st, "sources_synced": sync_map, "trouble": trouble()}


def start_state(chats=None):
    """What GET /api/state answers: the first call the page makes."""
    _part("profile", config.reload_if_profile_changed, None)
    cfg = _part("config", lambda: public_config(chats=chats), dict)
    bid = (cfg or {}).get("build") or _part("build", config.build_id, "")
    return {"config": cfg, "projects": _part("projects", store.projects, list),
            "runtime": _part("runtime", runtime.status, dict), "meetings": _part("meetings", meetings.status_cached, dict),
            "started": STARTED, "build": bid,
            "burn_alerts": _part("burn alerts", burn_monitor.active_alerts, list),
            "sources_synced": _part("import times", chat_import.last_sync_map, dict), "trouble": trouble()}


def selfcheck():
    """The page's own start-up calls, run here, for whatever watches the app (GET /api/selfcheck). ok is False when the
    page could not start or would start with something missing."""
    out = {"ok": True, "build": "", "pid": os.getpid(), "started": STARTED, "chats": 0, "problems": []}
    try:
        saved = _part("saved chats", lambda: store.list_chats(None), list)
        st = start_state(chats=saved)
        out["build"] = st.get("build") or ""
        cl = chat_list(None, chats=saved)
        json.dumps(cl, ensure_ascii=False, default=str)
        out["chats"] = len(cl["chats"])
        for f in ("index.html", "app.js"):
            if len((WEB / f).read_bytes()) < 200:
                out["problems"].append("the page file %s is empty" % f)
    except Exception as e:
        out["problems"].append(f"{e.__class__.__name__}: {e}"[:300])
    out["problems"] += ["%s: %s" % (t["part"], t["error"]) for t in trouble()]
    out["ok"] = not out["problems"]
    return out
# file types the system's table may not know (or gets wrong): served to the file viewers' <img>, <video> and <audio>
RAW_TYPES = {".avif": "image/avif", ".webp": "image/webp", ".ico": "image/x-icon", ".svg": "image/svg+xml", ".apng": "image/apng",
             ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
             ".ogv": "video/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg",
             ".opus": "audio/ogg", ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac", ".weba": "audio/webm"}


KEY_KINDS = ("openai", "anthropic", "opencode-go")
HIDDEN = ("key_file", "key_env", "key_provider", "key_success", "headers")     # never sent to the browser


def public_config(chats=None):
    cfg = config.get()
    eps = []
    for e in cfg["endpoints"]:
        x = {k: v for k, v in e.items() if k not in HIDDEN}
        x["has_key"] = bool(config.read_keys(e)) if e.get("kind") in KEY_KINDS else True
        x["key_source"] = ("plugin" if e.get("key_provider") else
                           "file" if (config.KEYS / e["id"]).exists() or (e.get("key_file") and config.expand(e["key_file"]).exists())
                           else "env" if e.get("key_env") and x["has_key"] else "")
        x["from_profile"] = config.in_profile(e["id"])
        x["show_in_picker"] = e.get("show_in_picker", True) is not False
        x["runtime"] = runtime_mod.cfg().get("label") or "server" if runtime_mod.serves(e["id"]) else None
        x["levels"], x["effort_default"] = effort.levels(e), effort.default(e, cfg["reasoning"])   # top-bar levels
        x["effort_from"] = effort.FROM_GENERIC.get(e.get("kind"))        # Fast/Think/Deep -> level, for chats with no pick
        eps.append(x)
    # Count recent model usage across chats so the picker puts the user's most-used models first
    usage_counts = {}
    for ch in (store.list_chats() if chats is None else chats):
        eid = ch.get("endpoint")
        if eid:
            usage_counts[eid] = usage_counts.get(eid, 0) + 1
    specs = mcp.specs()
    tools_status = {name: {"enabled": specs.get(name, {}).get("enabled", True), "installed": name in specs,
                           "builtin": specs.get(name, {}).get("builtin", False),
                           "command": " ".join([specs.get(name, {}).get("command", ""), *map(str, specs.get(name, {}).get("args", []))])[:300]}
                    for name in sorted(set(specs) | {"web", "browser"})}
    return {"endpoint": cfg["endpoint"], "reasoning": cfg["reasoning"], "compact_at": cfg["compact_at"], "endpoints": eps,
            "model_usage": usage_counts, "build": config.build_id(),
            "mcp": tools_status, "imported_mcp": cfg.get("imported_mcp", []), "project_files": cfg.get("project_files", False),
            "skills": skills.enabled(), "subagents": {**subagents.settings(), "presets": subagents.presets()}, "effort_levels": effort.NATIVE,
            "workspace": str(config.WORKSPACE), "data": str(config.DATA),
            "profile": str(config.PROFILE_PATH) if config.PROFILE_PATH.exists() else "", "preview_port": config.PREVIEW_PORT,
            "features": {"runtime": runtime_mod.enabled(), "meetings": meetings.enabled(),
                         "meetings_url": meetings.base() if meetings.enabled() else "", "meetings_label": meetings.label(), "dictation": dictation.available(),
                         "dictation_engine": cfg.get("dictation_engine") or ""}}


def safe_path(p):
    if not p:
        raise ValueError("no path")
    path = Path(os.path.expanduser(p))
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    path = Path(os.path.normpath(str(path)))
    if tools.DENY_RE.search(str(path)):
        raise PermissionError("that is a credential store")
    return path


IGNORE = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist", ".cache", ".pytest_cache", ".mypy_cache"}


def list_dir(path, show_hidden=False):
    out = []
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        raise ValueError(str(e))
    for e in entries:
        if not show_hidden and (e.name.startswith(".") and e.name not in (".env", ".gitignore", ".github")):
            continue
        try:
            isdir = e.is_dir()
            st = e.stat()
        except OSError:
            continue
        out.append({"name": e.name, "path": str(Path(path) / e.name), "dir": isdir, "size": None if isdir else st.st_size,
                    "mtime": st.st_mtime, "heavy": isdir and e.name in IGNORE})
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return out[:2000]


_sent = {}                        # (chat id, the page's key for one message) -> (when, what the send answered)
_sent_lock = threading.Lock()
_send_locks = {}                  # chat id -> the lock its sends take in turn
SENT_KEEP_S = 900


def send_once(cid, key, go):
    """Run one send. The page sends a message again when it got no answer (the connection dropped); with the same
    key the message is taken once and the later tries get the first answer, so a retry never makes a second turn or a
    second copy in the queue. No key (an older page, a script): every call is a send, as before."""
    key = str(key or "")[:80]
    if not key:
        return go()
    with _sent_lock:
        now = time.time()
        for k in [k for k, (t, _r) in _sent.items() if now - t > SENT_KEEP_S]:
            del _sent[k]
        mine = _send_locks.setdefault(cid, threading.Lock())
    with mine:                          # one chat's sends in order; another chat's never wait behind them
        with _sent_lock:
            seen = _sent.get((cid, key))
        if seen:
            return seen[1]
        r = go()
        with _sent_lock:
            _sent[(cid, key)] = (now, r)
        return r


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Flock"

    def log_message(self, *a):
        pass

    # ---- plumbing
    def _guard(self, changing):
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        is_remote, allowed = remote_ok(self.headers)
        if is_remote:
            if not allowed:
                self._json({"error": "Not allowed."}, 403)
                return False
        elif host not in ALLOWED_HOSTS or (origin and origin not in ALLOWED_ORIGINS):
            self._json({"error": "Not allowed."}, 403)
            return False
        if changing and self.headers.get("X-Flock-App") != "1" and self.headers.get("X-Nest-App") != "1" and self.headers.get("X-KC-App") != "1":
            self._json({"error": "Not allowed."}, 403)
            return False
        return True

    def _json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        packed = len(body) > 4096 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        if packed:                      # a chat's page is 300 KB of text: a fifth of that on the phone's link
            body = gzip.compress(body, 5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if packed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data, ctype, status=200, cache=False):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", csp())
        self.end_headers()
        self.wfile.write(data)

    def _body(self, limit=50 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n) if n else b""

    def _jbody(self):
        raw = self._body()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _q(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    # ---- routes
    def _record_slow(self, method, path, t0, status):
        if any(path.startswith(p) for p in _SLOW_SKIP_PREFIXES):
            return
        ms = (time.perf_counter() - t0) * 1000.0
        if ms >= SLOW_MS:
            _log_slow(f"server {method} {self.path[:180]} status={status} ms={ms:.0f}")

    def do_GET(self):
        t0 = time.perf_counter()
        path = urllib.parse.urlsplit(self.path).path
        status = 200
        try:
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.startswith("/api/llm/"):
                return self._llm("GET", path)
            if not self._guard(False):
                status = 403
                return
            try:
                self._get(path, self._q())
            except (ValueError, PermissionError, FileNotFoundError) as e:
                status = 400
                self._note(path, e)
                self._json({"error": str(e)}, 400)
            except Exception as e:
                status = 500
                self._fail(e)
        finally:
            self._record_slow("GET", path, t0, status)

    def do_POST(self):
        t0 = time.perf_counter()
        path = urllib.parse.urlsplit(self.path).path
        status = 200
        try:
            if path.startswith("/api/llm/"):
                return self._llm("POST", path)
            if not self._guard(True):
                status = 403
                return
            try:
                self._post(path, self._q())
            except (ValueError, PermissionError, FileNotFoundError, RuntimeError) as e:
                status = 400
                self._json({"error": str(e)}, 400)
            except Exception as e:
                status = 500
                self._fail(e)
        finally:
            self._record_slow("POST", path, t0, status)

    def _llm(self, method, path):
        """The selected model for other local apps (Meetings): its own token instead of the window's header (llm.py)."""
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            return self._json({"error": "Not allowed."}, 403)
        try:
            llm.serve(self, method, path)
        except (ConnectionError, OSError):
            self.close_connection = True
        except Exception as e:
            self._fail(e)

    def _note(self, path, e):
        """A refused GET is usually a bad request, but a ValueError can also be the app tripping on its own data (the
        chat list did, 21 Nov 2026): keep the trace so it can be found. One entry per path and error per 10 minutes."""
        key = (path, str(e)[:120])
        if time.time() - _noted.get(key, 0) < 600:
            return
        if len(_noted) > 500:
            _noted.clear()
        _noted[key] = time.time()
        try:
            with (config.LOGS / "refused.log").open("a", encoding="utf-8") as f:
                f.write(time.strftime("%F %T ") + path + "\n" + traceback.format_exc() + "\n")
        except OSError:
            pass

    def _fail(self, e):
        (config.LOGS / "errors.log").open("a", encoding="utf-8").write(time.strftime("%F %T ") + traceback.format_exc() + "\n")
        try:
            self._json({"error": f"{e.__class__.__name__}: {e}"}, 500)
        except Exception:
            pass

    def _static(self, rel):
        target = (WEB / rel).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            return self._bytes(b"no", "text/plain", 404)
        if not target.is_file():
            return self._bytes(b"not found", "text/plain", 404)
        self._bytes(target.read_bytes(), TYPES.get(target.suffix.lower(), "application/octet-stream"))

    def _raw(self, p):
        """A file's bytes for the file viewers, streamed, with HTTP Range (media seeking, reading the start or end of a big
        file). Only images, audio and video keep their type; everything else is plain text. The sandbox policy means even an
        SVG opened on its own can never run script as the app."""
        ctype = RAW_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or ""
        if not ctype.startswith(("image/", "audio/", "video/")):
            ctype = "text/plain; charset=utf-8"
        size = p.stat().st_size
        start, end, status = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:")
        self.end_headers()
        try:
            with p.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(left, 1 << 18))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            self.close_connection = True      # the browser stopped reading (a video seeking): nothing to report

    def _get(self, path, q):
        if path == "/api/build":
            config.reload_if_profile_changed()
            return self._json({"build": config.build_id(), "profile_mtime": config.profile_mtime(), "started": STARTED})
        if path.startswith("/api/chat-import/"):          # importing ChatGPT, Claude.ai, Claude Code, Codex chats
            return chat_import.http_get(self, path, q)
        if path == "/api/state":
            # probes of a closed local port take about 2 s on Windows: both answer from a cache refreshed in the background
            return self._json(start_state())
        if path == "/api/selfcheck":      # no page needed: the start-up calls run here and say what is wrong
            return self._json(selfcheck())
        if path == "/api/events":
            return self._events()
        if path == "/api/presets":
            return self._json({"presets": presets.public(), "claude": bool(config.claude_exe()), "codex": bool(config.codex_exe()),
                               "agy": bool(config.agy_exe())})
        if path == "/api/sections":
            return sections.http_get(self, path, q)
        if path == "/api/chats":
            return self._json(chat_list(q.get("project") or None))
        if path == "/api/busy":           # which chats have a turn running (the page checks it after a reconnect)
            chats, subs = agent.busy_chats(), agent.busy_subagents()
            watch = list(dict.fromkeys(chats + subs + ([q["chat"]] if q.get("chat") else [])))
            tails = {}
            for cid in watch:
                if not cid:
                    continue
                try:
                    tails[cid] = store.Chat(cid).tail_info()
                except ValueError:
                    pass
            return self._json({"chats": chats, "subagents": subs, "activity": agent.activities(chats + subs),
                               "background": agent.background_activities(chats + subs), "tails": tails})
        if path == "/api/capabilities":   # for tools that restart the app (the shipping pipeline): a running turn
            # carries on after a restart (resume.py), so a restart need not wait for the chats to be idle. Restart with
            # POST /api/restart, or by ending this process alone (never its whole tree: its commands keep running).
            return self._json({"resume_turns": True, "restart": "POST /api/restart", "pid": os.getpid()})
        if path.startswith("/api/chats/"):
            parts = path.split("/")
            cid = parts[3]
            if len(parts) > 4 and parts[4] == "subagents":
                return self._json({"subagents": subagents.children_of(cid)})
            if len(parts) > 5 and parts[4] == "group" and parts[5] == "state":
                from . import group_chat
                return self._json({"group": group_chat.public_state(cid)})
            if len(parts) > 4 and parts[4] == "subagent":      # an imported subagent run, opened from its tool row
                if q.get("limit"):
                    return self._json(chat_import.subagent_window(
                        cid, q.get("id") or "", int(q["limit"]), int(q["before"]) if q.get("before") else None))
                return self._json({"messages": chat_import.subagent_messages(cid, q.get("id") or "")})
            if q.get("after"):
                ch = store.Chat(cid)
                total, new_msgs, truncated = ch.since(q["after"])
                return self._json({
                    "after": q["after"], "total": total, "truncated": truncated,
                    "messages": [agent._public(m) for m in new_msgs],
                    "busy": agent.busy(cid), "meta": agent._meta_public(ch.meta()),
                    "queued": agent.queued(cid),
                })
            limit = int(q["limit"]) if q.get("limit") else None
            return self._json(agent.history(cid, limit, int(q["before"]) if q.get("before") else None))
        if path == "/api/files/list":
            p = safe_path(q.get("path"))
            return self._json({"path": str(p), "entries": list_dir(p, q.get("hidden") == "1")})
        if path == "/api/files/read":
            p = safe_path(q.get("path"))
            st = p.stat()
            if st.st_size > 5_000_000:
                return self._json({"path": str(p), "too_big": True, "size": st.st_size, "mtime": st.st_mtime})
            data = p.read_bytes()
            if tools.is_binary(data):
                return self._json({"path": str(p), "binary": True, "size": st.st_size, "mtime": st.st_mtime})
            return self._json({"path": str(p), "text": data.decode("utf-8", "replace"), "mtime": st.st_mtime,
                               "crlf": b"\r\n" in data[:20000]})
        if path == "/api/files/raw":
            return self._raw(safe_path(q.get("path")))
        if path == "/api/files/search":
            root = safe_path(q.get("root"))
            needle = (q.get("q") or "").lower()
            out = []
            for dirpath, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in IGNORE and not d.startswith(".")]
                for f in files:
                    full = os.path.join(dirpath, f)
                    rel = os.path.relpath(full, root)
                    if needle in rel.lower():
                        out.append(rel.replace("\\", "/"))
                        if len(out) >= 60:
                            break
                if len(out) >= 60:
                    break
            return self._json({"files": out})
        if path == "/api/preview-url":
            return self._json({"url": preview.url_for(safe_path(q.get("path")))})
        if path == "/api/runtime":
            return self._json(runtime.status())
        if path == "/api/endpoints/health":
            ep = config.endpoint(q.get("id"))
            return self._json(providers.health(ep))
        if path == "/api/endpoints/statuses":
            config.reload_if_profile_changed()
            return self._json({"statuses": providers.all_statuses(q.get("force") == "1")})
        if path == "/api/updates":
            return self._json(updates.check(q.get("force") == "1"))
        if path == "/api/updates/app":    # Flock itself: answers from the last check unless force=1
            return self._json(selfupdate.status(q.get("force") == "1"))
        if path == "/api/imports":
            return self._json({"items": imports.scan()})
        if path == "/api/meetings":
            # the Meeting button asks with fresh=1 (a stale "running" would show a dead app); the window's polls use the cache
            return self._json(meetings.status() if q.get("fresh") == "1" else meetings.status_cached())
        if path == "/api/burn":
            return self._json(burn_monitor.snapshot())
        if path.startswith("/api/computer/"):            # the Computer tool's questions, banner and settings
            return self._json(computer.http_get(path, q))
        if path == "/api/subagents/presets":
            return self._json({"presets": subagents.presets(), "models": subagents.model_lines()})
        if path == "/api/project-notes":
            p = store.project_notes_path(q.get("project") or "")
            return self._json({"text": p.read_text(encoding="utf-8") if p.exists() else "", "path": str(p)})
        self._json({"error": "unknown"}, 404)

    def _post(self, path, q):
        if path == "/api/dictate":
            data = self._body(30 * 1024 * 1024)
            ext = ".webm" if "webm" in (self.headers.get("Content-Type") or "") else ".wav"
            return self._json(dictation.transcribe(data, ext))
        if path.startswith("/api/dictation/"):            # before _jbody: audio arrives as raw 16-bit PCM
            return dictation.http_post(self, path, q)
        if path.startswith("/api/chat-import/"):          # before _jbody: an upload streams the export file to disk
            return chat_import.http_post(self, path, q)
        if path.startswith("/api/chats/") and path.endswith("/attach") and not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            # Raw binary upload with upload progress
            cid = path.split("/")[3]
            raw = self._body(vision.MAX_FILE_BYTES + 1024)
            folder = store.Chat(cid).dir / "attachments"
            p = vision.save_any_attachment(folder, raw, q.get("mime") or (self.headers.get("Content-Type") or ""), q.get("name") or "")
            return self._json({"path": str(p), "size": len(raw), "kind": "image" if vision.is_image(p) else "file"})
        b = self._jbody()
        if path == "/api/perf":
            evs = b.get("events") if isinstance(b.get("events"), list) else []
            for ev in evs[:30]:
                if not isinstance(ev, dict):
                    continue
                kind = str(ev.get("kind") or "ui")[:24]
                what = str(ev.get("what") or "")[:140]
                try:
                    ms = int(round(float(ev.get("ms") or 0)))
                except (TypeError, ValueError):
                    ms = 0
                detail = str(ev.get("detail") or "")[:140]
                _log_slow(f"client {kind} {what} ms={ms}" + (f" {detail}" if detail else ""))
            return self._json({"ok": True})
        if path == "/api/chats-manage":                   # harness_chats MCP tool from Claude Code / Codex (servers/agents.py)
            if self.headers.get("X-KC-Agent-Token") != subagents.TOKEN:
                return self._json({"error": "Not allowed."}, 403)
            cid = b.get("chat") or ""
            ch = store.Chat(cid) if cid else None
            em = (lambda ev: agent.emit(cid, ev)) if cid else None
            return self._json(sections.ai_manage(b.get("args") or {}, chat=ch, emit=em))
        if path.startswith("/api/sections/"):
            return sections.http_post(self, path, b)
        if path.startswith("/api/computer/"):             # Computer tool: answers, Stop, settings; calls from servers/computer.py
            return self._json(computer.http_post(path, self.headers, b))
        if path == "/api/subagents/presets":
            p = subagents.save_preset(b.get("name"), b.get("model"), b.get("instructions") or "",
                                      b.get("effort") or "", pid=b.get("id"))
            return self._json({"ok": True, "preset": p, "presets": subagents.presets(), "config": public_config()})
        if path == "/api/subagents/presets/delete":
            ok = subagents.delete_preset(b.get("id") or b.get("name"))
            return self._json({"ok": ok, "presets": subagents.presets(), "config": public_config()})
        if path.startswith("/api/subagent"):              # harness_agent calls from Claude Code / Codex (servers/agents.py)
            res = subagents.http(path, self.headers, b)
            try:
                return self._json(res)
            except OSError:
                if path.rstrip("/").endswith("/api/subagent"):
                    subagents.undelivered(b, res)         # the engine that asked is gone: the report goes in as a message
                raise
        if path.startswith("/api/groups/"):
            return groups.http_post(self, path, b)
        if path == "/api/chats":
            project = b.get("project") or str(config.WORKSPACE)
            store.add_project(project)
            group = b.get("group") if b.get("group") and groups.get(b.get("group")) else None
            if b.get("mode") == "group":
                from . import group_chat
                gcfg = b.get("group_chat") if isinstance(b.get("group_chat"), dict) else b
                room, meta = group_chat.create_room(
                    project,
                    members=gcfg.get("members") or [],
                    title=b.get("title") or gcfg.get("title") or "",
                    subagent_mode=gcfg.get("subagent_mode") or group_chat.DEFAULT_SUBAGENT_MODE,
                    worker=gcfg.get("worker"),
                    turn_budget_s=gcfg.get("turn_budget_s") or group_chat.DEFAULT_TURN_BUDGET_S,
                    max_rounds=gcfg.get("max_rounds") or group_chat.DEFAULT_MAX_ROUNDS,
                    max_total_s=gcfg.get("max_total_s") or group_chat.DEFAULT_MAX_TOTAL_S,
                    reasoning=b.get("reasoning"),
                    group=group,
                    section=b.get("section"),
                )
                return self._json({"id": room.id, "meta": agent._meta_public(meta)})
            chat = store.create(project, b.get("endpoint") or config.get()["endpoint"], reasoning=b.get("reasoning"), group=group)
            if b.get("mode") == "assistant":       # the personal assistant: short prompt, linked tool servers, no file tools
                chat.update_meta(mode="assistant")
            if isinstance(b.get("effort"), dict) and b["effort"]:             # a native level picked before the chat existed
                chat.update_meta(effort={str(k): str(v) for k, v in b["effort"].items()})
            origin = str(b.get("origin") or "").strip().lower()
            is_auto = bool(b.get("automated") or b.get("ephemeral") or origin in sections._AUTO_ORIGINS)
            if is_auto or origin:
                chat.update_meta(automated=is_auto, origin=origin or ("automation" if is_auto else ""))
            if b.get("section"):
                sections.assign_new_chat(chat.id, b.get("section"), group)
            if is_auto:
                sections.mark_automated(chat.id, origin or "automation")
            return self._json({"id": chat.id, "meta": agent._meta_public(chat.meta())})
        if path.startswith("/api/chats/"):
            parts = path.split("/")
            cid, act = parts[3], parts[4] if len(parts) > 4 else ""
            if act == "fork":
                res = chat_fork.fork_chat(
                    cid,
                    from_msg_id=b.get("from") or None,
                    title=b.get("title") or None,
                    endpoint=b.get("endpoint") or None,
                    section=b.get("section") or None,
                )
                return self._json({
                    "id": res["id"],
                    "meta": agent._meta_public(res["meta"]),
                    "messages_copied": res["messages_copied"],
                })
            if act == "subagents":
                return self._json(subagents.spawn_chat(
                    cid,
                    model=b.get("model"),
                    preset_id=b.get("preset_id") or b.get("preset"),
                    name=b.get("name") or "",
                    instructions=b.get("instructions") or "",
                    prompt=b.get("prompt") or "",
                    fork_context=b.get("fork_context", True),
                    effort=b.get("effort") or "",
                ))
            if act == "report-main":
                return self._json(subagents.report_to_main(
                    cid,
                    summary=b.get("summary") or "",
                    wake_main=bool(b.get("wake_main")),
                    source="user",
                ))
            chat = store.Chat(cid)
            if act == "group":
                from . import group_chat
                sub_act = parts[5] if len(parts) > 5 else (b.get("action") or "")
                action = str(b.get("action") or sub_act or "").strip().lower()
                if action == "pause":
                    return self._json(group_chat.pause(cid, immediate=bool(b.get("immediate"))))
                if action == "resume":
                    return self._json(group_chat.resume(cid))
                if action == "pass_baton":
                    return self._json(group_chat.pass_baton(cid, force=bool(b.get("force"))))
                if action == "config":
                    return self._json({"ok": True, "group": group_chat.update_room_config(cid, b)})
                if action == "stop":
                    return self._json(group_chat.stop(cid))
                raise ValueError(f"unknown group action: {action!r}")
            if act == "sync":
                return self._json(chat_import.update_imports("chat", cid).public())
            if act == "send":
                from . import group_chat
                if group_chat.is_group(chat):
                    return self._json(send_once(cid, b.get("key"), lambda: group_chat.send(
                        cid, b.get("text") or "", b.get("files") or [])))
                def go():
                    if b.get("report"):      # a task for a subagent chat: its answer is reported to its main chat
                        subagents.owe(cid)
                    return agent.start(cid, b.get("text") or "", b.get("files") or [], b.get("endpoint"), b.get("reasoning"))
                return self._json(send_once(cid, b.get("key"), go))
            if act == "stop":
                from . import group_chat
                if group_chat.is_group(chat):
                    return self._json(group_chat.stop(cid))
                return self._json(agent.stop(cid))
            if act == "unqueue":           # a message sent mid-turn, taken back before the model got it
                return self._json(agent.unqueue(cid, b.get("id") or ""))
            if act == "attach":            # a pasted image or uploaded file: kept with the chat (attachments/)
                if b.get("any_file"):
                    import base64
                    raw = base64.b64decode(b.get("data") or "")
                    p = vision.save_any_attachment(chat.dir / "attachments", raw, b.get("mime") or "", b.get("name") or "")
                    return self._json({"path": str(p), "size": len(raw), "kind": "image" if vision.is_image(p) else "file"})
                return self._json({"path": str(vision.save_attachment(chat, b.get("data") or "", b.get("mime") or "", b.get("name") or ""))})
            if act == "compact":
                threading.Thread(target=agent.compact_now, args=(cid,), daemon=True).start()
                return self._json({"ok": True})
            if act == "meta":
                allowed = {k: b[k] for k in ("title", "endpoint", "reasoning", "pinned", "deleted") if k in b}
                if "title" in allowed:
                    allowed["user_renamed"] = True
                    sections.clear_tidy_title(cid)
                if isinstance(b.get("effort"), dict):                         # native levels: {connection id: level}
                    allowed["effort"] = {**(chat.meta().get("effort") or {}), **{str(k): str(v) for k, v in b["effort"].items()}}
                updated_meta = chat.update_meta(**allowed)
                return self._json({"meta": agent._meta_public(updated_meta)})
            if act == "notes":
                chat.set_notes(b.get("text") or "")
                return self._json({"ok": True})
            if act == "truncate":          # edit and resend: drop the chosen message and everything after it
                mid = b.get("from")
                msgs = chat.messages()
                idx = next((i for i, m in enumerate(msgs) if m.get("id") == mid), None)
                if idx is None or agent.busy(cid):
                    raise ValueError("cannot do that now")
                chat.rewrite(msgs[:idx])
                chat.update_meta(ctx={})
                return self._json({"ok": True})
        if path == "/api/files/write":
            p = safe_path(b.get("path"))
            if b.get("mtime") and p.exists() and abs(p.stat().st_mtime - float(b["mtime"])) > 0.001 and not b.get("force"):
                return self._json({"error": "changed on disk since you opened it", "conflict": True, "mtime": p.stat().st_mtime}, 409)
            text = b.get("content") or ""
            if b.get("crlf"):
                text = text.replace("\r\n", "\n").replace("\n", "\r\n")
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            agent.hub.publish({"type": "file", "path": str(p), "change": "saved"})
            return self._json({"ok": True, "mtime": p.stat().st_mtime})
        if path == "/api/files/new":
            p = safe_path(b.get("path"))
            if p.exists():
                raise ValueError("it already exists")
            if b.get("dir"):
                p.mkdir(parents=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("", encoding="utf-8")
            return self._json({"ok": True})
        if path == "/api/files/rename":
            src, dst = safe_path(b.get("path")), safe_path(b.get("to"))
            if dst.exists():
                raise ValueError("the new name is taken")
            src.rename(dst)
            return self._json({"ok": True})
        if path == "/api/files/delete":
            p = safe_path(b.get("path"))
            trash = config.DATA / "trash" / time.strftime("%Y%m%d-%H%M%S")
            trash.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(trash / p.name))
            return self._json({"ok": True, "trash": str(trash)})
        if path == "/api/files/upload":
            p = safe_path(b.get("path"))
            import base64
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(base64.b64decode(b.get("data") or ""))
            return self._json({"ok": True, "path": str(p)})
        if path == "/api/projects":
            if b.get("remove"):
                config.update(lambda c: c.update(projects=[p for p in c.get("projects", []) if os.path.normcase(p["path"]) != os.path.normcase(b["remove"])]))
                return self._json({"projects": store.projects()})
            where = b.get("path") or str(config.WORKSPACE / (b.get("name") or "project"))
            store.add_project(where, b.get("name"))
            return self._json({"projects": store.projects(), "path": str(Path(where).resolve())})
        if path == "/api/project-notes":
            p = store.project_notes_path(b.get("project") or "")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(b.get("text") or "", encoding="utf-8")
            return self._json({"ok": True})
        if path == "/api/config":
            def fn(c):
                for k in ("endpoint", "reasoning", "compact_at", "project_files"):
                    if k in b:
                        c[k] = b[k]
                if b.get("dictation_engine") in ("", "whisper", "browser", "off"):
                    c["dictation_engine"] = b["dictation_engine"]
                if "updates_auto" in b:
                    c.setdefault("updates", {})["auto"] = bool(b["updates_auto"])
                if isinstance(b.get("runtime"), dict):               # the launcher's own settings and choice, nothing else
                    rt = c.setdefault("runtime", {})
                    if b["runtime"].get("choice"):
                        rt["choice"] = str(b["runtime"]["choice"])
                    if isinstance(b["runtime"].get("settings"), dict):
                        known = rt.get("settings") or {}
                        rt["settings"] = {**known, **{k: v for k, v in b["runtime"]["settings"].items() if k in known}}
                if isinstance(b.get("subagents"), dict):              # the Agent tool's switches
                    sa = dict(c.get("subagents") or {})
                    for k, v in b["subagents"].items():
                        if k == "presets":
                            sa["presets"] = subagents._clean_presets(v)
                        elif k in subagents.DEFAULTS:
                            sa[k] = v
                    c["subagents"] = sa
                for name, on in (b.get("mcp_enabled") or {}).items():
                    c.setdefault("mcp", {}).setdefault(name, {})["enabled"] = bool(on)
            config.update(fn)
            return self._json({"config": public_config()})
        if path == "/api/endpoints":
            e = {k: v for k, v in (b.get("endpoint") or {}).items() if k not in HIDDEN and k not in ("has_key", "key_source",
                                                                                                    "from_profile", "runtime", "detected",
                                                                                                    "levels", "effort_default", "effort_from")}
            key = e.pop("key", None)
            if not e.get("id"):
                base = re.sub(r"[^a-z0-9]+", "-", (e.get("name") or "model").lower()).strip("-")[:24] or "model"
                have = {x["id"] for x in config.get()["endpoints"]}
                eid, n = base, 1
                while eid in have:
                    n += 1
                    eid = f"{base}-{n}"
                e["id"] = eid
            if key:
                config.save_key(e["id"], key)                         # keys/<id>, never into config.json
            preset = next((p for p in presets.PRESETS if p["preset"] == e.pop("preset", None)), None)
            if preset:                                                # the preset's extras (efforts, level models, key_env)
                e = {**{k: v for k, v in preset.items() if k not in ("preset", "group", "help", "name")}, **e}

            def fn(c):
                lst = c["endpoints"]
                for i, x in enumerate(lst):
                    if x["id"] == e["id"]:
                        lst[i] = {**x, **e}
                        break
                else:
                    lst.append({"kind": "openai", "context": 128_000, "max_output": 16_384, **e})
                if not c.get("endpoint"):
                    c["endpoint"] = e["id"]
            config.update(fn)
            return self._json({"config": public_config(), "id": e["id"]})
        if path == "/api/endpoints/delete":
            eid = b.get("id")
            config.update(lambda c: c.update(endpoints=[x for x in c["endpoints"] if x["id"] != eid],
                                              endpoint=c["endpoint"] if c["endpoint"] != eid else ""))
            if not config.in_profile(eid):
                config.delete_key(eid)
            config.load()                                             # the default model moves to the next one
            return self._json({"config": public_config()})
        if path == "/api/runtime/start":
            return self._json(runtime.start(b.get("choice")))
        if path == "/api/runtime/stop":
            return self._json(runtime.stop())
        if path == "/api/runtime/switch":
            # stopping a running server is never a side effect: the caller must say so (a command-line client can)
            if runtime.status().get("state") in ("ready", "starting", "warming") and not b.get("stop_running"):
                return self._json({"error": "It is running: press Stop first, then pick the other one."}, 409)
            return self._json(runtime.switch(b.get("choice")))
        if path == "/api/updates/apply":
            return self._json(selfupdate.apply() if b.get("id") == "harness" else updates.update(b.get("id")))
        if path == "/api/updates/rollback":
            return self._json(selfupdate.rollback())
        if path == "/api/restart":        # running turns carry on in the new copy (resume.py): nothing to ask first
            return self._json({**selfupdate.restart(), "carrying_on": len(agent.busy_chats())})
        if path == "/api/imports":
            return self._json({"items": imports.apply(b.get("selected") or [])})
        if path == "/api/meetings/start":
            return self._json(meetings.start())
        if path == "/api/burn/dismiss":
            return self._json({"ok": True, "alerts": burn_monitor.dismiss(b.get("id"), all_alerts=bool(b.get("all")))})
        if path == "/api/screen/capture":
            is_rem, _ = remote_ok(self.headers)
            if is_rem:
                return self._json({"error": "Screen capture is only available on the local desktop."}, 403)
            return self._json(desktop.capture_screen(b.get("chat") or "", b.get("display", 0)))
        self._json({"error": "unknown"}, 404)

    def _events(self):
        q = agent.hub.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_rt = None
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=5)
                    self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n").encode("utf-8"))
                    # drain a burst in one write
                    for _ in range(200):
                        try:
                            ev = q.get_nowait()
                        except queue.Empty:
                            break
                        self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    rt = runtime.status()
                    sig = (rt.get("state"), rt.get("detail"), rt.get("up_min"))     # no launcher: only enabled + state
                    if sig != last_rt:
                        last_rt = sig
                        self.wfile.write(("data: " + json.dumps({"type": "runtime", "runtime": rt}) + "\n\n").encode("utf-8"))
                    else:
                        # Named ping event lets clients detect zombie/frozen SSE sockets (e.g. Android WebView sleep)
                        # while remaining ignored by clients that only listen to default onmessage.
                        self.wfile.write(b"event: ping\ndata: {}\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            agent.hub.unsubscribe(q)


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    config.load()
    meetings.status_cached()             # warm the probe caches so the first window does not wait
    runtime.status()
    providers.warm_statuses()            # warm model picker statuses in background so first tap is instant
    for p in store.projects():
        preview.register(p["path"])
    preview.register(config.WORKSPACE)
    preview.serve()
    srv = preview.Server(("127.0.0.1", config.PORT), Handler)
    updates.check_later()
    selfupdate.on_restart = srv.shutdown        # Settings > Updates > Restart: stop serving, a new copy takes over
    selfupdate.check_later()
    chat_import.auto_later()                    # Settings > Import: update imported chats on start / hourly (off by default)
    carried = agent.resume_turns()              # turns the last copy on this port left running carry on (resume.py)
    try:                                        # subagents that were mid-task for a main chat carry on too
        carried = list(carried or []) + list(subagents.resume_owed())
    except Exception:
        pass
    print(f"Flock on http://127.0.0.1:{config.PORT}" + (f" (carrying on {len(carried)} turn(s))" if carried else ""),
          flush=True)
    try:
        srv.serve_forever()
    finally:
        from . import mcp
        if selfupdate.restarting:               # running turns stop writing; their records stay for the new copy, and
            resume.leaving = True               # their commands and background jobs keep running through the restart
        mcp.stop_all()
        if not selfupdate.restarting:
            tools.stop_jobs()
        dictation.stop()
        if selfupdate.restarting:
            srv.server_close()
            os._exit(0)                         # the new copy waits for this process to end
