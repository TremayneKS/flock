"""A turn runs until the model finishes or the user stops it: no step cap, no time budget (user requirement).

    python -X utf8 tools/test_nosteps.py

A stand-in model asks for a tool 400 times, then answers; the tools are stand-ins too. Runs against a throwaway
KCH_HOME, never the user's data, no network."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-nosteps-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import agent, config, providers, store, subagents  # noqa: E402

config.load()
EP = {"id": "fake", "name": "Fake model", "kind": "openai", "context": 10 ** 9, "base_url": "http://127.0.0.1:9/v1",
      "model": "fake"}


class Model:
    """Asks for a read-only tool `n` times, then gives its answer."""

    def __init__(self, n):
        self.n, self.calls = n, 0

    def __call__(self, ep, messages, tools, reasoning, on_event, cancel, session=None, max_tokens=None, timeout=None):
        if cancel.is_set():
            raise providers.ModelError("cancelled", kind="cancelled")
        self.calls += 1
        r = providers.Result()
        r.t_start = r.t_end = time.time()
        r.model = "fake"
        if self.calls <= self.n:
            r.tool_calls = [{"id": "c%d" % self.calls, "name": "Glob", "arguments": '{"pattern": "*.md"}'}]
            r.finish = "tool_calls"
        else:
            r.text = "All done: I looked %d times." % self.n
            r.finish = "stop"
        return r


class NoStepCap(unittest.TestCase):
    def setUp(self):
        self._saved = (config.endpoint, providers.stream_chat, agent.run_tools, agent.estimate)
        config.endpoint = lambda eid=None: EP
        agent.run_tools = lambda run, chat, meta, ep, calls: [{"text": "README.md", "ok": True} for _ in calls]
        agent.estimate = lambda *a, **k: 0

    def tearDown(self):
        config.endpoint, providers.stream_chat, agent.run_tools, agent.estimate = self._saved

    def turn(self, model, stop_after=None):
        providers.stream_chat = model
        proj = str(TMP / "proj")
        Path(proj).mkdir(exist_ok=True)
        store.add_project(proj)
        chat = store.create(proj, "fake", reasoning="fast")
        run = agent.Run(chat.id)
        agent.runs[chat.id] = run
        if stop_after:
            def stopper():
                while model.calls < stop_after:
                    time.sleep(0.01)
                run.cancel.set()
            threading.Thread(target=stopper, daemon=True).start()
        agent._turn(run, chat, "Look at the files as many times as you need.", [])
        return chat.messages()

    def test_a_turn_goes_past_150_steps_and_finishes(self):
        msgs = self.turn(Model(400))
        tools_run = [m for m in msgs if m.get("role") == "tool"]
        notices = [m.get("content") or "" for m in msgs if m.get("role") == "notice"]
        self.assertEqual(len(tools_run), 400)
        self.assertFalse(any("Stopped after" in n for n in notices), notices)
        self.assertEqual(msgs[-1].get("role"), "assistant")
        self.assertIn("All done", msgs[-1].get("content") or "")

    def test_stop_still_stops_it(self):
        msgs = self.turn(Model(10 ** 6), stop_after=30)
        tools_run = [m for m in msgs if m.get("role") == "tool"]
        self.assertLess(len(tools_run), 10 ** 6)
        self.assertGreaterEqual(len(tools_run), 29)

    def test_no_step_cap_or_time_budget_left(self):
        self.assertFalse(hasattr(agent, "MAX_STEPS"))
        self.assertFalse(hasattr(agent.Run("x"), "max_steps"))
        s = subagents.settings()
        self.assertNotIn("max_steps", s)
        self.assertNotIn("max_minutes", s)

    def test_the_other_limits_are_gone_too(self):
        """the user, 20 Nov: no turn end after two fix rounds, no 600 s command ceiling, no 600 s model silence limit,
        no 45 min wait limit for a model server, no 10 min limit on the screen-control question."""
        import inspect
        from harness import checks, computer, tools
        self.assertFalse(hasattr(checks, "MAX_FIX"))
        self.assertNotIn("runtime_wait_min", config.DEFAULTS if hasattr(config, "DEFAULTS") else {})
        self.assertNotIn("deadline", inspect.getsource(agent._wait_runtime))
        self.assertNotIn("still fail after", inspect.getsource(agent))
        self.assertIsNone(computer.ASK_TIMEOUT)
        self.assertNotIn('"timeout"', inspect.getsource(computer.ask))
        self.assertNotIn("600)", inspect.getsource(tools.t_bash))
        self.assertIn("timeout = float(timeout) if timeout else None", inspect.getsource(providers.stream_chat))


if __name__ == "__main__":
    unittest.main(verbosity=2)
