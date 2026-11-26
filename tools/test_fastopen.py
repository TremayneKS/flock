"""Opening a long chat does not read its whole history again (user requirement).

    python -X utf8 tools/test_fastopen.py

What is held here:
- store.Chat.window gives exactly the turns the old read-everything version gave, for every limit and position, and
  keeps doing so as the file grows, is cut (edit and resend) or is replaced by another file;
- after the first open, a later open reads only the lines it returns (the bytes read are counted);
- a subagent's chip (subagents.chip_summary) says what the full history says, without parsing it again each time;
- a big JSON reply is gzipped for a client that accepts it and plain for one that does not.
Stand-ins only: a throwaway KCH_HOME, never the user's data."""
import builtins
import gzip
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-fastopen-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import agent, config, server, store, subagents  # noqa: E402

config.load()
PROJ = str(TMP / "proj")
Path(PROJ).mkdir(exist_ok=True)
store.add_project(PROJ)


def old_window(path, limit, before=None):
    """The window as it was before this change: read all, split, walk back."""
    try:
        raw = path.read_bytes()
    except OSError:
        return 0, 0, []
    lines = [x for x in raw.split(b"\n") if x.strip()]
    total = len(lines)
    end = total if before is None else max(0, min(int(before), total))
    out, i = [], end - 1
    while i >= 0:
        try:
            m = json.loads(lines[i])
        except ValueError:
            i -= 1
            continue
        out.append(m)
        i -= 1
        if len(out) >= limit and m.get("role") == "user" and not m.get("auto"):
            break
        if len(out) >= limit * 4:
            break
    out.reverse()
    return i + 1, total, out


def fill(chat, turns, steps=7, start=0):
    for t in range(start, start + turns):
        chat.append({"role": "user", "content": "ask %d   with a line separator" % t})
        for k in range(steps):
            chat.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c%d-%d" % (t, k), "name": "Bash"}]})
            chat.append({"role": "tool", "tool_call_id": "c%d-%d" % (t, k), "content": "x" * (50 + 13 * k)})
        chat.append({"role": "assistant", "content": "answer %d" % t})


def new_chat(**meta):
    c = store.Chat(store.new_id())
    c.dir.mkdir(parents=True, exist_ok=True)
    c.save_meta(dict({"id": c.id, "title": "t", "project": PROJ, "created": 1.0, "updated": 1.0}, **meta))
    return c


