"""A Neon (agy) turn with background work: what the model writes is shown, and a message from the user costs nothing.

    python -X utf8 tools/test_agy_background.py

The GPU chat, 22 Nov: the user asked for an update three times and saw nothing, and each message ended the two remote
GPU jobs. What was found on a scratch conversation (agy_live.py):
- agy's stdout is held back behind a command it put in the background, so Neon's status updates at 10:17, 10:43 and
  10:51 never reached the chat, although Neon wrote them (they are in its transcript);
- ending agy ends every command it started, so cutting the turn to hand it the message ended the jobs;
- agy reads an inbox at its next wake, and the model answers inside the same turn.

Held here, with a stand-in for agy (no model, no network, a throwaway KCH_HOME and brain folder):
- while stdout is held, the model's words come from the transcript, and are not shown twice when stdout catches up;
- a message sent while background work runs goes to the inbox with "answer in words first", the turn is NOT cut, and
  the chat says at once what is running and that nothing was stopped;
- a message the process never read (it ended first) is taken back from the inbox and starts the next turn;
- with nothing in the background a message still cuts the turn as before, and the resumed turn is told to answer in
  words first;
- after Stop, an unread message is taken back and does not start anything."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-agybg-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
os.environ["KCH_AGY_ACP"] = "0"           # the print-mode engine (agy_acp.py has its own tests: test_agy_acp.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import agent, agy_engine, agy_live, config, osutil, store  # noqa: E402
# These tests cover the inbox path (a message handed in while background work runs). It is switched OFF in the app
# (INBOX_WHILE_BACKGROUND, 22 Nov 2026: his messages must go through at once); the path is kept and tested here.
agy_engine.INBOX_WHILE_BACKGROUND = True

config.load()
PROJ = str(TMP / "proj")
Path(PROJ).mkdir(exist_ok=True)
store.add_project(PROJ)
NEON = {"id": "neon", "name": "Gemini 4 Neon", "kind": "agy"}
BRAIN = TMP / "brain"
agy_engine.TASK_LOGS = str(BRAIN)
agy_engine.HOLD_S = 0.3
agy_engine.FOLLOW_TICK = 0.05
agent.STEER_GRACE = 0.2


def until(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return bool(cond())


class FakeAgy:
    """agy --print as a stand-in: the test feeds its stream-json lines; it records its stdin and whether it was killed."""
    procs = []

    def __init__(self, *a, **k):
        self.lines, self.stdin_data = [], b""
        self.kwargs = k
        self.lock = threading.Condition()
        self.ended = False
        self.killed = False
        self.returncode = None
        self.pid = 900000 + len(FakeAgy.procs)
        FakeAgy.procs.append(self)
        test = self

        class _In:
            def write(self, b):
                test.stdin_data += b

            def close(self):
                pass

        class _Out:
            def __iter__(self):
                while True:
                    with test.lock:
                        while not test.lines and not test.ended:
                            test.lock.wait(0.05)
                        if test.lines:
                            ln = test.lines.pop(0)
                        else:
                            return
                    yield (json.dumps(ln) + "\n").encode()

            def read(self):
                return b""

        self.stdin, self.stdout, self.stderr = _In(), _Out(), _Out()

    def prompt(self):
        return json.loads(self.stdin_data.decode().splitlines()[0])["message"]["content"]

    def feed(self, *evs):
        with self.lock:
            self.lines.extend(evs)
            self.lock.notify_all()

    def end(self, killed=False):
        with self.lock:
            self.killed = self.killed or killed
            self.ended = True
            self.returncode = 1 if killed else 0
            self.lock.notify_all()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        until(lambda: self.ended, timeout or 10)
        return self.returncode


def tool(idx, state, name="run_command", conv="conv-1", **params):
    return {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": idx, "state": state,
                                                     "step_type": "tool", "tool_name": name,
                                                     "tool_info": {"name": name, "parameters": params}}}


def said(idx, text, conv="conv-1"):
    return [{"event": "step_update", "step_update": {"conversation_id": conv, "step_index": idx, "step_type":
                                                      "agent_response", "state": "ACTIVE", "text_delta": text}},
            {"event": "step_update", "step_update": {"conversation_id": conv, "step_index": idx, "step_type":
                                                      "agent_response", "state": "DONE"}}]


def result(response, conv="conv-1"):
    return {"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS", "response": response,
                                          "usage": {}}}


def transcript(conv, *entries):
    p = BRAIN / conv / ".system_generated" / "logs" / "transcript.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def planner(idx, content="", calls=None, at="2026-11-22T10:17:22Z"):
    e = {"step_index": idx, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": at,
         "content": content}
    if calls:
        e["tool_calls"] = calls
    return e


def inbox(conv):
    d = BRAIN / conv / ".system_generated" / "messages"
    msgs = sorted(d.glob("*.json")) if d.exists() else []
    marks = sorted((d / "undelivered").glob("*")) if (d / "undelivered").exists() else []
    return msgs, marks


class Harness:
    def __init__(self, conv):
        self.chat = store.create(PROJ, "neon")
        self.chat.update_meta(agy_conversation=conv)
        self.chat.append({"role": "user", "content": "run the GPU tests"})
        transcript(conv)                                   # the file exists before the turn (a resumed conversation)
        self.run = agent.Run(self.chat.id)
        self.th = threading.Thread(target=agy_engine.turn, args=(self.run, self.chat, self.chat.meta(), NEON,
                                                                 agent.emit), daemon=True)

    def start(self, n=1):
        self.th.start()
        assert until(lambda: len(FakeAgy.procs) >= n and FakeAgy.procs[-1].stdin_data)
        return FakeAgy.procs[-1]

    def send(self, text):
        self.run.queue.append({"id": "q%d" % len(self.run.queue), "text": text, "files": [], "t": time.time()})

    def rows(self, role):
        return [m for m in self.chat.messages() if m.get("role") == role]


class AgyBackground(unittest.TestCase):
    def setUp(self):
        FakeAgy.procs = []
        self.orig = (agy_engine.subprocess.Popen, agy_engine.agy_exe, osutil.kill_tree)
        agy_engine.subprocess.Popen = FakeAgy
        agy_engine.agy_exe = lambda: "agy"
        by_pid = lambda pid: next(p for p in FakeAgy.procs if p.pid == pid)  # noqa: E731
        osutil.kill_tree = lambda pid, timeout=20: by_pid(pid).end(killed=True)

    def tearDown(self):
        agy_engine.subprocess.Popen, agy_engine.agy_exe, osutil.kill_tree = self.orig

    def test_held_stdout_words_come_from_the_transcript_once(self):
        h = Harness("conv-held")
        p = h.start()
        p.feed({"event": "init", "conversation_id": "conv-held"},
               tool(5, "ACTIVE", conv="conv-held", CommandLine="kccolab run --name p4a", WaitMsBeforeAsync=1000))
        update = "Both G4 GPUs are live and running your two Phase 4 battles."
        transcript("conv-held", planner(6, calls=[{"name": "manage_task", "args": {"Action": '"status"'}}]),
                   planner(8, update))
        self.assertTrue(until(lambda: any(m.get("content") == update for m in h.rows("assistant"))),
                        "the model's words are shown while stdout is held behind the background command")
        # the command ends: stdout catches up with the same steps, then the turn ends
        p.feed(tool(5, "DONE", conv="conv-held", CommandLine="kccolab run --name p4a"),
               tool(6, "ACTIVE", "manage_task", conv="conv-held"), tool(6, "DONE", "manage_task", conv="conv-held"),
               *said(8, update, conv="conv-held"), *said(9, "All done.", conv="conv-held"),
               result(update + "\nAll done.", conv="conv-held"))
        p.end()
        h.th.join(5)
        texts = [m.get("content") for m in h.rows("assistant") if m.get("content")]
        self.assertEqual(texts.count(update), 1, "shown once, not again when stdout replays it: %r" % texts)
        self.assertIn("All done.", texts)
        self.assertFalse(p.killed)

    def test_message_during_background_work_goes_to_the_inbox_and_cuts_nothing(self):
        h = Harness("conv-bg")
        p = h.start()
        p.feed({"event": "init", "conversation_id": "conv-bg"},
               tool(3, "ACTIVE", conv="conv-bg", CommandLine="kccolab run --name p4a", WaitMsBeforeAsync=100,
                    toolSummary="Phase 4A on the G4"))
        time.sleep(2.3)                                    # past WaitMsBeforeAsync + 2 s: agy has put it in the background
        h.send("what's going on?")
        self.assertTrue(until(lambda: inbox("conv-bg")[1]), "the message is in agy's inbox")
        msgs, marks = inbox("conv-bg")
        body = json.loads(msgs[0].read_text(encoding="utf-8"))
        self.assertEqual(body["recipient"], "conv-bg")
        self.assertTrue(body["content"].startswith(agy_engine.WORDS_FIRST), "told to answer in words before any tool")
        self.assertTrue(body["content"].endswith("what's going on?"))
        self.assertFalse(p.killed, "the turn and its background command were not cut")
        self.assertEqual(len(FakeAgy.procs), 1)
        self.assertTrue(until(lambda: h.rows("notice")))
        note = h.rows("notice")[-1]["content"]
        self.assertIn("Phase 4A on the G4", note)
        self.assertIn("nothing was stopped", note)
        self.assertNotIn("interrupted", note)
        self.assertEqual([m["content"] for m in h.rows("user")][-1], "what's going on?")
        # agy reads it at its next wake (it removes the marker) and the model answers in the same turn
        os.remove(marks[0])
        p.feed(*said(4, "Both GPUs are running; Phase 4A is at 27 of 112.", conv="conv-bg"),
               tool(3, "DONE", conv="conv-bg"), result("", conv="conv-bg"))
        p.end()
        h.th.join(5)
        self.assertEqual(len(FakeAgy.procs), 1, "a message agy read starts no second turn")
        self.assertIn("Both GPUs are running; Phase 4A is at 27 of 112.", [m.get("content") for m in h.rows("assistant")])

    def test_message_never_read_starts_the_next_turn(self):
        h = Harness("conv-unread")
        p = h.start()
        p.feed({"event": "init", "conversation_id": "conv-unread"},
               tool(3, "ACTIVE", "schedule", conv="conv-unread", DurationSeconds=480, Prompt="liveness check"))
        h.send("are the GPUs still on?")
        self.assertTrue(until(lambda: inbox("conv-unread")[1]))
        self.assertTrue(until(lambda: h.rows("notice")))
        self.assertIn("in about 8 min", h.rows("notice")[-1]["content"], "says when the model will read it")
        p.feed(tool(3, "DONE", "schedule", conv="conv-unread"), result("ok", conv="conv-unread"))
        p.end()                                            # the process ends before agy took the message
        self.assertTrue(until(lambda: len(FakeAgy.procs) == 2))
        p2 = FakeAgy.procs[1]
        self.assertTrue(until(lambda: p2.stdin_data))
        self.assertTrue(p2.prompt().startswith(agy_engine.WORDS_FIRST))
        self.assertTrue(p2.prompt().endswith("are the GPUs still on?"))
        self.assertEqual(inbox("conv-unread"), ([], []), "taken back from the inbox, so agy never reads it twice")
        self.assertEqual([m["content"] for m in h.rows("user")].count("are the GPUs still on?"), 1,
                         "shown once in the chat")
        p2.feed(result("They are on.", conv="conv-unread"))
        p2.end()
        h.th.join(5)

    def test_nothing_in_background_cuts_as_before_and_asks_for_words_first(self):
        h = Harness("conv-fg")
        p = h.start()
        p.feed({"event": "init", "conversation_id": "conv-fg"}, tool(3, "ACTIVE", conv="conv-fg",
                                                                     CommandLine="python bench.py"))
        h.send("stop and tell me where it is")
        self.assertTrue(until(lambda: p.killed), "a foreground step is cut so the message is read within seconds")
        self.assertTrue(until(lambda: len(FakeAgy.procs) == 2))
        p2 = FakeAgy.procs[1]
        self.assertTrue(until(lambda: p2.stdin_data))
        self.assertTrue(p2.prompt().startswith(agy_engine.WORDS_FIRST))
        self.assertEqual(inbox("conv-fg"), ([], []))
        p2.feed(result("Here it is.", conv="conv-fg"))
        p2.end()
        h.th.join(5)

    def test_stop_takes_an_unread_message_back(self):
        h = Harness("conv-stop")
        p = h.start()
        p.feed({"event": "init", "conversation_id": "conv-stop"},
               tool(3, "ACTIVE", "schedule", conv="conv-stop", DurationSeconds=600, Prompt="check"))
        h.send("hello?")
        self.assertTrue(until(lambda: inbox("conv-stop")[1]))
        h.run.cancel.set()
        h.th.join(5)
        self.assertTrue(p.killed)
        self.assertEqual(len(FakeAgy.procs), 1, "nothing starts after Stop")
        self.assertEqual(inbox("conv-stop"), ([], []))

    def test_print_mode_can_start_subagents_in_the_app(self):
        """No MCP server per session in print mode: the agy process carries the chat's KCH_AGENT_*, so the command
        in its first prompt (servers/agents.py agent -) starts a subagent in the app, never `claude -p`."""
        h = Harness("conv-subs")
        p = h.start()
        env = p.kwargs.get("env") or {}
        self.assertEqual(env.get("KCH_AGENT_CHAT"), h.chat.id)
        self.assertTrue(env.get("KCH_AGENT_TOKEN"))
        self.assertTrue(env.get("KCH_AGENT_URL", "").endswith("/api/subagent"))
        self.assertEqual(env.get("KCH_AGENT_CAN_SPAWN"), "1")
        self.assertIn("PATH", env, "the rest of the environment is kept")
        first = p.prompt()
        self.assertTrue(first.startswith("[Flock] "))
        self.assertIn("Subagents in this app", first)
        self.assertIn("agents.py\" agent -", first)
        self.assertIn("claude -p", first)
        p.feed(result("ok", conv="conv-subs"))
        p.end()
        h.th.join(5)

    def test_live_helpers(self):
        e = planner(7, "hi", calls=[{"name": "schedule", "args": {"DurationSeconds": "480", "Prompt": '"x"'}}],
                    at="2026-11-22T10:17:15Z")
        import calendar
        self.assertEqual(agy_live.timers(e), [calendar.timegm((2026, 11, 22, 10, 25, 15, 0, 0, 0))])
        self.assertEqual(agy_live.words(e), "hi")
        self.assertEqual(agy_live.words({"type": "SYSTEM_MESSAGE", "source": "SYSTEM", "content": "x"}), "")
        p = TMP / "t.jsonl"
        p.write_text('{"step_index": 1}\n{"step_index": 2', encoding="utf-8")
        tr = agy_live.Transcript(str(p), from_end=False)
        self.assertEqual([agy_live.idx_of(x) for x in tr.new()], [1], "a half-written line waits")
        with open(p, "a", encoding="utf-8") as f:
            f.write("}\n")
        self.assertEqual([agy_live.idx_of(x) for x in tr.new()], [2])
        sd = str(TMP / "sd")
        mid = agy_live.send_inbox(sd, "c", "x")
        self.assertFalse(agy_live.delivered(sd, mid))
        self.assertTrue(agy_live.withdraw(sd, mid))
        self.assertFalse(agy_live.withdraw(sd, mid), "a message already gone is not taken back twice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
