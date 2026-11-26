"""Token-waste fixes from the 20 Nov audit.

    python -X utf8 tools/test_waste.py

1. A connection's "autocompact" reaches `claude -p` as --autocompact (Opus compacts at 650k, the user 19 Nov), on both copies.
2. A Codex chat's context size is the last API call's input from Codex's rollout file, not the turn's summed input
   (a real 6-turn chat showed 10.03M on a 500k window; its last call really held 336k).

Runs against a throwaway KCH_HOME and a fake CODEX_HOME; starts no model and no Claude process."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-waste-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import claude_engine, codex_engine, config, store  # noqa: E402

config.load()


class Stop(Exception):
    pass


class Autocompact(unittest.TestCase):
    def cmd_for(self, ep):
        seen = {}

        def popen(cmd, **kw):
            seen["cmd"] = cmd
            raise Stop()
        proj = str(TMP / "proj")
        os.makedirs(proj, exist_ok=True)
        chat = store.create(proj, ep["id"])
        chat.append({"role": "user", "content": "hi"})
        old_popen, old_exe = claude_engine.subprocess.Popen, config.claude_exe
        claude_engine.subprocess.Popen, config.claude_exe = popen, (lambda: "claude")
        try:
            with self.assertRaises(Stop):
                claude_engine.turn(None, chat, chat.meta(), ep, lambda *a, **k: None)
        finally:
            claude_engine.subprocess.Popen, config.claude_exe = old_popen, old_exe
        return seen["cmd"]

    def test_set(self):
        cmd = self.cmd_for({"id": "claude", "kind": "claude-code", "model": "claude-opus-4-5", "autocompact": 650000})
        self.assertIn("--autocompact", cmd)
        self.assertEqual(cmd[cmd.index("--autocompact") + 1], "650000")

    def test_default_and_disabled(self):
        cmd = self.cmd_for({"id": "sonnet", "kind": "claude-code", "model": "claude-sonnet-4-5"})
        self.assertIn("--autocompact", cmd)
        self.assertEqual(cmd[cmd.index("--autocompact") + 1], "200000")
        cmd_off = self.cmd_for({"id": "sonnet", "kind": "claude-code", "model": "claude-sonnet-4-5", "autocompact": 0})
        self.assertNotIn("--autocompact", cmd_off)


class CodexContext(unittest.TestCase):
    def setUp(self):
        self.home = TMP / "codex"
        day = self.home / "sessions" / time.strftime("%Y/%m/%d")
        day.mkdir(parents=True, exist_ok=True)
        self.tid = "01a1f7ef-test-thread"
        rows = [{"type": "session_meta", "payload": {"id": self.tid}}]
        for n in (23181, 120000, 335840):        # three API calls in one turn: the turn's sum would be 479k
            rows.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": n, "cached_input_tokens": n - 500},
                "total_token_usage": {"input_tokens": 479021}}}})
        rows.append({"type": "event_msg", "payload": {"type": "token_count", "info": None}})   # a rate-limit-only event
        (day / f"rollout-2026-11-20T07-50-40-{self.tid}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_last_call(self):
        self.assertEqual(codex_engine.rollout_ctx(self.tid, home=str(self.home)), 335840)

    def test_missing(self):
        self.assertIsNone(codex_engine.rollout_ctx("no-such-thread", home=str(self.home)))
        self.assertIsNone(codex_engine.rollout_ctx(None, home=str(self.home)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
