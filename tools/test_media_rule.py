"""Every engine is told how to show the user a sound, video or picture in the chat (agent.MEDIA_RULE).

    python -X utf8 tools/test_media_rule.py

The page turns the absolute path of a sound, video or picture written in backticks into a player or the
picture (web/app.js withMedia). Held here, with stand-ins (no model, no process started, a throwaway KCH_HOME):
the one sentence reaches
- the API models: the system prompt, and the assistant mode's prompt;
- Claude Code: the note it gets with --append-system-prompt;
- Codex: the first message of a new thread;
- Antigravity CLI (agy), over acp and in print mode: its first prompt (once per conversation)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)

TMP = Path(tempfile.mkdtemp(prefix="kch-media-"))
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
from harness import agent, agy_engine, claude_engine, codex_engine, config, store  # noqa: E402

config.load()
PROJ = str(TMP / "proj")
Path(PROJ).mkdir(exist_ok=True)
store.add_project(PROJ)
RULE = agent.MEDIA_RULE


class Started(Exception):
    pass


class Grab:
    """subprocess.Popen as a stand-in: keeps the command and what is written to its input, then ends the turn."""
    cmd, stdin = None, b""

    def __init__(self, cmd, *a, **k):
        Grab.cmd = cmd
        Grab.stdin = b""
        if "--" in cmd and "cli_input.py" in " ".join(str(x) for x in cmd):
            raise Started()
        g = self

        class _In:
            def write(self, b):
                Grab.stdin += b
                raise Started()

            def flush(self):
                pass

            def close(self):
                pass
        self.stdin = _In()
        self.pid = 4242
        self.returncode = None

    def poll(self):
        return None


def new_chat(endpoint):
    chat = store.create(PROJ, endpoint)
    chat.append({"role": "user", "content": "attach the take as audio"})
    return chat


class MediaRule(unittest.TestCase):
    def setUp(self):
        self.orig = (claude_engine.subprocess.Popen, config.claude_exe, config.codex_exe)
        claude_engine.subprocess.Popen = Grab
        config.claude_exe = lambda: "claude"
        config.codex_exe = lambda: "codex"

    def tearDown(self):
        claude_engine.subprocess.Popen, config.claude_exe, config.codex_exe = self.orig

    def test_one_sentence(self):
        self.assertEqual(RULE, "To show the user a sound, video or picture in the chat, write the file's absolute path "
                               "in backticks on its own line; it appears as a player or picture.")

    def test_api_models(self):
        chat = new_chat("deepseek")
        meta = chat.meta()
        ep = {"id": "deepseek", "kind": "openai", "context": 128000}
        self.assertIn(RULE, agent.system_prompt(meta, ep))
        self.assertIn(RULE, agent.system_prompt({**meta, "mode": "assistant"}, ep), "the assistant mode too")

    def test_claude_code(self):
        chat = new_chat("claude")
        run = agent.Run(chat.id)
        try:
            claude_engine.turn(run, chat, chat.meta(), {"id": "claude", "kind": "claude-code", "model": "opus"},
                               lambda *a: None)
        except Started:
            pass
        self.assertIsNotNone(Grab.cmd, "Claude Code was started")
        note = Grab.cmd[Grab.cmd.index("--append-system-prompt") + 1]
        self.assertIn(RULE, note)

    def test_codex(self):
        chat = new_chat("chatgpt")
        run = agent.Run(chat.id)
        try:
            codex_engine.turn(run, chat, chat.meta(), {"id": "chatgpt", "kind": "codex"}, lambda *a: None)
        except Started:
            pass
        self.assertIsNotNone(Grab.cmd, "Codex was started")
        self.assertIn(RULE, Grab.stdin.decode("utf-8"))

    def test_neon_first_prompt_once(self):
        chat = new_chat("neon")
        chat.update_meta(agy_conversation="conv-media")
        ep = {"id": "neon", "name": "Gemini 4 Neon", "kind": "agy"}
        first = agy_engine._first_text(chat, ep, lambda *a: None, "attach the take as audio")
        self.assertIn(RULE, first)
        self.assertTrue(first.endswith("attach the take as audio"))
        again = agy_engine._first_text(store.Chat(chat.id), ep, lambda *a: None, "next")
        self.assertEqual(again, "next", "once per conversation")
        # both Neon paths use this first prompt: acp (agy_acp.turn gets it) and print mode (_one_turn)
        src = (Path(agy_engine.__file__)).read_text(encoding="utf-8")
        self.assertIn("text = _first_text(chat, ep, emit, text)", src)
        self.assertIn("_first_text(chat, ep, emit, prompt_text) if first_turn", src)

    def test_neon_gets_it_without_subagents(self):
        from harness import subagents
        orig = subagents.offered
        subagents.offered = lambda meta: False
        try:
            chat = new_chat("neon")
            chat.update_meta(agy_conversation="conv-media-2")
            first = agy_engine._first_text(chat, {"id": "neon", "kind": "agy"}, lambda *a: None, "hi")
        finally:
            subagents.offered = orig
        self.assertIn(RULE, first)
        self.assertNotIn("Subagents in this app", first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
