"""Group Chat mode: multi-model rooms with per-model persistent sessions, delta-only message delivery,
soft turn budgets with gentle wrap-up nudges and early yield, three sub-agent policies
(off / shared_worker / open max 3), sign-off consensus, and an anti-loop fuse.

Standard library only. Zero hidden extra LLM calls: each model replies naturally in its own persistent
session, receiving only new room messages since its last turn.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import config, osutil, store

SUBAGENT_MODES = ("off", "shared_worker", "open")
DEFAULT_SUBAGENT_MODE = "shared_worker"
DEFAULT_TURN_BUDGET_S = 600          # 10 minutes soft budget per turn
MIN_TURN_BUDGET_S = 30
MAX_TURN_BUDGET_S = 3600
DEFAULT_MAX_ROUNDS = 12
DEFAULT_MAX_TOTAL_S = 7200           # 2 hours default total room budget
STALE_ROUND_LIMIT = 2                # auto-pause after 2 consecutive rounds with zero workspace changes
NUDGE_FRACTION = 0.80                # gentle wrap-up nudge at 80% of turn budget
GRACE_AFTER_BUDGET_S = 45            # soft grace after 100% before forcing baton handoff
SIGNOFF_BUDGET_CAP_S = 180           # quick sign-off pass cap (3 min)
OPEN_MODE_MAX_SUBAGENTS = 3          # Open Mode allows max 3 active subagents
SHARED_WORKER_MAX_SUBAGENTS = 1

_runs = {}                           # room_id -> GroupRun
_runs_lock = threading.RLock()
_meta_locks = {}                     # room_id -> RLock (serializes concurrent Council + async Worker room updates)
_meta_locks_guard = threading.Lock()
_sub_slots = {}                      # room_id -> set of active subagent call keys
_sub_slots_lock = threading.Lock()
_live_members = {}                   # member_chat_id -> {room_id, endpoint, name, role, before_n}
_live_members_lock = threading.Lock()

_FORWARD_EVENT_TYPES = frozenset({
    "step", "delta", "reasoning", "tool_draft", "assistant",
    "tool_start", "waiting", "retry", "activity", "step_cancelled",
})


def _register_live_member(member_chat_id, room_id, member, before_n):
    with _live_members_lock:
        _live_members[member_chat_id] = {
            "room_id": room_id,
            "endpoint": member.get("endpoint") or "",
            "name": _speaker_label(member),
            "role": member.get("role") or "relay",
            "before_n": int(before_n or 0),
        }


def _unregister_live_member(member_chat_id):
    with _live_members_lock:
        _live_members.pop(member_chat_id, None)


def forward(cid, ev):
    """Forward live thinking, tool calls, tool results, and streaming text from an active Group Chat member
    or Shared Worker chat to its parent room's SSE feed. O(1) no-op for non-group chats."""
    with _live_members_lock:
        info = _live_members.get(cid)
    if not info:
        return False
    etype = ev.get("type")
    if etype == "message":
        msg = ev.get("message") or {}
        if msg.get("role") != "tool":
            return False
    elif etype not in _FORWARD_EVENT_TYPES:
        return False
    from . import agent
    agent.hub.publish({
        "type": "group_stream",
        "chat": info["room_id"],
        "member_chat": cid,
        "member_endpoint": info["endpoint"],
        "member_name": info["name"],
        "member_role": info["role"],
        "ev": ev,
    })
    return True


def live_snapshots(room_id):
    """Return in-progress thinking & tool trace for any currently running member/worker in `room_id`,
    so opening or reloading a busy Group Chat immediately renders active work in progress."""
    with _live_members_lock:
        items = [(cid, dict(info)) for cid, info in _live_members.items() if info.get("room_id") == room_id]
    out = []
    for cid, info in items:
        try:
            ch = store.Chat(cid)
            trace = _collect_turn_trace(ch, info["before_n"])
            reply_txt, _, _ = _latest_member_reply(ch, info["before_n"])
        except Exception:
            trace, reply_txt = [], ""
        out.append({
            "member_chat": cid,
            "member_endpoint": info["endpoint"],
            "member_name": info["name"],
            "member_role": info["role"],
            "trace": trace,
            "text": reply_txt,
        })
    return out


def _meta_lock(room_id):
    with _meta_locks_guard:
        lk = _meta_locks.get(room_id)
        if lk is None:
            lk = _meta_locks[room_id] = threading.RLock()
        return lk


class GroupRun:
    def __init__(self, room_id):
        self.room_id = room_id
        self.thread = None
        self.stop_ev = threading.Event()
        self.pause_ev = threading.Event()       # soft pause: stop after active turn finishes
        self.pass_ev = threading.Event()        # soft pass baton: nudge active member to wrap up now
        self.force_pass_ev = threading.Event()  # hard pass baton: stop active member turn immediately
        self.wake_ev = threading.Event()        # wakes Council from zero-token sleep on worker done or user msg
        self.ended = False
        self.end_lock = threading.Lock()
        self.started = time.time()
        # Independent async Shared Worker lane state (never blocks Council turns)
        self.worker_lock = threading.Lock()
        self.worker_thread = None
        self.worker_stop_ev = threading.Event()
        self.worker_active = False
        self.worker_started = 0.0
        self.worker_before_n = 0
        self.worker_task = ""
        self.worker_what = ""
        self.worker_last_done_at = 0.0
        self.worker_last_summary = ""
        self.worker_last_tools = 0
        self.worker_last_files = []
        self.worker_last_seq = 0
        self.worker_ran_since_check = False
        # Unanimous wait state (zero-token sleep ONLY when every Council member chooses [ROOM:wait])
        self.waiting_cids = set()
        self.council_sleeping = False


# ------------------------------------------------------------------------------------------------ helpers & policy
def is_group(chat_or_meta):
    m = chat_or_meta.meta() if hasattr(chat_or_meta, "meta") else (chat_or_meta or {})
    return isinstance(m, dict) and m.get("mode") == "group" and isinstance(m.get("group_chat"), dict)


def room_of_member(chat_or_meta):
    """Return room_id if this chat is a member chat (or nested subagent of a member chat) of a Group Room."""
    m = chat_or_meta.meta() if hasattr(chat_or_meta, "meta") else (chat_or_meta or {})
    if not isinstance(m, dict):
        return ""
    if m.get("group_member_of"):
        return str(m["group_member_of"])
    if m.get("group_room"):
        return str(m["group_room"])
    return ""


def normalize_subagent_mode(val):
    s = str(val or "").strip().lower().replace("-", "_").replace(" ", "_")
    if s in ("off", "none", "relay", "locked"):
        return "off"
    if s in ("shared_worker", "shared", "worker", "council", "1_shared_worker"):
        return "shared_worker"
    if s in ("open", "open_mode", "3", "max3"):
        return "open"
    return DEFAULT_SUBAGENT_MODE


def subagent_limit_for_mode(mode):
    m = normalize_subagent_mode(mode)
    if m == "off":
        return 0
    if m == "shared_worker":
        return SHARED_WORKER_MAX_SUBAGENTS
    return OPEN_MODE_MAX_SUBAGENTS


def subagent_policy_for(meta):
    """Return (room_id, mode, max_active) when `meta` belongs to a Group Room member/subagent, else (None, None, None)."""
    rid = room_of_member(meta)
    if not rid:
        return None, None, None
    try:
        rm = store.Chat(rid).meta() or {}
    except ValueError:
        return None, None, None
    gc = rm.get("group_chat") if isinstance(rm.get("group_chat"), dict) else {}
    mode = normalize_subagent_mode(gc.get("subagent_mode"))
    return rid, mode, subagent_limit_for_mode(mode)


def subagents_allowed_for(meta):
    """True only when a chat is outside a Group Room, or in an Open-mode Group Room.
    In 'off' (Relay) mode sub-agents are locked off.
    In 'shared_worker' mode the room's dedicated Worker IS the 1 Shared Worker sub-agent, so neither
    Council members nor the Worker may spawn additional sub-agents on top."""
    rid, mode, limit = subagent_policy_for(meta)
    if rid is None:
        return True
    return mode == "open" and limit > 0


def active_subagent_count(room_id):
    with _sub_slots_lock:
        return len(_sub_slots.get(room_id) or ())


def acquire_subagent_slot(room_id, call_key, limit):
    """Enforce room-wide active subagent cap (0 for off, 1 for shared_worker, 3 for open). Returns (ok, active_count)."""
    with _sub_slots_lock:
        s = _sub_slots.setdefault(room_id, set())
        if call_key in s:
            return True, len(s)
        if len(s) >= limit:
            return False, len(s)
        s.add(call_key)
        return True, len(s)


def release_subagent_slot(room_id, call_key):
    if not room_id:
        return
    with _sub_slots_lock:
        s = _sub_slots.get(room_id)
        if s:
            s.discard(call_key)
            if not s:
                _sub_slots.pop(room_id, None)


def _ep_name(ep_id):
    try:
        ep = config.endpoint(ep_id)
        if ep:
            return ep.get("name") or ep["id"]
    except LookupError:
        pass
    return str(ep_id)


def _live(run):
    return bool(run and run.thread and run.thread.is_alive() and not run.ended)


def busy(room_id):
    with _runs_lock:
        return _live(_runs.get(room_id))


def busy_rooms():
    with _runs_lock:
        return [rid for rid, r in _runs.items() if _live(r)]


def room_activity(room_id):
    """Compact live activity dict for the sidebar / header while a Group Room is running."""
    with _runs_lock:
        run = _runs.get(room_id)
    if not _live(run):
        return None
    try:
        m = store.Chat(room_id).meta() or {}
    except ValueError:
        return None
    gc = m.get("group_chat") or {}
    if run.council_sleeping and run.worker_active:
        wm = gc.get("worker_member") if isinstance(gc.get("worker_member"), dict) else {}
        wname = wm.get("name") or _ep_name(wm.get("endpoint") or "")
        return {"what": f"Waiting for Worker ({wname})", "line": run.worker_what or run.worker_task, "since": run.worker_started or time.time()}
    who = _ep_name(gc.get("active_endpoint") or "")
    started = float(gc.get("active_started") or time.time())
    rd = int(gc.get("round") or 1)
    suffix = " + Worker" if run.worker_active else ""
    what = f"{who}{suffix} (Round {rd})" if who else f"Group Round {rd}"
    return {"what": what, "line": gc.get("state_reason") or "", "since": started}


