"""The model picked in the harness, offered to other local apps (Meetings) as an OpenAI-compatible endpoint, so they can
use it without ever holding its keys:

  GET  /api/llm/v1/models             the connections, which one is selected, their context sizes
  POST /api/llm/v1/chat/completions   {"model": "<connection id>" | "selected", "messages": [...], "stream": true,
                                       "max_tokens": n, "reasoning_effort": "none|low|medium|high|max",
                                       "kc_wait": seconds to wait for a model server that is starting (default 0),
                                       "kc_session": a name that keeps one conversation on one provider cache}

127.0.0.1 only, and every request needs `Authorization: Bearer <token>`: a per-install token kept in the harness's key
folder (fenced off from the agent's tools), handed to Meetings when the harness starts it or finds it running
(meetings.py). Browsers are refused outright (any Origin header), so a web page can never use it.

The request goes to the connection through providers.stream_chat (keys, failover, retries as in a chat), with no tools.
A connection whose server the harness launches (e.g. a local or remote GPU server) is never started from here: starting it is the
user's decision in the model menu. When it is starting, the request waits up to kc_wait seconds; when it is off, the
answer is at once a 503 that says so. Claude Code and Codex connections are CLIs with their own tools and sessions, not
chat APIs: they are listed (Meetings runs Claude Code itself) but not served.
"""
import hmac
import json
import os
import secrets
import threading
import time

from . import config, providers
from . import runtime as runtime_mod

TOKEN_FILE = ".local-model-token"          # in config.KEYS
_lock = threading.Lock()
_token = []
CLI_KINDS = ("claude-code", "codex")


def token():
    """This install's token for local apps, made on first use."""
    path = config.KEYS / TOKEN_FILE
    with _lock:
        if _token:
            return _token[0]
        try:
            got = path.read_text(encoding="utf-8").strip()
            if len(got) >= 32:
                _token.append(got)
                return got
        except OSError:
            pass
        config.KEYS.mkdir(parents=True, exist_ok=True)
        got = secrets.token_urlsafe(32)
        path.write_text(got + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _token.append(got)
        return got


def fingerprint():
    """Says whether another app holds the current token, without sending it."""
    import hashlib
    return hashlib.sha256(token().encode()).hexdigest()[:16]


def base_url():
    return f"http://127.0.0.1:{config.PORT}"


def describe(ep):
    kind = ep.get("kind") or "openai"
    return {"id": ep["id"], "name": ep.get("name") or ep["id"], "kind": kind, "model": ep.get("model") or "",
            "context": int(ep.get("context") or 128_000), "max_output": int(ep.get("max_output") or 16384),
            "served": kind not in CLI_KINDS, "runtime": bool(runtime_mod.serves(ep["id"]))}


def models():
    cfg = config.get()
    return {"object": "list", "selected": cfg.get("endpoint"), "data": [describe(e) for e in cfg["endpoints"]]}


def _level(effort):
    """OpenAI-style reasoning_effort to the harness's own levels."""
    effort = str(effort or "").lower()
    if effort in ("high", "medium"):
        return "think"
    if effort in ("max", "xhigh"):
        return "deep"
    return "fast"


class Refused(Exception):
    def __init__(self, status, kind, message):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


def _ready(ep, wait):
    """A launcher-backed connection: answering, or wait a little for one that is starting. Never starts it."""
    if not runtime_mod.serves(ep["id"]):
        return
    from .runtime import runtime
    if providers.health(ep, timeout=3).get("ok"):
        return
    deadline = time.time() + max(0.0, min(float(wait or 0), 120.0))
    while True:
        st = runtime.status()
        label = st.get("label") or "model server"
        if st["state"] in ("off", "failed", "stopping", "external", "orphaned"):
            why = f" ({st['detail']})" if st["state"] == "failed" and st.get("detail") else ""
            raise Refused(503, "unavailable", f"{ep['name']} is off: its {label} is not running{why}. Start it from the "
                                              "model menu in Flock, or use another model for now.")
        if time.time() >= deadline:
            detail = st.get("detail") or st["state"]
            raise Refused(503, "starting", f"{ep['name']} is starting ({detail}). It can take a few minutes.")
        time.sleep(3)
        if providers.health(ep, timeout=3).get("ok"):
            return


def _authorised(handler):
    if handler.headers.get("Origin"):
        return False                                   # browsers: never
    got = handler.headers.get("Authorization") or ""
    return got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip().encode(), token().encode())


