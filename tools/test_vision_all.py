"""Every model reads images natively; no vision helper or cross-model image description exists anywhere.

    python -X utf8 tools/test_vision_all.py
"""
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import testhome  # noqa: E402,F401  (a temp KCH_HOME: never the real chats)
from harness import agent, agy_acp, claude_engine, computer, config, providers, store, tools, vision  # noqa: E402

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class NativeVisionForAll(unittest.TestCase):
    def test_vision_helper_pathway_is_completely_erased(self):
        for attr in ("describe", "helpers", "helper_name", "DESCRIBE", "_api", "_claude", "_codex"):
            self.assertFalse(hasattr(vision, attr), f"vision.{attr} must be removed")
        for attr in ("_describe", "_describe_fallback", "LOOK_FOR"):
            self.assertFalse(hasattr(computer, attr), f"computer.{attr} must be removed")
        self.assertNotIn("vision_helper", config.DEFAULTS)

    def test_every_connection_sees_natively(self):
        for ep in (
            {"id": "local", "kind": "openai"},
            {"id": "legacy-off", "kind": "openai", "vision": False},
            {"id": "anth", "kind": "anthropic"},
            {"id": "deepseek", "kind": "opencode-go"},
            {"id": "gemini", "kind": "agy"},
            {"id": "claude", "kind": "claude-code"},
            {"id": "chatgpt", "kind": "codex"},
        ):
            self.assertTrue(vision.sees(ep), ep)
        self.assertFalse(vision.sees({}))

    def test_native_image_pathways_for_every_engine(self):
        d = Path(tempfile.mkdtemp(prefix="kch-vis-all-"))
        p = d / "shot.png"
        p.write_bytes(PNG_1X1)

        # 1. View & Read tools always return the native image, never "cannot see images"
        class Ctx:
            ep = {"id": "legacy-off", "kind": "openai", "vision": False}
            cwd = d
            cancel = None
        with mock.patch.object(tools, "resolve", return_value=p):
            vres = vision.view(Ctx(), {"file_path": str(p)})
            self.assertEqual(vres["image"], p)
            self.assertNotIn("cannot see", vres["text"].lower())
            rres = tools.t_read(Ctx(), {"file_path": str(p)})
            self.assertEqual(rres["image"], p)
            self.assertNotIn("cannot see", rres["text"].lower())

        # 2. OpenAI-compatible API messages include image_url parts (even for legacy vision: False endpoints)
        ch = store.create(str(d), "legacy-off", title="Vision test")
        um = agent.user_message(ch, {"endpoint": "legacy-off"}, "Inspect this screenshot", [str(p)])
        self.assertNotIn("described_by", um)
        self.assertNotIn("cannot see images", um.get("model_content", ""))
        ch.append(um)
        oai_msgs = agent.api_messages(ch, ch.meta(), {"id": "legacy-off", "kind": "openai", "vision": False})
        user_content = oai_msgs[-1]["content"]
        self.assertIsInstance(user_content, list)
        self.assertTrue(any(part.get("type") == "image_url" for part in user_content), user_content)

        # 3. Native Anthropic Messages API converts image_url to native base64 image blocks
        anth_body = providers.build_anthropic_body({"id": "anth", "kind": "anthropic", "model": "claude-sonnet-4-5"}, oai_msgs, None, "fast")
        anth_parts = anth_body["messages"][-1]["content"]
        img_blocks = [b for b in anth_parts if b.get("type") == "image"]
        self.assertEqual(len(img_blocks), 1)
        self.assertEqual(img_blocks[0]["source"]["type"], "base64")
        self.assertEqual(img_blocks[0]["source"]["media_type"], "image/png")

        # 4. Claude Code CLI builds native base64 image blocks
        cc_blocks = claude_engine._images({"files": [{"kind": "image", "path": str(p)}]})
        self.assertEqual(len(cc_blocks), 1)
        self.assertEqual(cc_blocks[0]["source"]["type"], "base64")

        # 5. Antigravity / Gemini CLI builds native ACP image blocks AND read_file path note
        acp_blocks = agy_acp._acp_images([{"kind": "image", "path": str(p)}])
        self.assertEqual(len(acp_blocks), 1)
        self.assertEqual(acp_blocks[0]["type"], "image")
        self.assertEqual(acp_blocks[0]["mimeType"], "image/png")
        with mock.patch.object(agent.config, "endpoint", return_value={"id": "gemini", "kind": "agy"}):
            note, seen_by = agent._images_note(ch, {"endpoint": "gemini"}, "Check", [p])
        self.assertIsNone(seen_by)
        self.assertIn("read_file", note)
        self.assertNotIn("cannot see images", note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
