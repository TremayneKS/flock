"""Neon (agy) over the Agent Client Protocol: `agy acp`, one process per chat, kept while its session has work.

Why not print mode (agy_engine._one_turn): proven on scratch conversations, 22 Nov 2026 --
- print mode holds its stdout back behind a background command or a timer (the model's words never reached the chat),
  and the only way to hand it the user's message mid-turn was to end the process, which ends every command it started;
  its inbox is read only at the next wake (a 10-minute timer kept a message waiting 10 minutes);
- over acp a second session/prompt steers the running turn: a question sent while a background command and a timer ran
  was answered in words in 8.6 s, the command kept running, and nothing was cut. A prompt returns end_turn while its
  background work goes on (the chat is free), and a timer firing later wakes the session by itself, with no prompt
  outstanding: its words and tool rows are shown as they come (a wake). The stream is live, never held back.

The process stays while its session has a tool still running (a background command, a timer) or was used in the
last IDLE_CLOSE seconds; closing it ends its background work, so it is never closed while something runs. Stop sends
session/cancel (agy then stops the session's background work too); if the prompt does not end, the process is ended.
A restart of the app ends the process (its stdin closes), as it always did; a job that must outlive that has to be
started outside agy. If `agy acp` cannot start or load the conversation, the turn runs in print mode as before.

Standard library only.
"""
import json
import os
import subprocess
import threading
import time

from . import config, osutil, store

IDLE_CLOSE = 900        # seconds an idle session with nothing running is kept (the next message then needs no reload)
CANCEL_GRACE = 8        # seconds a cancelled prompt gets to end before the process is ended
START_TIMEOUT = 90      # initialize + session/load of a long conversation
WAKE_QUIET = 3.0        # seconds without an update before a wake's words are written to the chat
STREAM_QUIET = 2.0      # seconds without a chunk before reply text is flushed during a turn (reloads/phone sync see it)
# session/load replays the conversation's history as updates. On a long conversation written by print mode agy goes on
# replaying after it has answered the load (22 Nov 13:32-13:50: 840 to 1,300 old rows dumped into four Neon chats at
# their first acp turn, the old final report among them, and the turn ended when the replay did). So after the load the
# session waits for the updates to stop before the prompt goes in, and drops a replay that still comes later.
LOAD_QUIET = 1.5        # seconds without an update that end the replay after a load
LOAD_DRAIN_MAX = 120    # at most this long
REPLAY_QUIET = 1.0      # a replay seen during a turn: updates are dropped until they stop for this long
TURN_QUIET = 1.0        # a turn whose prompts have all come back ends once the session has been quiet this long
REAP_TICK = 30          # how often idle sessions are looked at
STALE_TOOL_H = 6        # a tool call never reported finished counts as running for at most this long

_sessions = {}
_lock = threading.Lock()


class Unavailable(Exception):
    """acp could not be started for this chat: the turn runs in print mode instead."""


