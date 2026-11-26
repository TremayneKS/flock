"""Neon over acp (harness/agy_acp.py): a message from the user goes straight into the running turn, nothing is cut.

    python -X utf8 tools/test_agy_acp.py

The GPU chat, 22 Nov: a message to a Neon sitting on a 10-minute timer waited for the timer (print mode reads its
inbox only at a wake), and before that each message ended the remote jobs. Over `agy acp` a second session/prompt
steers the running turn (scratch probe: answered in 8.6 s, background command still running), a prompt ends while
its background work goes on, and a timer firing later wakes the session by itself.

Held here, with a stand-in for `agy acp` that speaks JSON-RPC (no model, no network, a throwaway KCH_HOME):
- a turn: words, thinking, a tool row, usage; the conversation is created and kept on the chat;
- a message sent while a background command and a timer run is sent as a prompt at once (with "answer in words
  first"), answered in the same turn, and nothing is killed or restarted;
- the turn ends when its prompts end while the background work goes on; the chat is free and its list row says so
  ("bg"); a timer firing later (no prompt outstanding) is shown in the chat as it comes;
- the next message reuses the live process (no reload); a new process loads the conversation and the replayed
  history is not shown again;
- Stop sends session/cancel; a prompt that does not end is ended with its process;
- when acp cannot start, the turn runs in print mode as before;
- an idle session with nothing running is closed; one with a timer running is not."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-acp-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
os.environ.pop("KCH_AGY_ACP", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import activity, agent, agy_acp, agy_engine, config, osutil, store  # noqa: E402

config.load()
PROJ = str(TMP / "proj")
Path(PROJ).mkdir(exist_ok=True)
store.add_project(PROJ)
NEON = {"id": "neon", "name": "Gemini 4 Neon", "kind": "agy"}
agy_engine.TASK_LOGS = str(TMP / "brain")
agy_acp.WAKE_QUIET = 0.3
agy_acp.CANCEL_GRACE = 1.0
agent.STEER_GRACE = 0.2


def until(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return bool(cond())


class Pipe:
    def __init__(self):
        self.lines, self.cv, self.closed = [], threading.Condition(), False

    def put(self, obj):
        with self.cv:
            self.lines.append((json.dumps(obj) + "\n").encode())
            self.cv.notify_all()

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def __iter__(self):
        while True:
            with self.cv:
                while not self.lines and not self.closed:
                    self.cv.wait(0.05)
                if self.lines:
                    ln = self.lines.pop(0)
                elif self.closed:
                    return
            yield ln


class FakeAcp:
    """`agy acp` as a stand-in. Each session/prompt runs FakeAcp.script(fake, rid, text) in a thread of its own."""
    procs = []
    script = None
    fail_init = False
    on_load = None

    def __init__(self, cmd, *a, **k):
        self.cmd = cmd
        self.out, self.err_pipe = Pipe(), Pipe()
        self.stdout, self.stderr = self.out, self.err_pipe
        self.returncode = None
        self.killed = False
        self.pid = 800000 + len(FakeAcp.procs)
        self.methods, self.prompts, self.cancelled = [], [], threading.Event()
        self.params = {}
        FakeAcp.procs.append(self)
        fake = self

        class _In:
            buf = b""

            def write(self, b):
                self.buf += b
                while b"\n" in self.buf:
                    ln, self.buf = self.buf.split(b"\n", 1)
                    fake._got(json.loads(ln))

            def flush(self):
                pass

            def close(self):
                fake.end()

        self.stdin = _In()

    def _got(self, m):
        meth = m.get("method")
        if not meth:
            return
        self.methods.append(meth)
        self.params[meth] = m.get("params")
        rid = m.get("id")
        if meth == "session/prompt":
            self.prompt_at = time.time()
        if meth == "initialize":
            if FakeAcp.fail_init:
                self.out.put({"jsonrpc": "2.0", "id": rid, "error": {"code": -1, "message": "acp not supported"}})
            else:
                self.out.put({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1}})
        elif meth == "session/new":
            self.out.put({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "conv-new"}})
        elif meth == "session/load":
            sid = m["params"]["sessionId"]
            if FakeAcp.on_load:
                FakeAcp.on_load(self, rid, sid)
                return
            self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD REPLY"}})
            self.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "old-1", "status": "in_progress",
                              "name": "run_command", "args": {"CommandLine": "old"}})
            self.out.put({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif meth == "session/set_model":
            self.out.put({"jsonrpc": "2.0", "id": rid, "result": None})
        elif meth == "session/cancel":
            self.cancelled.set()
        elif meth == "session/prompt":
            text = m["params"]["prompt"][0]["text"]
            self.prompts.append(text)
            threading.Thread(target=FakeAcp.script, args=(self, rid, text), daemon=True).start()

    def update(self, sid, u):
        self.out.put({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})

    def say(self, text, sid="conv-1"):
        self.update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

    def done(self, rid, stop="end_turn"):
        self.out.put({"jsonrpc": "2.0", "id": rid, "result": {"stopReason": stop, "usage": {
            "inputTokens": 100, "outputTokens": 20, "cachedReadTokens": 50, "thoughtTokens": 5}}})

    def end(self, killed=False):
        self.killed = self.killed or killed
        if self.returncode is None:
            self.returncode = 1 if killed else 0
        self.out.close()
        self.err_pipe.close()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        until(lambda: self.returncode is not None, timeout or 5)
        if self.returncode is None:
            raise TimeoutError
        return self.returncode


def tool_call(fake, tid, name, status="in_progress", sid="conv-1", **args):
    fake.update(sid, {"sessionUpdate": "tool_call", "toolCallId": tid, "name": name, "args": args, "status": status,
                      "title": name})


def tool_done(fake, tid, text="ok", sid="conv-1", status="completed"):
    fake.update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": tid, "status": status,
                      "content": [{"type": "content", "content": {"type": "text", "text": text}}]})


class Chat:
    def __init__(self, conv="conv-1", first="run the GPU tests"):
        self.chat = store.create(PROJ, "neon")
        if conv:
            self.chat.update_meta(agy_conversation=conv)
        self.chat.append({"role": "user", "content": first})
        self.run = None

    def turn(self):
        self.run = agent.Run(self.chat.id)
        th = threading.Thread(target=agy_engine.turn, args=(self.run, self.chat, self.chat.meta(), NEON, agent.emit),
                              daemon=True)
        th.start()
        return th

    def send(self, text):
        self.run.queue.append({"id": "q%d" % len(self.run.queue), "text": text, "files": [], "t": time.time()})

    def rows(self, role):
        return [m for m in store.Chat(self.chat.id).messages() if m.get("role") == role]   # (a wake writes too)

    def said(self):
        return [m.get("content") for m in self.rows("assistant") if m.get("content")]


class AgyAcp(unittest.TestCase):
    def setUp(self):
        FakeAcp.procs, FakeAcp.fail_init, FakeAcp.on_load = [], False, None
        self.orig = (agy_acp.subprocess.Popen, agy_engine.agy_exe, osutil.kill_tree)
        agy_acp.subprocess.Popen = FakeAcp
        agy_engine.agy_exe = lambda: "agy"
        osutil.kill_tree = lambda pid, timeout=20: [p.end(killed=True) for p in FakeAcp.procs if p.pid == pid]

    def tearDown(self):
        for cid in list(agy_acp._sessions):
            agy_acp.close(cid)
        agy_acp.subprocess.Popen, agy_engine.agy_exe, osutil.kill_tree = self.orig

    def test_a_turn(self):
        def script(f, rid, text):
            f.update("conv-1", {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "plan"}})
            f.say("Reading it now.")
            tool_call(f, "c1", "read_file", Path="a.py")
            tool_done(f, "c1", "print('hi')")
            f.say("It prints hi.")
            f.done(rid)
        FakeAcp.script = script
        c = Chat()
        c.turn().join(8)
        p = FakeAcp.procs[0]
        self.assertEqual(p.cmd[1:], ["acp", "--dangerously-skip-permissions"])
        self.assertEqual(p.methods[:3], ["initialize", "session/load", "session/set_model"])
        self.assertEqual(c.said(), ["Reading it now.", "It prints hi."])
        self.assertNotIn("OLD REPLY", " ".join(c.said()), "history replayed by session/load is not shown again")
        self.assertEqual([m["name"] for m in c.rows("tool")], ["Read"])
        self.assertEqual(c.rows("tool")[0]["content"], "print('hi')")
        stats = c.rows("assistant")[-1].get("stats") or {}
        self.assertEqual(stats.get("out_tokens"), 20)
        self.assertEqual(agy_acp.running(c.chat.id), [], "the replayed old tool call is not counted as running")

    def replay(self, f, sid, n=200, user="run the GPU tests and report back on all of it"):
        """agy replaying a long print-mode history: old user message, old words, old tool calls, the old report."""
        f.update(sid, {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": user}})
        for i in range(n):
            f.update(sid, {"sessionUpdate": "tool_call", "toolCallId": "old-%d" % i, "status": "completed",
                           "name": "edit_file", "args": {"Path": "x.py"}})
            f.say("old step %d" % i, sid=sid)
        f.say("OLD FINAL REPORT", sid=sid)
        self.replay_end = time.time()

    def test_late_replay_after_the_load_is_not_shown(self):
        """22 Nov 13:32-13:50: the load answered, then 840-1,300 old rows came and were written into the chat."""
        def on_load(f, rid, sid):
            f.out.put({"jsonrpc": "2.0", "id": rid, "result": {}})          # answered first...
            def late():
                time.sleep(0.2)
                for _ in range(3):
                    self.replay(f, sid, n=60)                                # ...then the history, in bursts
                    time.sleep(0.3)
            threading.Thread(target=late, daemon=True).start()
        FakeAcp.on_load = on_load
        FakeAcp.script = lambda f, rid, text: (f.say("The new answer."), f.done(rid))
        c = Chat(first="run the GPU tests and report back on all of it")
        c.chat.append({"role": "assistant", "content": "earlier"})
        c.chat.append({"role": "user", "content": "send me an update"})
        c.turn().join(15)
        self.assertEqual(c.said()[-1], "The new answer.")
        self.assertFalse([t for t in c.said() if t.startswith("old step") or t == "OLD FINAL REPORT"],
                         "nothing of the replay is in the chat")
        self.assertGreater(FakeAcp.procs[0].prompt_at, self.replay_end, "the prompt went in after the replay ended")
        self.assertEqual([m["name"] for m in c.rows("tool")], [])

    def test_replay_during_the_prompt_is_dropped(self):
        FakeAcp.on_load = lambda f, rid, sid: f.out.put({"jsonrpc": "2.0", "id": rid, "result": {}})

        def script(f, rid, text):
            self.replay(f, "conv-1", n=80)
            time.sleep(1.6)                                                  # the model's own answer comes after
            tool_call(f, "new-1", "read_file", Path="a.py")
            tool_done(f, "new-1", "hi")
            f.say("The new answer.")
            f.done(rid)
        FakeAcp.script = script
        c = Chat(first="run the GPU tests and report back on all of it")
        c.chat.append({"role": "user", "content": "send me an update"})
        c.turn().join(15)
        self.assertEqual(c.said(), ["The new answer."])
        self.assertEqual([m["content"] for m in c.rows("tool")], ["hi"])

    def test_turn_stays_open_while_the_session_still_streams(self):
        """At 13:49:58 the prompt came back as the replay ended while agy was only starting on the message: the turn
        ended there, the old report was taken as the turn's report, and the real work came as a wake."""
        def script(f, rid, text):
            f.done(rid)                                                      # back at once...
            for i in range(6):                                               # ...but still working
                time.sleep(0.25)
                f.say("part %d. " % i)
            tool_call(f, "w1", "run_command", CommandLine="git fetch")
            tool_done(f, "w1", "fetched")
            f.say("All merged.")
        FakeAcp.script = script
        c = Chat()
        th = c.turn()
        time.sleep(1.0)
        self.assertTrue(th.is_alive(), "not ended while updates still come")
        th.join(10)
        said = c.said()
        self.assertEqual(said[-1], "All merged.", "the turn's last words are the real ones")
        self.assertEqual([m["content"] for m in c.rows("tool")], ["fetched"])

    def test_wake_rows_are_not_handed_back_as_missed(self):
        """13:44:30: the next turn was "handed what was said while it was away": its own wake rows."""
        FakeAcp.script = lambda f, rid, text: (tool_call(f, "t1", "schedule", DurationSeconds=60, Prompt="x"),
                                               f.say("set"), f.done(rid))
        c = Chat()
        c.turn().join(8)
        p = FakeAcp.procs[0]
        tool_done(p, "t1", "Timer has expired")
        p.say("Timer fired: 41 of 112.")
        self.assertTrue(until(lambda: "Timer fired: 41 of 112." in c.said()))
        self.assertTrue(until(lambda: (store.Chat(c.chat.id).meta().get("engine_cursor") or {}).get("agy") ==
                              [m for m in store.Chat(c.chat.id).messages() if m.get("role") == "assistant"][-1]["id"]))
        FakeAcp.script = lambda f, rid, text: (f.say("ok"), f.done(rid))
        c.chat = store.Chat(c.chat.id)
        c.chat.append({"role": "user", "content": "and now?"})
        c.turn().join(8)
        self.assertFalse([m for m in c.rows("notice") if "Switched to" in (m.get("content") or "")])
        self.assertFalse(FakeAcp.procs[0].prompts[-1].startswith("<"), "no handover of its own steps")

    def test_subagents_in_the_app_harness_server_and_note(self):
        """22 Nov 14:01: a Neon asked for subagents ran `claude -p` in a shell, then agy's own invoke_subagent: it had
        no way to start one in the app. Over acp it now gets the harness MCP server (harness_agent), with this chat's
        id, and its first prompt says how, once per conversation."""
        FakeAcp.script = lambda f, rid, text: (f.say("ok"), f.done(rid))
        c = Chat()
        c.turn().join(8)
        p = FakeAcp.procs[0]
        servers = p.params["session/load"]["mcpServers"]
        self.assertEqual([s["name"] for s in servers], ["harness"])
        env = {e["name"]: e["value"] for e in servers[0]["env"]}
        self.assertEqual(env["KCH_AGENT_CHAT"], c.chat.id)
        self.assertEqual(env["KCH_AGENT_CAN_SPAWN"], "1")
        self.assertTrue(env["KCH_AGENT_TOKEN"])
        self.assertTrue(servers[0]["args"][0].endswith(os.path.join("servers", "agents.py")))
        first = p.prompts[0]
        self.assertTrue(first.startswith("[Flock] "), first[:80])
        self.assertIn("Subagents in this app", first)
        for want in ("mcp_harness_harness_agent", "claude -p", "invoke_subagent", "agents.py\" agent -"):
            self.assertIn(want, first)
        self.assertTrue(first.endswith("run the GPU tests"))
        c.chat = store.Chat(c.chat.id)
        c.chat.append({"role": "user", "content": "next"})
        c.turn().join(8)
        self.assertEqual(p.prompts[-1], "next", "the note is given once per conversation")

    def test_new_conversation_gets_the_server_and_the_note_once(self):
        FakeAcp.script = lambda f, rid, text: (f.say("ok", sid="conv-new"), f.done(rid))
        c = Chat(conv=None)
        c.turn().join(8)
        p = FakeAcp.procs[0]
        self.assertEqual([s["name"] for s in p.params["session/new"]["mcpServers"]], ["harness"])
        self.assertIn("Subagents in this app", p.prompts[0])
        c.chat = store.Chat(c.chat.id)
        c.chat.append({"role": "user", "content": "again"})
        c.turn().join(8)
        self.assertEqual(p.prompts[-1], "again", "not given again once agy named the conversation")

    def test_subagent_chat_gets_report_and_parent_tools(self):
        FakeAcp.script = lambda f, rid, text: (f.say("ok"), f.done(rid))
        c = Chat()
        c.chat.update_meta(sub_of="20261122-000000-parent", depth=1)
        c.turn().join(8)
        p = FakeAcp.procs[0]
        env = {e["name"]: e["value"] for e in p.params["session/load"]["mcpServers"][0]["env"]}
        self.assertEqual(env["KCH_AGENT_IS_SUB"], "1")
        self.assertIn("mcp_harness_harness_report_to_main", p.prompts[0])

    def test_no_subagents_no_server_no_note(self):
        from harness import subagents
        orig = subagents.offered
        subagents.offered = lambda meta: False
        try:
            FakeAcp.script = lambda f, rid, text: (f.say("ok"), f.done(rid))
            c = Chat()
            c.turn().join(8)
        finally:
            subagents.offered = orig
        p = FakeAcp.procs[0]
        self.assertEqual(p.params["session/load"]["mcpServers"], [])
        self.assertNotIn("Subagents", p.prompts[0])
        self.assertNotIn("harness_agent", p.prompts[0])
        self.assertTrue(p.prompts[0].endswith("run the GPU tests"))

    def test_new_conversation_is_kept_on_the_chat(self):
        FakeAcp.script = lambda f, rid, text: (f.say("hello", sid="conv-new"), f.done(rid))
        c = Chat(conv=None)
        c.turn().join(8)
        self.assertIn("session/new", FakeAcp.procs[0].methods)
        self.assertEqual(c.chat.meta().get("agy_conversation"), "conv-new")

    def test_message_during_background_work_goes_straight_in(self):
        gate = threading.Event()

        def script(f, rid, text):
            if "what's going on" in text:
                f.say("Both GPUs are running; Phase 4A is at 27 of 112.")
                f.done(rid)
                gate.set()
                return
            tool_call(f, "bg1", "run_command", CommandLine="kccolab run --name p4a", WaitMsBeforeAsync=1000)
            tool_call(f, "t1", "schedule", DurationSeconds=600, Prompt="liveness check")
            f.say("Started both; next check in 10 min.")
            gate.wait(10)                          # the turn is still running when the message comes
            f.done(rid)
        FakeAcp.script = script
        c = Chat()
        th = c.turn()
        p = None
        self.assertTrue(until(lambda: FakeAcp.procs and len(FakeAcp.procs[0].prompts) == 1))
        p = FakeAcp.procs[0]
        self.assertTrue(until(lambda: activity.now(c.chat.id) and activity.now(c.chat.id)["n"] == 2))
        t_sent = time.time()
        c.send("what's going on?")
        self.assertTrue(until(lambda: len(p.prompts) == 2, 3), "the message went in as a prompt of its own")
        self.assertLess(time.time() - t_sent, 1.5, "at once, not at the next wake")
        self.assertTrue(p.prompts[1].startswith(agy_engine.WORDS_FIRST))
        self.assertTrue(p.prompts[1].endswith("what's going on?"))
        th.join(8)
        self.assertFalse(th.is_alive())
        self.assertFalse(p.killed, "nothing was cut")
        self.assertNotIn("session/cancel", p.methods)
        self.assertEqual(len(FakeAcp.procs), 1)
        self.assertIn("Both GPUs are running; Phase 4A is at 27 of 112.", c.said())
        self.assertEqual([m["content"] for m in c.rows("user")][-1], "what's going on?")
        self.assertFalse(any("interrupted" in (m.get("content") or "") for m in c.rows("notice")))
        # the turn has ended; the command and the timer still run in the live session
        self.assertEqual(sorted(agy_acp.running(c.chat.id)), ["bg1", "t1"])
        agent.emit(c.chat.id, {"type": "turn_end"})  # what agent._turn does after the engine returns
        a = activity.now(c.chat.id)
        self.assertTrue(a and a.get("bg"), "the free chat still shows its background work")
        self.assertIn(c.chat.id, agent.background_activities([]))
        # the timer fires later, no prompt outstanding: shown as it comes
        tool_done(p, "t1", "Timer has expired")
        p.say("10-minute check: both GPUs live, 41 of 112.")
        self.assertTrue(until(lambda: "10-minute check: both GPUs live, 41 of 112." in c.said()))
        self.assertTrue(until(lambda: len([m for m in c.rows("tool") if m["content"] == "Timer has expired"]) == 1))
        self.assertEqual(agy_acp.running(c.chat.id), ["bg1"])

    def test_next_message_reuses_the_live_process(self):
        FakeAcp.script = lambda f, rid, text: (f.say("ok " + text[-3:]), f.done(rid))
        c = Chat()
        c.turn().join(8)
        c.chat.append({"role": "user", "content": "two"})
        c.turn().join(8)
        self.assertEqual(len(FakeAcp.procs), 1)
        self.assertEqual(FakeAcp.procs[0].methods.count("session/load"), 1)
        self.assertEqual(FakeAcp.procs[0].methods.count("session/prompt"), 2)
        agy_acp.close(c.chat.id)                   # gone (an app restart): the next turn loads the conversation again
        c.chat.append({"role": "user", "content": "three"})
        c.turn().join(8)
        self.assertEqual(len(FakeAcp.procs), 2)
        self.assertIn("session/load", FakeAcp.procs[1].methods)
        self.assertNotIn("OLD REPLY", " ".join(c.said()))

    def test_stop_cancels_and_ends_a_prompt_that_does_not_stop(self):
        def obedient(f, rid, text):
            f.say("working")
            f.cancelled.wait(10)
            f.done(rid, "cancelled")
        FakeAcp.script = obedient
        c = Chat()
        th = c.turn()
        self.assertTrue(until(lambda: FakeAcp.procs and FakeAcp.procs[0].prompts))
        c.run.cancel.set()
        th.join(5)
        p = FakeAcp.procs[0]
        self.assertIn("session/cancel", p.methods)
        self.assertFalse(p.killed, "a prompt that ends on cancel keeps its process")

        def deaf(f, rid, text):
            time.sleep(30)
        FakeAcp.script = deaf
        c.chat.append({"role": "user", "content": "again"})
        th = c.turn()
        self.assertTrue(until(lambda: len(p.prompts) == 2))
        c.run.cancel.set()
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertTrue(p.killed, "a prompt that ignores cancel is ended with its process")

    def test_print_mode_when_acp_cannot_start(self):
        FakeAcp.fail_init = True
        printed = {}

        def fake_one_turn(run, chat, ep, emit, text, first, carry=None):
            printed["text"] = text
            m = chat.append({"role": "assistant", "content": "from print mode", "endpoint": "neon"})
            emit(chat.id, {"type": "assistant", "temp": None, "message": m})
            return "done"
        orig = agy_engine._one_turn
        agy_engine._one_turn = fake_one_turn
        try:
            c = Chat()
            c.turn().join(8)
        finally:
            agy_engine._one_turn = orig
        self.assertTrue(printed.get("text", "").endswith("run the GPU tests"))
        self.assertIn("Subagents in this app", printed["text"], "print mode is told how to start subagents too")
        self.assertIn("from print mode", c.said())
        self.assertTrue(FakeAcp.procs[0].returncode is not None, "the acp process that failed is closed")

    def test_idle_session_closed_but_not_while_a_timer_runs(self):
        agy_acp.IDLE_CLOSE, agy_acp.REAP_TICK = 0.5, 0.1
        agy_acp._reaper[0] = None
        try:
            FakeAcp.script = lambda f, rid, text: (tool_call(f, "t9", "schedule", DurationSeconds=60, Prompt="x"),
                                                    f.say("set"), f.done(rid))
            c = Chat()
            c.turn().join(8)
            time.sleep(1.2)
            self.assertIsNotNone(agy_acp.live(c.chat.id), "kept: its timer is still running")
            tool_done(FakeAcp.procs[0], "t9", "Timer has expired")
            self.assertTrue(until(lambda: agy_acp.live(c.chat.id) is None, 5), "closed once idle with nothing running")
        finally:
            agy_acp.IDLE_CLOSE, agy_acp.REAP_TICK = 900, 30

    def test_permission_request_callback_selects_allow_always_as_fallback(self):
        """Even with --dangerously-skip-permissions on `agy acp`, `_answer` still auto-approves permission callbacks."""
        got_reply = []

        def script(f, rid, text):
            orig_got = f._got

            def capture(m):
                if m.get("id") == 999 and "result" in m:
                    got_reply.append(m["result"])
                    return
                orig_got(m)

            f._got = capture
            f.out.put({
                "jsonrpc": "2.0",
                "id": 999,
                "method": "session/request_permission",
                "params": {"options": [
                    {"optionId": "deny", "kind": "reject_once"},
                    {"optionId": "allow_once", "kind": "allow_once"},
                    {"optionId": "always", "kind": "allow_always"},
                ]},
            })
            until(lambda: bool(got_reply), 3)
            f.say("approved")
            f.done(rid)

        FakeAcp.script = script
        c = Chat()
        c.turn().join(8)
        self.assertEqual(FakeAcp.procs[0].cmd[1:], ["acp", "--dangerously-skip-permissions"])
        self.assertEqual(got_reply, [{"outcome": {"outcome": "selected", "optionId": "always"}}])
        self.assertEqual(c.said(), ["approved"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
