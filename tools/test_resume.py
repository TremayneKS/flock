"""A turn carries on after the app restarts (user requirement).

    python -X utf8 tools/test_resume.py            all cases (about 3 minutes)
    python -X utf8 tools/test_resume.py -k kill    one case

Each case starts the real app (run.py) as its own process on a spare port with a throwaway KCH_HOME, and a stand-in
model over HTTP (OpenAI-compatible, streamed) that asks for Bash commands one at a time (`sleep; echo k >> steps.txt`),
then answers. The app is then killed (taskkill /F of the app's process only, as an update or a crash does) or restarted
from its own Restart (POST /api/restart), started again, and the turn has to finish by itself: every step run exactly
once, the command that was running when the app died waited for (it keeps running), the model told in one line, the
turn record gone. Stop, a finished turn and a failed turn must stay as they were. No real model, no network, never
the user's data or ports."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)

APP = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"
NOWIN = 0x08000000 if WINDOWS else 0


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------------------------------------ stand-in model
class Script:
    """What the stand-in model does: `steps` Bash calls of `sleep` seconds each, then an answer. `slow_step`: that
    model call itself takes `slow_secs` (to cut the app while the model is answering). `fail`: answer HTTP 400."""

    def __init__(self, steps=4, sleep=4, slow_step=None, slow_secs=0, fail=False):
        self.steps, self.sleep, self.slow_step, self.slow_secs, self.fail = steps, sleep, slow_step, slow_secs, fail
        self.requests = []            # the messages of every request


class Model(BaseHTTPRequestHandler):
    script = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        s = Model.script
        msgs = body.get("messages") or []
        s.requests.append(msgs)
        if s.fail:
            data = b'{"error": {"message": "bad request (stand-in)"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        last_user = max(i for i, m in enumerate(msgs) if m["role"] == "user" and "steps.txt" in str(m.get("content")))
        done = sum(1 for m in msgs[last_user:] if m["role"] == "tool")
        if s.slow_step is not None and done == s.slow_step:
            time.sleep(s.slow_secs)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(delta, finish=None):
            ev = {"id": "x", "object": "chat.completion.chunk", "model": "standin",
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
            self.wfile.flush()
        if done < s.steps:
            k = done + 1
            args = json.dumps({"command": f"sleep {s.sleep}; echo {k} >> steps.txt; echo step {k} done"})
            chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_{k}_{int(time.time()*1000)}", "type": "function",
                                                        "function": {"name": "Bash", "arguments": args}}]})
            chunk({}, "tool_calls")
        else:
            chunk({"role": "assistant", "content": f"All {s.steps} steps are done."})
            chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


# ------------------------------------------------------------------------------------------------ the app
class App:
    def __init__(self, home, port, profile, claude=None):
        self.home, self.port, self.profile, self.proc, self.claude = home, port, profile, None, claude

    def start(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "KCH_"))}
        env.update(KCH_HOME=str(self.home), KCH_PORT=str(self.port), KCH_PROFILE=str(self.profile), PYTHONUTF8="1",
                   KCH_CODEX=sys.executable, **({"KCH_CLAUDE": str(self.claude)} if self.claude else {}))
        log = open(self.home / "server.log", "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-X", "utf8", str(APP / "run.py")], cwd=str(APP), env=env,
                                     stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     creationflags=NOWIN | (0x00000200 if WINDOWS else 0))
        self.wait_up()
        return self

    def wait_up(self, timeout=150):          # a busy PC can take a minute to start the app
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                self.get("/api/busy", timeout=2)
                return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("the app did not come up: " + (self.home / "server.log").read_text(errors="replace")[-2000:])

    def kill(self, tree=False):
        """What an update or a crash does: the app's own process ends at once; what it started is not touched (tree:
        everything it started ends too)."""
        if WINDOWS:
            subprocess.run(["taskkill", "/F", *(["/T"] if tree else []), "/PID", str(self.proc.pid)], capture_output=True,
                           creationflags=NOWIN)
        else:
            os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(20)

    def stop_all(self):
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)] if WINDOWS else ["kill", str(self.proc.pid)],
                           capture_output=True, creationflags=NOWIN)

    def get(self, path, timeout=10):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout) as r:
            return json.load(r)

    def post(self, path, body, timeout=30):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-KC-App": "1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)


# A stand-in for the Codex CLI: the app runs KCH_CODEX (here this Python) with "exec ..." as its arguments, so Python
# runs this file, saved as `exec` in the project folder (Codex's working folder). It prints Codex's JSON events, with a
# slow command in the middle, and logs how it was called.
FAKE_CODEX = r'''
import json, os, sys, time, uuid
args = sys.argv[1:]
prompt = sys.stdin.read()
log = open("codex_calls.log", "a", encoding="utf-8")
log.write(json.dumps({"resume": args[:1] == ["resume"], "prompt": prompt[-300:]}) + "\n")
log.close()
def ev(o):
    sys.stdout.write(json.dumps(o) + "\n")
    sys.stdout.flush()
if args[:1] == ["resume"]:
    ev({"type": "thread.started", "thread_id": args[1]})
    ev({"type": "turn.started"})
    ev({"type": "item.completed", "item": {"id": "r1", "type": "agent_message", "text": "Resumed after: " + prompt.strip()[-120:]}})
    ev({"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 5}})
    sys.exit(0)
ev({"type": "thread.started", "thread_id": str(uuid.uuid4())})
ev({"type": "turn.started"})
ev({"type": "item.completed", "item": {"id": "a1", "type": "agent_message", "text": "Starting the slow command."}})
ev({"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "slow"}})
open("fake_started", "w").close()
time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP") or 8))
open("fake_finished", "w").close()
ev({"type": "item.completed", "item": {"id": "c1", "type": "command_execution", "command": "slow", "exit_code": 0,
                                       "aggregated_output": "slow done"}})
ev({"type": "item.completed", "item": {"id": "a2", "type": "agent_message", "text": "All done."}})
ev({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}})
'''


# A stand-in for the Claude Code CLI (KCH_CLAUDE = a .cmd / script that runs this with Python). It reads stream-json
# messages, answers each, and for "start the agent" starts a background agent the way Claude Code does: inside its
# own process (a thread here), so it ends when the process ends, reported by task_started / task_notification. On
# --resume it first reports the agents an earlier process took with it as stopped, each with an empty result
# (num_turns 0), as the real CLI does. Every line it answers names its pid, so the test sees which process answered.
FAKE_CLAUDE = r'''
import json, os, sys, threading, time
args = sys.argv[1:]
if "-p" not in args:                  # the app's own checks (claude --version)
    print("0.0.0 (stand-in)")
    sys.exit(0)
pid = os.getpid()
lock = threading.Lock()
def ev(o):
    with lock:
        sys.stdout.write(json.dumps(o) + "\n")
        sys.stdout.flush()
def log(**kw):
    with open("claude_calls.log", "a", encoding="utf-8") as f:
        f.write(json.dumps({"pid": pid, **kw}) + "\n")
n = [0]
def reply(text, turns=1):
    n[0] += 1
    mid = "msg_%d_%d" % (pid, n[0])
    ev({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
    ev({"type": "assistant", "message": {"id": mid, "model": "fake", "content": [{"type": "text", "text": text}],
                                         "usage": {"input_tokens": 5, "output_tokens": 2}}})
    ev({"type": "result", "subtype": "success", "is_error": False, "num_turns": turns, "result": text})
log(start=True, resume="--resume" in args)
if "--resume" in args and os.path.exists("agent_running"):
    ev({"type": "system", "subtype": "task_notification", "task_id": "agent1", "status": "stopped",
        "summary": "Background agent didn't finish before the previous session ended"})
    ev({"type": "system", "subtype": "init"})
    ev({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "result": ""})
agents = []
kill = threading.Event()              # the CLI's interrupt ends every subagent it runs (measured, 23 Nov)
busy = threading.Event()              # a turn is running a long foreground command
held = []                             # messages written in while it runs: Claude gets them at its next step
fg = {"stop": threading.Event(), "how": None}
def agent(secs):
    open("agent_running", "w").close()
    if kill.wait(secs):
        ev({"type": "system", "subtype": "task_updated", "task_id": "agent1", "patch": {"status": "killed"}})
        ev({"type": "system", "subtype": "task_notification", "task_id": "agent1", "status": "stopped",
            "summary": "killed: stopped by the user or by the app connected to this session"})
        return
    os.remove("agent_running")
    open("agent_done", "w").close()
    while busy.is_set():
        time.sleep(0.1)
    ev({"type": "system", "subtype": "task_notification", "task_id": "agent1", "status": "completed", "summary": "done"})
    reply("pid %d: the agent reported back" % pid)
def start_agent():
    secs = float(open("agent_secs").read()) if os.path.exists("agent_secs") else 10
    n[0] += 1
    mid = "msg_%d_%d" % (pid, n[0])
    ev({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
    ev({"type": "assistant", "message": {"id": mid, "model": "fake", "usage": {"input_tokens": 5, "output_tokens": 2},
                                         "content": [{"type": "tool_use", "id": "tu_agent", "name": "Agent",
                                                      "input": {"run_in_background": True}}]}})
    ev({"type": "system", "subtype": "task_started", "task_id": "agent1", "is_backgrounded": True, "tool_use_id": "tu_agent",
        "task_type": "local_agent", "description": "the background agent"})
    ev({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_agent",
                                                                 "content": "Async agent launched"}]}})
    t = threading.Thread(target=agent, args=(secs,))
    t.start()
    agents.append(t)
def work():                           # a long foreground command of the main turn (30 s unless stopped)
    n[0] += 1
    mid = "msg_%d_%d" % (pid, n[0])
    ev({"type": "stream_event", "event": {"type": "message_start", "message": {"id": mid}}})
    ev({"type": "assistant", "message": {"id": mid, "model": "fake", "usage": {"input_tokens": 5, "output_tokens": 2},
                                         "content": [{"type": "text", "text": "pid %d: now working" % pid},
                                                     {"type": "tool_use", "id": "tu_fg", "name": "Bash", "input": {}}]}})
    ev({"type": "system", "subtype": "task_started", "task_id": "bfgwork01", "is_backgrounded": False, "tool_use_id": "tu_fg",
        "task_type": "local_bash", "description": "the long command"})
    stopped = fg["stop"].wait(30)
    ev({"type": "system", "subtype": "task_notification", "task_id": "bfgwork01", "status": "stopped" if stopped else "completed"})
    if fg["how"] == "interrupt":
        ev({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_fg",
                                                                     "content": "rejected", "is_error": True}]}})
        ev({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 2})
    else:
        ev({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_fg",
            "content": "Exit code 137" if stopped else "work done", "is_error": bool(stopped)}]}})
        got = "".join("; got " + t for t in held)
        held.clear()
        reply("pid %d: the work step ended%s" % (pid, got), turns=3)
    busy.clear()
for raw in sys.stdin.buffer:
    msg = json.loads(raw)
    if msg.get("type") == "control_request":
        req = msg.get("request") or {}
        log(control=req.get("subtype"))
        if req.get("subtype") == "interrupt":
            kill.set()
            fg["how"] = "interrupt"
            fg["stop"].set()
        elif req.get("subtype") == "stop_task" and req.get("task_id") == "bfgwork01":
            fg["how"] = "stop"
            fg["stop"].set()
        ev({"type": "control_response", "response": {"subtype": "success", "request_id": msg.get("request_id")}})
        continue
    if msg.get("type") != "user":
        continue
    text = " ".join(b.get("text", "") for b in msg["message"]["content"] if b.get("type") == "text")
    log(got=text[-200:])
    if busy.is_set():
        held.append(text.strip()[-80:])
    elif "start the agent and work" in text:
        start_agent()
        busy.set()
        fg["stop"].clear()
        fg["how"] = None
        threading.Thread(target=work).start()
    elif "start the agent" in text:
        start_agent()
        reply("pid %d: the agent is working in the background" % pid, turns=2)
    else:
        reply("pid %d: got %s" % (pid, text.strip()[-80:]))
while busy.is_set():
    time.sleep(0.1)
for t in agents:                      # input closed: still up for its background agents, as Claude Code is
    t.join()
'''


class Resume(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kch-resume-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        (self.proj / "exec").write_text(FAKE_CODEX, encoding="utf-8")
        fake = self.tmp / "fake_claude.py"
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        if WINDOWS:
            claude = self.tmp / "claude.cmd"
            claude.write_text(f'@"{sys.executable}" -X utf8 "{fake}" %*\r\n', encoding="utf-8")
        else:
            claude = self.tmp / "claude"
            claude.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
            claude.chmod(0o755)
        mport = free_port()
        self.script = Model.script = Script()
        self.model = ThreadingHTTPServer(("127.0.0.1", mport), Model)
        threading.Thread(target=self.model.serve_forever, daemon=True).start()
        prof = {"endpoint": "standin", "state_dir": str(self.home / "state"),
                "endpoints": [{"id": "standin", "name": "Stand-in", "kind": "openai", "base": f"http://127.0.0.1:{mport}/v1",
                               "model": "standin", "context": 1000000},
                              {"id": "cx", "name": "ChatGPT", "kind": "codex", "model": "fake", "context": 1000000},
                              {"id": "cc", "name": "Claude", "kind": "claude-code", "model": "fake", "context": 1000000}],
                "runtime": {"enabled": False}, "meetings": {"enabled": False}}
        self.profile = self.tmp / "profile.json"
        self.profile.write_text(json.dumps(prof), encoding="utf-8")
        self.app = App(self.home, free_port(), self.profile, claude).start()

    def tearDown(self):
        self.app.stop_all()
        self._kill_port_owner()          # a copy started by the app's own Restart
        self.model.shutdown()
        time.sleep(0.5)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers
    def new_chat(self, text=None, endpoint="standin"):
        cid = self.app.post("/api/chats", {"project": str(self.proj), "endpoint": endpoint})["id"]
        self.app.post(f"/api/chats/{cid}/send", {"text": text or "Write the steps into steps.txt, one Bash call each."})
        return cid

    def msgs(self, cid):
        f = self.home / "chats" / cid / "messages.jsonl"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()] if f.exists() else []

    def turns_dir(self, cid):
        return self.home / "turns" / cid

    def wait(self, cond, timeout=90, what="condition"):
        t0 = time.time()
        while time.time() - t0 < timeout:
            v = cond()
            if v:
                return v
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def running_call(self, cid, k):
        """Step k's command has started (its process record exists) and has not finished."""
        d = self.turns_dir(cid)
        running = [f for f in d.glob("*.run.json") if not (d / f.name.replace(".run.json", ".exit")).exists()]             if d.exists() else []
        steps = (self.proj / "steps.txt").read_text().split() if (self.proj / "steps.txt").exists() else []
        return len(steps) == k - 1 and bool(running)

    def idle(self, cid):
        return cid not in (self.app.get("/api/busy").get("chats") or [])

    def check_finished(self, cid, steps=4, resumed=1):
        ms = self.msgs(cid)
        self.assertEqual((self.proj / "steps.txt").read_text().split(), [str(k) for k in range(1, steps + 1)],
                         "every step ran exactly once")
        self.assertEqual(ms[-1].get("role"), "assistant", ms[-1])
        self.assertIn(f"All {steps} steps are done", ms[-1].get("content") or "")
        lines = [m for m in ms if m.get("resume")]
        self.assertEqual(len(lines), resumed, "one quiet line per restart")
        for m in lines:
            self.assertTrue(m.get("auto"))
            self.assertIn("Carry on from where you stopped", m.get("content"))
        tools_ = [m for m in ms if m.get("role") == "tool"]
        self.assertEqual(len(tools_), steps, "one result per call, none doubled")
        self.assertTrue(all(m.get("ok") and "step " in (m.get("content") or "") for m in tools_),
                        [m.get("content") for m in tools_])
        self.assertFalse((self.turns_dir(cid) / "turn.json").exists(), "the turn record is removed when the turn ends")
        seen = [m for req in self.script.requests for m in req if m.get("role") == "user" and "restarted" in str(m.get("content"))]
        self.assertTrue(seen or not resumed, "the model was told")
        return ms

    # ---- cases
    def test_kill_in_the_middle_of_a_command(self):
        """The update case: the app is killed while a step's command runs; the command keeps going, the new copy waits
        for it, takes its real output and carries on."""
        cid = self.new_chat()
        self.wait(lambda: self.running_call(cid, 2), what="step 2's command")
        self.app.kill()
        time.sleep(1)
        rec = json.loads((self.turns_dir(cid) / "turn.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["chat"], cid)
        self.app.start()
        self.assertIn(cid, self.app.get("/api/busy")["chats"], "the page sees it working again at once")
        self.wait(lambda: self.idle(cid), what="the turn to finish")
        ms = self.check_finished(cid)
        two = next(m for m in ms if m.get("role") == "tool" and "step 2" in (m.get("content") or ""))
        self.assertNotIn("cut off", two["content"])

    def test_kill_while_the_model_answers(self):
        """Killed while the model call is in flight: the new copy asks again from the same messages."""
        self.script.slow_step, self.script.slow_secs = 2, 6
        cid = self.new_chat()
        self.wait(lambda: (self.proj / "steps.txt").exists() and len((self.proj / "steps.txt").read_text().split()) == 2
                  and len(self.script.requests) >= 3, what="the slow model call")
        self.app.kill()
        self.script.slow_step = None
        self.app.start()
        self.wait(lambda: self.idle(cid), what="the turn to finish")
        self.check_finished(cid)

    def test_restart_from_the_app(self):
        """Settings > Restart (POST /api/restart, also what the shipping pipeline should call): the turn goes on in the
        new copy, which tells the model it was restarted."""
        cid = self.new_chat()
        self.wait(lambda: self.running_call(cid, 2), what="step 2's command")
        self.assertTrue(self.app.get("/api/capabilities").get("resume_turns"), "what the shipping pipeline checks")
        r = self.app.post("/api/restart", {})
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("carrying_on"), 1)
        self.app.proc.wait(30)
        # the new copy is started by the old one (run.py --after <pid>): find it by its port
        self.app.wait_up()
        self.wait(lambda: self.idle(cid), what="the turn to finish")
        self.check_finished(cid)

    def _kill_port_owner(self):
        if not WINDOWS:
            return
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=NOWIN).stdout
        for ln in out.splitlines():
            p = ln.split()
            if len(p) >= 5 and p[1].endswith(f":{self.app.port}") and p[3] == "LISTENING":
                subprocess.run(["taskkill", "/F", "/T", "/PID", p[4]], capture_output=True, creationflags=NOWIN)

    def test_two_restarts_in_a_row(self):
        """Updates two minutes apart: killed twice in one turn, it still finishes, with one line per restart."""
        self.script.steps = 5
        cid = self.new_chat()
        self.wait(lambda: self.running_call(cid, 2), what="step 2's command")
        self.app.kill()
        self.app.start()
        self.wait(lambda: self.running_call(cid, 4), what="step 4's command")
        self.app.kill()
        self.app.start()
        self.wait(lambda: self.idle(cid), what="the turn to finish")
        self.check_finished(cid, steps=5, resumed=2)

    def test_message_sent_while_it_worked_is_kept(self):
        """A message the user sent while it worked, not yet given to the model, goes in after the restart."""
        self.script.sleep = 8
        cid = self.new_chat()
        self.wait(lambda: self.running_call(cid, 2), what="step 2's command")
        self.app.post(f"/api/chats/{cid}/send", {"text": "Also: keep it short."})
        self.app.kill()                                 # within the 3 s before the message would cut the command
        self.app.start()
        self.wait(lambda: self.idle(cid), what="the turn to finish")
        ms = self.msgs(cid)
        sent = [m for m in ms if m.get("role") == "user" and m.get("content") == "Also: keep it short."]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].get("steer"))
        self.assertTrue(any(m.get("content") == "Also: keep it short." for req in self.script.requests for m in req))

    def codex_calls(self):
        f = self.proj / "codex_calls.log"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def test_codex_goes_on_through_the_restart(self):
        """Codex (ChatGPT) writes its events to a file, so killing the app does not touch it: the new copy follows the
        same process to the end of its turn. Codex is called once; nothing is resumed or repeated."""
        cid = self.new_chat("Run the slow command.", endpoint="cx")
        self.wait(lambda: (self.proj / "fake_started").exists(), what="Codex's command")
        self.app.kill()
        self.app.start()
        self.wait(lambda: self.idle(cid), what="the turn to finish")
        ms = self.msgs(cid)
        self.assertEqual(len(self.codex_calls()), 1, "Codex was not started again")
        self.assertTrue((self.proj / "fake_finished").exists())
        texts = [m.get("content") for m in ms if m.get("role") == "assistant" and m.get("content")]
        self.assertEqual(texts, ["Starting the slow command.", "All done."])
        self.assertEqual([m.get("content") for m in ms if m.get("role") == "tool"], ["slow done"])
        self.assertEqual(len([m for m in ms if m.get("resume")]), 1)
        self.assertIn("carried on through the restart", [m for m in ms if m.get("resume")][0]["content"])
        self.assertFalse((self.turns_dir(cid) / "turn.json").exists())

    def test_codex_resumes_its_thread_when_its_process_died_too(self):
        """Everything killed (taskkill /T): Codex's own thread is resumed with the one line."""
        cid = self.new_chat("Run the slow command.", endpoint="cx")
        self.wait(lambda: (self.proj / "fake_started").exists(), what="Codex's command")
        self.app.kill(tree=True)
        self.app.start()
        self.wait(lambda: self.idle(cid), what="the turn to finish")
        calls = self.codex_calls()
        self.assertEqual([c["resume"] for c in calls], [False, True])
        self.assertIn("Carry on from where you stopped", calls[1]["prompt"])
        ms = self.msgs(cid)
        texts = [m.get("content") for m in ms if m.get("role") == "assistant" and m.get("content")]
        self.assertEqual(texts[0], "Starting the slow command.", "what it said before it died is kept")
        self.assertIn("Resumed after:", texts[-1])
        self.assertIn("Carry on from where you stopped", texts[-1])
        self.assertEqual([m for m in ms if m.get("role") == "tool"], [])
        self.assertFalse((self.turns_dir(cid) / "turn.json").exists())

    # ---- Claude Code: background agents live inside its process (23 Nov 2026: four agents and a background command
    # stopped five times in 80 minutes, by the next message, a restart, and the CLI ending once its input was shut)
    def claude_calls(self):
        f = self.proj / "claude_calls.log"
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def said(self, cid, part):
        return any(part in (m.get("content") or "") for m in self.msgs(cid) if m.get("role") == "assistant")

    def check_one_process_did_it(self, cid, starts=1):
        calls = self.claude_calls()
        self.assertEqual(len([c for c in calls if c.get("start")]), starts, calls)
        self.assertEqual(len({c["pid"] for c in calls[-3:]}), 1, "the same process answered and kept its agent")
        self.assertTrue((self.proj / "agent_done").exists(), "the background agent finished its work")
        self.assertTrue(self.said(cid, "the agent reported back"), "its report is shown in the chat")
        ms = self.msgs(cid)
        self.assertFalse([m for m in ms if "still running unseen" in str(m.get("content"))])
        self.assertFalse((self.turns_dir(cid) / "turn.json").exists())

    def test_claude_background_agent_survives_the_next_message(self):
        (self.proj / "agent_secs").write_text("12")
        cid = self.new_chat("Please start the agent.", endpoint="cc")
        self.wait(lambda: self.said(cid, "the agent is working in the background"), what="the agent to start")
        time.sleep(2)
        self.assertFalse(self.idle(cid), "the chat shows Claude up for its background agent")
        self.app.post(f"/api/chats/{cid}/send", {"text": "Second message"})
        self.wait(lambda: self.said(cid, "got Second message"), timeout=20, what="the message to go in")
        self.assertFalse((self.proj / "agent_done").exists(), "answered while the agent still worked")
        self.wait(lambda: self.idle(cid) and (self.proj / "agent_done").exists(), what="the agent to finish")
        self.check_one_process_did_it(cid)

    def test_claude_background_agent_survives_a_restart(self):
        """The app is killed (an update) while Claude's background agent works; the new copy writes the next message
        into the same Claude process, so the agent is not ended for it."""
        (self.proj / "agent_secs").write_text("25")
        cid = self.new_chat("Please start the agent.", endpoint="cc")
        self.wait(lambda: self.said(cid, "the agent is working in the background"), what="the agent to start")
        time.sleep(1)
        self.app.kill()
        self.app.start()
        self.app.post(f"/api/chats/{cid}/send", {"text": "After the restart"})
        self.wait(lambda: self.said(cid, "got After the restart"), timeout=30, what="the message to go in")
        self.assertFalse((self.proj / "agent_done").exists(), "answered while the agent still worked")
        self.wait(lambda: self.idle(cid) and (self.proj / "agent_done").exists(), what="the agent to finish")
        self.check_one_process_did_it(cid)

    def test_claude_stopped_reports_at_resume_do_not_shut_the_input(self):
        """The 23 Nov loop: a resumed session starts with reports of the agents the last process took with it and an
        empty result each. Taken for the end of the turn, they shut the input; the agents Claude then started had to
        be ended for the next message, which made the next resume start the same way."""
        (self.proj / "agent_secs").write_text("15")
        cid = self.new_chat("hello", endpoint="cc")
        self.wait(lambda: self.idle(cid) and self.said(cid, "got hello"), what="the first turn")
        (self.proj / "agent_running").write_text("")          # an agent the last process took with it
        self.app.post(f"/api/chats/{cid}/send", {"text": "Please start the agent."})
        self.wait(lambda: self.said(cid, "the agent is working in the background"), what="the agent to start")
        time.sleep(2)
        self.app.post(f"/api/chats/{cid}/send", {"text": "Second message"})
        self.wait(lambda: self.said(cid, "got Second message"), timeout=20, what="the message to go in")
        self.wait(lambda: self.idle(cid) and (self.proj / "agent_done").exists(), what="the agent to finish")
        self.check_one_process_did_it(cid, starts=2)

    def test_claude_message_during_a_running_turn_keeps_the_agent(self):
        """23 Nov 20:51: a message sent while the main turn ran a long command was cut in with the CLI's interrupt,
        which ended the subagents too. Now it is written in, the long command alone is stopped, the agent goes on."""
        (self.proj / "agent_secs").write_text("20")
        cid = self.new_chat("Please start the agent and work.", endpoint="cc")
        self.wait(lambda: self.said(cid, "now working"), what="the long command")
        time.sleep(1)
        self.app.post(f"/api/chats/{cid}/send", {"text": "Mid-turn message"})
        self.wait(lambda: self.said(cid, "got Mid-turn message"), timeout=20, what="the message to go in")
        self.assertNotIn("interrupt", [c.get("control") for c in self.claude_calls()], "no interrupt was sent")
        self.assertIn("stop_task", [c.get("control") for c in self.claude_calls()])
        self.assertFalse((self.proj / "agent_done").exists(), "answered while the agent still worked")
        self.wait(lambda: self.idle(cid) and (self.proj / "agent_done").exists(), what="the agent to finish")
        self.check_one_process_did_it(cid)

    def test_claude_stop_still_ends_it_and_its_agent(self):
        (self.proj / "agent_secs").write_text("30")
        cid = self.new_chat("Please start the agent.", endpoint="cc")
        self.wait(lambda: self.said(cid, "the agent is working in the background"), what="the agent to start")
        pid = self.claude_calls()[0]["pid"]
        self.app.post(f"/api/chats/{cid}/stop", {})
        self.wait(lambda: self.idle(cid), timeout=20, what="Stop")
        sys.path.insert(0, str(APP))
        from harness import osutil
        self.wait(lambda: not osutil.alive(pid), timeout=15, what="the Claude process to end")
        self.assertFalse((self.proj / "agent_done").exists())

    def test_stopped_turn_stays_stopped(self):
        cid = self.new_chat()
        self.wait(lambda: self.running_call(cid, 2), what="step 2's command")
        self.app.post(f"/api/chats/{cid}/stop", {})
        self.wait(lambda: self.idle(cid), what="Stop")
        self.assertFalse((self.turns_dir(cid) / "turn.json").exists())
        n = len(self.msgs(cid))
        self.app.kill()
        self.app.start()
        time.sleep(4)
        self.assertTrue(self.idle(cid))
        self.assertEqual(len(self.msgs(cid)), n, "nothing was carried on")

    def test_finished_and_failed_turns_stay_as_they_were(self):
        self.script.steps, self.script.sleep = 1, 1
        done = self.new_chat()
        self.wait(lambda: self.idle(done) and len(self.msgs(done)) >= 4, what="a short turn")
        self.script.fail = True
        failed = self.new_chat()
        self.wait(lambda: self.idle(failed) and any(m.get("role") == "error" for m in self.msgs(failed)), what="a failed turn")
        self.script.fail = False
        before = {c: len(self.msgs(c)) for c in (done, failed)}
        self.assertFalse(any((self.home / "turns").glob("*/turn.json")))
        self.app.kill()
        self.app.start()
        time.sleep(4)
        self.assertEqual({c: len(self.msgs(c)) for c in (done, failed)}, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