# ------------------------------------------------------------------------------------------------ room creation & config
PROTOCOL_VERSION = 2

_LEGACY_PROTO_RE = re.compile(
    r"\[/?BOARD_POST\]|</?BOARD_POST>|BOARD PROTOCOL|Changed:/Verdict:/Next:|"
    r"^\s*(?:\*\*|-\s*)?(?:STATUS\s*:\s*(?:continue|done|need_user|need user)\b|(?:CHANGED|VERDICT|NEXT)\s*:)",
    re.I | re.M,
)


def _member_needs_reset(chat_id):
    """True when an existing member chat still carries sub_of parent-chat injection or old protocol history."""
    if not chat_id:
        return True
    try:
        ch = store.Chat(chat_id)
        cm = ch.meta() or {}
    except Exception:
        return True
    if cm.get("sub_of"):
        return True
    try:
        for msg in ch.messages():
            for field in (msg.get("content"), msg.get("model_content")):
                if field and _LEGACY_PROTO_RE.search(str(field)):
                    return True
            for part in (msg.get("parts") or []):
                if isinstance(part, dict) and part.get("text") and _LEGACY_PROTO_RE.search(str(part["text"])):
                    return True
    except Exception:
        pass
    return False


def _recent_user_cursor(room):
    """Return the board_seq immediately before the most recent user message in the room (so rotated members
    see the current user topic without re-reading ancient pre-migration chatter)."""
    last_user_seq = 0
    for msg in room.messages():
        if msg.get("role") == "user":
            seq = int(msg.get("board_seq") or 0)
            if seq > last_user_seq:
                last_user_seq = seq
    return max(0, last_user_seq - 1) if last_user_seq > 0 else 0


def _ensure_member_chat(room_chat, ep_id, role, subagent_mode):
    """Create a hidden persistent native chat for one Council/Relay member or the dedicated Shared Worker sub-agent.
    Uses group_member_of (NOT sub_of) so member sessions stay hidden in the sidebar without injecting sub-agent
    parent-chat boilerplate or ParentContext/ReportToMain tools."""
    rmeta = room_chat.meta() or {}
    project = rmeta.get("project") or str(config.WORKSPACE)
    ep_label = _ep_name(ep_id)
    is_worker = role == "worker"
    chat_title = f"{rmeta.get('title') or 'Group'} - Worker ({ep_label})" if is_worker else f"{rmeta.get('title') or 'Group'} - {ep_label}"
    ch = store.create(project, ep_id, title=chat_title, reasoning=rmeta.get("reasoning"))
    extra = {
        "group_member_of": room_chat.id,
        "group_room": room_chat.id,
        "group_role": role,
    }
    if isinstance(rmeta.get("effort"), dict) and rmeta["effort"]:
        extra["effort"] = dict(rmeta["effort"])
    ch.update_meta(touch=False, **extra)
    return ch


def _make_worker_record(room_chat, worker_id, cursor=0):
    wchat = _ensure_member_chat(room_chat, worker_id, "worker", "shared_worker")
    return {
        "endpoint": worker_id,
        "name": _ep_name(worker_id),
        "chat_id": wchat.id,
        "cursor": int(cursor or 0),
        "role": "worker",
        "turns": 0,
    }


def _valid_endpoint_or(eid, fallback):
    s = str(eid or "").strip()
    if s:
        try:
            config.endpoint(s)
            return s
        except LookupError:
            pass
    return fallback


def _ensure_migrated(room, meta=None, allow_rotate=True):
    """Auto-migrate legacy rooms:
    1. Separate dedicated worker_member sub-agent chat when a council member was demoted to worker.
    2. Rotate any legacy member/worker chats that carry old sub_of parent-chat injection or old structured
       protocol prompts so models start cleanly without repeating old headers."""
    if meta is None:
        meta = room.meta() or {}
    if not is_group(meta):
        return meta
    gc = dict(meta.get("group_chat") or {})
    mode = normalize_subagent_mode(gc.get("subagent_mode"))
    members = [dict(m) for m in (gc.get("members") or [])]
    council_ids = {m.get("chat_id") for m in members if m.get("chat_id")}
    changed = False

    want_role = "panel" if mode == "shared_worker" else "relay"
    for m in members:
        if m.get("role") != want_role:
            m["role"] = want_role
            changed = True
            try:
                store.Chat(m["chat_id"]).update_meta(touch=False, group_role=want_role)
            except Exception:
                pass

    if mode == "shared_worker":
        fallback_w = members[0]["endpoint"] if members else (config.get().get("endpoint") or "")
        worker_id = _valid_endpoint_or(gc.get("worker"), fallback_w)
        if gc.get("worker") != worker_id:
            gc["worker"] = worker_id
            changed = True
        wm = gc.get("worker_member") if isinstance(gc.get("worker_member"), dict) else None
        needs_new_worker = (
            not wm
            or not wm.get("chat_id")
            or wm.get("chat_id") in council_ids
            or wm.get("endpoint") != worker_id
        )
        if needs_new_worker and worker_id:
            gc["worker_member"] = _make_worker_record(room, worker_id)
            changed = True

    # Rotate legacy member chats that still have sub_of or old protocol history (never while room is actively running)
    if allow_rotate and int(gc.get("protocol_v") or 0) < PROTOCOL_VERSION and not busy(room.id):
        base_cursor = _recent_user_cursor(room)
        rotated_any = False
        for m in members:
            if _member_needs_reset(m.get("chat_id")):
                fresh = _ensure_member_chat(room, m["endpoint"], m.get("role") or want_role, mode)
                m["chat_id"] = fresh.id
                m["turns"] = 0
                m["cursor"] = base_cursor
                rotated_any = True
                changed = True
        wm = gc.get("worker_member") if isinstance(gc.get("worker_member"), dict) else None
        if wm and _member_needs_reset(wm.get("chat_id")):
            gc["worker_member"] = _make_worker_record(room, wm["endpoint"], cursor=base_cursor)
            rotated_any = True
            changed = True
        if rotated_any:
            gc["signoff_by"] = ""
            gc["signoff_pending"] = []
        gc["protocol_v"] = PROTOCOL_VERSION
        changed = True

    if changed:
        gc["members"] = members
        meta = room.update_meta(touch=False, group_chat=gc)
    return meta


def create_room(project, members, title=None, subagent_mode=DEFAULT_SUBAGENT_MODE, worker=None,
                turn_budget_s=DEFAULT_TURN_BUDGET_S, max_rounds=DEFAULT_MAX_ROUNDS,
                max_total_s=DEFAULT_MAX_TOTAL_S, reasoning=None,
                group=None, section=None):
    """Create a new Group Chat room and its per-model persistent member chats (plus a separate dedicated
    Shared Worker sub-agent chat when subagent_mode == 'shared_worker')."""
    ep_ids = []
    for m in (members or []):
        eid = m.get("endpoint") if isinstance(m, dict) else str(m or "").strip()
        if eid and eid not in ep_ids:
            ep_ids.append(eid)
    if len(ep_ids) < 2:
        raise ValueError("Select at least 2 models for a Group Chat")
    for eid in ep_ids:
        try:
            config.endpoint(eid)
        except LookupError:
            raise ValueError(f"Unknown model endpoint: {eid}")

    mode = normalize_subagent_mode(subagent_mode)
    worker_id = _valid_endpoint_or(worker, ep_ids[0])
    if worker and worker != worker_id:
        raise ValueError(f"Unknown Shared Worker model endpoint: {worker}")
    budget = max(MIN_TURN_BUDGET_S, min(MAX_TURN_BUDGET_S, int(turn_budget_s or DEFAULT_TURN_BUDGET_S)))
    m_rounds = max(1, min(100, int(max_rounds or DEFAULT_MAX_ROUNDS)))
    m_total = max(300, min(86400, int(max_total_s or DEFAULT_MAX_TOTAL_S)))

    clean_title = (title or "").strip() or "Group: " + " + ".join(_ep_name(e).split()[0] for e in ep_ids[:3]) + (f" (+{len(ep_ids) - 3})" if len(ep_ids) > 3 else "")
    room = store.create(project, ep_ids[0], title=clean_title, reasoning=reasoning, group=group)
    room.update_meta(touch=False, mode="group", user_renamed=bool((title or "").strip()))

    member_role = "panel" if mode == "shared_worker" else "relay"
    member_recs = []
    for eid in ep_ids:
        mchat = _ensure_member_chat(room, eid, member_role, mode)
        member_recs.append({
            "endpoint": eid,
            "name": _ep_name(eid),
            "chat_id": mchat.id,
            "cursor": 0,
            "role": member_role,
            "turns": 0,
        })

    worker_member = _make_worker_record(room, worker_id) if mode == "shared_worker" else None

    gc = {
        "members": member_recs,
        "worker_member": worker_member,
        "subagent_mode": mode,
        "worker": worker_id,
        "turn_budget_s": budget,
        "max_rounds": m_rounds,
        "max_total_s": m_total,
        "goal": "",
        "state": "idle",
        "state_reason": "",
        "round": 0,
        "active_idx": 0,
        "active_endpoint": "",
        "active_member_chat": "",
        "active_role": "",
        "active_started": 0.0,
        "turn_deadline": 0.0,
        "stale_rounds": 0,
        "signoff_by": "",
        "signoff_pending": [],
        "board_seq": 0,
        "protocol_v": PROTOCOL_VERSION,
    }
    meta = room.update_meta(group_chat=gc)
    if section:
        from . import sections
        sections.assign_new_chat(room.id, section, group)
    return room, meta


