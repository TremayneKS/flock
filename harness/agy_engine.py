"""Antigravity CLI (agy) as a plug-in engine: Gemini 4 Neon, Hydrogen, Carbon and Argon on the user's signed-in plan.

One agy conversation per chat (meta `agy_conversation`, passed with `--conversation <id>`). Its stream-json events
become normal harness messages (text, thinking, tool calls and results), so chats look the same whichever model runs.
If the user sends a steering message while a turn is running, the harness waits briefly for the turn to finish on its
own; if it is still running after STEER_GRACE seconds and the conversation id is known, the harness interrupts the
process, posts a quiet notice, and resumes the same conversation with the user's new instruction (work so far is kept).
While the turn has background work running (a command agy put in the background, a timer), it is never cut: ending
agy ends every command it started. The message goes into agy's inbox instead and the model reads it at its next wake,
and while agy holds its stdout back the model's words are shown from its transcript (agy_live.py).
"""
import json
import os
import subprocess
import threading
import time

from . import agy_live, config, effort, osutil, store


def agy_exe():
    exe = config.agy_exe()
    if not exe:
        raise RuntimeError("the agy CLI is not installed (install Antigravity CLI and run `agy` once to sign in)")
    return exe


def _model_arg(ep, level):
    base = (ep.get("model") or "gemini-4-neon").strip()
    if base.endswith(("-low", "-medium", "-high", "-xhigh")):
        base = base.rsplit("-", 1)[0]
    lv = level or "high"
    return f"{base}-{lv}"


def _emit_tool(chat, emit, ep, name, args, result, ok=True):
    cid = chat.id
    call = {"id": store.new_id("ag"), "name": name, "arguments": json.dumps(args, ensure_ascii=False)}
    am = chat.append({"role": "assistant", "content": "", "endpoint": ep["id"], "tool_calls": [call]})
    emit(cid, {"type": "assistant", "temp": None, "message": am})
    emit(cid, {"type": "tool_start", "call": {"id": call["id"], "name": name, "args": call["arguments"][:4000]}})
    tm = chat.append({"role": "tool", "tool_call_id": call["id"], "name": name, "content": str(result or "")[:30_000],
                      "ok": ok, "args": call["arguments"][:20_000]})
    emit(cid, {"type": "message", "message": tm})


TASK_LOGS = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli", "brain")


def task_log(conv, idx):
    """Where agy writes a command's output once it runs on in the background: brain/<conversation>/.system_generated/
    tasks/task-<step index>.log (its task id is <conversation>/task-<step index>)."""
    if not conv or idx is None:
        return None
    return os.path.join(TASK_LOGS, conv, ".system_generated", "tasks", "task-%s.log" % idx)


def _activity_start(agent, cid, conv, idx, tname, params):
    """The step agy has started, for the page: what it is, since when, and for a command the file its output goes to,
    so a 40-minute remote job shows its own progress instead of a chat that looks stopped."""
    from . import activity
    p = params if isinstance(params, dict) else {}
    until = None
    if tname == "schedule" and p.get("DurationSeconds"):
        try:
            until = time.time() + float(p["DurationSeconds"])
        except (TypeError, ValueError):
            until = None
        what = "Timer: " + str(p.get("Prompt") or "%ss" % p["DurationSeconds"])
    else:
        what = activity.summary(tname, p)
    agent.act_start(cid, "agy:%s" % idx, what, log=task_log(conv, idx) if tname == "Bash" else None, until=until)


# Put before a message the user sent while the model worked (a steer, a message read from the inbox, one carried to
# the next turn): in the GPU chat the user asked for an update three times and saw only tool rows.
WORDS_FIRST = ("[Sent by the user while you were working. Before any tool call, answer them in words: one to three "
               "sentences with the direct answer, or what you are doing, what is running and how far along it is. Then "
               "carry on. Do not stop or restart running jobs unless they ask you to.]")
