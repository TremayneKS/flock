"""Claude as a plug-in: the Claude Code CLI with the user's own Claude plan (whatever `claude` is signed in with), with
its own tools, run in the chat's project folder. One Claude session per chat (--session-id, then --resume). Its stream
is turned into the harness's messages, so the chat looks the same whichever model answers. The user's own Claude Code
setup (CLAUDE.md files, hooks, allow-lists, auto-memory) is not loaded: the harness gives the same short context to
every model."""
import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import config, effort, osutil

# no hooks and no auto-memory (a memory index would otherwise ride along in every prompt)
SETTINGS = json.dumps({"disableAllHooks": True, "switchModelsOnFlag": False, "autoMemoryEnabled": False})
# Hard-block Claude Code's hidden built-in subagents so chats only use visible in-app harness_agent subagents.
DISALLOWED_TOOLS = "Agent,Task"
DEFAULT_AUTOCOMPACT = 200_000
DEFAULT_OPUS_AUTOCOMPACT = DEFAULT_AUTOCOMPACT
SUBAGENT_RULE = "For subagents, use only the in-app harness_agent tool (never built-in Agent or Task)."
STUCK_S = 10                     # a message of the user's still waiting after this long is forced through (see killer)
IDLE_CLOSE_S = 20                # Claude idle, nothing of its left in the background, no turn begun: its input closes after this


def _autocompact_val(ep):
    """Token threshold for --autocompact: explicit endpoint setting, or 200k default for all Claude Code models."""
    ep = ep or {}
    if "autocompact" in ep:
        raw = ep.get("autocompact")
        if raw in (None, "", False, 0, "0"):
            return None
        try:
            v = int(raw)
            return str(v) if v > 0 else None
        except (TypeError, ValueError):
            return str(raw).strip() or None
    return str(DEFAULT_AUTOCOMPACT)

# Variables a Claude Code host (the desktop app, an IDE, another `claude`) sets for its own session. A harness started
# from inside such a session inherits them, and the `claude -p` it runs would then think it is that session's child:
# extra tools (AskUserQuestion), the host's auth refresh, its message socket. The child gets its own clean session.
HOST_VARS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID",
             "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
             "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
             "CLAUDE_CODE_TERMINAL_MCP_TOOLS", "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL", "CLAUDE_CODE_DESKTOP_MESSAGE_CARD",
             "CLAUDE_CODE_DESKTOP_APP_VERSION", "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES", "CLAUDE_CODE_EMIT_STARTUP_TIMING",
             "CLAUDE_CODE_STARTUP_FAILURE_RESULTS", "CLAUDE_CODE_REPORT_FINDINGS", "CLAUDE_CODE_EAGER_FLUSH",
             "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING", "CLAUDE_CODE_HOST_SHORT_TEMP_DIRS", "CLAUDE_CODE_OAUTH_SCOPES",
             "CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "CLAUDE_CODE_DISABLE_CRON", "CLAUDE_CODE_SIMPLE",
             "CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_ACCOUNT_UUID", "CLAUDE_CODE_ORGANIZATION_UUID")


def clean_env():
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY" and k not in HOST_VARS}


def short_model(name):
    """claude-sonnet-4-5 -> Sonnet 4.5 (for notices)."""
    parts = str(name or "").replace("claude-", "").split("-")
    return (parts[0].capitalize() + " " + ".".join(p for p in parts[1:] if p.isdigit())).strip() if parts[0] else "Claude"


def _claude_ids():
    return {e["id"] for e in config.get().get("endpoints") or [] if e.get("kind") == "claude-code"}


def usage_stats(u):
    """Claude's usage as the harness's numbers. prompt_new (uncached + written) stays for older pages and totals."""
    unc, wrote, read = u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0, u.get("cache_read_input_tokens") or 0
    return {"prompt_new": unc + wrote, "prompt_uncached": unc, "prompt_written": wrote, "prompt_cached": read,
            "out_tokens": u.get("output_tokens") or 0, "ctx_tokens": unc + wrote + read}


def claude_exe():
    exe = config.claude_exe()
    if not exe:
        raise RuntimeError("the claude CLI is not installed (npm i -g @anthropic-ai/claude-code, then run `claude` once to sign in)")
    return exe


def _images(msg):
    """Images pasted or attached to the message, as Claude's own image blocks."""
    from . import vision
    out = []
    for f in msg.get("files") or []:
        if f.get("kind") == "image":
            try:
                if os.path.getsize(f["path"]) <= vision.MAX_BYTES:
                    out.append({"type": "image", "source": {"type": "base64", "media_type": vision.mime(f["path"]),
                                                            "data": vision.data_url(f["path"]).split(",", 1)[1]}})
            except OSError:
                pass
    return out


