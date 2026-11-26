"""Tests for the Computer tool (harness/computer.py, harness/desktop.py) that never touch the real mouse, keyboard or
screen: Windows input runs as a dry run (every SendInput call is recorded, not sent) and screenshots there are a blank
picture. Covers the PNG encoder, coordinate scaling across several displays, the input each action sends, the
permission flow (ask, allow once / for the chat, deny, Stop letting go of held keys) and the Bash guard.

    python -X utf8 tools/test_computer.py
    python -X utf8 tools/test_computer.py --x11 Ubuntu-24.04     also drive a virtual X display (Xvfb + xterm) in WSL
    python -X utf8 tools/test_computer.py --x11 local            ... or on this Linux machine (needs Xvfb, xterm, xdotool, xwd)

Runs against a throwaway KCH_HOME, never the user's data."""
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-cu-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
os.environ["KCH_COMPUTER_BACKEND"] = "dryrun"
os.environ["KCH_COMPUTER_MONITORS"] = "[[0, 0, 2560, 1440, 1], [-1920, 200, 1920, 1080, 0]]"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import computer, config, desktop, store, tools  # noqa: E402

X11 = None
if "--x11" in sys.argv:
    i = sys.argv.index("--x11")
    X11 = sys.argv[i + 1]
    del sys.argv[i:i + 2]


def unpng(data):
    """(w, h, rgb) of a PNG made by desktop.png (8-bit RGB, filter 0)."""
    w, h = struct.unpack(">II", data[16:24])
    pos, idat = 8, b""
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        t = data[pos + 4:pos + 8]
        if t == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    rows = [raw[y * (w * 3 + 1) + 1:(y + 1) * (w * 3 + 1)] for y in range(h)]
    return w, h, b"".join(rows)


class Ctx(tools.Ctx):
    pass


def make_ctx(vision=True):
    config.load()
    chat = store.create(str(TMP / "proj"), "test")
    chat.update_meta(tools=["Read", "Bash", "Computer"])
    ep = {"id": "test", "name": "Test Model", "vision": vision}
    return tools.Ctx(chat, TMP / "proj", ep, lambda ev: None, threading.Event(), {})


def fresh():
    desktop.reset()
    b = desktop.get()
    b.sent.clear()
    with computer._lock:
        computer._chats.clear()
        computer._asks.clear()
        computer._active.clear()
    return b


def answer_next(choice, after=0.05):
    """Answer the next question the tool asks (as the user would in the window)."""
    def go():
        for _ in range(200):
            time.sleep(after)
            with computer._lock:
                ids = list(computer._asks)
            if ids:
                computer.answer(ids[0], choice)
                return
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


class Pictures(unittest.TestCase):
    def test_png_roundtrip(self):
        rgb = bytes(range(256)) * 3 * 4                  # 16 x 48 pixels... any bytes
        w, h = 32, len(rgb) // (32 * 3)
        data = desktop.png(w, h, rgb[:w * h * 3])
        self.assertEqual(desktop.png_size(data), (w, h))
        self.assertEqual(unpng(data), (w, h, rgb[:w * h * 3]))
        self.assertEqual(zlib.crc32(data[12:29]) & 0xFFFFFFFF, struct.unpack(">I", data[29:33])[0])

    def test_bgrx_and_scale(self):
        bgrx = bytes([10, 20, 30, 0, 40, 50, 60, 255])
        self.assertEqual(bytes(desktop.bgrx_to_rgb(bgrx, 2)), bytes([30, 20, 10, 60, 50, 40]))
        rgb = bytes([1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4])            # 2 x 2
        self.assertEqual(desktop.scale_rgb(2, 2, rgb, 1, 1), bytes([1, 1, 1]))
        self.assertEqual(len(desktop.scale_rgb(2, 2, rgb, 4, 4)), 4 * 4 * 3)
        self.assertEqual(desktop.crop_rgb(2, 2, rgb, 1, 1, 1, 1), bytes([4, 4, 4]))

    def test_xwd_parse(self):
        w, h = 3, 2
        hdr = [100 + 4, 7, 2, 24, w, h, 0, 0, 32, 0, 32, 32, w * 4, 4, 0xFF0000, 0xFF00, 0xFF, 8, 256, 0, w, h, 0, 0, 0]
        px = bytes([10, 20, 30, 0] * (w * h))            # B G R X
        data = struct.pack(">25I", *hdr) + b"xyz\0" + px
        self.assertEqual(desktop.parse_xwd(data), (w, h, bytearray([30, 20, 10] * (w * h))))


