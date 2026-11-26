"""harness_agent: a stdio MCP server that Flock starts for its Claude Code and Codex sessions, so those models can
hand a task to a subagent on any model connected in the harness (Anthropic, OpenAI, Gemini, DeepSeek, local models, Claude, ChatGPT...).

Standard library only. Each call goes back to the running harness (POST /api/subagent on 127.0.0.1 with the app's
header and the token it made at start-up), which runs the subagent in the chat's project folder, shows its progress
under the call in the window, and answers with its final reply. Environment (set by the harness):
  KCH_AGENT_URL      http://127.0.0.1:<port>/api/subagent
  KCH_AGENT_TOKEN    the app's token (in memory only, never in a file)
  KCH_AGENT_CHAT     the chat this session belongs to (its depth, Stop and the window come from it)
  KCH_AGENT_SPEC     a JSON file with the tool's name, description and parameters (fixed for the chat)
  KCH_AGENT_TIMEOUT  how long the CLI itself waits for one call (set by the harness); this server waits as long as it runs"""
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
        with open(os.environ["KCH_AGENT_SPEC"], encoding="utf-8") as f:
            s = json.load(f)
    except (KeyError, OSError, ValueError):
        s = {"name": "harness_agent", "description": "Run a subagent on any model or named subagent in Flock.",
             "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "subagent": {"type": "string"},
                                                             "model": {"type": "string"}, "description": {"type": "string"}},
                            "required": ["prompt"]}}
    return {"name": s["name"], "description": s["description"], "inputSchema": s["parameters"]}


def chats_spec():
    return {
        "name": "harness_chats",
        "description": "List, move, archive, unarchive or clean up Flock chats and chat groups across sections (e.g. General, Work, Personal), or undo a change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": '"list", "move", "archive", "unarchive", "cleanup" or "undo"'},
                "section": {"type": "string", "description": 'Target section name for move (e.g. "General", "Work", "Personal")'},
                "query": {"type": "string", "description": "Keyword to match chat titles, folders or group names"},
                "chats": {"type": "array", "items": {"type": "string"}, "description": "Chat ids or title matches"},
                "groups": {"type": "array", "items": {"type": "string"}, "description": "Group ids or names"},
                "undo_id": {"type": "string", "description": "Undo id from an earlier change (optional)"},
            },
            "required": ["action"],
        },
    }


def post(path_suffix, body, timeout, full_url=None):
    url = full_url or (os.environ.get("KCH_AGENT_URL", "") + path_suffix)
    u = urllib.parse.urlsplit(url)
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "X-KC-App": "1", "Host": u.netloc,
        "X-KC-Agent-Token": os.environ.get("KCH_AGENT_TOKEN", "")})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))      # 127.0.0.1: never through a proxy
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


running = {}                     # JSON-RPC id -> key the harness knows the call by


def call_chats(rid, args):
    base = os.environ.get("KCH_AGENT_URL", "")
    url = base.rsplit("/api/", 1)[0] + "/api/chats-manage" if "/api/" in base else ""
    try:
        res = post("", {"args": args, "chat": os.environ.get("KCH_AGENT_CHAT")}, timeout=30, full_url=url)
        text, err = str(res.get("text") or res.get("error") or ""), not res.get("ok")
    except Exception as e:
        text, err = f"Error: Flock could not organize chats ({e})", True
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text or "done"}], "isError": err}})


def call(rid, args):
    key = secrets.token_hex(8)
    running[rid] = key
    try:
        res = post("", {**{k: args.get(k) for k in ("prompt", "subagent", "model", "description")},
                        "chat": os.environ.get("KCH_AGENT_CHAT"),
                        "key": key}, timeout=None)        # waits as long as the subagent works (it has no budget)
        text, err = str(res.get("text") or res.get("error") or ""), not res.get("ok")
    except Exception as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]      # HTTPError: the harness said why
        except Exception:
            pass
        text, err = f"Error: Flock did not run the subagent ({e.__class__.__name__}: {body or e})", True
    finally:
        running.pop(rid, None)
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text or "(no answer)"}], "isError": err}})


def parent_context_spec():
    return {
        "name": "harness_parent_context",
        "description": "Search or read the main (parent) chat's full history, notes and live updates without loading the whole transcript into context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": '"search", "latest", "read" or "notes"'},
                "query": {"type": "string", "description": "Keyword or regex to search for in parent chat"},
                "start": {"type": "integer", "description": "1-based turn index for read"},
                "limit": {"type": "integer", "description": "Max hits or turns to return (default 10)"},
            },
            "required": ["action"],
        },
    }