def adopt(run, chat, ep, emit, rec):
    """After a restart (agent._carry_on): the Claude Code process the old copy started went on with its turn (its events
    go to a file, and it finishes a turn even when its input closes). Follow it to its end, or, if it has ended too,
    take in what it wrote after the old copy last read. Returns True if it finished the turn, False if not, None if
    there is nothing to follow."""
    from . import resume as turn_record
    e = rec.get("engine") or {}
    out_f, err_f = turn_record.engine_files(chat.id)
    if not e.get("pid") or not out_f.exists():
        return None
    live = osutil.same_process(e["pid"], e.get("created"))
    pos = int(rec.get("engine_pos") or 0)
    turn_record.note(run, engine=e if live else None, engine_pos=pos)
    meta = chat.meta()
    level = effort.resolve(ep, meta, meta.get("reasoning") or config.get().get("reasoning"))
    proc = turn_record.Running(e["pid"], e.get("created") if live else -1)
    # Started through cli_input.py: its input is still open, so the user's messages go into this same process and the
    # background agents and commands it runs live on (no inbox: a process an older copy started, input gone with it).
    inbox = inbox_file(chat.id) if live and e.get("inbox") and _inbox_open(inbox_file(chat.id)) else None
    return _follow(run, chat, ep, emit, level, proc, out_f, err_f, pos, None, inbox=inbox,
                   idle=bool(rec.get("completed")))


def inbox_file(chat_id):
    """Where the app writes Claude Code's input (cli_input.py passes it on), next to its events file."""
    from . import resume as turn_record
    return turn_record.engine_files(chat_id)[0].with_name("engine.in")


def _inbox_open(path):
    """The inbox is there and its input was not closed (its last line is not cli_input.END)."""
    from . import cli_input
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 256))
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return False
    return not lines or lines[-1] != cli_input.END


def end_leftover(chat, meta):
    """One session, one process. A Claude Code process an earlier turn of this chat left running (the app lost track
    of it over a restart: 21 Nov 08:40, two copies of one chat worked at once, one of them unseen) is ended before a
    new one starts. True when one was ended."""
    old = (meta or {}).get("claude_proc") or {}
    if not (old.get("pid") and osutil.same_process(old["pid"], old.get("created"))):
        return False
    from . import subagents
    subagents.detach_calls(chat.id)                  # its subagents keep working; their reports come in later
    osutil.kill_tree(old["pid"])
    for _ in range(20):
        if not osutil.alive(old["pid"]):
            break
        time.sleep(0.25)
    return True


