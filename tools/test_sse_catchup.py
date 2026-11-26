#!/usr/bin/env python3
"""Regression tests for live SSE catch-up while busy, resume reconnect, /api/busy tails, and ?after= incremental fetch.

Uses an isolated temp KCH_HOME on an ephemeral port: never touches real chats.

    python -X utf8 tools/test_sse_catchup.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="kch-sse-r43-"))
os.environ["KCH_HOME"] = str(TMP)
os.environ["KCH_PROFILE"] = str(TMP / "profile.json")
os.environ["KCH_TEST"] = "1"

(TMP / "profile.json").write_text(json.dumps({
    "endpoints": [
        {"id": "neon", "name": "Gemini 4 Neon", "kind": "agy", "model": "gemini-4-neon-high"},
    ],
}), encoding="utf-8")

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import testhome  # noqa: F401,E402
from harness import agent, config, server, store  # noqa: E402

for d in (config.CHATS, config.LOGS, config.WORKSPACE):
    d.mkdir(parents=True, exist_ok=True)
config.load()

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name, ("  " + str(detail)[:260]) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def get_json(port, path):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}", "X-KC-App": "1"},
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def launch_browser(p):
    for ch in ("chrome", "msedge", None):
        try:
            return p.chromium.launch(channel=ch, headless=True) if ch else p.chromium.launch(headless=True)
        except Exception:
            continue
    raise RuntimeError("Could not launch Chromium/Chrome/Edge via Playwright")


def main():
    ch = store.create(str(config.WORKSPACE), "neon", title="Busy Neon Catchup Test")
    m1 = ch.append({"role": "user", "content": "Run long GPU benchmark"})
    m2 = ch.append({"role": "assistant", "endpoint": "neon", "content": "Started background benchmark."})

    # ---- 1. Store tail_info() and since() unit checks ----
    t0 = ch.tail_info()
    check("tail_info reports count and last_id", t0 == {"total": 2, "last_id": m2["id"]}, t0)
    tot, msgs, trunc = ch.since(m2["id"])
    check("since(last_id) returns empty list when up to date", tot == 2 and msgs == [] and not trunc, (tot, msgs, trunc))

    m3 = ch.append({"role": "assistant", "endpoint": "neon", "content": "Benchmark checkpoint 1 ready."})
    tot, msgs, trunc = ch.since(m2["id"])
    check("since(prev_id) returns only missed messages",
          tot == 3 and len(msgs) == 1 and msgs[0]["id"] == m3["id"] and not trunc, (tot, msgs, trunc))
    tot2, msgs2, trunc2 = ch.since("nonexistent-id")
    check("since(unknown_id) signals truncated=True", tot2 == 3 and msgs2 == [] and trunc2 is True, (tot2, msgs2, trunc2))

    # Simulate a continuously busy Neon ACP chat (running detached background work)
    fake_run = agent.Run(ch.id)
    fake_run.thread = threading.Thread(target=lambda: time.sleep(60), daemon=True)
    fake_run.thread.start()
    with agent.runs_lock:
        agent.runs[ch.id] = fake_run

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    server.ALLOWED_HOSTS.add(f"127.0.0.1:{port}")
    server.ALLOWED_ORIGINS.add(f"http://127.0.0.1:{port}")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        # ---- 2. HTTP /api/busy tails and /api/chats/<id>?after= checks ----
        busy_r = get_json(port, f"/api/busy?chat={ch.id}")
        check("/api/busy includes busy chat and its tail",
              ch.id in (busy_r.get("chats") or [])
              and (busy_r.get("tails") or {}).get(ch.id) == {"total": 3, "last_id": m3["id"]},
              busy_r)

        inc_r = get_json(port, f"/api/chats/{ch.id}?after={m2['id']}")
        check("/api/chats/<id>?after= returns missed message with busy=True",
              inc_r.get("busy") is True and not inc_r.get("truncated")
              and len(inc_r.get("messages") or []) == 1 and inc_r["messages"][0]["id"] == m3["id"],
              inc_r)

        # ---- 3. Browser Playwright tests (phone 390x844 viewport) ----
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sync_playwright = None

        if sync_playwright:
            with sync_playwright() as p:
                browser = launch_browser(p)
                try:
                    ctx = browser.new_context(viewport={"width": 390, "height": 844})
                    page = ctx.new_page()
                    page_errors = []
                    page.on("pageerror", lambda err: page_errors.append(str(err)))
                    page.goto(base_url, wait_until="domcontentloaded")
                    page.wait_for_function("() => window.KCH && typeof window.KCH.openChat === 'function'", timeout=10000)
                    page.evaluate("(cid) => window.KCH.openChat(cid)", ch.id)
                    page.wait_for_function(
                        "() => document.getElementById('msgs').textContent.includes('Benchmark checkpoint 1 ready.')",
                        timeout=10000,
                    )

                    # Scenario A: Chat stays busy:true; SSE misses a message written directly to messages.jsonl.
                    # Calling syncBusy(false) (the periodic timer) MUST detect tail change and append the reply!
                    m4 = ch.append({"role": "assistant", "endpoint": "neon", "content": "Missed while busy: 21:42 update."})
                    res_a = page.evaluate("""async () => {
                        await window.KCH.syncBusy(false);
                        const txt = document.getElementById('msgs').textContent;
                        return {
                            hasM4: txt.includes('Missed while busy: 21:42 update.'),
                            stillBusy: window.KCH.S.busy.has(window.KCH.S.chat),
                            countM4: (txt.match(/Missed while busy: 21:42 update\\./g) || []).length,
                        };
                    }""")
                    check("syncBusy(false) catches up missed message while chat stays busy:true",
                          res_a["hasM4"] and res_a["stillBusy"] and res_a["countM4"] == 1,
                          res_a)

                    # Scenario B: App resumes from background (focus/pageshow/onAppResume) after another missed reply
                    m5 = ch.append({"role": "assistant", "endpoint": "neon", "content": "Missed while phone locked: 22:29 update."})
                    res_b = page.evaluate("""async () => {
                        window.KCH.onAppResume('visible');
                        const t0 = Date.now();
                        while (Date.now() - t0 < 3000) {
                            if (document.getElementById('msgs').textContent.includes('Missed while phone locked: 22:29 update.')) break;
                            await new Promise((r) => setTimeout(r, 50));
                        }
                        const txt = document.getElementById('msgs').textContent;
                        return {
                            hasM5: txt.includes('Missed while phone locked: 22:29 update.'),
                            countM5: (txt.match(/Missed while phone locked: 22:29 update\\./g) || []).length,
                            stillBusy: window.KCH.S.busy.has(window.KCH.S.chat),
                        };
                    }""")
                    check("onAppResume catches up missed reply on return from phone lock",
                          res_b["hasM5"] and res_b["countM5"] == 1 and res_b["stillBusy"],
                          res_b)

                    # Scenario C: Duplicate SSE delivery after catchUpChat does NOT duplicate DOM text,
                    # and active unrelated streaming live step is preserved!
                    m6 = ch.append({"role": "assistant", "endpoint": "neon", "content": "Deduped update 22:31."})
                    res_c = page.evaluate("""async (m6) => {
                        // Simulate active unrelated live step still streaming
                        const L = window.KCH.S.live['live-test'] = {
                            turn: document.querySelector('#msgs .turn:last-of-type'),
                            text: 'Currently streaming another paragraph...',
                            reasoning: '', think: null, prose: null, drafts: {}, t: 0,
                        };
                        await window.KCH.catchUpChat(window.KCH.S.chat);
                        // Also run syncBusy again right after
                        await window.KCH.syncBusy(true);
                        const txt = document.getElementById('msgs').textContent;
                        const liveStillThere = Boolean(window.KCH.S.live['live-test']);
                        delete window.KCH.S.live['live-test'];
                        return {
                            countM6: (txt.match(/Deduped update 22:31\\./g) || []).length,
                            liveStillThere,
                        };
                    }""", m6)
                    check("catchUpChat deduplicates messages and preserves active unrelated live stream",
                          res_c["countM6"] == 1 and res_c["liveStillThere"],
                          res_c)
                    check("zero page JS errors during catch-up and resume", not page_errors, page_errors)
                    ctx.close()
                finally:
                    browser.close()
        else:
            print("skip Playwright browser section (playwright not installed in this interpreter)")
    finally:
        with agent.runs_lock:
            agent.runs.pop(ch.id, None)
        httpd.shutdown()
        httpd.server_close()

    print("\nSummary: %d failed" % len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
