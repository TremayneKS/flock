"""Tests for Claude usage guardrails:
1. --disallowedTools Agent,Task passed on every Claude CLI launch (main + subagent) + prompt rule.
2. Claude autocompact defaults to 200,000 when unset, honors explicit values, and respects 0 to disable.
3. Live Claude burn-rate monitor: deduplication per api_id, call-rate spike, repeated 5-min cache rewrites,
   overall global spike, cooldown, SSE burn_alert event, and /api/burn + dismiss.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402

_TMP = Path(tempfile.mkdtemp(prefix="kch-burn-test-"))
(_TMP / ".kch-test-home").write_text("1", encoding="utf-8")
os.environ["KCH_TEST"] = "1"
os.environ["KCH_HOME"] = str(_TMP)

_PROFILE = _TMP / "profile.json"
_PROFILE.write_text(json.dumps({
    "endpoint": "claude",
    "endpoints": [
        {"id": "claude", "name": "Claude Opus", "kind": "claude-code", "model": "claude-opus-4-5", "context": 1000000, "autocompact": 200000},
        {"id": "sonnet", "name": "Claude Sonnet", "kind": "claude-code", "model": "claude-sonnet-4-5", "context": 200000},
    ],
    "burn_alert": {
        "enabled": True,
        "cooldown_s": 600,
        "chat_calls_10m": 5,
        "chat_calls_60m": 20,
        "cache_rewrite_min_tokens": 50000,
        "chat_cache_rewrites_30m": 3,
        "chat_written_tokens_30m": 300000,
        "chat_total_tokens_15m": 5000000,
        "global_calls_15m": 8,
        "global_written_tokens_30m": 400000,
    },
}), encoding="utf-8")
os.environ["KCH_PROFILE"] = str(_PROFILE)

from harness import agent, burn_monitor, claude_engine, config, server, store  # noqa: E402


def run_tests():
    config.load()
    passed = 0

    # 1. Autocompact helper: explicit 200k, all Claude Code models default to 200k, explicit 0 disables
    assert claude_engine._autocompact_val({"model": "claude-opus-4-5", "autocompact": 200000}) == "200000"
    assert claude_engine._autocompact_val({"model": "claude-opus-4-5"}) == "200000"
    assert claude_engine._autocompact_val({"model": "claude-sonnet-4-5"}) == "200000"
    assert claude_engine._autocompact_val({}) == "200000"
    assert claude_engine._autocompact_val({"model": "claude-sonnet-4-5", "autocompact": 300000}) == "300000"
    assert claude_engine._autocompact_val({"model": "claude-opus-4-5", "autocompact": 0}) is None
    assert claude_engine._autocompact_val({"model": "claude-sonnet-4-5", "autocompact": 0}) is None
    assert claude_engine.DISALLOWED_TOOLS == "Agent,Task"
    assert "harness_agent" in claude_engine.SUBAGENT_RULE
    print("PASS 1: _autocompact_val defaults all Claude Code models to 200k and honors explicit overrides")
    passed += 1

    # 2. Verify local Claude CLI launch command includes --disallowedTools Agent,Task, --autocompact 200000, and SUBAGENT_RULE
    captured_cmds = []
    orig_popen = claude_engine.subprocess.Popen
    orig_follow = claude_engine._follow
    orig_exe = claude_engine.claude_exe

    class DummyProc:
        pid = 99999
        def poll(self): return 0
        def wait(self): return 0

    def fake_popen(holder, *a, **kw):
        captured_cmds.append(list(holder))
        return DummyProc()

    try:
        claude_engine.claude_exe = lambda: "claude-fake"
        claude_engine.subprocess.Popen = fake_popen
        claude_engine._follow = lambda *a, **kw: True

        for ep_id, is_sub in (("claude", False), ("claude", True), ("sonnet", False)):
            c = store.create(str(_TMP), ep_id)
            if is_sub:
                c.update_meta(sub_of="parent-1", sub_origin="agent", sub_name="Worker")
            c.append({"role": "user", "content": "test prompt"})
            run = agent.Run(c.id)
            claude_engine.turn(run, c, c.meta(), config.endpoint(ep_id), lambda cid, ev: None)
    finally:
        claude_engine.subprocess.Popen = orig_popen
        claude_engine._follow = orig_follow
        claude_engine.claude_exe = orig_exe

    assert len(captured_cmds) == 3, captured_cmds
    for holder in captured_cmds:
        idx = holder.index("--")
        cmd = holder[idx + 1:]
        di = cmd.index("--disallowedTools")
        assert cmd[di + 1] == "Agent,Task", cmd
        ai = cmd.index("--autocompact")
        assert cmd[ai + 1] == "200000", cmd
        pi = cmd.index("--append-system-prompt")
        assert "harness_agent" in cmd[pi + 1], cmd[pi + 1]
    print("PASS 2: main Opus, subagent Opus, and Sonnet Claude CLI launches all enforce --disallowedTools Agent,Task and --autocompact 200000")
    passed += 1

    # 3. Burn monitor: multi-block dedup + call-rate spike + cooldown + SSE event
    burn_monitor.reset()
    sse_q = agent.hub.subscribe()
    try:
        t0 = time.time() - 700.0
        meta = {"title": "Runaway Site Chat", "sub_of": "parent-xyz"}
        for k in range(3):
            res = burn_monitor.record_call(
                "c-spike", meta,
                {"model": "claude-opus-4-5", "prompt_uncached": 500, "prompt_written": 2000, "prompt_cached": 100000, "out_tokens": 100 * (k + 1)},
                api_id="msg_same", now=t0 + k,
            )
            assert res == [], res
        snap = burn_monitor.snapshot(now=t0 + 5)
        assert snap["chats"][0]["calls_10m"] == 1, snap

        fired = []
        for i in range(2, 7):
            fired.extend(burn_monitor.record_call(
                "c-spike", meta,
                {"model": "claude-opus-4-5", "prompt_uncached": 500, "prompt_written": 1000, "prompt_cached": 100000, "out_tokens": 200},
                api_id=f"msg_{i}", now=t0 + 10 + i,
            ))
        assert any(a["reason"] == "call_rate" and a["chat"] == "c-spike" for a in fired), fired

        again = burn_monitor.record_call(
            "c-spike", meta,
            {"model": "claude-opus-4-5", "prompt_uncached": 500, "prompt_written": 1000, "prompt_cached": 100000, "out_tokens": 200},
            api_id="msg_7", now=t0 + 25,
        )
        assert not any(a["reason"] == "call_rate" for a in again), again

        meta2 = {"title": "Cache Thrash Chat"}
        rw_fired = []
        for i in range(3):
            rw_fired.extend(burn_monitor.record_call(
                "c-cache", meta2,
                {"model": "claude-opus-4-5", "prompt_uncached": 200, "prompt_written": 120000, "prompt_cached": 10000, "out_tokens": 300},
                api_id=f"rw_{i}", now=t0 + 40 + i * 310,
            ))
        assert any(a["reason"] == "cache_rewrites" and a["chat"] == "c-cache" for a in rw_fired), rw_fired
        assert any(a["reason"] == "global_burn" for a in rw_fired), rw_fired

        evs = []
        while not sse_q.empty():
            evs.append(sse_q.get_nowait())
        assert any(e.get("type") == "burn_alert" for e in evs), evs
        print("PASS 3: burn_monitor dedups api_id, fires call_rate/cache_rewrites/global_burn, respects cooldown, and emits SSE")
        passed += 1

        # 4. HTTP endpoints: GET /api/state includes burn_alerts, GET /api/burn works, POST /api/burn/dismiss clears banner
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = srv.server_address[1]
        server.ALLOWED_HOSTS.add(f"127.0.0.1:{port}")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/burn", timeout=5) as r:
                bs = json.loads(r.read().decode("utf-8"))
            assert len(bs["alerts"]) >= 2, bs
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
                st = json.loads(r.read().decode("utf-8"))
            assert len(st.get("burn_alerts") or []) >= 2, st
            aid = bs["alerts"][0]["id"]
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/burn/dismiss",
                data=json.dumps({"id": aid}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "X-KC-App": "1"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                dr = json.loads(r.read().decode("utf-8"))
            assert dr["ok"] and all(a["id"] != aid for a in dr["alerts"]), dr
            req_all = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/burn/dismiss",
                data=json.dumps({"all": True}).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "X-KC-App": "1"},
            )
            with urllib.request.urlopen(req_all, timeout=5) as r:
                dr2 = json.loads(r.read().decode("utf-8"))
            assert dr2["ok"] and dr2["alerts"] == [], dr2
            print("PASS 4: /api/state, /api/burn, and /api/burn/dismiss work end-to-end")
            passed += 1
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        agent.hub.unsubscribe(sse_q)

    print(f"\nALL {passed}/4 CLAUDE BURN GUARD TESTS PASSED")


if __name__ == "__main__":
    run_tests()
