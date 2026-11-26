"""A small MCP client (stdio transport: one JSON-RPC message per line). Enough for tools: initialize, tools/list,
tools/call. Servers start on first use, hidden, and restart if they die."""
import json
import os
import subprocess
import threading
import time

from pathlib import Path

from . import config, osutil

NOWIN = osutil.NOWIN

PLAYWRIGHT_MCP = "0.0.83"        # the version the Browser tool's argument mapping was written and tested against


def builtin_specs():
    """The two servers that come with the harness, when they are installed (install.py, or paths in the profile):
    web = harness/servers/search.py in a Python that has its packages; browser = Playwright MCP, headless and isolated."""
    cfg = config.get()
    out = {}
    py = config.expand(cfg.get("search_python") or osutil.venv_python(config.TOOLS / "search-venv"))
    if py.exists():
        out["web"] = {"command": str(py), "args": [str(config.APP / "harness" / "servers" / "search.py")],
                      "env": {"KCH_CACHE": str(config.DATA / "cache")}, "builtin": True}
    cli = config.expand(cfg.get("playwright_mcp") or config.TOOLS / "node_modules" / "@playwright" / "mcp" / "cli.js")
    node = osutil.which("node")
    if node and cli.exists():
        out["browser"] = {"command": node, "args": [str(cli), "--headless", "--isolated", "--viewport-size", "1280x800",
                                                    "--browser", cfg.get("browser_channel") or osutil.browser_channel(),
                                                    "--timeout-navigation", "25000",
                                                    "--init-script", str(config.APP / "harness" / "browser_init.js"),
                                                    "--idle-timeout", "600000", "--output-dir", str(config.DATA / "browser")],
                          "builtin": True}
    return out


def specs():
    """Every MCP server: the built-in ones that are installed, then the profile's and the app's own (same name = changes it)."""
    out = builtin_specs()
    for name, s in (config.get().get("mcp") or {}).items():
        if isinstance(s, dict):
            out[name] = {**out.get(name, {}), **s}
    return {k: v for k, v in out.items() if v.get("command")}


def available(name):
    s = specs().get(name)
    return bool(s and s.get("enabled", True))


class MCPError(Exception):
    pass


class Server:
    def __init__(self, name, spec):
        self.name = name
        self.spec = spec
        self.proc = None
        self.lock = threading.Lock()
        self.pending = {}
        self.next_id = 1
        self.tools = []
        self.err_tail = []
        self.started = 0

    def _spawn(self):
        cmd = [self.spec["command"], *self.spec.get("args", [])]
        env = {**os.environ, **(self.spec.get("env") or {}), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     cwd=self.spec.get("cwd") or str(config.DATA), env=env, **osutil.group())
        self.started = time.time()
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._stderr, args=(self.proc,), daemon=True).start()
        self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "flock", "version": "1.0"}}, timeout=60)
        self._notify("notifications/initialized", {})
        res = self._request("tools/list", {}, timeout=60)
        self.tools = res.get("tools") or []

    def _reader(self, proc):
        for raw in proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                slot = self.pending.pop(msg["id"], None)
                if slot:
                    slot["msg"] = msg
                    slot["ev"].set()
            elif msg.get("method") and "id" in msg:      # a request from the server (roots, sampling): answer empty
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        for slot in list(self.pending.values()):
            slot["ev"].set()

    def _stderr(self, proc):
        for raw in proc.stderr:
            self.err_tail = (self.err_tail + [raw.decode("utf-8", "replace").rstrip()])[-30:]

    def _send(self, msg):
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method, params, timeout=120, cancel=None):
        rid = self.next_id
        self.next_id += 1
        slot = {"ev": threading.Event(), "msg": None}
        self.pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        t0 = time.time()
        while not slot["ev"].wait(0.25):
            if cancel is not None and cancel.is_set():
                self.pending.pop(rid, None)
                self._notify("notifications/cancelled", {"requestId": rid, "reason": "stopped"})
                raise MCPError("stopped")
            if time.time() - t0 > timeout:
                self.pending.pop(rid, None)
                raise MCPError(f"{self.name}: {method} took longer than {timeout} s")
            if self.proc.poll() is not None:
                break
        msg = slot["msg"]
        if msg is None:
            raise MCPError(f"{self.name} stopped: {' | '.join(self.err_tail[-5:])[:600]}")
        if msg.get("error"):
            raise MCPError(f"{self.name}: {msg['error'].get('message')}")
        return msg.get("result") or {}

    def ensure(self):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self._spawn()

    def call(self, tool, args, timeout=180, cancel=None):
        self.ensure()
        return self._request("tools/call", {"name": tool, "arguments": args}, timeout=timeout, cancel=cancel)

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                osutil.kill_tree(self.proc.pid, timeout=15)
            except Exception:
                self.proc.kill()


_servers = {}
_guard = threading.Lock()


def server(name):
    spec = specs().get(name) or resolve_imported(name)
    if not spec or not spec.get("enabled", True):
        hint = " (run install.py to set up web search and the browser)" if name in ("web", "browser") else ""
        raise MCPError(f"MCP server {name} is not set up or is turned off{hint}")
    with _guard:
        s = _servers.get(name)
        if s is None or s.spec != spec:
            if s:
                s.stop()
            s = _servers[name] = Server(name, spec)
        return s


def restart(name):
    """Kill a server (and its child processes, e.g. a hung Chromium); the next call starts a fresh one."""
    with _guard:
        s = _servers.pop(name, None)
    if s:
        s.stop()


def text_of(result):
    """Join an MCP tool result's text parts; images come back separately as (mime, base64)."""
    texts, images = [], []
    for part in result.get("content") or []:
        if part.get("type") == "text":
            texts.append(part.get("text", ""))
        elif part.get("type") == "image":
            images.append((part.get("mimeType", "image/png"), part.get("data", "")))
        elif part.get("type") == "resource":
            res = part.get("resource") or {}
            texts.append(res.get("text") or f"[resource {res.get('uri')}]")
    return "\n".join(texts), images, bool(result.get("isError"))


def resolve_imported(name):
    """An MCP server imported from Claude Code or Codex is read from that tool's own config each time, so its keys
    (if any) stay where they are and are never copied."""
    from . import imports
    for item in config.get().get("imported_mcp", []):
        if item.get("name") == name and item.get("enabled", True):
            spec = imports.mcp_spec(item.get("source"), item.get("ref") or name)
            if spec:
                return {**spec, "enabled": True}
    return None


def stop_all():
    with _guard:
        for s in _servers.values():
            s.stop()
        _servers.clear()
