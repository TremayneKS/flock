"""A message sent while a model works is read within seconds, on every engine, and destroys no work (user requirement).

    python -X utf8 tools/test_steer.py

What is held here:
- Claude Code: a message goes in while Claude idles on commands it left in the background (its input used to be closed
  by then, so the message waited for all of them); a reply being written is cut after agent.GEN_GRACE; a subagent's
  late report never cuts anything.
- The app's own loop (any model over an API): a reply being written is cut after agent.GEN_GRACE too.
- Subagents, every engine: one subagent is one chat (a call continues the chat this parent already has for it); a
  message from the user ends the WAIT for a subagent, never the subagent, whose report is then delivered as a message;
  an engine cancelling its tool call does not stop the subagent; a call that never ran leaves no chat behind.
Stand-ins only: no model, no network, a throwaway KCH_HOME, never the user's data."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-steer-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import agent, claude_engine, config, providers, store, subagents  # noqa: E402

config.load()
PROJ = str(TMP / "proj")
Path(PROJ).mkdir(exist_ok=True)
store.add_project(PROJ)
EP = {"id": "fake", "name": "Fake model", "kind": "openai", "context": 10 ** 9, "base_url": "http://127.0.0.1:9/v1",
      "model": "fake"}
NEON = {"id": "neon", "name": "Gemini 4 Neon", "kind": "agy"}


def until(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return bool(cond())


# ------------------------------------------------------------------------------------------- a stand-in Claude CLI
class Stdin:
    def __init__(self):
        self.lines, self.closed = [], False

    def write(self, b):
        if self.closed:
            raise OSError("closed")
        self.lines.append((time.time(), json.loads(b.decode())))

    def flush(self):
        pass

    def close(self):
        self.closed = True


class Proc:
    pid = 4_000_000                  # no such process: nothing here may ever be killed

    def __init__(self):
        self.stdin, self.done, self.returncode = Stdin(), threading.Event(), None

    def poll(self):
        return 0 if self.done.is_set() else None

    def wait(self, timeout=None):
        self.done.wait(timeout)
        self.returncode = 0
        return 0


class Cli:
    """The test writes the CLI's events into the file the engine follows and reads what the engine wrote to its input."""

    def __init__(self):
        self.chat = store.create(PROJ, "fake", reasoning="fast")
        self.run = agent.Run(self.chat.id)
        self.proc = Proc()
        d = TMP / ("engine-" + self.chat.id)
        d.mkdir(parents=True)
        self.out, self.err = d / "engine.out", d / "engine.err"
        self.out.write_bytes(b"")
        self.err.write_bytes(b"")
        self.events, self.got = [], {}
        self.thread = threading.Thread(target=self._go, daemon=True)
        self.thread.start()

    def _go(self):
        self.got["completed"] = claude_engine._follow(
            self.run, self.chat, {**EP, "kind": "claude-code"}, lambda cid, ev: self.events.append(ev), "high",
            self.proc, self.out, self.err, 0, [{"type": "text", "text": "the first message"}])

    def ev(self, *objs):
        with open(self.out, "ab") as f:
            for o in objs:
                f.write((json.dumps(o) + "\n").encode())

    def wrote(self, kind):
        return [(t, o) for t, o in self.proc.stdin.lines if o.get("type") == kind]

    def user_texts(self):
        out = []
        for _, o in self.wrote("user"):
            out.append(" ".join(b.get("text", "") for b in o["message"]["content"] if b.get("type") == "text"))
        return out

    def say(self, text, soft=False, age=0.0):
        item = {"id": store.new_id("q"), "text": text, "files": [], "t": time.time() - age}
        if soft:
            item["soft"] = True
        self.run.queue.append(item)
        return time.time()

    def end(self):
        self.proc.done.set()
        self.thread.join(6)

    def roles(self):
        return [m.get("role") for m in self.chat.messages()]


N = [0]


def start():
    N[0] += 1
    return {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "msg_%d" % N[0]}}}


def said(text=None, tool=None, name="Bash"):
    content = ([{"type": "text", "text": text}] if text else []) + \
              ([{"type": "tool_use", "id": tool, "name": name, "input": {}}] if tool else [])
    return {"type": "assistant", "message": {"id": "msg_%d" % N[0], "model": "fake", "content": content,
                                             "usage": {"input_tokens": 1, "output_tokens": 1}}}


def came_back(tool, text="ok", err=False):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool, "content": text, "is_error": err}]}}


def result(err=False):
    return {"type": "result", "subtype": "error_during_execution" if err else "success", "is_error": err}


def task(tid, started=True):
    if started:
        return {"type": "system", "subtype": "task_started", "task_id": tid, "is_backgrounded": True, "task_type": "local_bash"}
    return {"type": "system", "subtype": "task_notification", "task_id": tid, "status": "completed"}


class Patched(unittest.TestCase):
    """Constants and functions a case changes are put back after it."""

    def setUp(self):
        self._saved = (agent.STEER_GRACE, agent.GEN_GRACE, claude_engine.IDLE_CLOSE_S, subagents.run, subagents._busy,
                       agent.start, config.endpoint, providers.stream_chat, agent.estimate)

    def tearDown(self):
        (agent.STEER_GRACE, agent.GEN_GRACE, claude_engine.IDLE_CLOSE_S, subagents.run, subagents._busy,
         agent.start, config.endpoint, providers.stream_chat, agent.estimate) = self._saved