class Scaling(unittest.TestCase):
    def test_fit(self):
        self.assertEqual(computer.fit(2560, 1440, 1280, 800), (1280, 720))
        self.assertEqual(computer.fit(1920, 1080, 1280, 800), (1280, 720))
        self.assertEqual(computer.fit(1280, 800, 1280, 800), (1280, 800))
        self.assertEqual(computer.fit(1024, 768, 1280, 800), (1024, 768))        # never scaled up
        self.assertEqual(computer.fit(1600, 1000, 1280, 800), (1280, 800))
        self.assertEqual(computer.fit(200, 100, 1280, 800, up=2.0), (400, 200))

    def test_to_real(self):
        d = {"x": -1920, "y": 200, "w": 1920, "h": 1080}
        self.assertEqual(computer.to_real(d, (1280, 720), 0, 0), (-1920, 200))
        self.assertEqual(computer.to_real(d, (1280, 720), 1279, 719), (-1, 1279))
        self.assertEqual(computer.to_real(d, (1280, 720), 640, 360), (-960 + 0, 200 + 540))
        with self.assertRaises(tools.ToolError):
            computer.to_real(d, (1280, 720), 1280, 10)
        x, y = computer.to_real(d, (1280, 720), 500, 300)
        self.assertEqual(computer.to_shot(d, (1280, 720), x, y), (500, 300))


