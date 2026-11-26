"""Tests for harness/chat_import.py on small synthetic exports and sessions that copy the real formats.

    python -X utf8 tools/test_chat_import.py

Runs against a throwaway KCH_HOME, never the user's data."""
import base64
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="kch-import-test-"))
os.environ["KCH_HOME"] = str(TMP / "home")
os.environ["KCH_PROFILE"] = str(TMP / "no-profile.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import chat_import as ci, store  # noqa: E402

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def node(nid, parent, children, role, content, **kw):
    msg = None if role is None else {"id": nid, "author": {"role": role, "name": kw.pop("name", None), "metadata": {}},
                                     "create_time": kw.pop("t", 1_700_000_000), "content": content, "status": "finished_successfully",
                                     "recipient": kw.pop("recipient", "all"), "metadata": kw.pop("metadata", {})}
    return {"id": nid, "message": msg, "parent": parent, "children": children}


def gpt_conv(cid="c-1", title="Plotting in Python"):
    t = lambda s: {"content_type": "text", "parts": [s]}           # noqa: E731
    mapping = {
        "root": node("root", None, ["sys"], None, None),
        "sys": node("sys", "root", ["u1"], "system", t(""), metadata={"is_visually_hidden_from_conversation": True}),
        "u1": node("u1", "sys", ["a1"], "user", {"content_type": "multimodal_text", "parts": [
            {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-AbC123", "width": 1, "height": 1},
            "Plot this, café ☕ 日本語"]}, metadata={"attachments": [{"id": "file-AbC123", "name": "dot.png", "mime_type": "image/png"},
                                                               {"id": "file-Doc9", "name": "notes.txt", "mime_type": "text/plain"}]}),
        "a1": node("a1", "u1", ["think"], "assistant", {"content_type": "code", "language": "python", "text": "print('x' * 3)"}, recipient="python"),
        "think": node("think", "a1", ["tool1"], "assistant", {"content_type": "thoughts", "thoughts": [{"summary": "Checking", "content": "the output"}]}),
        "tool1": node("tool1", "think", ["a2"], "tool", {"content_type": "execution_output", "text": "y" * 50_000}, name="python"),
        "a2": node("a2", "tool1", ["u2", "u2b"], "assistant", t("Here it is 【3†source】 done.")),
        "u2": node("u2", "a2", ["a3"], "user", t("first version of my question"), t=1_700_000_100),
        "a3": node("a3", "u2", [], "assistant", t("old answer")),
        "u2b": node("u2b", "a2", ["a3b"], "user", t("edited question"), t=1_700_000_200),
        "a3b": node("a3b", "u2b", ["empty"], "assistant", t("new answer")),
        "empty": node("empty", "a3b", [], "assistant", t("")),
    }
    return {"title": title, "create_time": 1_700_000_000.5, "update_time": 1_700_000_300.1, "mapping": mapping,
            "current_node": "empty", "conversation_id": cid, "id": cid}


def claude_conv(uid="cl-1"):
    root = ci.ROOT_UUID
    return {"uuid": uid, "name": "Résumé help", "summary": "", "created_at": "2025-02-01T10:00:00.000000Z",
            "updated_at": "2025-02-03T11:00:00.000000Z", "account": {"uuid": "x"}, "chat_messages": [
                {"uuid": "m1", "parent_message_uuid": root, "sender": "human", "created_at": "2025-02-01T10:00:00Z", "text": "Fix my CV",
                 "content": [{"type": "text", "text": "Fix my CV"}, {"type": "injected_prompt_block", "injection_source": "date_note"}],
                 "attachments": [{"file_name": "cv.txt", "file_size": 10, "file_type": "text/plain", "extracted_content": "Jane Doe\nEngineer"}],
                 "files": [{"file_name": "photo.jpg"}]},
                {"uuid": "m2", "parent_message_uuid": "m1", "sender": "assistant", "created_at": "2025-02-01T10:00:05Z", "text": "",
                 "content": [{"type": "thinking", "thinking": "Let me look."},
                             {"type": "tool_use", "id": "tu1", "name": "web_search", "input": {"query": "cv tips"}},
                             {"type": "tool_result", "tool_use_id": "tu1", "name": "web_search", "content": [{"type": "text", "text": "tip 1"}], "is_error": False},
                             {"type": "text", "text": "Here is a better CV."}]},
                {"uuid": "m3", "parent_message_uuid": "m2", "sender": "human", "created_at": "2025-02-02T10:00:00Z", "text": "retry A",
                 "content": [{"type": "text", "text": "retry A"}]},
                {"uuid": "m3b", "parent_message_uuid": "m2", "sender": "human", "created_at": "2025-02-03T10:00:00Z", "text": "retry B",
                 "content": [{"type": "text", "text": "retry B"}]},
                {"uuid": "m4", "parent_message_uuid": "m3b", "sender": "assistant", "created_at": "2025-02-03T10:00:09Z", "text": "B answer",
                 "content": [{"type": "text", "text": "B answer"}]},
            ]}


def cc_session(path):
    lines = [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": "s"},
        {"parentUuid": None, "isSidechain": False, "type": "user", "uuid": "u1", "timestamp": "2026-10-01T09:00:00Z", "cwd": "C:/proj",
         "message": {"role": "user", "content": "<system-reminder>ignore me</system-reminder>Build a site"}},
        {"parentUuid": "u1", "isSidechain": False, "type": "attachment", "uuid": "att1", "attachment": {"type": "date"}},
        {"parentUuid": "att1", "isSidechain": False, "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "plan"}]},
         "type": "assistant", "uuid": "a1", "timestamp": "2026-10-01T09:00:02Z"},
        {"parentUuid": "a1", "isSidechain": False, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "C:/proj/a.txt", "old_string": "a\nb", "new_string": "a\nc"}}]},
         "type": "assistant", "uuid": "a2", "timestamp": "2026-10-01T09:00:03Z"},
        {"parentUuid": "a2", "isSidechain": False, "type": "user", "uuid": "r1", "timestamp": "2026-10-01T09:00:04Z", "toolUseResult": {},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "z" * 80_000}]}},
        {"parentUuid": "r1", "isSidechain": True, "type": "user", "uuid": "side", "message": {"role": "user", "content": "sidechain"}},
        {"type": "api-request-blob", "sessionId": "s", "hash": "h", "message": {"content": "x" * 1000}},
        "{not json",
        {"parentUuid": "r1", "isSidechain": False, "message": {"role": "assistant", "content": [{"type": "text", "text": "Done, the site is up."}]},
         "type": "assistant", "uuid": "a3", "timestamp": "2026-10-01T09:00:05Z"},
        {"parentUuid": "a3", "isSidechain": False, "type": "user", "uuid": "u2", "isMeta": True, "message": {"role": "user", "content": "meta"}},
        {"parentUuid": None, "logicalParentUuid": "a3", "isSidechain": False, "type": "system", "subtype": "compact_boundary", "uuid": "cb"},
        {"parentUuid": "cb", "isSidechain": False, "type": "user", "uuid": "u3", "timestamp": "2026-10-01T10:00:00Z", "message": {"role": "user", "content": [
            {"type": "text", "text": "<command-name>/review</command-name><command-args>now</command-args>"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}}]}},
        # a rewound branch: a reply to u1 that the user went back from
        {"parentUuid": "u1", "isSidechain": False, "message": {"role": "assistant", "content": [{"type": "text", "text": "abandoned"}]},
         "type": "assistant", "uuid": "old", "timestamp": "2026-10-01T09:00:01Z"},
        {"parentUuid": "u3", "isSidechain": False, "message": {"role": "assistant", "content": [{"type": "text", "text": "Reviewed."}]},
         "type": "assistant", "uuid": "a4", "timestamp": "2026-10-01T10:00:05Z"},
        {"type": "custom-title", "customTitle": "Site build", "sessionId": "s"},
    ]
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def codex_session(path):
    lines = [
        {"timestamp": "2026-09-01T08:00:00Z", "type": "session_meta", "payload": {"id": "codex-123", "cwd": "C:/work", "timestamp": "2026-09-01T08:00:00Z"}},
        {"timestamp": "2026-09-01T08:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "<permissions instructions>x</permissions instructions>"}]}},
        {"timestamp": "2026-09-01T08:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>C:/work</cwd>\n</environment_context>"}]}},
        {"timestamp": "2026-09-01T08:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "List the files"}]}},
        {"timestamp": "2026-09-01T08:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "List the files"}},
        {"timestamp": "2026-09-01T08:00:02Z", "type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "**Listing**"}], "encrypted_content": "zzz"}},
        {"timestamp": "2026-09-01T08:00:02Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["ls"]}), "call_id": "c1"}},
        {"timestamp": "2026-09-01T08:00:03Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": json.dumps({"output": "a.txt\nb.txt", "metadata": {"exit_code": 0}})}},
        {"timestamp": "2026-09-01T08:00:03Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "echo hi", "call_id": "c2"}},
        {"timestamp": "2026-09-01T08:00:04Z", "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "hi"}]}},
        {"timestamp": "2026-09-01T08:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "Two files: a.txt and b.txt."}]}},
        {"timestamp": "2026-09-01T08:00:06Z", "type": "compacted", "payload": {"message": ""}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json at all\n", encoding="utf-8")


def wait(job, secs=60):
    t = time.time()
    while job.state == "running" and time.time() - t < secs:
        time.sleep(0.05)
    return job.public()


class Parsers(unittest.TestCase):
    def test_chatgpt_branch_tools_assets(self):
        zpath = TMP / "gpt.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("conversations.json", json.dumps([gpt_conv()]))
            z.writestr("file-AbC123-dot.png", PNG)
            z.writestr("file-Doc9-notes.txt", "some notes")
            z.writestr("chat.html", "<html></html>")
        ex = ci.Export(zpath)
        self.assertEqual(ex.kind, "chatgpt")
        c = ci.chatgpt(next(ex.conversations()), ex)
        roles = [m["role"] for m in c["turns"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])
        u1 = c["turns"][0]["parts"]
        self.assertIn("café ☕ 日本語", u1[0]["text"])
        self.assertEqual([p["kind"] for p in u1], ["text", "image", "file"])      # the image is not listed twice
        a1 = c["turns"][1]["parts"]
        tool = next(p for p in a1 if p["kind"] == "tool")
        self.assertEqual(tool["name"], "python")
        self.assertLessEqual(len(tool["model_output"]), ci.MODEL_OUT + 80)
        self.assertTrue(any(p["kind"] == "thinking" for p in a1))
        self.assertEqual(a1[-1]["text"], "Here it is  done.")                    # citation marks removed
        self.assertEqual(c["turns"][2]["parts"][0]["text"], "edited question")   # the branch last seen
        self.assertTrue(c["notes"])
        ex.close()

    def test_claude_ai_tree_and_blocks(self):
        c = ci.claude_ai(claude_conv())
        self.assertEqual([m["role"] for m in c["turns"]], ["user", "assistant", "user", "assistant"])
        u = c["turns"][0]["parts"]
        self.assertEqual(u[0]["text"], "Fix my CV")
        self.assertEqual(u[1]["text"], "Jane Doe\nEngineer")
        self.assertTrue(u[2].get("missing"))
        kinds = [p["kind"] for p in c["turns"][1]["parts"]]
        self.assertEqual(kinds, ["thinking", "tool", "text"])
        self.assertEqual(c["turns"][1]["parts"][1]["output"], "tip 1")
        self.assertEqual(c["turns"][2]["parts"][0]["text"], "retry B")
        self.assertIn("1 message", c["notes"][0])

    def test_claude_ai_zip_of_zips(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as z:
            z.writestr("conversations.json", json.dumps([claude_conv("a"), claude_conv("b")]))
        outer = TMP / "claude-export.zip"
        with zipfile.ZipFile(outer, "w") as z:
            z.writestr("manifest.json", "{}")
            z.writestr("conversations-000.zip", inner.getvalue())
            z.writestr("../evil.txt", "path traversal is never extracted")
        ex = ci.Export(outer)
        self.assertEqual(ex.kind, "claude")
        self.assertEqual(len(list(ex.conversations())), 2)
        ex.close()
        self.assertFalse(ex.inner.exists())

    def test_stream_parser(self):
        items = [{"i": i, "s": "é" * (i * 97) + "x" * (i * 3000)} for i in range(300)]
        text = json.dumps(items)
        got = list(ci.iter_array(io.StringIO(text)))
        self.assertEqual(got, items)
        with self.assertRaises(ValueError):
            list(ci.iter_array(io.StringIO(text[:-500])))
        self.assertEqual(list(ci.iter_array(io.StringIO("[]"))), [])

    def test_claude_code(self):
        p = TMP / "cc" / "11111111-2222.jsonl"
        p.parent.mkdir(exist_ok=True)
        cc_session(p)
        c = ci.claude_code(p)
        self.assertEqual(c["title"], "Site build")
        self.assertEqual(c["cwd"], "C:/proj")
        roles = [m["role"] for m in c["turns"]]
        self.assertEqual(roles, ["user", "assistant", "notice", "user", "assistant"])
        self.assertEqual(c["turns"][0]["parts"][0]["text"], "Build a site")
        a = c["turns"][1]["parts"]
        self.assertEqual([x["kind"] for x in a], ["thinking", "tool", "text"])
        self.assertIn("-b", a[1]["diff"])
        self.assertIn("+c", a[1]["diff"])
        self.assertLess(len(a[1]["output"]), 31_000)
        self.assertNotIn("abandoned", json.dumps(c["turns"], default=str))
        self.assertNotIn("sidechain", json.dumps(c["turns"], default=str))
        u3 = c["turns"][3]["parts"]
        self.assertEqual(u3[0]["text"], "/review now")
        self.assertEqual(u3[1]["kind"], "image")
        self.assertTrue(c["notes"])

    def test_codex(self):
        p = TMP / "rollout-2026-09-01T08-00-00-0199aaaa-bbbb-cccc-dddd-eeeeffff0000.jsonl"
        codex_session(p)
        c = ci.codex(p)
        self.assertEqual(c["sid"], "0199aaaa-bbbb-cccc-dddd-eeeeffff0000")
        roles = [m["role"] for m in c["turns"]]
        self.assertEqual(roles, ["user", "assistant", "notice"])
        self.assertEqual(c["turns"][0]["parts"][0]["text"], "List the files")
        a = c["turns"][1]["parts"]
        self.assertEqual([x["kind"] for x in a], ["thinking", "tool", "tool", "text"])
        self.assertEqual(a[1]["output"], "a.txt\nb.txt")
        self.assertEqual(a[2]["output"], "hi")


class Saving(unittest.TestCase):
    def test_import_export_job_twice(self):
        zpath = TMP / "gpt2.zip"
        convs = [gpt_conv(f"c-{i}", f"Chat {i}") for i in range(5)] + [{"title": "empty", "mapping": {}, "id": "c-empty"}]
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("conversations-000.json", json.dumps(convs[:3]))
            z.writestr("conversations-001.json", json.dumps(convs[3:]))
            z.writestr("file-AbC123-dot.png", PNG)
        s = wait(ci.scan_export(zpath))
        self.assertEqual(s["state"], "done", s)
        self.assertEqual((s["result"]["count"], s["result"]["already"], s["result"]["kind"]), (6, 0, "chatgpt"))
        r = wait(ci.import_export(zpath))
        self.assertEqual(r["state"], "done", r)
        self.assertEqual((r["result"]["imported"], r["result"]["skipped"], r["result"]["failed"]), (5, 1, 0), r)
        chats = [c for c in store.list_chats() if c.get("source") == "ChatGPT"]
        self.assertEqual(len(chats), 5)
        self.assertLess(chats[0]["updated"], 1_800_000_000)                        # original dates
        msgs = store.Chat(chats[0]["id"]).messages()
        self.assertEqual(msgs[0]["role"], "notice")
        user = next(m for m in msgs if m["role"] == "user")
        img = next(p for p in user["parts"] if p["kind"] == "image")
        self.assertTrue(Path(img["path"]).is_file())
        self.assertTrue(Path(img["path"]).resolve().is_relative_to(store.Chat(chats[0]["id"]).dir.resolve()))
        self.assertIn("imported from ChatGPT", user["model_content"])
        r2 = wait(ci.import_export(zpath))
        self.assertEqual((r2["result"]["imported"], r2["result"]["skipped"]), (0, 6))
        # what the model would get when the chat is continued
        from harness import agent, config
        chat = store.Chat(chats[0]["id"])
        api = agent.api_messages(chat, {**chat.meta(), "system": "sys"}, config.endpoint(None) if config.get()["endpoints"] else {})
        a = next(m for m in api if m["role"] == "assistant")
        self.assertIn("[python]", a["content"])
        self.assertLess(len(json.dumps(api)), 20_000)

    def test_long_chat_starts_from_summary(self):
        T = ci.Turns()
        for i in range(60):
            T.user([{"kind": "text", "text": f"question {i}"}])
            T.add({"kind": "text", "text": "answer " + "w" * 8000})
        cid, n = ci.save_chat({"source": "codex", "sid": "long", "title": "", "created": None, "updated": None, "turns": T.msgs, "notes": []},
                              str(TMP / "proj"), "")
        chat = store.Chat(cid)
        summary, ctx = chat.context_messages()
        self.assertIsNotNone(summary)
        self.assertEqual(ctx, [])
        self.assertIn("question 59", summary["summary"])
        self.assertEqual(n, 120)
        self.assertIn("<earlier_in_this_chat>", ci.prior_transcript(chat))

    def test_local_paths_fenced(self):
        with self.assertRaises(PermissionError):
            ci.import_local("claude-code", [str(TMP / "x.jsonl")])


# ------------------------------------------------------------------------------------------------ Claude Code structure
P_ID = "aaaaaaaa-1111-4111-8111-000000000001"      # a session ...
F_ID = "bbbbbbbb-2222-4222-8222-000000000002"      # ... forked from it (its transcript starts with a copy of P)
S_ID = "cccccccc-3333-4333-8333-000000000003"      # a side session P started (no copy)
T_ID = "dddddddd-4444-4444-8444-000000000004"      # run in a terminal elsewhere: the desktop app does not know it
G_ID = "eeeeeeee-5555-4555-8555-000000000005"      # a later fork of P (appears after the first import)
U_ID = "ffffffff-6666-4666-8666-000000000006"      # a terminal session started after the first import


def rec(kind, uid, parent, sid, text=None, t="2026-10-01T09:00:00Z", content=None, cwd="C:/work/site"):
    msg = {"role": kind, "content": content if content is not None else (text if kind == "user" else [{"type": "text", "text": text}])}
    return {"parentUuid": parent, "isSidechain": False, "type": kind, "uuid": uid, "timestamp": t, "sessionId": sid, "cwd": cwd,
            "message": msg}


def write_lines(path, lines, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        f.write("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))      # compact, as Claude Code writes


def cc_family(home):
    """Three desktop sessions (P, its fork F, its side session S), one terminal session T, and P's subagent run."""
    proj = home / ".claude" / "projects" / "C--work-site"
    agent_call = {"type": "tool_use", "id": "toolu_agent1", "name": "Agent", "input": {"description": "Find the bug", "prompt": "look"}}
    p_lines = [
        rec("user", "p-u1", None, P_ID, "Make a site", "2026-10-01T09:00:00Z"),
        rec("assistant", "p-a1", "p-u1", P_ID, "Made it.", "2026-10-01T09:00:05Z"),
        rec("user", "p-u2", "p-a1", P_ID, "Now fix the menu", "2026-10-01T09:01:00Z"),
        rec("assistant", "p-a2", "p-u2", P_ID, content=[agent_call], t="2026-10-01T09:01:05Z"),
        rec("user", "p-r2", "p-a2", P_ID, content=[{"type": "tool_result", "tool_use_id": "toolu_agent1", "content": "found it"}],
            t="2026-10-01T09:02:00Z"),
        rec("assistant", "p-a3", "p-r2", P_ID, "Menu fixed.", "2026-10-01T09:02:05Z"),
        {"type": "custom-title", "customTitle": "title in the transcript", "sessionId": P_ID},
    ]
    write_lines(proj / f"{P_ID}.jsonl", p_lines)
    sub = proj / P_ID / "subagents"
    write_lines(sub / "agent-a1.jsonl", [
        {**rec("user", "s-u1", None, P_ID, "look", "2026-10-01T09:01:06Z"), "isSidechain": True},
        {**rec("assistant", "s-a1", "s-u1", P_ID, content=[{"type": "tool_use", "id": "t9", "name": "Grep", "input": {"pattern": "menu"}}],
               t="2026-10-01T09:01:07Z"), "isSidechain": True},
        {**rec("user", "s-r1", "s-a1", P_ID, content=[{"type": "tool_result", "tool_use_id": "t9", "content": "x" * 9000}],
               t="2026-10-01T09:01:08Z"), "isSidechain": True},
        {**rec("assistant", "s-a2", "s-r1", P_ID, "found it", "2026-10-01T09:01:09Z"), "isSidechain": True},
    ])
    (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": "Find the bug",
                                                         "toolUseId": "toolu_agent1"}), encoding="utf-8")
    # the fork: P's first turn copied (P's id on those lines), then its own
    write_lines(proj / f"{F_ID}.jsonl", p_lines[:2] + [
        rec("user", "f-u1", "p-a1", F_ID, "Try a dark theme instead", "2026-10-02T10:00:00Z"),
        rec("assistant", "f-a1", "f-u1", F_ID, "Dark theme done.", "2026-10-02T10:00:05Z"),
    ])
    write_lines(proj / f"{S_ID}.jsonl", [
        rec("user", "x-u1", None, S_ID, "Check the links", "2026-10-03T10:00:00Z"),
        rec("assistant", "x-a1", "x-u1", S_ID, "All fine.", "2026-10-03T10:00:05Z"),
    ])
    write_lines(home / ".claude" / "projects" / "C--work-tool" / f"{T_ID}.jsonl", [
        rec("user", "t-u1", None, T_ID, "Write a script", "2026-09-20T10:00:00Z", cwd="C:/work/tool"),
        rec("assistant", "t-a1", "t-u1", T_ID, "Done.", "2026-09-20T10:00:05Z", cwd="C:/work/tool"),
    ])
    desk = home / "desk" / "claude-code-sessions" / "acct" / "org"
    desk.mkdir(parents=True, exist_ok=True)

    def ds(local, cli, title, **kw):
        (desk / f"local_{local}.json").write_text(json.dumps({"sessionId": f"local_{local}", "cliSessionId": cli, "title": title,
                                                              "cwd": "C:/work/site", "createdAt": 1_790_000_000_000,
                                                              "lastActivityAt": 1_790_000_100_000, **kw}), encoding="utf-8")
    ds("p", P_ID, "Site build")
    ds("f", F_ID, "Site build (fork)", forkedFromSessionId="local_p", spawnedFrom={"sessionId": "local_p", "userFork": True, "sideSession": True})
    ds("s", S_ID, "Link check", spawnedFrom={"sessionId": "local_p", "sideSession": True})
    return proj


class Structure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = TMP / "cchome"
        cls.proj = cc_family(cls.home)
        ci.HOME = cls.home
        ci.DESKTOP_DIRS = [cls.home / "desk" / "claude-code-sessions"]

    def test_1_fork_prefix_left_out(self):
        c = ci.claude_code(self.proj / f"{F_ID}.jsonl")
        self.assertEqual(c["fork"]["from"], P_ID)
        self.assertEqual(c["fork"]["after"], "Make a site")
        self.assertEqual([m["role"] for m in c["turns"]], ["user", "assistant"])
        self.assertEqual(c["turns"][0]["parts"][0]["text"], "Try a dark theme instead")
        p = ci.claude_code(self.proj / f"{P_ID}.jsonl")
        self.assertIsNone(p["fork"])
        agent = next(x for m in p["turns"] for x in m.get("parts") or [] if x["kind"] == "tool")
        self.assertEqual(agent["subagents"][0]["id"], "agent-a1")

    def test_2_catalog_families(self):
        cat = ci.cc_catalog()
        self.assertEqual(set(cat), {P_ID, F_ID, S_ID, T_ID})
        self.assertEqual((cat[F_ID]["parent"], cat[F_ID]["kind"], cat[F_ID]["title"]), (P_ID, "fork", "Site build (fork)"))
        self.assertEqual((cat[S_ID]["parent"], cat[S_ID]["kind"]), (P_ID, "side"))
        self.assertEqual(cat[S_ID]["root"], P_ID)
        self.assertEqual(cat[P_ID]["group"][:2], ("claude-code:family:" + P_ID, "Site build"))
        self.assertEqual(cat[T_ID]["group"][:2], ci.OTHER)            # the desktop app does not know it
        rows = ci.local_sessions("claude-code")
        at = [r["sid"] for r in rows].index(P_ID)
        self.assertEqual([r["sid"] for r in rows][at:at + 3], [P_ID, F_ID, S_ID])   # the family together, root first
        self.assertEqual([r["depth"] for r in rows][at:at + 3], [0, 1, 1])
        # without the desktop app's list: the fork is still found from its copied start, sessions grouped by folder
        ci.DESKTOP_DIRS = []
        try:
            cat = ci.cc_catalog()
            self.assertEqual((cat[F_ID]["parent"], cat[F_ID]["kind"]), (P_ID, "fork"))
            self.assertIsNone(cat[S_ID]["parent"])
            self.assertEqual(cat[T_ID]["group"][1], "tool")
        finally:
            ci.DESKTOP_DIRS = [self.home / "desk" / "claude-code-sessions"]

    def test_3_import_all_update_and_dedup(self):
        from harness import groups
        r = wait(ci.import_local("claude-code", everything=True), 60)
        self.assertEqual(r["state"], "done", r)
        self.assertEqual((r["result"]["imported"], r["result"]["failed"], r["result"]["nested"]), (4, 0, 2), r)
        idx = ci._index()
        cid = {k: idx[f"claude-code:{k}"] for k in (P_ID, F_ID, S_ID, T_ID)}
        meta = {k: store.Chat(v).meta() for k, v in cid.items()}
        self.assertEqual(meta[P_ID]["title"], "Site build")
        self.assertEqual(meta[F_ID]["parent"], cid[P_ID])
        self.assertEqual(meta[S_ID]["parent"], cid[P_ID])
        self.assertEqual(meta[F_ID]["branch"]["kind"], "fork")
        self.assertEqual(meta[S_ID]["branch"]["kind"], "side")
        fam = groups.get(meta[P_ID]["group"])
        self.assertEqual(fam["name"], "Site build")
        self.assertEqual(meta[F_ID]["group"], fam["id"])
        self.assertEqual(groups.get(meta[T_ID]["group"])["name"], ci.OTHER[1])
        listed = {c["id"]: c for c in store.list_chats()}
        self.assertEqual(listed[cid[F_ID]]["parent"], cid[P_ID])
        fmsgs = store.Chat(cid[F_ID]).messages()
        self.assertEqual([m["role"] for m in fmsgs], ["notice", "compaction", "user", "assistant"])
        self.assertEqual(fmsgs[1]["why"], "fork")
        self.assertIn("Make a site", fmsgs[1]["summary"])
        summary, ctx = store.Chat(cid[F_ID]).context_messages()      # carrying it on: the model gets what came before
        self.assertEqual(summary["why"], "fork")
        self.assertEqual(len(ctx), 2)
        # P's subagent run is kept beside it and opened on demand
        pm = store.Chat(cid[P_ID]).messages()
        part = next(x for m in pm for x in m.get("parts") or [] if x.get("subagents"))
        run = part["subagents"][0]
        self.assertEqual((run["type"], run["messages"]), ("general-purpose", 2))
        sub = ci.subagent_messages(cid[P_ID], run["id"])
        self.assertEqual(sub[-1]["content"], "found it")
        self.assertLess(len(sub[0]["parts"][0]["output"]) if sub[0]["role"] == "assistant" else 0, ci.SUB_SHOW + 100)
        # a second import of everything adds nothing
        r2 = wait(ci.import_local("claude-code", everything=True), 60)
        self.assertEqual((r2["result"]["imported"], r2["result"]["updated"], r2["result"]["skipped"]), (0, 0, 4), r2)
        # the user renames a chat and moves another: an import keeps that
        store.Chat(cid[T_ID]).update_meta(title="My script")
        groups.move_chats([cid[S_ID]], None)
        # the fork went on: its new turns go on the end, nothing repeated, the earlier messages kept as they were
        before_ids = [m["id"] for m in fmsgs]
        write_lines(self.proj / f"{F_ID}.jsonl", [
            rec("user", "f-u2", "f-a1", F_ID, "And a bigger font", "2026-10-04T10:00:00Z"),
            rec("assistant", "f-a2", "f-u2", F_ID, "Bigger now.", "2026-10-04T10:00:05Z")], "a")
        self.assertEqual(next(x for x in ci.local_sessions("claude-code") if x["sid"] == F_ID)["state"], "grown")
        r3 = wait(ci.import_local("claude-code", everything=True), 60)
        self.assertEqual((r3["result"]["imported"], r3["result"]["updated"], r3["result"]["messages"]), (0, 1, 2), r3)
        after = store.Chat(cid[F_ID]).messages()
        self.assertEqual([m["content"] for m in after if m["role"] == "user"], ["Try a dark theme instead", "And a bigger font"])
        self.assertEqual([m["id"] for m in after[:3]], before_ids[:3])
        self.assertEqual(store.Chat(cid[T_ID]).meta()["title"], "My script")
        self.assertIsNone(store.Chat(cid[S_ID]).meta().get("group"))
        # something said here, then the session goes on again: its new turns come after, with a line saying so
        store.Chat(cid[F_ID]).append({"role": "user", "content": "carried on here"})
        write_lines(self.proj / f"{F_ID}.jsonl", [
            rec("user", "f-u3", "f-a2", F_ID, "Back in Claude Code", "2026-10-05T10:00:00Z"),
            rec("assistant", "f-a3", "f-u3", F_ID, "Hello again.", "2026-10-05T10:00:05Z")], "a")
        wait(ci.import_local("claude-code", everything=True), 60)
        tail = [m.get("content") for m in store.Chat(cid[F_ID]).messages()[-4:]]
        self.assertEqual(tail[0], "carried on here")
        self.assertIn("later message", tail[1])
        self.assertEqual(tail[2:], ["Back in Claude Code", "Hello again."])

    def test_4_update_imported_chats(self):
        """The "Update imported chats" button: grown sessions get their new messages, a new fork lands under its parent
        in its family's group, a session started after the first import is added, unchanged ones are skipped, a chat
        deleted here is not brought back, and a group's or a chat's update touches only those."""
        from harness import groups
        wait(ci.import_local("claude-code", everything=True), 60)          # whatever earlier tests left, brought level
        idx = ci._index()
        cid = {k: idx[f"claude-code:{k}"] for k in (P_ID, F_ID, S_ID, T_ID)}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 5))
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
        # T went on; P got a new fork (its start copied from P); a new terminal session U started after the import
        write_lines(self.home / ".claude" / "projects" / "C--work-tool" / f"{T_ID}.jsonl", [
            rec("user", "t-u2", "t-a1", T_ID, "Add a flag", now, cwd="C:/work/tool"),
            rec("assistant", "t-a2", "t-u2", T_ID, "Added --dry-run.", now, cwd="C:/work/tool")], "a")
        p_lines = [json.loads(x) for x in (self.proj / f"{P_ID}.jsonl").read_text(encoding="utf-8").splitlines()]
        write_lines(self.proj / f"{G_ID}.jsonl", p_lines[:2] + [
            rec("user", "g-u1", "p-a1", G_ID, "Try a serif font", now),
            rec("assistant", "g-a1", "g-u1", G_ID, "Serif it is.", now)])
        desk = self.home / "desk" / "claude-code-sessions" / "acct" / "org"
        (desk / "local_g.json").write_text(json.dumps({"sessionId": "local_g", "cliSessionId": G_ID, "title": "Serif try",
                                                       "cwd": "C:/work/site", "createdAt": int(time.time() * 1000),
                                                       "lastActivityAt": int(time.time() * 1000), "forkedFromSessionId": "local_p",
                                                       "spawnedFrom": {"sessionId": "local_p", "userFork": True}}), encoding="utf-8")
        write_lines(self.home / ".claude" / "projects" / "C--work-other" / f"{U_ID}.jsonl", [
            rec("user", "u-u1", None, U_ID, "A new terminal job", later, cwd="C:/work/other"),
            rec("assistant", "u-a1", "u-u1", U_ID, "On it.", later, cwd="C:/work/other")])
        r = wait(ci.update_imports(), 60)
        self.assertEqual(r["state"], "done", r)
        res = r["result"]
        self.assertEqual((res["imported"], res["updated"], res["failed"], res["continued"]), (2, 1, 0, 0), res)
        self.assertEqual(res["skipped"], 3, res)                           # P, F, S unchanged
        self.assertEqual(res["sources"], ["claude-code"])
        idx = ci._index()
        g, u = idx[f"claude-code:{G_ID}"], idx[f"claude-code:{U_ID}"]
        gm, pm = store.Chat(g).meta(), store.Chat(cid[P_ID]).meta()
        self.assertEqual((gm["parent"], gm["group"], gm["title"]), (cid[P_ID], pm["group"], "Serif try"))
        self.assertEqual(gm["project"], pm["project"])                     # where the session it came from is
        self.assertEqual(gm["branch"]["kind"], "fork")
        self.assertEqual(groups.get(store.Chat(u).meta()["group"])["name"], ci.OTHER[1])
        how = {c["id"]: c["how"] for c in res["changed"]}
        self.assertEqual(how, {cid[T_ID]: "grown", g: "new", u: "new"})
        self.assertTrue(all(c.get("title") and c.get("project") for c in res["changed"]))
        self.assertEqual([m["content"] for m in store.Chat(cid[T_ID]).messages() if m["role"] == "user"], ["Write a script", "Add a flag"])
        # a second click: nothing to do
        r = wait(ci.update_imports(), 60)
        self.assertEqual((r["result"]["imported"], r["result"]["updated"], r["result"]["skipped"]), (0, 0, 6), r)
        # deleted here: not brought back, even when its session goes on
        store.Chat(u).update_meta(deleted=True)
        write_lines(self.home / ".claude" / "projects" / "C--work-other" / f"{U_ID}.jsonl",
                    [rec("user", "u-u2", "u-a1", U_ID, "more", later, cwd="C:/work/other")], "a")
        r = wait(ci.update_imports(), 60)
        self.assertEqual((r["result"]["imported"], r["result"]["updated"]), (0, 0), r)
        # one group, then one chat: only those
        write_lines(self.proj / f"{P_ID}.jsonl", [rec("user", "p-u3", "p-a3", P_ID, "Deploy it", later),
                                                  rec("assistant", "p-a4", "p-u3", P_ID, "Deployed.", later)], "a")
        write_lines(self.home / ".claude" / "projects" / "C--work-tool" / f"{T_ID}.jsonl",
                    [rec("user", "t-u3", "t-a2", T_ID, "And tests", later, cwd="C:/work/tool")], "a")
        r = wait(ci.update_imports("group", pm["group"]), 60)
        self.assertEqual(({c["id"] for c in r["result"]["changed"]}, r["result"]["imported"]), ({cid[P_ID]}, 0), r)
        r = wait(ci.update_imports("chat", cid[T_ID]), 60)
        self.assertEqual([c["id"] for c in r["result"]["changed"]], [cid[T_ID]], r)
        # a chat with a turn running is left for next time
        write_lines(self.proj / f"{S_ID}.jsonl", [rec("user", "x-u2", "x-a1", S_ID, "Again", later)], "a")
        real = ci._busy
        ci._busy = lambda c: c == cid[S_ID]
        try:
            r = wait(ci.update_imports(), 60)
        finally:
            ci._busy = real
        self.assertEqual((r["result"]["busy"], r["result"]["updated"]), (1, 0), r)
        self.assertEqual(wait(ci.update_imports("chat", cid[S_ID]), 60)["result"]["updated"], 1)
        st = ci.update_status()
        self.assertEqual((st["auto"], st["job"]["state"]), ("off", "done"))
        self.assertGreaterEqual(st["imported"], 5)

    def test_5_codex_update(self):
        p = self.home / ".codex" / "sessions" / "2026" / "09" / "01" / "rollout-2026-09-01T08-00-00-0199aaaa-bbbb-cccc-dddd-eeeeffff0001.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        codex_session(p)
        r = wait(ci.import_local("codex", [str(p)]), 60)
        self.assertEqual(r["result"]["imported"], 1, r)
        cid = ci._index()["codex:0199aaaa-bbbb-cccc-dddd-eeeeffff0001"]
        with open(p, "a", encoding="utf-8") as f:
            for x in [{"timestamp": "2026-09-02T08:00:00Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "And the hidden ones"}]}},
                      {"timestamp": "2026-09-02T08:00:02Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ".env and .git"}]}}]:
                f.write(json.dumps(x) + "\n")
        r = wait(ci.update_imports(), 60)
        self.assertIn("codex", r["result"]["sources"])
        self.assertIn(cid, [c["id"] for c in r["result"]["changed"]], r)
        users = [m["content"] for m in store.Chat(cid).messages() if m["role"] == "user"]
        self.assertEqual(users, ["List the files", "And the hidden ones"])


class ContinuedHere(unittest.TestCase):
    """The rule for a chat that was imported, then carried on in this app, while its session also went on."""

    def conv(self, *pairs, grown=""):
        T = ci.Turns()
        for i, (q, a) in enumerate(pairs):
            T.user([{"kind": "text", "text": q}], 1_790_000_000 + i * 10)
            T.add({"kind": "text", "text": a + (grown if i == len(pairs) - 1 else "")}, 1_790_000_005 + i * 10)
        return {"source": "claude-code", "sid": "cont-1", "title": "Cont", "created": None, "updated": None, "turns": T.msgs,
                "notes": [], "size": 1}

    def test_grown_then_continued(self):
        c1 = self.conv(("q1", "a1"))
        cid, _ = ci.save_chat(c1, str(TMP / "proj"), "")
        chat = store.Chat(cid)
        a1_id = next(m["id"] for m in chat.messages() if m.get("content") == "a1")
        chat.append({"role": "user", "content": "here 1"})
        chat.append({"role": "assistant", "content": "reply 1"})
        c2 = self.conv(("q1", "a1"), ("q2", "a2"))
        c2["turns"][1]["parts"][0]["text"] = "a1 grown"                  # the imported last message grew there
        added, how = ci.update_chat(cid, c2)
        self.assertEqual((how, added), ("continued", 2))
        msgs = store.Chat(cid).messages()
        body = [(m["role"], m.get("content")) for m in msgs]
        self.assertEqual(body[1:5], [("user", "q1"), ("assistant", "a1 grown"), ("user", "here 1"), ("assistant", "reply 1")])
        self.assertEqual(body[5][0], "notice")
        self.assertIn("2 later messages", body[5][1])
        self.assertEqual(body[6:], [("user", "q2"), ("assistant", "a2")])
        self.assertEqual(msgs[2]["id"], a1_id)                             # redone where it was, same id
        self.assertTrue(store.Chat(cid).meta()["imported"]["continued"])
        # nothing new: nothing changes
        self.assertEqual(ci.update_chat(cid, c2), (0, "same"))
        self.assertEqual(len(store.Chat(cid).messages()), len(msgs))
        # the session goes on again, nothing said here since: appended with no second line
        c3 = self.conv(("q1", "a1"), ("q2", "a2"), ("q3", "a3"))
        c3["turns"][1]["parts"][0]["text"] = "a1 grown"
        self.assertEqual(ci.update_chat(cid, c3), (2, "grown"))
        body = [(m["role"], m.get("content")) for m in store.Chat(cid).messages()]
        self.assertEqual(body[-4:], [("user", "q2"), ("assistant", "a2"), ("user", "q3"), ("assistant", "a3")])
        self.assertEqual(sum(1 for r, _ in body if r == "notice"), 2)
        # the model gets the chat in that order: what was said here before the session's later messages
        _, ctx = store.Chat(cid).context_messages()
        said = [m.get("content") for m in ctx if m["role"] in ("user", "assistant")]
        self.assertLess(said.index("reply 1"), said.index("q2"))
        # rewound there: the imported part is rebuilt, what was said here is kept after it, once
        c4 = self.conv(("q1", "a1"), ("other question", "other answer"))
        added, how = ci.update_chat(cid, c4)
        self.assertEqual(how, "rebuilt")
        body = [(m["role"], m.get("content")) for m in store.Chat(cid).messages()]
        said = [c for r, c in body if r in ("user", "assistant")]
        self.assertEqual(said, ["q1", "a1", "other question", "other answer", "here 1", "reply 1"])
        self.assertIn("history changed", body[-3][1])


class Groups(unittest.TestCase):
    def test_groups_nest_order_delete(self):
        from harness import groups
        a = groups.create("Alpha", project="P")
        b = groups.create("Beta", project="P")
        c = groups.create("Child", parent=a["id"])
        ids = [g["id"] for g in groups.all_groups()]
        self.assertIn(c["id"], ids)
        with self.assertRaises(ValueError):
            groups.update(a["id"], parent=c["id"])                  # not inside its own subgroup
        groups.update(b["id"], before=a["id"])
        tops = sorted((g for g in groups.all_groups() if not g.get("parent") and g.get("project") == "P"), key=lambda g: g["order"])
        self.assertEqual([g["name"] for g in tops], ["Beta", "Alpha"])
        groups.update(c["id"], name="Kid", collapsed=True)
        self.assertEqual((groups.get(c["id"])["name"], groups.get(c["id"])["collapsed"]), ("Kid", True))
        chat = store.create(str(TMP / "proj"), "", group=c["id"])
        updated = chat.meta()["updated"]
        groups.move_chats([chat.id], b["id"])
        self.assertEqual(chat.meta()["group"], b["id"])
        self.assertEqual(chat.meta()["updated"], updated)            # moving is not activity
        groups.move_chats([chat.id], c["id"])
        self.assertEqual(groups.delete(a["id"]), 0)                  # Alpha goes, Kid moves up
        self.assertIsNone(groups.get(c["id"])["parent"])
        self.assertEqual(groups.delete(c["id"]), 1)                  # Kid goes, its chat is kept, in no group
        self.assertIsNone(chat.meta().get("group"))
        self.assertFalse(chat.meta().get("deleted"))
        self.assertEqual(next(x for x in store.list_chats() if x["id"] == chat.id)["group"], None)

    def test_window_starts_at_a_turn(self):
        chat = store.create(str(TMP / "proj"), "")
        for i in range(30):
            chat.append({"role": "user", "content": f"q{i}"})
            chat.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
            chat.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r"})
            chat.append({"role": "assistant", "content": f"a{i}"})
        start, total, msgs = chat.window(10)
        self.assertEqual(total, 120)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(start + len(msgs), 120)
        self.assertGreaterEqual(len(msgs), 10)
        start2, _, earlier = chat.window(10, start)
        self.assertEqual(earlier[0]["role"], "user")
        self.assertEqual(start2 + len(earlier), start)
        from harness import agent
        h = agent.history(chat.id, 10)
        self.assertEqual((h["start"], h["total"], len(h["messages"])), (start, 120, len(msgs)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