# ---------------------------------------------------------------------------------------------------- Claude Code
class ClaudeCode(Patched):
    def test_a_message_goes_in_while_claude_idles_on_background_work(self):
        """The 21 Nov case: Claude ended its reply with a benchmark still running in the background; the user's message
        then waited for the whole benchmark. Now it is written into Claude's input within a second, nothing is cut."""
        c = Cli()
        c.ev(start(), task("b1"), said("started the benchmark"), result())
        time.sleep(1.2)
        self.assertFalse(c.proc.stdin.closed, "the input must stay open while Claude's background command runs")
        t = c.say("are you still there?")
        self.assertTrue(until(lambda: len(c.wrote("user")) == 2))
        self.assertLess(c.wrote("user")[1][0] - t, 1.5)
        self.assertIn("are you still there?", c.user_texts()[1])
        self.assertEqual(c.wrote("control_request"), [], "nothing was running: nothing may be interrupted")
        c.ev(start(), said("yes, still here"), result())
        time.sleep(0.8)
        self.assertFalse(c.proc.stdin.closed, "the background command is still running")
        c.ev(task("b1", started=False), start(), said("the benchmark finished"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed), "everything ended: the input closes so Claude can end")
        c.end()
        self.assertTrue(c.got["completed"])
        mine = [m for m in c.chat.messages() if m.get("role") == "user"]
        self.assertTrue(mine and mine[-1].get("steer"))

    def test_a_message_is_forced_through_when_the_cut_gets_no_answer(self):
        """21 Nov, twice: Claude had answered and was up only for a background command; the user's message waited 16 and 3
        minutes. Here the engine believes Claude is writing (a stray start with no result after it), so it sends a
        cut, and nothing comes back. The message must still go in, by the guard, within STUCK_S."""
        saved = claude_engine.STUCK_S
        claude_engine.STUCK_S, agent.GEN_GRACE = 2, 1
        try:
            c = Cli()
            c.ev(start(), task("b1"), said("started the long command"), result())
            time.sleep(0.8)
            c.ev(start())                                # something began on Claude's side and never ends in a result
            time.sleep(0.6)
            t = c.say("are you there?")
            self.assertTrue(until(lambda: len(c.wrote("control_request")) == 1, 4), "the cut is tried first")
            self.assertTrue(until(lambda: len(c.wrote("user")) == 2, 5), "the message must not wait for the command")
            self.assertLess(c.wrote("user")[1][0] - t, 4.5)
            self.assertIn("are you there?", c.user_texts()[1])
            self.assertEqual(c.run.queue, [])
            c.ev(start(), said("yes"), result())
            c.ev(task("b1", started=False), start(), said("the command finished"), result())
            self.assertTrue(until(lambda: c.proc.stdin.closed, 4))
            c.end()
            mine = [m for m in c.chat.messages() if m.get("role") == "user"]
            self.assertTrue(mine and mine[-1].get("steer"))
        finally:
            claude_engine.STUCK_S = saved

    def test_a_message_waiting_with_the_input_shut_ends_the_process_for_a_new_turn(self):
        """Claude's input is shut (its turn ended with nothing known in the background) but its process is still up.
        A message that arrives then has no way in: the process is ended, so the turn starts again with the message."""
        from harness import osutil
        saved, real_kill, killed = claude_engine.STUCK_S, osutil.kill_tree, []
        claude_engine.STUCK_S = 1
        osutil.kill_tree = lambda pid, *a, **k: killed.append(pid)
        try:
            c = Cli()
            c.ev(start(), said("done"), result())
            self.assertTrue(until(lambda: c.proc.stdin.closed, 2))
            c.say("one more thing")
            self.assertTrue(until(lambda: killed == [Proc.pid], 4), "the process must be ended for the message")
            self.assertEqual([q["text"] for q in c.run.queue], ["one more thing"], "the message stays for the new turn")
            self.assertEqual(len(c.wrote("user")), 1, "nothing can be written into a shut input")
            c.end()
        finally:
            claude_engine.STUCK_S, osutil.kill_tree = saved, real_kill

    def test_a_subagents_late_report_is_never_forced_through(self):
        saved = claude_engine.STUCK_S
        claude_engine.STUCK_S = 1
        try:
            c = Cli()
            c.ev(start())                                # Claude is working
            c.say("[Report from subagent X]", soft=True)
            time.sleep(2.5)
            self.assertEqual(c.wrote("control_request"), [])
            self.assertEqual(len(c.wrote("user")), 1, "a report waits for the next natural break")
            c.ev(said("done"), result())
            self.assertTrue(until(lambda: len(c.wrote("user")) == 2, 3))
            c.ev(start(), said("noted"), result())
            c.end()
        finally:
            claude_engine.STUCK_S = saved

    def test_the_steering_loop_survives_an_error_and_says_so(self):
        saved_due, hits = agent.steer_due, []

        def broken(run, since):
            hits.append(1)
            if len(hits) == 1:
                raise RuntimeError("boom")
            return saved_due(run, since)

        agent.steer_due = broken
        agent.STEER_GRACE = 1
        try:
            c = Cli()
            c.ev(start(), said(tool="t1"))               # a tool call is running
            t = c.say("change of plan")
            self.assertTrue(until(lambda: len(c.wrote("control_request")) == 1, 5), "the loop must carry on after an error")
            c.ev(came_back("t1", err=True), result(err=True))
            self.assertTrue(until(lambda: len(c.wrote("user")) == 2, 3))
            c.ev(start(), said("ok"), result())
            c.end()
            self.assertIn("steering loop: ", (config.LOGS / "errors.log").read_text(encoding="utf-8"))
        finally:
            agent.steer_due = saved_due

    def test_steering_decisions_are_logged(self):
        c = Cli()
        c.ev(start(), task("b1"), said("started"), result())
        time.sleep(0.8)
        c.say("hello")
        self.assertTrue(until(lambda: len(c.wrote("user")) == 2, 3))
        c.ev(start(), said("hi"), result(), task("b1", started=False), start(), said("done"), result())
        c.end()
        log = (config.LOGS / "steer.log").read_text(encoding="utf-8")
        self.assertIn(c.chat.id + " written in while idle on background work", log)

    def test_the_input_closes_when_the_background_work_ends_and_no_turn_follows(self):
        claude_engine.IDLE_CLOSE_S = 1
        c = Cli()
        c.ev(start(), task("b1"), said("started"), result())
        time.sleep(0.6)
        self.assertFalse(c.proc.stdin.closed)
        c.ev(task("b1", started=False))
        self.assertTrue(until(lambda: c.proc.stdin.closed, 4), "a turn that never comes must not hold the chat open")
        c.end()
        self.assertTrue(c.got["completed"])

    def test_the_empty_results_a_resumed_session_begins_with_do_not_shut_the_input(self):
        """23 Nov: a resumed session first reports the agents the last process took with it as stopped, with an empty
        result each (num_turns 0), before it reads the message. The input must stay open through them, so the agents
        Claude then starts are not ended for the next message."""
        c = Cli()
        stopped = {"type": "system", "subtype": "task_notification", "task_id": "a_old", "status": "stopped"}
        empty = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "result": ""}
        c.ev(stopped, {"type": "system", "subtype": "init"}, empty, {"type": "system", "subtype": "init"}, empty)
        time.sleep(0.8)
        self.assertFalse(c.proc.stdin.closed, "an empty result ends nothing")
        c.ev(start(), said(tool="tu1", name="Agent"), task("a_new"), came_back("tu1", "Async agent launched"),
             start(), said("the agent is working"), {**result(), "num_turns": 2, "result": "the agent is working"})
        time.sleep(0.8)
        self.assertFalse(c.proc.stdin.closed, "its new agent is still working")
        c.say("next message")
        self.assertTrue(until(lambda: len(c.wrote("user")) == 2, 3), "the message goes into the same process")
        c.ev(start(), said("got it"), result(), task("a_new", started=False), start(), said("agent done"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 4))
        c.end()

    def test_with_nothing_in_the_background_the_input_closes_at_the_result_as_before(self):
        c = Cli()
        c.ev(start(), said("done"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 2))
        c.end()
        self.assertTrue(c.got["completed"])

    def test_a_finished_foreground_command_does_not_hold_the_input_open(self):
        c = Cli()
        c.ev(start(), said(tool="t1"), task("f1"), task("f1", started=False), came_back("t1"), start(), said("done"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 2))
        c.end()

    def test_a_reply_being_written_is_cut_after_the_grace(self):
        agent.GEN_GRACE = 1
        c = Cli()
        c.ev(start())                                    # Claude is writing or thinking: no tool call is running
        t = c.say("stop, do this instead")
        self.assertTrue(until(lambda: len(c.wrote("control_request")) == 1, 4), "the reply must be cut")
        waited = c.wrote("control_request")[0][0] - t
        self.assertGreaterEqual(waited, 0.9)
        self.assertLess(waited, 2.5)
        self.assertEqual(c.wrote("control_request")[0][1]["request"], {"subtype": "interrupt"})
        # what the real CLI does then (measured): the text so far, an interrupted marker, an error result
        c.ev(said("The story so far"), {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "[Request interrupted by user]"}]}}, result(err=True))
        self.assertTrue(until(lambda: len(c.wrote("user")) == 2))
        self.assertIn("stop, do this instead", c.user_texts()[1])
        c.ev(start(), said("ok, doing that"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 2))
        c.end()
        self.assertNotIn("error", c.roles(), "a reply cut for the user's message is not an error")
        self.assertIn("The story so far", [m.get("content") for m in c.chat.messages()], "the text so far is kept")

    # 23 Nov 20:51: a message sent while the main turn ran was cut in with the CLI's interrupt, which ended every
    # subagent Claude had working ("killed"). With subagents working, the message is written in instead; a long
    # foreground command of the main turn is ended alone (stop_task), the agents never.
    @staticmethod
    def agent_task(tid, tool, backgrounded=True):
        return {"type": "system", "subtype": "task_started", "task_id": tid, "tool_use_id": tool,
                "is_backgrounded": backgrounded, "task_type": "local_agent"}

    def test_with_subagents_working_a_long_command_is_stopped_alone_and_the_message_written_in(self):
        agent.STEER_GRACE = 1
        c = Cli()
        c.ev(start(), said(tool="tu_a", name="Agent"), self.agent_task("a_lead", "tu_a"),
             came_back("tu_a", "Async agent launched"), start(), said(tool="tu_b"),
             {"type": "system", "subtype": "task_started", "task_id": "bfg000001", "tool_use_id": "tu_b",
              "is_backgrounded": False, "task_type": "local_bash"},
             # a command of the agent's own: never stopped by the main turn's message
             {"type": "system", "subtype": "task_started", "task_id": "bsub00001", "tool_use_id": "tu_x",
              "is_backgrounded": False, "task_type": "local_bash", "owned_by_subagent": True, "parent_task_id": "a_lead"})
        time.sleep(0.3)
        c.say("also do this")
        self.assertTrue(until(lambda: len(c.wrote("user")) == 2, 5), "the message goes straight in")
        self.assertIn("also do this", c.user_texts()[1])
        self.assertTrue(until(lambda: len(c.wrote("control_request")) == 1, 2))
        reqs = [o["request"] for _, o in c.wrote("control_request")]
        self.assertEqual(reqs, [{"subtype": "stop_task", "task_id": "bfg000001"}], "only the main turn's command")
        time.sleep(1.5)
        self.assertEqual(len(c.wrote("control_request")), 1, "no interrupt follows")
        c.ev({"type": "system", "subtype": "task_notification", "task_id": "bfg000001", "status": "stopped"},
             came_back("tu_b", "Exit code 137", err=True), start(), said("ok, doing that too"), result())
        def tool():
            return [m for m in c.chat.messages() if m.get("role") == "tool" and m.get("tool_call_id") == "tu_b"]
        self.assertTrue(until(lambda: tool(), 3))
        self.assertTrue(tool()[0]["content"].startswith("Stopped"), tool())
        time.sleep(0.8)
        self.assertFalse(c.proc.stdin.closed, "the agent still works: the input stays open")
        c.ev({"type": "system", "subtype": "task_notification", "task_id": "bsub00001", "status": "completed"},
             {"type": "system", "subtype": "task_notification", "task_id": "a_lead", "status": "completed"},
             start(), said("the lead reported"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 4))
        c.end()

    def test_with_subagents_working_a_reply_being_written_is_not_cut(self):
        agent.GEN_GRACE = 1
        c = Cli()
        c.ev(start(), said(tool="tu_a", name="Agent"), self.agent_task("a_lead", "tu_a"),
             came_back("tu_a", "Async agent launched"), start())       # Claude is writing, its agent works
        t = c.say("one more thing")
        self.assertTrue(until(lambda: len(c.wrote("user")) == 2, 4))
        self.assertLess(c.wrote("user")[1][0] - t, 2.5)
        time.sleep(1.5)
        self.assertEqual(c.wrote("control_request"), [], "no interrupt: it would end the agent")
        c.ev(said("done writing"), result())
        time.sleep(0.5)
        self.assertFalse(c.proc.stdin.closed)
        c.ev(start(), said("about one more thing"), result(),
             {"type": "system", "subtype": "task_notification", "task_id": "a_lead", "status": "completed"},
             start(), said("the lead reported"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 4))
        c.end()
        self.assertEqual([m.get("content") for m in c.chat.messages() if m.get("role") == "user"][-1], "one more thing")

    def test_a_quick_reply_is_not_cut(self):
        agent.GEN_GRACE = 2
        c = Cli()
        c.ev(start())
        c.say("one more thing")
        time.sleep(0.5)
        c.ev(said("done"), result())                     # it finished inside the grace
        self.assertTrue(until(lambda: len(c.wrote("user")) == 2))
        self.assertEqual(c.wrote("control_request"), [])
        c.ev(start(), said("and that"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 2))
        c.end()

    def test_a_subagents_late_report_never_cuts_anything(self):
        agent.GEN_GRACE = 1
        agent.STEER_GRACE = 1
        c = Cli()
        c.ev(start())
        c.say("[Report from subagent Neon]", soft=True, age=30)
        time.sleep(1.6)
        self.assertEqual(c.wrote("control_request"), [], "a report must not cut a reply being written")
        c.ev(said(tool="t1"))
        time.sleep(1.6)
        self.assertEqual(c.wrote("control_request"), [], "nor a running tool call")
        c.ev(came_back("t1"), start())
        time.sleep(0.5)
        self.assertEqual(c.wrote("control_request"), [], "nor the step that follows")
        c.ev(said("done"), result())
        self.assertTrue(until(lambda: len(c.wrote("user")) == 2), "it goes in when the turn ends")
        self.assertIn("[Report from subagent Neon]", c.user_texts()[1])
        c.ev(start(), said("read it"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 2))
        c.end()

    def test_a_message_cuts_the_wait_for_a_subagent_but_not_the_subagent(self):
        agent.STEER_GRACE = 1
        c = Cli()
        child = store.create(PROJ, "fake", title="Subagent: fix the thing")
        child.update_meta(sub_of=c.chat.id, sub_preset="Neon", sub_desc="fix the thing")
        subagents._links[child.id] = {"parent": c.chat.id, "call": "t1"}
        try:
            c.ev(start(), said(tool="t1", name="mcp__harness__harness_agent"))
            time.sleep(0.2)
            c.say("how is it going?")
            self.assertTrue(until(lambda: len(c.wrote("control_request")) == 1, 4))
            self.assertTrue(subagents._detached.get("t1"), "the call is marked: its subagent goes on, report later")
            c.ev(came_back("t1", "The user doesn't want to proceed with this tool use.", err=True), result(err=True))
            self.assertTrue(until(lambda: len(c.wrote("user")) == 2))
            told = c.user_texts()[1]
            self.assertIn("how is it going?", told)
            self.assertIn("did not stop your subagent call", told)
            self.assertIn('Neon ("fix the thing")', told)
            shown = [m for m in c.chat.messages() if m.get("role") == "user"][-1]
            self.assertEqual(shown.get("content"), "how is it going?", "the note is for the model, not shown as the user's words")
            c.ev(start(), said("fine"), result())
            self.assertTrue(until(lambda: c.proc.stdin.closed, 2))
            c.end()
        finally:
            subagents._links.pop(child.id, None)
            subagents._detached.pop("t1", None)