def _error(handler, status, kind, message):
    handler._json({"error": {"type": kind, "message": message}}, status)


def serve(handler, method, path):
    """Called by server.py for /api/llm/... after its Host check."""
    if not _authorised(handler):
        return _error(handler, 401, "auth", "Flock did not accept this app's token.")
    if method == "GET" and path == "/api/llm/v1/models":
        return handler._json(models())
    if method != "POST" or path != "/api/llm/v1/chat/completions":
        return _error(handler, 404, "not_found", "Not found.")
    try:
        body = handler._jbody()
    except ValueError:
        return _error(handler, 400, "bad_request", "Expected JSON.")
    eid = body.get("model")
    if not eid or eid in ("selected", "default"):
        eid = config.get().get("endpoint")
    eps = {e["id"]: e for e in config.get()["endpoints"]}
    ep = eps.get(eid)
    if not ep:
        return _error(handler, 404, "unavailable", f"Flock has no connection called {eid!r} any more. Pick a model there again.")
    if ep.get("kind") in CLI_KINDS:
        what = "the Claude Code CLI" if ep["kind"] == "claude-code" else "the Codex CLI"
        return _error(handler, 400, "unsupported", f"{ep.get('name')} runs through {what}, which this app cannot use through "
                                                   "Flock. Pick another model there, or use one of this app's own.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error(handler, 400, "bad_request", "messages is required.")
    try:
        _ready(ep, body.get("kc_wait"))
    except Refused as r:
        return _error(handler, r.status, r.kind, r.message)
    level = _level(body.get("reasoning_effort"))
    max_tokens = body.get("max_tokens") or body.get("max_completion_tokens")
    max_tokens = min(int(max_tokens), int(ep.get("max_output") or 32768)) if max_tokens else None
    session = str(body.get("kc_session") or "kc-local")[:80]
    cancel = threading.Event()
    stream = bool(body.get("stream"))
    created = int(time.time())
    rid = "chatcmpl-" + secrets.token_hex(8)

    def chunk(delta=None, finish=None, usage=None):
        out = {"id": rid, "object": "chat.completion.chunk", "created": created, "model": ep["id"],
               "choices": [] if usage is not None and delta is None else [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
        if usage is not None:
            out["usage"] = usage
        return out

    if not stream:
        try:
            res = providers.stream_chat(ep, messages, None, level, lambda *a: None, cancel, session=session, max_tokens=max_tokens)
        except providers.ModelError as e:
            return _error(handler, _status(e), _kind(e), str(e))
        return handler._json({"id": rid, "object": "chat.completion", "created": created, "model": ep["id"],
                              "choices": [{"index": 0, "finish_reason": res.finish or "stop",
                                           "message": {"role": "assistant", "content": res.text}}], "usage": res.usage or {}})

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.close_connection = True
    write_lock = threading.Lock()

    def send(obj):
        data = obj if isinstance(obj, bytes) else ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")
        with write_lock:
            try:
                handler.wfile.write(data)
                handler.wfile.flush()
            except (OSError, ValueError):
                cancel.set()                       # the app hung up (its Stop): stop the model too

    def on_event(kind, data):
        if kind == "text":
            send(chunk({"content": data}))
        elif kind == "reasoning":
            send(chunk({"reasoning_content": data}))

    # Comments while a slow model reads a long prompt, so a hung-up client is noticed and the model stopped.
    done = threading.Event()

    def keepalive():
        while not done.wait(10):
            send(b": waiting\n\n")
    threading.Thread(target=keepalive, daemon=True).start()
    try:
        send(chunk({"role": "assistant", "content": ""}))
        res = providers.stream_chat(ep, messages, None, level, on_event, cancel, session=session, max_tokens=max_tokens)
        send(chunk({}, finish=res.finish or "stop"))
        if res.usage:
            send(chunk(usage=res.usage))
    except providers.ModelError as e:
        if e.kind != "cancelled":
            send({"error": {"type": _kind(e), "message": str(e)[:600]}})
    finally:
        done.set()
    send(b"data: [DONE]\n\n")


def _kind(e):
    return {"offline": "unavailable", "auth": "auth_upstream", "quota": "limit", "overflow": "overflow",
            "busy": "busy"}.get(e.kind, "error")


def _status(e):
    return {"offline": 503, "quota": 429, "overflow": 400, "busy": 503}.get(e.kind, 502)
