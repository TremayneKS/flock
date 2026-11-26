"""Every engine has a working way to start subagents the user sees in the app, and is told the right one.

    python -X utf8 tools/test_subagent_tools.py

22 Nov 14:01: a Neon asked for subagents ran `claude -p` in a shell and then agy's own invoke_subagent; it had no
harness tool, and the note every model gets said "the Agent / harness_agent tool". Held here (stand-ins only: a fake
harness HTTP server on 127.0.0.1, a throwaway KCH_HOME):
- servers/agents.py as a command (a Neon in print mode): JSON on stdin starts a subagent through the app and prints
  its reply; report and parent go to their endpoints; bad input and a missing chat environment are refused;
- servers/agents.py as an MCP server (Claude Code, Codex, Neon over acp) still lists harness_agent;
- the wording per engine: API models "the Agent tool", Claude Code and Codex "the harness_agent tool", Neon the
  harness_agent tool as agy names it or the command; never "Agent / harness_agent";
- the tool each engine really has: API models the app's Agent tool, the CLIs harness_agent from mcp_server()
  (Claude Code and Codex pass it; acp and print mode are held in test_agy_acp.py / test_agy_background.py)."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)

TMP = Path(tempfile.mkdtemp(prefix="kch-subtools-"))
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
from harness import config, subagents  # noqa: E402

config.load()
APP = Path(__file__).resolve().parent.parent
AGENTS = APP / "harness" / "servers" / "agents.py"
SEEN = []


class Fake(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        SEEN.append({"path": self.path, "token": self.headers.get("X-KC-Agent-Token"), "body": body})
        if self.path.endswith("/report-main"):
            out = {"ok": True, "text": "Sent findings to main chat."}
        elif self.path.endswith("/parent-context"):
            out = {"ok": True, "text": "latest: the main chat said hello"}
        else:
            out = {"ok": True, "text": "subagent reply: 3 files checked"}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


SRV = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=SRV.serve_forever, daemon=True).start()
ENV = {"KCH_AGENT_URL": "http://127.0.0.1:%d/api/subagent" % SRV.server_address[1], "KCH_AGENT_TOKEN": "tok-1",
       "KCH_AGENT_CHAT": "chat-neon-1"}


def cli(args, stdin="", env=ENV):
    e = {k: v for k, v in os.environ.items() if not k.startswith("KCH_AGENT_")}
    e.update(env)
    p = subprocess.run([sys.executable, "-X", "utf8", str(AGENTS)] + args, input=stdin, capture_output=True,
                       text=True, encoding="utf-8", env=e, timeout=60)
    return p.returncode, p.stdout.strip()


class SubagentTools(unittest.TestCase):
    def setUp(self):
        SEEN.clear()

    def test_command_starts_a_subagent_through_the_app(self):
        rc, out = cli(["agent", "-"], json.dumps({"model": "deepseek", "prompt": "check the 3 files",
                                                 "description": "check files"}))
        self.assertEqual((rc, out), (0, "subagent reply: 3 files checked"))
        self.assertEqual(len(SEEN), 1)
        req = SEEN[0]
        self.assertEqual(req["path"], "/api/subagent")
        self.assertEqual(req["token"], "tok-1")
        self.assertEqual(req["body"]["chat"], "chat-neon-1", "the subagent belongs to this chat (shown under it)")
        self.assertEqual((req["body"]["model"], req["body"]["prompt"]), ("deepseek", "check the 3 files"))

    def test_command_from_a_file_and_with_a_bom(self):
        f = TMP / "task.json"
        f.write_text("﻿" + json.dumps({"subagent": "Reviewer", "prompt": "review"}), encoding="utf-8")
        rc, out = cli(["agent", "@" + str(f)])
        self.assertEqual(rc, 0, out)
        self.assertEqual(SEEN[0]["body"]["subagent"], "Reviewer")

    def test_report_and_parent(self):
        rc, out = cli(["report", "-"], json.dumps({"summary": "done: 2 fixes", "wake_main": True}))
        self.assertEqual((rc, out), (0, "Sent findings to main chat."))
        self.assertEqual(SEEN[-1]["path"], "/api/subagent/report-main")
        self.assertEqual(SEEN[-1]["body"]["summary"], "done: 2 fixes")
        rc, out = cli(["parent", "-"], json.dumps({"action": "latest"}))
        self.assertEqual(rc, 0)
        self.assertEqual(SEEN[-1]["path"], "/api/subagent/parent-context")

    def test_bad_input_and_no_chat_are_refused(self):
        rc, out = cli(["agent", "{model:deepseek,prompt:x}"])          # what PowerShell 5 makes of a JSON argument
        self.assertEqual(rc, 2)
        self.assertIn("Pipe it in", out)
        rc, out = cli(["agent", "-"], json.dumps({"prompt": "x"}), env={})
        self.assertEqual(rc, 2)
        self.assertIn("only inside a Flock chat", out)
        rc, out = cli(["claude", "-"], "{}")
        self.assertEqual(rc, 2)
        self.assertFalse(SEEN, "nothing reached the app")

    def test_mcp_mode_still_lists_harness_agent(self):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        e = {k: v for k, v in os.environ.items() if not k.startswith("KCH_AGENT_")}
        e.update(ENV)
        p = subprocess.run([sys.executable, "-X", "utf8", str(AGENTS)], input="".join(json.dumps(m) + "\n" for m in msgs),
                           capture_output=True, text=True, env=e, timeout=30)
        lines = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
        names = [t["name"] for t in lines[-1]["result"]["tools"]]
        self.assertIn("harness_agent", names)
        self.assertIn("harness_chats", names)

    def test_each_engine_is_told_its_own_way(self):
        orig = subagents.presets
        subagents.presets = lambda: [{"name": "Reviewer", "model_name": "DeepSeek", "id": "rev", "model": "deepseek",
                                      "instructions": ""}]
        try:
            notes = {k: subagents.presets_note({"kind": k}) for k in ("claude-code", "codex", "agy", "openai", None)}
        finally:
            subagents.presets = orig
        for k, n in notes.items():
            self.assertNotIn("Agent / harness_agent", n, k)
            self.assertIn("Reviewer", n)
        self.assertIn("the harness_agent tool", notes["claude-code"])
        self.assertIn("the harness_agent tool", notes["codex"])
        self.assertIn("mcp_harness_harness_agent", notes["agy"])
        self.assertIn("agents.py\" agent -", notes["agy"])
        self.assertIn("the Agent tool", notes["openai"])
        self.assertIn("the Agent tool", notes[None])

    def test_the_tool_each_engine_really_has(self):
        meta = {"depth": 0}
        self.assertEqual(subagents.spec(meta)["name"], "Agent", "API models: the app's own Agent tool")
        self.assertEqual(subagents.spec(meta, engine=True)["name"], "harness_agent", "the CLIs: the MCP tool")
        for f in ("claude_engine.py", "codex_engine.py"):
            self.assertIn("subagents.mcp_server(", (APP / "harness" / f).read_text(encoding="utf-8"), f)
        self.assertIn("subagents.acp_mcp(", (APP / "harness" / "agy_acp.py").read_text(encoding="utf-8"))
        self.assertIn("subagents.mcp_server(", (APP / "harness" / "agy_engine.py").read_text(encoding="utf-8"))
        note = subagents.agy_note(meta)
        for want in ("mcp_harness_harness_agent", "agents.py\" agent -", "claude -p", "invoke_subagent"):
            self.assertIn(want, note)
        self.assertEqual(subagents.agy_note({"mode": "assistant"}), "", "no subagents offered: no note")


if __name__ == "__main__":
    unittest.main(verbosity=2)
