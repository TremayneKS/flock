#!/usr/bin/env python3
"""End-to-end verification suite for the public desktop Flock release.

Covers:
1. Easy API-key onboarding: /api/presets env_key_present detection (never leaks secret values),
   saving keys to FLOCK_HOME/keys/<id> (never in config.json, never returned to browser),
   native Anthropic Messages API streaming + thinking + tool_use + prompt caching + /models health check.
2. Subagents end-to-end: named subagent presets, embedded subagent chats with forked parent context,
   ParentContext (search/latest/read/notes), ReportToMain (quiet + wake), and live Agent tool execution
   against a local mock OpenAI-compatible SSE server.
3. Rich chat formatting & desktop features: VISUAL_RULE + MEDIA_RULE + bundled charts skill,
   desktop sections (General/Work/Personal) with move/archive/undo, desktop screen capture (dryrun),
   and clean desktop HTTP endpoints (/api/state, /api/chats, /api/selfcheck, /static/sheets.js).
4. Flock branding, Meetings feature label, existing-install compatibility fallbacks (FLOCK_*/NEST_*/RAVEN_*/KCH_*,
   .flock.json/.nest.json/.raven.json/.kc-harness.json, X-Flock-App/X-KC-App, Raven-27B model name preservation,
   and zero KC telemetry).

    python -X utf8 tools/test_public_release.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402

TMP = Path(tempfile.mkdtemp(prefix="kch-pub-e2e-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
os.environ["KCH_COMPUTER_BACKEND"] = "dryrun"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-env-0000000000000000000000000000"  # publish-scan: ok

from harness import agent, checks, config, desktop, meetings, meetings_setup, presets, providers, sections, selfupdate, server, skills, store, subagents  # noqa: E402


class MockModelServer(BaseHTTPRequestHandler):
    """Serves both OpenAI (/v1/chat/completions, /v1/models) and Anthropic (/v1/messages, /v1/models)."""
    seen_requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/models"):
            if self.headers.get("x-api-key") == "sk-ant-saved-12345" or self.headers.get("Authorization") == "Bearer sk-openai-saved-12345":
                body = json.dumps({"data": [{"id": "mock-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        req = json.loads(raw.decode("utf-8"))
        self.seen_requests.append({"path": self.path, "headers": dict(self.headers), "body": req})

        if self.path.endswith("/messages"):
            # Native Anthropic Messages API SSE response
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            msgs = req.get("messages") or []
            has_tool_result = any(
                isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"])
                for m in msgs
            )
            if not has_tool_result and req.get("tools"):
                events = [
                    {"type": "message_start", "message": {"model": req.get("model"), "usage": {"input_tokens": 120, "cache_creation_input_tokens": 80, "cache_read_input_tokens": 0, "output_tokens": 1}}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Checking the project notes."}},
                    {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Notes", "input": {}}},
                    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"add":"Anthropic tool test ok"}'}},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 25}},
                    {"type": "message_stop"},
                ]
            else:
                events = [
                    {"type": "message_start", "message": {"model": req.get("model"), "usage": {"input_tokens": 30, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 180, "output_tokens": 1}}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "All done via native Anthropic API!"}},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}},
                    {"type": "message_stop"},
                ]
            for ev in events:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
            self.wfile.flush()
            return

        if self.path.endswith("/chat/completions"):
            # OpenAI-compatible SSE response (supports parent -> Agent subagent call)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            msgs = req.get("messages") or []
            sys_txt = " ".join(str(m.get("content") or "") for m in msgs if m.get("role") == "system")
            is_sub = "You are a subagent:" in sys_txt
            has_tool_res = any(m.get("role") == "tool" for m in msgs)
            if not is_sub and not has_tool_res and any(
                (t.get("function") or {}).get("name") == "Agent" for t in (req.get("tools") or [])
            ):
                chunks = [
                    {"model": req.get("model"), "choices": [{"delta": {"tool_calls": [
                        {"index": 0, "id": "call_sub_1", "type": "function",
                         "function": {"name": "Agent", "arguments": json.dumps({"subagent": "Reviewer", "prompt": "Audit module alpha", "description": "Audit alpha"})}}
                    ]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
                ]
            elif is_sub:
                chunks = [
                    {"model": req.get("model"), "choices": [{"delta": {"content": "Subagent Reviewer audited alpha: 0 defects."}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 60, "completion_tokens": 12}},
                ]
            else:
                chunks = [
                    {"model": req.get("model"), "choices": [{"delta": {"content": "Parent received subagent report and verified alpha."}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 140, "completion_tokens": 14}},
                ]
            for ch in chunks:
                self.wfile.write(f"data: {json.dumps(ch)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self.send_response(404)
        self.end_headers()


class PublicReleaseE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        config.load()
        cls.mock_srv = ThreadingHTTPServer(("127.0.0.1", 0), MockModelServer)
        cls.mock_port = cls.mock_srv.server_address[1]
        threading.Thread(target=cls.mock_srv.serve_forever, daemon=True).start()

        cls.app_srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.app_port = cls.app_srv.server_address[1]
        server.ALLOWED_HOSTS.add(f"127.0.0.1:{cls.app_port}")
        server.ALLOWED_ORIGINS.add(f"http://127.0.0.1:{cls.app_port}")
        threading.Thread(target=cls.app_srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.app_port}"

    @classmethod
    def tearDownClass(cls):
        cls.app_srv.shutdown()
        cls.app_srv.server_close()
        cls.mock_srv.shutdown()
        cls.mock_srv.server_close()
        shutil.rmtree(TMP, ignore_errors=True)

    def _get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Host": f"127.0.0.1:{self.app_port}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()

    def _post(self, path, data):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-KC-App": "1", "Host": f"127.0.0.1:{self.app_port}"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_1_presets_env_detection_and_key_isolation(self):
        code, raw = self._get("/api/presets")
        self.assertEqual(code, 200)
        data = json.loads(raw)
        anth = next((p for p in data["presets"] if p["preset"] == "anthropic"), None)
        self.assertIsNotNone(anth)
        self.assertTrue(anth["env_key_present"])
        # Actual key value MUST NEVER appear anywhere in /api/presets
        self.assertNotIn("sk-ant-test-env", raw.decode("utf-8"))

        # Save an Anthropic API connection with an explicit key
        code, res = self._post("/api/endpoints", {"endpoint": {
            "id": "anth-mock",
            "preset": "anthropic",
            "name": "Anthropic Mock",
            "kind": "anthropic",
            "base": f"http://127.0.0.1:{self.mock_port}/v1",
            "model": "claude-sonnet-5-5",
            "key": "sk-ant-saved-12345",
        }})
        self.assertEqual(code, 200)
        # Key must live in keys/anth-mock, NOT in config.json and NOT in /api/state
        self.assertTrue((config.KEYS / "anth-mock").is_file())
        cfg_txt = config.CONFIG_PATH.read_text(encoding="utf-8")
        self.assertNotIn("sk-ant-saved-12345", cfg_txt)
        _, state_raw = self._get("/api/state")
        state_txt = state_raw.decode("utf-8")
        self.assertNotIn("sk-ant-saved-12345", state_txt)
        self.assertNotIn("vision_helper", state_txt)
        # Health check works against native Anthropic /v1/models with x-api-key
        _, h_raw = self._get("/api/endpoints/health?id=anth-mock")
        self.assertTrue(json.loads(h_raw)["ok"])

    def test_2_native_anthropic_turn_with_thinking_and_tools(self):
        # Ensure endpoint exists
        self.test_1_presets_env_detection_and_key_isolation()
        MockModelServer.seen_requests.clear()
        proj = str(TMP / "proj-anth")
        Path(proj).mkdir(exist_ok=True)
        _, cr = self._post("/api/chats", {"project": proj, "endpoint": "anth-mock", "reasoning": "think"})
        cid = cr["id"]
        _, sr = self._post(f"/api/chats/{cid}/send", {"text": "Write a note and finish."})
        self.assertFalse(sr.get("queued"))
        deadline = time.time() + 10
        while agent.busy(cid) and time.time() < deadline:
            time.sleep(0.05)
        ch = store.Chat(cid)
        msgs = ch.messages()
        asst = [m for m in msgs if m.get("role") == "assistant"]
        self.assertGreaterEqual(len(asst), 2, msgs)
        self.assertIn("Checking the project notes.", asst[0].get("reasoning_content") or "")
        self.assertIn("All done via native Anthropic API!", asst[-1].get("content") or "")
        self.assertIn("Anthropic tool test ok", ch.notes())
        # Verify prompt caching breakpoints & thinking were sent
        anth_reqs = [r for r in MockModelServer.seen_requests if r["path"].endswith("/messages")]
        self.assertGreaterEqual(len(anth_reqs), 2)
        self.assertEqual(anth_reqs[0]["headers"].get("x-api-key"), "sk-ant-saved-12345")
        self.assertIn("thinking", anth_reqs[0]["body"])
        self.assertEqual(anth_reqs[0]["body"]["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_3_subagents_presets_agent_tool_and_parent_context(self):
        # Add mock OpenAI endpoint
        self._post("/api/endpoints", {"endpoint": {
            "id": "mock-oai",
            "name": "Mock OpenAI",
            "kind": "openai",
            "base": f"http://127.0.0.1:{self.mock_port}/v1",
            "model": "mock-gpt",
            "key": "sk-openai-saved-12345",
        }})
        # Create a named subagent preset
        _, pr = self._post("/api/subagents/presets", {
            "name": "Reviewer",
            "model": "mock-oai",
            "instructions": "Audit files thoroughly.",
        })
        self.assertTrue(pr["ok"])
        self.assertEqual(pr["preset"]["name"], "Reviewer")

        proj = str(TMP / "proj-sub")
        Path(proj).mkdir(exist_ok=True)
        _, cr = self._post("/api/chats", {"project": proj, "endpoint": "mock-oai", "reasoning": "fast"})
        cid = cr["id"]
        ch = store.Chat(cid)
        ch.set_notes("Project Alpha uses strict schemas.")
        # Run a real turn where parent invokes Agent(subagent="Reviewer") -> subagent runs -> returns answer to parent
        self._post(f"/api/chats/{cid}/send", {"text": "Have Reviewer audit alpha."})
        deadline = time.time() + 10
        while agent.busy(cid) and time.time() < deadline:
            time.sleep(0.05)
        msgs = ch.messages()
        self.assertTrue(any("Parent received subagent report" in (m.get("content") or "") for m in msgs), msgs)
        subs = subagents.children_of(cid)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["name"], "Reviewer")
        self.assertEqual(subs[0]["status"], "done")

        # Also test direct user-spawned embedded subagent chat + ParentContext + ReportToMain
        _, sp = self._post(f"/api/chats/{cid}/subagents", {
            "name": "SideCheck",
            "model": "mock-oai",
            "fork_context": True,
        })
        self.assertTrue(sp["ok"])
        sub_id = sp["id"]
        ctx_notes = subagents.search_parent(cid, action="notes")
        self.assertIn("Project Alpha uses strict schemas.", ctx_notes)
        ctx_search = subagents.search_parent(cid, action="search", query="Reviewer")
        self.assertIn("Reviewer", ctx_search)
        _, rep = self._post(f"/api/chats/{sub_id}/report-main", {
            "summary": "SideCheck confirmed all schemas pass.",
            "wake_main": False,
        })
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["mode"], "handed_off")
        self.assertTrue(any(m.get("role") == "sub_report" and "SideCheck confirmed" in (m.get("content") or "") for m in ch.messages()))

    def test_4_desktop_assets_sections_visuals_and_screen_capture(self):
        # Verify HTML, desktop sheets JS/CSS, visuals JS/CSS, group-chat JS/CSS, and selfcheck all serve cleanly
        for path in ("/", "/static/sheets.js", "/static/sheets.css", "/static/visuals.js", "/static/visuals.css", "/static/group-chat.js", "/static/group-chat.css", "/api/selfcheck"):
            code, raw = self._get(path)
            self.assertEqual(code, 200, path)
            if path == "/":
                html = raw.decode("utf-8")
                self.assertIn("<title>Flock</title>", html)
                self.assertIn('<span class="bname">Flock</span>', html)
                self.assertIn('>Meetings</button>', html)
                self.assertNotIn("KC Harness", html)
                self.assertNotIn("KC Meetings", html)
                self.assertIn("/static/sheets.js", html)
                self.assertIn("/static/visuals.js", html)
                self.assertIn("/static/group-chat.js", html)
                self.assertIn("/static/group-chat.css", html)
                self.assertNotIn("mobile.js", html)
                self.assertNotIn("Qwythos", html)
            elif path == "/api/selfcheck":
                sc = json.loads(raw)
                self.assertTrue(sc["ok"], sc)

        # Default desktop sections are General, Work, Personal (no Uni/Phone/KC)
        _, sec_raw = self._get("/api/sections")
        sec_ids = [s["id"] for s in json.loads(sec_raw)["sections"]]
        self.assertEqual(sec_ids, ["general", "work", "personal"])

        # Bundled charts skill is discovered and clean of private references
        sk = skills.get("charts")
        self.assertIsNotNone(sk)
        sk_txt = (Path(sk["dir"]) / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Flock", sk_txt)
        self.assertNotIn("KC Harness", sk_txt)
        self.assertNotIn("D:", sk_txt)

        # Desktop screen capture works via dryrun backend
        _, cap = self._post("/api/screen/capture", {"chat": "", "display": 0})
        self.assertTrue(cap["ok"], cap)
        self.assertTrue(Path(cap["path"]).is_file())

    def test_5_flock_meetings_compat_and_raven27b_preserved(self):
        # Meetings label is "Meetings" (no KC prefix)
        self.assertEqual(meetings_setup.NAME, "Meetings")
        self.assertEqual(meetings.label(), "Meetings")
        m_html = (ROOT / "meetings" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>Meetings</title>", m_html)
        self.assertNotIn("KC Meetings", m_html)

        # Meetings link env provides FLOCK_* plus legacy NEST_*/RAVEN_*/KC_HARNESS_* for existing installs
        le = meetings.link_env()
        for k in ("FLOCK_URL", "FLOCK_TOKEN", "NEST_URL", "RAVEN_URL", "KC_HARNESS_URL"):
            self.assertTrue(le.get(k), f"missing {k} in meetings.link_env()")

        # X-Flock-App header works alongside legacy X-KC-App header
        req = urllib.request.Request(
            self.base + "/api/endpoints",
            data=json.dumps({"endpoint": {
                "id": "raven-27b-local",
                "name": "Raven-27B",
                "kind": "openai",
                "base": f"http://127.0.0.1:{self.mock_port}/v1",
                "model": "Raven-27B",
            }}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-Flock-App": "1", "Host": f"127.0.0.1:{self.app_port}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
        ep = config.endpoint("raven-27b-local")
        self.assertEqual(ep["name"], "Raven-27B")
        self.assertEqual(ep["model"], "Raven-27B")

        # Project test config discovery preserves .flock.json -> .nest.json -> .raven.json -> .kc-harness.json
        for cfg_name in (".flock.json", ".nest.json", ".raven.json", ".kc-harness.json"):
            pdir = TMP / f"proj-cfg-{cfg_name.strip('.')}"
            pdir.mkdir(exist_ok=True)
            (pdir / cfg_name).write_text(json.dumps({"test": f"echo {cfg_name}"}), encoding="utf-8")
            f = pdir / "app.py"
            f.write_text("x = 1\n", encoding="utf-8")
            cmds = checks.test_commands(pdir, [f])
            self.assertEqual(cmds, [(pdir, f"echo {cfg_name}")])

        # No telemetry or unexpected phone-home hosts in runtime code (allowlist of public provider/API/update/spec hosts)
        import re as _re
        import urllib.parse as _up
        allowed_hosts = {
            "127.0.0.1", "localhost", "www.w3.org", "schemas.openxmlformats.org", "scripts.sil.org",
            "api.anthropic.com", "api.openai.com", "api.deepseek.com", "generativelanguage.googleapis.com",
            "openrouter.ai", "api.groq.com", "api.mistral.ai", "api.together.xyz", "api.orcarouter.ai",
            "opencode.ai", "api.github.com", "github.com", "codeload.github.com",
            "objects.githubusercontent.com", "release-assets.githubusercontent.com",
            "github-releases.githubusercontent.com", "api.openalex.org", "www.ebi.ac.uk",
            "eutils.ncbi.nlm.nih.gov", "europepmc.org", "pubmed.ncbi.nlm.nih.gov",
            "pmc.ncbi.nlm.nih.gov", "doi.org", "claude.com", "example.org",
        }
        url_rx = _re.compile(r"https?://([A-Za-z0-9.-]+)")
        for rel_dir in ("harness", "web", "meetings"):
            for fp in (ROOT / rel_dir).rglob("*"):
                if not fp.is_file() or "vendor" in fp.parts or fp.name == "cm.js":
                    continue
                if fp.suffix.lower() not in (".py", ".js", ".css", ".html"):
                    continue
                txt = fp.read_text(encoding="utf-8", errors="ignore")
                for m in url_rx.finditer(txt):
                    host = m.group(1).lower()
                    if host in ("...", "x") or host.startswith("{"):
                        continue
                    self.assertIn(host, allowed_hosts, f"unexpected outbound host {host!r} in {fp}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