def turn(run, chat, meta, ep, emit):
    cid = chat.id
    from . import resume as turn_record
    if end_leftover(chat, meta):
        nm = chat.append({"role": "notice", "content": "An earlier Claude process of this chat was still running "
                          "unseen; it was ended so only one works on this chat."})
        emit(cid, {"type": "message", "message": nm})
    out_f, err_f = turn_record.engine_files(cid)
    msgs = chat.messages()
    last = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
    if not last:
        return
    sid = meta.get("claude_session")
    resume = bool(sid)
    if not sid:
        sid = str(uuid.uuid4())
        chat.update_meta(claude_session=sid)
    level = effort.resolve(ep, meta, meta.get("reasoning") or config.get().get("reasoning"))   # this turn's native level
    from . import agent, subagents
    note = (f"You are running inside Flock. This chat's notes file is {chat.notes_path}: keep short bullets there of "
            "the goal, decisions, key files and open tasks. " + SUBAGENT_RULE + " " + agent.WAIT_RULE + " A message from the user can arrive "
            "while you work: read it and follow it. " + agent.MEDIA_RULE + " " + agent.VISUAL_RULE) + (subagents.sub_note_for(meta) if meta.get("sub_of") else "")
    ac_val = _autocompact_val(ep)
    cmd = [claude_exe(), "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
           "--include-partial-messages", *(["--model", ep["model"]] if ep.get("model") else []),
           "--effort", level,
           # where Claude Code compacts this model's session, in tokens (profile: "autocompact"); Opus defaults to 200k
           *(["--autocompact", ac_val] if ac_val else []),
           "--disallowedTools", DISALLOWED_TOOLS,
           "--setting-sources", "", "--settings", SETTINGS, "--strict-mcp-config", "--dangerously-skip-permissions",
           "--append-system-prompt", note]
    cmd += ["--resume", sid] if resume else ["--session-id", sid]
    cwd = meta.get("project") or str(config.WORKSPACE)
    env = clean_env()
    last_model = meta.get("claude_model")
    if resume and last_model and ep.get("model") and last_model != ep["model"]:
        # Each model has its own cache: the first request after a switch reads the whole session uncached (Claude Code
        # docs, prompt caching > Switching models). Say so, with the size, so a switch is a choice and not a surprise.
        # Back to a model used in this session within the cache's hour: its cache still holds the chat up to when it left,
        # so it reads only the turns since.
        ctx = meta.get("ctx") or {}
        size = ctx.get("tokens") if str(ctx.get("by") or "") in _claude_ids() else None
        warm = time.time() - float((meta.get("claude_used") or {}).get(ep["model"]) or 0) < 3300
        frm, to = short_model(last_model), short_model(ep["model"])
        if warm:
            text = (f"Back to {to} from {frm}: its cache from earlier this hour is still warm, so it reads only the turns "
                    "since it last answered.")
        else:
            text = (f"Switched from {frm} to {to} in the same Claude session: it reads the whole chat once without the "
                    "cache" + (f" (about {size // 1000}k tokens)" if size else "") + ", then later turns are cached. "
                    f"Switching back to {frm} within an hour reuses its cache.")
        nm = chat.append({"role": "notice", "content": text})
        emit(cid, {"type": "message", "message": nm})
    from . import computer
    cmd += computer.claude_mcp_args(chat, meta, env)    # the Computer tool, through the app's approval and Stop
    srv = subagents.mcp_server(chat, meta)          # harness_agent: subagents on any connected model
    if srv:
        f = chat.dir / "harness-mcp.json"           # the token stays out of the file: the server inherits it from env
        f.write_text(json.dumps({"mcpServers": {"harness": {"type": "stdio", "command": srv["command"], "args": srv["args"]}}}),
                     encoding="utf-8")
        cmd += ["--mcp-config", str(f)]
        env.update(srv["env"])
        env["MCP_TOOL_TIMEOUT"] = str(int(srv["env"]["KCH_AGENT_TIMEOUT"]) * 1000)
    effort.log_cmd(cid, cmd)
    # Its events go to a file, not a pipe to this process: if the app restarts mid-turn, Claude Code finishes the turn
    # (it does even when its input closes) and the new copy follows the same file (adopt) instead of cutting it off.
    # Its input is not a pipe from this process either: cli_input.py holds it and passes on what the app appends to the
    # inbox file, so a new copy after a restart writes the user's next message into the same process, and the
    # background agents and commands that live inside it are not ended for it (23 Nov 2026).
    inbox = inbox_file(cid)
    for f in (inbox, out_f, err_f):
        open(f, "wb").close()                          # this process's own: nothing left from the last one
    from .subagents import _python
    holder = [_python(), "-I", str(Path(__file__).with_name("cli_input.py")), str(inbox), str(out_f), str(err_f),
              "--", *cmd]                              # -I: nothing of harness/ shadows what it imports
    with open(err_f, "ab") as fe:
        proc = subprocess.Popen(holder, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=fe, env=env,
                                **osutil.group())
    turn_record.engine(run, proc, inbox=True)          # a restart follows it, writing into its inbox (adopt)
    chat.update_meta(touch=False, claude_proc={"pid": proc.pid, "created": osutil.created(proc.pid)})   # see end_leftover
    text = last.get("model_content") or last.get("content") or ""
    if not resume and meta.get("parent_context"):
        text = meta["parent_context"] + "\n\n" + text
    from .chat_import import handover_prefix, mark_engine_cursor
    prior = handover_prefix(chat, "claude-code", resume, ep.get("name") or "Claude", emit)
    if prior:
        text = prior + text
    first = _images(last) + [{"type": "text", "text": text}]
    res = _follow(run, chat, ep, emit, level, proc, out_f, err_f, 0, first, locals().get("resume"), inbox=inbox)
    return bool(res)


def steer_log(cid, what, **state):
    """One line per steering decision (logs/steer.log), so a message that waited can be explained afterwards."""
    try:
        with open(config.LOGS / "steer.log", "a", encoding="utf-8") as f:
            f.write("%s %s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), cid, what,
                                        " ".join("%s=%s" % kv for kv in state.items())))
    except OSError:
        pass


_task_desc = {}                       # (chat id, task id) -> (description, output file, when it started)


def _bg_show(cid, task_id, since=None):
    """The page's line while Claude has finished writing and waits on a command of its own in the background:
    "Waiting on background: <command> · 12m", with the command's last output line (activity.py)."""
    from . import agent as _ag
    desc, out_file, t = _task_desc.get((cid, task_id), (str(task_id), None, time.time()))
    _ag.act_start(cid, "bg:" + str(task_id), "Waiting on background: " + desc, log=out_file, since=since or t)


