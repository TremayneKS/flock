"""A working chat shows the step that is running and since when, not just the word "Working".

    python -X utf8 tools/test_activity.py

Held here, with stand-ins only (no model, no network, a throwaway KCH_HOME):
- any engine: a call that starts is the chat's activity until its result comes back; the turn ending clears it;
- the harness's own Bash: the activity carries the command's output file, so the page shows its last line;
- agy: a step that goes ACTIVE is the activity, with agy's own background-task log for a command (its last line
  is shown) and an end time for a timer; DONE clears it; steps a stopped process left behind are cleared;
- /api/busy: each working chat comes with what it does and how many seconds ago that started (never a clock time)."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-activity-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
os.environ["KCH_AGY_ACP"] = "0"           # the print-mode engine (agy_acp.py has its own tests: test_agy_acp.py)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import activity, agent, agy_engine, config, store  # noqa: E402

config.load()
PROJ = str(TMP / "proj")
Path(PROJ).mkdir(exist_ok=True)
store.add_project(PROJ)
NEON = {"id": "neon", "name": "Gemini 4 Neon", "kind": "agy"}


def until(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.03)
    return bool(cond())


class FakeAgy:
    """agy --print as a stand-in: the test feeds its stream-json lines one at a time."""

    def __init__(self, *a, **k):
        self.lines = []
        self.lock = threading.Condition()
        self.ended = False
        self.returncode = None
        self.pid = 999999
        FakeAgy.last = self
        test = self

        class _In:
            def write(self, b):
                pass

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

    def feed(self, ev):
        with self.lock:
            self.lines.append(ev)
            self.lock.notify_all()

    def end(self):
        with self.lock:
            self.ended = True
            self.returncode = 0
            self.lock.notify_all()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        until(lambda: self.ended, timeout or 10)
        return 0


def step(idx, state, tool="run_command", **params):
    return {"event": "step_update", "step_update": {"conversation_id": "conv-1", "step_index": idx, "state": state,
                                                     "step_type": "tool", "tool_name": tool,
                                                     "tool_info": {"name": tool, "parameters": params}}}


class Activity(unittest.TestCase):
    def test_events_any_engine(self):
        cid = "chat-a"
        agent.emit(cid, {"type": "tool_start", "call": {"id": "c1", "name": "Bash", "args": json.dumps({"command": "python bench.py --long"})}})
        a = activity.now(cid)
        self.assertEqual(a["what"], "Bash: python bench.py --long")
        agent.emit(cid, {"type": "tool_start", "call": {"id": "c2", "name": "Read", "args": json.dumps({"file_path": "x.py"})}})
        self.assertEqual(activity.now(cid)["what"], "Bash: python bench.py --long", "the step running longest is shown")
        self.assertEqual(activity.now(cid)["n"], 2)
        agent.emit(cid, {"type": "message", "message": {"role": "tool", "tool_call_id": "c1", "content": "done"}})
        self.assertEqual(activity.now(cid)["what"], "Read: x.py")
        agent.emit(cid, {"type": "turn_end"})
        self.assertIsNone(activity.now(cid))

    def test_harness_bash_output_line(self):
        from harness import resume
        cid = "chat-b"
        out, _, _ = resume.bash_files(cid, "c9")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("step 1 of 40\nstep 2 of 40\r\n\x1b[32mstep 3 of 40\x1b[0m\n", encoding="utf-8")
        agent.emit(cid, {"type": "tool_start", "call": {"id": "c9", "name": "Bash", "args": json.dumps({"command": "./run"})}})
        self.assertEqual(activity.now(cid)["line"], "step 3 of 40")
        agent.emit(cid, {"type": "turn_end"})

    def test_busy_payload_has_seconds_not_clock(self):
        cid = "chat-c"
        activity.start(cid, "k", "Bash: sleep", since=time.time() - 125)
        got = agent.activities([cid])[cid]
        self.assertGreaterEqual(got["ago"], 124)
        self.assertLess(got["ago"], 130)
        activity.clear(cid)
        activity.touch(cid, time.time() - 600)
        got = agent.activities([cid])[cid]
        self.assertTrue(got.get("idle"))
        self.assertGreaterEqual(got["ago"], 599)

    def test_agy_steps_and_task_log(self):
        logs = TMP / "brain"
        agy_engine.TASK_LOGS = str(logs)
        chat = store.create(PROJ, "neon")
        chat.update_meta(agy_conversation="conv-1")
        chat.append({"role": "user", "content": "build it"})
        run = agent.Run(chat.id)
        orig_popen, orig_exe = agy_engine.subprocess.Popen, agy_engine.agy_exe
        agy_engine.subprocess.Popen = FakeAgy
        agy_engine.agy_exe = lambda: "agy"
        try:
            th = threading.Thread(target=agy_engine.turn, args=(run, chat, chat.meta(), NEON, agent.emit), daemon=True)
            th.start()
            self.assertTrue(until(lambda: getattr(FakeAgy, "last", None) is not None))
            p = FakeAgy.last
            p.feed({"event": "init", "conversation_id": "conv-1"})
            p.feed(step(7, "ACTIVE", CommandLine="kccolab run --runtime g4 --name stacka", toolSummary="Run phase 3 on the G4"))
            self.assertTrue(until(lambda: (activity.now(chat.id) or {}).get("what") == "Bash: Run phase 3 on the G4"))
            log = logs / "conv-1" / ".system_generated" / "tasks" / "task-7.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("[42:40] | [27/32] hmed_03 PASS\n[42:51] | ROLE-SPLIT: ACC=96.88%\n", encoding="utf-8")
            self.assertEqual(activity.now(chat.id)["line"], "[42:51] | ROLE-SPLIT: ACC=96.88%")
            p.feed(step(9, "ACTIVE", tool="schedule", DurationSeconds=600, Prompt="check the run"))
            self.assertTrue(until(lambda: (activity.now(chat.id) or {}).get("n") == 2))
            p.feed(step(7, "DONE", CommandLine="kccolab run"))
            self.assertTrue(until(lambda: (activity.now(chat.id) or {}).get("what", "").startswith("Timer: check the run")))
            self.assertGreater(activity.now(chat.id)["until"], time.time() + 500)
            p.end()                                     # the process stops with the timer still ACTIVE
            th.join(5)
            self.assertIsNone(activity.now(chat.id), "steps a finished process left behind are cleared")
        finally:
            agy_engine.subprocess.Popen, agy_engine.agy_exe = orig_popen, orig_exe

    def test_claude_background_wait_and_restart(self):
        from harness import claude_engine
        cid = "chat-claude-bg"
        out = TMP / "engine.out"
        evs = [{"type": "system", "subtype": "task_started", "task_id": "b1", "description": "Run the 40-minute bench",
                "is_backgrounded": False, "output_file": str(TMP / "b1.out")},
               {"type": "system", "subtype": "task_updated", "task_id": "b1", "patch": {"is_backgrounded": True}},
               {"type": "system", "subtype": "task_started", "task_id": "b2", "description": "quick ls",
                "is_backgrounded": True},
               {"type": "system", "subtype": "task_notification", "task_id": "b2"},
               {"type": "system", "subtype": "task_started", "task_id": "b3", "description": "foreground only",
                "is_backgrounded": False}]
        out.write_text("".join(json.dumps(e) + "\n" for e in evs), encoding="utf-8")
        (TMP / "b1.out").write_text("case 17 of 40\n", encoding="utf-8")
        bg = {}
        claude_engine._bg_since_restart(cid, str(out), out.stat().st_size, bg)
        a = activity.now(cid)
        self.assertEqual(a["what"], "Waiting on background: Run the 40-minute bench",
                         "a command moved to the background before a restart is shown again after it")
        self.assertEqual(a["line"], "case 17 of 40")
        self.assertEqual(a["n"], 1, "an ended one and a foreground one are not")
        self.assertEqual(list(bg), ["b1"])
        activity.clear(cid)

    def test_background_survives_turn_end(self):
        cid = "chat-bg-keep"
        activity.start(cid, "acp:t1", "Timer: check the run", bg=True)
        activity.start(cid, "c1", "Read: x.py")
        agent.emit(cid, {"type": "turn_end"})
        a = activity.now(cid)
        self.assertEqual(a["what"], "Timer: check the run")
        self.assertTrue(a.get("bg"))
        self.assertIn(cid, agent.background_activities([]))
        self.assertNotIn(cid, agent.background_activities([cid]), "a busy chat is not listed as background")
        activity.done(cid, "acp:t1")
        self.assertIsNone(activity.now(cid))

    def test_summary(self):
        self.assertEqual(activity.summary("Bash", '{"command": "a\\nb"}'), "Bash: a")
        self.assertEqual(activity.summary("Read", {"file_path": "x"}), "Read: x")
        self.assertEqual(activity.summary("Weird", "not json"), "Weird")
        self.assertLessEqual(len(activity.summary("Bash", {"command": "x" * 500})), activity.WHAT_CHARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
