"""computer: a stdio MCP server that Flock starts for its Claude Code and Codex sessions, so those models get the
same Computer tool as every other model in the app (screenshots, mouse, keyboard), with the same approval in the
app's window and the same Stop.

Standard library only. Each call goes back to the running harness (POST /api/computer/call on 127.0.0.1 with the app's
header and the token it made at start-up), which asks the user when needed, runs the action and answers; a screenshot
comes back as an MCP image. Environment (set by the harness):
  KCH_COMPUTER_URL      http://127.0.0.1:<port>/api/computer
  KCH_COMPUTER_TOKEN    the app's token (in memory only, never in a file)
  KCH_COMPUTER_CHAT     the chat this session belongs to (its permission, banner and Stop)
  KCH_COMPUTER_SPEC     a JSON file with the tool's name, description and parameters
  KCH_COMPUTER_TIMEOUT  seconds to wait for one call (the user may take a while to answer)"""
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request

out_lock = threading.Lock()


def send(msg):
    data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    with out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def spec():
    try:
        with open(os.environ["KCH_COMPUTER_SPEC"], encoding="utf-8") as f:
            s = json.load(f)
    except (KeyError, OSError, ValueError):
        s = {"name": "computer", "description": "See the user's screen and, with their permission, use the mouse and keyboard.",
             "parameters": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}}
    return {"name": s["name"], "description": s["description"], "inputSchema": s["parameters"]}


def post(suffix, body, timeout):
    url = os.environ.get("KCH_COMPUTER_URL", "") + suffix
    u = urllib.parse.urlsplit(url)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "X-KC-App": "1", "Host": u.netloc,
        "X-KC-Computer-Token": os.environ.get("KCH_COMPUTER_TOKEN", "")})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 127.0.0.1: never through a proxy
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


running = {}


def call(rid, args):
    key = secrets.token_hex(8)
    running[rid] = key
    content, err = [], True
    try:
        res = post("/call", {"args": args, "chat": os.environ.get("KCH_COMPUTER_CHAT"), "key": key},
                   timeout=float(os.environ.get("KCH_COMPUTER_TIMEOUT") or 900))
        content.append({"type": "text", "text": str(res.get("text") or "(no answer)")})
        if res.get("image"):
            content.append({"type": "image", "data": res["image"], "mimeType": res.get("mime") or "image/png"})
        err = not res.get("ok", True)
    except Exception as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]      # HTTPError: the harness said why
        except Exception:
            pass
        content = [{"type": "text", "text": f"Error: Flock did not run the action ({e.__class__.__name__}: {body or e})"}]
    finally:
        running.pop(rid, None)
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": content, "isError": err}})


def main():
    for raw in sys.stdin.buffer:
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": ver, "capabilities": {"tools": {}},
                                                           "serverInfo": {"name": "flock-computer", "version": "1.0"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [spec()]}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            if p.get("name") != spec()["name"]:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {p.get('name')}"}})
                continue
            threading.Thread(target=call, args=(rid, p.get("arguments") or {}), daemon=True).start()
        elif method == "notifications/cancelled":
            key = running.get((msg.get("params") or {}).get("requestId"))
            if key:
                try:
                    post("/cancel", {"key": key}, timeout=10)
                except Exception:
                    pass
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None and method:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}})


if __name__ == "__main__":
    main()
