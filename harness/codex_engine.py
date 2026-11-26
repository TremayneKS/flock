"""ChatGPT as a plug-in: the Codex CLI with the user's own ChatGPT plan (whatever `codex login` signed in), with its own
tools (shell, file edits, web search), run in the chat's project folder. One Codex thread per chat (`codex exec`, then
`codex exec resume <thread>`). Its JSON events become the harness's messages, so the chat looks the same whichever model
answers: commands show as Bash steps, file changes as Edit steps, searches as WebSearch steps."""
import json
import os
import re
import subprocess
import threading
import time

from . import config, effort, osutil, store

# Engine configuration/deprecation warnings (e.g. Codex desktop keys the CLI ignores) belong in the chat log, not in chat.
_CONFIG_NOTICE_RE = re.compile(
    r"unrecognized configuration setting|ignoring \d+ unrecognized|deprecated setting|config\.toml|"
    r"unknown config|configuration warning",
    re.I,
)


def rollout_ctx(thread, home=None, days=3):
    """How full the thread's context is now: the last API call's input, from Codex's own rollout file
    (~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl, last `token_count` event). `codex exec` only reports the
    turn's summed input, which for a 100-call turn is many times the window (a 6-turn chat showed 10M on a 500k
    window). None when the file or the event is not found."""
    if not thread:
        return None
    root = os.path.join(home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "sessions")
    path = None
    for back in range(days):                      # the thread's file sits under the day it was started
        d = time.localtime(time.time() - back * 86400)
        day = os.path.join(root, time.strftime("%Y", d), time.strftime("%m", d), time.strftime("%d", d))
        try:
            hit = [n for n in os.listdir(day) if n.endswith(thread + ".jsonl")]
        except OSError:
            continue
        if hit:
            path = os.path.join(day, hit[0])
            break
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 512_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            info = (json.loads(line).get("payload") or {}).get("info") or {}
        except ValueError:
            continue
        n = (info.get("last_token_usage") or {}).get("input_tokens")
        if n:
            return int(n)
    return None


def _emit_tool(chat, emit, ep, name, args, result, ok=True):
    """One Codex action as a harness step: an assistant tool call and its result."""
    cid = chat.id
    call = {"id": store.new_id("cx"), "name": name, "arguments": json.dumps(args, ensure_ascii=False)}
    am = chat.append({"role": "assistant", "content": "", "endpoint": ep["id"], "tool_calls": [call]})
    emit(cid, {"type": "assistant", "temp": None, "message": am})
    emit(cid, {"type": "tool_start", "call": {"id": call["id"], "name": name, "args": call["arguments"][:4000]}})
    tm = chat.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": str(result or "")[:30_000], "ok": ok,
                      "args": call["arguments"][:20_000]})
    emit(cid, {"type": "message", "message": tm})