def _bg_since_restart(cid, out_f, pos, bg, tasks=None):
    """After a restart the turn is followed from byte pos: the commands started before it and still running (no
    task_notification yet) are put back on the page, so the chat does not read "last step 40m" while it waits on them."""
    started, backgrounded, ended = {}, set(), set()
    try:
        with open(out_f, "rb") as f:
            data = f.read(pos)
    except OSError:
        return
    for raw in data.splitlines():
        if b'"task_' not in raw:
            continue
        try:
            ev = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        tid, sub = ev.get("task_id"), ev.get("subtype")
        if not tid:
            continue
        if sub == "task_started":
            started[tid] = ev
            if ev.get("is_backgrounded"):
                backgrounded.add(tid)
        elif sub == "task_updated":
            patch = ev.get("patch") or {}
            if patch.get("is_backgrounded"):
                backgrounded.add(tid)
            if patch.get("status") in ("completed", "failed", "killed", "stopped", "cancelled"):
                ended.add(tid)
        elif sub == "task_notification":
            ended.add(tid)
    for tid, ev in started.items():
        if tid in ended or tid not in backgrounded:
            continue
        bg.setdefault(tid, time.time())
        if tasks is not None:
            tasks[tid] = {"agent": ev.get("task_type") == "local_agent", "tool": ev.get("tool_use_id"), "fg": False}
        _task_desc[(cid, tid)] = (str(ev.get("description") or ev.get("command") or tid), ev.get("output_file") or None,
                                  time.time())
        _bg_show(cid, tid)