# ---------------------------------------------------------------------------------------- the rules every engine uses
class QueueRules(Patched):
    def test_a_late_report_is_not_the_user_waiting(self):
        run = agent.Run("x")
        run.queue.append({"id": "q1", "text": "report", "t": time.time() - 60, "soft": True})
        self.assertFalse(agent.steer_due(run, time.time() - 60))
        self.assertEqual(agent.steer_waited(run), 0.0)
        self.assertEqual(agent.hard_queue(run), [])
        run.queue.append({"id": "q2", "text": "from the user", "t": time.time() - 10})
        self.assertTrue(agent.steer_due(run, time.time() - 60))
        self.assertGreaterEqual(agent.steer_waited(run), 9.5)
        self.assertEqual([q["id"] for q in agent.take_queue(run)], ["q1", "q2"], "both go in, in order")

    def test_steer_cut_trips_on_stop_or_on_a_message_that_has_waited(self):
        agent.GEN_GRACE = 1
        run = agent.Run("x")
        cut = agent.SteerCut(run)
        self.assertFalse(cut.is_set())
        self.assertFalse(cut.wait(0.2))
        run.queue.append({"id": "q", "text": "hey", "t": time.time()})
        self.assertFalse(cut.is_set(), "not before the grace")
        self.assertTrue(cut.wait(2.0))
        self.assertTrue(cut.steered)
        stop = agent.Run("y")
        stop.cancel.set()
        cut2 = agent.SteerCut(stop)
        self.assertTrue(cut2.is_set())
        self.assertFalse(cut2.steered, "Stop is not a steer")

    def test_the_apps_own_loop_cuts_a_reply_for_the_users_message(self):
        """Any model over an API (DeepSeek, the local ones): the reply being written is cut after the grace and the
        user's message is in the very next request."""
        agent.GEN_GRACE = 1
        config.endpoint = lambda eid=None: EP
        agent.estimate = lambda *a, **k: 0
        seen = []

        def model(ep, messages, tools, reasoning, on_event, cancel, session=None, max_tokens=None, timeout=None):
            seen.append([m.get("content") for m in messages if m.get("role") == "user"])
            r = providers.Result()
            r.t_start = r.t_end = time.time()
            r.model = "fake"
            if len(seen) == 1:                           # a long reply: writes until it is stopped
                end = time.time() + 20
                while time.time() < end:
                    if cancel.is_set():
                        raise providers.ModelError("stopped", "cancelled")
                    time.sleep(0.05)
                r.text = "the long reply, never cut"
            else:
                r.text = "ok, doing the new thing"
            r.finish = "stop"
            return r
        providers.stream_chat = model
        chat = store.create(PROJ, "fake", reasoning="fast")
        run = agent.Run(chat.id)
        agent.runs[chat.id] = run
        th = threading.Thread(target=agent._turn, args=(run, chat, "write me a long essay", []), daemon=True)
        t0 = time.time()
        th.start()
        self.assertTrue(until(lambda: len(seen) == 1))
        run.queue.append({"id": "q1", "text": "no, do the new thing", "files": [], "t": time.time()})
        th.join(10)
        self.assertFalse(th.is_alive())
        self.assertLess(time.time() - t0, 6, "it must not wait for the long reply")
        self.assertEqual(len(seen), 2)
        self.assertTrue(any("no, do the new thing" in str(x) for x in seen[1]))
        msgs = chat.messages()
        self.assertEqual(msgs[-1].get("content"), "ok, doing the new thing")
        self.assertNotIn("error", [m.get("role") for m in msgs])