def _one_turn(run, chat, meta, ep, emit, prompt_text, images, first_turn):
    """Run one `codex exec` or `codex exec resume <thread>` invocation. Returns ('done' | 'steered' | 'cancelled')."""
    from . import agent
    cid = chat.id
    exe = config.codex_exe()
    thread = meta.get("codex_thread")
    level = effort.resolve(ep, meta, meta.get("reasoning") or config.get().get("reasoning"))   # this turn's native level
    cwd = meta.get("project") or str(config.WORKSPACE)
    cmd = [exe, "exec"] + (["resume", thread] if thread else [])
    if ep.get("model"):
        cmd += ["-m", ep["model"]]
    cmd += ["-c", f'model_reasoning_effort="{level}"', "-c", "notify=[]", "--skip-git-repo-check", "--json"]
    from . import subagents
    srv = subagents.mcp_server(chat, meta)          # harness_agent: subagents on any connected model
    if srv:                                         # Codex passes MCP servers only the env it is told to (TOML values)
        env_t = "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in srv["env"].items()) + "}"
        cmd += ["-c", f"mcp_servers.harness.command={json.dumps(srv['command'])}",
                "-c", f"mcp_servers.harness.args={json.dumps(srv['args'])}", "-c", f"mcp_servers.harness.env={env_t}",
                "-c", "mcp_servers.harness.startup_timeout_sec=30",
                "-c", f"mcp_servers.harness.tool_timeout_sec={srv['env']['KCH_AGENT_TIMEOUT']}"]
    if not thread:
        cmd += ["-C", cwd]
    cmd += [f"--image={p}" for p in images if os.path.isfile(p)]
    from . import computer
    cmd += computer.codex_mcp_args(chat, meta)          # the Computer tool, through the app's approval and Stop
    # the harness's own tools run without prompts too; Codex's Windows sandbox also fails from the CLI on some machines
    cmd += ["--dangerously-bypass-approvals-and-sandbox", "-"]
    text = prompt_text
    if first_turn:
        from .chat_import import handover_prefix
        prior = handover_prefix(chat, "codex", bool(thread), ep.get("name") or "ChatGPT", emit)
        if prior:
            text = prior + text
        if not thread:
            from .agent import MEDIA_RULE, VISUAL_RULE, WAIT_RULE
            pctx = (meta.get("parent_context") + "\n\n") if meta.get("parent_context") else ""
            snote = subagents.sub_note_for(meta) if meta.get("sub_of") else ""
            text = (f"(You are running inside Flock. This chat's notes file is {chat.notes_path}: keep short bullets there "
                    f"of the goal, decisions, key files and open tasks. {WAIT_RULE} {MEDIA_RULE} {VISUAL_RULE}"
                    f"{snote})\n\n{pctx}{text}")
    env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY",)}      # use the ChatGPT plan, not an API key
    effort.log_cmd(cid, cmd)
    from . import resume as turn_record
    out_f, err_f = turn_record.engine_files(cid)
    # Codex's events go to a file, not a pipe to this process: if the app restarts in the middle of the turn, Codex does
    # not notice and finishes it, and the new copy follows the same file (adopt) instead of cutting the turn off.
    with open(out_f, "wb") as fo, open(err_f, "wb") as fe:
        proc = subprocess.Popen(cmd, cwd=cwd if os.path.isdir(cwd) else None, stdin=subprocess.PIPE, stdout=fo, stderr=fe,
                                env=env, **osutil.group())
    turn_record.engine(run, proc)
    proc.stdin.write(text.encode("utf-8"))
    proc.stdin.close()
    st = _follow(run, chat, ep, emit, level, proc, out_f, 0, thread)
    if st["completed"] or st["failed"]:
        if proc.poll() is None:
            rem = max(0.0, POST_TURN_GRACE_S - (time.time() - (st["done_at"] or time.time())))
            try:
                proc.wait(timeout=max(0.5, rem))
            except subprocess.TimeoutExpired:
                # Turn already completed/failed, but codex hung (e.g. a background preview server
                # inherited handles). Terminate ONLY the CLI wrapper process so detached servers survive.
                osutil.terminate_only(proc.pid)
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
    else:
        proc.wait()
    err = turn_record.tail_lines(err_f)
    from .chat_import import mark_engine_cursor
    mark_engine_cursor(chat, "codex")
    if run.cancel.is_set():
        return "cancelled"
    if st["steered"]:
        return "steered"
    if proc.returncode and not st["completed"] and err:
        m = chat.append({"role": "error", "content": "Codex stopped: " + " | ".join(err[-4:])[:800]})
        emit(cid, {"type": "message", "message": m})
    return "done"


def adopt(run, chat, ep, emit, rec):
    """After a restart (agent._carry_on): the Codex process the old copy started is still running its turn, writing to
    the same file. Follow it to its end. If it has ended too (everything was killed), what it wrote after the old copy
    last read is still taken in, so the chat shows the turn up to where it stopped. Returns True if it finished the
    turn, False if it ended without finishing (or was stopped, or cut for a message), None if there is nothing to follow."""
    from . import resume as turn_record
    e = rec.get("engine") or {}
    out_f, _ = turn_record.engine_files(chat.id)
    if not e.get("pid") or not out_f.exists():
        return None
    live = osutil.same_process(e["pid"], e.get("created"))
    pos = int(rec.get("engine_pos") or 0)
    turn_record.note(run, engine=e if live else None, engine_pos=pos)
    meta = chat.meta()
    level = effort.resolve(ep, meta, meta.get("reasoning") or config.get().get("reasoning"))
    proc = turn_record.Running(e["pid"], e.get("created") if live else -1)
    st = _follow(run, chat, ep, emit, level, proc, out_f, pos, meta.get("codex_thread"))
    if (st["completed"] or st["failed"]) and proc.poll() is None:
        osutil.terminate_only(proc.pid)
    from .chat_import import mark_engine_cursor
    mark_engine_cursor(chat, "codex")
    return st["completed"]


