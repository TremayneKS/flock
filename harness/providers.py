"""Talking to models. Everything that speaks the OpenAI chat API goes through stream_chat(): llama.cpp's llama-server,
vLLM, LM Studio, Ollama, OpenCode Go, DeepSeek, OpenRouter, Gemini, OpenAI, or any other local or remote endpoint.

Streaming SSE is parsed by hand (http.client), so a Stop closes the socket at once, and llama.cpp's extras (timings:
tokens read from cache vs processed, decode speed, draft acceptance) come through untouched.
"""
import http.client
import json
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

from . import config

UA = "Flock/1.0"                                  # some CDNs refuse the default Python user agent


class ModelError(Exception):
    def __init__(self, msg, kind="error", status=None):
        super().__init__(msg)
        self.kind = kind            # error | overflow | auth | quota | offline | cancelled
        self.status = status


class Result:
    def __init__(self):
        self.text = ""
        self.reasoning = ""
        self.tool_calls = []        # [{"id", "name", "arguments" (raw string)}]
        self.finish = None
        self.usage = {}
        self.timings = {}
        self.t_first = None
        self.t_end = None
        self.model = None


def _classify(status, body):
    low = (body or "").lower()
    if "overloaded" in low or "retry shortly" in low or "temporarily" in low or (status or 0) >= 500:
        return "busy"
    if status in (401, 403):
        return "auth"
    if status in (402, 429) or "usagelimit" in low or "rate limit" in low:
        return "quota"
    if "context" in low and ("exceed" in low or "too long" in low or "maximum" in low or "size" in low):
        return "overflow"
    return "error"


def _headers(ep, key, session):
    h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        h["Authorization"] = "Bearer " + key
    h["User-Agent"] = UA
    if ep.get("kind") == "opencode-go":
        h["x-opencode-session"] = session or "flock"      # keeps one chat on one cache
    for k, v in (ep.get("headers") or {}).items():             # e.g. OpenRouter's HTTP-Referer / X-Title
        h[str(k)] = str(v)
    return h


def _keys(ep):
    """(base URL, [keys]): several keys fail over in order when one is refused or out of quota."""
    default_base = ("https://opencode.ai/zen/go/v1" if ep.get("kind") == "opencode-go"
                    else "https://api.anthropic.com/v1" if ep.get("kind") == "anthropic" else "")
    base = (ep.get("base") or default_base).rstrip("/")
    if not base:
        raise ModelError(f"{ep.get('name')} has no base URL", "error")
    keys = config.read_keys(ep)
    if not keys and (ep.get("kind") in ("opencode-go", "anthropic") or not _local(base)):
        raise ModelError(f"{ep.get('name')} needs an API key: add it in Settings > Connections", "auth")
    return base, keys or [""]


def _local(base):
    host = urllib.parse.urlsplit(base).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1") or host.endswith(".local") or host.startswith(("192.168.", "10."))