class Window(unittest.TestCase):
    def same(self, chat):
        for limit in (1, 5, 16, 40, 400):
            for before in (None, 0, 1, 7, 33, 90, 10 ** 6):
                self.assertEqual(store.Chat(chat.id).window(limit, before), old_window(chat.messages_path, limit, before),
                                 "limit %s before %s" % (limit, before))

    def test_same_turns_as_reading_everything(self):
        c = new_chat()
        self.assertEqual(c.window(10), (0, 0, []))
        fill(c, 9)
        self.same(c)

    def test_follows_the_file_as_it_grows(self):
        c = new_chat()
        fill(c, 3)
        self.same(c)
        n = len(store._LINES[c.id]["offs"])
        fill(c, 2, start=3)
        self.same(c)
        self.assertEqual(len(store._LINES[c.id]["offs"]), n + 2 * 16)

    def test_blank_and_broken_lines_are_skipped_as_before(self):
        c = new_chat()
        fill(c, 2)
        with open(c.messages_path, "a", encoding="utf-8") as f:
            f.write("\n\n{not json\n   \n" + json.dumps({"role": "user", "content": "after the mess"}) + "\n")
        self.same(c)

    def test_cut_tail_and_replaced_file_start_over(self):
        c = new_chat()
        fill(c, 6)
        self.same(c)
        c.rewrite(c.messages()[:20])                       # edit and resend: the tail goes
        self.same(c)
        other = "".join(json.dumps({"role": "user" if i % 3 == 0 else "assistant", "content": "other %d " % i + "y" * 300},
                                   ensure_ascii=False) + "\n" for i in range(200))
        c.messages_path.write_text(other, encoding="utf-8")   # another, longer file under the same name
        self.same(c)

    def test_a_later_open_reads_only_what_it_returns(self):
        c = new_chat()
        fill(c, 400, steps=10)                             # 8,800 lines
        size = c.messages_path.stat().st_size
        c.window(40)
        read = [0]
        real_open = builtins.open

        class Counting(io.BufferedReader):
            def read(self, *a):
                data = super().read(*a)
                read[0] += len(data)
                return data

        def counting_open(file, mode="r", *a, **k):
            if str(file) == str(c.messages_path) and "b" in mode:
                return Counting(io.FileIO(file, "r"))
            return real_open(file, mode, *a, **k)

        builtins.open = counting_open
        try:
            start, total, msgs = store.Chat(c.id).window(40)
        finally:
            builtins.open = real_open
        self.assertEqual(total, 8800)
        self.assertGreaterEqual(len(msgs), 40)
        self.assertLess(read[0], size // 20, "read %d of %d bytes" % (read[0], size))


class Chips(unittest.TestCase):
    def full(self, ch):
        msgs = ch.messages()
        return {
            "turns": sum(1 for m in msgs if m.get("role") == "user" and not m.get("auto")),
            "first_user": next((m.get("content") or "" for m in msgs if m.get("role") == "user" and not m.get("auto")), ""),
            "last_asst": next((m.get("content") or "" for m in reversed(msgs)
                               if m.get("role") == "assistant" and (m.get("content") or "").strip()), ""),
            "last_err": next((m.get("content") or "" for m in reversed(msgs) if m.get("role") == "error"), ""),
        }

    def same(self, ch):
        got = subagents.chip_facts(store.Chat(ch.id))
        self.assertEqual({k: got[k] for k in ("turns", "first_user", "last_asst", "last_err")}, self.full(store.Chat(ch.id)))

    def test_facts_match_the_whole_history_as_it_changes(self):
        parent = new_chat()
        sub = new_chat(sub_of=parent.id, sub_name="Helper", endpoint="fake")
        self.same(sub)
        sub.append({"role": "user", "content": "the main chat's first ask", "auto": True})
        sub.append({"role": "user", "content": "do the thing"})
        self.same(sub)
        fill(sub, 3)
        sub.append({"role": "error", "content": "it broke"})
        self.same(sub)
        sub.append({"role": "assistant", "content": "final words"})
        self.same(sub)
        sub.rewrite(sub.messages()[:5])
        self.same(sub)

    def test_summary_without_parsing_the_history_again(self):
        parent = new_chat()
        sub = new_chat(sub_of=parent.id, sub_name="Helper", endpoint="fake")
        sub.append({"role": "user", "content": "count the files in the folder please"})
        fill(sub, 2)
        s1 = subagents.chip_summary(sub.id)
        self.assertEqual(s1["turns"], 3)
        self.assertEqual(s1["description"], "count the files in the folder please")
        self.assertEqual(s1["last_preview"], "answer 1")
        self.assertEqual(s1["status"], "done")
        calls = [0]
        real = store.Chat.messages

        def counted(self):
            calls[0] += 1
            return real(self)

        store.Chat.messages = counted
        try:
            s2 = subagents.chip_summary(sub.id)
        finally:
            store.Chat.messages = real
        self.assertEqual(calls[0], 0, "the chip parsed the whole history again")
        self.assertEqual(s1, s2)


class Gzip(unittest.TestCase):
    def reply(self, value, accept):
        h = server.Handler.__new__(server.Handler)
        h.headers = {"Accept-Encoding": accept} if accept else {}
        h.wfile = io.BytesIO()
        sent = {}
        h.send_response = lambda status: sent.__setitem__("status", status)
        h.send_header = lambda k, v: sent.__setitem__(k, v)
        h.end_headers = lambda: None
        h._json(value)
        return sent, h.wfile.getvalue()

    def test_big_replies_are_packed_only_for_those_who_ask(self):
        big = {"messages": [{"role": "assistant", "content": "word " * 200} for _ in range(60)]}
        sent, body = self.reply(big, "gzip, deflate, br")
        self.assertEqual(sent.get("Content-Encoding"), "gzip")
        self.assertEqual(int(sent["Content-Length"]), len(body))
        self.assertEqual(json.loads(gzip.decompress(body)), big)
        self.assertLess(len(body), len(json.dumps(big)) // 5)
        sent, body = self.reply(big, "")
        self.assertNotIn("Content-Encoding", sent)
        self.assertEqual(json.loads(body), big)
        sent, body = self.reply({"ok": True}, "gzip")
        self.assertNotIn("Content-Encoding", sent)
        self.assertEqual(json.loads(body), {"ok": True})


class History(unittest.TestCase):
    def test_the_page_gets_the_newest_turns_and_where_they_start(self):
        c = new_chat(endpoint="fake")
        fill(c, 30)
        h = agent.history(c.id, 80, None)
        self.assertEqual(h["total"], 480)
        self.assertEqual(h["messages"][0]["role"], "user")
        self.assertEqual(h["messages"][-1]["content"], "answer 29")
        self.assertEqual(h["start"], 480 - len(h["messages"]))
        earlier = agent.history(c.id, 300, h["start"])
        self.assertEqual(earlier["start"] + len(earlier["messages"]), h["start"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
