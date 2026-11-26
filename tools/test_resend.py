"""A message whose send fails in transit is sent again until it gets through, and is taken once (user requirement).

    python -X utf8 tools/test_resend.py

- the server: one message (one key from the page) is one send, however many times the page had to send it;
- the page (its own code from web/app.js, run in node with a stand-in connection): a dropped connection or a 5xx is
  tried again with the same key and the user sees "Sending" meanwhile; a refusal (4xx) is not tried again; after
  SEND_TRY_MS the message goes back to the user.
Stand-ins only: a throwaway KCH_HOME, never the user's data."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-resend-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import config, server  # noqa: E402

config.load()


class Once(unittest.TestCase):
    def setUp(self):
        server._sent.clear()
        self.calls = []

    def go(self, tag="a"):
        def f():
            self.calls.append(tag)
            return {"queued": False, "n": len(self.calls)}
        return f

    def test_the_same_message_sent_again_is_one_send(self):
        r1 = server.send_once("chat-1", "k1", self.go())
        r2 = server.send_once("chat-1", "k1", self.go())
        r3 = server.send_once("chat-1", "k1", self.go())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(r1, r2)
        self.assertEqual(r1, r3)

    def test_another_message_or_another_chat_is_its_own_send(self):
        server.send_once("chat-1", "k1", self.go())
        server.send_once("chat-1", "k2", self.go())
        server.send_once("chat-2", "k1", self.go())
        self.assertEqual(len(self.calls), 3)

    def test_without_a_key_every_call_is_a_send(self):
        server.send_once("chat-1", "", self.go())
        server.send_once("chat-1", None, self.go())
        self.assertEqual(len(self.calls), 2)

    def test_two_tries_arriving_together_still_make_one_send(self):
        gate = threading.Event()

        def slow():
            gate.wait(2)
            self.calls.append("slow")
            return {"queued": True, "id": "q1"}

        out = []
        ts = [threading.Thread(target=lambda: out.append(server.send_once("chat-1", "k9", slow))) for _ in range(4)]
        for t in ts:
            t.start()
        gate.set()
        for t in ts:
            t.join(5)
        self.assertEqual(self.calls, ["slow"])
        self.assertEqual(out, [{"queued": True, "id": "q1"}] * 4)

    def test_old_keys_are_forgotten(self):
        server.send_once("chat-1", "k1", self.go())
        k = ("chat-1", "k1")
        server._sent[k] = (server._sent[k][0] - server.SENT_KEEP_S - 5, server._sent[k][1])
        server.send_once("chat-1", "k-other", self.go())
        self.assertNotIn(k, server._sent)


NODE = r"""
const src = require('fs').readFileSync(process.argv[2], 'utf8');
const a = src.indexOf('const SEND_TRY_MS'), b = src.indexOf('KCH.sendThrough = sendThrough;');
if (a < 0 || b < 0) { console.log(JSON.stringify({ error: 'the page code was not found' })); process.exit(0); }
let block = src.slice(a, b).replace('const SEND_TRY_MS = 180000;', 'let SEND_TRY_MS = 180000;');
const rows = [];                                  // what the "Sending" box showed, each time it was painted
const box = { hidden: true, innerHTML: '', append() {}, before() {} };
const S = { chat: 'c1' };
function $(id) { return id === 'sendingBox' ? box : { before() {} }; }
function el(tag, cls, text) { const e = { cls, text: text || '', append(...k) { for (const x of k) if (x.text) rows.push(x.text); } }; return e; }
let plan = [], calls = [];
async function api(path, body) {
  calls.push({ path, key: body.key, text: body.text });
  const step = plan.shift() || { ok: { queued: false } };
  if (step.drop) throw new TypeError('Failed to fetch');
  if (step.status) { const e = new Error('HTTP ' + step.status); e.status = step.status; throw e; }
  return step.ok;
}
const realSetTimeout = setTimeout;
global.setTimeout = (f, ms) => realSetTimeout(f, Math.min(ms, 5));     // the waits between tries, shortened
eval(block + '; globalThis.T = { sendThrough, sendKey, transient, sending, setLimit: (ms) => { SEND_TRY_MS = ms; } };');
(async () => {
  const out = {};
  // 1. the connection drops twice, then the message gets through
  plan = [{ drop: 1 }, { status: 502 }, { ok: { queued: true, id: 'q1' } }]; calls = []; rows.length = 0;
  let key = T.sendKey();
  out.r1 = await T.sendThrough('c1', { text: 'hello there', key });
  out.calls1 = calls.length; out.sameKey1 = calls.every((c) => c.key === key && c.path === '/api/chats/c1/send');
  out.shown1 = rows.filter((t) => t.includes('Sending') && t.includes('hello there')).length;
  out.left1 = T.sending.size; out.hidden1 = box.hidden;
  // 2. the server refuses (a bad request): not tried again
  plan = [{ status: 400 }]; calls = [];
  try { await T.sendThrough('c1', { text: 'x', key: T.sendKey() }); out.r2 = 'no error'; } catch (e) { out.r2 = e.status; }
  out.calls2 = calls.length;
  // 3. first try works: one call, nothing shown
  plan = [{ ok: { queued: false } }]; calls = []; rows.length = 0;
  out.r3 = await T.sendThrough('c1', { text: 'y', key: T.sendKey() });
  out.calls3 = calls.length; out.shown3 = rows.length;
  // 4. it never gets through: handed back after the limit, not for ever
  T.setLimit(60); plan = Array.from({ length: 500 }, () => ({ drop: 1 })); calls = [];
  try { await T.sendThrough('c1', { text: 'z', key: T.sendKey() }); out.r4 = 'no error'; } catch (e) { out.r4 = String(e.message); }
  out.calls4 = calls.length; out.left4 = T.sending.size;
  // 5. keys differ from message to message
  out.keys = new Set(Array.from({ length: 200 }, () => T.sendKey())).size;
  out.transient = [T.transient({}), T.transient({ status: 503 }), T.transient({ status: 429 }), T.transient({ status: 404 }), T.transient({ status: 400 })];
  console.log(JSON.stringify(out));
})();
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        js = TMP / "run.js"
        js.write_text(NODE, encoding="utf-8")
        r = subprocess.run(["node", str(js), str(ROOT / "web" / "app.js")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        cls.raw = (r.stdout or "") + (r.stderr or "")
        try:
            cls.out = json.loads((r.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            cls.out = {"error": cls.raw[-600:]}

    def test_the_page_code_ran(self):
        self.assertNotIn("error", self.out, self.out.get("error"))

    def test_a_dropped_connection_is_tried_again_with_the_same_key(self):
        o = self.out
        self.assertEqual(o["r1"], {"queued": True, "id": "q1"})
        self.assertEqual(o["calls1"], 3)
        self.assertTrue(o["sameKey1"])

    def test_the_user_sees_it_is_still_being_sent_and_then_not(self):
        o = self.out
        self.assertGreaterEqual(o["shown1"], 2)
        self.assertEqual(o["left1"], 0)
        self.assertTrue(o["hidden1"])
        self.assertEqual((o["calls3"], o["shown3"]), (1, 0))

    def test_a_refusal_is_not_tried_again(self):
        self.assertEqual((self.out["r2"], self.out["calls2"]), (400, 1))

    def test_it_gives_up_after_the_limit_and_hands_the_message_back(self):
        o = self.out
        self.assertIn("fetch", o["r4"])
        self.assertGreater(o["calls4"], 2)
        self.assertLess(o["calls4"], 400)
        self.assertEqual(o["left4"], 0)

    def test_keys_and_what_counts_as_a_dropped_connection(self):
        self.assertEqual(self.out["keys"], 200)
        self.assertEqual(self.out["transient"], [True, True, True, False, False])

    def test_the_composer_only_gets_the_text_back_when_it_is_empty(self):
        src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertTrue(re.search(r"!input\.value\.trim\(\)\) input\.value = text;", src))
        self.assertIn("key: sendKey()", src)


if __name__ == "__main__":
    unittest.main(verbosity=1)
