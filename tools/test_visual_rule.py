"""Every engine receives VISUAL_RULE alongside MEDIA_RULE, and the bundled charts skill is discoverable.

    python -X utf8 tools/test_visual_rule.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: throwaway KCH_HOME, never live data)

TMP = Path(tempfile.mkdtemp(prefix="kch-visual-"))
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
from harness import agent, agy_engine, claude_engine, codex_engine, config, skills, store  # noqa: E402

config.load()
PROJ = str(TMP / "proj")
Path(PROJ).mkdir(exist_ok=True)
store.add_project(PROJ)
RULE = agent.VISUAL_RULE


class Started(Exception):
    pass


class Grab:
    cmd, stdin = None, b""

    def __init__(self, cmd, *a, **k):
        Grab.cmd = cmd
        Grab.stdin = b""
        if "--" in cmd and "cli_input.py" in " ".join(str(x) for x in cmd):
            raise Started()

        class _In:
            def write(self, b):
                Grab.stdin += b
                raise Started()

            def flush(self):
                pass

            def close(self):
                pass

        self.stdin = _In()
        self.pid = 4243
        self.returncode = None

    def poll(self):
        return None


def new_chat(endpoint):
    chat = store.create(PROJ, endpoint)
    chat.append({"role": "user", "content": "plot my weekly calories"})
    return chat


class VisualRuleTest(unittest.TestCase):
    def setUp(self):
        self.orig = (claude_engine.subprocess.Popen, config.claude_exe, config.codex_exe)
        claude_engine.subprocess.Popen = Grab
        config.claude_exe = lambda: "claude"
        config.codex_exe = lambda: "codex"

    def tearDown(self):
        claude_engine.subprocess.Popen, config.claude_exe, config.codex_exe = self.orig

    def test_rule_mentions_all_visual_blocks_and_is_cmd_safe(self):
        for token in ("```chart", "```compare", "```numbers", "```table", "```steps", "[!NOTE]", "[!CAUTION]", "[!TIP]"):
            self.assertIn(token, RULE)
        for bad in ('"', "|", ">", "<", "&", "^", "%"):
            self.assertNotIn(bad, RULE, f"VISUAL_RULE must stay Windows .cmd-safe (found {bad!r})")

    def test_api_models_and_assistant(self):
        chat = new_chat("deepseek")
        meta = chat.meta()
        ep = {"id": "deepseek", "kind": "openai", "context": 128000}
        self.assertIn(RULE, agent.system_prompt(meta, ep))
        self.assertIn(RULE, agent.system_prompt({**meta, "mode": "assistant"}, ep))

    def test_claude_code(self):
        chat = new_chat("claude")
        run = agent.Run(chat.id)
        try:
            claude_engine.turn(run, chat, chat.meta(), {"id": "claude", "kind": "claude-code", "model": "opus"}, lambda *a: None)
        except Started:
            pass
        self.assertIsNotNone(Grab.cmd)
        note = Grab.cmd[Grab.cmd.index("--append-system-prompt") + 1]
        self.assertIn(RULE, note)
        self.assertIn(agent.MEDIA_RULE, note)

    def test_codex(self):
        chat = new_chat("chatgpt")
        run = agent.Run(chat.id)
        try:
            codex_engine.turn(run, chat, chat.meta(), {"id": "chatgpt", "kind": "codex"}, lambda *a: None)
        except Started:
            pass
        self.assertIsNotNone(Grab.cmd)
        sent = Grab.stdin.decode("utf-8")
        self.assertIn(RULE, sent)
        self.assertIn(agent.MEDIA_RULE, sent)

    def test_neon_first_prompt_once(self):
        chat = new_chat("neon")
        chat.update_meta(agy_conversation="conv-vis-1")
        ep = {"id": "neon", "name": "Gemini 4 Neon", "kind": "agy"}
        first = agy_engine._first_text(chat, ep, lambda *a: None, "plot my weekly calories")
        self.assertIn(RULE, first)
        self.assertIn(agent.MEDIA_RULE, first)
        self.assertTrue(first.endswith("plot my weekly calories"))
        again = agy_engine._first_text(store.Chat(chat.id), ep, lambda *a: None, "next turn")
        self.assertEqual(again, "next turn")

    def test_bundled_charts_skill_discovered(self):
        sk = skills.get("charts")
        self.assertIsNotNone(sk, "bundled charts skill should be discovered automatically")
        self.assertIn("chart", sk["description"].lower())
        self.assertTrue((Path(sk["dir"]) / "SKILL.md").is_file())
        self.assertIn("- charts:", skills.prompt_block())


if __name__ == "__main__":
    unittest.main(verbosity=2)
