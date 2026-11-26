"""Comprehensive unit + Playwright E2E tests for chat forking (desktop, narrow window, all models/engines).

Runs completely isolated against a temporary DATA directory and a dedicated port (8819).
Never touches live Flock data or running servers.
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Configure isolated DATA + workspace BEFORE importing harness modules
TMP = Path(tempfile.mkdtemp(prefix="kch-fork-test-"))
DATA = TMP / "data"
WORKSPACE = TMP / "workspace"
DATA.mkdir(parents=True, exist_ok=True)
WORKSPACE.mkdir(parents=True, exist_ok=True)

os.environ["KCH_HOME"] = str(DATA)
os.environ["KCH_DATA"] = str(DATA)
os.environ["KCH_PROFILE"] = str(TMP / "profile.json")
os.environ["KCH_WORKSPACE"] = str(WORKSPACE)
os.environ["KCH_PORT"] = "8819"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import agent, chat_import, config, fork, sections, server, store  # noqa: E402

assert Path(config.DATA).resolve() == DATA.resolve(), f"Refusing to run against non-temp DATA: {config.DATA}"
assert Path(config.PROFILE_PATH).resolve() == (TMP / "profile.json").resolve(), f"Refusing to read live profile: {config.PROFILE_PATH}"


def test_backend_all_engines():
    print("[1/5] Testing backend fork: OpenAI/DeepSeek/Gemma, mid-chat, tools, notes, attachments, isolation...")
    config.update(lambda c: c.update({
        "endpoint": "deepseek",
        "endpoints": [
            {"id": "deepseek", "name": "DeepSeek", "kind": "openai", "base": "http://127.0.0.1:1/v1", "model": "deepseek-chat"},
            {"id": "claude", "name": "Claude Opus", "kind": "claude-code", "model": "opus"},
            {"id": "chatgpt", "name": "Codex", "kind": "codex", "model": "gpt-5"},
            {"id": "neon", "name": "Gemini 4 Neon", "kind": "agy", "model": "neon"},
        ],
    }))
    store.add_project(str(WORKSPACE), "testproj")
    sections._ensure()  # initialize sections.json before creating user chats (matches real app startup)

    # Create parent chat with 2 turns, tool call, notes, attachment, and section
    c = store.create(str(WORKSPACE), "deepseek", title="API refactor", reasoning="medium")
    c.set_notes("Keep backward compatibility for /v1.")
    att_dir = c.dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    att_file = att_dir / "diagram.txt"
    att_file.write_text("arch diagram", encoding="utf-8")

    m1 = c.append({"id": "u1", "role": "user", "content": "Inspect server.py", "files": [{"path": str(att_file), "kind": "file"}]})
    m2 = c.append({
        "id": "a1",
        "role": "assistant",
        "endpoint": "deepseek",
        "content": "Checking server.py now.",
        "tool_calls": [{"id": "tc1", "name": "Read", "arguments": '{"path":"server.py"}'}],
        "stats": {"prompt_new": 100, "out_tokens": 20, "secs": 0.5},
    })
    m3 = c.append({"id": "t1", "role": "tool", "tool_call_id": "tc1", "name": "Read", "content": "def main(): pass", "ok": True})
    m4 = c.append({"id": "a2", "role": "assistant", "endpoint": "deepseek", "content": "Server has a single main() function.", "stats": {"prompt_new": 120, "out_tokens": 15, "secs": 0.4}})
    m5 = c.append({"id": "u2", "role": "user", "content": "Now add caching"})
    m6 = c.append({"id": "a3", "role": "assistant", "endpoint": "deepseek", "content": "Added LRU cache to main().", "stats": {"prompt_new": 150, "out_tokens": 18, "secs": 0.6}})
    sections.assign_new_chat(c.id, "work")

    # 1a. Full fork
    r_full = fork.fork_chat(c.id)
    fc = store.Chat(r_full["id"])
    fmeta = fc.meta()
    assert fmeta["title"] == "API refactor (fork)", fmeta["title"]
    assert fmeta["parent"] == c.id
    assert fmeta["branch"]["kind"] == "fork"
    assert fmeta["branch"]["of"] == "API refactor"
    assert fmeta["branch"]["of_chat"] == c.id
    assert len(fc.messages()) == 6
    assert fc.notes() == "Keep backward compatibility for /v1."
    # Attachment copied & rewritten to fork's own attachments dir
    fork_att = Path(fc.messages()[0]["files"][0]["path"])
    assert fork_att.exists() and fork_att.parent == (fc.dir / "attachments")
    assert fork_att != att_file
    # Section inherited
    assert sections._ensure()["chats"].get(fc.id) == "work"
    # OpenAI api_messages builds cleanly
    api_msgs = agent.api_messages(fc, fmeta, config.endpoint("deepseek"))
    assert any("Server has a single main()" in str(m.get("content")) for m in api_msgs)
    assert any("Added LRU cache" in str(m.get("content")) for m in api_msgs)
    # Isolation: appending to fork does NOT change parent, and vice versa
    fc.append({"role": "user", "content": "Fork-only follow-up"})
    assert len(c.messages()) == 6
    assert len(fc.messages()) == 7

    # Re-forking a fork increments title cleanly
    r_full2 = fork.fork_chat(fc.id)
    assert store.Chat(r_full2["id"]).meta()["title"] == "API refactor (fork 2)"

    # 1b. Mid-chat fork from assistant message a2 (turn 1 end)
    r_mid = fork.fork_chat(c.id, from_msg_id="a2")
    mc = store.Chat(r_mid["id"])
    mids = [m["id"] for m in mc.messages()]
    assert mids == ["u1", "a1", "t1", "a2"], mids
    assert mc.meta()["branch"]["from_msg"] == "a2"

    # 1c. Mid-chat fork from assistant message a1 (has tool_call tc1 -> includes matching tool result t1!)
    r_tool = fork.fork_chat(c.id, from_msg_id="a1")
    tc_chat = store.Chat(r_tool["id"])
    assert [m["id"] for m in tc_chat.messages()] == ["u1", "a1", "t1"]

    # 1d. Mid-chat fork from user message u1
    r_u1 = fork.fork_chat(c.id, from_msg_id="u1")
    assert [m["id"] for m in store.Chat(r_u1["id"]).messages()] == ["u1"]

    # 1e. Dangling tool_call sanitization (OpenAI API safety)
    c_dang = store.create(str(WORKSPACE), "deepseek", title="Dangling")
    c_dang.append({"id": "du1", "role": "user", "content": "Run check"})
    c_dang.append({"id": "da1", "role": "assistant", "content": "Running", "tool_calls": [{"id": "orphan_tc", "name": "Bash", "arguments": "{}"}]})
    r_dang = fork.fork_chat(c_dang.id)
    dc = store.Chat(r_dang["id"])
    assert not dc.messages()[1].get("tool_calls")
    assert "Bash" in dc.messages()[1].get("model_content", "")

    # 1f. Bad message id raises ValueError
    try:
        fork.fork_chat(c.id, from_msg_id="no-such-msg")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass

    print("[2/5] Testing CLI engine session isolation & handover_prefix (Claude Code, Codex, Agy/Neon)...")
    c_cli = store.create(str(WORKSPACE), "claude", title="CLI session chat")
    c_cli.update_meta(
        claude_session="sess-secret-123",
        claude_model="opus",
        claude_used={"opus": time.time()},
        codex_thread="thread-abc-456",
        agy_conversation="conv-xyz-789",
        engine_cursor={"claude": "a1", "codex": "a1", "agy": "a1"},
        source="Claude Code",
        source_id="src-999",
        sub_of="parent-sub",
    )
    c_cli.append({"id": "cu1", "role": "user", "content": "We chose PostgreSQL for billing."})
    c_cli.append({"id": "ca1", "role": "assistant", "endpoint": "claude", "content": "Understood, PostgreSQL for billing."})
    c_cli.append({"id": "cu2", "role": "user", "content": "Also add Redis cache."})
    c_cli.append({"id": "ca2", "role": "assistant", "endpoint": "claude", "content": "Added Redis cache plan."})

    # Fork mid-chat at ca1 (before Redis was mentioned!)
    for ep_id, eng_key in [("claude", "claude"), ("chatgpt", "codex"), ("neon", "agy")]:
        rf = fork.fork_chat(c_cli.id, from_msg_id="ca1", endpoint=ep_id)
        fchat = store.Chat(rf["id"])
        fm = fchat.meta()
        # Zero mutable session/import/subagent keys leaked
        for forbidden in ("claude_session", "claude_model", "claude_used", "codex_thread", "agy_conversation",
                          "engine_cursor", "source", "source_id", "sub_of"):
            assert forbidden not in fm, f"{forbidden} leaked into fork meta!"
        # Simulate user sending the first new message in the fork
        fchat.append({"id": "new_u", "role": "user", "content": "Use SQLite instead?"})
        prefix = chat_import.handover_prefix(fchat, eng_key, False, ep_id, None)
        assert "<earlier_in_this_chat>" in prefix, f"missing handover for {eng_key}: {prefix!r}"
        assert "PostgreSQL for billing" in prefix, f"missing prior context for {eng_key}"
        assert "Redis cache" not in prefix, f"post-fork message leaked into {eng_key} handover!"
        assert "Use SQLite instead?" not in prefix, f"new user turn duplicated in {eng_key} handover!"

    return c.id


def test_playwright_e2e(regular_cid):
    from playwright.sync_api import sync_playwright

    # Start isolated server on port 8819
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 8819), server.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base_url = "http://127.0.0.1:8819"
    errors = []

    try:
        with sync_playwright() as p:
            browser = None
            for kw in ({"channel": "chrome"}, {"channel": "msedge"}, {}):
                try:
                    browser = p.chromium.launch(**kw)
                    break
                except Exception:
                    continue
            if browser is None:
                raise RuntimeError("No Chromium/Chrome/Edge browser available for Playwright")

            # ---------------- DESKTOP E2E (1280x900) ----------------
            print("[4/5] Playwright Desktop E2E (header Fork, no per-message Fork clutter, sidebar menu Fork, Neon Fork, Open parent)...")
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"desktop pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"desktop console.{m.type}: {m.text}") if m.type == "error" else None)

            page.goto(base_url, wait_until="networkidle")
            page.wait_for_function("() => window.KCH && window.KCH.openChat")
            page.evaluate("(p) => window.KCH.setProject(p)", str(WORKSPACE))   # the list shows the test project's chats
            page.evaluate("(id) => window.KCH.openChat(id)", regular_cid)
            page.wait_for_function("(id) => window.KCH.S.chat === id", arg=regular_cid)
            page.wait_for_selector('#forkBtn:visible')
            # Only one Fork button in the header — no per-message/per-turn fork buttons cluttering the thread
            assert page.locator('.msg-fork, .turn-fork').count() == 0

            # 1. Header Fork (full chat)
            page.click('#forkBtn')
            page.wait_for_function("(id) => window.KCH.S.chat && window.KCH.S.chat !== id", arg=regular_cid)
            page.wait_for_selector('.forkbar')
            bar_txt = page.locator('.forkbar').inner_text()
            assert "Forked from API refactor" in bar_txt, bar_txt
            assert page.locator('#msgs .msg.user').count() == 2
            assert page.locator('#msgs .turn').count() == 2

            # 2. Click "Open it" in forkbar -> jumps back to parent
            page.click('.forkbar [data-open]')
            page.wait_for_function("(id) => window.KCH.S.chat === id", arg=regular_cid)
            assert page.locator('.forkbar').count() == 0

            # 3. Sidebar context menu Fork (c was assigned to 'work' section)
            prev_cid = page.evaluate("() => window.KCH.S.chat")
            page.click('#mSecBar [data-sec="work"]')
            page.wait_for_selector(f'.chat-item[data-id="{regular_cid}"]')
            page.locator(f'.chat-item[data-id="{regular_cid}"]').click(button="right")
            page.wait_for_selector('#popover button.item:has-text("Fork")')
            page.click('#popover button.item:has-text("Fork")')
            page.wait_for_function("(id) => window.KCH.S.chat && window.KCH.S.chat !== id", arg=prev_cid)
            page.wait_for_selector('.forkbar')
            assert page.locator('#msgs .msg.user').count() == 2
            ctx.close()

            # ---------------- NARROW WINDOW E2E (412x900) ----------------
            print("[4/4] Playwright Narrow-Window E2E (#mForkBtn, title sheet Fork, no per-message Fork clutter)...")
            mctx = browser.new_context(viewport={"width": 412, "height": 900}, is_mobile=True, has_touch=True)
            mpage = mctx.new_page()
            mpage.on("pageerror", lambda e: errors.append(f"narrow pageerror: {e}"))
            mpage.on("console", lambda m: errors.append(f"narrow console.{m.type}: {m.text}") if m.type == "error" else None)

            mpage.goto(base_url, wait_until="networkidle")
            mpage.wait_for_function("() => window.KCH && window.KCH.openChat")
            mpage.evaluate("(id) => window.KCH.openChat(id)", regular_cid)
            mpage.wait_for_function("(id) => window.KCH.S.chat === id", arg=regular_cid)
            assert mpage.locator('.msg-fork, .turn-fork').count() == 0

            assert mpage.locator('#mForkBtn').is_visible()
            mpage.click('#mForkBtn')
            mpage.wait_for_function("(id) => window.KCH.S.chat && window.KCH.S.chat !== id", arg=regular_cid)
            mpage.wait_for_selector('.forkbar')
            assert "Forked from API refactor" in mpage.locator('.forkbar').inner_text()

            mpage.click('.forkbar [data-open]')
            mpage.wait_for_function("(id) => window.KCH.S.chat === id", arg=regular_cid)
            mpage.click('#chatHead .title')
            mpage.wait_for_selector('.m-sheet.open')
            first_row = mpage.locator('#mSheetBody .m-sh-row').first
            assert first_row.inner_text().strip() == "Fork", repr(first_row.inner_text())
            first_row.click()
            mpage.wait_for_function("(id) => window.KCH.S.chat && window.KCH.S.chat !== id", arg=regular_cid)
            mpage.wait_for_selector('.forkbar')
            assert mpage.locator('#msgs .msg.user').count() == 2
            mctx.close()
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert not errors, f"Browser errors:\n" + "\n".join(errors)


if __name__ == "__main__":
    reg_id = test_backend_all_engines()
    test_playwright_e2e(reg_id)
    print("ALL FORK TESTS PASSED!")