POST_TURN_GRACE_S = 5.0


def _follow(run, chat, ep, emit, level, proc, out_f, pos, thread):
    """Turn Codex's JSON events (from out_f, from byte pos on) into the chat's messages until the process ends
    (or until POST_TURN_GRACE_S after turn.completed/turn.failed if a background child keeps the CLI alive). After
    each line that added a message, how far the file has been read is kept in the turn record, so a copy that takes
    over after a restart carries on from the next line. Returns the state: completed (Codex finished its turn),
    steered (cut so a message from the user could go in)."""
    from . import agent, subagents
    from . import resume as turn_record
    cid = chat.id
    st = {"temp": None, "pending": None, "thinking": [], "t0": time.time(), "mcp": {},
          "thread": thread, "steered": False, "notices": set(), "n": 0,
          "completed": False, "failed": False, "done_at": 0.0}
    done_ev = threading.Event()

    def add(msg):
        st["n"] += 1
        return chat.append(msg)

    def killer():
        while proc.poll() is None and not done_ev.is_set():
            if run.cancel.wait(0.3):
                if done_ev.is_set():
                    return
                osutil.kill_tree(proc.pid)
                return
            if done_ev.is_set() or st["done_at"]:
                continue
            # Codex takes no stdin mid-run: once its thread id is known and STEER_GRACE has elapsed,
            # interrupt cleanly so turn() can resume the same thread with the user's steering message.
            if run.queue and st["thread"] and agent.steer_due(run, st["t0"]):
                st["steered"] = True
                from . import subagents
                subagents.detach_calls(chat.id)      # its subagents keep working; their reports come in later
                osutil.kill_tree(proc.pid)
                return
    threading.Thread(target=killer, daemon=True).start()

    def flush(stats=None):
        """Add the held-back answer (with the turn's numbers when they are known)."""
        am = st["pending"]
        if not am:
            return
        st["pending"] = None
        if stats:
            am["stats"] = stats
        am = add(am)
        emit(cid, {"type": "assistant", "temp": st["temp"], "message": am, "ctx": chat.meta().get("ctx")})
        st["temp"] = None

    def tool(name, args, result, ok=True):
        flush()
        st["n"] += 1
        _emit_tool(chat, emit, ep, name, args, result, ok)

    def keep_following():
        if proc.poll() is not None:
            return False
        if st["done_at"] and (time.time() - st["done_at"]) >= POST_TURN_GRACE_S:
            return False
        return True

    for raw, end in turn_record.follow(out_f, keep_following, pos):
        n0 = st["n"]
        try:
            ev = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        typ = ev.get("type")
        item = ev.get("item") or {}
        kind = item.get("type")
        if typ == "thread.started" and ev.get("thread_id"):
            st["thread"] = ev["thread_id"]
            chat.update_meta(codex_thread=ev["thread_id"])
        elif typ == "turn.started":
            st["temp"], st["t0"] = store.new_id("t"), time.time()
            emit(cid, {"type": "step", "id": st["temp"], "endpoint": ep["id"]})
        elif typ == "item.started" and kind == "command_execution":
            emit(cid, {"type": "waiting", "text": "Running: " + str(item.get("command") or "")[:200]})
        elif typ == "item.started" and kind == "mcp_tool_call":     # shown at once: a subagent draws its progress under it
            flush()
            name = f"mcp__{item.get('server')}__{item.get('tool')}"
            call = {"id": store.new_id("cx"), "name": name, "arguments": json.dumps(item.get("arguments") or {}, ensure_ascii=False)}
            st["mcp"][item.get("id")] = call
            am = add({"role": "assistant", "content": "", "endpoint": ep["id"], "tool_calls": [call]})
            emit(cid, {"type": "assistant", "temp": None, "message": am})
            emit(cid, {"type": "tool_start", "call": {"id": call["id"], "name": name, "args": call["arguments"][:4000]}})
            if item.get("tool") == subagents.TOOL:
                subagents.note_call(cid, call["id"], item.get("arguments"))
        elif typ == "item.completed":
            if kind == "agent_message":
                flush()
                st["pending"] = {"role": "assistant", "content": item.get("text") or "", "endpoint": ep["id"]}
                if st["thinking"]:
                    st["pending"]["reasoning_content"] = "\n".join(st["thinking"])
                    st["thinking"] = []
            elif kind == "reasoning":
                st["thinking"].append(item.get("text") or item.get("summary") or "")
                if st["temp"]:
                    emit(cid, {"type": "reasoning", "id": st["temp"], "text": (item.get("text") or "") + "\n"})
            elif kind == "command_execution":
                code = item.get("exit_code")
                out = (item.get("aggregated_output") or "").rstrip() + ("" if code in (0, None) else f"\n[exit code {code}]")
                tool("Bash", {"command": item.get("command") or ""}, out or "(no output)", code in (0, None))
            elif kind == "file_change":
                for ch in item.get("changes") or []:
                    path = ch.get("path") or ""
                    tool("Write" if ch.get("kind") == "add" else "Edit", {"file_path": path},
                         f"{ch.get('kind') or 'update'}: {path}", item.get("status") != "failed")
                    emit(cid, {"type": "file", "path": path, "change": "created" if ch.get("kind") == "add" else "changed"})
            elif kind == "web_search":
                act = item.get("action") or {}
                rows = "\n".join(f"- {r.get('title') or r.get('url')} {r.get('url') or ''}" for r in item.get("results") or []
                                 if isinstance(r, dict))
                tool("WebSearch", {"query": item.get("query") or act.get("query") or ""}, rows or act.get("url") or "searched")
            elif kind == "mcp_tool_call":
                res = item.get("result") or item.get("error") or ""
                parts = res.get("content") if isinstance(res, dict) else None
                out = ("\n".join(p.get("text", "") for p in parts if isinstance(p, dict)) if isinstance(parts, list)
                       else res if isinstance(res, str) else json.dumps(res))
                call = st["mcp"].pop(item.get("id"), None)
                if call:                            # started above: answer that call
                    tm = add({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": out[:30_000],
                                      "ok": not item.get("error"), "args": call["arguments"][:20_000]})
                    emit(cid, {"type": "message", "message": tm})
                else:
                    tool(f"mcp__{item.get('server')}__{item.get('tool')}", item.get("arguments") or {}, out[:30_000],
                         not item.get("error"))
            elif kind == "todo_list":
                todos = [{"content": str(t.get("text") or "")[:200], "status": "completed" if t.get("completed") else "pending"}
                         for t in item.get("items") or [] if isinstance(t, dict)]
                chat.update_meta(todos=todos)
                emit(cid, {"type": "todos", "todos": todos})
            elif kind == "error":
                msg = str(item.get("message") or "").strip()[:600]
                if not msg:
                    continue
                norm = re.sub(r"\s+", " ", msg).lower()
                if norm in st["notices"]:
                    continue
                st["notices"].add(norm)
                if _CONFIG_NOTICE_RE.search(msg):
                    flat = re.sub(r"\s+", " ", msg)[:320]
                    chat.append_log(f"- {time.strftime('%d %b %H:%M')} Codex config notice: {flat}\n")
                    turn_record.note(run, engine_pos=end)
                    continue
                flush()
                m = add({"role": "notice", "content": "Codex: " + msg})
                emit(cid, {"type": "message", "message": m})
        elif typ == "turn.completed":
            # `codex exec` reports the thread's running totals (checked against its rollout file): this turn = the difference
            tot = {k: (ev.get("usage") or {}).get(k) or 0 for k in ("input_tokens", "cached_input_tokens", "output_tokens")}
            tid = chat.meta().get("codex_thread")
            seen = chat.meta().get("codex_seen") or {}
            seen = seen if seen.get("thread") == tid and all(tot[k] >= (seen.get(k) or 0) for k in tot) else {}
            u = {k: tot[k] - (seen.get(k) or 0) for k in tot}
            chat.update_meta(codex_seen={**tot, "thread": tid})
            cached = u.get("cached_input_tokens") or 0
            stats = {"model": ep.get("model") or "Codex default", "prompt_new": max(0, (u.get("input_tokens") or 0) - cached),
                     "prompt_cached": cached, "out_tokens": u.get("output_tokens"), "secs": round(time.time() - st["t0"], 1),
                     "effort": effort.label(ep, level),
                     "ctx_tokens": rollout_ctx(tid) or u.get("input_tokens") or 0}   # last call's input, not the turn's sum
            use = chat.meta().get("usage") or {}
            for k in ("prompt_new", "prompt_cached", "out_tokens"):
                use[k] = (use.get(k) or 0) + (stats.get(k) or 0)
            use["steps"] = (use.get("steps") or 0) + 1
            chat.update_meta(usage=use, ctx={"tokens": stats["ctx_tokens"], "limit": ep.get("context"), "by": ep["id"]})
            flush(stats)
            st["completed"] = True
            st["done_at"] = time.time()
            turn_record.note(run, completed=True)        # a restart from here on has nothing to carry on
        elif typ in ("turn.failed", "error") and not run.cancel.is_set() and not st["steered"]:
            e = ev.get("error")
            why = e.get("message") if isinstance(e, dict) else (ev.get("message") or e)
            why_s = str(why or typ).strip()
            if typ == "error" and _CONFIG_NOTICE_RE.search(why_s):
                norm = re.sub(r"\s+", " ", why_s).lower()
                if norm not in st["notices"]:
                    st["notices"].add(norm)
                    flat = re.sub(r"\s+", " ", why_s)[:320]
                    chat.append_log(f"- {time.strftime('%d %b %H:%M')} Codex config notice: {flat}\n")
                turn_record.note(run, engine_pos=end)
                continue
            flush()
            m = add({"role": "error", "content": "Codex: " + why_s[:800]})
            emit(cid, {"type": "message", "message": m})
            if typ == "turn.failed":
                st["failed"] = True
                st["done_at"] = time.time()
        if st["n"] != n0:
            turn_record.note(run, engine_pos=end)     # a copy taking over after a restart reads on from here
    done_ev.set()
    flush()
    return st