def clone_room_state(new_room, parent_gc, copied_msgs, from_msg_id=None):
    """Build a fresh, independent group_chat state for a forked Group Chat room.
    Creates brand-new hidden member chats (and a fresh dedicated Shared Worker chat when in
    shared_worker mode) with cursor=0 and turns=0 so each model receives the copied room history
    on the fork's first turn with ZERO shared CLI sessions/threads with the parent room."""
    parent_gc = parent_gc if isinstance(parent_gc, dict) else {}
    mode = normalize_subagent_mode(parent_gc.get("subagent_mode"))
    member_role = "panel" if mode == "shared_worker" else "relay"

    ep_ids = []
    for m in (parent_gc.get("members") or []):
        eid = str((m.get("endpoint") if isinstance(m, dict) else m) or "").strip()
        if not eid or eid in ep_ids:
            continue
        try:
            config.endpoint(eid)
            ep_ids.append(eid)
        except LookupError:
            pass
    if not ep_ids:
        fallback_ep = (new_room.meta() or {}).get("endpoint") or config.get().get("endpoint") or ""
        if fallback_ep:
            ep_ids = [fallback_ep]

    member_recs = []
    for eid in ep_ids:
        mchat = _ensure_member_chat(new_room, eid, member_role, mode)
        member_recs.append({
            "endpoint": eid,
            "name": _ep_name(eid),
            "chat_id": mchat.id,
            "cursor": 0,
            "role": member_role,
            "turns": 0,
        })

    fallback_w = ep_ids[0] if ep_ids else (config.get().get("endpoint") or "")
    worker_id = _valid_endpoint_or(parent_gc.get("worker"), fallback_w)
    worker_member = _make_worker_record(new_room, worker_id, cursor=0) if (mode == "shared_worker" and worker_id) else None

    # Preserve existing board_seq on copied room messages and guarantee monotonic positive seqs
    max_seq = 0
    for msg in (copied_msgs or []):
        if msg.get("role") in ("user", "group_post"):
            seq = int(msg.get("board_seq") or 0)
            if seq <= max_seq:
                seq = max_seq + 1
                msg["board_seq"] = seq
            max_seq = seq

    if from_msg_id:
        u_texts = [
            str(m.get("content") or "").strip()
            for m in (copied_msgs or [])
            if m.get("role") == "user" and str(m.get("content") or "").strip()
        ]
        goal = (u_texts[0] + "".join(f"\n\nFollow-up from user: {t}" for t in u_texts[1:])) if u_texts else ""
    else:
        goal = str(parent_gc.get("goal") or "")
        if not goal:
            u_texts = [
                str(m.get("content") or "").strip()
                for m in (copied_msgs or [])
                if m.get("role") == "user" and str(m.get("content") or "").strip()
            ]
            if u_texts:
                goal = u_texts[0] + "".join(f"\n\nFollow-up from user: {t}" for t in u_texts[1:])

    return {
        "members": member_recs,
        "worker_member": worker_member,
        "subagent_mode": mode,
        "worker": worker_id,
        "turn_budget_s": int(parent_gc.get("turn_budget_s") or DEFAULT_TURN_BUDGET_S),
        "max_rounds": int(parent_gc.get("max_rounds") or DEFAULT_MAX_ROUNDS),
        "max_total_s": int(parent_gc.get("max_total_s") or DEFAULT_MAX_TOTAL_S),
        "goal": goal,
        "state": "idle",
        "state_reason": "",
        "round": 0,
        "active_idx": 0,
        "active_endpoint": "",
        "active_member_chat": "",
        "active_role": "",
        "active_started": 0.0,
        "turn_deadline": 0.0,
        "stale_rounds": 0,
        "signoff_by": "",
        "signoff_pending": [],
        "board_seq": max_seq,
        "protocol_v": PROTOCOL_VERSION,
    }


def update_room_config(room_id, body):
    """Update a Group Chat's mode, separate Shared Worker model, turn budget, max rounds, or Council member list."""
    room = store.Chat(room_id)
    meta = _ensure_migrated(room)
    if not is_group(meta):
        raise ValueError("not a group chat")
    gc = dict(meta.get("group_chat") or {})
    members = [dict(x) for x in (gc.get("members") or [])]

    if "subagent_mode" in body:
        gc["subagent_mode"] = normalize_subagent_mode(body["subagent_mode"])
    if "turn_budget_s" in body:
        gc["turn_budget_s"] = max(MIN_TURN_BUDGET_S, min(MAX_TURN_BUDGET_S, int(body["turn_budget_s"])))
    if "max_rounds" in body:
        gc["max_rounds"] = max(1, min(100, int(body["max_rounds"])))
    if "max_total_s" in body:
        gc["max_total_s"] = max(300, min(86400, int(body["max_total_s"])))

    mode = gc["subagent_mode"]
    member_role = "panel" if mode == "shared_worker" else "relay"

    if isinstance(body.get("members"), list) and len(body["members"]) >= 2:
        by_ep = {m["endpoint"]: m for m in members}
        new_members = []
        seen = set()
        for item in body["members"]:
            eid = item.get("endpoint") if isinstance(item, dict) else str(item or "").strip()
            if not eid or eid in seen:
                continue
            try:
                config.endpoint(eid)
            except LookupError:
                raise ValueError(f"Unknown model endpoint: {eid}")
            seen.add(eid)
            if eid in by_ep:
                new_members.append(by_ep[eid])
            else:
                mchat = _ensure_member_chat(room, eid, member_role, mode)
                new_members.append({
                    "endpoint": eid,
                    "name": _ep_name(eid),
                    "chat_id": mchat.id,
                    "cursor": 0,
                    "role": member_role,
                    "turns": 0,
                })
        if len(new_members) >= 2:
            members = new_members

    ep_ids = [m["endpoint"] for m in members]
    if "worker" in body and body["worker"]:
        want_w = str(body["worker"]).strip()
        try:
            config.endpoint(want_w)
        except LookupError:
            raise ValueError(f"Unknown Shared Worker model endpoint: {want_w}")
        gc["worker"] = want_w
    elif not gc.get("worker") and ep_ids:
        gc["worker"] = ep_ids[0]

    for m in members:
        m["name"] = _ep_name(m["endpoint"])
        m["role"] = member_role
        try:
            store.Chat(m["chat_id"]).update_meta(touch=False, group_role=member_role)
        except Exception:
            pass

    council_ids = {m["chat_id"] for m in members}
    if mode == "shared_worker":
        wm = dict(gc["worker_member"]) if isinstance(gc.get("worker_member"), dict) else None
        if wm and wm.get("endpoint") == gc["worker"] and wm.get("chat_id") and wm["chat_id"] not in council_ids:
            wm["name"] = _ep_name(gc["worker"])
            wm["role"] = "worker"
            gc["worker_member"] = wm
        else:
            gc["worker_member"] = _make_worker_record(room, gc["worker"])

    gc["members"] = members
    updated = room.update_meta(group_chat=gc)
    _emit_state(room_id, updated)
    return public_state(room_id, updated)


def _speaker_label(member):
    nm = member.get("name") or _ep_name(member.get("endpoint") or "")
    return f"Worker ({nm})" if member.get("role") == "worker" else nm


def public_state(room_id, meta=None):
    """Public UI-friendly snapshot of the Group Chat room state."""
    room = None
    try:
        room = store.Chat(room_id)
        if meta is None:
            meta = room.meta() or {}
        if is_group(meta):
            meta = _ensure_migrated(room, meta)
    except ValueError:
        if meta is None:
            return {}
    gc = dict(meta.get("group_chat") or {})
    now = time.time()
    with _runs_lock:
        run = _runs.get(room_id)
    running = _live(run)
    w_active = bool(run and run.worker_active)
    w_started = float(run.worker_started or 0) if w_active else 0.0
    w_task = (run.worker_task or "") if w_active else ""
    w_what = (run.worker_what or "") if w_active else ""
    c_waiting = bool(run and running and run.council_sleeping)
    waiting_list = sorted(run.waiting_cids) if (run and running) else []
    started = float(gc.get("active_started") or 0)
    deadline = float(gc.get("turn_deadline") or 0)
    elapsed = round(max(0.0, now - started), 1) if (running and started and not c_waiting) else 0.0
    remaining = round(max(0.0, deadline - now), 1) if (running and deadline and not c_waiting) else 0.0
    active_cid = gc.get("active_member_chat") if (running and not c_waiting) else ""
    mode = normalize_subagent_mode(gc.get("subagent_mode"))
    wm = gc.get("worker_member") if (mode == "shared_worker" and isinstance(gc.get("worker_member"), dict)) else None
    wm_pub = None
    if wm and wm.get("chat_id"):
        wm_pub = {
            "endpoint": wm["endpoint"],
            "name": wm.get("name") or _ep_name(wm["endpoint"]),
            "chat_id": wm["chat_id"],
            "role": "worker",
            "cursor": int(wm.get("cursor") or 0),
            "turns": int(wm.get("turns") or 0),
            "active": w_active,
        }
    active_role = gc.get("active_role") if (running and not c_waiting) else ""
    if running and not c_waiting and gc.get("active_endpoint"):
        active_name = _ep_name(gc.get("active_endpoint"))
    else:
        active_name = ""
    return {
        "room_id": room_id,
        "goal": gc.get("goal") or "",
        "state": gc.get("state") or ("running" if running else "idle"),
        "state_reason": gc.get("state_reason") or "",
        "busy": running,
        "subagent_mode": mode,
        "subagent_limit": subagent_limit_for_mode(mode),
        "active_subagents": active_subagent_count(room_id),
        "worker": gc.get("worker") or "",
        "worker_member": wm_pub,
        "worker_busy": w_active,
        "worker_started": w_started,
        "worker_task": w_task,
        "worker_what": w_what,
        "council_waiting": c_waiting,
        "waiting_members": waiting_list,
        "turn_budget_s": int(gc.get("turn_budget_s") or DEFAULT_TURN_BUDGET_S),
        "max_rounds": int(gc.get("max_rounds") or DEFAULT_MAX_ROUNDS),
        "round": int(gc.get("round") or 0),
        "active_endpoint": gc.get("active_endpoint") if (running and not c_waiting) else "",
        "active_name": active_name,
        "active_role": active_role,
        "active_member_chat": active_cid,
        "active_started": started if (running and not c_waiting) else 0,
        "elapsed_s": elapsed,
        "remaining_s": remaining,
        "stale_rounds": int(gc.get("stale_rounds") or 0),
        "signoff_by": gc.get("signoff_by") or "",
        "signoff_pending": list(gc.get("signoff_pending") or []),
        "board_seq": int(gc.get("board_seq") or 0),
        "members": [
            {
                "endpoint": m["endpoint"],
                "name": m.get("name") or _ep_name(m["endpoint"]),
                "chat_id": m["chat_id"],
                "role": m.get("role") or ("panel" if mode == "shared_worker" else "relay"),
                "cursor": int(m.get("cursor") or 0),
                "turns": int(m.get("turns") or 0),
                "active": bool(running and not c_waiting and active_cid == m["chat_id"]),
                "waiting": bool(m["chat_id"] in waiting_list),
            }
            for m in (gc.get("members") or [])
        ],
    }