class DryRunInput(unittest.TestCase):
    def setUp(self):
        self.b = fresh()
        self.ctx = make_ctx()
        computer._busy = lambda c: True            # a turn is running
        with computer._lock:
            computer._st(computer.top(self.ctx.chat)).update(screen=True, control="chat")

    def run_tool(self, **a):
        r = tools.run(self.ctx, "Computer", a)
        self.assertTrue(r["ok"], r["text"])
        return r

    def test_displays(self):
        ds = self.b.displays()
        self.assertEqual([(d["x"], d["w"], d["primary"]) for d in ds], [(0, 2560, True), (-1920, 1920, False)])
        self.assertEqual(self.b._virtual(), (-1920, 0, 4480, 1440))

    def test_click_maps_to_absolute_virtual_desktop(self):
        self.run_tool(action="left_click", coordinate=[640, 360])
        mv, down, up = self.b.sent
        # (640, 360) of a 1280x720 shot of the 2560x1440 main display -> real (1281, 721); virtual desktop starts at -1920
        self.assertEqual(mv["flags"], 0x0001 | 0x8000 | 0x4000)
        self.assertEqual(mv["dx"], round((1281 + 1920) * 65535 / 4479))
        self.assertEqual(mv["dy"], round(721 * 65535 / 1439))
        self.assertEqual((down["flags"], up["flags"]), (0x0002, 0x0004))

    def test_second_display(self):
        self.run_tool(action="screenshot", display=1)
        self.run_tool(action="mouse_move", coordinate=[0, 0])
        mv = self.b.sent[-1]
        self.assertEqual(mv["dx"], 0)
        self.assertEqual(mv["dy"], round(200 * 65535 / 1439))

    def test_double_right_and_modifier_click(self):
        self.run_tool(action="double_click", coordinate=[10, 10])
        self.assertEqual([s["flags"] for s in self.b.sent[1:]], [2, 4, 2, 4])
        self.b.sent.clear()
        self.run_tool(action="right_click", coordinate=[10, 10])
        self.assertEqual([s["flags"] for s in self.b.sent[1:]], [8, 16])
        self.b.sent.clear()
        self.run_tool(action="left_click", coordinate=[10, 10], text="shift")
        keys = [(s["vk"], s["flags"]) for s in self.b.sent if s["type"] == "key"]
        self.assertEqual(keys, [(0x10, 0), (0x10, 2)])

    def test_keys(self):
        self.run_tool(action="key", text="ctrl+s")
        self.assertEqual([(s["vk"], s["flags"]) for s in self.b.sent], [(0x11, 0), (ord("S"), 0), (ord("S"), 2), (0x11, 2)])
        self.b.sent.clear()
        self.run_tool(action="key", text="Return alt+Tab Page_Down")
        self.assertEqual([s["vk"] for s in self.b.sent], [0x0D, 0x0D, 0x12, 0x09, 0x09, 0x12, 0x22, 0x22])
        self.assertEqual(self.b.sent[-1]["flags"], 2 | 1)            # Page Down is an extended key
        r = tools.run(self.ctx, "Computer", {"action": "key", "text": "hyperspace"})
        self.assertFalse(r["ok"])

    def test_type_unicode_and_newline(self):
        self.run_tool(action="type", text="hé\n€")
        ks = self.b.sent
        self.assertEqual([(k["scan"], k["flags"]) for k in ks[:4]], [(ord("h"), 4), (ord("h"), 6), (ord("é"), 4), (ord("é"), 6)])
        self.assertEqual(ks[4]["vk"], 0x0D)
        self.assertEqual(ks[6]["scan"], ord("€"))

    def test_scroll_and_drag(self):
        self.run_tool(action="scroll", coordinate=[100, 100], scroll_direction="down", scroll_amount=2)
        self.assertEqual((self.b.sent[-1]["flags"], self.b.sent[-1]["data"]), (0x0800, -240))
        self.b.sent.clear()
        self.run_tool(action="left_click_drag", start_coordinate=[10, 10], coordinate=[110, 60])
        flags = [s["flags"] for s in self.b.sent]
        self.assertEqual(flags.count(2), 1)
        self.assertEqual(flags[-1], 4)
        self.assertEqual(sum(1 for f in flags if f & 1), 13)

    def test_screenshot_vision_and_zoom(self):
        r = self.run_tool(action="screenshot")
        self.assertTrue(Path(r["image"]).exists())
        self.assertEqual(desktop.png_size(Path(r["image"]).read_bytes()), (1280, 720))
        self.assertIn("1280x720", r["text"])
        self.assertIn("Other displays: 1", r["text"])
        z = self.run_tool(action="zoom", region=[0, 0, 320, 180])
        self.assertEqual(desktop.png_size(Path(z["image"]).read_bytes()), computer.fit(642, 362, 1280, 800, up=2.0))
        self.assertIn("still refer to the full 1280x720", z["text"])

    def test_every_model_gets_native_screenshot_never_described(self):
        self.assertFalse(hasattr(computer, "_describe"))
        self.assertFalse(hasattr(computer, "_describe_fallback"))
        self.ctx.ep = {"id": "test", "name": "Small", "vision": False}
        r = self.run_tool(action="screenshot")
        self.assertTrue(Path(r["image"]).exists())
        self.assertNotIn("cannot see images", r["text"])
        self.assertNotIn("described it", r["text"])

    def test_outside_and_unknown(self):
        r = tools.run(self.ctx, "Computer", {"action": "left_click", "coordinate": [5000, 10]})
        self.assertFalse(r["ok"])
        self.assertIn("outside the screenshot", r["text"])
        r = tools.run(self.ctx, "Computer", {"action": "explode"})
        self.assertFalse(r["ok"])