# ------------------------------------------------------------------------------------------------------- subagents
class Ctx:
    def __init__(self, chat):
        self.chat, self.cancel, self.go = chat, threading.Event(), False

    def steer(self, since):
        return self.go


class Subagents(Patched):
    def parent(self):
        return store.create(PROJ, "fake", title="Main chat")

    def child(self, parent, origin="agent", preset="", endpoint="neon", link=True):
        ch = store.create(PROJ, endpoint, title="Subagent")
        ch.update_meta(sub_of=parent.id, sub_origin=origin, sub_preset=preset, sub_name=preset or "Gemini 4 Neon",
                       sub_desc="a task")
        if link and hasattr(subagents, "_link_child"):
            subagents._link_child(parent.id, ch.id)          # the phone branch keeps a shared index of subagent chats
        elif link:
            subagents._record(parent, "rec-" + ch.id, {"id": ch.id})   # the desktop branch: only the parent's own record
        return ch

    def test_one_subagent_is_one_chat(self):
        """the user: "why does your subagent list keep growing ... it should only be the subagents that I assigned you".
        Every call used to make a new chat; six calls to Neon made six rows."""
        busy = set()
        subagents._busy = lambda cid: cid in busy
        p = self.parent()
        self.assertIsNone(subagents._reuse_for(p.id, NEON, None), "none yet: the first call makes one")
        made = self.child(p)
        self.assertEqual(subagents._reuse_for(p.id, NEON, None).id, made.id, "the next call continues it")
        busy.add(made.id)
        self.assertIsNone(subagents._reuse_for(p.id, NEON, None), "all busy and model-made: a parallel call may make one")
        mine = self.child(p, origin="user", preset="Neon")
        time.sleep(0.02)
        self.assertEqual(subagents._reuse_for(p.id, NEON, None).id, mine.id, "the one the user added comes first")
        self.assertEqual(subagents._reuse_for(p.id, NEON, {"name": "Neon"}).id, mine.id)
        busy.add(mine.id)
        self.assertEqual(subagents._reuse_for(p.id, NEON, {"name": "neon"}).id, mine.id,
                         "busy or not, a call to his subagent waits for it: no second one is made")
        self.assertIsNone(subagents._reuse_for(p.id, NEON, {"name": "Genius"}), "another named subagent is not it")
        self.assertIsNone(subagents._reuse_for(p.id, {"id": "deepseek", "name": "DeepSeek"}, None), "nor another model")
        other = self.parent()
        self.assertIsNone(subagents._reuse_for(other.id, NEON, None), "nor another chat's subagent")
        # one removed from the list is not brought back by a call (found live on 21 Nov: a check call went into a
        # row that had just been removed, because removing it had made it the most recently changed)
        q = self.parent()
        gone = self.child(q)
        kept = self.child(q)
        time.sleep(0.02)
        gone.update_meta(deleted=True)
        self.assertEqual(subagents._reuse_for(q.id, NEON, None).id, kept.id)
        kept.update_meta(deleted=True)
        self.assertIsNone(subagents._reuse_for(q.id, NEON, None))

    def test_a_message_ends_the_wait_not_the_subagent(self):
        """The app's own loop (any model over an API): the Agent call returns as soon as the user's message is due;
        the subagent goes on."""
        p = self.parent()
        ch = self.child(p, preset="Neon", link=False)
        release, state = threading.Event(), {}

        def fake_run(parent, pmeta, call_id, args, cancel, gone=None):
            subagents._links[ch.id] = {"parent": parent.id, "call": call_id}
            state["cancel"] = cancel
            release.wait(10)
            state["finished"] = True
            return {"text": "THE REPORT", "ok": True}
        subagents.run = fake_run
        ctx = Ctx(p)
        got = {}
        th = threading.Thread(target=lambda: got.update(res=subagents.tool(ctx, {"prompt": "do it"}, "c9")), daemon=True)
        th.start()
        time.sleep(0.5)
        self.assertTrue(th.is_alive(), "no message: it waits for the subagent")
        ctx.go = True
        th.join(3)
        try:
            self.assertFalse(th.is_alive(), "the user's message is due: the call stops waiting")
            self.assertEqual(got["res"], {"text": subagents.STILL_RUNNING, "ok": True, "steered": True})
            self.assertNotIn("finished", state, "the subagent itself is still running")
            self.assertFalse(state["cancel"].is_set(), "and was not told to stop")
            self.assertTrue(subagents._detached.get("c9"))
            self.assertIn('Neon ("a task")', subagents.detached_note(p.id))
            self.assertEqual(subagents.detached_note(p.id), "", "the model is told once")
        finally:
            release.set()
            subagents._links.pop(ch.id, None)
            subagents._detached.pop("c9", None)

    def test_a_detached_subagents_report_is_delivered_as_a_soft_message(self):
        p = self.parent()
        ch = self.child(p, preset="Neon")
        ch.append({"role": "user", "content": "do it"})
        ch.append({"role": "assistant", "content": "Here is my full report: all 12 tests pass."})
        sent = []
        agent.start = lambda cid, text, files=None, **kw: sent.append((cid, text, kw)) or {"queued": True, "id": "q"}
        subagents._links[ch.id] = {"parent": p.id, "call": "k1"}
        subagents._detached["k1"] = True
        res = subagents._finish(p, ch, NEON, "k1", time.time() - 5, None, subagents.settings())
        self.assertTrue(res["ok"])
        self.assertEqual(len(sent), 1, "the report is handed to the parent chat")
        cid, text, kw = sent[0]
        self.assertEqual(cid, p.id)
        self.assertTrue(kw.get("soft"), "soft: it must never cut the parent's running work")
        self.assertIn("all 12 tests pass", text)
        self.assertIn("not a message from the user", text)
        self.assertIn('on "a task"', text)
        self.assertNotIn("k1", subagents._detached)
        # a call somebody is still waiting for is returned to them, not sent as a message as well
        subagents._links[ch.id] = {"parent": p.id, "call": "k2"}
        res = subagents._finish(p, ch, NEON, "k2", time.time() - 5, None, subagents.settings())
        self.assertIn("all 12 tests pass", res["text"])
        self.assertEqual(len(sent), 1)

    def test_a_result_the_app_could_not_write_back_is_not_lost(self):
        """The engine that asked was ended (Codex and Antigravity are, to take the user's message): the connection is
        gone when the subagent finishes. This morning that report was simply dropped (ConnectionResetError)."""
        p = self.parent()
        ch = self.child(p, preset="Neon")
        ch.append({"role": "assistant", "content": "Report: committed as abc123."})
        sent = []
        agent.start = lambda cid, text, files=None, **kw: sent.append((cid, text, kw)) or {"queued": False}
        subagents._links[ch.id] = {"parent": p.id, "call": "k3"}
        res = subagents._finish(p, ch, NEON, "k3", time.time() - 5, None, subagents.settings())
        self.assertEqual(sent, [])
        self.assertTrue(subagents.undelivered({"chat": p.id}, res))
        self.assertEqual(len(sent), 1)
        self.assertIn("committed as abc123", sent[0][1])
        self.assertFalse(subagents.undelivered({"chat": p.id}, res), "once only")
        self.assertEqual(len(sent), 1)

    def test_an_engine_cancelling_its_call_does_not_stop_the_subagent(self):
        """Claude Code rejects its running tool call when it is interrupted for the user's message, and the MCP server
        then posts /cancel. That used to stop the subagent (its work was thrown away twice on 21 Nov)."""
        p = self.parent()
        seen, release = {}, threading.Event()

        def fake_run(parent, pmeta, call_id, args, cancel, gone=None):
            seen.update(cancel=cancel, gone=gone, call=call_id)
            release.wait(10)
            return {"text": "done", "ok": True}
        subagents.run = fake_run
        subagents.note_call(p.id, "call-1", {"prompt": "the task"})
        head = {"X-KC-Agent-Token": subagents.TOKEN}
        th = threading.Thread(target=lambda: subagents.http("/api/subagent", head, {"chat": p.id, "prompt": "the task", "key": "req-1"}),
                              daemon=True)
        th.start()
        self.assertTrue(until(lambda: "gone" in seen))
        self.assertEqual(seen["call"], "call-1")
        self.assertEqual(subagents.http("/api/subagent/cancel", head, {"key": "req-1"}), {"ok": True})
        self.assertTrue(seen["gone"].is_set(), "the engine gave up waiting")
        self.assertFalse(seen["cancel"].is_set(), "that is not Stop: the subagent must go on")
        release.set()
        th.join(3)

    def test_a_call_that_never_ran_leaves_no_chat_behind(self):
        p = self.parent()
        ch = self.child(p)
        subagents._record(p, "kx", {"id": ch.id, "status": "running"})
        subagents._links[ch.id] = {"parent": p.id, "call": "kx"}
        self.assertIn(ch.id, subagents._child_ids(p.id))
        lock = subagents._child_locks.setdefault(ch.id, threading.Lock())
        lock.acquire()                                   # it has to wait, and is stopped while it waits
        cancel, got = threading.Event(), {}
        th = threading.Thread(target=lambda: got.update(res=subagents._drive(
            p, ch, EP, "kx", time.time(), cancel, subagents.settings(), "the task", fresh=True)), daemon=True)
        th.start()
        time.sleep(0.4)
        cancel.set()
        th.join(4)
        lock.release()
        self.assertFalse(th.is_alive())
        self.assertFalse(got["res"]["ok"])
        self.assertIn("before the subagent started", got["res"]["text"])
        self.assertNotIn(ch.id, subagents._child_ids(p.id), "no empty subagent left in the list")
        self.assertNotIn(ch.id, subagents._links)

    def test_the_users_message_tells_the_model_once_which_subagents_go_on(self):
        p = self.parent()
        ch = self.child(p, preset="Neon")
        subagents._links[ch.id] = {"parent": p.id, "call": "k9"}
        try:
            self.assertEqual(subagents.detach_calls(p.id), ['Neon ("a task")'])
            self.assertEqual(subagents.detach_calls(p.id), [], "already detached: not named twice")
            um = agent.user_message(p, p.meta(), "what's taking so long", [])
            self.assertEqual(um["content"], "what's taking so long")
            self.assertIn("did not stop your subagent call", um["model_content"])
            self.assertIn("Do not start that work again", um["model_content"])
            um2 = agent.user_message(p, p.meta(), "and another thing", [])
            self.assertNotIn("subagent", um2.get("model_content") or "")
        finally:
            subagents._links.pop(ch.id, None)
            subagents._detached.pop("k9", None)


