#!/usr/bin/env python3
"""Regression and speed tests for Chats app responsiveness fixes and slow-timing logging.

Covers:
1. store.Chat.append() never parses an existing multi-MB messages.jsonl under self.lock.
2. providers.all_statuses() returns immediately (stale-while-revalidate) even with closed local ports.
3. subagents.build_parent_context() uses fast head+tail reads on multi-MB parent chats.
4. chat_import.subagent_window() pages large imported subagent transcripts cleanly.
5. server.py POST /api/perf and slow-request logging write concise entries to LOGS/slow.log.
6. Playwright phone E2E: instant Model sheet, optimistic Notify flip, deferred hidden sidebar repaints, client perf log.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import testhome  # noqa: E402,F401  (temp KCH_HOME: never touch real chats)


class PerfSpeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kch-perf-test-"))
        self.port = 8796

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_py(self, code):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KCH_", "CLAUDE", "ANTHROPIC"))}
        env.update(KCH_HOME=str(self.tmp), KCH_PORT=str(self.port), PYTHONUTF8="1")
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            self.fail(f"Subprocess failed (exit {r.returncode}):\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
        return r.stdout.strip()

    def test_01_store_append_does_not_parse_large_chat(self):
        out = self._run_py(r"""
import json, time
from harness import config, store
config.load()
ch = store.create(str(config.WORKSPACE), "claude", title="Big chat")
big_payload = "x" * 10000
with open(ch.messages_path, "w", encoding="utf-8") as f:
    for i in range(1200):
        f.write(json.dumps({"id": f"m{i}", "ts": 1700000000 + i, "role": "assistant", "content": big_payload}) + "\n")
size_mb = ch.messages_path.stat().st_size / 1e6
fresh = store.Chat(ch.id)
t0 = time.perf_counter()
fresh.append({"role": "user", "content": "quick tap"})
dt_ms = (time.perf_counter() - t0) * 1000
assert fresh._messages is None, "append must not load full messages list into memory"
assert dt_ms < 250, f"append took {dt_ms:.1f} ms on {size_mb:.1f} MB chat"
# Window still reads the newly appended message cleanly
start, total, msgs = store.Chat(ch.id).window(10)
assert total == 1201, f"expected 1201, got {total}"
assert msgs[-1]["content"] == "quick tap"
print(f"OK size={size_mb:.1f}MB append={dt_ms:.1f}ms total={total}")
""")
        self.assertIn("OK", out)

    def test_02_providers_all_statuses_non_blocking(self):
        out = self._run_py(r"""
import time
from harness import config, providers
config.load()
# Add an unreachable local OpenAI endpoint that would stall Windows connect for ~1-2s if probed synchronously
config.update(lambda c: c["endpoints"].append({
    "id": "dead-local", "name": "Dead Local", "kind": "openai", "base": "http://127.0.0.1:19997/v1", "model": "x"
}))
config.load()
t0 = time.perf_counter()
st1 = providers.all_statuses(force=False)
dt1 = (time.perf_counter() - t0) * 1000
assert "dead-local" in st1
assert dt1 < 300, f"cold all_statuses blocked for {dt1:.1f} ms"
t1 = time.perf_counter()
st2 = providers.all_statuses(force=True)
dt2 = (time.perf_counter() - t1) * 1000
assert "dead-local" in st2
assert dt2 < 200, f"force all_statuses blocked for {dt2:.1f} ms"
print(f"OK cold={dt1:.1f}ms force={dt2:.1f}ms")
""")
        self.assertIn("OK", out)

    def test_03_subagent_build_parent_context_fast_on_large_chat(self):
        out = self._run_py(r"""
import json, time
from harness import config, store, subagents
config.load()
parent = store.create(str(config.WORKSPACE), "claude", title="Huge parent")
child = store.create(str(config.WORKSPACE), "claude", title="Subagent")
big_payload = "y" * 10000
with open(parent.messages_path, "w", encoding="utf-8") as f:
    f.write(json.dumps({"id": "m0", "ts": 1700000000, "role": "user", "content": "Original goal: make everything fast"}) + "\n")
    for i in range(1, 1200):
        f.write(json.dumps({"id": f"m{i}", "ts": 1700000000 + i, "role": "assistant", "content": f"Turn {i}: " + big_payload}) + "\n")
    f.write(json.dumps({"id": "m_last", "ts": 1700009999, "role": "user", "content": "Latest instruction: verify speed"}) + "\n")