def turn(run, chat, meta, ep, emit):
    from . import agent
    cid = chat.id
    msgs = chat.messages()
    last = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
    if not last:
        return
    if not config.codex_exe():
        m = chat.append({"role": "error", "content": "The Codex CLI is not installed (npm i -g @openai/codex, then `codex login`)."})
        emit(cid, {"type": "message", "message": m})
        return
    prompt = last.get("model_content") or last.get("content") or ""
    images = [f["path"] for f in last.get("files") or [] if f.get("kind") == "image"]
    first = True
    while not run.cancel.is_set():
        outcome = _one_turn(run, chat, chat.meta(), ep, emit, prompt, images, first)
        first = False
        if outcome == "cancelled" or run.cancel.is_set():
            break
        if run.queue:
            if outcome == "steered":
                nm = chat.append({"role": "notice",
                                  "content": "ChatGPT (Codex) cannot take input mid-run: interrupted the turn and "
                                             "resumed the thread with your message (work so far is kept)."})
                emit(cid, {"type": "message", "message": nm})
            items = agent.take_queue(run)
            parts, imgs = [], []
            for item in items:
                um = agent.user_message(chat, chat.meta(), item["text"], item["files"])
                if item.get("steer"):
                    um["steer"] = True
                m = chat.append(um)
                emit(cid, {"type": "message", "message": agent._public(m), "queued_id": item.get("id")})
                parts.append(m.get("model_content") or m.get("content") or "")
                imgs.extend(f["path"] for f in m.get("files") or [] if f.get("kind") == "image")
            prompt = "\n\n".join(p for p in parts if p)
            images = imgs
            if not prompt and not images:
                break
            continue
        break