# ------------------------------------------------------------------- a subagent that finished always tells its main chat
class OwedReports(Patched):
    """the user, 21 Nov: "both opus subagents finished. did that not notify you? why haven't you said anything". One had
    been cut by an app restart and carried on by hand; the other was a side chat started with a task. Neither told
    the main chat when it finished."""

    def pair(self, origin="user"):
        parent = store.create(PROJ, "fake", title="Main chat")
        ch = store.create(PROJ, "fake", title="Subagent")
        ch.update_meta(sub_of=parent.id, sub_origin=origin, sub_name="Opus", sub_desc="audit the thing")
        if hasattr(subagents, "_link_child"):
            subagents._link_child(parent.id, ch.id)
        return parent, ch

    def catch(self):
        sent = []
        agent.start = lambda cid, text, files=None, **kw: sent.append((cid, text, kw)) or {"queued": False}
        return sent

    def test_a_side_chat_given_a_task_reports_when_it_finishes(self):
        sent = self.catch()
        parent, ch = self.pair()
        self.assertTrue(subagents.owe(ch.id))
        ch.append({"role": "user", "content": "audit it"})
        ch.append({"role": "assistant", "content": "The audit is finished: 6 findings."})
        self.assertTrue(subagents.turn_ended(ch.id))
        self.assertEqual(len(sent), 1)
        cid, text, kw = sent[0]
        self.assertEqual(cid, parent.id)
        self.assertTrue(kw.get("soft"), "it must not cut the main chat's running work")
        self.assertIn("6 findings", text)
        self.assertIn("finished the task it was given", text)
        self.assertIn('on "audit the thing"', text)
        self.assertFalse(subagents.turn_ended(ch.id), "told once")
        self.assertEqual(len(sent), 1)

    def test_talking_to_a_subagent_yourself_reports_nothing(self):
        sent = self.catch()
        parent, ch = self.pair()
        ch.append({"role": "user", "content": "what do you think?"})
        ch.append({"role": "assistant", "content": "I think it is fine."})
        self.assertFalse(subagents.turn_ended(ch.id), "no task was given: the user is in that chat himself")
        self.assertEqual(sent, [])

    def test_a_stopped_task_owes_nothing(self):
        sent = self.catch()
        parent, ch = self.pair()
        subagents.owe(ch.id)
        ch.append({"role": "assistant", "content": "half an answer"})
        self.assertFalse(subagents.turn_ended(ch.id, stopped=True))
        self.assertEqual(sent, [])
        self.assertFalse(ch.meta().get("sub_owes"))

    def test_a_task_that_ended_in_an_error_says_so(self):
        sent = self.catch()
        parent, ch = self.pair()
        subagents.owe(ch.id)
        ch.append({"role": "assistant", "content": "I got part of the way."})
        ch.append({"role": "error", "content": "Claude stopped: rate limit"})
        self.assertTrue(subagents.turn_ended(ch.id))
        self.assertIn("ended without a report", sent[0][1])
        self.assertIn("rate limit", sent[0][1])
        self.assertIn("part of the way", sent[0][1])

    def test_a_call_still_waiting_gets_the_answer_itself(self):
        sent = self.catch()
        parent, ch = self.pair(origin="agent")
        subagents.owe(ch.id)
        subagents._links[ch.id] = {"parent": parent.id, "call": "kw1"}
        try:
            ch.append({"role": "assistant", "content": "done"})
            self.assertFalse(subagents.turn_ended(ch.id), "the waiting call returns it: not sent twice")
            self.assertEqual(sent, [])
            res = subagents._finish(parent, ch, NEON, "kw1", time.time() - 3, None, subagents.settings())
            self.assertTrue(res["ok"])
            self.assertFalse(ch.meta().get("sub_owes"), "answered through the call")
        finally:
            subagents._links.pop(ch.id, None)

    def test_a_task_cut_by_a_restart_is_carried_on_and_then_reports(self):
        sent = self.catch()
        subagents._busy = lambda cid: False
        parent, ch = self.pair(origin="agent")
        subagents.owe(ch.id)                             # it was working for the main chat when the app went down
        ch.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "Bash", "arguments": "{}"}]})
        idle_parent, idle = self.pair()
        idle.append({"role": "assistant", "content": "just a conversation"})   # owes nothing: left alone
        started = subagents.resume_owed()
        self.assertIn(ch.id, started)
        self.assertNotIn(idle.id, started)
        mine = [s for s in sent if s[0] == ch.id]
        self.assertEqual(len(mine), 1)
        self.assertIn("restarted while you were working", mine[0][1])
        # it finishes after being carried on: the main chat is told
        ch.append({"role": "assistant", "content": "Final report after the restart."})
        self.assertTrue(subagents.turn_ended(ch.id))
        self.assertTrue(any(s[0] == parent.id and "Final report after the restart." in s[1] for s in sent))

    def test_an_old_owed_task_is_not_woken_by_itself(self):
        sent = self.catch()
        subagents._busy = lambda cid: False
        parent, ch = self.pair()
        subagents.owe(ch.id)
        ch.update_meta(touch=False, updated=time.time() - 7 * 3600)
        self.assertNotIn(ch.id, subagents.resume_owed())
        self.assertFalse(ch.meta().get("sub_owes"))