class Permission(unittest.TestCase):
    def setUp(self):
        self.b = fresh()
        self.ctx = make_ctx()
        self.busy = True
        computer._busy = lambda c: self.busy

    def tearDown(self):
        self.busy = False

    def test_screenshot_asks_once_per_chat(self):
        answer_next("allow")
        r = tools.run(self.ctx, "Computer", {"action": "screenshot"})
        self.assertTrue(r["ok"], r["text"])
        t0 = time.time()
        r = tools.run(self.ctx, "Computer", {"action": "screenshot"})       # no question this time
        self.assertTrue(r["ok"])
        self.assertLess(time.time() - t0, 1.5)

    def test_screen_permission_does_not_allow_control(self):
        answer_next("allow")
        tools.run(self.ctx, "Computer", {"action": "screenshot"})
        answer_next("deny")
        r = tools.run(self.ctx, "Computer", {"action": "left_click", "coordinate": [1, 1]})
        self.assertFalse(r["ok"])
        self.assertIn("did not allow control", r["text"])
        self.assertEqual(self.b.sent, [])                                   # nothing was sent
        r = tools.run(self.ctx, "Computer", {"action": "type", "text": "x"})   # denied for the rest of the reply
        self.assertIn("did not allow control", r["text"])
        self.assertEqual(self.b.sent, [])

    def test_allow_once_ends_with_the_turn(self):
        cid = computer.top(self.ctx.chat)
        answer_next("once")
        r = tools.run(self.ctx, "Computer", {"action": "left_click", "coordinate": [1, 1]})
        self.assertTrue(r["ok"], r["text"])
        self.assertIn(cid, computer._active)                                # the banner is up
        self.busy = False                                                   # the reply ends
        for _ in range(50):
            if cid not in computer._active:
                break
            time.sleep(0.05)
        self.assertNotIn(cid, computer._active)
        self.assertIsNone(computer._st(cid)["control"])

    def test_allow_for_chat_and_stop_takes_it_back(self):
        cid = computer.top(self.ctx.chat)
        answer_next("chat")
        tools.run(self.ctx, "Computer", {"action": "mouse_move", "coordinate": [1, 1]})
        self.assertEqual(computer._st(cid)["control"], "chat")
        out = {}
        th = threading.Thread(target=lambda: out.update(tools.run(self.ctx, "Computer", {"action": "hold_key", "text": "shift",
                                                                                          "duration": 5})))
        th.start()
        time.sleep(0.4)
        computer.stop(cid)
        th.join(3)
        self.assertFalse(th.is_alive())
        self.assertFalse(out["ok"])
        self.assertIn("stopped", out["text"].lower())
        keys = [(s["vk"], s["flags"]) for s in self.b.sent if s["type"] == "key"]
        self.assertEqual(keys[-1], (0x10, 2))                               # shift was let go
        self.assertIsNone(computer._st(cid)["control"])
        r = tools.run(self.ctx, "Computer", {"action": "left_click", "coordinate": [1, 1]})
        self.assertFalse(r["ok"])                                           # asks again; Stop also refused the rest of the turn

    def test_always_ask_mode_has_no_chat_choice(self):
        config.update(lambda c: c.setdefault("computer", {}).update(mode="ask"))
        try:
            got = {}

            def peek():
                for _ in range(100):
                    time.sleep(0.03)
                    with computer._lock:
                        a = next(iter(computer._asks.values()), None)
                    if a:
                        got["choices"] = a["choices"]
                        computer.answer(a["id"], "deny")
                        return
            threading.Thread(target=peek, daemon=True).start()
            tools.run(self.ctx, "Computer", {"action": "left_click", "coordinate": [1, 1]})
            self.assertEqual(got["choices"], ["once", "deny"])
        finally:
            config.update(lambda c: c["computer"].update(mode="chat"))

    def test_off(self):
        config.update(lambda c: c.setdefault("computer", {}).update(enabled=False))
        try:
            r = tools.run(self.ctx, "Computer", {"action": "screenshot"})
            self.assertIn("turned off", r["text"])
            self.assertFalse(computer.offered())
        finally:
            config.update(lambda c: c["computer"].update(enabled=True))

    def test_turn_stop_cancels_a_waiting_question(self):
        out = {}
        th = threading.Thread(target=lambda: out.update(tools.run(self.ctx, "Computer", {"action": "screenshot"})))
        th.start()
        time.sleep(0.3)
        self.ctx.cancel.set()
        th.join(3)
        self.assertIn("Stopped", out["text"])
        self.assertEqual(computer._asks, {})