def _emit_state(room_id, meta=None):
    from . import agent
    st = public_state(room_id, meta)
    agent.emit(room_id, {"type": "group_state", "group": st})
    return st


# ------------------------------------------------------------------------------------------------ reply parsing & workspace diff
_ROOM_MARKER_RE = re.compile(r"\[ROOM\s*:\s*(done|need_user|need user|continue|wait)\s*\]", re.I)
_WORKER_CMD_RE = re.compile(r"^\s*\[WORKER\s*:\s*(run|steer|stop)\s*\](.*)$", re.I | re.M)
_BOARD_TAG_RE = re.compile(r"</?BOARD_POST>|\[/?BOARD_POST\]", re.I)
_LEGACY_HEADER_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(STATUS|CHANGED|VERDICT|NEXT|QUESTION|SUMMARY)(?:\*\*)?\s*:\s*(.*)$",
    re.I,
)


def _strip_legacy_headers(text):
    """Strip any leftover structured protocol header lines from a reply as a safety net.
    Returns (cleaned_text, inferred_status_or_None)."""
    lines = str(text or "").splitlines()
    matched = []
    for idx, ln in enumerate(lines):
        m = _LEGACY_HEADER_LINE_RE.match(ln)
        if m:
            matched.append((idx, m.group(1).upper(), m.group(2).strip()))

    if not matched:
        return str(text or "").strip(), None

    keys = {k for _, k, _ in matched}
    has_status_val = any(
        k == "STATUS" and re.match(r"^(done|need_user|need user|continue)\b", v, re.I)
        for _, k, v in matched
    )
    # Only strip when this is clearly a legacy protocol header block (a STATUS: continue/done/need_user line,
    # or 2+ protocol labels like CHANGED + VERDICT / NEXT).
    if not (has_status_val or len(keys - {"SUMMARY", "QUESTION"}) >= 2):
        return str(text or "").strip(), None

    inferred_status = None
    fallback_verdict = ""
    fallback_question = ""
    drop_indices = set()
    for idx, k, v in matched:
        drop_indices.add(idx)
        if k == "STATUS":
            sm = re.match(r"^(done|need_user|need user|continue)\b", v, re.I)
            if sm:
                inferred_status = sm.group(1).lower().replace(" ", "_")
        elif k == "VERDICT" and v:
            fallback_verdict = v
        elif k == "QUESTION" and v and v.lower() not in ("none", "n/a", "-"):
            fallback_question = v

    kept = [ln for idx, ln in enumerate(lines) if idx not in drop_indices]
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    if not cleaned:
        cleaned = (fallback_question if inferred_status == "need_user" and fallback_question else fallback_verdict) or ""
    return cleaned, inferred_status


def parse_board_post(raw_text, is_signoff=False):
    """Parse a member's natural prose/markdown reply and extract any trailing invisible
    [ROOM:done|need_user|wait|continue] and [WORKER:run|steer|stop] directives.
    Strips all control tags and any leftover legacy headers cleanly."""
    txt = str(raw_text or "").strip()
    status = None
    matches = list(_ROOM_MARKER_RE.finditer(txt))
    if matches:
        raw_st = matches[-1].group(1).strip().lower().replace(" ", "_")
        if raw_st in ("done", "need_user", "continue", "wait"):
            status = raw_st

    worker_cmds = []
    for wm in _WORKER_CMD_RE.finditer(txt):
        act = wm.group(1).strip().lower()
        arg = _ROOM_MARKER_RE.sub("", wm.group(2) or "").strip()
        worker_cmds.append((act, arg))

    clean = _WORKER_CMD_RE.sub("", txt)
    clean = _ROOM_MARKER_RE.sub("", clean)
    clean = _BOARD_TAG_RE.sub("", clean).strip()
    clean, legacy_st = _strip_legacy_headers(clean)
    if status is None and legacy_st in ("done", "need_user", "continue", "wait"):
        status = legacy_st
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()

    # If a model wrote bare [WORKER:run] / [WORKER:steer] with no inline payload, use the clean reply text as the task
    if worker_cmds:
        resolved = []
        for act, arg in worker_cmds:
            if act in ("run", "steer") and not arg and clean:
                arg = clean
            resolved.append((act, arg))
        worker_cmds = resolved

    if status is None:
        low = clean[-300:].lower()
        if is_signoff and any(k in low for k in ("looks good", "lgtm", "approved", "agree", "verified", "all good", "complete")) \
                and not any(k in low for k in ("not done", "missing", "broken", "object", "still need", "fail")):
            status = "done"
        else:
            status = "continue"

    if not clean:
        if worker_cmds:
            act0, arg0 = worker_cmds[-1]
            clean = f"(Sent {act0} to Shared Worker{': ' + arg0[:120] if arg0 else ''})"
        elif status == "wait":
            clean = "(Waiting for Shared Worker.)"
        else:
            clean = "(Finished without a text reply; open Trace to inspect steps.)"

    non_empty = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    short_line = re.sub(r"[*_`#]+", "", non_empty[-1] if non_empty else clean).strip()[:160]
    question = short_line if status == "need_user" else ""
    verdict = re.sub(r"[*_`#]+", "", non_empty[0] if non_empty else clean).strip()[:160]

    return {
        "status": status,
        "changed": "",
        "verdict": verdict,
        "next_step": "",
        "question": question,
        "summary": clean,
        "worker_cmds": worker_cmds,
    }


