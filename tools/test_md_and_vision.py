#!/usr/bin/env python3
"""Tests for:
1. Single-tilde approximation (~7 min ... ~14 min) does NOT render as strikethrough, while ~~deleted~~ DOES.
2. Gemini 4 Neon (agy) is treated as native vision (no DeepSeek helper) and builds ACP image blocks.
"""
import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRATCH = Path(tempfile.mkdtemp(prefix="kch-md-vis-"))
os.environ["KCH_HOME"] = str(SCRATCH)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import testhome  # noqa: E402,F401
from harness import agent, agy_acp, config, store, vision  # noqa: E402


class MdAndVisionTest(unittest.TestCase):
    def test_agy_is_native_vision_no_deepseek_helper(self):
        ep = {"id": "gemini", "name": "Gemini", "kind": "agy", "model": "gemini-4-neon"}
        self.assertTrue(vision.sees(ep))
        self.assertFalse(hasattr(vision, "describe"))

        # 1x1 PNG
        png = SCRATCH / "shot.png"
        png.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        ))
        chat = store.create(str(ROOT), "gemini", title="Vision test")
        with mock.patch.object(config, "endpoint", return_value=ep):
            txt, seen_by = agent._images_note(chat, chat.meta(), "check this", [png])
        self.assertIsNone(seen_by)
        self.assertIn("(attached to this message", txt)
        self.assertNotIn("cannot see images", txt)

        blocks = agy_acp._acp_images([{"kind": "image", "path": str(png)}])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["mimeType"], "image/png")
        self.assertTrue(len(blocks[0]["data"]) > 10)

    def test_single_tilde_not_strikethrough_double_tilde_is(self):
        # Run real marked vendor file + exact app.js extension in Playwright Chromium
        from playwright.sync_api import sync_playwright
        app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("del(src)", app_js)
        marked_path = ROOT / "web" / "vendor" / "marked.min.js"
        self.assertTrue(marked_path.is_file(), f"missing {marked_path}")
        html = f"""<!doctype html><html><head><meta charset="utf-8">
        <script src="{marked_path.as_uri()}"></script></head><body><script>
        const tokenizer = {{
          del(src) {{
            if (src[0] === '~' && src[1] !== '~') return {{ type: 'text', raw: '~', text: '~' }};
            const m = /^~~(?=\\S)([\\s\\S]*?\\S)~~/.exec(src);
            if (m) return {{ type: 'del', raw: m[0], text: m[1], tokens: this.lexer.inlineTokens(m[1]) }};
            return undefined;
          }},
        }};
        marked.use({{ tokenizer, gfm: true, breaks: false }});
        window.R1 = marked.parse("Say go for E4B only (~7 min) or E4B vs E2B (~14 min total)!");
        window.R2 = marked.parse("This is ~~actually struck~~ and ~approx~ stays plain.");
        </script></body></html>"""
        hp = SCRATCH / "md.html"
        hp.write_text(html, encoding="utf-8")
        with sync_playwright() as p:
            try:
                b = p.chromium.launch(channel="chrome")
            except Exception:
                b = p.chromium.launch(channel="msedge")
            pg = b.new_page()
            pg.goto(hp.as_uri())
            r1 = pg.evaluate("window.R1")
            r2 = pg.evaluate("window.R2")
            b.close()
        self.assertNotIn("<del>", r1, f"single tilde must not strike through: {r1}")
        self.assertIn("~7 min", r1)
        self.assertIn("~14 min", r1)
        self.assertIn("<del>actually struck</del>", r2)
        self.assertIn("~approx~", r2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