def _follow(run, chat, ep, emit, level, proc, out_f, err_f, pos, first, resume=None, allow_fable_fallback=False,
            inbox=None, idle=False):
    """Write `first` to Claude (None: a process taken over after a restart), then turn its events (out_f, from byte pos
    on) into the chat's messages until it ends. Returns True if Claude finished its turn. inbox: its input file
    (cli_input.py; None: its stdin, gone for a process taken over). idle: a taken-over process had answered and was up
    for its background work."""
    from . import resume as turn_record
    from . import agent, subagents
    from .chat_import import mark_engine_cursor
    cid = chat.id
    adopted = first is None
    t_adopt = time.time()
    st = {"n": 0, "completed": False, "idle": idle, "idle_t": time.time() if idle else 0.0, "took": False,
          "wrote_in": False, "stopped": {}, "close_at": 0.0}
    done_ev = threading.Event()
    POST_TURN_GRACE_S = 5.0

    def add(msg):
        st["n"] += 1
        return chat.append(msg)

    # The input stays open for the whole turn. A message the user sends while Claude works is read within seconds:
    # - a tool call is running: after agent.STEER_GRACE seconds (a sleep, a benchmark, a wait loop) the harness sends a
    #   stream-json interrupt (control_request); a quick call finishes first and the message goes in between steps;
    # - Claude is writing or thinking: after agent.GEN_GRACE seconds the same interrupt (the CLI keeps the text so far);
    # - Claude is done for now but commands it started in the background are still running (its process stays up for
    #   them and takes a turn by itself when each ends): the input is kept open and the message is written straight in.
    #   Measured with the real CLI: answered 1.6 s after it was written, the background command untouched.
    # The CLI ends an interrupted turn (a running call is rejected; a subagent behind it is NOT stopped, see
    # subagents.detach_calls) and stays up, and the message goes in as the next turn of the same session, context kept.
    # Until then the user can take it back. The input closes at a result with nothing waiting and nothing in the
    # background; a message that arrives after that is answered by a new turn (agent._turn's leftovers).
    feed = {"open": not adopted or inbox is not None, "lock": threading.Lock(), "interrupted": False}
    running = {}                     # tool_use id -> when it started (Claude's tool calls still waiting for a result)
    bg = {}                          # task id -> when it started: commands of Claude's still running (task_started / task_notification)
    tasks = {}                       # task id -> {"agent": a subagent, "fg": the main turn's own foreground shell command, "tool"}

    def send(obj):
        line = (json.dumps(obj) + "\n").encode()
        if inbox is not None:                        # cli_input.py passes it on
            if proc.poll() is not None:
                raise OSError("Claude has ended")
            with open(inbox, "ab") as f:
                f.write(line)
            return
        if proc.stdin is None:
            raise OSError("no input: a Claude process taken over after a restart")
        proc.stdin.write(line)
        proc.stdin.flush()

    def write(content):
        send({"type": "user", "message": {"role": "user", "content": content}})

    def close(why=""):
        if feed["open"] and why:
            steer_log(cid, "input shut: " + why, idle=st["idle"], calls=len(running), bg=",".join(bg) or "-",
                      waiting=len(run.queue))
        was_open, feed["open"] = feed["open"], False
        try:
            if inbox is not None:
                if was_open:
                    from . import cli_input
                    with open(inbox, "ab") as f:
                        f.write(cli_input.END + b"\n")
            elif proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass

    def steer():
        """Hand Claude everything the user queued: each message stored and shown individually, sent as one user turn
        so Claude reads all of them together and never closes stdin after the first."""
        from . import agent
        with feed["lock"]:
            if not feed["open"]:
                return
            items = agent.take_queue(run)
            if not items:
                return
            blocks = []
            for item in items:
                um = agent.user_message(chat, chat.meta(), item["text"], item["files"])
                if item.get("steer"):
                    um["steer"] = True               # sent while Claude worked: the page shows it inside the turn
                m = add(um)
                emit(cid, {"type": "message", "message": agent._public(m), "queued_id": item.get("id")})
                blocks.extend(_images(m))
                txt = m.get("model_content") or m.get("content") or ""
                if txt:
                    blocks.append({"type": "text", "text": txt})
            if not blocks:
                return
            try:
                write(blocks)
            except OSError:
                close("the message could not be written")

    if first:
        write(first)

    def state():
        return {"idle": st["idle"], "open": feed["open"], "cut": feed["interrupted"], "calls": len(running),
                "bg": len(bg), "waiting": len(run.queue), "waited": "%.0fs" % agent.steer_waited(run)}

    def killer():
        while proc.poll() is None and not done_ev.is_set():
            if run.cancel.wait(0.3):
                if done_ev.is_set():
                    return
                osutil.kill_tree(proc.pid)
                return
            if done_ev.is_set():
                return
            try:
                if watch() == "ended":
                    return
            except Exception:                        # this loop is the only way a waiting message gets in: it never dies
                import traceback
                try:
                    (config.LOGS / "errors.log").open("a", encoding="utf-8").write(
                        time.strftime("%F %T ") + "steering loop: " + traceback.format_exc() + "\n")
                except OSError:
                    pass

    def watch():
            try:
                since = min(running.values()) if running else None
            except (ValueError, RuntimeError):      # changed while we looked: look again next time round
                since = None
            if since and not feed["interrupted"] and agent.steer_due(run, since):
                interrupt()
            elif not since and not st["idle"] and feed["open"] and not feed["interrupted"] \
                    and agent.steer_waited(run) >= agent.GEN_GRACE:
                interrupt()                  # writing or thinking, no call running: cut it so the message is read now
            if st["idle"] and feed["open"]:
                if run.queue and not run.cancel.is_set():
                    st["idle"] = False
                    steer_log(cid, "written in while idle on background work", **state())
                    steer()                  # idle on background work: the message goes straight in
                elif not bg and time.time() - st["idle_t"] > IDLE_CLOSE_S:
                    with feed["lock"]:
                        close("background work ended and no turn followed")    # let Claude end
            if adopted and not feed["open"] and run.queue and agent.steer_due(run, t_adopt):
                # no input to write the message into: end the process; the turn resumes its session with the message
                # (only a process an older copy started without cli_input.py, or one whose input was closed)
                steer_log(cid, "taken-over process ended for the message", **state())
                subagents.detach_calls(cid)  # its subagents keep working; their reports come in later
                osutil.kill_tree(proc.pid)
                return "ended"
            # The guard. 21 Nov 2026, twice (15:22 and 16:07): Claude had answered and was up only for a command it
            # left in the background; the user's message would otherwise wait until stopped
            # the chat. Whatever this loop believes about Claude (cut sent and no result came back, not idle, input
            # shut), a message of the user's that has waited STUCK_S with the process up is forced through: written
            # straight into the input when it is open (the CLI takes it as its next turn), else the process is ended
            # so the turn below starts again with the message (agent._turn's leftovers).
            waited = agent.steer_waited(run)
            if waited >= STUCK_S and not run.cancel.is_set():
                if feed["open"] and (not running or waited >= 3 * STUCK_S):
                    steer_log(cid, "GUARD: waited too long, written straight in", **state())
                    feed["interrupted"] = False
                    st["idle"] = False
                    steer()
                elif not feed["open"]:
                    steer_log(cid, "GUARD: waited too long with the input shut, process ended for a new turn", **state())
                    subagents.detach_calls(cid)
                    osutil.kill_tree(proc.pid)
                    return "ended"

    def agents_running():
        """Claude Code's own subagents still working (Agent calls, in the background or not, and the agents they
        started). Its stream-json interrupt ends every one of them with all their commands (23 Nov 2026 20:51: four
        agents "killed" by a message sent while the main turn ran); the main session's background commands outlive it."""
        return [t for t in list(bg) if tasks.get(t, {}).get("agent")]

    def write_in(agents):
        """A message for a turn that has subagents working: written straight in, no cut. Claude Code holds it and
        gives it to Claude at its next step (or as its next turn). A shell command of the main turn's own that keeps
        Claude waiting is ended alone (stop_task), as a cut would have ended it; the agents go on."""
        steer_log(cid, "written in, no cut: subagents working", agents=",".join(agents), **state())
        subagents.detach_calls(cid)  # a wait on a harness subagent ends; the subagent goes on
        st["wrote_in"] = True
        steer()
        for tid, info in list(tasks.items()):
            if tid in bg and info.get("fg") and info.get("tool") in running and tid not in st["stopped"]:
                st["stopped"][tid] = info["tool"]
                steer_log(cid, "foreground command stopped for the message", task=tid)
                try:
                    send({"type": "control_request", "request_id": "steer-" + uuid.uuid4().hex[:8],
                          "request": {"subtype": "stop_task", "task_id": tid}})
                except OSError:
                    pass

    def interrupt():
        """Cut Claude's step short so the queued message goes in as a turn of its own (see steer at the result)."""
        if feed["open"] and not feed["interrupted"] and run.queue:
            agents = agents_running()
            if agents:
                write_in(agents)
                return
        with feed["lock"]:
            if feed["open"] and not feed["interrupted"]:
                feed["interrupted"] = True
                steer_log(cid, "cut sent", **state())
                subagents.detach_calls(cid)  # a subagent behind a running call goes on; its report comes in later
                try:
                    send({"type": "control_request", "request_id": "steer-" + uuid.uuid4().hex[:8],
                          "request": {"subtype": "interrupt"}})
                except OSError:
                    pass
    threading.Thread(target=killer, daemon=True).start()
    temp = None
    t0 = time.time()
    names, counted = {}, {}
    cur_api = None                   # the API call being streamed (its id comes with message_start)
    if pos:
        _bg_since_restart(cid, out_f, pos, bg, tasks)      # commands still running from before the restart, on the page again

    def keep_following():
        if proc.poll() is not None:
            return False
        if st["completed"] and not st["idle"] and st["close_at"] and (time.time() - st["close_at"]) >= POST_TURN_GRACE_S:
            return False
        return True

    for raw, end in turn_record.follow(out_f, keep_following, pos):
        n0 = st["n"]
        try:
            ev = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        typ = ev.get("type")
        if typ == "stream_event":
            e = ev.get("event") or {}
            if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "tool_use"                     and str((e.get("content_block") or {}).get("name") or "").endswith("__" + subagents.TOOL):
                subagents.note_call(cid, e["content_block"].get("id"))
            if e.get("type") == "message_start":
                from . import store
                if not ev.get("parent_tool_use_id"):
                    st["idle"] = False               # a turn began (a message, or a background command ended)
                    st["took"] = True
                    if st["completed"]:
                        st["completed"] = False      # working again: a restart from here on carries it on
                        turn_record.note(run, completed=False)
                temp = store.new_id("t")
                if not ev.get("parent_tool_use_id"):
                    cur_api = (e.get("message") or {}).get("id")
                emit(cid, {"type": "step", "id": temp, "endpoint": ep["id"]})
            elif e.get("type") == "message_delta" and not ev.get("parent_tool_use_id"):
                # The call's real output count comes here, after its blocks were stored (each block only carries the
                # count so far). Store the final numbers as a stats-only record: the page keeps a call's largest.
                prev = counted.get(cur_api)
                out = int((e.get("usage") or {}).get("output_tokens") or 0)
                if prev and out > (prev.get("out_tokens") or 0):
                    final = {**prev, "out_tokens": out}
                    counted[cur_api] = final
                    sm = add({"role": "assistant", "content": "", "endpoint": ep["id"], "stats": final,
                                      "usage_only": True})
                    use = chat.meta().get("usage") or {}
                    use["out_tokens"] = (use.get("out_tokens") or 0) + out - (prev.get("out_tokens") or 0)
                    chat.update_meta(usage=use)
                    from . import burn_monitor
                    burn_monitor.record_call(cid, chat.meta(), final, ep=ep, api_id=cur_api)
                    emit(cid, {"type": "stats", "message": sm})
            elif e.get("type") == "content_block_delta":
                d = e.get("delta") or {}
                if d.get("type") == "text_delta" and temp:
                    emit(cid, {"type": "delta", "id": temp, "text": d.get("text", "")})
                elif d.get("type") == "thinking_delta" and temp:
                    emit(cid, {"type": "reasoning", "id": temp, "text": d.get("thinking", "")})
        elif typ == "assistant":
            m = ev.get("message") or {}
            texts, calls, thinking = [], [], []
            for blk in m.get("content") or []:
                if blk.get("type") == "text":
                    texts.append(blk.get("text", ""))
                elif blk.get("type") == "thinking":
                    thinking.append(blk.get("thinking", ""))
                elif blk.get("type") == "tool_use":
                    calls.append({"id": blk["id"], "name": blk.get("name"), "arguments": json.dumps(blk.get("input") or {})})
                    names[blk["id"]] = blk.get("name")
                    running[blk["id"]] = time.time()
                    if str(blk.get("name") or "").endswith("__" + subagents.TOOL):
                        subagents.note_call(cid, blk["id"], blk.get("input"))
            # One API call arrives as one block per event (thinking, text, each tool call), all with the call's id. The
            # input numbers are the same on each; output_tokens grows block by block (the first block's count is only
            # what was written so far: the harness used to keep that and showed ~2% of the real output). Each block
            # carries the call's numbers so far; the chat totals take only what is new since the call's last block.
            stats = {"model": m.get("model"), **usage_stats(m.get("usage") or {}),
                     "secs": round(time.time() - t0, 1), "effort": effort.label(ep, level),
                     "api_id": m.get("id")}        # the page counts one call once, with its last (largest) numbers
            amsg = {"role": "assistant", "content": "\n".join(texts), "endpoint": ep["id"], "stats": stats}
            if thinking:
                amsg["reasoning_content"] = "\n".join(thinking)
            if calls:
                amsg["tool_calls"] = calls
            amsg = add(amsg)
            prev = counted.get(m.get("id"))
            counted[m.get("id")] = stats
            use = chat.meta().get("usage") or {}
            for k in ("prompt_new", "prompt_uncached", "prompt_written", "prompt_cached", "out_tokens"):
                use[k] = (use.get(k) or 0) + max(0, (stats.get(k) or 0) - ((prev or {}).get(k) or 0))
            if not prev:
                use["steps"] = (use.get("steps") or 0) + 1
            upd = {"usage": use, "ctx": {"tokens": stats["ctx_tokens"], "at_msg": amsg["id"], "limit": ep.get("context"),
                                         "by": ep["id"]}}
            if m.get("model") and m["model"] != "<synthetic>" and not ev.get("parent_tool_use_id"):
                upd["claude_model"] = m["model"]          # the session's own model (not a subagent's)
                upd["claude_used"] = {**(chat.meta().get("claude_used") or {}), m["model"]: time.time()}
            chat.update_meta(**upd)
            from . import burn_monitor
            burn_monitor.record_call(cid, chat.meta(), stats, ep=ep, api_id=m.get("id"))
            emit(cid, {"type": "assistant", "temp": temp, "message": amsg, "ctx": chat.meta().get("ctx")})
            for c in calls:
                emit(cid, {"type": "tool_start", "call": {"id": c["id"], "name": c["name"], "args": c["arguments"][:4000]}})
            temp = None
            t0 = time.time()
        elif typ == "user":
            m = ev.get("message") or {}
            for blk in m.get("content") or [] if isinstance(m.get("content"), list) else []:
                if blk.get("type") == "tool_result":
                    content = blk.get("content")
                    if isinstance(content, list):
                        content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                    t_start = running.pop(blk.get("tool_use_id"), None)
                    if (feed["interrupted"] or blk.get("tool_use_id") in st["stopped"].values()) and blk.get("is_error"):
                        took = f" after {time.time() - t_start:.0f} s" if t_start else ""
                        content = f"Stopped{took} so your message could go in at once."
                    tm = add({"role": "tool", "tool_call_id": blk.get("tool_use_id"), "name": names.get(blk.get("tool_use_id"), "tool"),
                                      "content": str(content or ""), "ok": not blk.get("is_error")})
                    emit(cid, {"type": "message", "message": tm})
            if agent.hard_queue(run) and not run.cancel.is_set() and not running and not feed["interrupted"]:
                # Every call of the step came back and the user is waiting: stop the step Claude just began and give
                # it the message as a turn of its own. Written into the running turn instead, Claude Code folds it
                # into the tool result, and models take it for text from the tool, not the user (Haiku ignored one).
                # (A subagent's late report is not the user waiting: it goes in when the turn ends.)
                interrupt()
        elif typ == "system":
            if ev.get("subtype") == "task_started" and ev.get("task_id"):
                bg[ev["task_id"]] = time.time()
                tasks[ev["task_id"]] = {"agent": ev.get("task_type") == "local_agent", "tool": ev.get("tool_use_id"),
                                        "fg": ev.get("task_type") == "local_bash" and not ev.get("is_backgrounded")
                                        and not ev.get("owned_by_subagent")}
                _task_desc[(cid, ev["task_id"])] = (str(ev.get("description") or ev.get("command") or ev["task_id"]),
                                                    ev.get("output_file") or None, time.time())
                if ev.get("is_backgrounded"):
                    steer_log(cid, "background command started", task=ev["task_id"], open=feed["open"])
                    _bg_show(cid, ev["task_id"])
            elif ev.get("subtype") == "task_updated" and ev.get("task_id"):
                patch = ev.get("patch") or {}
                if patch.get("is_backgrounded"):             # a command Claude moved to the background while it ran
                    tasks.get(ev["task_id"], {})["fg"] = False
                    steer_log(cid, "command moved to the background", task=ev["task_id"], open=feed["open"])
                    _bg_show(cid, ev["task_id"])
                if patch.get("status") in ("completed", "failed", "killed", "stopped", "cancelled"):
                    from . import agent as _ag
                    _ag.act_done(cid, "bg:" + str(ev["task_id"]))
            elif ev.get("subtype") == "task_notification" and ev.get("task_id"):
                bg.pop(ev["task_id"], None)          # it ended (Claude takes a turn on it by itself, if it was in the background)
                st["idle_t"] = time.time()
                from . import agent as _ag
                _ag.act_done(cid, "bg:" + str(ev["task_id"]))
        elif typ == "result" and not st["took"] and not ev.get("is_error") and not ev.get("num_turns") \
                and not str(ev.get("result") or "").strip():
            # A result that ends nothing: a resumed session whose background agents or commands the last process took
            # with it first reports each as stopped, then one empty result per report (num_turns 0), all before it
            # reads the message written to it. Taken for the end of the turn, it shut the input while Claude was only
            # starting; Claude then worked and started background agents with no way to write the user's next message
            # in, so that message ended the process and everything in it (23 Nov 2026: 5 times in 80 minutes).
            steer_log(cid, "empty result passed over (nothing was asked of the model)", **state())
        elif typ == "result":
            st["took"] = False
            st["stopped"].clear()
            steered = feed["interrupted"]    # the turn we cut short for the user's message: no error, carry on below
            feed["interrupted"] = False
            running.clear()
            if ev.get("is_error") and not run.cancel.is_set() and not steered:
                res_txt = str(ev.get("result") or ev.get("subtype"))
                m = add({"role": "error", "content": "Claude: " + res_txt})
                emit(cid, {"type": "message", "message": m})
            if run.queue and not run.cancel.is_set():
                steer()                      # sent just as it finished (or cut in): Claude answers it next, same process
            elif steered and not run.cancel.is_set():
                with feed["lock"]:           # the message was taken back after the cut: let Claude carry on
                    if feed["open"]:
                        try:
                            write([{"type": "text", "text": "(The user took their message back. Carry on where you were.)"}])
                        except OSError:
                            close("the carry-on note could not be written")
            elif (bg or st["wrote_in"]) and feed["open"] and not run.cancel.is_set() \
                    and not (bool((chat.meta() or {}).get("group_member_of")) and not st["wrote_in"]):
                # Claude is done for now, but commands it left running in the background are not: its process stays up
                # for them. The input stays open, so a message from the user is read at once (killer) instead of
                # after the last of them; it closes when they have all ended and no turn follows (IDLE_CLOSE_S).
                # (Group Chat members do NOT stay busy waiting on background servers so the baton passes cleanly.)
                st["idle"], st["idle_t"] = True, time.time()
                st["completed"] = True
                turn_record.note(run, completed=True)
            else:
                with feed["lock"]:
                    close("group chat turn finished" if (bg and (chat.meta() or {}).get("group_member_of")) else "a result with nothing known in the background")
                st["completed"] = True
                st["close_at"] = time.time()
                turn_record.note(run, completed=True)    # a restart from here on has nothing to carry on
            st["wrote_in"] = False
        if st["n"] != n0:
            turn_record.note(run, engine_pos=end)     # a copy taking over after a restart reads on from here
    done_ev.set()
    close()
    if st["completed"] and proc.poll() is None:
        rem = max(0.0, POST_TURN_GRACE_S - (time.time() - (st["close_at"] or time.time())))
        try:
            proc.wait(timeout=max(0.5, rem))
        except (subprocess.TimeoutExpired, TimeoutError):
            # Turn already completed, but CLI did not exit (e.g. a background server stayed up).
            # Ask cli_input.py to terminate ONLY the CLI process (leaving detached servers alive).
            if inbox is not None:
                try:
                    from . import cli_input
                    with open(inbox, "ab") as f:
                        f.write(cli_input.TERM + b"\n")
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3.5)
                except (subprocess.TimeoutExpired, TimeoutError):
                    pass
            if proc.poll() is None:
                osutil.terminate_only(proc.pid)
                try:
                    proc.wait(timeout=3.0)
                except (subprocess.TimeoutExpired, TimeoutError):
                    pass
    else:
        proc.wait()
    if ((chat.meta() or {}).get("claude_proc") or {}).get("pid") == getattr(proc, "pid", None):
        chat.update_meta(touch=False, claude_proc=None)        # it has ended: nothing of this chat's is left running
    err = turn_record.tail_lines(err_f)
    mark_engine_cursor(chat, "claude-code")
    old_cli = any("isn't described by this version's model catalog" in ln for ln in err)
    if old_cli and chat.meta().get("warned_old_cli") != ep.get("model"):
        # An older Claude Code than the model: it assumes a 200k window and compacts there, and may get effort and thinking wrong. Say it once per model.
        chat.update_meta(warned_old_cli=ep.get("model"))
        m = add({"role": "notice", "content": f"This Claude Code is older than {short_model(ep.get('model'))}: it "
                         "keeps the chat under 200k tokens and compacts there. Update Claude Code (claude update) to give "
                         "it its full context."})
        emit(cid, {"type": "message", "message": m})
    if proc.returncode and not st["completed"] and not run.cancel.is_set() and err:
        err_joined = " | ".join(err[-4:])[:800]
        m = add({"role": "error", "content": "Claude stopped: " + err_joined})
        emit(cid, {"type": "message", "message": m})
    return st["completed"]