def _workspace_fingerprint(project_dir):
    """Fast, bounded fingerprint of workspace state (git status + diff hash, or top-level mtimes) for the anti-loop fuse."""
    p = Path(project_dir or config.WORKSPACE)
    if not p.is_dir():
        return "", ""
    # Prefer git status + diff stat when inside a git repo
    if (p / ".git").exists():
        try:
            st = subprocess.run(
                ["git", "-C", str(p), "status", "--porcelain"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, creationflags=osutil.NOWIN,
            )
            if st.returncode == 0:
                df = subprocess.run(
                    ["git", "-C", str(p), "diff", "--stat"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, creationflags=osutil.NOWIN,
                )
                raw = (st.stdout or "") + "\n---\n" + (df.stdout or "")
                # Also hash mtimes of modified/untracked files so edits inside an already-dirty file change the hash
                mt = []
                for line in (st.stdout or "").splitlines()[:40]:
                    rel = line[3:].strip().strip('"')
                    fp = p / rel
                    try:
                        s = fp.stat()
                        mt.append(f"{rel}:{s.st_size}:{s.st_mtime_ns}")
                    except OSError:
                        pass
                sig = hashlib.sha1((raw + "\n".join(mt)).encode("utf-8", "replace")).hexdigest()[:16]
                summary = (df.stdout or st.stdout or "").strip()[:1200]
                return sig, summary
        except Exception:
            pass
    # Fallback: scan up to 600 non-ignored files for size + mtime
    items = []
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    win_reserved = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
    for root, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for fn in files:
            if fn.split(".", 1)[0].lower() in win_reserved:
                continue
            fp = os.path.join(root, fn)
            try:
                s = os.stat(fp)
                rel = os.path.relpath(fp, p)
                items.append(f"{rel}:{s.st_size}:{s.st_mtime_ns}")
            except (OSError, ValueError):
                pass
            if len(items) >= 600:
                break
        if len(items) >= 600:
            break
    items.sort()
    return hashlib.sha1("\n".join(items).encode("utf-8", "replace")).hexdigest()[:16], ""


def _collect_turn_trace(member_chat, before_count):
    """Collect a compact, ordered trace of thinking blocks and tool calls (with outputs/diffs) from one member turn
    so every Group Chat message can expand thinking & tool calls inline just like a normal chat."""
    msgs = member_chat.messages()[max(0, int(before_count or 0)):]
    tool_results = {}
    for m in msgs:
        if m.get("role") == "tool" and m.get("tool_call_id"):
            tool_results[m["tool_call_id"]] = m
    trace = []
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        rc = str(m.get("reasoning_content") or "").strip()
        if rc:
            trace.append({"kind": "thinking", "text": rc[:8000]})
        for tc in (m.get("tool_calls") or []):
            cid = str(tc.get("id") or "")
            tr = tool_results.get(cid)
            item = {
                "kind": "tool",
                "id": cid,
                "name": str(tc.get("name") or "tool"),
                "arguments": str(tc.get("arguments") or "")[:4000],
            }
            if tr is not None:
                item["output"] = str(tr.get("content") or "")[:12000]
                item["ok"] = bool(tr.get("ok", True))
                if isinstance(tr.get("ui"), dict) and tr["ui"].get("diff"):
                    item["diff"] = tr["ui"]["diff"]
            trace.append(item)
        for p in (m.get("parts") or []):
            if not isinstance(p, dict):
                continue
            pk = p.get("kind")
            if pk == "thinking" and str(p.get("text") or "").strip():
                trace.append({"kind": "thinking", "text": str(p["text"]).strip()[:8000]})
            elif pk == "tool":
                item = {
                    "kind": "tool",
                    "id": str(p.get("id") or ""),
                    "name": str(p.get("name") or "tool"),
                    "arguments": str(p.get("input") or "")[:4000],
                }
                if "output" in p and p["output"] is not None:
                    item["output"] = str(p["output"])[:12000]
                    item["ok"] = not bool(p.get("error"))
                if p.get("diff"):
                    item["diff"] = p["diff"]
                trace.append(item)
    return trace[:120]


def _changed_files_in_member_turn(member_chat, before_count):
    """List files written/edited and tool count from the member's newest messages."""
    msgs = member_chat.messages()[before_count:]
    files = []
    tools_n = 0
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            tools_n += 1
            if tc.get("name") in ("Write", "Edit"):
                try:
                    fp = json.loads(tc.get("arguments") or "{}").get("file_path") or ""
                    if fp and fp not in files:
                        files.append(fp)
                except Exception:
                    pass
        for p in (m.get("parts") or []):
            if isinstance(p, dict) and p.get("kind") == "tool":
                tools_n += 1
    return files, tools_n


def _latest_member_reply(member_chat, before_count):
    msgs = member_chat.messages()[before_count:]
    last_id = ""
    stats = {}
    texts = []
    for m in msgs:
        if m.get("role") == "assistant":
            last_id = m.get("id") or last_id
            if m.get("stats"):
                stats = m["stats"]
            t = (m.get("content") or "").strip()
            if not t and m.get("parts"):
                t = "\n".join(str(p.get("text") or "").strip() for p in m["parts"] if isinstance(p, dict) and p.get("kind") == "text").strip()
            if t:
                texts.append(t)
    return ("\n\n".join(texts[-2:]) if texts else ""), last_id, stats


def _fmt_dur(sec):
    s = max(0, int(round(float(sec or 0))))
    m, r = divmod(s, 60)
    return f"{m}m {r:02d}s" if m else f"{r}s"


def _recent_worker_tools(chat_id, before_n, limit=2):
    """Return last 1-2 tool calls from the Shared Worker's active turn as compact strings."""
    if not chat_id:
        return []
    try:
        msgs = store.Chat(chat_id).messages()[max(0, int(before_n or 0)):]
    except Exception:
        return []
    out = []
    for m in msgs:
        for tc in (m.get("tool_calls") or []):
            nm = str(tc.get("name") or "").strip()
            if not nm:
                continue
            brief = ""
            try:
                args = json.loads(tc.get("arguments") or "{}")
                for k in ("file_path", "path", "command", "pattern", "query"):
                    v = args.get(k)
                    if v:
                        brief = str(v).replace("\n", " ").strip()[:36]
                        break
            except Exception:
                pass
            out.append(f"{nm}({brief})" if brief else nm)
        for p in (m.get("parts") or []):
            if isinstance(p, dict) and p.get("kind") == "tool":
                nm = str(p.get("name") or p.get("title") or "tool").strip()
                out.append(nm[:40])
    return out[-limit:]


def _worker_status_snapshot(run, gc, delta_seqs):
    """Compact 1-2 line live Shared Worker status snapshot injected at the start of each Council turn."""
    wm = gc.get("worker_member")
    if not isinstance(wm, dict) or not wm.get("chat_id"):
        return ""
    wname = wm.get("name") or _ep_name(wm.get("endpoint") or "")
    if run and run.worker_active:
        elap = _fmt_dur(time.time() - (run.worker_started or time.time()))
        task_part = f" - {run.worker_task[:100]}" if run.worker_task else ""
        line1 = f"Worker ({wname}): running ({elap}){task_part}"
        recent = _recent_worker_tools(wm["chat_id"], run.worker_before_n, limit=2)
        act_bits = []
        if run.worker_what:
            act_bits.append(run.worker_what[:90])
        if recent:
            tools_txt = ", ".join(recent)
            if not act_bits or tools_txt not in act_bits[0]:
                act_bits.append(f"recent tools: {tools_txt}")
        return f"{line1}\nWorker activity: {' | '.join(act_bits)}" if act_bits else line1
    if run and run.worker_last_done_at > 0:
        ago = _fmt_dur(time.time() - run.worker_last_done_at)
        t_info = f", {run.worker_last_tools} tool{'s' if run.worker_last_tools != 1 else ''}" if run.worker_last_tools else ""
        line1 = f"Worker ({wname}): idle (finished {ago} ago{t_info})."
        # Only add a compact 1-line summary if the Worker's output wasn't already included in this turn's delta messages
        if run.worker_last_summary and run.worker_last_seq not in delta_seqs:
            return f"{line1}\nLast output: {run.worker_last_summary[:140]}"
        return line1
    return f"Worker ({wname}): idle."


# ------------------------------------------------------------------------------------------------ prompt builder (delta only!)
def _member_prompt(room, gc, member, is_signoff=False, run=None):
    """Build a minimal delta-only prompt for this member's turn.
    First turn: 2 short plain lines naming who is in the chat plus optional hidden tags.
    Subsequent turns: ONLY the plain new messages since this member's cursor (plus a compact 1-2 line
    Shared Worker status snapshot for Council turns in shared_worker mode)."""
    cursor = int(member.get("cursor") or 0)
    first_turn = int(member.get("turns") or 0) == 0
    mode = normalize_subagent_mode(gc.get("subagent_mode"))
    role = member.get("role") or "relay"
    council_names = ", ".join(m["name"] for m in (gc.get("members") or []))
    wm = gc.get("worker_member") if (mode == "shared_worker" and isinstance(gc.get("worker_member"), dict)) else None

    # Collect ONLY plain new messages after this member's cursor (sanitizing any legacy room posts)
    delta_lines = []
    delta_seqs = set()
    max_seq = cursor
    for msg in room.messages():
        seq = int(msg.get("board_seq") or 0)
        if seq <= cursor:
            continue
        if seq > max_seq:
            max_seq = seq
        delta_seqs.add(seq)
        if msg.get("role") == "user":
            delta_lines.append(f"User:\n{msg.get('content') or ''}")
        elif msg.get("role") == "group_post":
            who = msg.get("member_name") or msg.get("member_endpoint") or "Model"
            body = _BOARD_TAG_RE.sub("", str(msg.get("content") or ""))
            body, _ = _strip_legacy_headers(body)
            if body:
                delta_lines.append(f"{who}:\n{body}")

    parts = []
    if first_turn:
        if mode == "shared_worker" and wm:
            if role == "worker":
                parts.append(
                    f"Group chat with {council_names}. You are the Shared Worker ({wm['name']}), running tasks in the background.\n"
                    "Reply naturally with your results when finished."
                )
            else:
                parts.append(
                    f"Group chat with {council_names}. Shared Worker: {wm['name']} (runs in the background without blocking you).\n"
                    "Reply naturally. Optional hidden tags on their own last lines: [WORKER:run] <task>, [WORKER:steer] <note>, [WORKER:stop], [ROOM:wait] (sleep if everyone waits), [ROOM:need_user], or [ROOM:done]."
                )
        else:
            parts.append(
                f"Group chat with {council_names}.\n"
                "Reply naturally. Only if needed, put [ROOM:need_user] (waiting on the user) or [ROOM:done] (finished) on its own last line."
            )

    if mode == "shared_worker" and role != "worker" and wm:
        snap = _worker_status_snapshot(run, gc, delta_seqs)
        if snap:
            parts.append(snap)

    if delta_lines:
        parts.append("\n\n".join(delta_lines))
    elif not is_signoff:
        parts.append("Your turn.")

    if is_signoff:
        proposer = gc.get("signoff_by") or "The previous model"
        parts.append(f"{proposer} marked this done. Reply naturally; end with [ROOM:done] on a new line if you agree.")

    return "\n\n".join(parts), max_seq


# ------------------------------------------------------------------------------------------------ orchestrator loop
def _notify_room(room, title_suffix, text, msg_id="", is_error=False):
    """Desktop room state transitions are already streamed to the UI over SSE."""
    return None


def _append_notice(room, text):
    from . import agent
    m = room.append({"role": "notice", "content": text})
    agent.emit(room.id, {"type": "message", "message": agent._public(m)})
    return m


def _set_room_state(room, **updates):
    with _meta_lock(room.id):
        meta = room.meta() or {}
        gc = dict(meta.get("group_chat") or {})
        gc.update(updates)
        updated = room.update_meta(group_chat=gc)
    _emit_state(room.id, updated)
    return updated


def _record_post(room, member, res, cur_round, is_signoff=False):
    """Atomically append a group_post and advance this speaker's cursor/turns (safe for concurrent Council + Worker)."""
    from . import agent
    with _meta_lock(room.id):
        meta = room.meta() or {}
        gc = dict(meta.get("group_chat") or {})
        seq = int(gc.get("board_seq") or 0) + 1
        who_lbl = _speaker_label(member)
        post = room.append({
            "role": "group_post",
            "board_seq": seq,
            "round": cur_round,
            "member_endpoint": member["endpoint"],
            "member_name": who_lbl,
            "member_role": member.get("role") or "relay",
            "member_chat_id": member["chat_id"],
            "member_msg_id": res["msg_id"],
            "status": res["status"],
            "changed": res["changed"],
            "verdict": res["verdict"],
            "next_step": res["next_step"],
            "question": res["question"],
            "content": res["summary"],
            "secs": res["elapsed"],
            "stats": res["stats"],
            "tool_count": res["tools_n"],
            "files_changed": res["files_changed"],
            "trace": res.get("trace") or [],
            "nudged": res["nudged"],
            "is_signoff": is_signoff,
        })
        members, wm = _bump_speaker_cursor(gc, member["chat_id"], seq)
        gc["members"] = members
        gc["worker_member"] = wm
        gc["board_seq"] = seq
        room.update_meta(group_chat=gc)
    agent.emit(room.id, {"type": "message", "message": agent._public(post)})
    return post, seq, members, wm, who_lbl


def _worker_lane(run, room_id, task_text, assigned_by=""):
    """Independent asynchronous Shared Worker lane. Runs in the background without ever blocking Council turns."""
    from . import agent
    room = store.Chat(room_id)
    meta = room.meta() or {}
    gc = dict(meta.get("group_chat") or {})
    wm = dict(gc["worker_member"]) if isinstance(gc.get("worker_member"), dict) else None
    if not wm or not wm.get("chat_id"):
        with run.worker_lock:
            run.worker_active = False
        run.wake_ev.set()
        return

    wchat_id = wm["chat_id"]
    wchat = store.Chat(wchat_id)
    before_n = len(wchat.messages())
    t0 = time.time()
    budget_s = int(gc.get("turn_budget_s") or DEFAULT_TURN_BUDGET_S)
    deadline = t0 + budget_s
    who_label = _speaker_label(wm)

    # Build minimal delta prompt for the Worker + explicit task assignment
    base_prompt, _ = _member_prompt(room, gc, wm, is_signoff=False, run=run)
    task_clean = str(task_text or "").strip()
    if task_clean:
        header = f"Task from {assigned_by}:\n{task_clean}" if assigned_by else f"Task:\n{task_clean}"
        prompt = f"{base_prompt}\n\n{header}" if base_prompt and base_prompt != "Your turn." else header
    else:
        prompt = base_prompt

    acquire_subagent_slot(room_id, "shared_worker", SHARED_WORKER_MAX_SUBAGENTS)
    _register_live_member(wchat_id, room_id, wm, before_n)
    with run.worker_lock:
        run.worker_active = True
        run.worker_started = t0
        run.worker_before_n = before_n
        run.worker_task = task_clean[:140]
        run.worker_what = ""
        run.worker_stop_ev.clear()
        run.worker_ran_since_check = True
    _emit_state(room_id)

    forced_stop = False
    try:
        agent.start(wchat_id, prompt, files=[], endpoint=wm["endpoint"])
        time.sleep(0.15)
        last_tick = 0.0
        while True:
            if run.stop_ev.is_set() or run.worker_stop_ev.is_set():
                agent.stop(wchat_id)
                forced_stop = True
                break
            if not agent.busy(wchat_id):
                break
            now = time.time()
            if now >= deadline + GRACE_AFTER_BUDGET_S:
                agent.stop(wchat_id)
                forced_stop = True
                break
            if now - last_tick >= 1.5:
                last_tick = now
                act = agent.activities([wchat_id]).get(wchat_id) or {}
                line = act.get("what") or act.get("line") or ""
                if line:
                    run.worker_what = line[:140]
                    agent.emit(room_id, {"type": "group_activity", "member": wm["endpoint"], "chat_id": wchat_id,
                                         "name": who_label, "role": "worker",
                                         "what": line[:140], "elapsed_s": round(now - t0, 1),
                                         "remaining_s": round(max(0.0, deadline - now), 1)})
            time.sleep(0.25)

        for _ in range(25):
            if not agent.busy(wchat_id):
                break
            time.sleep(0.15)
    finally:
        _unregister_live_member(wchat_id)
        release_subagent_slot(room_id, "shared_worker")

    elapsed = round(time.time() - t0, 1)
    reply_text, msg_id, stats = _latest_member_reply(wchat, before_n)
    files_changed, tools_n = _changed_files_in_member_turn(wchat, before_n)
    trace = _collect_turn_trace(wchat, before_n)
    parsed = parse_board_post(reply_text, is_signoff=False)
    res = {
        "reply": reply_text,
        "msg_id": msg_id,
        "stats": stats,
        "files_changed": files_changed,
        "tools_n": tools_n,
        "trace": trace,
        "elapsed": elapsed,
        "nudged": False,
        "forced_stop": forced_stop,
        **parsed,
    }

    post_seq = 0
    if not run.stop_ev.is_set() and (reply_text.strip() or tools_n or files_changed or not forced_stop):
        cur_round = int((room.meta() or {}).get("group_chat", {}).get("round") or 1)
        _, post_seq, _, _, _ = _record_post(room, wm, res, cur_round, is_signoff=False)

    with run.worker_lock:
        run.worker_active = False
        run.worker_what = ""
        run.worker_last_done_at = time.time()
        run.worker_last_summary = (res["verdict"] or res["summary"])[:160]
        run.worker_last_tools = tools_n
        run.worker_last_files = list(files_changed)
        run.worker_last_seq = post_seq
        run.worker_ran_since_check = True
        run.worker_stop_ev.clear()
    _emit_state(room_id)
    # Wake Council immediately if it was in zero-token sleep waiting for the Worker!
    run.wake_ev.set()


def _start_async_worker(run, room, task_text, assigned_by=""):
    """Start the background Shared Worker lane (or soft-steer it if already running)."""
    with run.worker_lock:
        already = run.worker_active
    if already:
        return _steer_async_worker(run, room, task_text, from_who=assigned_by)
    t = threading.Thread(target=_worker_lane, args=(run, room.id, task_text, assigned_by), daemon=True)
    with run.worker_lock:
        run.worker_thread = t
        run.worker_active = True
        run.worker_started = time.time()
        run.worker_task = str(task_text or "").strip()[:140]
        run.worker_stop_ev.clear()
        run.worker_ran_since_check = True
    t.start()
    return True


def _steer_async_worker(run, room, note_text, from_who=""):
    """Soft-steer the currently running Shared Worker mid-run (or start it if idle)."""
    from . import agent
    with run.worker_lock:
        active = run.worker_active
    if not active:
        return _start_async_worker(run, room, note_text, assigned_by=from_who)
    wm = ((room.meta() or {}).get("group_chat") or {}).get("worker_member")
    if not isinstance(wm, dict) or not wm.get("chat_id"):
        return False
    msg = f"{from_who}:\n{note_text}" if from_who else str(note_text or "")
    agent.start(wm["chat_id"], msg, files=[], soft=True)
    run.worker_what = f"Steered by {from_who}" if from_who else "Steered"
    _emit_state(room.id)
    return True


def _stop_async_worker(run, room):
    """Stop/cancel the currently running background Shared Worker."""
    from . import agent
    wm = ((room.meta() or {}).get("group_chat") or {}).get("worker_member")
    run.worker_stop_ev.set()
    if isinstance(wm, dict) and wm.get("chat_id"):
        try:
            agent.stop(wm["chat_id"])
        except Exception:
            pass
    run.wake_ev.set()
    return True


def _apply_worker_cmds(run, room, gc, worker_cmds, from_who):
    """Execute lightweight Council commands ([WORKER:run|steer|stop]) in shared_worker mode."""
    if not worker_cmds or normalize_subagent_mode(gc.get("subagent_mode")) != "shared_worker":
        return False
    did = False
    for act, arg in worker_cmds:
        if act == "stop":
            _stop_async_worker(run, room)
            did = True
        elif act == "steer" and arg:
            _steer_async_worker(run, room, arg, from_who=from_who)
            did = True
        elif act == "run" and arg:
            _start_async_worker(run, room, arg, assigned_by=from_who)
            did = True
    return did


def _run_member_turn(run, room, member, prompt, budget_s, is_signoff=False):
    """Run one member's turn in its persistent native chat, with 80% gentle nudge and early yield.
    When the dedicated Shared Worker sub-agent runs, tracks it in the room's 1 Shared Worker slot."""
    from . import agent
    mchat_id = member["chat_id"]
    mchat = store.Chat(mchat_id)
    before_n = len(mchat.messages())
    t0 = time.time()
    deadline = t0 + budget_s
    nudge_at = t0 + max(15.0, min(budget_s * NUDGE_FRACTION, budget_s - 60.0 if budget_s >= 180 else budget_s * NUDGE_FRACTION))
    is_worker = member.get("role") == "worker"
    who_label = _speaker_label(member)

    if is_worker:
        acquire_subagent_slot(room.id, "shared_worker", SHARED_WORKER_MAX_SUBAGENTS)
    _register_live_member(mchat_id, room.id, member, before_n)

    try:
        _set_room_state(
            room,
            state="running",
            active_endpoint=member["endpoint"],
            active_member_chat=mchat_id,
            active_role=member.get("role") or "relay",
            active_started=t0,
            turn_deadline=deadline,
            state_reason=f"{who_label} {'reviewing sign-off' if is_signoff else 'working'}...",
        )

        agent.start(mchat_id, prompt, files=[], endpoint=member["endpoint"])
        nudged = False
        forced_stop = False

        # Wait for member turn to start and complete naturally (early yield) or hit soft budget / baton pass / stop
        time.sleep(0.2)
        last_tick = 0.0
        while True:
            if run.stop_ev.is_set():
                agent.stop(mchat_id)
                forced_stop = True
                break
            if run.force_pass_ev.is_set():
                run.force_pass_ev.clear()
                run.pass_ev.clear()
                agent.stop(mchat_id)
                forced_stop = True
                break
            if not agent.busy(mchat_id):
                break
            now = time.time()
            if (run.pass_ev.is_set() or now >= nudge_at) and not nudged:
                nudged = True
                left_s = max(10, int(deadline - now))
                why_nudge = "The user passed the baton." if run.pass_ev.is_set() else f"About {max(1, round(left_s / 60))} min left."
                run.pass_ev.clear()
                nudge_txt = f"{why_nudge} Please wrap up and post your reply now."
                agent.start(mchat_id, nudge_txt, files=[], soft=True)
                _set_room_state(room, state_reason=f"Wrap-up nudge sent to {who_label}")
            if now >= deadline + GRACE_AFTER_BUDGET_S:
                agent.stop(mchat_id)
                forced_stop = True
                break
            if now - last_tick >= 2.0:
                last_tick = now
                # Broadcast live member activity line to the room header
                act = agent.activities([mchat_id]).get(mchat_id) or {}
                line = act.get("what") or act.get("line") or ""
                if line:
                    agent.emit(room.id, {"type": "group_activity", "member": member["endpoint"], "chat_id": mchat_id,
                                         "name": who_label, "role": member.get("role") or "relay",
                                         "what": line[:140], "elapsed_s": round(now - t0, 1),
                                         "remaining_s": round(max(0.0, deadline - now), 1)})
            time.sleep(0.35)

        # Wait briefly for agent thread to flush its final assistant message
        for _ in range(25):
            if not agent.busy(mchat_id):
                break
            time.sleep(0.2)
    finally:
        _unregister_live_member(mchat_id)
        if is_worker:
            release_subagent_slot(room.id, "shared_worker")

    elapsed = round(time.time() - t0, 1)
    reply_text, msg_id, stats = _latest_member_reply(mchat, before_n)
    files_changed, tools_n = _changed_files_in_member_turn(mchat, before_n)
    trace = _collect_turn_trace(mchat, before_n)
    parsed = parse_board_post(reply_text, is_signoff=is_signoff)
    return {
        "reply": reply_text,
        "msg_id": msg_id,
        "stats": stats,
        "files_changed": files_changed,
        "tools_n": tools_n,
        "trace": trace,
        "elapsed": elapsed,
        "nudged": nudged,
        "forced_stop": forced_stop,
        **parsed,
    }


def _speaker_by_chat_id(gc, cid):
    wm = gc.get("worker_member")
    if isinstance(wm, dict) and wm.get("chat_id") == cid:
        return dict(wm)
    for m in (gc.get("members") or []):
        if m.get("chat_id") == cid:
            return dict(m)
    # Fallback for legacy signoff entries that stored an endpoint id
    for m in (gc.get("members") or []):
        if m.get("endpoint") == cid:
            return dict(m)
    return None


def _bump_speaker_cursor(gc, chat_id, seq):
    """Advance cursor and turn count strictly by chat_id so Council and Worker sessions stay 100% separate."""
    members = [dict(m) for m in (gc.get("members") or [])]
    wm = dict(gc["worker_member"]) if isinstance(gc.get("worker_member"), dict) else None
    if wm and wm.get("chat_id") == chat_id:
        wm["cursor"] = seq
        wm["turns"] = int(wm.get("turns") or 0) + 1
    else:
        for m in members:
            if m.get("chat_id") == chat_id:
                m["cursor"] = seq
                m["turns"] = int(m.get("turns") or 0) + 1
                break
    return members, wm


def _build_turn_queue(gc):
    """Return ordered list of Council/Relay speaker chat_ids for one round.
    In shared_worker mode, the Shared Worker runs on its own independent async lane and NEVER blocks the Council."""
    members = gc.get("members") or []
    return [m["chat_id"] for m in members if m.get("chat_id")]


def _room_loop(run, room_id):
    from . import agent
    room = store.Chat(room_id)
    try:
        while not run.stop_ev.is_set():
            meta = _ensure_migrated(room)
            gc = dict(meta.get("group_chat") or {})
            members = [dict(m) for m in (gc.get("members") or [])]
            if not members:
                _set_room_state(room, state="stopped", state_reason="No models configured")
                break

            max_rounds = int(gc.get("max_rounds") or DEFAULT_MAX_ROUNDS)
            max_total_s = int(gc.get("max_total_s") or DEFAULT_MAX_TOTAL_S)
            if time.time() - run.started >= max_total_s:
                reason = f"Auto-paused: reached total room time cap ({round(max_total_s / 60)} min)."
                _append_notice(room, reason)
                _set_room_state(room, state="paused", state_reason=reason, active_endpoint="", active_member_chat="", active_role="")
                _notify_room(room, "Paused", reason)
                break

            # Handle sign-off pass first if someone already proposed `done`
            signoff_pending = list(gc.get("signoff_pending") or [])
            if signoff_pending:
                next_cid = signoff_pending[0]
                member = _speaker_by_chat_id(gc, next_cid)
                if member is None:
                    signoff_pending.pop(0)
                    _set_room_state(room, signoff_pending=signoff_pending)
                    continue
                budget = min(int(gc.get("turn_budget_s") or DEFAULT_TURN_BUDGET_S), SIGNOFF_BUDGET_CAP_S)
                prompt, _ = _member_prompt(room, gc, member, is_signoff=True, run=run)
                res = _run_member_turn(run, room, member, prompt, budget, is_signoff=True)
                if run.stop_ev.is_set():
                    break

                # Record board post + advance this speaker's cursor atomically
                post, seq, members, wm, who_lbl = _record_post(room, member, res, int(gc.get("round") or 1), is_signoff=True)
                _apply_worker_cmds(run, room, gc, res.get("worker_cmds"), who_lbl)

                if res["status"] == "need_user":
                    reason = res["question"] or f"{who_lbl} needs your input during sign-off."
                    _set_room_state(room, signoff_by="", signoff_pending=[],
                                    state="need_user", state_reason=reason, active_endpoint="", active_member_chat="", active_role="")
                    _notify_room(room, "Needs you", f"{who_lbl}: {reason}", msg_id=post["id"])
                    break
                if res["status"] == "done":
                    rem = [c for c in signoff_pending if c != next_cid and c != member["chat_id"]]
                    if not rem:
                        reason = "Complete"
                        _set_room_state(room, signoff_by="", signoff_pending=[],
                                        state="done", state_reason=reason, active_endpoint="", active_member_chat="", active_role="")
                        _notify_room(room, "Done", res["verdict"] or res["summary"], msg_id=post["id"])
                        break
                    _set_room_state(room, signoff_pending=rem)
                    continue
                # Member objected -> cancel sign-off and keep working!
                _set_room_state(room, signoff_by="", signoff_pending=[])
                if run.pause_ev.is_set():
                    _set_room_state(room, state="paused", state_reason="Paused by user.", active_endpoint="", active_member_chat="", active_role="")
                    break
                continue

            # Normal round execution
            cur_round = int(gc.get("round") or 0) + 1
            if cur_round > max_rounds:
                reason = f"Auto-paused: reached max rounds ({max_rounds}). Send a message or click Resume to continue."
                _append_notice(room, reason)
                _set_room_state(room, state="paused", state_reason=reason, active_endpoint="", active_member_chat="", active_role="")
                _notify_room(room, "Paused", reason)
                break

            queue_ids = _build_turn_queue(gc)
            start_pos = int(gc.get("active_idx") or 0) % len(queue_ids)
            round_fp_before, _ = _workspace_fingerprint(meta.get("project"))
            round_files_touched = []
            round_user_steers = int(gc.get("board_seq") or 0)
            run.worker_ran_since_check = False
            stop_room = False
            woke_from_sleep = False

            _set_room_state(room, round=cur_round)

            for pos in range(start_pos, len(queue_ids)):
                if run.stop_ev.is_set():
                    stop_room = True
                    break
                meta = _ensure_migrated(room)
                gc = dict(meta.get("group_chat") or {})
                member = _speaker_by_chat_id(gc, queue_ids[pos])
                if member is None:
                    continue
                budget = int(gc.get("turn_budget_s") or DEFAULT_TURN_BUDGET_S)
                prompt, _ = _member_prompt(room, gc, member, is_signoff=False, run=run)
                res = _run_member_turn(run, room, member, prompt, budget, is_signoff=False)
                if run.stop_ev.is_set():
                    stop_room = True
                    break

                post, seq, members, wm, who_lbl = _record_post(room, member, res, cur_round, is_signoff=False)
                round_files_touched.extend(res["files_changed"])
                _apply_worker_cmds(run, room, gc, res.get("worker_cmds"), who_lbl)

                next_pos = (pos + 1) % len(queue_ids)
                if res["status"] == "need_user":
                    run.waiting_cids.clear()
                    reason = res["question"] or f"{who_lbl} paused and needs your input."
                    _set_room_state(room, active_idx=next_pos,
                                    state="need_user", state_reason=reason, active_endpoint="", active_member_chat="", active_role="")
                    _notify_room(room, "Needs you", f"{who_lbl}: {reason}", msg_id=post["id"])
                    stop_room = True
                    break

                if res["status"] == "done":
                    run.waiting_cids.clear()
                    total_turns_done = sum(int(m.get("turns") or 0) for m in members) + (int(wm.get("turns") or 0) if wm else 0)
                    did_work = bool(round_files_touched) or bool(res["files_changed"]) or (res["tools_n"] > 0) or (total_turns_done > 1) or run.worker_ran_since_check
                    others = [m["chat_id"] for m in members if m["chat_id"] != member["chat_id"]] if did_work else []
                    if not others:
                        reason = "Complete"
                        _set_room_state(room, active_idx=0,
                                        state="done", state_reason=reason, active_endpoint="", active_member_chat="", active_role="")
                        _notify_room(room, "Done", res["verdict"] or res["summary"], msg_id=post["id"])
                        stop_room = True
                        break
                    _set_room_state(room, active_idx=next_pos, signoff_by=who_lbl, signoff_pending=others)
                    break

                if res["status"] == "wait":
                    run.waiting_cids.add(member["chat_id"])
                    if set(queue_ids).issubset(run.waiting_cids):
                        if run.worker_active:
                            # Unanimous wait while Shared Worker is running -> zero-token sleep until Worker finishes or user writes!
                            w_name = _speaker_label(wm) if isinstance(wm, dict) else "Shared Worker"
                            run.council_sleeping = True
                            run.wake_ev.clear()
                            _set_room_state(
                                room, active_idx=0, state="running",
                                state_reason=f"All models waiting for {w_name}...",
                                active_endpoint="", active_member_chat="", active_role="",
                            )
                            while run.worker_active and not run.stop_ev.is_set() and not run.pause_ev.is_set():
                                if time.time() - run.started >= max_total_s:
                                    break
                                if run.wake_ev.wait(timeout=0.5):
                                    break
                            run.council_sleeping = False
                            run.waiting_cids.clear()
                            run.wake_ev.clear()
                            woke_from_sleep = True
                            if run.stop_ev.is_set():
                                stop_room = True
                            elif run.pause_ev.is_set():
                                _append_notice(room, "Room paused cleanly.")
                                _set_room_state(room, state="paused", state_reason="Paused by user.", active_endpoint="", active_member_chat="", active_role="")
                                stop_room = True
                            break
                        else:
                            # Every Council member chose wait while Worker is idle -> pause cleanly for user
                            run.waiting_cids.clear()
                            reason = "All models waiting (Shared Worker is idle)."
                            _set_room_state(room, active_idx=next_pos, state="need_user", state_reason=reason,
                                            active_endpoint="", active_member_chat="", active_role="")
                            _notify_room(room, "Waiting", reason, msg_id=post["id"])
                            stop_room = True
                            break
                else:
                    # Non-wait reply resets unanimous wait cleanly (zero anti-chatter)
                    run.waiting_cids.clear()

                _set_room_state(room, active_idx=next_pos)
                if run.pause_ev.is_set():
                    _append_notice(room, "Room paused cleanly after turn.")
                    _set_room_state(room, state="paused", state_reason="Paused by user.", active_endpoint="", active_member_chat="", active_role="")
                    stop_room = True
                    break

            if stop_room:
                break

            # If sign-off started or Council just woke from zero-token sleep on Worker completion, loop straight in
            if woke_from_sleep or (room.meta() or {}).get("group_chat", {}).get("signoff_pending"):
                _set_room_state(room, active_idx=0, stale_rounds=0)
                continue

            # Reset active_idx to 0 for next round; in shared_worker mode never apply anti-chatter / stale-round pause
            mode_now = normalize_subagent_mode((room.meta() or {}).get("group_chat", {}).get("subagent_mode"))
            if mode_now == "shared_worker" or run.worker_active or run.worker_ran_since_check:
                _set_room_state(room, active_idx=0, stale_rounds=0)
                continue

            round_fp_after, _ = _workspace_fingerprint((room.meta() or {}).get("project"))
            meta = room.meta() or {}
            gc = dict(meta.get("group_chat") or {})
            user_steered = any(
                m.get("role") == "user" and int(m.get("board_seq") or 0) > round_user_steers
                for m in room.messages()[-20:]
            )
            changed = bool(round_files_touched) or (round_fp_after != round_fp_before) or user_steered
            stale = 0 if changed else int(gc.get("stale_rounds") or 0) + 1
            if stale >= STALE_ROUND_LIMIT:
                reason = f"Auto-paused (anti-loop fuse): {stale} consecutive rounds with no workspace changes. Steer or click Resume to continue."
                _append_notice(room, reason)
                _set_room_state(room, active_idx=0, stale_rounds=stale, state="paused",
                                state_reason=reason, active_endpoint="", active_member_chat="")
                _notify_room(room, "Paused", reason)
                break
            _set_room_state(room, active_idx=0, stale_rounds=stale)
    except Exception as e:
        import traceback
        try:
            (config.LOGS / "errors.log").open("a", encoding="utf-8").write(time.strftime("%F %T ") + f"group {room_id}: " + traceback.format_exc() + "\n")
        except OSError:
            pass
        m = room.append({"role": "error", "content": f"Group room error: {e.__class__.__name__}: {e}"})
        agent.emit(room_id, {"type": "message", "message": agent._public(m)})
        _set_room_state(room, state="paused", state_reason=f"Error: {e}", active_endpoint="", active_member_chat="")
    finally:
        with run.end_lock:
            run.ended = True
        if run.stop_ev.is_set():
            _set_room_state(room, state="stopped", state_reason="Stopped by user.", active_endpoint="", active_member_chat="")
        meta_end = room.meta() or {}
        agent.emit(room_id, {"type": "turn_end", "meta": agent._meta_public(meta_end), "log": room.log()[-12_000:]})


# ------------------------------------------------------------------------------------------------ public controls
def send(room_id, text, files=None):
    """Send a goal or live steer to a Group Chat room."""
    from . import agent
    room = store.Chat(room_id)
    meta = _ensure_migrated(room)
    if not is_group(meta):
        raise ValueError("not a group chat")
    text = str(text or "").strip()
    if not text and not files:
        return {"queued": False}

    with _meta_lock(room_id):
        meta = room.meta() or {}
        gc = dict(meta.get("group_chat") or {})
        seq = int(gc.get("board_seq") or 0) + 1
        is_goal = not bool(gc.get("goal"))
        if is_goal:
            gc["goal"] = text
            if not meta.get("user_renamed"):
                meta = room.update_meta(title=store.clean_title(text))
                agent.emit(room_id, {"type": "title", "title": meta["title"]})

        um = room.append({
            "role": "user",
            "content": text,
            "files": [{"path": str(f), "kind": "file"} for f in (files or [])],
            "board_seq": seq,
            "is_goal": is_goal,
            "steer": busy(room_id),
        })
        gc["board_seq"] = seq
        gc["stale_rounds"] = 0
        if gc.get("state") in ("done", "need_user", "paused", "stopped", "idle") and not is_goal:
            gc["goal"] = gc["goal"] + f"\n\nFollow-up from user: {text}"
            gc["signoff_by"] = ""
            gc["signoff_pending"] = []
        room.update_meta(group_chat=gc)
    agent.emit(room_id, {"type": "message", "message": agent._public(um)})

    steered_active = None
    live_run = None
    with _runs_lock:
        cur = _runs.get(room_id)
        if _live(cur):
            live_run = cur
            steered_active = gc.get("active_member_chat") or ""
        else:
            run = _runs[room_id] = GroupRun(room_id)
    if live_run is not None:
        # Room is actively running -> wake Council if sleeping, and steer active member if mid-turn
        live_run.waiting_cids.clear()
        live_run.wake_ev.set()
        if steered_active:
            agent.start(steered_active, f"User:\n{text}", files=files or [], soft=True)
        _emit_state(room_id)
        return {"queued": False, "steered": True}

    _set_room_state(room, state="running", state_reason="Starting room...")
    run.thread = threading.Thread(target=_room_loop, args=(run, room_id), daemon=True)
    run.thread.start()
    return {"queued": False}


def pause(room_id, immediate=False):
    with _runs_lock:
        run = _runs.get(room_id)
    if not _live(run):
        return {"ok": False, "state": public_state(room_id)}
    run.pause_ev.set()
    run.wake_ev.set()
    if immediate:
        run.stop_ev.set()
        run.worker_stop_ev.set()
    _set_room_state(store.Chat(room_id), state_reason="Pausing after current turn..." if not immediate else "Pausing...")
    return {"ok": True, "state": public_state(room_id)}


def resume(room_id):
    room = store.Chat(room_id)
    meta = _ensure_migrated(room)
    if not is_group(meta):
        raise ValueError("not a group chat")
    gc = dict(meta.get("group_chat") or {})
    if not gc.get("goal"):
        raise ValueError("Send a goal message first to start this Group Chat")
    with _runs_lock:
        cur = _runs.get(room_id)
        if _live(cur):
            cur.pause_ev.clear()
            return {"ok": True, "state": public_state(room_id)}
        run = _runs[room_id] = GroupRun(room_id)
    gc["stale_rounds"] = 0
    if int(gc.get("round") or 0) >= int(gc.get("max_rounds") or DEFAULT_MAX_ROUNDS):
        gc["max_rounds"] = int(gc.get("round") or 0) + 6
    gc["state"] = "running"
    gc["state_reason"] = "Resuming..."
    room.update_meta(group_chat=gc)
    _emit_state(room_id)
    run.thread = threading.Thread(target=_room_loop, args=(run, room_id), daemon=True)
    run.thread.start()
    return {"ok": True, "state": public_state(room_id)}


def pass_baton(room_id, force=False):
    with _runs_lock:
        run = _runs.get(room_id)
    if not _live(run):
        return {"ok": False, "state": public_state(room_id)}
    if force:
        run.force_pass_ev.set()
    else:
        run.pass_ev.set()
    return {"ok": True, "forced": bool(force), "state": public_state(room_id)}


def stop(room_id):
    from . import agent
    with _runs_lock:
        run = _runs.get(room_id)
    room = store.Chat(room_id)
    gc = (room.meta() or {}).get("group_chat") or {}
    active_mc = gc.get("active_member_chat")
    wm = gc.get("worker_member") if isinstance(gc.get("worker_member"), dict) else None
    if run:
        run.stop_ev.set()
        run.worker_stop_ev.set()
        run.wake_ev.set()
    if active_mc:
        try:
            agent.stop(active_mc)
        except Exception:
            pass
    if wm and wm.get("chat_id"):
        try:
            agent.stop(wm["chat_id"])
        except Exception:
            pass
    _set_room_state(room, state="stopped", state_reason="Stopped by user.", active_endpoint="", active_member_chat="")
    agent.emit(room_id, {"type": "turn_end", "meta": agent._meta_public(room.meta())})
    return {"ok": True, "dropped": []}


def fork_room_state(new_room, src_gc, copied_msgs):
    """Build a fresh, independent group_chat state dict for a forked Group Chat room.
    Creates brand-new member chats and (in shared_worker mode) a fresh Shared Worker chat,
    resets cursors to 0 so each fresh member session receives all copied room messages on
    its first turn, and remaps copied group_post member_chat_id links to the fork's chats."""
    src_gc = src_gc if isinstance(src_gc, dict) else {}
    mode = normalize_subagent_mode(src_gc.get("subagent_mode"))
    member_role = "panel" if mode == "shared_worker" else "relay"

    # Compute board_seq across copied messages (preserve existing seqs, fill gaps if needed)
    max_seq = 0
    for m in copied_msgs:
        seq = int(m.get("board_seq") or 0)
        if seq > max_seq:
            max_seq = seq
    if max_seq == 0 and copied_msgs:
        seq = 0
        for m in copied_msgs:
            if m.get("role") in ("user", "group_post"):
                seq += 1
                m["board_seq"] = seq
        max_seq = seq

    # Create brand-new independent member chats
    new_members = []
    ep_to_new_cid = {}
    old_to_new_cid = {}
    for m in (src_gc.get("members") or []):
        eid = str(m.get("endpoint") or "").strip()
        if not eid:
            continue
        mchat = _ensure_member_chat(new_room, eid, member_role, mode)
        new_members.append({
            "endpoint": eid,
            "name": _ep_name(eid),
            "chat_id": mchat.id,
            "cursor": 0,
            "role": member_role,
            "turns": 0,
        })
        ep_to_new_cid.setdefault(eid, mchat.id)
        if m.get("chat_id"):
            old_to_new_cid[m["chat_id"]] = mchat.id

    fallback_w = new_members[0]["endpoint"] if new_members else (config.get().get("endpoint") or "")
    worker_id = _valid_endpoint_or(src_gc.get("worker"), fallback_w)
    worker_member = None
    if mode == "shared_worker" and worker_id:
        worker_member = _make_worker_record(new_room, worker_id, cursor=0)
        src_wm = src_gc.get("worker_member")
        if isinstance(src_wm, dict) and src_wm.get("chat_id"):
            old_to_new_cid[src_wm["chat_id"]] = worker_member["chat_id"]

    # Remap group_post trace links to the fork's fresh member/worker chats (never point back to parent sessions)
    for m in copied_msgs:
        if m.get("role") != "group_post":
            continue
        old_cid = m.get("member_chat_id") or ""
        if old_cid and old_cid in old_to_new_cid:
            m["member_chat_id"] = old_to_new_cid[old_cid]
        elif m.get("member_role") == "worker" and worker_member:
            m["member_chat_id"] = worker_member["chat_id"]
        elif m.get("member_endpoint") in ep_to_new_cid:
            m["member_chat_id"] = ep_to_new_cid[m["member_endpoint"]]

    user_texts = [str(m.get("content") or "").strip() for m in copied_msgs if m.get("role") == "user" and str(m.get("content") or "").strip()]
    if user_texts:
        goal = user_texts[0]
        for extra in user_texts[1:]:
            goal = f"{goal}\n\nFollow-up from user: {extra}"
    else:
        goal = str(src_gc.get("goal") or "")

    return {
        "members": new_members,
        "worker_member": worker_member,
        "subagent_mode": mode,
        "worker": worker_id,
        "turn_budget_s": int(src_gc.get("turn_budget_s") or DEFAULT_TURN_BUDGET_S),
        "max_rounds": int(src_gc.get("max_rounds") or DEFAULT_MAX_ROUNDS),
        "max_total_s": int(src_gc.get("max_total_s") or DEFAULT_MAX_TOTAL_S),
        "goal": goal,
        "state": "idle",
        "state_reason": "",
        "round": 0,
        "active_idx": 0,
        "active_endpoint": "",
        "active_member_chat": "",
        "active_role": "",
        "active_started": 0.0,
        "turn_deadline": 0.0,
        "stale_rounds": 0,
        "signoff_by": "",
        "signoff_pending": [],
        "board_seq": max_seq,
        "protocol_v": PROTOCOL_VERSION,
    }