size_mb = parent.messages_path.stat().st_size / 1e6
ep = config.endpoint("claude")
t0 = time.perf_counter()
block, ctx_k, total_m, shown_m, trimmed, hist_path = subagents.build_parent_context(parent.id, child, ep)
dt_ms = (time.perf_counter() - t0) * 1000
assert dt_ms < 500, f"build_parent_context took {dt_ms:.1f} ms on {size_mb:.1f} MB chat"
assert "Original goal: make everything fast" in block
assert "Latest instruction: verify speed" in block
assert hist_path.is_file()
assert trimmed is True
# ParentContext 'notes' and 'latest' also stay fast on a 12 MB parent chat
t_n = time.perf_counter()
notes_out = subagents.search_parent(parent.id, action="notes")
dt_n = (time.perf_counter() - t_n) * 1000
assert dt_n < 150 and "Huge parent" in notes_out, f"notes took {dt_n:.1f} ms"
t_l = time.perf_counter()
lat_out = subagents.search_parent(parent.id, action="latest", limit=5)
dt_l = (time.perf_counter() - t_l) * 1000
assert dt_l < 400 and "Latest instruction: verify speed" in lat_out, f"latest took {dt_l:.1f} ms"
print(f"OK size={size_mb:.1f}MB ctx_build={dt_ms:.1f}ms notes={dt_n:.1f}ms latest={dt_l:.1f}ms")
""")
        self.assertIn("OK", out)

    def test_04_imported_subagent_window_paging(self):
        out = self._run_py(r"""
import json
from harness import chat_import, config, store
config.load()
ch = store.create(str(config.WORKSPACE), "claude", title="Imported parent")
msgs = [{"role": "assistant", "content": f"Step {i}", "parts": [{"kind": "text", "text": f"Step {i}"}]} for i in range(120)]
with open(ch.dir / "subagents.jsonl", "w", encoding="utf-8") as f:
    f.write(json.dumps({"id": "sub-run-1", "messages": msgs}) + "\n")
