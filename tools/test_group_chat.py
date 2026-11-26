"""Automated tests for Group Chat mode (harness/group_chat.py + subagent caps + forking).

Verifies (using a throwaway test home and a local stub model runner, zero live LLM cost):
1. Room creation with multiple models, hidden persistent member chats, and chat-list visibility.
2. Structured [ROOM:*] parsing (continue / need_user / done + graceful fallback when untagged).
3. Delta-only Room Board delivery (each member receives ONLY new board posts since its cursor).
4. Sub-agent 3-way policy enforcement:
   - off (Relay): subagents.offered() is False, subagents.run() raises ToolError.
   - shared_worker: max 1 active subagent room-wide; 2nd concurrent call refused; Worker runs asynchronously.
   - open: max 3 active subagents room-wide; 4th concurrent call refused; releases cleanly.
5. Soft turn budget & baton passing:
   - Early yield passes baton immediately when a member finishes quickly.
   - Pass Baton (gentle nudge and forced immediate pass) advances speaker cleanly.
6. Stop conditions:
   - `need_user` pauses the room with the member's question.
   - `done` runs a 1-pass sign-off across other models when work happened; trivial greeting finishes cleanly.
   - Anti-loop fuse auto-pauses after 2 consecutive rounds with zero workspace changes.
7. Live Steer while running delivers steer to active member + Room Board.
8. Live config updates (changing mode, worker, budget, members) persist cleanly.
9. Live stream forwarding & persisted per-post thinking/tool trace.
10. Full and mid-message Group Chat forking with fresh independent member/worker chats and remapped `member_chat_id`.
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402

from harness import agent, config, fork, group_chat, store, subagents, tools


def _seed_endpoints():
    def fn(c):
        c["endpoints"] = [
            {"id": "m_claude", "name": "Claude Sonnet 5.5", "kind": "openai", "base": "http://127.0.0.1:9", "model": "claude-sonnet-5-5"},
            {"id": "m_gpt", "name": "ChatGPT 6.2", "kind": "openai", "base": "http://127.0.0.1:9", "model": "gpt-6.2"},
            {"id": "m_ds", "name": "DeepSeek V4.1", "kind": "openai", "base": "http://127.0.0.1:9", "model": "deepseek-v4.1-flash"},
            {"id": "m_local", "name": "Local Model", "kind": "openai", "base": "http://127.0.0.1:9", "model": "local"},
        ]
        c["endpoint"] = "m_claude"
    config.update(fn)


_seed_endpoints()


class FakeMemberHarness:
    """Patches agent.start / agent.busy / agent.stop for deterministic, instant Group Chat room tests."""

    def __init__(self, handler):
        self.handler = handler     # fn(chat_id, text, steers, stop_ev) -> str reply
        self.calls = []
        self._busy = {}
        self._stops = {}
        self._steers = {}
        self._orig_start = agent.start
        self._orig_busy = agent.busy
        self._orig_stop = agent.stop

    def __enter__(self):
        def fake_start(cid, text, files=None, endpoint=None, reasoning=None, qid=None, soft=False):
            self.calls.append({"chat_id": cid, "text": text, "soft": soft, "endpoint": endpoint})
            if soft and self._busy.get(cid):
                self._steers.setdefault(cid, []).append(text)
                return {"queued": True}
            stop_ev = threading.Event()
            self._stops[cid] = stop_ev
            self._busy[cid] = True

            def worker():
                try:
                    ch = store.Chat(cid)
                    ch.append({"role": "user", "content": text})
                    reply = self.handler(cid, text, self._steers.setdefault(cid, []), stop_ev)
                    if reply is not None:
                        ch.append({"role": "assistant", "content": reply, "stats": {"out_tokens": 40}})
                finally:
                    self._busy[cid] = False

            threading.Thread(target=worker, daemon=True).start()
            return {"queued": False}

        def fake_busy(cid):
            if cid in self._busy:
                return bool(self._busy[cid])
            return group_chat.busy(cid)

        def fake_stop(cid):
            if cid in self._stops:
                self._stops[cid].set()
            return {"ok": True, "dropped": []}

        agent.start = fake_start
        agent.busy = fake_busy
        agent.stop = fake_stop
        return self

    def __exit__(self, *a):
        agent.start = self._orig_start
        agent.busy = self._orig_busy
        agent.stop = self._orig_stop


def _wait_idle(room_id, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not group_chat.busy(room_id):
            time.sleep(0.05)
            if not group_chat.busy(room_id):
                return True
        time.sleep(0.05)
    return False


class GroupChatTests(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="flock-grp-ws-"))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_parse_board_post_structured_and_fallback(self):
        p1 = group_chat.parse_board_post(
            "I checked the config and hit a quick question: which API key should I use?\n[ROOM:need_user]"
        )
        self.assertEqual(p1["status"], "need_user")
        self.assertNotIn("[ROOM:", p1["summary"])
        self.assertNotIn("Changed:", p1["summary"])
        self.assertNotIn("Verdict:", p1["summary"])
        self.assertIn("which API key should I use?", p1["summary"])
        self.assertIn("which API key", p1["question"])

        p2 = group_chat.parse_board_post("Looks good, verified clean and all tests pass.\n[ROOM:done]")
        self.assertEqual(p2["status"], "done")
        self.assertNotIn("[ROOM:", p2["summary"])

        p3 = group_chat.parse_board_post("Looks good, approved and LGTM.", is_signoff=True)
        self.assertEqual(p3["status"], "done")

        p4 = group_chat.parse_board_post("Plain reply without tags")
        self.assertEqual(p4["status"], "continue")
        self.assertEqual(p4["summary"], "Plain reply without tags")

        # Safety-net stripping of leftover legacy STATUS/CHANGED/VERDICT/NEXT blocks
        p5 = group_chat.parse_board_post(
            "STATUS: done\nCHANGED: none\nVERDICT: Greeting acknowledged.\nNEXT: Wait for user.\n\nHappy to chat naturally!"
        )
        self.assertEqual(p5["status"], "done")
        self.assertEqual(p5["summary"], "Happy to chat naturally!")
        for bad in ("STATUS:", "CHANGED:", "VERDICT:", "NEXT:"):
            self.assertNotIn(bad, p5["summary"])

        # Legacy block with no trailing prose falls back to plain verdict text (no label)
        p6 = group_chat.parse_board_post("STATUS: continue\nCHANGED: app.py\nVERDICT: Updated parser cleanly.\nNEXT: Run tests.")
        self.assertEqual(p6["status"], "continue")
        self.assertEqual(p6["summary"], "Updated parser cleanly.")
        self.assertNotIn("VERDICT:", p6["summary"])

    def test_casual_greeting_stops_cleanly_without_signoff_spam(self):
        room, meta = group_chat.create_room(
            str(self.ws),
            members=["m_claude", "m_gpt"],
            subagent_mode="shared_worker",
            worker="m_ds",
        )
        turns_called = []

        def handler(cid, text, steers, stop_ev):
            turns_called.append(cid)
            return "Hey! Ready whenever you are. What would you like us to build?\n[ROOM:done]"

        with FakeMemberHarness(handler):
            group_chat.send(room.id, "hi")
            self.assertTrue(_wait_idle(room.id, timeout=8.0))

        self.assertEqual(len(turns_called), 1)
        self.assertEqual(group_chat.public_state(room.id)["state"], "done")
        posts = [m for m in room.messages() if m.get("role") == "group_post"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["content"], "Hey! Ready whenever you are. What would you like us to build?")
        self.assertNotIn("[ROOM:", posts[0]["content"])
        self.assertNotIn("Changed:", posts[0]["content"])
        notices = [m for m in room.messages() if m.get("role") == "notice"]
        self.assertEqual(notices, [])

    def test_create_room_hides_member_chats_and_lists_room(self):
        room, meta = group_chat.create_room(
            str(self.ws),
            members=["m_claude", "m_gpt", "m_ds"],
            title="Experiment Room",
            subagent_mode="shared_worker",
            worker="m_claude",
            turn_budget_s=600,
        )
        self.assertEqual(meta["mode"], "group")
        listed_ids = {c["id"] for c in store.list_chats()}
        self.assertIn(room.id, listed_ids)
        gc = meta["group_chat"]
        council_ids = [m["chat_id"] for m in gc["members"]]
        for m in gc["members"]:
            self.assertNotIn(m["chat_id"], listed_ids)
            self.assertEqual(m["role"], "panel")
        wm = gc["worker_member"]
        self.assertIsNotNone(wm)
        self.assertEqual(wm["endpoint"], "m_claude")
        self.assertEqual(wm["role"], "worker")
        self.assertNotIn(wm["chat_id"], council_ids)
        self.assertNotIn(wm["chat_id"], listed_ids)
        for cid in council_ids + [wm["chat_id"]]:
            cm = store.Chat(cid).meta()
            self.assertFalse(cm.get("sub_of"))
            self.assertEqual(cm.get("group_member_of"), room.id)
            tlist = agent.tool_list(cm)
            self.assertNotIn("ParentContext", tlist)
            self.assertNotIn("ReportToMain", tlist)

    def test_legacy_protocol_members_auto_rotate_and_prompts_stay_minimal(self):
        room, meta = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt"], subagent_mode="off")
        gc = dict(meta["group_chat"])
        old_c0 = gc["members"][0]["chat_id"]
        old_c1 = gc["members"][1]["chat_id"]
        store.Chat(old_c0).update_meta(touch=False, sub_of=room.id)
        store.Chat(old_c0).append({"role": "user", "content": "BOARD PROTOCOL (required): [BOARD_POST]\nSTATUS: continue\nCHANGED: none"})
        store.Chat(old_c1).append({"role": "assistant", "content": "STATUS: done\nCHANGED: none\nVERDICT: ok\nNEXT: wait"})
        gc["protocol_v"] = 1
        room.update_meta(touch=False, group_chat=gc)

        st = group_chat.public_state(room.id)
        new_c0 = st["members"][0]["chat_id"]
        new_c1 = st["members"][1]["chat_id"]
        self.assertNotEqual(new_c0, old_c0)
        self.assertNotEqual(new_c1, old_c1)
        self.assertFalse(store.Chat(new_c0).meta().get("sub_of"))
        self.assertEqual(store.Chat(new_c0).messages(), [])

        prompts = {}

        def handler(cid, text, steers, stop_ev):
            prompts.setdefault(cid, []).append(text)
            if len(prompts[cid]) == 1 and cid == new_c0:
                return "Hey, glad to be here!"
            if cid == new_c1:
                return "Same, let's chat."
            return "All good!\n[ROOM:done]"

        with FakeMemberHarness(handler):
            group_chat.send(room.id, "just chat naturally")
            self.assertTrue(_wait_idle(room.id, timeout=8.0))

        first_p = prompts[new_c0][0]
        for unwanted in ("Workspace diff", "Soft turn budget", "Changed:", "Verdict:", "Next:", "STATUS:", "BOARD_POST"):
            self.assertNotIn(unwanted, first_p)
        second_p = prompts[new_c0][1]
        self.assertTrue(second_p.startswith("ChatGPT 6.2:\nSame, let's chat."), repr(second_p))
        self.assertNotIn("New messages in the Group Chat", second_p)
        self.assertNotIn("Workspace diff", second_p)

    def test_shared_worker_can_be_outside_council_and_auto_migrates_legacy_room(self):
        room, meta = group_chat.create_room(
            str(self.ws),
            members=["m_claude", "m_gpt"],
            subagent_mode="shared_worker",
            worker="m_local",
        )
        st = group_chat.public_state(room.id)
        self.assertEqual([m["endpoint"] for m in st["members"]], ["m_claude", "m_gpt"])
        self.assertTrue(all(m["role"] == "panel" for m in st["members"]))
        self.assertEqual(st["worker_member"]["endpoint"], "m_local")
        self.assertNotIn(st["worker_member"]["chat_id"], {m["chat_id"] for m in st["members"]})

        legacy_gc = dict(room.meta()["group_chat"])
        legacy_gc["worker_member"] = None
        legacy_gc["worker"] = "m_gpt"
        legacy_gc["members"][1]["role"] = "worker"
        room.update_meta(touch=False, group_chat=legacy_gc)

        migrated = group_chat.public_state(room.id)
        self.assertTrue(all(m["role"] == "panel" for m in migrated["members"]))
        self.assertIsNotNone(migrated["worker_member"])
        self.assertEqual(migrated["worker_member"]["endpoint"], "m_gpt")
        self.assertNotIn(migrated["worker_member"]["chat_id"], {m["chat_id"] for m in migrated["members"]})

    def test_subagent_caps_off_shared_worker_and_open_max_3(self):
        room_off, m_off = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt"], subagent_mode="off")
        mc_off = store.Chat(m_off["group_chat"]["members"][0]["chat_id"])
        self.assertFalse(subagents.offered(mc_off.meta()))
        with self.assertRaises(tools.ToolError) as cm:
            subagents.run(mc_off, mc_off.meta(), "c1", {"prompt": "test"}, threading.Event())
        self.assertIn("locked OFF", str(cm.exception))

        room_sw, m_sw = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt"], subagent_mode="shared_worker", worker="m_claude")
        mc_sw = store.Chat(m_sw["group_chat"]["members"][0]["chat_id"])
        mw_sw = store.Chat(m_sw["group_chat"]["worker_member"]["chat_id"])
        self.assertFalse(subagents.offered(mc_sw.meta()))
        self.assertFalse(subagents.offered(mw_sw.meta()))
        with self.assertRaises(tools.ToolError) as cm_sw:
            subagents.run(mc_sw, mc_sw.meta(), "c1", {"prompt": "extra"}, threading.Event())
        self.assertIn("dedicated Shared Worker", str(cm_sw.exception))
        ok1, n1 = group_chat.acquire_subagent_slot(room_sw.id, "shared_worker", 1)
        ok2, n2 = group_chat.acquire_subagent_slot(room_sw.id, "extra", 1)
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual((n1, n2), (1, 1))
        group_chat.release_subagent_slot(room_sw.id, "shared_worker")
        self.assertEqual(group_chat.active_subagent_count(room_sw.id), 0)

        room_op, m_op = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt", "m_ds"], subagent_mode="open")
        rid, mode, limit = group_chat.subagent_policy_for(store.Chat(m_op["group_chat"]["members"][0]["chat_id"]).meta())
        self.assertEqual((rid, mode, limit), (room_op.id, "open", 3))
        self.assertTrue(subagents.offered(store.Chat(m_op["group_chat"]["members"][0]["chat_id"]).meta()))
        for k in ("a", "b", "c"):
            self.assertTrue(group_chat.acquire_subagent_slot(room_op.id, k, limit)[0])
        self.assertFalse(group_chat.acquire_subagent_slot(room_op.id, "d", limit)[0])
        group_chat.release_subagent_slot(room_op.id, "b")
        self.assertTrue(group_chat.acquire_subagent_slot(room_op.id, "d", limit)[0])
        for k in ("a", "c", "d"):
            group_chat.release_subagent_slot(room_op.id, k)

    def test_delta_only_board_and_unanimous_signoff_done(self):
        room, meta = group_chat.create_room(
            str(self.ws),
            members=["m_claude", "m_gpt", "m_ds"],
            subagent_mode="shared_worker",
            worker="m_claude",
            turn_budget_s=60,
        )
        gc = meta["group_chat"]
        worker_cid = gc["worker_member"]["chat_id"]
        council_by_cid = {m["chat_id"]: m["endpoint"] for m in gc["members"]}
        self.assertNotIn(worker_cid, council_by_cid)
        self.assertNotIn(worker_cid, group_chat._build_turn_queue(gc))

        seen_prompts = {}
        worker_started = threading.Event()
        worker_release = threading.Event()
        worker_steers_seen = []
        worker_slot_observed = []
        council_saw_worker_running = {"yes": False}
        gpt_turns = {"n": 0}
        claude_turns = {"n": 0}

        def handler(cid, text, steers, stop_ev):
            seen_prompts.setdefault(cid, []).append(text)
            if cid == worker_cid:
                worker_slot_observed.append(group_chat.active_subagent_count(room.id))
                worker_started.set()
                t0 = time.time()
                while time.time() - t0 < 5.0 and not stop_ev.is_set():
                    if any("add edge-case test" in s for s in steers):
                        worker_steers_seen.extend(steers)
                        break
                    if worker_release.is_set():
                        break
                    time.sleep(0.03)
                (self.ws / "feature.txt").write_text("built\n", encoding="utf-8")
                return "Built feature.txt with edge-case coverage."
            ep = council_by_cid[cid]
            if "marked this done" in text:
                return f"Checked {ep} side, looks clean and complete.\n[ROOM:done]"
            if "Built feature.txt" in text:
                return f"{ep}: worker output looks great, wrapping up.\n[ROOM:done]"
            if ep == "m_claude":
                claude_turns["n"] += 1
                if claude_turns["n"] == 1:
                    return "Let's get the worker building feature X right away.\n[WORKER:run] Build feature X in feature.txt"
                return "Keeping an eye on the worker, let's wait for it now.\n[ROOM:wait]"
            if ep == "m_gpt":
                gpt_turns["n"] += 1
                if "Worker (Claude Sonnet 5.5): running" in text:
                    council_saw_worker_running["yes"] = True
                if gpt_turns["n"] == 1:
                    return "Good call, I'll steer the worker to include edge-case tests while we talk.\n[WORKER:steer] Please add edge-case test too"
                return "Waiting for worker result.\n[ROOM:wait]"
            if "Worker (Claude Sonnet 5.5): running" in text:
                council_saw_worker_running["yes"] = True
            return "Sounds solid, waiting on the worker output.\n[ROOM:wait]"

        with FakeMemberHarness(handler):
            group_chat.send(room.id, "Build feature X and verify")
            self.assertTrue(_wait_idle(room.id, timeout=12.0))

        st = group_chat.public_state(room.id)
        self.assertEqual(st["state"], "done")
        self.assertTrue(worker_started.is_set())
        self.assertEqual(worker_slot_observed, [1])
        self.assertEqual(group_chat.active_subagent_count(room.id), 0)
        self.assertTrue(council_saw_worker_running["yes"])
        self.assertTrue(any("add edge-case test" in s for s in worker_steers_seen))
        for m in room.messages():
            if m.get("role") == "group_post":
                self.assertNotIn("[ROOM:", m["content"])
                self.assertNotIn("[WORKER:", m["content"])
                self.assertNotIn("Changed:", m["content"])
                self.assertNotIn("Verdict:", m["content"])

    def test_unanimous_wait_zero_token_sleep_and_no_anti_chatter(self):
        room, meta = group_chat.create_room(
            str(self.ws),
            members=["m_claude", "m_gpt"],
            subagent_mode="shared_worker",
            worker="m_ds",
            turn_budget_s=60,
            max_rounds=10,
        )
        gc = meta["group_chat"]
        worker_cid = gc["worker_member"]["chat_id"]
        c_by_cid = {m["chat_id"]: m["endpoint"] for m in gc["members"]}

        worker_release = threading.Event()
        sleep_observed = {"yes": False}
        turns_during_sleep = {"n": 0}
        claude_n = {"n": 0}
        gpt_n = {"n": 0}

        def handler(cid, text, steers, stop_ev):
            if cid == worker_cid:
                worker_release.wait(timeout=6.0)
                return "Worker finished heavy build cleanly."
            if sleep_observed["yes"] and not worker_release.is_set():
                turns_during_sleep["n"] += 1
            ep = c_by_cid[cid]
            if "marked this done" in text or "Worker finished heavy build" in text:
                return f"{ep} confirmed worker result.\n[ROOM:done]"
            if ep == "m_claude":
                claude_n["n"] += 1
                if claude_n["n"] == 1:
                    return "Kick off the build.\n[WORKER:run] Run heavy build"
                if claude_n["n"] == 2:
                    return "I can wait if you want.\n[ROOM:wait]"
                return "Okay let's both wait for the worker now.\n[ROOM:wait]"
            gpt_n["n"] += 1
            if gpt_n["n"] <= 2:
                return f"Still chatting freely on round {gpt_n['n']} while worker runs."
            return "Agreed, waiting for worker now.\n[ROOM:wait]"

        with FakeMemberHarness(handler):
            group_chat.send(room.id, "Run heavy build and discuss")
            t0 = time.time()
            while time.time() - t0 < 8.0:
                st_now = group_chat.public_state(room.id)
                if st_now.get("council_waiting") and st_now.get("worker_busy"):
                    sleep_observed["yes"] = True
                    break
                time.sleep(0.05)
            self.assertTrue(sleep_observed["yes"], "Council should enter zero-token sleep when all members wait")
            time.sleep(0.35)
            self.assertEqual(turns_during_sleep["n"], 0)
            worker_release.set()
            self.assertTrue(_wait_idle(room.id, timeout=8.0))

        self.assertEqual(group_chat.public_state(room.id)["state"], "done")
        self.assertGreaterEqual(claude_n["n"], 3)
        self.assertGreaterEqual(gpt_n["n"], 3)

    def test_need_user_pauses_room_and_resume_continues(self):
        room, meta = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt"], subagent_mode="off")
        ep_by_cid = {m["chat_id"]: m["endpoint"] for m in meta["group_chat"]["members"]}
        asked = {"n": 0}

        def handler(cid, text, steers, stop_ev):
            ep = ep_by_cid[cid]
            if ep == "m_claude" and asked["n"] == 0:
                asked["n"] += 1
                return "Before I start, do you want a blue or red theme?\n[ROOM:need_user]"
            return "Applied the blue theme cleanly.\n[ROOM:done]"

        with FakeMemberHarness(handler):
            group_chat.send(room.id, "Pick a theme")
            self.assertTrue(_wait_idle(room.id, timeout=8.0))
            st1 = group_chat.public_state(room.id)
            self.assertEqual(st1["state"], "need_user")
            self.assertIn("blue or red", st1["state_reason"])

            group_chat.send(room.id, "Go with blue")
            self.assertTrue(_wait_idle(room.id, timeout=8.0))
            st2 = group_chat.public_state(room.id)
            self.assertEqual(st2["state"], "done")

    def test_anti_loop_fuse_pauses_after_2_stale_rounds(self):
        room, _ = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt"], subagent_mode="off", max_rounds=10)

        def handler(cid, text, steers, stop_ev):
            return "Still thinking through the options."

        with FakeMemberHarness(handler):
            group_chat.send(room.id, "Vague goal")
            self.assertTrue(_wait_idle(room.id, timeout=8.0))

        st = group_chat.public_state(room.id)
        self.assertEqual(st["state"], "paused")
        self.assertIn("anti-loop fuse", st["state_reason"])
        self.assertEqual(st["round"], group_chat.STALE_ROUND_LIMIT)

    def test_pass_baton_sends_wrapup_nudge_and_live_steer(self):
        room, meta = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt"], subagent_mode="off", turn_budget_s=600)
        ep_by_cid = {m["chat_id"]: m["endpoint"] for m in meta["group_chat"]["members"]}
        got_steers = []
        started_claude = threading.Event()

        def handler(cid, text, steers, stop_ev):
            ep = ep_by_cid[cid]
            if ep == "m_claude":
                started_claude.set()
                t0 = time.time()
                while time.time() - t0 < 5.0 and not stop_ev.is_set():
                    if any("passed the baton" in s or "wrap up" in s for s in steers):
                        got_steers.extend(steers)
                        break
                    time.sleep(0.05)
                return "Wrapped up on the timer nudge.\n[ROOM:done]"
            return "Checked and approved.\n[ROOM:done]"

        with FakeMemberHarness(handler):
            group_chat.send(room.id, "Long task")
            self.assertTrue(started_claude.wait(timeout=3.0))
            time.sleep(0.15)
            group_chat.send(room.id, "Focus on tests first")
            group_chat.pass_baton(room.id, force=False)
            self.assertTrue(_wait_idle(room.id, timeout=8.0))

        self.assertTrue(any("Focus on tests first" in s for s in got_steers))
        self.assertTrue(any("passed the baton" in s for s in got_steers))
        self.assertEqual(group_chat.public_state(room.id)["state"], "done")

    def test_update_room_config_live(self):
        room, _ = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt"], subagent_mode="off", turn_budget_s=600)
        st = group_chat.update_room_config(room.id, {
            "members": ["m_gpt", "m_ds", "m_local"],
            "subagent_mode": "open",
            "turn_budget_s": 900,
            "max_rounds": 8,
        })
        self.assertEqual(st["subagent_mode"], "open")
        self.assertEqual(st["subagent_limit"], 3)
        self.assertEqual(st["turn_budget_s"], 900)
        self.assertEqual([m["endpoint"] for m in st["members"]], ["m_gpt", "m_ds", "m_local"])

    def test_live_stream_forwarding_and_persisted_trace(self):
        room, meta = group_chat.create_room(str(self.ws), ["m_claude", "m_gpt"], subagent_mode="off")
        gc = meta["group_chat"]
        c0_id = gc["members"][0]["chat_id"]

        captured = []
        orig_pub = agent.hub.publish

        def spy_publish(ev):
            if ev.get("type") == "group_stream" and ev.get("chat") == room.id:
                captured.append(ev)
            return orig_pub(ev)

        def handler(cid, text, steers, stop_ev):
            ch = store.Chat(cid)
            agent.emit(cid, {"type": "step", "id": "s1"})
            agent.emit(cid, {"type": "reasoning", "id": "s1", "text": "Checking files first..."})
            m_asst = ch.append({
                "role": "assistant",
                "reasoning_content": "Checking files first...",
                "content": "",
                "tool_calls": [{"id": "tc1", "name": "Read", "arguments": '{"file_path": "app.py"}'}],
            })
            agent.emit(cid, {"type": "assistant", "temp": "s1", "message": agent._public(m_asst)})
            agent.emit(cid, {"type": "tool_start", "call": {"id": "tc1", "name": "Read"}})
            m_tool = ch.append({"role": "tool", "tool_call_id": "tc1", "name": "Read", "content": "print('hi')", "ok": True})
            agent.emit(cid, {"type": "message", "message": agent._public(m_tool)})
            agent.emit(cid, {"type": "delta", "id": "s2", "text": "All checked and ready."})
            snaps = group_chat.live_snapshots(room.id)
            self.assertEqual(len(snaps), 1)
            self.assertEqual(snaps[0]["member_chat"], cid)
            self.assertTrue(any(p["kind"] == "thinking" for p in snaps[0]["trace"]))
            self.assertTrue(any(p["kind"] == "tool" and p["name"] == "Read" for p in snaps[0]["trace"]))
            return "All checked and ready.\n[ROOM:done]"

        agent.hub.publish = spy_publish
        try:
            with FakeMemberHarness(handler):
                group_chat.send(room.id, "inspect app.py")
                self.assertTrue(_wait_idle(room.id, timeout=8.0))
        finally:
            agent.hub.publish = orig_pub

        c0_events = [e for e in captured if e["member_chat"] == c0_id]
        inner_types = [e["ev"]["type"] for e in c0_events]
        for want in ("step", "reasoning", "assistant", "tool_start", "message", "delta"):
            self.assertIn(want, inner_types)

        posts = [m for m in room.messages() if m.get("role") == "group_post"]
        self.assertGreaterEqual(len(posts), 1)
        tr = posts[0].get("trace") or []
        self.assertTrue(any(p["kind"] == "thinking" and "Checking files" in p["text"] for p in tr))
        tool_items = [p for p in tr if p["kind"] == "tool"]
        self.assertEqual(len(tool_items), 1)
        self.assertEqual(tool_items[0]["name"], "Read")
        self.assertEqual(tool_items[0]["output"], "print('hi')")
        self.assertTrue(tool_items[0]["ok"])

    def test_fork_group_chat_full_and_mid_message_with_independent_sessions(self):
        room, meta = group_chat.create_room(
            str(self.ws),
            members=["m_claude", "m_gpt"],
            title="Design Sprint",
            subagent_mode="shared_worker",
            worker="m_ds",
            turn_budget_s=480,
            max_rounds=15,
        )
        parent_gc = meta["group_chat"]
        p_c0 = parent_gc["members"][0]["chat_id"]
        p_c1 = parent_gc["members"][1]["chat_id"]
        parent_member_cids = {p_c0, p_c1, parent_gc["worker_member"]["chat_id"]}
        for cid in parent_member_cids:
            store.Chat(cid).update_meta(touch=False, claude_session="sess-parent", codex_thread="th-parent", agy_conversation="conv-parent")

        room.append({"id": "gu1", "role": "user", "content": "Design the new landing page", "board_seq": 1})
        room.append({"id": "gp1", "role": "group_post", "board_seq": 2, "member_chat_id": p_c0, "member_endpoint": "m_claude", "member_name": "Claude Sonnet 5.5", "member_role": "panel", "content": "Claude recommends clean typography."})
        room.append({"id": "gp2", "role": "group_post", "board_seq": 3, "member_chat_id": p_c1, "member_endpoint": "m_gpt", "member_name": "ChatGPT 6.2", "member_role": "panel", "content": "GPT agrees and proposes a hero grid."})
        room.append({"id": "gu2", "role": "user", "content": "Also check pricing copy", "board_seq": 4})
        room.append({"id": "gp3", "role": "group_post", "board_seq": 5, "member_chat_id": p_c0, "member_endpoint": "m_claude", "member_name": "Claude Sonnet 5.5", "member_role": "panel", "content": "Pricing copy looks sharp."})
        parent_gc["board_seq"] = 5
        parent_gc["goal"] = "Design the new landing page\n\nFollow-up from user: Also check pricing copy"
        parent_gc["state"] = "done"
        parent_gc["signoff_by"] = "Claude Sonnet 5.5"
        parent_gc["signoff_pending"] = ["old-cid"]
        parent_gc["stale_rounds"] = 2
        room.update_meta(group_chat=parent_gc)

        # 1. Full fork
        rf = fork.fork_chat(room.id)
        froom = store.Chat(rf["id"])
        fmeta = froom.meta()
        fgc = fmeta["group_chat"]
        self.assertEqual(fmeta["mode"], "group")
        self.assertEqual(fmeta["title"], "Design Sprint (fork)")
        self.assertEqual(fgc["subagent_mode"], "shared_worker")
        self.assertEqual(fgc["worker"], "m_ds")
        self.assertEqual(fgc["turn_budget_s"], 480)
        self.assertEqual(fgc["max_rounds"], 15)
        self.assertEqual(fgc["board_seq"], 5)
        self.assertEqual(fgc["state"], "idle")
        self.assertEqual(fgc["signoff_by"], "")
        self.assertEqual(fgc["signoff_pending"], [])
        self.assertEqual(fgc["stale_rounds"], 0)
        fmsgs = froom.messages()
        self.assertEqual([m["id"] for m in fmsgs], ["gu1", "gp1", "gp2", "gu2", "gp3"])
        self.assertEqual([m["board_seq"] for m in fmsgs], [1, 2, 3, 4, 5])
        # Remapped member_chat_id on copied group_post messages never points to parent member chats
        f_c0 = fgc["members"][0]["chat_id"]
        f_c1 = fgc["members"][1]["chat_id"]
        self.assertEqual(fmsgs[1].get("member_chat_id"), f_c0)
        self.assertEqual(fmsgs[2].get("member_chat_id"), f_c1)
        self.assertEqual(fmsgs[4].get("member_chat_id"), f_c0)
        fork_member_cids = {f_c0, f_c1, fgc["worker_member"]["chat_id"]}
        self.assertEqual(parent_member_cids & fork_member_cids, set())
        for m in fgc["members"] + [fgc["worker_member"]]:
            self.assertEqual(m["cursor"], 0)
            self.assertEqual(m["turns"], 0)
            cm = store.Chat(m["chat_id"]).meta()
            self.assertEqual(cm.get("group_member_of"), froom.id)
            self.assertFalse(cm.get("sub_of"))
            for forbidden in ("claude_session", "codex_thread", "agy_conversation"):
                self.assertNotIn(forbidden, cm)

        seen_first = {}

        def handler(cid, text, steers, stop_ev):
            seen_first.setdefault(cid, text)
            return "Continuing cleanly from the forked discussion.\n[ROOM:done]"

        with FakeMemberHarness(handler):
            group_chat.send(froom.id, "Now try a dark palette instead")
            self.assertTrue(_wait_idle(froom.id, timeout=8.0))

        self.assertIn("Claude recommends clean typography.", seen_first[f_c0])
        self.assertIn("GPT agrees and proposes a hero grid.", seen_first[f_c0])
        self.assertIn("Pricing copy looks sharp.", seen_first[f_c0])
        self.assertIn("Now try a dark palette instead", seen_first[f_c0])
        self.assertEqual(len(room.messages()), 5)

        # 2. Fork from a specific mid-chat group_post message (gp2)
        rmid = fork.fork_chat(room.id, from_msg_id="gp2")
        mroom = store.Chat(rmid["id"])
        mgc = mroom.meta()["group_chat"]
        mmsgs = mroom.messages()
        self.assertEqual([m["id"] for m in mmsgs], ["gu1", "gp1", "gp2"])
        self.assertEqual(mgc["board_seq"], 3)
        self.assertEqual(mgc["goal"], "Design the new landing page")
        self.assertNotIn("pricing copy", mgc["goal"].lower())
        self.assertEqual(mgc["state"], "idle")


if __name__ == "__main__":
    unittest.main(verbosity=2)
