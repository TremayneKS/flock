"""Tests that CLI engines (Codex, Claude Code, Antigravity) finish turns promptly after turn.completed / result
even when a background child process keeps the CLI alive, without killing detached user preview servers."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402

ROOT = Path(__file__).resolve().parent.parent
from harness import agy_engine, claude_engine, codex_engine, osutil, store


class FakeRun:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.cancel = threading.Event()
        self.queue = []
        self.sub = False
        self.lock = threading.Lock()


class CliTurnFinishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flock-cli-case-"))
        self.bg_pids = []

    def tearDown(self):
        for pid in self.bg_pids:
            osutil.kill_tree(pid)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn_hanging_codex_and_bg(self, out_f):
        """Spawn a fake CLI wrapper that emits turn.completed, spawns a detached background server, and hangs."""
        bg_pid_file = self.tmp / "bg.pid"
        script = (
            "import json, subprocess, sys, time\n"
            f"bg = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(bg_pid_file)!r}, 'w').write(str(bg.pid))\n"
            "for ev in [\n"
            "  {'type': 'thread.started', 'thread_id': 'th-1'},\n"
            "  {'type': 'turn.started'},\n"
            "  {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Server is up on :18766.'}},\n"
            "  {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'cached_input_tokens': 0, 'output_tokens': 5}},\n"
            "]:\n"
            "  print(json.dumps(ev), flush=True)\n"
            "time.sleep(60)\n"
        )
        with open(out_f, "wb") as fo:
            proc = subprocess.Popen([sys.executable, "-c", script], stdout=fo, stderr=subprocess.DEVNULL, **osutil.group())
        return proc, bg_pid_file

    def test_codex_finishes_after_turn_completed_without_killing_bg_server(self):
        orig_grace = codex_engine.POST_TURN_GRACE_S
        codex_engine.POST_TURN_GRACE_S = 0.8
        try:
            ch = store.create(str(self.tmp), "codex", title="Codex Hang Test")
            run = FakeRun(ch.id)
            ep = {"id": "codex", "name": "Codex", "kind": "codex", "model": "gpt-6.2"}
            out_f = self.tmp / "engine.out"
            proc, bg_pid_file = self._spawn_hanging_codex_and_bg(out_f)

            t0 = time.time()
            st = codex_engine._follow(run, ch, ep, lambda *a: None, "medium", proc, out_f, 0, None)
            if st["completed"] and proc.poll() is None:
                osutil.terminate_only(proc.pid)
                proc.wait(timeout=3.0)
            elapsed = time.time() - t0

            self.assertTrue(st["completed"])
            self.assertLess(elapsed, 4.0)
            self.assertIsNotNone(proc.poll())
            bg_pid = int(bg_pid_file.read_text().strip())
            self.bg_pids.append(bg_pid)
            self.assertTrue(osutil.alive(bg_pid), "terminate_only must NOT kill detached background preview server")
            msgs = [m for m in ch.messages() if m.get("role") == "assistant"]
            self.assertEqual(len(msgs), 1)
            self.assertIn("Server is up", msgs[0]["content"])
        finally:
            codex_engine.POST_TURN_GRACE_S = orig_grace

    def test_claude_group_member_finishes_after_result_with_bg_server_alive(self):
        ch = store.create(str(self.tmp), "claude", title="Claude Group Hang Test")
        ch.update_meta(touch=False, group_member_of="room-123")
        run = FakeRun(ch.id)
        ep = {"id": "claude", "name": "Claude", "kind": "claude-code", "model": "claude-sonnet-5-5"}
        inbox = self.tmp / "engine.in"
        out_f = self.tmp / "engine.out"
        err_f = self.tmp / "engine.err"
        for f in (inbox, out_f, err_f):
            f.write_bytes(b"")
        bg_pid_file = self.tmp / "claude_bg.pid"
        fake_cli = self.tmp / "fake_claude.py"
        fake_cli.write_text(
            "import json, subprocess, sys, time\n"
            f"bg = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"open({str(bg_pid_file)!r}, 'w').write(str(bg.pid))\n"
            "sys.stdin.readline()\n"
            "evs = [\n"
            "  {'type': 'system', 'subtype': 'task_started', 'task_id': 'bg1', 'task_type': 'local_bash', 'is_backgrounded': True, 'description': 'mock server'},\n"
            "  {'type': 'assistant', 'message': {'id': 'm1', 'model': 'claude-sonnet-5-5', 'content': [{'type': 'text', 'text': 'Mock server ready.'}], 'usage': {'input_tokens': 10, 'output_tokens': 5}}},\n"
            "  {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 1, 'result': 'Mock server ready.'},\n"
            "]\n"
            "for e in evs:\n"
            "  print(json.dumps(e), flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        holder = [sys.executable, "-I", str(ROOT / "harness" / "cli_input.py"),
                  str(inbox), str(out_f), str(err_f), "--", sys.executable, str(fake_cli)]
        proc = subprocess.Popen(holder, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **osutil.group())

        t0 = time.time()
        ok = claude_engine._follow(
            run, ch, ep, lambda *a: None, "high", proc, out_f, err_f, 0,
            [{"type": "text", "text": "start server"}], resume=False, inbox=inbox,
        )
        elapsed = time.time() - t0

        self.assertTrue(ok)
        self.assertLess(elapsed, 12.0)
        self.assertIsNotNone(proc.poll())
        bg_pid = int(bg_pid_file.read_text().strip())
        self.bg_pids.append(bg_pid)
        self.assertTrue(osutil.alive(bg_pid), "cli_input TERM must NOT kill detached background mock server")
        msgs = [m for m in ch.messages() if m.get("role") == "assistant"]
        self.assertTrue(any("Mock server ready" in (m.get("content") or "") for m in msgs))

    def test_agy_breaks_on_result_even_if_child_holds_stdout_open(self):
        ch = store.create(str(self.tmp), "agy", title="Agy Hang Test")
        run = FakeRun(ch.id)
        ep = {"id": "agy", "name": "Antigravity", "kind": "agy", "model": "gemini-4-neon"}
        bg_pid_file = self.tmp / "agy_bg.pid"
        fake_agy = self.tmp / "fake_agy.py"
        fake_agy.write_text(
            "import json, subprocess, sys, time\n"
            "bg = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL)\n"
            f"open({str(bg_pid_file)!r}, 'w').write(str(bg.pid))\n"
            "sys.stdin.readline()\n"
            "print(json.dumps({'event': 'init', 'conversation_id': 'c-1'}), flush=True)\n"
            "print(json.dumps({'event': 'result', 'conversation_id': 'c-1', 'result': {'status': 'SUCCESS', 'response': 'Done checking.', 'usage': {'input_tokens': 10, 'output_tokens': 5}}}), flush=True)\n"
            "time.sleep(60)\n",
            encoding="utf-8",
        )
        orig_exe = agy_engine.agy_exe
        agy_engine.agy_exe = lambda: sys.executable
        orig_popen = subprocess.Popen

        def fake_popen(cmd, **kw):
            if cmd and cmd[0] == sys.executable:
                return orig_popen([sys.executable, str(fake_agy)], **kw)
            return orig_popen(cmd, **kw)

        subprocess.Popen = fake_popen
        try:
            t0 = time.time()
            out = agy_engine._one_turn(run, ch, ep, lambda *a: None, "hello", True)
            elapsed = time.time() - t0
        finally:
            subprocess.Popen = orig_popen
            agy_engine.agy_exe = orig_exe

        self.assertEqual(out, "done")
        self.assertLess(elapsed, 12.0)
        bg_pid = int(bg_pid_file.read_text().strip())
        self.bg_pids.append(bg_pid)
        self.assertTrue(osutil.alive(bg_pid), "agy post-result cleanup must NOT kill detached background child")
        msgs = [m for m in ch.messages() if m.get("role") == "assistant"]
        self.assertTrue(any("Done checking" in (m.get("content") or "") for m in msgs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