# ------------------------------------------------------------------------------- one chat, one process, over a restart
class OneProcess(unittest.TestCase):
    """21 Nov 08:40: the chats app was restarted while Claude worked on a turn it had begun by itself (a background
    command of its had ended). The turn was still marked finished from its first reply, so the new copy of the app
    stopped following the process; the user's next message ("?") then started a second process on the same session. Two
    copies of one chat worked at once for six minutes, one of them unseen."""

    def setUp(self):
        from harness import osutil, resume
        self.osutil, self.resume = osutil, resume
        self._saved = (agent._ADOPTS, osutil.same_process, osutil.kill_tree, osutil.alive, resume.note)

    def tearDown(self):
        agent._ADOPTS, self.osutil.same_process, self.osutil.kill_tree, self.osutil.alive, self.resume.note = self._saved

    def test_a_turn_is_unfinished_again_once_claude_starts_working_by_itself(self):
        notes = []
        real = self.resume.note
        self.resume.note = lambda run, **kw: (notes.append(kw), real(run, **kw))[1]
        c = Cli()
        c.ev(start(), task("b1"), said("started"), result())
        self.assertTrue(until(lambda: {"completed": True} in notes))
        c.ev(task("b1", started=False), start())             # its background command ended: it takes a turn by itself
        self.assertTrue(until(lambda: {"completed": False} in notes),
                        "working again: a restart now must carry this turn on, not drop it")
        c.ev(said("it finished"), result())
        self.assertTrue(until(lambda: c.proc.stdin.closed, 3))
        c.end()
        flags = [n["completed"] for n in notes if "completed" in n]
        self.assertEqual(flags, [True, False, True])

    def test_a_claude_that_answered_but_is_still_up_is_followed_through_a_restart(self):
        followed = []

        class Engine:
            @staticmethod
            def adopt(run, chat, ep, emit, rec):
                followed.append(rec["engine"]["pid"])
                return True
        agent._ADOPTS = {"claude-code": lambda: Engine}
        chat = store.create(PROJ, "fake", reasoning="fast")
        ep = {"id": "fake", "name": "Claude", "kind": "claude-code"}
        rec = {"completed": True, "engine": {"pid": 4_000_001, "created": 5.0}}
        self.osutil.same_process = lambda pid, created=None: True       # idle on its background work, or working again
        self.assertFalse(agent._carry_on(agent.Run(chat.id), chat, chat.meta(), ep, dict(rec)))
        self.assertEqual(followed, [4_000_001], "its process is up: the app keeps following it")
        self.osutil.same_process = lambda pid, created=None: False      # it really ended: nothing to follow
        self.assertFalse(agent._carry_on(agent.Run(chat.id), chat, chat.meta(), ep, dict(rec)))
        self.assertEqual(followed, [4_000_001])

    def test_a_leftover_process_is_ended_before_a_new_one_starts(self):
        killed = []
        chat = store.create(PROJ, "fake", reasoning="fast")
        chat.update_meta(claude_proc={"pid": 4_000_002, "created": 5.0})
        self.osutil.kill_tree = lambda pid, timeout=20: killed.append(pid)
        self.osutil.alive = lambda pid: False
        self.osutil.same_process = lambda pid, created=None: False      # that process is gone (or the number is reused)
        self.assertFalse(claude_engine.end_leftover(chat, chat.meta()))
        self.assertEqual(killed, [])
        self.osutil.same_process = lambda pid, created=None: True       # still running, unseen
        self.assertTrue(claude_engine.end_leftover(chat, chat.meta()))
        self.assertEqual(killed, [4_000_002], "one session, one process")
        self.assertFalse(claude_engine.end_leftover(chat, {}), "a chat that never ran Claude has nothing to end")


if __name__ == "__main__":
    unittest.main(verbosity=2)