FOLLOW_TICK = 1.0   # how often the transcript is read
HOLD_S = 3          # seconds a transcript step may wait for stdout before it is shown from the transcript


def steered_text(text):
    return WORDS_FIRST + "\n\n" + text if text else text


def _map_tool_name(name):
    return {
        "read_file": "Read",
        "write_file": "Write",
        "edit_file": "Edit",
        "run_command": "Bash",
        "search_web": "WebSearch",
        "grep_search": "Grep",
        "find_by_name": "Glob",
    }.get(name or "", name or "tool")


def _first_text(chat, ep, emit, text):
    """A turn's first prompt: earlier messages from another model (a handover), a subagent's brief."""
    meta = chat.meta()
    conv_id = meta.get("agy_conversation")
    from .chat_import import handover_prefix
    prior = handover_prefix(chat, "agy", bool(conv_id), ep.get("name") or "Gemini", emit)
    if prior:
        text = prior + text
    from . import subagents
    if meta.get("sub_of") and not conv_id:
        pctx = (meta.get("parent_context") + "\n\n") if meta.get("parent_context") else ""
        text = subagents.sub_note_for(meta).strip() + "\n\n" + pctx + text
    # Once per conversation: how to start subagents the user sees in the app (22 Nov: a Neon asked for subagents ran
    # `claude -p` in a shell and agy's own invoke_subagent; neither shows in the app).
    from .agent import MEDIA_RULE, VISUAL_RULE
    note = "\n\n".join(x for x in (MEDIA_RULE, VISUAL_RULE, subagents.agy_note(meta)) if x)
    if note:
        import hashlib
        mark = "%s|%s" % (conv_id, hashlib.sha1(note.encode("utf-8")).hexdigest()[:12])
        if meta.get("agy_note") != mark:
            text = "[Flock] " + note + "\n\n" + text
            chat.update_meta(agy_note=mark)
    return text


def _note_conv(chat):
    """A note given to a conversation that had no id yet is kept under the id agy gave it (not given twice)."""
    meta = chat.meta()
    mark, conv = meta.get("agy_note") or "", meta.get("agy_conversation")
    if mark.startswith("None|") and conv:
        chat.update_meta(agy_note=conv + mark[4:])


# Off: messages go straight to the model instead of waiting behind background timers.
# a message the user sends always goes to the model at once, cutting the turn and resuming with his words, as before.
# With it on, a message sent while a timer or background command ran was put in agy's inbox and waited for its next wake.
INBOX_WHILE_BACKGROUND = False