class EscWatcher(unittest.TestCase):
    def test_physical_esc_stops_control(self):
        b = fresh()
        ctx = make_ctx()
        computer._busy = lambda c: True
        cid = computer.top(ctx.chat)
        with computer._lock:
            computer._st(cid).update(screen=True, control="chat")
        b.can_watch_esc = True
        pressed = {"at": time.time() + 0.5}
        b.esc_down = lambda: time.time() > pressed["at"]
        out = {}
        th = threading.Thread(target=lambda: out.update(tools.run(ctx, "Computer", {"action": "hold_key", "text": "ctrl",
                                                                                     "duration": 5})))
        th.start()
        th.join(4)
        b.can_watch_esc = False
        self.assertFalse(th.is_alive())
        self.assertIn("stopped", out["text"].lower())
        self.assertNotIn(cid, computer._active)
        self.assertEqual([(s["vk"], s["flags"]) for s in b.sent][-1], (0x11, 2))       # ctrl let go


class BashGuard(unittest.TestCase):
    def test_refused(self):
        ctx = make_ctx()
        for cmd in ['python -c "import pyautogui; pyautogui.click(10, 10)"',
                    "xdotool type hello",
                    "powershell -c \"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('hi')\"",
                    "python - <<'EOF'\nimport pyautogui\npyautogui.press('enter')\nEOF",
                    'osascript -e \'tell application "System Events" to keystroke "a"\'',
                    "AutoHotkey.exe macro.ahk", "nircmd sendkeypress enter",
                    "python -c 'from PIL import ImageGrab; ImageGrab.grab().save(\"s.png\")'"]:
            self.assertIsNotNone(computer.gui_automation_refusal(cmd, ctx), cmd)
        r = tools.run(ctx, "Bash", {"command": "xdotool key Return"})
        self.assertFalse(r["ok"])
        self.assertIn("Use the Computer tool", r["text"])

    def test_script_file_is_read(self):
        ctx = make_ctx()
        ctx.cwd.mkdir(parents=True, exist_ok=True)
        (ctx.cwd / "auto.py").write_text("import time\nimport pyautogui\npyautogui.typewrite('x')\n", encoding="utf-8")
        (ctx.cwd / "fine.py").write_text("print('hello')\n", encoding="utf-8")
        self.assertIsNotNone(computer.gui_automation_refusal("python auto.py", ctx))
        self.assertIsNone(computer.gui_automation_refusal("python fine.py && echo ok", ctx))

    def test_allowed(self):
        ctx = make_ctx()
        for cmd in ["grep -rn pyautogui .", "pip install pyautogui", "rg xdotool src", "git commit -m 'drop xdotool'",
                    "ls -la", "python -m pytest -q", "npm run build", "echo done"]:
            self.assertIsNone(computer.gui_automation_refusal(cmd, ctx), cmd)