w1 = chat_import.subagent_window(ch.id, "sub-run-1", limit=40)
assert w1["total"] == 120 and w1["start"] == 80 and len(w1["messages"]) == 40
assert w1["messages"][-1]["content"] == "Step 119"
w2 = chat_import.subagent_window(ch.id, "sub-run-1", limit=40, before=w1["start"])
assert w2["start"] == 40 and len(w2["messages"]) == 40
assert w2["messages"][-1]["content"] == "Step 79"
# Backwards-compatible subagent_messages still returns full list
full = chat_import.subagent_messages(ch.id, "sub-run-1")
assert len(full) == 120
print("OK paging verified")
""")
        self.assertIn("OK", out)

    def test_05_server_perf_and_playwright_phone_ui(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KCH_", "CLAUDE", "ANTHROPIC"))}
        env.update(KCH_HOME=str(self.tmp), KCH_PORT=str(self.port), PYTHONUTF8="1")
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "run.py"], cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def api(path, body=None):
            url = f"http://127.0.0.1:{self.port}{path}"
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(
                url, data=data,
                headers={"X-KC-App": "1", "Content-Type": "application/json"},
                method="POST" if body is not None else "GET",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))

        try:
            for _ in range(80):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/capabilities", timeout=2) as r:
                        if r.status == 200:
                            break
                except Exception:
                    time.sleep(0.25)
            else:
                self.fail("Isolated server did not start")

            # Verify /api/endpoints/statuses is instant (< 200 ms)
            t0 = time.perf_counter()
            st = api("/api/endpoints/statuses")
            dt_st = (time.perf_counter() - t0) * 1000
            self.assertIn("statuses", st)
            self.assertLess(dt_st, 250, f"/api/endpoints/statuses took {dt_st:.1f} ms")

            # Verify POST /api/perf logs to LOGS/slow.log
            res = api("/api/perf", {"events": [{"kind": "click", "what": "Notify: On", "ms": 340, "detail": "test"}]})
            self.assertTrue(res.get("ok"))
            slow_log = self.tmp / "logs" / "slow.log"
            self.assertTrue(slow_log.is_file())
            txt = slow_log.read_text(encoding="utf-8")
            self.assertIn("client click Notify: On ms=340 test", txt)

            # Create parent + subagent and verify Playwright phone responsiveness
            parent = api("/api/chats", {"endpoint": "claude", "title": "Speed Parent"})
            pid = parent["id"]
            sub = api(f"/api/chats/{pid}/subagents", {"name": "Quick Sub", "model": "claude", "notify_user": True})
            sid = sub["id"]

            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome")
                ctx = browser.new_context(viewport={"width": 412, "height": 915}, is_mobile=True, has_touch=True, color_scheme="dark")
                page = ctx.new_page()
                errs = []
                page.on("pageerror", lambda e: errs.append(str(e)))
                page.goto(f"http://127.0.0.1:{self.port}/", wait_until="domcontentloaded")
                page.wait_for_function("() => window.KCH && window.KCH.S && window.KCH.S.cfg && window.KCH.openChat", timeout=10000)
                page.wait_for_timeout(300)
                self.assertEqual(errs, [], f"Page errors on load: {errs}")
                page.evaluate(f"() => window.KCH.openChat({json.dumps(sid)})")
                page.wait_for_selector("#subBackBar:not([hidden])", timeout=10000)
                self.assertEqual(errs, [], f"Page errors after openChat: {errs}")
                # Subagent back bar shows Back to main and Report to main (no phone-only Notify toggle)
                self.assertTrue(page.locator("#subBackBar [data-back]").is_visible())
                self.assertTrue(page.locator("#subBackBar [data-report]").is_visible())

                # Model sheet opens immediately with cached statuses (not blanked to 'checking')
                mod_res = page.evaluate("""() => {
                    window.KCH.S.statuses = { claude: { state: 'ready', detail: 'Claude plan' } };
                    const t0 = performance.now();
                    document.getElementById('modelBtn').click();
                    const open = document.querySelector('.m-sheet.open');
                    const rows = open ? open.querySelectorAll('.m-sh-row').length : 0;
                    const pills = open ? [...open.querySelectorAll('.st-pill')].map((x) => x.textContent.trim()) : [];
                    return { ms: performance.now() - t0, rows, pills };
                }""")
                self.assertGreater(mod_res["rows"], 0, "Model sheet must render rows synchronously on click")
                self.assertLess(mod_res["ms"], 80, f"Model sheet JS open took {mod_res['ms']:.1f} ms")
                self.assertNotIn("checking", mod_res["pills"], f"Model sheet should use cached statuses, got {mod_res['pills']}")
                page.click(".m-sheet .m-close")

                # Verify hidden phone sidebar skips redraws during SSE bursts and flushes on open
                paint_count = page.evaluate("""() => {
                    let n = 0;
                    const orig = window.KCL.paint;
                    window.KCL.paint = function() { n++; return orig.apply(this, arguments); };
                    for (let i = 0; i < 15; i++) {
                        window.KCH.S.busy.add('fake-' + i);
                        window.KCH.paintList?.();
                    }
                    const hiddenCount = n;
                    window.KCH.flushList?.();
                    const flushedCount = n;
                    window.KCL.paint = orig;
                    return { hiddenCount, flushedCount };
                }""")
                self.assertEqual(paint_count["hiddenCount"], 0, f"Hidden phone sidebar should not repaint on bursts: {paint_count}")
                self.assertEqual(paint_count["flushedCount"], 1, f"flushList should repaint once on drawer open: {paint_count}")

                # Verify stuck steering box glitch fixes:
                # (a) Clicking X on an already-consumed queued item dismisses the box even when /unqueue returns ok=false
                page.evaluate("""() => {
                    window.KCH.S.queue = [{ id: 'q-stale-1', text: 'Already consumed by model', when: 'step' }];
                    window.KCH.paintQueue();
                }""")
                page.wait_for_selector("#queued:not([hidden]) [data-x]", timeout=3000)
                page.click("#queued [data-x]")
                page.wait_for_function("() => document.getElementById('queued').hidden === true", timeout=3000)
                self.assertEqual(page.evaluate("() => (window.KCH.S.queue || []).length"), 0)

                # (b) A late /send response for an already-dropped queued_id never re-inserts a zombie box
                readded = page.evaluate("""() => {
                    const orig = window.fetch;
                    window.fetch = (url, init) => {
                        if (String(url).endsWith('/send')) {
                            return Promise.resolve(new Response(JSON.stringify({ queued: true, id: 'q-stale-1', when: 'step' }), {
                                status: 200, headers: { 'Content-Type': 'application/json' }
                            }));
                        }
                        return orig.call(this, url, init);
                    };
                    document.getElementById('input').value = 'Already dropped';
                    document.getElementById('sendBtn').click();
                    return new Promise((res) => setTimeout(() => {
                        window.fetch = orig;
                        res({ hidden: document.getElementById('queued').hidden, len: (window.KCH.S.queue || []).length });
                    }, 120));
                }""")
                self.assertTrue(readded["hidden"], f"Already-dropped queued_id must not re-open box: {readded}")
                self.assertEqual(readded["len"], 0)

                ctx.close()
                browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