def build_body(ep, messages, tools, reasoning, max_tokens=None):
    model = (ep.get("level_models") or {}).get(reasoning) or ep.get("model") or "default"
    body = {"model": model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = True
    body["max_tokens"] = max_tokens or ep.get("max_output") or 16384
    if ep.get("no_thinking"):
        for k, v in (ep.get("sampling") or {}).items():
            body.setdefault(k, v)
    elif ep.get("llama"):
        body.update(ep.get("sampling") or {})
        body["cache_prompt"] = True
        # Qwen3.5: thinking on/off per request through the chat template. A connection's template_efforts also passes
        # the template its own effort per level (Qwen3.8: {"think": "medium", "deep": "xhigh"})
        body["chat_template_kwargs"] = {"enable_thinking": reasoning != "fast"}
        if (ep.get("template_efforts") or {}).get(reasoning):
            body["chat_template_kwargs"]["reasoning_effort"] = ep["template_efforts"][reasoning]
    elif ep.get("kind") == "opencode-go":
        # DeepSeek: thinking off, or on at an effort; a connection's `efforts` may change the mapping ("off" = no thinking)
        want = (ep.get("efforts") or {}).get(reasoning) or {"fast": "off", "deep": "max"}.get(reasoning, "high")
        if want in ("off", "none"):
            body["thinking"] = {"type": "disabled"}
        else:
            body["reasoning_effort"] = want
    else:
        for k, v in (ep.get("sampling") or {}).items():
            body.setdefault(k, v)
        if (ep.get("efforts") or {}).get(reasoning):          # per-endpoint map, e.g. Gemini low / medium / high
            body["reasoning_effort"] = ep["efforts"][reasoning]
        elif ep.get("reasoning_param") and reasoning != "fast":
            body["reasoning_effort"] = "high" if reasoning == "think" else "max"
    return body


def _anthropic_blocks(content):
    """Convert OpenAI-style message content (str or list of text/image_url parts) to Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text" and p.get("text"):
            out.append({"type": "text", "text": str(p["text"])})
        elif t == "image_url":
            url = (p.get("image_url") or {}).get("url") or ""
            if url.startswith("data:") and ";base64," in url:
                head, b64 = url.split(";base64,", 1)
                mime = head[5:] or "image/png"
                out.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
    return out


def build_anthropic_body(ep, messages, tools, reasoning, max_tokens=None):
    """Build a native Anthropic Messages API (/v1/messages) streaming request from OpenAI-shaped messages & tools."""
    model = (ep.get("level_models") or {}).get(reasoning) or ep.get("model") or "claude-sonnet-4-5"
    sys_parts = []
    conv = []
    for m in messages or []:
        role = m.get("role")
        if role == "system":
            txt = m.get("content") if isinstance(m.get("content"), str) else "\n".join(
                str(p.get("text") or "") for p in (m.get("content") or []) if isinstance(p, dict))
            if txt.strip():
                sys_parts.append(txt.strip())
            continue
        if role == "tool":
            blk = {"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "call_0",
                   "content": str(m.get("content") or "")}
            if conv and conv[-1]["role"] == "user":
                conv[-1]["content"].append(blk)
            else:
                conv.append({"role": "user", "content": [blk]})
            continue
        if role == "assistant":
            blks = _anthropic_blocks(m.get("content"))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments") if fn else tc.get("arguments")
                try:
                    inp = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args if isinstance(raw_args, dict) else {})
                except ValueError:
                    inp = {}
                blks.append({"type": "tool_use", "id": tc.get("id") or "call_0",
                             "name": (fn.get("name") if fn else tc.get("name")) or "tool", "input": inp})
            if not blks:
                continue
            if conv and conv[-1]["role"] == "assistant":
                conv[-1]["content"].extend(blks)
            else:
                conv.append({"role": "assistant", "content": blks})
            continue
        if role == "user":
            blks = _anthropic_blocks(m.get("content"))
            if not blks:
                blks = [{"type": "text", "text": " "}]
            if conv and conv[-1]["role"] == "user":
                conv[-1]["content"].extend(blks)
            else:
                conv.append({"role": "user", "content": blks})
    if not conv:
        conv = [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]
    # Add an ephemeral cache breakpoint near the end of history so multi-step turns reuse the prefix cache
    for msg in reversed(conv[:-1]):
        if msg["content"] and isinstance(msg["content"][-1], dict):
            msg["content"][-1]["cache_control"] = {"type": "ephemeral"}
            break
    out_max = int(max_tokens or ep.get("max_output") or 16384)
    body = {"model": model, "messages": conv, "max_tokens": out_max, "stream": True}
    if sys_parts:
        body["system"] = [{"type": "text", "text": "\n\n".join(sys_parts), "cache_control": {"type": "ephemeral"}}]
    if tools:
        atools = []
        for t in tools:
            fn = t.get("function") or {}
            atools.append({"name": fn.get("name") or t.get("name") or "tool",
                           "description": fn.get("description") or "",
                           "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
        if atools:
            atools[-1]["cache_control"] = {"type": "ephemeral"}
            body["tools"] = atools
    if not ep.get("no_thinking") and reasoning in ("think", "deep"):
        budget = 10_000 if reasoning == "think" else 20_000
        if body["max_tokens"] <= budget + 2048:
            body["max_tokens"] = budget + 4096
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
    else:
        for k, v in (ep.get("sampling") or {}).items():
            if k in ("temperature", "top_p", "top_k"):
                body.setdefault(k, v)
    return body


def _one_anthropic(ep, base, key, body, on_event, cancel, timeout):
    u = urllib.parse.urlsplit(base)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    path = (u.path.rstrip("/") or "") + "/messages"
    hdrs = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": UA,
            "x-api-key": key, "anthropic-version": "2023-06-01"}
    for k, v in (ep.get("headers") or {}).items():
        hdrs[str(k)] = str(v)
    done = threading.Event()

    def watch():
        while not done.is_set():
            if cancel is not None and cancel.wait(0.2):
                while not done.is_set():
                    sock = conn.sock
                    if sock:
                        _abort(sock)
                        return
                    done.wait(0.1)
                return
    threading.Thread(target=watch, daemon=True).start()
    res = Result()
    t0 = time.time()
    try:
        try:
            if cancel is not None and cancel.is_set():
                raise ModelError("stopped", "cancelled")
            conn.request("POST", path, body=body, headers=hdrs)
            resp = conn.getresponse()
        except (ConnectionRefusedError, socket.gaierror) as e:
            raise ModelError(f"{ep.get('name')} is not reachable at {base} ({e.__class__.__name__}).", "offline")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, http.client.RemoteDisconnected,
                ssl.SSLEOFError, ssl.SSLZeroReturnError) as e:
            if cancel is not None and cancel.is_set():
                raise ModelError("stopped", "cancelled")
            raise ModelError(f"{ep.get('name')}: the connection closed before the reply started ({e})", "dropped")
        except (OSError, http.client.HTTPException) as e:
            if cancel is not None and cancel.is_set():
                raise ModelError("stopped", "cancelled")
            raise ModelError(f"{ep.get('name')}: {e}", "offline")
        if resp.status != 200:
            txt = resp.read().decode("utf-8", "replace")
            if key:
                txt = txt.replace(key, "<key>")
            raise ModelError(f"{ep.get('name')} answered HTTP {resp.status}: {txt[:600]}", _classify(resp.status, txt), resp.status)
        calls = {}
        in_tok, cached_tok, created_tok, out_tok = 0, 0, 0, 0
        while True:
            try:
                line = resp.readline()
            except (OSError, http.client.HTTPException, ValueError):
                if cancel is not None and cancel.is_set():
                    raise ModelError("stopped", "cancelled")
                raise ModelError(f"{ep.get('name')}: the connection dropped mid-reply", "offline")
            if cancel is not None and cancel.is_set():
                raise ModelError("stopped", "cancelled")
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            typ = ev.get("type")
            if typ == "error":
                err = ev.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise ModelError(f"{ep.get('name')}: {msg}", _classify(None, msg))
            elif typ == "message_start":
                msg = ev.get("message") or {}
                if msg.get("model"):
                    res.model = msg["model"]
                u0 = msg.get("usage") or {}
                in_tok = int(u0.get("input_tokens") or 0)
                cached_tok = int(u0.get("cache_read_input_tokens") or 0)
                created_tok = int(u0.get("cache_creation_input_tokens") or 0)
                out_tok = int(u0.get("output_tokens") or 0)
            elif typ == "content_block_start":
                idx = ev.get("index", len(calls))
                cb = ev.get("content_block") or {}
                if cb.get("type") == "tool_use":
                    res.t_first = res.t_first or time.time()
                    calls[idx] = {"id": cb.get("id") or f"call_{int(t0)}_{idx}",
                                  "name": cb.get("name") or "", "arguments": ""}
                    on_event("tool", {"index": idx, "name": calls[idx]["name"], "args": ""})
            elif typ == "content_block_delta":
                idx = ev.get("index", 0)
                d = ev.get("delta") or {}
                dt = d.get("type")
                if dt == "text_delta" and d.get("text"):
                    res.t_first = res.t_first or time.time()
                    res.text += d["text"]
                    on_event("text", d["text"])
                elif dt == "thinking_delta" and d.get("thinking"):
                    res.t_first = res.t_first or time.time()
                    res.reasoning += d["thinking"]
                    on_event("reasoning", d["thinking"])
                elif dt == "input_json_delta" and d.get("partial_json"):
                    res.t_first = res.t_first or time.time()
                    c = calls.setdefault(idx, {"id": f"call_{int(t0)}_{idx}", "name": "", "arguments": ""})
                    c["arguments"] += d["partial_json"]
                    on_event("tool", {"index": idx, "name": c["name"], "args": c["arguments"]})
            elif typ == "message_delta":
                d = ev.get("delta") or {}
                if d.get("stop_reason"):
                    res.finish = "tool_calls" if d["stop_reason"] == "tool_use" else d["stop_reason"]
                u1 = ev.get("usage") or {}
                if u1.get("output_tokens") is not None:
                    out_tok = int(u1["output_tokens"])
        if cancel is not None and cancel.is_set():
            raise ModelError("stopped", "cancelled")
        res.tool_calls = [calls[i] for i in sorted(calls)]
        for c in res.tool_calls:
            if not c["arguments"].strip():
                c["arguments"] = "{}"
        res.usage = {
            "prompt_tokens": in_tok + created_tok + cached_tok,
            "completion_tokens": out_tok,
            "cache_creation_input_tokens": created_tok,
            "prompt_tokens_details": {"cached_tokens": cached_tok},
        }
        res.t_end = time.time()
        res.t_start = t0
        return res
    finally:
        done.set()
        try:
            conn.close()
        except Exception:
            pass


def stream_chat(ep, messages, tools, reasoning, on_event, cancel, session=None, max_tokens=None, timeout=None):
    """Run one completion. on_event(kind, data) gets ("text", str), ("reasoning", str), ("tool", {index, name, args}).
    Returns a Result. Raises ModelError. timeout: the longest silence (seconds) before giving up, for the first answer
    (prompt processing) and between chunks. None (the default) = no limit: a reply runs until finished or Stop."""
    timeout = float(timeout) if timeout else None
    base, keys = _keys(ep)
    is_anth = ep.get("kind") == "anthropic"
    payload = build_anthropic_body(ep, messages, tools, reasoning, max_tokens) if is_anth else build_body(ep, messages, tools, reasoning, max_tokens)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last = None
    for attempt in range(4):                            # "overloaded", 5xx and dropped connections are retried after a pause
        if attempt:
            on_event("retry", {"attempt": attempt, "why": str(last)[:200]})
            pause = (1, 3, 6) if last is not None and last.kind == "dropped" else (2, 6, 15)
            if cancel is not None and cancel.wait(pause[attempt - 1]):
                raise ModelError("stopped", "cancelled")
        for ki, key in enumerate(keys):
            try:
                res = (_one_anthropic(ep, base, key, body, on_event, cancel, timeout)
                       if is_anth else _one(ep, base, key, body, on_event, cancel, session, timeout))
                if ki:
                    config.key_used(ep, key)            # failed over: a profile's key plugin may prefer it next time
                return res
            except ModelError as e:
                last = e
                if e.kind in ("auth", "quota") and ki + 1 < len(keys):
                    continue
                if e.kind in ("busy", "dropped"):     # dropped: closed before any answer, so nothing was half-sent
                    break
                raise
        if last is None or last.kind not in ("busy", "dropped"):
            break
    raise last or ModelError("no key")


def _abort(sock):
    """Wake a read blocked on this socket in another thread. shutdown() does it on Linux and macOS; on Windows a socket
    with a timeout waits in select(), which neither shutdown() nor close() wakes (close only drops Python's reference
    while the response file still holds one), so the handle is detached and closed for real."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        socket.close(sock.detach())
    except OSError:
        pass


def _one(ep, base, key, body, on_event, cancel, session, timeout):
    u = urllib.parse.urlsplit(base)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    path = (u.path.rstrip("/") or "") + "/chat/completions"
    done = threading.Event()

    def watch():                                      # Stop: close the socket so a blocked read returns at once
        while not done.is_set():
            if cancel is not None and cancel.wait(0.2):
                while not done.is_set():              # Stop may come before the socket exists (still connecting)
                    sock = conn.sock
                    if sock:
                        _abort(sock)
                        return
                    done.wait(0.1)
                return
    threading.Thread(target=watch, daemon=True).start()
    res = Result()
    t0 = time.time()
    try:
        try:
            if cancel is not None and cancel.is_set():
                raise ModelError("stopped", "cancelled")
            conn.request("POST", path, body=body, headers=_headers(ep, key, session))
            resp = conn.getresponse()
        except (ConnectionRefusedError, socket.gaierror) as e:
            raise ModelError(f"{ep.get('name')} is not reachable at {base} ({e.__class__.__name__}). Is it running?", "offline")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, http.client.RemoteDisconnected,
                ssl.SSLEOFError, ssl.SSLZeroReturnError) as e:
            # (an SSL "unexpected EOF" is the same thing over https: some hosted APIs drop an occasional request before
            # answering, and a tunnelled local server can close early before the request reaches llama-server, so
            # sending it again is safe; stream_chat retries it.)
            if cancel is not None and cancel.is_set():
                raise ModelError("stopped", "cancelled")
            raise ModelError(f"{ep.get('name')}: the connection closed before the reply started ({e})", "dropped")
        except (OSError, http.client.HTTPException) as e:
            if cancel is not None and cancel.is_set():
                raise ModelError("stopped", "cancelled")
            if isinstance(e, socket.timeout):
                raise ModelError(f"{ep.get('name')} did not answer within {timeout:.0f} s. The server may be stuck or "
                                 "overloaded; try again, or restart it.", "offline")
            raise ModelError(f"{ep.get('name')}: {e}", "offline")
        if resp.status != 200:
            txt = resp.read().decode("utf-8", "replace")
            if key:
                txt = txt.replace(key, "<key>")
            raise ModelError(f"{ep.get('name')} answered HTTP {resp.status}: {txt[:600]}", _classify(resp.status, txt), resp.status)
        calls = {}
        while True:
            try:
                line = resp.readline()
            except (OSError, http.client.HTTPException, ValueError) as e:
                if cancel is not None and cancel.is_set():
                    raise ModelError("stopped", "cancelled")
                if isinstance(e, socket.timeout):
                    raise ModelError(f"{ep.get('name')} stopped sending for {timeout:.0f} s mid-reply", "offline")
                raise ModelError(f"{ep.get('name')}: the connection dropped mid-reply", "offline")
            if cancel is not None and cancel.is_set():
                raise ModelError("stopped", "cancelled")
            if not line:
                break
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            if ev.get("error"):
                err = ev["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise ModelError(f"{ep.get('name')}: {msg}", _classify(None, msg))
            if ev.get("model"):
                res.model = ev["model"]
            if ev.get("usage"):
                res.usage = ev["usage"]
            if ev.get("timings"):
                res.timings = ev["timings"]
            for ch in ev.get("choices") or []:
                d = ch.get("delta") or {}
                r = d.get("reasoning_content") or d.get("reasoning")
                if r:
                    res.t_first = res.t_first or time.time()
                    res.reasoning += r
                    on_event("reasoning", r)
                if d.get("content"):
                    res.t_first = res.t_first or time.time()
                    res.text += d["content"]
                    on_event("text", d["content"])
                for tc in d.get("tool_calls") or []:
                    res.t_first = res.t_first or time.time()
                    i = tc.get("index", len(calls))
                    c = calls.setdefault(i, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        c["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        c["name"] += fn["name"]
                    if fn.get("arguments"):
                        c["arguments"] += fn["arguments"]
                    if tc.get("extra_content"):     # Gemini 3: the thought signature, which must go back with the call
                        c["extra"] = {**(c.get("extra") or {}), **tc["extra_content"]}
                    on_event("tool", {"index": i, "name": c["name"], "args": c["arguments"]})
                if ch.get("finish_reason"):
                    res.finish = ch["finish_reason"]
        if cancel is not None and cancel.is_set():
            raise ModelError("stopped", "cancelled")
        res.tool_calls = [calls[i] for i in sorted(calls)]
        for n, c in enumerate(res.tool_calls):
            c["id"] = c["id"] or f"call_{int(t0)}_{n}"
        res.t_end = time.time()
        res.t_start = t0
        return res
    finally:
        done.set()
        try:
            conn.close()
        except Exception:
            pass


def health(ep, timeout=3):
    """Is the endpoint answering? For llama.cpp also returns the server's context size and build."""
    if ep.get("kind") == "claude-code":
        return {"ok": bool(config.claude_exe()), **({} if config.claude_exe() else {"error": "the claude CLI is not installed"})}
    if ep.get("kind") == "codex":
        return {"ok": bool(config.codex_exe()), **({} if config.codex_exe() else {"error": "the codex CLI is not installed"})}
    if ep.get("kind") == "agy":
        return {"ok": bool(config.agy_exe()), **({} if config.agy_exe() else {"error": "the agy CLI is not installed"})}
    try:
        base, keys = _keys(ep)
    except ModelError as e:
        return {"ok": False, "error": str(e)}
    if ep.get("kind") == "opencode-go":
        return {"ok": bool(keys), "remote": True}
    if ep.get("kind") == "anthropic":
        try:
            req = urllib.request.Request(base + "/models", headers={"x-api-key": keys[0] or "", "anthropic-version": "2023-06-01", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=max(timeout, 10)) as r:
                return {"ok": r.status == 200, "remote": True}
        except Exception as e:
            return {"ok": False, "error": getattr(e, "code", None) or e.__class__.__name__}
    u = urllib.parse.urlsplit(base)
    root = f"{u.scheme}://{u.netloc}"
    if not ep.get("llama"):           # any other OpenAI-style API (Gemini, vLLM, LM Studio...): the standard model list
        try:
            req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + (keys[0] or "")})
            with urllib.request.urlopen(req, timeout=max(timeout, 10)) as r:
                return {"ok": r.status == 200, "remote": u.hostname not in ("127.0.0.1", "localhost")}
        except Exception as e:
            return {"ok": False, "error": getattr(e, "code", None) or e.__class__.__name__}
    try:
        req = urllib.request.Request(root + "/health", headers={"Authorization": "Bearer " + (keys[0] or "")})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = r.status == 200
        out = {"ok": ok}
        if ep.get("llama") and ok:
            try:
                req = urllib.request.Request(root + "/props", headers={"Authorization": "Bearer " + (keys[0] or "")})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    props = json.loads(r.read())
                gs = props.get("default_generation_settings") or {}
                out["n_ctx"] = gs.get("n_ctx") or props.get("n_ctx")
                out["build"] = props.get("build_info")
            except Exception:
                pass
        return out
    except Exception as e:
        return {"ok": False, "error": e.__class__.__name__}


# ---- fast cached statuses for the model picker ("ready" | "signed out" | "offline")
_status_cache = {"t": 0.0, "data": {}}
_status_lock = threading.Lock()
_status_refreshing = False
_agy_auth_cache = {"t": 0.0, "ok": None}
STATUS_TTL = 60.0
AGY_AUTH_TTL = 300.0


def _nonempty(path):
    try:
        return path.is_file() and path.stat().st_size > 2
    except OSError:
        return False


def _agy_signed_in_fast():
    """Instant file-based hint for Antigravity CLI sign-in while background `agy models` refreshes."""
    if not config.agy_exe():
        return False
    if _agy_auth_cache["ok"] is not None:
        return _agy_auth_cache["ok"]
    return (config.HOME / ".gemini" / "antigravity-cli" / "installation_id").is_file()


def _agy_signed_in():
    """Fast cached check for Antigravity CLI sign-in without exposing any credentials."""
    now = time.time()
    if _agy_auth_cache["ok"] is not None and now - _agy_auth_cache["t"] < AGY_AUTH_TTL:
        return _agy_auth_cache["ok"]
    exe = config.agy_exe()
    if not exe:
        _agy_auth_cache.update(t=now, ok=False)
        return False
    # Presence of the Antigravity CLI state folder with installation/state indicates local setup;
    # confirm with a quick `agy models` only if needed (cached for 5 min).
    import subprocess
    from . import osutil
    try:
        r = subprocess.run([exe, "models"], capture_output=True, text=True, timeout=8, creationflags=osutil.NOWIN)
        txt = (r.stdout + "\n" + r.stderr).lower()
        bad = any(w in txt for w in ("sign in", "signed out", "login required", "not logged in", "authenticate"))
        ok = (r.returncode == 0 and not bad and ("gemini" in txt or "claude" in txt))
    except Exception:
        ok = (config.HOME / ".gemini" / "antigravity-cli" / "installation_id").is_file()
    _agy_auth_cache.update(t=now, ok=ok)
    return ok


def _probe_local_models(base, key):
    """Returns (reachable_bool, set_of_model_ids_or_None) for a local OpenAI-compatible server."""
    u = urllib.parse.urlsplit(base)
    root = f"{u.scheme}://{u.netloc}"
    hdr = {"Authorization": "Bearer " + key} if key else {}
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models", headers=hdr)
        with urllib.request.urlopen(req, timeout=0.5) as r:
            if r.status == 200:
                data = json.loads(r.read().decode("utf-8", "replace"))
                ids = {str(x.get("id") or "").lower() for x in (data.get("data") or []) if isinstance(x, dict)}
                return True, ids
    except urllib.error.HTTPError:
        # Port answered HTTP (e.g. 404 on /models): fall through to /health
        pass
    except Exception:
        # Connection refused / timeout on localhost: server is off, no need to retry same closed port
        return False, None
    try:
        req = urllib.request.Request(root + "/health", headers=hdr)
        with urllib.request.urlopen(req, timeout=0.4) as r:
            return r.status == 200, None
    except Exception:
        return False, None


def endpoint_status(ep, local_cache=None, fast_only=False):
    """One-word status for the model picker: 'ready', 'signed out', or 'offline'."""
    kind = ep.get("kind") or "openai"
    if kind == "claude-code":
        if not config.claude_exe():
            return {"state": "offline", "detail": "CLI not installed"}
        cred = config.HOME / ".claude" / ".credentials.json"
        if not _nonempty(cred):
            return {"state": "signed out", "detail": "Run claude to sign in"}
        return {"state": "ready", "detail": "Claude plan"}
    if kind == "codex":
        if not config.codex_exe():
            return {"state": "offline", "detail": "CLI not installed"}
        auth = config.HOME / ".codex" / "auth.json"
        if not _nonempty(auth):
            return {"state": "signed out", "detail": "Run codex login"}
        return {"state": "ready", "detail": "ChatGPT plan"}
    if kind == "agy":
        if not config.agy_exe():
            return {"state": "offline", "detail": "CLI not installed"}
        ok = _agy_signed_in_fast() if fast_only else _agy_signed_in()
        if not ok:
            return {"state": "signed out", "detail": "Sign in with agy"}
        return {"state": "ready", "detail": "Antigravity"}
    if kind == "opencode-go":
        keys = config.read_keys(ep)
        return {"state": "ready", "detail": "OpenCode Go"} if keys else {"state": "signed out", "detail": "Needs key"}
    if kind == "anthropic":
        keys = config.read_keys(ep)
        return {"state": "ready", "detail": "Anthropic API"} if keys else {"state": "signed out", "detail": "Needs key"}
    base = (ep.get("base") or "").rstrip("/")
    if not base:
        return {"state": "offline", "detail": "No URL"}
    if _local(base):
        if fast_only and (local_cache is None or base not in local_cache):
            return {"state": "offline", "detail": "Checking"}
        keys = config.read_keys(ep)
        key = keys[0] if keys else ""
        if local_cache is not None and base in local_cache:
            up, ids = local_cache[base]
        else:
            up, ids = _probe_local_models(base, key)
            if local_cache is not None:
                local_cache[base] = (up, ids)
        if not up:
            return {"state": "offline", "detail": "Server off"}
        want = str(ep.get("model") or "").lower()
        if ids is not None and want and ids and want not in ids:
            # Another model is currently loaded on this local port
            return {"state": "offline", "detail": "Not loaded"}
        return {"state": "ready", "detail": "Local server"}
    keys = config.read_keys(ep)
    if not keys:
        return {"state": "signed out", "detail": "Needs key"}
    return {"state": "ready", "detail": "API key"}


def _compute_statuses():
    """Run local port probes and CLI auth checks in parallel, update cache, and push SSE updates on change."""
    eps = config.get().get("endpoints") or []
    bases = {}
    for ep in eps:
        b = (ep.get("base") or "").rstrip("/")
        if ep.get("kind", "openai") == "openai" and b and _local(b) and b not in bases:
            keys = config.read_keys(ep)
            bases[b] = keys[0] if keys else ""
    local_cache = {}
    need_agy = any(ep.get("kind") == "agy" for ep in eps)
    if bases or need_agy:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(bases) + 1)) as ex:
            futs = {ex.submit(_probe_local_models, b, k): b for b, k in bases.items()}
            agy_fut = ex.submit(_agy_signed_in) if need_agy else None
            for f, b in futs.items():
                try:
                    local_cache[b] = f.result(timeout=1.5)
                except Exception:
                    local_cache[b] = (False, None)
            if agy_fut:
                try:
                    agy_fut.result(timeout=8.0)
                except Exception:
                    pass
    out = {ep["id"]: endpoint_status(ep, local_cache) for ep in eps}
    changed = False
    with _status_lock:
        changed = out != _status_cache["data"]
        _status_cache["t"] = time.time()
        _status_cache["data"] = out
    if changed:
        try:
            from . import agent
            agent.hub.publish({"type": "statuses", "statuses": dict(out)})
        except Exception:
            pass
    return dict(out)


