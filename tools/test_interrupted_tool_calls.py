"""Regression tests for interrupted/late tool calls in harness.agent."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402

from harness import agent  # noqa: E402


class FakeChat:
    def __init__(self, msgs, summary=None):
        self.id = "test-chat-1"
        self._msgs = list(msgs)
        self._summary = summary

    def context_messages(self):
        return self._summary, list(self._msgs)

    def messages(self):
        return list(self._msgs)

    def append(self, m):
        m = dict(m)
        m.setdefault("id", f"m-{len(self._msgs)}")
        self._msgs.append(m)
        return m


def _validate_openai_sequence(api_msgs):
    """Assert every assistant tool_call is immediately followed by matching tool responses, with no orphan tools."""
    i = 0
    while i < len(api_msgs):
        m = api_msgs[i]
        role = m["role"]
        if role == "tool":
            raise AssertionError(f"Stray tool message at index {i}: {m}")
        if role == "assistant" and m.get("tool_calls"):
            need = [tc["id"] for tc in m["tool_calls"]]
            got = []
            j = i + 1
            while j < len(api_msgs) and api_msgs[j]["role"] == "tool":
                got.append(api_msgs[j]["tool_call_id"])
                j += 1
            if set(got) != set(need) or len(got) != len(need):
                raise AssertionError(f"Assistant at {i} expected tool ids {need}, got {got}")
            i = j
        else:
            i += 1


class TestInterruptedToolCalls(unittest.TestCase):
    ep = {"id": "deepseek", "base": "https://api.deepseek.com/v1", "vision": True}
    meta = {"system": "sys"}

    def test_orphaned_tool_calls_followed_by_user(self):
        chat = FakeChat([
            {"role": "user", "content": "why did u stop"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "Bash", "arguments": "{}"},
                {"id": "c2", "name": "Bash", "arguments": "{}"},
            ]},
            {"role": "notice", "content": "Stopped."},
            {"role": "user", "content": "?"},
        ])
        out = agent.api_messages(chat, self.meta, self.ep)
        _validate_openai_sequence(out)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool", "tool", "user"])
        self.assertEqual([out[3]["tool_call_id"], out[4]["tool_call_id"]], ["c1", "c2"])

    def test_late_zombie_tool_results_after_user_are_dropped(self):
        chat = FakeChat([
            {"role": "user", "content": "why did u stop"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "Bash", "arguments": "{}"},
                {"id": "c2", "name": "Bash", "arguments": "{}"},
            ]},
            {"role": "user", "content": "?"},
            {"role": "tool", "tool_call_id": "c1", "content": "late 1"},
            {"role": "tool", "tool_call_id": "c2", "content": "late 2"},
            {"role": "user", "content": "hello"},
        ])
        out = agent.api_messages(chat, self.meta, self.ep)
        _validate_openai_sequence(out)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool", "tool", "user", "user"])

    def test_partial_tool_results_followed_by_user(self):
        chat = FakeChat([
            {"role": "user", "content": "run"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "Bash", "arguments": "{}"},
                {"id": "c2", "name": "Bash", "arguments": "{}"},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok 1"},
            {"role": "user", "content": "stop"},
        ])
        out = agent.api_messages(chat, self.meta, self.ep)
        _validate_openai_sequence(out)
        self.assertEqual(out[3]["content"], "ok 1")
        self.assertIn("Stopped", out[4]["content"])

    def test_claude_parallel_blocks_still_merge(self):
        chat = FakeChat([
            {"role": "user", "content": "do both"},
            {"role": "assistant", "content": "checking", "tool_calls": [{"id": "c1", "name": "Read", "arguments": "{}"}]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "name": "Read", "arguments": "{}"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        ])
        out = agent.api_messages(chat, self.meta, self.ep)
        _validate_openai_sequence(out)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual(len(out[2]["tool_calls"]), 2)

    def test_parallel_tool_image_deferred_until_all_tools_answered(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            img_path = f.name
        try:
            chat = FakeChat([
                {"role": "user", "content": "look"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "name": "View", "arguments": "{}"},
                    {"id": "c2", "name": "Read", "arguments": "{}"},
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": "img", "image": img_path},
                {"role": "tool", "tool_call_id": "c2", "content": "txt"},
            ])
            out = agent.api_messages(chat, self.meta, self.ep)
            _validate_openai_sequence(out)
            self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool", "tool", "user"])
        finally:
            os.unlink(img_path)

    def test_close_open_calls_closes_unanswered_only(self):
        chat = FakeChat([
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "name": "Bash", "arguments": "{}"},
                {"id": "c2", "name": "Bash", "arguments": "{}"},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "done"},
        ])
        agent._close_open_calls(chat, emit_events=False)
        tool_ids = [m["tool_call_id"] for m in chat._msgs if m.get("role") == "tool"]
        self.assertEqual(tool_ids, ["c1", "c2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