class Session:
    """One `agy acp` process and the session it holds for one chat."""

    def __init__(self, cid, cwd, exe):
        self.cid, self.cwd = cid, cwd
        # --dangerously-skip-permissions is required on `agy acp` itself (matching print mode in agy_engine.py):
        # `_answer()` only receives `session/request_permission` callbacks for the parent ACP session, while child
        # subagents spawned via agy's built-in `invoke_subagent` do not forward permission requests over ACP and
        # stall indefinitely in waiting_for_input on run_command or out-of-workspace read_file without this flag.
        self.proc = subprocess.Popen([exe, "acp", "--dangerously-skip-permissions"], cwd=cwd,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, **osutil.group())
        self.wlock = threading.Lock()
        self.nid = 0
        self.waiting = {}                    # request id -> {"ev": Event, "msg": the response}
        self.sid = None
        self.model = None
        self.ep = None
        self.supports_image = False
        self.sink = None                     # the turn showing updates now; None: a wake shows them
        self.wake = None
        self.tools = {}                      # tool call id -> when it started: still running in the session
        self.last = time.time()
        self.loading = False
        self.err = []
        self.closed = False
        self.replay_until = 0.0              # updates before this are a late history replay (dropped)
        self.replay_ids = set()              # tool calls seen in a replay: their later updates are old too
        self.known_user = set()              # what the user's earlier messages said (a replayed one is recognised)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    # -- wire --------------------------------------------------------------------------------------------------
    def alive(self):
        return not self.closed and self.proc.poll() is None

    def _write(self, msg):
        with self.wlock:
            try:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (OSError, ValueError):
                raise Unavailable("agy acp is not reading its input")

    def send(self, method, params):
        """A request; returns its id (wait on it with result())."""
        with self.wlock:
            self.nid += 1
            rid = self.nid
            self.waiting[rid] = {"ev": threading.Event(), "msg": None}
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def done(self, rid):
        w = self.waiting.get(rid)
        return bool(w and w["ev"].is_set())

    def result(self, rid, timeout=None):
        """The response of request rid: its result, or Unavailable for an error or a process that ended."""
        w = self.waiting[rid]
        if not w["ev"].wait(timeout):
            raise Unavailable("no answer to request %s" % rid)
        self.waiting.pop(rid, None)
        m = w["msg"] or {}
        if "error" in m or not m:
            raise Unavailable(str((m.get("error") or {}).get("message") or m.get("error") or "agy acp ended"))
        return m.get("result")

    def request(self, method, params, timeout=60):
        return self.result(self.send(method, params), timeout)

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_err(self):
        try:
            for raw in self.proc.stderr:
                self.err = (self.err + [raw.decode("utf-8", "replace").rstrip()])[-20:]
        except (OSError, ValueError):
            pass

    def _read(self):
        try:
            for raw in self.proc.stdout:
                try:
                    m = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                if "id" in m and ("result" in m or "error" in m) and m["id"] in self.waiting:
                    w = self.waiting[m["id"]]
                    w["msg"] = m
                    w["ev"].set()
                elif "id" in m and m.get("method"):
                    self._answer(m)
                elif m.get("method") == "session/update":
                    self._update((m.get("params") or {}).get("update") or {})
        except (OSError, ValueError):
            pass
        self.closed = True
        for w in list(self.waiting.values()):
            w["ev"].set()
        self._show(None)

    def _answer(self, m):
        """agy asking the client: a tool permission (allowed, as --dangerously-skip-permissions does in print mode)."""
        if m.get("method") == "session/request_permission":
            opts = (m.get("params") or {}).get("options") or []
            pick = next((o for o in opts if o.get("kind") == "allow_always"), None) or \
                next((o for o in opts if "allow" in str(o.get("kind") or "")), None) or (opts[0] if opts else None)
            res = {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}} if pick else \
                {"outcome": {"outcome": "cancelled"}}
            self._write({"jsonrpc": "2.0", "id": m["id"], "result": res})
        else:
            self._write({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported"}})

    def _update(self, u):
        now = time.time()
        self.last = now
        k = u.get("sessionUpdate")
        tid = u.get("toolCallId")
        if self.loading:                     # session/load replays the history first: shown long ago
            if tid:
                self.replay_ids.add(tid)
            return
        if k == "user_message_chunk" and self._old_user(u):
            self.replay_until = now + REPLAY_QUIET   # agy replaying history late: one of the user's old messages
        if now < self.replay_until:
            self.replay_until = now + REPLAY_QUIET
            if tid:
                self.replay_ids.add(tid)
            return
        if tid and tid in self.replay_ids:
            return
        if k == "tool_call" and tid and u.get("status") not in ("completed", "failed"):
            self.tools[tid] = time.time()
        elif k in ("tool_call", "tool_call_update") and tid and u.get("status") in ("completed", "failed"):
            self.tools.pop(tid, None)
        self._show(u)

    def _old_user(self, u):
        t = str((u.get("content") or {}).get("text") or "").strip() if isinstance(u.get("content"), dict) else ""
        if len(t) < 12:
            return False
        head = t[:200]
        return any(head in k for k in self.known_user)

    def _show(self, u):
        sink = self.sink
        if sink:
            sink(u)
            return
        if u is None:
            if self.wake:
                self.wake.flush()
            return
        if u.get("sessionUpdate") not in View.SHOWN:
            return
        if not self.wake:                    # agy woke by itself (a timer fired, a task ended): show it as it comes
            chat = store.Chat(self.cid)
            from . import agent
            ep = self.ep
            if not ep:
                try:
                    ep = config.endpoint(chat.meta().get("endpoint"))
                except LookupError:
                    ep = None
            self.wake = View(chat, ep or {"id": "neon"}, agent.emit)
            threading.Thread(target=self._wake_flusher, daemon=True).start()
        self.wake.on(u)

    def _wake_flusher(self):
        while self.wake and not self.sink:
            time.sleep(0.5)
            w = self.wake
            if w and time.time() - self.last >= WAKE_QUIET:
                am = w.flush()
                if am:
                    try:                     # advance cursor only if no user/other-model message intervened while away
                        from .chat_import import mark_engine_cursor
                        mark_engine_cursor(store.Chat(self.cid), "agy", msg_id=am.get("id"), only_if_contiguous=True)
                    except Exception:
                        pass
                if time.time() - self.last >= 30 and not w.open_calls:
                    self.wake = None
                    return

    # -- session ---------------------------------------------------------------------------------------------
    def running(self):
        """Tool calls still running in the session (background commands, timers), not counting ones long stale."""
        now = time.time()
        return [t for t, s in list(self.tools.items()) if now - s < STALE_TOOL_H * 3600]

    def take(self, sink):
        """A turn starts showing the session's updates (a wake in progress is written out first)."""
        w, self.wake = self.wake, None
        if w:
            w.flush()
        self.sink = sink

    def close(self):
        self.closed = True
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(5)
        except Exception:
            osutil.kill_tree(self.proc.pid)


def _map_tool(name, kind):
    from .agy_engine import _map_tool_name
    if name:
        return _map_tool_name(name)
    return {"read": "Read", "edit": "Edit", "execute": "Bash", "search": "Grep", "fetch": "WebFetch"}.get(kind or "", "tool")


def _content_text(u):
    out = []
    for c in u.get("content") or []:
        if isinstance(c, dict):
            inner = c.get("content") if isinstance(c.get("content"), dict) else c
            if inner.get("type") == "text" and inner.get("text"):
                out.append(str(inner["text"]))
    if not out and u.get("rawOutput") is not None:
        ro = u["rawOutput"]
        out.append(ro if isinstance(ro, str) else json.dumps(ro, ensure_ascii=False))
    return "\n".join(out)


class View:
    """The session's updates as chat rows: text and thinking, a row per tool call, the page's activity line."""
    SHOWN = ("agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update")

    def __init__(self, chat, ep, emit):
        self.chat, self.ep, self.emit, self.cid = chat, ep, emit, chat.id
        self.text, self.think = [], []
        self.temp = None
        self.last_chunk_at = 0.0
        self.calls = {}                      # tool call id -> (name, args)
        self.open_calls = set()
        self.lock = threading.Lock()

    def on(self, u):
        if u is None:
            return
        k = u.get("sessionUpdate")
        if k == "agent_message_chunk":
            t = str((u.get("content") or {}).get("text") or "")
            if t:
                with self.lock:
                    self._step()
                    self.text.append(t)
                    self.last_chunk_at = time.time()
                self.emit(self.cid, {"type": "delta", "id": self.temp, "text": t})
        elif k == "agent_thought_chunk":
            t = str((u.get("content") or {}).get("text") or "")
            if t:
                with self.lock:
                    self._step()
                    self.think.append(t)
                    self.last_chunk_at = time.time()
                self.emit(self.cid, {"type": "reasoning", "id": self.temp, "text": t})
        elif k in ("tool_call", "tool_call_update"):
            self._tool(u)

    def _step(self):
        if not self.temp:
            self.temp = store.new_id("t")
            self.emit(self.cid, {"type": "step", "id": self.temp, "endpoint": self.ep.get("id")})

    def _tool(self, u):
        from . import agent
        tid = u.get("toolCallId") or store.new_id("acp")
        st = u.get("status")
        if tid not in self.calls:
            self.flush()
            name = _map_tool(u.get("name"), u.get("kind"))
            args = u.get("args") if isinstance(u.get("args"), dict) else (u.get("rawInput") or {})
            if not isinstance(args, dict):
                args = {"input": args}
            if not args and u.get("title"):
                args = {"title": u["title"]}
            self.calls[tid] = (name, args)
            if st not in ("completed", "failed"):
                self.open_calls.add(tid)
                self.emit(self.cid, {"type": "waiting", "text": "Running %s" % name})
                until = None
                if (u.get("name") == "schedule" or name == "schedule") and args.get("DurationSeconds"):
                    try:
                        until = time.time() + float(args["DurationSeconds"])
                    except (TypeError, ValueError):
                        until = None
                from . import activity
                what = ("Timer: " + str(args.get("Prompt") or "%ss" % args["DurationSeconds"])) if until else \
                    activity.summary(name, args)
                agent.act_start(self.cid, "acp:" + tid, what, until=until, bg=True)
        if st in ("completed", "failed"):
            name, args = self.calls.get(tid, (_map_tool(u.get("name"), u.get("kind")), {}))
            self.open_calls.discard(tid)
            agent.act_done(self.cid, "acp:" + tid)
            self._row(name, args, _content_text(u) or st, ok=(st == "completed"))

    def _row(self, name, args, result, ok=True):
        call = {"id": store.new_id("ag"), "name": name, "arguments": json.dumps(args, ensure_ascii=False)}
        am = self.chat.append({"role": "assistant", "content": "", "endpoint": self.ep.get("id"), "tool_calls": [call]})
        self.emit(self.cid, {"type": "assistant", "temp": None, "message": am})
        self.emit(self.cid, {"type": "tool_start", "call": {"id": call["id"], "name": name,
                                                             "args": call["arguments"][:4000]}})
        tm = self.chat.append({"role": "tool", "tool_call_id": call["id"], "name": name,
                               "content": str(result or "")[:30_000], "ok": ok, "args": call["arguments"][:20_000]})
        self.emit(self.cid, {"type": "message", "message": tm})

    def flush_if_quiet(self, quiet_s=STREAM_QUIET):
        """Commit buffered visible reply text once chunks stop arriving, even if a prompt/background job stays open."""
        with self.lock:
            ready = bool(self.text) and self.last_chunk_at > 0 and (time.time() - self.last_chunk_at >= quiet_s)
        if ready:
            return self.flush()
        return None

    def flush(self, stats=None):
        with self.lock:
            body, thought = "".join(self.text).strip(), "".join(self.think).strip()
            self.text, self.think = [], []
            temp, self.temp = self.temp, None
            self.last_chunk_at = 0.0
        if not body and not thought:
            return None
        am = {"role": "assistant", "content": body, "endpoint": self.ep.get("id")}
        if thought:
            am["reasoning_content"] = thought
        if stats:
            am["stats"] = stats
        am = self.chat.append(am)
        self.emit(self.cid, {"type": "assistant", "temp": temp, "message": am, "ctx": self.chat.meta().get("ctx")})
        return am


def enabled(ep):
    if os.environ.get("KCH_AGY_ACP", "").strip() in ("0", "off", "no"):
        return False
    return ep.get("acp", config.get().get("agy_acp", True)) not in (False, 0, "0", "off")


def session(chat, cwd):
    """The chat's live session, or a new process with the chat's conversation loaded (Unavailable if that fails)."""
    from .agy_engine import agy_exe
    cid = chat.id
    with _lock:
        s = _sessions.get(cid)
        if s and s.alive() and s.cwd == cwd:
            return s
        _sessions.pop(cid, None)
    if s:
        s.close()
    try:
        s = Session(cid, cwd, agy_exe())
    except (OSError, RuntimeError) as e:
        raise Unavailable(str(e))
    try:
        init_res = s.request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, START_TIMEOUT) or {}
        s.supports_image = bool(((init_res.get("agentCapabilities") or {}).get("promptCapabilities") or {}).get("image"))
        conv = chat.meta().get("agy_conversation")
        from . import subagents
        # the harness MCP server, as Claude Code and Codex get it: harness_agent (subagents the user sees in the app),
        # harness_chats, and in a subagent chat harness_report_to_main / harness_parent_context
        servers = subagents.acp_mcp(chat, chat.meta())
        if conv:
            users = [str(m.get("model_content") or m.get("content") or "") for m in chat.messages() if m.get("role") == "user"]
            s.known_user = set(users[:-1])   # not the message this turn sends: its answer must never look like a replay
            s.loading = True
            try:
                s.request("session/load", {"sessionId": conv, "cwd": cwd, "mcpServers": servers}, START_TIMEOUT)
                end = time.time() + LOAD_DRAIN_MAX
                while time.time() - s.last < LOAD_QUIET and time.time() < end and s.alive():
                    time.sleep(0.1)          # the replay still coming after the load's answer
            finally:
                s.loading = False
            s.sid = conv
        else:
            r = s.request("session/new", {"cwd": cwd, "mcpServers": servers}, START_TIMEOUT) or {}
            s.sid = r.get("sessionId")
            if not s.sid:
                raise Unavailable("agy acp gave no session id")
            chat.update_meta(agy_conversation=s.sid)
    except Unavailable:
        s.close()
        raise
    with _lock:
        _sessions[cid] = s
    _reaper_start()
    return s


def live(cid):
    s = _sessions.get(cid)
    return s if s and s.alive() else None


def running(cid):
    """The chat's background work still running in its live session (tool call ids), [] when none."""
    s = live(cid)
    return s.running() if s else []


def close(cid):
    with _lock:
        s = _sessions.pop(cid, None)
    if s:
        s.close()


def kill(cid):
    with _lock:
        s = _sessions.pop(cid, None)
    if s:
        s.closed = True
        osutil.kill_tree(s.proc.pid)


_reaper = [None]


def _reaper_start():
    if _reaper[0] and _reaper[0].is_alive():
        return

    def loop():
        while True:
            time.sleep(REAP_TICK)
            for cid, s in list(_sessions.items()):
                if not s.alive():
                    _sessions.pop(cid, None)
                elif not s.sink and not s.running() and time.time() - s.last > IDLE_CLOSE:
                    close(cid)
    _reaper[0] = threading.Thread(target=loop, daemon=True)
    _reaper[0].start()


def _acp_images(files):
    """Build native ACP image content blocks for attached images under vision.MAX_BYTES."""
    from . import vision
    out = []
    for f in files or []:
        if isinstance(f, dict) and f.get("kind") == "image" and f.get("path"):
            try:
                if os.path.getsize(f["path"]) <= vision.MAX_BYTES:
                    out.append({
                        "type": "image",
                        "mimeType": vision.mime(f["path"]),
                        "data": vision.data_url(f["path"]).split(",", 1)[1],
                    })
            except OSError:
                pass
    return out


def turn(run, chat, ep, emit, text, model_name, level_label, steered_text, files=None):
    """One turn over acp: the prompt, and every message the user sends meanwhile as a prompt of its own (it steers the
    running turn; nothing is cut). Ends when every prompt has come back. Raises Unavailable before anything was sent."""
    from . import agent, effort, resume as turn_record
    cid = chat.id
    meta = chat.meta()
    cwd = meta.get("project") or str(config.WORKSPACE)
    if not os.path.isdir(cwd):
        cwd = str(config.WORKSPACE)
        os.makedirs(cwd, exist_ok=True)
    s = session(chat, cwd)
    s.ep = ep
    if s.model != model_name:
        try:
            s.request("session/set_model", {"sessionId": s.sid, "modelId": model_name}, 30)
            s.model = model_name
        except Unavailable:
            pass                              # agy keeps the conversation's model
    effort.log_cmd(cid, ["agy", "acp", "session/prompt", s.sid, model_name])
    turn_record.engine(run, s.proc)
    view = View(chat, ep, emit)
    s.take(view.on)
    t0 = time.time()
    usage = {"in": 0, "cached": 0, "out": 0, "think": 0, "calls": 0}
    outstanding = []
    err = None
    cancelled_at = None

    def prompt(t, att_files=None):
        blocks = []
        if s.supports_image and att_files:
            blocks.extend(_acp_images(att_files))
        blocks.append({"type": "text", "text": t})
        outstanding.append(s.send("session/prompt", {"sessionId": s.sid, "prompt": blocks}))

    try:
        prompt(text, files)
        back_at = None                                # when the last prompt came back
        while outstanding or back_at:
            if outstanding:
                back_at = None
            for rid in [r for r in outstanding if s.done(r)]:
                outstanding.remove(rid)
                try:
                    res = s.result(rid, 1) or {}
                    u = res.get("usage") or {}
                    usage["in"] += int(u.get("inputTokens") or 0)
                    usage["cached"] += int(u.get("cachedReadTokens") or 0)
                    usage["out"] += int(u.get("outputTokens") or 0)
                    usage["think"] += int(u.get("thoughtTokens") or 0)
                    usage["calls"] += 1
                    if res.get("stopReason") == "refusal":
                        err = "the model refused"
                except Unavailable as e:
                    if not run.cancel.is_set():
                        err = str(e)
            # Commit finished reply text on quiet while a prompt/background job stays open; when outstanding is
            # empty the loop exits within TURN_QUIET and finally flushes with full usage stats attached.
            if outstanding:
                flushed = view.flush_if_quiet(STREAM_QUIET)
                if flushed:
                    try:
                        from .chat_import import mark_engine_cursor
                        mark_engine_cursor(chat, "agy", msg_id=flushed.get("id"))
                    except Exception:
                        pass
            if not outstanding:
                if run.queue and not run.cancel.is_set():
                    pass                              # a message (or a late report) that came as the last one ended
                elif (time.time() - max(s.last, back_at or time.time()) < TURN_QUIET and s.alive()
                      and not run.cancel.is_set()):
                    back_at = back_at or time.time()   # still streaming: not over until the session is quiet
                else:
                    break
            if run.cancel.is_set():
                if cancelled_at is None:
                    cancelled_at = time.time()
                    try:
                        s.notify("session/cancel", {"sessionId": s.sid})
                    except Unavailable:
                        pass
                elif time.time() - cancelled_at > CANCEL_GRACE:
                    kill(cid)                         # it did not stop: end it (its background work with it)
                    break
            elif run.queue:
                # Flush any buffered assistant text BEFORE appending the user's new message so chronology stays intact.
                flushed_pre = view.flush()
                if flushed_pre:
                    try:
                        from .chat_import import mark_engine_cursor
                        mark_engine_cursor(chat, "agy", msg_id=flushed_pre.get("id"))
                    except Exception:
                        pass
                items = agent.take_queue(run)
                parts = []
                q_files = []
                for item in items:
                    if item.get("soft"):
                        if item.get("id"):
                            emit(cid, {"type": "unqueued", "id": item.get("id")})
                        parts.append(item.get("text") or "")
                        continue
                    um = agent.user_message(chat, chat.meta(), item["text"], item["files"])
                    um["steer"] = True
                    m = chat.append(um)
                    emit(cid, {"type": "message", "message": agent._public(m), "queued_id": item.get("id")})
                    parts.append(steered_text(m.get("model_content") or m.get("content") or ""))
                    if m.get("files"):
                        q_files.extend(m["files"])
                body = "\n\n".join(p for p in parts if p)
                if body:
                    prompt(body, q_files)             # straight in: it steers the running turn
            if not s.alive():
                if not run.cancel.is_set():
                    err = "Antigravity stopped: " + " | ".join(s.err[-4:])[:800] if s.err else "Antigravity stopped"
                break
            time.sleep(0.1)
    finally:
        stats = {"model": model_name, "prompt_new": usage["in"], "prompt_uncached": usage["in"],
                 "prompt_cached": usage["cached"], "out_tokens": usage["out"], "secs": round(time.time() - t0, 1),
                 "effort": level_label}
        if usage["calls"] > 1:
            stats["calls"] = usage["calls"]
        view.flush(stats if usage["calls"] else None)
        s.sink = None
        if usage["calls"]:
            use = chat.meta().get("usage") or {}
            for k in ("prompt_new", "prompt_uncached", "prompt_cached", "out_tokens"):
                use[k] = (use.get(k) or 0) + (stats.get(k) or 0)
            use["steps"] = (use.get("steps") or 0) + usage["calls"]
            chat.update_meta(usage=use)
        for tid in list(view.open_calls):
            if tid not in s.tools or not s.alive():      # (one still running in the session stays on the page)
                agent.act_done(cid, "acp:" + tid)
    if err and not run.cancel.is_set():
        m = chat.append({"role": "error", "content": ("Antigravity: " + err)[:800]})
        emit(cid, {"type": "message", "message": m})
        if not usage["calls"]:
            return "error"
    return "cancelled" if run.cancel.is_set() else "done"