def _refresh_statuses_async():
    """Coalesced background refresh so UI clicks never block on closed local sockets or `agy models`."""
    global _status_refreshing
    with _status_lock:
        if _status_refreshing:
            return
        _status_refreshing = True

    def _go():
        global _status_refreshing
        try:
            _compute_statuses()
        except Exception:
            pass
        finally:
            with _status_lock:
                _status_refreshing = False

    threading.Thread(target=_go, name="status-probe", daemon=True).start()


def warm_statuses():
    """Kick off an asynchronous probe at server startup so the first Model or Subagents tap is instant."""
    _refresh_statuses_async()


def all_statuses(force=False, blocking=False):
    """Status map for every configured endpoint (instant stale-while-revalidate; probes run in background)."""
    now = time.time()
    if blocking:
        return _compute_statuses()
    with _status_lock:
        cached = dict(_status_cache["data"]) if _status_cache["data"] else None
        age = now - _status_cache["t"]
    if cached is not None:
        if force or age >= STATUS_TTL:
            _refresh_statuses_async()
        return cached
    # Very first cold call before warm_statuses() finishes: answer immediately from fast file checks + probe in bg
    eps = config.get().get("endpoints") or []
    fast = {ep["id"]: endpoint_status(ep, None, fast_only=True) for ep in eps}
    _refresh_statuses_async()
    return fast