class EngineServers(unittest.TestCase):
    def test_mcp_args(self):
        ctx = make_ctx()
        meta = ctx.chat.meta()
        env = {}
        args = computer.claude_mcp_args(ctx.chat, meta, env)
        self.assertEqual(args[0], "--mcp-config")
        self.assertNotIn(computer.TOKEN, Path(args[1]).read_text(encoding="utf-8"))   # the token is in env only
        self.assertEqual(env["KCH_COMPUTER_TOKEN"], computer.TOKEN)
        self.assertGreaterEqual(int(env["MCP_TOOL_TIMEOUT"]), 600_000)
        self.assertIn("mcp_servers.computer.command=", " ".join(computer.codex_mcp_args(ctx.chat, meta)))
        ctx.chat.update_meta(tools=["Read"])
        self.assertEqual(computer.claude_mcp_args(ctx.chat, ctx.chat.meta(), {}), [])

    def test_mcp_server_roundtrip(self):
        """servers/computer.py speaks MCP on stdio and forwards to the app; here the app is a tiny stand-in server."""
        import http.server
        import json
        got = {}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                got.update(path=self.path, token=self.headers.get("X-KC-Computer-Token"), body=body)
                out = json.dumps({"text": "Screenshot of display 0", "ok": True, "image": "aGk=", "mime": "image/png"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        spec = TMP / "spec.json"
        spec.write_text(json.dumps(computer.spec(engine=True)), encoding="utf-8")
        env = {**os.environ, "KCH_COMPUTER_URL": f"http://127.0.0.1:{srv.server_port}/api/computer",
               "KCH_COMPUTER_TOKEN": "t0k", "KCH_COMPUTER_CHAT": "c1", "KCH_COMPUTER_SPEC": str(spec)}
        p = subprocess.Popen([sys.executable, str(Path(computer.__file__).parent / "servers" / "computer.py")], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "computer", "arguments": {"action": "screenshot"}}}]
        p.stdin.write(("\n".join(json.dumps(m) for m in msgs) + "\n").encode())
        p.stdin.flush()
        lines = [json.loads(p.stdout.readline()) for _ in range(3)]
        p.stdin.close()
        p.wait(5)
        p.stdout.close()
        srv.shutdown()
        srv.server_close()
        self.assertEqual(lines[1]["result"]["tools"][0]["name"], "computer")
        res = next(m for m in lines if m.get("id") == 3)["result"]
        self.assertEqual(res["content"][1], {"type": "image", "data": "aGk=", "mimeType": "image/png"})
        self.assertEqual((got["path"], got["token"], got["body"]["chat"]), ("/api/computer/call", "t0k", "c1"))


@unittest.skipUnless(X11, "--x11 DISTRO|local not given")
class LiveX11(unittest.TestCase):
    """The X11 backend on a virtual display: Xvfb with one xterm, never the user's own screen."""
    DISPLAY = ":97"

    @classmethod
    def setUpClass(cls):
        cls.wrap = [] if X11 == "local" else ["wsl", "-d", X11, "--"]
        nw = 0x08000000 if os.name == "nt" else 0
        cls.procs = [subprocess.Popen(cls.wrap + ["Xvfb", cls.DISPLAY, "-screen", "0", "1600x1000x24", "-nolisten", "tcp"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=nw)]
        time.sleep(2)
        cls.procs.append(subprocess.Popen(cls.wrap + ["env", f"DISPLAY={cls.DISPLAY}", "xterm", "-geometry", "100x30+0+0",
                                                      "-e", "bash --norc -i"], stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, creationflags=nw))
        time.sleep(2.5)
        cls.b = desktop.X11Desktop(display=cls.DISPLAY, wrap=cls.wrap + ["env", f"DISPLAY={cls.DISPLAY}"])

    @classmethod
    def tearDownClass(cls):
        subprocess.run(cls.wrap + ["pkill", "-f", f"Xvfb {cls.DISPLAY}"], capture_output=True)
        for p in cls.procs:
            p.kill()

    def test_screen_click_type(self):
        ds = self.b.displays()
        self.assertEqual((ds[0]["w"], ds[0]["h"]), (1600, 1000))
        size = computer.fit(1600, 1000, 1280, 800)
        data = self.b.capture(0, 0, 1600, 1000, *size)
        w, h, rgb = unpng(data)
        self.assertEqual((w, h), (1280, 800))
        self.assertGreater(len(set(rgb[i:i + 3] for i in range(0, len(rgb), 3 * 97))), 1)    # the xterm is there
        x, y = computer.to_real(ds[0], size, 200, 100)
        self.b.move(x, y)
        self.b.button("left", True)
        self.b.button("left", False)
        self.b.flush()
        self.assertEqual(self.b.cursor(), (x, y))
        out = "/tmp/kch-cu-typed.txt"
        subprocess.run(self.wrap + ["rm", "-f", out], capture_output=True)
        self.b.text(f"echo typed-by-test > {out}")
        self.b.key("enter", True)
        self.b.key("enter", False)
        self.b.flush()
        time.sleep(1.0)
        r = subprocess.run(self.wrap + ["cat", out], capture_output=True)
        self.assertEqual(r.stdout.decode().strip(), "typed-by-test")


if __name__ == "__main__":
    try:
        unittest.main(verbosity=1)
    finally:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