def _one_turn(run, chat, ep, emit, prompt_text, first_turn, carry=None):
    """Run one agy turn. Returns ('done' | 'steered' | 'cancelled'). Messages handed to agy's inbox that it never read
    (the process ended first) are added to carry, for the next turn."""
    cid = chat.id
    meta = chat.meta()
    conv_id = meta.get("agy_conversation")
    level = effort.resolve(ep, meta, meta.get("reasoning") or config.get().get("reasoning"))
    model_name = _model_arg(ep, level)
    cwd = meta.get("project") or str(config.WORKSPACE)
    if not os.path.isdir(cwd):
        cwd = str(config.WORKSPACE)
        os.makedirs(cwd, exist_ok=True)
    text = _first_text(chat, ep, emit, prompt_text) if first_turn else prompt_text
    # The prompt goes in on stdin as one stream-json message ({"event": "user", "message": {...}}, agy's own format),
    # not as `-p=<text>`: a handover of earlier messages runs to tens of thousands of characters, and Windows refuses a
    # command line over ~32k (WinError 206), which ended a switch to Neon on a long chat before it started.
    cmd = [agy_exe(), "--print=", "--input-format", "stream-json", "--model", model_name, "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--disable-slash-commands"]
    if conv_id:
        cmd += ["--conversation", conv_id]
    effort.log_cmd(cid, cmd)
    # where this turn starts in a resumed conversation's transcript, taken before agy can write to it
    tr0 = None
    if conv_id:
        tr0 = agy_live.Transcript(os.path.join(agy_live.sysdir(TASK_LOGS, conv_id), "logs", "transcript.jsonl"))
    # No MCP server per session in print mode (agy refuses mcpServers in --settings): the chat's KCH_AGENT_* go on the
    # agy process instead, so the commands Neon runs can start subagents in the app (servers/agents.py agent -).
    from . import subagents
    env = dict(os.environ)
    srv = subagents.mcp_server(chat, meta)
    if srv:
        env.update(srv["env"])
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, **osutil.group())
    from . import resume as turn_record
    turn_record.engine(run, proc)                      # a restart ends it before the conversation is resumed (resume.py)
    try:
        line = json.dumps({"event": "user", "message": {"role": "user", "content": text}})
        proc.stdin.write((line + "\n").encode("utf-8"))
        proc.stdin.close()
    except OSError:
        pass
    err = []
    threading.Thread(target=lambda: err.extend(proc.stderr.read().decode("utf-8", "replace").splitlines()[-20:]),
                     daemon=True).start()

    from . import agent
    state = {"steered": False, "t0": time.time(), "conv": conv_id, "out_max": -1, "shown": set(), "held": False,
             "wake": [], "inbox": []}
    seen_tools = {}
    lock = threading.Lock()

    def background_alive():
        """Work that ending agy would end: a timer, a command agy has put in the background, or a stdout held back
        behind one (the transcript is ahead of it)."""
        now = time.time()
        for tname, params, t in list(seen_tools.values()):
            if tname == "schedule":
                return True
            if tname == "Bash" and isinstance(params, dict) and params.get("WaitMsBeforeAsync") is not None:
                try:
                    if now - t > float(params["WaitMsBeforeAsync"]) / 1000 + 2:
                        return True
                except (TypeError, ValueError):
                    return True
        return state["held"]

    def status_line():
        from . import activity
        a = activity.now(cid) or {}
        what = a.get("what") or "background work"
        if a.get("since") and time.time() - a["since"] >= 60:
            what += " (%d min)" % ((time.time() - a["since"]) // 60)
        wake = [w for w in state["wake"] + ([a["until"]] if a.get("until") else []) if w > time.time()]
        when = "at its next check-in"
        if wake:
            when += ", in about %d min" % max(1, int((min(wake) - time.time() + 59) // 60))
        return ("%s is running %s. Your message is with it and it answers %s; nothing was stopped. Stop cuts in now, "
                "and that also ends this background work." % (ep.get("name") or "The model", what, when))

    def hand_in():
        """The user's message while background work runs: into agy's inbox, the turn and its jobs left running."""
        sd = agy_live.sysdir(TASK_LOGS, state["conv"])
        items = agent.take_queue(run)
        for item in items:
            um = agent.user_message(chat, chat.meta(), item["text"], item["files"])
            um["steer"] = True
            m = chat.append(um)
            emit(cid, {"type": "message", "message": agent._public(m), "queued_id": item.get("id")})
            text = m.get("model_content") or m.get("content") or ""
            if not text:
                continue
            try:
                state["inbox"].append((agy_live.send_inbox(sd, state["conv"], steered_text(text)), text))
            except OSError:
                if carry is not None:
                    carry.append(text)
        nm = chat.append({"role": "notice", "content": status_line()})
        emit(cid, {"type": "message", "message": nm})

    done_ev = threading.Event()
    POST_TURN_GRACE_S = 5.0

    def killer():
        while proc.poll() is None and not done_ev.is_set():
            if run.cancel.wait(0.3):
                if done_ev.is_set():
                    return
                osutil.kill_tree(proc.pid)
                return
            if done_ev.is_set() or state.get("completed"):
                return
            if run.queue and state["conv"] and agent.steer_due(run, state["t0"]):
                if INBOX_WHILE_BACKGROUND and background_alive():
                    hand_in()
                    continue
                state["steered"] = True
                from . import subagents
                subagents.detach_calls(cid)          # its subagents keep working; their reports come in later
                osutil.kill_tree(proc.pid)
                return

    def transcript():
        return agy_live.Transcript(os.path.join(agy_live.sysdir(TASK_LOGS, state["conv"]), "logs", "transcript.jsonl"),
                                   from_end=False)  # a new conversation: its transcript starts with this turn

    def follow():
        """The model's words from agy's transcript while stdout is held back behind background work."""
        tr, pending = tr0, {}
        while proc.poll() is None and not done_ev.is_set():
            time.sleep(FOLLOW_TICK)
            if not state["conv"]:
                continue
            if tr is None:
                tr = transcript()
            now = time.time()
            for e in tr.new():
                i = agy_live.idx_of(e)
                if i is not None and i > state["out_max"]:
                    pending[i] = (now, e)
            for i in sorted(pending):
                t, e = pending[i]
                if i <= state["out_max"]:
                    pending.pop(i)                       # stdout caught up and showed it itself
                    continue
                if now - t < HOLD_S:
                    continue
                pending.pop(i)
                state["held"] = True
                state["wake"].extend(agy_live.timers(e))
                body = agy_live.words(e)
                if not body:
                    continue
                with lock:
                    state["shown"].add(i)
                    am = chat.append({"role": "assistant", "content": body, "endpoint": ep["id"]})
                emit(cid, {"type": "assistant", "temp": None, "message": am})

    threading.Thread(target=killer, daemon=True).start()
    threading.Thread(target=follow, daemon=True).start()

    temp = store.new_id("t")
    emit(cid, {"type": "step", "id": temp, "endpoint": ep["id"]})
    text_buf = []
    think_buf = []
    final_usage = None
    result_err = None
    calls_u = {"in": 0, "cached": 0, "out": 0, "think": 0, "calls": 0, "last_ctx": 0}

    def flush_prose(stats=None):
        nonlocal temp
        body = "".join(text_buf).strip()
        thought = "".join(think_buf).strip()
        text_buf.clear()
        think_buf.clear()
        if not body and not thought:
            return
        am = {"role": "assistant", "content": body, "endpoint": ep["id"]}
        if thought:
            am["reasoning_content"] = thought
        if stats:
            am["stats"] = stats
        am = chat.append(am)
        emit(cid, {"type": "assistant", "temp": temp, "message": am, "ctx": chat.meta().get("ctx")})
        temp = None

    for raw in proc.stdout:
        try:
            ev = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        etype = ev.get("event")
        got_cid = ev.get("conversation_id") or (ev.get("init") or {}).get("conversation_id") or \
            (ev.get("step_update") or {}).get("conversation_id") or (ev.get("result") or {}).get("conversation_id")
        if got_cid and got_cid != state["conv"]:
            state["conv"] = got_cid
            chat.update_meta(agy_conversation=got_cid)
        if etype == "step_update":
            su = ev.get("step_update") or {}
            stype = su.get("step_type")
            st = su.get("state")
            idx = su.get("step_index")
            if isinstance(idx, int) and idx > state["out_max"]:
                state["out_max"] = idx
            if stype == "agent_response" and idx in state["shown"] and not su.get("usage"):
                continue                             # already shown from the transcript while stdout was held
                # One model call. Its numbers are its own: input_tokens is the uncached part, cache_read_tokens the
                # rest. (The final result's usage is the whole conversation's running total, not this turn's.)
                su_u = su["usage"]
                for k, v in (("in", su_u.get("input_tokens")), ("cached", su_u.get("cache_read_tokens")),
                             ("out", su_u.get("output_tokens")), ("think", su_u.get("thinking_tokens"))):
                    calls_u[k] += int(v or 0)
                calls_u["calls"] += 1
                calls_u["last_ctx"] = int(su_u.get("input_tokens") or 0) + int(su_u.get("cache_read_tokens") or 0)
            if stype == "agent_response":
                if not temp:
                    temp = store.new_id("t")
                    emit(cid, {"type": "step", "id": temp, "endpoint": ep["id"]})
                if idx in state["shown"]:
                    continue
                if su.get("thinking_delta"):
                    think_buf.append(su["thinking_delta"])
                    emit(cid, {"type": "reasoning", "id": temp, "text": su["thinking_delta"]})
                if su.get("text_delta"):
                    text_buf.append(su["text_delta"])
                    emit(cid, {"type": "delta", "id": temp, "text": su["text_delta"]})
            elif stype == "tool":
                tname = _map_tool_name(su.get("tool_name") or (su.get("tool_info") or {}).get("name"))
                params = (su.get("tool_info") or {}).get("parameters") or {}
                if st == "ACTIVE":
                    flush_prose()
                    seen_tools[idx] = (tname, params, time.time())
                    emit(cid, {"type": "waiting", "text": f"Running {tname}"})
                    _activity_start(agent, cid, state["conv"], idx, tname, params)
                elif st in ("DONE", "ERROR"):
                    agent.act_done(cid, "agy:%s" % idx)
                    tname, params = seen_tools.pop(idx, (tname, params, 0))[:2]
                    dur = su.get("duration_seconds")
                    summary = su.get("output") or (f"{st.lower()} ({dur:.1f}s)" if dur else st.lower())
                    _emit_tool(chat, emit, ep, tname, params, summary, ok=(st != "ERROR"))
        elif etype == "result":
            res = ev.get("result") or {}
            u = res.get("usage") or {}
            if res.get("status") not in (None, "SUCCESS") and res.get("error"):
                result_err = str(res["error"])
            if not text_buf and res.get("response") and not state["shown"]:   # (shown ones are already in the chat)
                text_buf.append(str(res["response"]))
            if not calls_u["calls"]:         # an older agy without per-step usage: the totals, less the last seen
                seen = chat.meta().get("agy_seen") or {}
                same = seen.get("conv") == state["conv"]
                calls_u.update({"in": max(0, int(u.get("input_tokens") or 0) - (seen.get("in", 0) if same else 0)),
                                "cached": max(0, int(u.get("cache_read_tokens") or 0) - (seen.get("cached", 0) if same else 0)),
                                "out": max(0, int(u.get("output_tokens") or 0) - (seen.get("out", 0) if same else 0)),
                                "calls": 1})
            chat.update_meta(agy_seen={"conv": state["conv"], "in": int(u.get("input_tokens") or 0),
                                       "cached": int(u.get("cache_read_tokens") or 0), "out": int(u.get("output_tokens") or 0)})
            final_usage = {
                "model": model_name,
                "prompt_new": calls_u["in"],
                "prompt_uncached": calls_u["in"],
                "prompt_cached": calls_u["cached"],
                "out_tokens": calls_u["out"],
                "secs": round(time.time() - state["t0"], 1),
                "effort": effort.label(ep, level),
                "ctx_tokens": calls_u["last_ctx"] or calls_u["in"] + calls_u["cached"],
            }
            if calls_u["calls"] > 1:
                final_usage["calls"] = calls_u["calls"]
            use = chat.meta().get("usage") or {}
            for k in ("prompt_new", "prompt_uncached", "prompt_cached", "out_tokens"):
                use[k] = (use.get(k) or 0) + (final_usage.get(k) or 0)
            use["steps"] = (use.get("steps") or 0) + calls_u["calls"]
            chat.update_meta(usage=use, ctx={"tokens": final_usage["ctx_tokens"], "limit": ep.get("context"), "by": ep["id"]})
            flush_prose(final_usage)
            state["completed"] = True
            break                                    # turn finished: do not block forever if a background child inherited stdout

    done_ev.set()
    flush_prose(final_usage)
    try:
        proc.stdout.close()
    except Exception:
        pass
    if state.get("completed"):
        if proc.poll() is None:
            try:
                proc.wait(timeout=POST_TURN_GRACE_S)
            except subprocess.TimeoutExpired:
                # Turn already finished (result event flushed), but agy hung (e.g. background child).
                # Terminate ONLY the CLI wrapper process so detached servers survive.
                osutil.terminate_only(proc.pid)
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
    else:
        proc.wait()
    for idx in list(seen_tools):                   # steps the process left unfinished (stopped, steered, cut off)
        agent.act_done(cid, "agy:%s" % idx)
    sd = agy_live.sysdir(TASK_LOGS, state["conv"])
    for mid, text in state["inbox"]:                # handed in but never read: the next turn gets it (not after Stop)
        if agy_live.withdraw(sd, mid) and carry is not None and not run.cancel.is_set():
            carry.append(text)
    from .chat_import import mark_engine_cursor
    mark_engine_cursor(chat, "agy")

    if run.cancel.is_set():
        return "cancelled"
    if state["steered"]:
        return "steered"
    if result_err:
        m = chat.append({"role": "error", "content": "Antigravity: " + result_err[:800]})
        emit(cid, {"type": "message", "message": m})
    elif proc.returncode and not state.get("completed") and err:
        m = chat.append({"role": "error", "content": "Antigravity stopped: " + " | ".join(err[-4:])[:800]})
        emit(cid, {"type": "message", "message": m})
    return "done"


def turn(run, chat, meta, ep, emit):
    from . import agent
    cid = chat.id
    msgs = chat.messages()
    last = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
    if not last:
        return
    try:
        agy_exe()
    except RuntimeError as e:
        m = chat.append({"role": "error", "content": str(e)})
        emit(cid, {"type": "message", "message": m})
        return
    text = last.get("model_content") or last.get("content") or ""
    first = True
    from . import agy_acp
    if agy_acp.enabled(ep):
        # Over acp a message sent meanwhile goes straight into the running turn and nothing is cut (agy_acp.py).
        text = _first_text(chat, ep, emit, text)
        first = False
        meta = chat.meta()
        level = effort.resolve(ep, meta, meta.get("reasoning") or config.get().get("reasoning"))
        try:
            outcome = agy_acp.turn(run, chat, ep, emit, text, _model_arg(ep, level), effort.label(ep, level), steered_text,
                                   files=last.get("files"))
            if outcome != "error":
                from .chat_import import mark_engine_cursor
                mark_engine_cursor(chat, "agy")
            _note_conv(chat)
            return
        except agy_acp.Unavailable as e:
            effort.log_cmd(cid, ["agy acp unavailable, print mode:", str(e)[:300]])
    while not run.cancel.is_set():
        carry = []
        outcome = _one_turn(run, chat, ep, emit, text, first, carry)
        first = False
        if outcome == "cancelled" or run.cancel.is_set():
            break
        if run.queue or carry:
            if outcome == "steered":
                nm = chat.append({"role": "notice",
                                  "content": "Antigravity CLI cannot take input mid-step: interrupted the turn and "
                                             "resumed the conversation with your message (work so far is kept)."})
                emit(cid, {"type": "message", "message": nm})
            items = agent.take_queue(run)
            parts = list(carry)                      # already in the chat: shown when they went to the inbox
            for item in items:
                um = agent.user_message(chat, chat.meta(), item["text"], item["files"])
                if item.get("steer"):
                    um["steer"] = True
                m = chat.append(um)
                emit(cid, {"type": "message", "message": agent._public(m), "queued_id": item.get("id")})
                parts.append(m.get("model_content") or m.get("content") or "")
            text = steered_text("\n\n".join(p for p in parts if p))
            if not text:
                break
            continue
        break
    _note_conv(chat)