def report_main_spec():
    return {
        "name": "harness_report_to_main",
        "description": "Send a concise summary of your findings back to the main (parent) chat.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Concise findings, file paths and conclusions for the main chat"},
                "wake_main": {"type": "boolean", "description": "True to send to the main model now (steers if busy); false to hand off quietly for its next turn"},
            },
            "required": ["summary"],
        },
    }


def call_sub_helper(rid, suffix, args):
    try:
        res = post(suffix, {**args, "chat": os.environ.get("KCH_AGENT_CHAT")}, timeout=30)
        text, err = str(res.get("text") or res.get("message") or res.get("error") or ""), not res.get("ok", True)
    except Exception as e:
        text, err = f"Error: Flock subagent helper failed ({e})", True
    send({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text or "done"}], "isError": err}})


def main():
    can_spawn = os.environ.get("KCH_AGENT_CAN_SPAWN", "1") != "0"
    is_sub = os.environ.get("KCH_AGENT_IS_SUB", "0") == "1"
    for raw in sys.stdin.buffer:
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if method == "initialize":
            ver = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": ver, "capabilities": {"tools": {}},
                                                           "serverInfo": {"name": "flock-agents", "version": "1.0"}}})
        elif method == "tools/list":
            tlist = []
            if can_spawn:
                tlist.append(spec())
            tlist.append(chats_spec())
            if is_sub:
                tlist.append(parent_context_spec())
                tlist.append(report_main_spec())
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": tlist}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            tname = p.get("name")
            targs = p.get("arguments") or {}
            if tname == "harness_chats":
                threading.Thread(target=call_chats, args=(rid, targs), daemon=True).start()
                continue
            if tname == "harness_parent_context" and is_sub:
                threading.Thread(target=call_sub_helper, args=(rid, "/parent-context", targs), daemon=True).start()
                continue
            if tname == "harness_report_to_main" and is_sub:
                threading.Thread(target=call_sub_helper, args=(rid, "/report-main", targs), daemon=True).start()
                continue
            if not can_spawn or tname != spec()["name"]:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {tname}"}})
                continue
            threading.Thread(target=call, args=(rid, targs), daemon=True).start()
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


CLI_TOOLS = ("agent", "chats", "report", "parent")


def cli(argv):
    """The same calls as the tools, as a command, for a Neon in print mode (agy takes no MCP server per session there;
    its shell commands inherit KCH_AGENT_* from the agy process the harness started):

        '{"model": "deepseek", "prompt": "..."}' | python agents.py agent -      (JSON on stdin: PowerShell 5 strips
        python agents.py agent @task.json                                          the quotes of a JSON argument)
        python agents.py report -   /   parent -   /   chats -

    Prints the answer (a subagent's final reply waits until it is done). Exit 0, 1 on an error, 2 on bad input."""
    tool = argv[0] if argv else ""
    if tool not in CLI_TOOLS:
        print("usage: agents.py {%s} -|@file|'<json>'  (the JSON fields of the tool)" % "|".join(CLI_TOOLS))
        return 2
    raw = argv[1] if len(argv) > 1 else "-"
    try:
        if raw == "-":
            raw = sys.stdin.read()
        elif raw.startswith("@"):
            with open(raw[1:], encoding="utf-8-sig") as f:
                raw = f.read()
        args = json.loads(raw.lstrip("﻿")) if raw.strip() else {}
    except (OSError, ValueError) as e:
        print("Error: the input is not JSON (%s). Pipe it in: '{\"prompt\": \"...\"}' | agents.py %s -" % (e, tool))
        return 2
    if not isinstance(args, dict):
        print("Error: the input must be a JSON object")
        return 2
    if not os.environ.get("KCH_AGENT_URL") or not os.environ.get("KCH_AGENT_CHAT"):
        print("Error: this works only inside a Flock chat (KCH_AGENT_URL / KCH_AGENT_CHAT are not set)")
        return 2
    got = []
    global send
    send = got.append                              # the tool functions answer through send(): keep the answer
    if tool == "agent":
        call(0, args)
    elif tool == "chats":
        call_chats(0, args)
    else:
        call_sub_helper(0, "/report-main" if tool == "report" else "/parent-context", args)
    res = (got[-1] if got else {}).get("result") or {}
    text = "".join(c.get("text", "") for c in res.get("content") or [])
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(text or "(no answer)")
    return 1 if res.get("isError") else 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    main()
