"""Subagents: the Agent tool. A chat hands a self-contained task to a fresh agent on any configured connection (the chat's
own model by default) and gets its final answer back as the tool result.

A subagent is a hidden child chat (meta "sub_of", "sub_call", "depth") run by the normal turn code, so it has the same
tools, fences (tools.DENY), compaction and engines: on a Claude Code or Codex connection it is a one-off session of that
CLI with its own tools. Its events reach the window wrapped as {"type": "sub", "call", "sub", "ev"} on the parent chat,
and the window draws them under the Agent call.

The other direction: Claude Code and Codex sessions get the same tool (harness_agent) from a small stdio MCP server
(servers/agents.py) that calls back into this app (POST /api/subagent with a token made at start-up, kept in memory).

Limits: a depth limit (no endless nesting) and a slot per connection; no step or time budget (a turn runs
until the model finishes or the user stops it, and the parent's Stop stops its subagents). A single-slot local server (llama.cpp) runs one subagent at a time. A parent waits while its subagent works, so a subagent
on the parent's own single-slot server does not compete with it; with llama-server's --cache-ram the parent's prompt
cache is kept in host RAM meanwhile and its next step reads from it again."""
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from . import config, providers, resume, store, tools

TOKEN = resume.local_token("agent")    # proves a request to /api/subagent comes from an MCP server this app started (kept across restarts)
DEFAULTS = {"enabled": True, "max_depth": 2, "start_servers": False, "parallel": 4, "presets": []}
# How long Claude Code and Codex wait for one harness_agent call before giving up on it themselves (they need a number;
# Codex's own default is 60 s). Not a budget: the subagent has none, so this only has to outlast any real run.
CLI_WAIT_S = 7 * 24 * 3600
TOOL = "harness_agent"           # the MCP tool's name (Claude Code also has its own Agent/Task tool, for Claude only)
SUB_NOTE = ("\n\nYou are a subagent: another agent gave you the task in the first message and sees only your final reply. "
            "Do the task with your tools and check it, then end with a short, complete report: what you found or did, "
            "file paths, results, anything left. Do not ask questions; if something blocks you, say so in the report. "
            "If you need earlier or newer details from the main chat, use ParentContext (or harness_parent_context).")
SUB_CHAT_NOTE = ("\n\nYou are an embedded subagent chat forked from a main chat. You can answer the user directly here "
                 "while the main chat keeps working separately, or call ReportToMain (or harness_report_to_main) when "
                 "the user asks you to send findings back to the main chat. Use ParentContext (or harness_parent_context) "
                 "to search or read earlier or newer messages, notes or logs from the main chat whenever you need more detail.")


def sub_note_for(meta):
    base = SUB_CHAT_NOTE if (meta or {}).get("sub_origin") == "user" else SUB_NOTE
    pname = (meta or {}).get("sub_preset") or (meta or {}).get("sub_name") or ""
    inst = (meta or {}).get("sub_instructions") or ""
    if inst:
        base += f"\n\nYour assigned subagent role ({pname or 'Subagent'}): {inst}"
    return base


# ------------------------------------------------------------------------------------------------ embedded chats & parent context
_idx_lock = threading.Lock()


def _index_path():
    return config.DATA / "subagents_index.json"


def _load_index():
    try:
        raw = json.loads(_index_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(data):
    p = _index_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def _link_child(parent_id, child_id):
    if not parent_id or not child_id:
        return
    with _idx_lock:
        idx = _load_index()
        lst = [x for x in (idx.get(parent_id) or []) if isinstance(x, str)]
        if child_id not in lst:
            lst.append(child_id)
            idx[parent_id] = lst[-120:]
            _save_index(idx)


def parent_info(parent_id):
    """Lightweight info about a subagent's parent chat (for the embedded Back bar and live busy indicator)."""
    if not parent_id:
        return None
    from . import agent
    try:
        pm = store.Chat(parent_id).meta()
    except ValueError:
        return None
    if not pm:
        return None
    return {
        "id": parent_id,
        "title": pm.get("title") or "Main chat",
        "endpoint": pm.get("endpoint") or "",
        "busy": agent.busy(parent_id),
    }


def _context_budget_chars(ep):
    """Window-aware char budget for forked parent context (keeps small models fast and leaves >=75% window free)."""
    ctx = int((ep or {}).get("context") or 128_000)
    if ctx <= 36_000:
        return 14_000
    if ctx <= 96_000:
        return 24_000
    if ctx <= 160_000:
        return 32_000
    return 48_000


def _msg_text_and_steps(m):
    """Extract clean readable text and compact 1-line tool notes from either native or imported/Neon messages."""
    from .chat_import import _step_note
    t = (m.get("model_content") or m.get("content") or "").strip()
    steps = []
    if m.get("parts"):
        txts = []
        for p in m.get("parts") or []:
            if not isinstance(p, dict):
                continue
            k = p.get("kind")
            if k == "text" and (p.get("text") or "").strip():
                txts.append(str(p.get("text")).strip())
            elif k == "tool":
                nm = str(p.get("name") or "tool").split("__")[-1]
                inp = str(p.get("model_input") or p.get("input") or "").strip().splitlines()[0][:100] if (p.get("model_input") or p.get("input")) else ""
                steps.append(f"{nm} {inp}".strip() if inp else nm)
        if not t and txts:
            t = "\n".join(txts)
    for c in m.get("tool_calls") or []:
        steps.append(_step_note(c))
    return t, steps


def _load_parent_records(parent_id, tail_bytes=None):
    """Read normalized messages, notes, log and compaction summary from a parent chat.
    When tail_bytes is set on a large chat, reads only the head (for the original goal) and the last
    tail_bytes of messages.jsonl so starting a subagent on a large parent chat takes milliseconds."""
    ep_names = {e["id"]: e.get("name") or e["id"] for e in config.get().get("endpoints") or []}
    total_estimate = None
    truncated_tail = False
    ch = store.Chat(parent_id)
    meta = ch.meta() or {}
    notes = ch.notes().strip()
    log = ch.log().strip()
    mp = ch.messages_path
    fsize = mp.stat().st_size if mp.is_file() else 0
    if tail_bytes and fsize > tail_bytes:
        truncated_tail = True
        head_raw = ch.head_raw_lines(65_536)
        tail_raw, _ = ch.tail_raw_lines(tail_bytes)
        raw_msgs = []
        first_u = None
        for b in head_raw:
            try:
                m = json.loads(b)
            except ValueError:
                continue
            if m.get("role") == "user" and not m.get("auto"):
                first_u = m
                break
        seen_ids = set()
        if first_u is not None:
            raw_msgs.append(first_u)
            if first_u.get("id"):
                seen_ids.add(first_u["id"])
        for b in tail_raw:
            try:
                m = json.loads(b)
            except ValueError:
                continue
            mid = m.get("id")
            if mid and mid in seen_ids:
                continue
            raw_msgs.append(m)
        total_estimate = max(len(raw_msgs), store.count_messages(ch))
    else:
        raw_msgs = ch.messages()
    comp = next((m for m in reversed(raw_msgs) if m.get("role") == "compaction"), None)
    summary = (comp.get("summary") or "").strip() if comp else ""

    records = []
    idx = 0
    for m in raw_msgs:
        role = m.get("role")
        if role not in ("user", "assistant", "sub_report"):
            continue
        if role == "user" and m.get("auto"):
            continue
        if m.get("usage_only"):
            continue
        text, steps = _msg_text_and_steps(m)
        if not text and not steps:
            continue
        idx += 1
        if role == "user":
            who = "User"
        elif role == "sub_report":
            who = f"Subagent report ({m.get('sub_name') or m.get('model') or 'Subagent'})"
        else:
            ml = m.get("model_label") or ep_names.get(m.get("endpoint") or "")
            who = f"Assistant ({ml})" if ml else "Assistant"
        records.append({
            "index": idx,
            "role": role,
            "who": who,
            "text": text,
            "steps": steps,
            "ts": m.get("ts") or 0,
        })
    return {
        "title": meta.get("title") or "Main chat",
        "project": meta.get("project") or str(config.WORKSPACE),
        "endpoint": meta.get("endpoint") or "",
        "notes": notes,
        "log": log,
        "summary": summary,
        "messages": records,
        "total_messages": total_estimate or len(records),
        "truncated_tail": truncated_tail,
    }


def _write_parent_history_file(child, rec):
    """Write a clean local copy of the parent chat transcript inside the subagent's folder (searchable by all engines)."""
    parts = [f"# Main chat transcript: {rec['title']}", f"Project: {rec['project']}", ""]
    if rec["notes"]:
        parts += ["## Main chat notes", rec["notes"][:12_000], ""]
    if rec["summary"]:
        parts += ["## Compaction summary", rec["summary"][:20_000], ""]
    parts.append(f"## Messages ({rec.get('total_messages') or len(rec['messages'])} turns)")
    total_chars = 0
    for r in rec["messages"]:
        block = f"\n### [{r['index']}] {r['who']}\n"
        if r["text"]:
            block += _cut(r["text"], 8000) + "\n"
        if r["steps"]:
            block += "Steps: " + "; ".join(r["steps"][:12]) + "\n"
        total_chars += len(block)
        if total_chars > 350_000:
            parts.append("\n[... older/overflow messages truncated in file; use ParentContext tool for exact turn lookup ...]")
            break
        parts.append(block)
    p = child.dir / "parent-history.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = child.dir / "parent-history.md.tmp"
    tmp.write_text("\n".join(parts), encoding="utf-8")
    os.replace(tmp, p)
    return p


def build_parent_context(parent_id, child, ep):
    """Build a fast, window-fitted snapshot of the main chat for a subagent, plus a local searchable transcript file."""
    rec = _load_parent_records(parent_id, tail_bytes=1_500_000)
    hist_path = _write_parent_history_file(child, rec)
    if rec.get("truncated_tail"):
        def _bg_full_history():
            try:
                _write_parent_history_file(child, _load_parent_records(parent_id))
            except Exception:
                pass
        threading.Thread(target=_bg_full_history, name="sub-parent-hist", daemon=True).start()
    budget = _context_budget_chars(ep)
    msgs = rec["messages"]
    total_all = rec.get("total_messages") or len(msgs)
    if not msgs and not rec["notes"] and not rec["summary"]:
        return "", 0, 0, 0, False, hist_path

    head_bits = [f"Main chat: {rec['title']} (project: {rec['project']}, {total_all} messages total)."]
    used = len(head_bits[0])
    if rec["notes"]:
        n_lim = min(5000, budget // 4)
        n_txt = _cut(rec["notes"], n_lim)
        head_bits.append("Main chat notes:\n" + n_txt)
        used += len(n_txt) + 20
    if rec["summary"]:
        s_lim = min(10_000, budget // 3)
        s_txt = _cut(rec["summary"], s_lim)
        head_bits.append("Earlier compaction summary:\n" + s_txt)
        used += len(s_txt) + 30

    per_msg_lim = 3000 if budget <= 16_000 else 6000
    rem = max(4000, budget - used)
    kept = []
    trimmed = bool(rec.get("truncated_tail"))
    for r in reversed(msgs):
        body = _cut(r["text"], per_msg_lim) if r["text"] else ""
        step_s = (" [Steps: " + "; ".join(r["steps"][:8]) + "]") if r["steps"] else ""
        if len(step_s) > 420:
            step_s = step_s[:420] + " ...]"
        line = f"[{r['index']}] {r['who']}: {body}{step_s}".strip()
        if len(r["text"]) > per_msg_lim:
            trimmed = True
        if len(line) > rem and kept:
            trimmed = True
            break
        kept.append(line)
        rem -= len(line) + 2
    kept.reverse()

    first_user = next((r for r in msgs if r["role"] == "user" and r["text"]), None)
    if trimmed and first_user and not any(x.startswith(f"[{first_user['index']}] ") for x in kept):
        goal_line = f"[{first_user['index']}] Original goal (User): {_cut(first_user['text'], 1600)}"
        kept.insert(0, goal_line + "\n(... earlier messages omitted to fit this model's window; use ParentContext search/read if needed ...)")

    if ep and ep.get("kind") == "agy":
        tail_hint = f"Full searchable main chat transcript is saved at: {hist_path}"
    else:
        tail_hint = (f"Showing {len(kept)} of {total_all} main-chat messages. Use ParentContext "
                     "(action='search'|'read'|'latest'|'notes') to look up earlier details or live updates from the main chat.")
    body_text = "\n\n".join(head_bits + (["Recent main-chat messages:\n" + "\n\n".join(kept)] if kept else []) + [tail_hint])
    block = f"<main_chat_context>\n{body_text}\n</main_chat_context>"
    ctx_k = max(1, round(len(block) / 4000))
    return block, ctx_k, total_all, len(kept), trimmed, hist_path


def search_parent(parent_id, action="search", query="", start=1, limit=10):
    """Live read/search over the parent chat's full history, notes and log (never mutates parent cache)."""
    act = str(action or "search").strip().lower()
    lim = max(1, min(int(limit or 10), 30))

    if act == "notes":
        ch = store.Chat(parent_id)
        meta = ch.meta() or {}
        title = meta.get("title") or "Main chat"
        total = store.count_messages(ch)
        notes = ch.notes().strip()
        log = ch.log().strip()
        out = [f"Main chat: {title} ({total} messages total)"]
        out.append("Notes:\n" + (notes[:12_000] if notes else "(none)"))
        if log:
            out.append("Recent log:\n" + log[-6000:])
        return "\n\n".join(out)

    q = str(query or "").strip()
    use_tail = (act == "latest") or (act == "search" and not q)
    rec = _load_parent_records(parent_id, tail_bytes=1_500_000 if use_tail else None)
    msgs = rec["messages"]
    total_m = rec.get("total_messages") or len(msgs)

    if act == "notes":
        out = [f"Main chat: {rec['title']} ({total_m} messages total)"]
        out.append("Notes:\n" + (rec["notes"][:12_000] if rec["notes"] else "(none)"))
        if rec["log"]:
            out.append("Recent log:\n" + rec["log"][-6000:])
        return "\n\n".join(out)

    if act == "latest":
        slice_msgs = msgs[-lim:]
        if not slice_msgs:
            return f"Main chat ({rec['title']}) has no messages yet."
        lines = [f"Latest {len(slice_msgs)} of {total_m} messages in main chat ({rec['title']}):"]
        for r in slice_msgs:
            step_s = ("\nSteps: " + "; ".join(r["steps"][:10])) if r["steps"] else ""
            lines.append(f"[{r['index']}] {r['who']}:\n{_cut(r['text'], 4000)}{step_s}")
        return "\n\n".join(lines)[:26_000]

    if act == "read":
        s_idx = max(1, int(start or 1))
        picked = [r for r in msgs if s_idx <= r["index"] < s_idx + lim]
        if not picked:
            return f"No messages in range {s_idx}..{s_idx + lim - 1} (main chat has {len(msgs)} messages)."
        lines = [f"Main chat messages {picked[0]['index']}..{picked[-1]['index']} of {len(msgs)}:"]
        for r in picked:
            step_s = ("\nSteps: " + "; ".join(r["steps"][:10])) if r["steps"] else ""
            lines.append(f"[{r['index']}] {r['who']}:\n{_cut(r['text'], 5000)}{step_s}")
        return "\n\n".join(lines)[:26_000]

    # Default: search (regex with literal fallback across messages, notes, summary, log)
    q = str(query or "").strip()
    if not q:
        return search_parent(parent_id, action="latest", limit=min(lim, 8))
    try:
        rx = re.compile(q, re.I)
    except re.error:
        rx = re.compile(re.escape(q), re.I)
    hits = []
    if rec["notes"] and rx.search(rec["notes"]):
        m = rx.search(rec["notes"])
        a, b = max(0, m.start() - 120), min(len(rec["notes"]), m.end() + 240)
        hits.append(f"[notes.md] ...{rec['notes'][a:b].strip()}...")
    if rec["summary"] and rx.search(rec["summary"]):
        m = rx.search(rec["summary"])
        a, b = max(0, m.start() - 120), min(len(rec["summary"]), m.end() + 240)
        hits.append(f"[compaction summary] ...{rec['summary'][a:b].strip()}...")
    for r in reversed(msgs):
        hay = r["text"] + ("\nSteps: " + "; ".join(r["steps"]) if r["steps"] else "")
        m = rx.search(hay)
        if not m:
            continue
        a, b = max(0, m.start() - 140), min(len(hay), m.end() + 260)
        snip = re.sub(r"\s+", " ", hay[a:b]).strip()
        hits.append(f"[{r['index']}] {r['who']}: ...{snip}...")
        if len(hits) >= lim:
            break
    if not hits:
        return f"No matches for {q!r} across {len(msgs)} main-chat messages, notes and summary."
    return f"Found {len(hits)} match(es) for {q!r} in main chat ({len(msgs)} messages total; use action='read' with start=<index> for full text):\n\n" + "\n".join(hits)


_FACTS = {}                       # chat id -> what its messages file said so far (see chip_facts)
_facts_guard = threading.Lock()


def chip_facts(ch):
    """The few things a subagent's chip needs from its history: how many times it was asked, its first ask, its last
    answer and last error. Read once, then only the lines added since (the file only grows), because this runs for every
    subagent each time the main chat is opened and each time a subagent takes a step."""
    path = ch.messages_path
    with _facts_guard:
        st = _FACTS.get(ch.id)
        mode, _size = store.file_state(path, st)
        if mode == "gone":
            _FACTS.pop(ch.id, None)
            return {"turns": 0, "first_user": "", "last_asst": "", "last_err": ""}
        if mode == "same":
            return st
        if mode == "new":
            st = {"size": 0, "head": b"", "edge": b"", "turns": 0, "first_user": "", "last_asst": "", "last_err": ""}
        data, pos = store.read_more(path, st)
        for line in data.split(b"\n"):
            if not line.strip():
                continue
            try:
                m = json.loads(line)
            except ValueError:
                continue
            role, text = m.get("role"), m.get("content") or ""
            if not isinstance(text, str):
                text = ""
            if role == "user" and not m.get("auto"):
                st["turns"] += 1
                if not st["first_user"]:
                    st["first_user"] = text[:400]
            elif role == "assistant" and text.strip():
                st["last_asst"] = text[:2000]
            elif role == "error":
                st["last_err"] = text[:2000]
        store.file_stamp(path, st, pos + len(data))
        _FACTS[ch.id] = st
        return st


def chip_summary(child_id):
    """Compact live summary of one embedded subagent chat for its parent's strip and Subagents sheet."""
    from . import agent
    try:
        ch = store.Chat(child_id)
        cm = ch.meta()
    except ValueError:
        return None
    if not cm or cm.get("deleted") or not cm.get("sub_of"):
        return None
    eps = {e["id"]: e for e in config.get().get("endpoints") or []}
    ep_id = cm.get("endpoint") or ""
    ep = eps.get(ep_id) or {}
    ep_label = ep.get("name") or ep_id or "Model"
    facts = chip_facts(ch)             # not ch.messages(): that parsed the subagent's whole history on every call
    last_asst, last_err = facts["last_asst"], facts["last_err"]
    is_busy = agent.busy(child_id)
    status = "running" if is_busy else (cm.get("sub_status") or ("done" if last_asst else "failed" if last_err else "ready"))
    name = cm.get("sub_preset") or cm.get("sub_name") or ep_label
    desc = cm.get("sub_desc") or ""
    if not desc:
        desc = re.sub(r"\s+", " ", facts["first_user"]).strip()[:60]
    return {
        "id": child_id,
        "parent": cm.get("sub_of"),
        "name": name,
        "title": cm.get("title") or name,
        "model": f"{name} ({ep_label})" if name.lower() != ep_label.lower() else ep_label,
        "model_name": ep_label,
        "endpoint": ep_id,
        "preset": cm.get("sub_preset") or "",
        "description": desc,
        "origin": cm.get("sub_origin") or "agent",
        "status": status,
        "busy": is_busy,
        "steps": int((cm.get("usage") or {}).get("steps") or 0),
        "turns": facts["turns"],
        "started": float(cm.get("created") or 0),
        "updated": float(cm.get("updated") or cm.get("created") or 0),
        "last_preview": _cut(re.sub(r"\s+", " ", last_asst).strip(), 160) if last_asst else "",
        "forked": bool(cm.get("sub_forked")),
        "ctx_k": int(cm.get("sub_ctx_k") or 0),
    }


def children_of(parent_id):
    """All embedded subagent chats belonging to parent_id (both model-spawned and user-spawned), newest/active first."""
    if not parent_id:
        return []
    ids = []
    seen = set()
    for cid in (_load_index().get(parent_id) or []):
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    try:
        pm = store.Chat(parent_id).meta() or {}
        for rec in (pm.get("subagents") or {}).values():
            cid = rec.get("id") if isinstance(rec, dict) else None
            if cid and cid not in seen:
                seen.add(cid)
                ids.append(cid)
    except ValueError:
        pass
    out = []
    for cid in ids:
        s = chip_summary(cid)
        if s:
            out.append(s)
    out.sort(key=lambda x: (0 if x["busy"] else 1, -x["updated"]))
    return out


def notify_parent_chip(child_id, status=None):
    """Push a live chip update to the parent chat whenever an embedded subagent starts, finishes or updates."""
    from . import agent
    try:
        ch = store.Chat(child_id)
        cm = ch.meta()
    except ValueError:
        return
    if not cm or not cm.get("sub_of"):
        return
    if status:
        ch.update_meta(touch=False, sub_status=status)
    chip = chip_summary(child_id)
    if chip:
        agent.hub.publish({"type": "sub_chip", "chat": cm["sub_of"], "chip": chip})


def spawn_chat(parent_id, model=None, preset_id=None, name="", instructions="", prompt="", fork_context=True, effort=""):
    """Directly open a new embedded subagent chat attached to parent_id (works even while the main chat is busy)."""
    from . import agent
    if not parent_id:
        raise ValueError("parent chat id is required")
    pmeta = store.Chat(parent_id).meta()
    if not pmeta:
        raise ValueError("parent chat not found")

    ep, preset = None, None
    if preset_id:
        ep, preset = resolve_target(want_subagent=preset_id)
    if not ep and model:
        ep, preset = resolve_target(want_model=model)
    if not ep:
        fallback = pmeta.get("endpoint") or config.get().get("endpoint")
        ep = resolve(fallback) or (config.get().get("endpoints") or [None])[0]
    if not ep:
        raise ValueError("no valid model found for subagent")

    ep_label = ep.get("name") or ep["id"]
    clean_name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    preset_name = preset["name"] if preset else ""
    display_name = clean_name or preset_name or ep_label
    inst = str(instructions or (preset.get("instructions") if preset else "") or "").strip()[:1000]
    eff_val = str(effort or (preset.get("effort") if preset else "") or "").strip()[:24]
    project = pmeta.get("project") or str(config.WORKSPACE)
    depth = int(pmeta.get("depth") or 0) + 1
    s = settings()
    if depth > s["max_depth"]:
        raise ValueError("subagents go at most %d deep; go back to the chat that should start one" % s["max_depth"])

    child_title = f"{display_name} ({ep_label})" if display_name.lower() != ep_label.lower() else display_name
    child = store.create(project, ep["id"], title=child_title, reasoning=pmeta.get("reasoning"))
    base_tools = [n for n in agent.tool_list({"project": project, "depth": depth, "sub_of": parent_id})
                  if n != "Agent" or depth < s["max_depth"]]
    for extra_t in ("ParentContext", "ReportToMain"):
        if extra_t not in base_tools:
            base_tools.append(extra_t)
    child_eff = dict(pmeta.get("effort") or {})
    if eff_val:
        child_eff[ep["id"]] = eff_val

    parent_ctx, ctx_k, total_m, shown_m, trimmed = "", 0, 0, 0, False
    hist_path = None
    if fork_context:
        parent_ctx, ctx_k, total_m, shown_m, trimmed, hist_path = build_parent_context(parent_id, child, ep)

    meta_fields = dict(
        sub_of=parent_id,
        sub_origin="user",
        sub_name=display_name,
        sub_preset=preset_name,
        sub_instructions=inst,
        sub_status="ready",
        sub_forked=bool(fork_context and parent_ctx),
        sub_ctx_k=ctx_k,
        depth=depth,
        tools=base_tools,
        agent_models=model_lines(),
        agent_presets=presets(),
        seen_subagents=presets_digest(),
        effort=child_eff,
        parent_context=parent_ctx,
        parent_history_path=str(hist_path) if hist_path else "",
    )
    cmeta = child.update_meta(**meta_fields)
    sys_extra = sub_note_for(cmeta)
    if parent_ctx and ep.get("kind") not in ("claude-code", "codex", "agy"):
        sys_extra += "\n\n" + parent_ctx
    child.update_meta(system=agent.system_prompt(cmeta, ep, base_tools) + sys_extra)

    if fork_context and parent_ctx:
        how = f"forked {shown_m} of {total_m} messages (~{ctx_k}k tokens, sized for {ep_label})" if trimmed else f"forked {shown_m} messages (~{ctx_k}k tokens)"
        child.append({"role": "notice", "content": f"Started from main chat ({how}). Use ParentContext to search older or newer main-chat messages anytime."})

    _link_child(parent_id, child.id)
    notify_parent_chip(child.id, status="ready")

    clean_prompt = str(prompt or "").strip()
    if clean_prompt:
        child.update_meta(touch=False, sub_owes=True, sub_desc=clean_name or clean_prompt.splitlines()[0][:60])
        agent.start(child.id, clean_prompt, [])     # a task: its answer is reported to the main chat (turn_ended)

    return {"ok": True, "id": child.id, "meta": agent._meta_public(child.meta()), "chip": chip_summary(child.id)}


def report_to_main(child_id, summary="", wake_main=False, source="user"):
    """Deliver findings from a subagent chat back to its main (parent) chat, either steering/waking it or quietly for next turn."""
    from . import agent
    ch = store.Chat(child_id)
    cm = ch.meta()
    if not cm or not cm.get("sub_of"):
        raise ValueError("this chat is not a subagent")
    parent_id = cm["sub_of"]
    text = str(summary or "").strip()
    if not text:
        msgs = ch.messages()
        text = next((m.get("content") or "" for m in reversed(msgs) if m.get("role") == "assistant" and (m.get("content") or "").strip()), "").strip()
    if not text:
        raise ValueError("nothing to report yet: wait for a reply or write a summary")
    text = _cut(text, 12_000)

    eps = {e["id"]: e for e in config.get().get("endpoints") or []}
    ep_label = (eps.get(cm.get("endpoint") or "") or {}).get("name") or cm.get("endpoint") or "Subagent"
    who = cm.get("sub_preset") or cm.get("sub_name") or ep_label
    label = f"{who} ({ep_label})" if who.lower() != ep_label.lower() else ep_label

    parent = store.Chat(parent_id)
    if wake_main:
        payload = f"[Findings from subagent {label}]\n\n{text}"
        # soft: a subagent's words are not the user waiting. They go in at the main chat's next natural break and never
        # cut a call it is running (21 Nov 2026: a findings report interrupted the main chat's command and showed as
        # the user's own message).
        res = agent.start(parent_id, payload, [], soft=True)
        mode = "queued" if res.get("queued") else "sent"
        note_text = ("Queued findings for main chat (it will read them on its next step)." if mode == "queued"
                     else "Sent findings to main chat and started a turn.")
    else:
        mode = "handed_off"
        rm = parent.append({
            "role": "sub_report",
            "sub_id": child_id,
            "sub_name": who,
            "model": ep_label,
            "content": text,
        })
        with parent.lock:
            pm = parent.meta()
            pend = list(pm.get("pending_subagent_reports") or [])
            pend.append({"sub_id": child_id, "from": label, "text": text, "ts": time.time()})
            parent.update_meta(pending_subagent_reports=pend[-10:])
        agent.emit(parent_id, {"type": "message", "message": agent._public(rm)})
        note_text = "Handed findings to main chat (saved quietly; the main model will see them on its next turn)."

    nm = ch.append({"role": "notice", "content": note_text})
    agent.emit(child_id, {"type": "message", "message": agent._public(nm)})
    notify_parent_chip(child_id)
    return {"ok": True, "mode": mode, "parent": parent_id, "message": note_text}


def _clean_presets(raw):
    out, seen = [], set()
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict):
            continue
        name = re.sub(r"\s+", " ", str(it.get("name") or "")).strip()[:60]
        model = str(it.get("model") or "").strip()
        if not name or not model:
            continue
        pid = re.sub(r"[^a-z0-9_-]+", "-", str(it.get("id") or name).lower()).strip("-")[:32] or "agent"
        base_pid, n = pid, 1
        while pid in seen:
            n += 1
            pid = f"{base_pid}-{n}"
        seen.add(pid)
        out.append({
            "id": pid,
            "name": name,
            "model": model,
            "instructions": str(it.get("instructions") or "").strip()[:1000],
            "effort": str(it.get("effort") or "").strip()[:24],
        })
    return out


def settings():
    s = {**DEFAULTS, **(config.get().get("subagents") or {})}
    s["max_depth"] = max(0, min(int(s["max_depth"] or 0), 5))
    s["presets"] = _clean_presets(s.get("presets"))
    return s


def presets():
    """Configured named subagents enriched with their target model's display name and usability."""
    eps = {e["id"]: e for e in config.get().get("endpoints") or []}
    out = []
    for p in settings()["presets"]:
        ep = eps.get(p["model"]) or resolve(p["model"])
        ok, _ = _status(ep) if ep else (False, "")
        out.append({
            **p,
            "model": ep["id"] if ep else p["model"],
            "model_name": (ep.get("name") or ep["id"]) if ep else p["model"],
            "usable": bool(ep and ok),
        })
    return out


def save_preset(name, model, instructions="", effort="", pid=None):
    """Create or update a named subagent mapped to any model in the model selector."""
    clean_name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    if not clean_name:
        raise ValueError("Subagent name is required")
    ep = resolve(model)
    if not ep:
        raise ValueError(f"Unknown model {model!r}")
    inst = str(instructions or "").strip()[:1000]
    eff = str(effort or "").strip()[:24]
    saved_item = {}

    def fn(c):
        sa = dict(c.get("subagents") or {})
        cur = _clean_presets(sa.get("presets"))
        idx = next((i for i, x in enumerate(cur) if (pid and x["id"] == pid) or x["name"].lower() == clean_name.lower()), None)
        if idx is not None:
            item_id = cur[idx]["id"]
            cur[idx] = {"id": item_id, "name": clean_name, "model": ep["id"], "instructions": inst, "effort": eff}
        else:
            base = re.sub(r"[^a-z0-9]+", "-", clean_name.lower()).strip("-")[:28] or "subagent"
            have = {x["id"] for x in cur}
            item_id, n = base, 1
            while item_id in have:
                n += 1
                item_id = f"{base}-{n}"
            cur.append({"id": item_id, "name": clean_name, "model": ep["id"], "instructions": inst, "effort": eff})
        sa["presets"] = cur
        c["subagents"] = sa
        saved_item.update({"id": item_id, "name": clean_name, "model": ep["id"],
                           "model_name": ep.get("name") or ep["id"], "instructions": inst, "effort": eff})

    config.update(fn)
    cur_list = presets()
    try:
        from . import agent
        agent.hub.publish({"type": "subagents_changed", "presets": cur_list})
    except Exception:
        pass
    return next((x for x in cur_list if x["id"] == saved_item.get("id")), saved_item)


def delete_preset(pid):
    """Delete a saved named subagent by id or name."""
    target = str(pid or "").strip().lower()
    if not target:
        return False
    removed = {"ok": False}

    def fn(c):
        sa = dict(c.get("subagents") or {})
        cur = _clean_presets(sa.get("presets"))
        nxt = [x for x in cur if x["id"].lower() != target and x["name"].lower() != target]
        removed["ok"] = len(nxt) != len(cur)
        sa["presets"] = nxt
        c["subagents"] = sa

    config.update(fn)
    if removed["ok"]:
        try:
            from . import agent
            agent.hub.publish({"type": "subagents_changed", "presets": presets()})
        except Exception:
            pass
    return removed["ok"]


def presets_digest():
    """Stable fingerprint of the current named subagents list (empty string when none)."""
    ps = presets()
    if not ps:
        return ""
    return "|".join(f"{p['id']}:{p['name']}:{p['model']}:{p['effort']}:{p['instructions']}" for p in ps)


def tool_words(ep):
    """How this engine starts a subagent the user sees in the app. The API models have the app's own Agent tool;
    Claude Code and Codex get harness_agent from mcp_server(); a Neon (agy) gets harness_agent over acp (agy lists it
    as mcp_harness_harness_agent) and, in print mode, the agents.py command (agy has no per-session MCP there)."""
    kind = (ep or {}).get("kind")
    if kind in ("claude-code", "codex"):
        return "the harness_agent tool"
    if kind == "agy":
        return ("the harness_agent tool (mcp_harness_harness_agent), or without it the JSON piped into "
                + cli_command("agent") + " -")
    return "the Agent tool"


def cli_command(tool):
    """The command a Neon in print mode runs (its shell commands inherit the chat's KCH_AGENT_* from agy)."""
    return '"%s" "%s" %s' % (_python(), config.APP / "harness" / "servers" / "agents.py", tool)


def agy_note(meta):
    """Once per conversation in a Neon's first prompt: subagents the user can see are started through the app."""
    can, is_sub = offered(meta), bool((meta or {}).get("sub_of"))
    if not can and not is_sub:
        return ""
    lines = []
    if can:
        lines.append(
            "Subagents in this app: to hand a task to a subagent (on any model connected in Flock, or a named "
            "subagent), call the harness_agent tool, which agy lists as mcp_harness_harness_agent, with prompt (the "
            "whole task), and model or subagent. If you do not have that tool, run this command instead, with the "
            "same fields as JSON piped in on stdin: '{\"model\": \"...\", \"prompt\": \"...\"}' | " +
            cli_command("agent") + " - . "
            "Each subagent is a chat of its own that the user sees inside the app, under this one; calling it again "
            "with the same subagent or model follows up in that chat. Its final reply comes back as the tool's "
            "answer; if your turn is cut first, it reaches you later as a message. Never start subagents with "
            "`claude -p`, `codex exec`, another agy, or your own invoke_subagent: those run outside the app and the "
            "user cannot see or steer them.")
    if is_sub:
        lines.append(
            "You are a subagent chat in this app: send findings to the main chat with harness_report_to_main "
            "(mcp_harness_harness_report_to_main) and read the main chat with harness_parent_context "
            "(mcp_harness_harness_parent_context); without those tools pipe the JSON into the command: "
            "'{\"summary\": \"...\"}' | " + cli_command("report") + " -   or   '{\"action\": \"latest\"}' | " +
            cli_command("parent") + " - .")
    return "\n\n".join(lines)


def acp_mcp(chat, meta):
    """The harness MCP server in agy acp's own form (session/new and session/load take mcpServers; env is a list)."""
    srv = mcp_server(chat, meta)
    if not srv:
        return []
    return [{"name": "harness", "command": srv["command"], "args": srv["args"],
             "env": [{"name": k, "value": str(v)} for k, v in srv["env"].items()]}]


def presets_note(ep=None):
    """Short cache-safe note appended once to the next user message when named subagents change mid-chat."""
    ps = presets()
    if not ps:
        return ""
    bits = []
    for p in ps:
        role = f" ({p['instructions'][:120]})" if p.get("instructions") else ""
        bits.append(f"{p['name']} -> {p['model_name']} [id: {p['id']}, model: {p['model']}]{role}")
    return ("\n\n(Named subagents available in this workspace via " + tool_words(ep) + " "
            "(pass subagent=\"<name>\" or model=\"<name>\"): " + "; ".join(bits) +
            ". You can also pass any model id from the model selector directly.)")


def offered(meta):
    """Does a new chat at this depth get the Agent tool?"""
    s = settings()
    from . import group_chat
    if not group_chat.subagents_allowed_for(meta):
        return False
    return bool(s["enabled"]) and meta.get("mode") != "assistant" and int(meta.get("depth") or 0) < s["max_depth"]


# ------------------------------------------------------------------------------------------------ models
def _status(ep):
    """(usable now or on demand, note for the tool description)."""
    from . import runtime
    kind = ep.get("kind")
    if kind == "claude-code":
        return bool(config.claude_exe()), "Claude Code, with its own tools"
    if kind == "codex":
        return bool(config.codex_exe()), "Codex, with its own tools"
    if kind == "agy":
        return bool(config.agy_exe()), "Antigravity CLI, with its own tools"
    if runtime.serves(ep["id"]):
        label = runtime.cfg().get("label") or "model server"
        if settings()["start_servers"]:
            return True, f"on the user's {label}; started if it is off (takes minutes)"
        return True, f"on the user's {label}, single slot; when it is off the call says so"
    try:
        base, _ = providers._keys(ep)
    except providers.ModelError:
        return False, ""
    return True, "local, single slot" if (ep.get("llama") or providers._local(base)) else ""


def model_lines():
    """The connections a subagent can use, as [id, line] for the tool description (kept with the chat: fixed prefix)."""
    out = []
    for ep in config.get()["endpoints"]:
        ok, note = _status(ep)
        if ok:
            out.append([ep["id"], f"{ep['id']}: {ep.get('name') or ep['id']}" + (f" ({note})" if note else "")])
    return out


def _meta_presets(meta, engine=False):
    """Named subagents visible in the frozen tool schema: fixed per chat for API prompt-cache reuse."""
    if engine:
        return presets()
    if isinstance(meta, dict):
        if "agent_presets" in meta:
            return meta.get("agent_presets") or []
        if "tools" in meta:
            return []
    return presets()


def describe(meta, engine=False):
    lines = model_lines() if engine else (meta.get("agent_models") or model_lines())
    own = meta.get("endpoint") or config.get().get("endpoint")
    text = ("Hand a task to a subagent: a fresh agent with its own context and the normal tools, working in the same "
            "project folder. It sees only your prompt and you get back only its final answer, so put everything it needs "
            "in the prompt (paths, what to do, what to report). Use it for side tasks that would fill your context, or to "
            "have another model do or check something. Several calls in one step run at the same time (one at a time on "
            "a single-slot local model). A subagent this chat already has (one the user added, or one you called before) "
            "keeps its own chat: calling it again continues that chat, so it remembers its earlier work, and a second "
            "call to it waits for the first. If the user sends a message while you wait, the subagent is not stopped: "
            "its report reaches you later as a message.")
    if engine:
        text += " Your own Agent/Task tool only starts Claude; this one starts any model the user has connected in Flock or any named subagent they added."
    ps = _meta_presets(meta, engine=engine)
    if ps:
        text += "\nNamed subagents (pass as subagent or model):\n" + "\n".join(
            f"- {p['name']} -> {p['model_name']} (id: {p['id']}, model: {p['model']}"
            + (f"; {p['instructions'][:140]}" if p.get("instructions") else "") + ")"
            for p in ps
        )
    text += f"\nModels (the model field; default {own}, this chat's own):\n" + "\n".join(f"- {ln}" for _, ln in lines)
    return text


def spec(meta, engine=False):
    lines = model_lines() if engine else (meta.get("agent_models") or model_lines())
    ids = [i for i, _ in lines]
    pnames = [p["name"] for p in _meta_presets(meta, engine=engine)]
    sub_hint = ("Name of a saved subagent: " + ", ".join(pnames)) if pnames else "Name of a saved subagent (optional)"
    params = {"type": "object", "properties": {
        "description": {"type": "string", "description": "A short label for the task (3-5 words)"},
        "prompt": {"type": "string", "description": "The whole task, with everything the subagent needs to know"},
        "subagent": {"type": "string", "description": sub_hint},
        "model": {"type": "string", "description": "Which model (or named subagent) runs it: " + ", ".join(ids)}},
        "required": ["prompt"]}
    return {"name": TOOL if engine else "Agent", "description": describe(meta, engine), "parameters": params}


def resolve_preset(want):
    """Match a configured named subagent by id or name (exact case-insensitive, then unique substring)."""
    w = str(want or "").strip().lower()
    if not w:
        return None
    ps = presets()
    for key in ("id", "name"):
        for p in ps:
            if str(p.get(key) or "").lower() == w:
                return p
    hits = [p for p in ps if w in str(p.get("name") or "").lower() or w in str(p.get("id") or "").lower()]
    return hits[0] if len(hits) == 1 else None


def resolve(want):
    """A connection by id, name or model, case-insensitive; then by a word of its name ("opus", "flash", "neon")."""
    eps = config.get()["endpoints"]
    w = str(want or "").strip().lower()
    if not w:
        return None
    for key in ("id", "name", "model"):
        for ep in eps:
            if str(ep.get(key) or "").lower() == w:
                return ep
    hits = [ep for ep in eps if w in str(ep.get("name") or "").lower() or w in str(ep.get("model") or "").lower()]
    return hits[0] if len(hits) == 1 else None


def resolve_target(want_subagent=None, want_model=None):
    """Resolve (endpoint, preset_or_None) from either a named subagent or any model in the model selector."""
    if want_subagent:
        p = resolve_preset(want_subagent)
        if p:
            return resolve(p["model"]), p
        ep = resolve(want_subagent)
        if ep:
            return ep, None
        return None, None
    if want_model:
        p = resolve_preset(want_model)
        if p:
            return resolve(p["model"]), p
        return resolve(want_model), None
    return None, None


def ready(ep):
    """(True, "") when a subagent can run on it now (or its server may be started), else (False, why)."""
    from . import runtime as rt
    kind = ep.get("kind")
    if kind in ("claude-code", "codex", "agy"):
        h = providers.health(ep)
        return bool(h.get("ok")), h.get("error") or ""
    if rt.serves(ep["id"]):
        if providers.health(ep, timeout=3).get("ok"):
            return True, ""
        st = rt.runtime.status()
        if st.get("state") in ("starting", "warming") or settings()["start_servers"]:
            return True, ""
        return False, (f"{ep['name']} runs on the user's {st.get('label') or 'model server'}, which is off. Subagents may not "
                       "start it (it can cost money; Settings > Tools changes that). Ask the user to start it, or use "
                       "another model.")
    try:
        providers._keys(ep)
    except providers.ModelError as e:
        return False, str(e)
    return True, ""


# ------------------------------------------------------------------------------------------------ slots
_slots, _slots_lock = {}, threading.Lock()


def _slot(ep):
    """One semaphore per connection: a single-slot local server takes one subagent at a time (more would evict each
    other's prompt cache on every step); a remote API takes several."""
    from . import runtime as rt
    n = ep.get("parallel")
    if not n:
        local = ep.get("llama") or rt.serves(ep["id"]) or providers._local(ep.get("base") or "https://x")
        n = 1 if local else settings()["parallel"]
    with _slots_lock:
        s = _slots.get(ep["id"])
        if s is None or s[0] != n:
            s = _slots[ep["id"]] = (n, threading.BoundedSemaphore(int(n)))
        return s[1]


# ------------------------------------------------------------------------------------------------ live events
_links = {}                      # child chat id -> {"parent", "call"}
_calls, _calls_lock = {}, threading.Lock()     # engine parents: chat id -> [{"id", "prompt", "claimed", "t"}]


# ------------------------------------------------------------------------------- one subagent, one chat; never cut loose
# Rules for every engine (Claude Code, Codex, Antigravity, the app's own loop):
#   1. A call to a subagent this chat already has continues that subagent's chat. A new chat is made only when there is
#      none for that model or named subagent (or, for ones the model made itself, when they are all busy).
#   2. A message from the user never stops a running subagent. The parent stops WAITING for it (detach_calls); the
#      subagent goes on in its own chat and its report is handed to the parent when it finishes (_deliver_late).
#   3. A call that never ran leaves no chat behind.
_detached = {}                   # call key -> True: the caller stopped waiting, the subagent goes on
_notes = {}                      # parent chat id -> [labels]: told to the model with the user's next message
_det_lock = threading.Lock()
_child_locks = {}                # subagent chat id -> Lock: one task at a time in one subagent chat


def _label(child_id):
    try:
        cm = store.Chat(child_id).meta() or {}
    except ValueError:
        return "a subagent"
    who = cm.get("sub_preset") or cm.get("sub_name") or cm.get("endpoint") or "subagent"
    desc = str(cm.get("sub_desc") or "").strip()
    return f'{who} ("{desc}")' if desc else str(who)


def detach_calls(parent_id, only=None):
    """The parent stopped waiting for its running subagent calls: the user's message cut in, or its CLI was ended to
    take that message, or the CLI gave up on the call. The subagents are not stopped. Returns their labels."""
    names = []
    with _det_lock:
        for child_id, link in list(_links.items()):
            key = link.get("call")
            if link.get("parent") != parent_id or (only and key != only) or _detached.get(key):
                continue
            _detached[key] = True
            names.append(_label(child_id))
        if names:
            _notes.setdefault(parent_id, []).extend(names)
    return names


def detached_note(parent_id):
    """What the model must be told with the user's message (agent.user_message): which subagents that message cut
    loose. Given once."""
    with _det_lock:
        names = _notes.pop(parent_id, None)
    if not names:
        return ""
    return ("[App note, not from the user: this message did not stop your subagent call" + ("s" if len(names) > 1 else "")
            + ". Still working, each in its own chat: " + "; ".join(names) + ". The report will be delivered to you "
            "here as a message when it finishes. Do not start that work again and do not wait in a loop for it.]")


def _child_ids(parent_id):
    ids = [c for c in (_load_index().get(parent_id) or []) if isinstance(c, str)]
    try:
        for rec in ((store.Chat(parent_id).meta() or {}).get("subagents") or {}).values():
            cid = rec.get("id") if isinstance(rec, dict) else None
            if cid and cid not in ids:
                ids.append(cid)
    except ValueError:
        pass
    return ids


def _busy(child_id):
    from . import agent
    r = agent.runs.get(child_id)
    return bool(r and agent._live(r))


def _reuse_for(parent_id, ep, preset):
    """The subagent chat a new call should continue, or None (then one is made). The same named subagent, or, when the
    call names only a model, any subagent of this chat on that model. One the user added comes first, and is waited
    for when busy; ones the model made are reused when idle."""
    want = (preset["name"] if preset else "").strip().lower()
    mine, made = [], []
    for cid in _child_ids(parent_id):
        try:
            ch = store.Chat(cid)
            cm = ch.meta()
        except ValueError:
            continue
        if not cm or cm.get("sub_of") != parent_id or cm.get("endpoint") != ep["id"] or cm.get("archived") \
                or cm.get("deleted"):            # one the user removed from the list is not brought back by a call
            continue
        if want and (cm.get("sub_preset") or "").strip().lower() != want:
            continue
        row = (not _busy(cid), float(cm.get("updated") or 0), ch)
        (mine if cm.get("sub_origin") == "user" else made).append(row)
    if mine:
        return max(mine, key=lambda r: r[:2])[2]
    idle = [r for r in made if r[0]]
    return max(idle, key=lambda r: r[:2])[2] if idle else None


def _unlink_child(parent, child_id, key):
    """Take a subagent chat that never ran out of its parent's list."""
    with _idx_lock:
        idx = _load_index()
        lst = [x for x in (idx.get(parent.id) or []) if x != child_id]
        if lst != (idx.get(parent.id) or []):
            idx[parent.id] = lst
            _save_index(idx)
    with parent.lock:
        subs = dict(parent.meta().get("subagents") or {})
        kept = {k: v for k, v in subs.items() if k != key and not (isinstance(v, dict) and v.get("id") == child_id)}
        if len(kept) != len(subs):
            parent.update_meta(touch=False, subagents=kept)


def _deliver_late(parent, child, who, res, cut=True):
    """A subagent whose caller had stopped waiting has finished: its report goes to the parent chat as a message of its
    own. A busy parent gets it at its next natural break (soft: it cuts nothing short); an idle one starts a turn on
    it, so the work carries on without the user having to ask."""
    from . import agent
    desc = str(child.meta().get("sub_desc") or "").strip()
    how = ("It kept working after your call to it was cut short by a message from the user." if cut else
           "It has finished the task it was given (nothing was waiting on it, so its answer comes to you this way).")
    head = (f"[Report from subagent {who}" + (f' on "{desc}"' if desc else "") + f". {how} This is its result, not a "
            "message from the user.]")
    body = str(res.get("text") or "").strip()
    try:
        got = agent.start(parent.id, head + "\n\n" + body, [], soft=True)
        note = ("Report queued for the main chat (it reads it at its next break)." if got.get("queued")
                else "Report sent to the main chat, which carries on with it.")
    except Exception as e:                   # never lost: kept quietly for the parent's next turn
        try:
            report_to_main(child.id, summary=body, wake_main=False, source="late")
            return
        except Exception:
            note = f"The report could not be handed to the main chat ({e.__class__.__name__}); it is the last reply above."
    nm = child.append({"role": "notice", "content": note})
    agent.emit(child.id, {"type": "message", "message": agent._public(nm)})


def note_call(cid, call_id, args=None):
    """An engine (Claude Code, Codex) is calling harness_agent: remember the call's id so the subagent the MCP server asks
    for is drawn under it."""
    with _calls_lock:
        lst = [c for c in _calls.setdefault(cid, []) if time.time() - c["t"] < 3600]
        c = next((c for c in lst if c["id"] == call_id), None)
        if c is None:
            c = {"id": call_id, "prompt": None, "claimed": False, "t": time.time()}
            lst.append(c)
        if isinstance(args, dict) and args.get("prompt"):
            c["prompt"] = args["prompt"]
        _calls[cid] = lst


def _claim(cid, prompt, wait=4.0):
    end = time.time() + wait
    while True:
        with _calls_lock:
            free = [c for c in _calls.get(cid, []) if not c["claimed"]]
            pick = next((c for c in free if c["prompt"] == prompt), None) or (free[0] if free else None)
            if pick:
                pick["claimed"] = True
                return pick["id"]
        if time.time() > end:
            return None
        time.sleep(0.1)


def forward(cid, ev):
    """Called by agent.emit: a subagent's event goes to its parent's window, wrapped. True when it was handled."""
    link = _links.get(cid)
    if not link:
        return False
    from . import agent
    t = ev.get("type")
    if t == "file":                                  # the file pane refreshes whichever chat changed the file
        agent.hub.publish(ev)
        return True
    slim = _slim(ev)
    if slim is not None:
        agent.emit(link["parent"], {"type": "sub", "call": link["call"], "sub": cid, "ev": slim})
    return True


def _cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + " [...]"


def _slim(ev):
    t = ev.get("type")
    if t in ("delta", "reasoning"):
        return {"type": t, "id": ev.get("id"), "text": ev.get("text")}
    if t == "assistant":
        m = ev.get("message") or {}
        return {"type": t, "temp": ev.get("temp"), "message": {
            "id": m.get("id"), "content": _cut(m.get("content"), 6000), "endpoint": m.get("endpoint"), "stats": m.get("stats"),
            "tool_calls": [{"id": c.get("id"), "name": c.get("name"), "arguments": _cut(c.get("arguments"), 2000)}
                           for c in m.get("tool_calls") or []]}}
    if t == "message":
        m = ev.get("message") or {}
        keep = {k: m.get(k) for k in ("id", "role", "tool_call_id", "name", "ok", "secs") if k in m}
        keep["content"] = _cut(m.get("content") if isinstance(m.get("content"), str) else "", 3000)
        if (m.get("ui") or {}).get("diff"):
            keep["ui"] = {"diff": _cut(m["ui"]["diff"], 6000)}
        return {"type": t, "message": keep}
    if t == "turn_end":
        return {"type": t}
    if t in ("step", "tool_start", "tool_draft", "waiting", "retry", "compacting", "step_cancelled", "sub", "sub_start", "activity",
             "sub_end"):
        return ev
    return None                                      # titles, notes, todos, queued: the parent's window has no use for them


def _record(parent, key, info):
    with parent.lock:
        subs = parent.meta().get("subagents") or {}
        subs[key] = {**subs.get(key, {}), **info}
        parent.update_meta(subagents=subs)


# ------------------------------------------------------------------------------------------------ running one
class _Any:
    """Several Stop flags read as one (the parent's turn, an MCP cancel)."""

    def __init__(self, *evs):
        self.evs = [e for e in evs if e is not None]

    def is_set(self):
        return any(e.is_set() for e in self.evs)


def run(parent, pmeta, call_id, args, cancel, gone=None):
    """Run one subagent to the end. Returns the tool result {"text", "ok"}; raises tools.ToolError for a call that
    cannot start (unknown model, depth limit, server off).
    cancel: the parent's Stop (the only thing that stops a subagent). gone: set when the caller gave up waiting (an
    engine cancelled its tool call): the subagent goes on and its report is delivered later."""
    from . import agent, group_chat
    s = settings()
    if not s["enabled"]:
        raise tools.ToolError("subagents are turned off (Settings > Tools)")
    groom_id, gmode, glimit = group_chat.subagent_policy_for(pmeta)
    gslot_key = ""
    if groom_id:
        if gmode == "shared_worker":
            raise tools.ToolError(
                "In 1 Shared Worker mode, the room already has its single dedicated Shared Worker sub-agent. "
                "Do not spawn additional sub-agents; coordinate through the Room Board instead."
            )
        if glimit <= 0:
            raise tools.ToolError("Sub-agents are locked OFF in this Group Chat room (Relay mode). Do this step yourself.")
        gslot_key = f"{parent.id}:{call_id or secrets.token_hex(4)}"
        ok_slot, cur_n = group_chat.acquire_subagent_slot(groom_id, gslot_key, glimit)
        if not ok_slot:
            raise tools.ToolError(
                f"Group Chat sub-agent limit reached ({cur_n}/{glimit} active in {gmode} mode). "
                "Wait for an active sub-agent to finish or handle this step yourself."
            )
    try:
        return _run_inner(parent, pmeta, call_id, args, cancel, gone=gone, s=s, groom_id=groom_id)
    finally:
        if groom_id and gslot_key:
            group_chat.release_subagent_slot(groom_id, gslot_key)


def _run_inner(parent, pmeta, call_id, args, cancel, gone=None, s=None, groom_id=""):
    from . import agent
    s = s or settings()
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise tools.ToolError("prompt is missing: say what the subagent should do")
    depth = int(pmeta.get("depth") or 0)
    if depth >= s["max_depth"]:
        raise tools.ToolError(f"subagents can be nested {s['max_depth']} deep at most; do this part yourself")
    want_sub = args.get("subagent")
    want_mod = args.get("model")
    if want_sub or want_mod:
        ep, preset = resolve_target(want_sub, want_mod)
    else:
        default_id = pmeta.get("endpoint") or config.get().get("endpoint")
        ep, preset = resolve(default_id), None
    if not ep:
        want = want_sub or want_mod
        names = ", ".join([p["name"] for p in presets()] + [i for i, _ in model_lines()])
        raise tools.ToolError(f"there is no subagent or model called {want!r}; use one of: {names}")
    ok, why = ready(ep)
    if not ok:
        raise tools.ToolError(why or f"{ep['name']} cannot be used right now")
    desc = re.sub(r"\s+", " ", str(args.get("description") or prompt.splitlines()[0])).strip()[:60]
    project = pmeta.get("project") or str(config.WORKSPACE)
    ep_label = ep.get("name") or ep["id"]
    display_model = f"{preset['name']} ({ep_label})" if preset else ep_label
    old = _reuse_for(parent.id, ep, preset)
    if old is not None:              # this chat already has that subagent: the task goes into its chat, no new one
        child = old
        ometa = child.meta()
        child.update_meta(sub_call=call_id, sub_desc=desc, sub_status="running", sub_owes=True)
        who = ometa.get("sub_preset") or ometa.get("sub_name") or ep_label
        prompt = ("[Task from the main chat's model, not typed by the user. Do it, check it, and end with a complete "
                  "report: your final reply here is what the main chat gets back.]\n\n" + prompt)
        key = call_id or "sub-" + child.id + "-" + secrets.token_hex(3)
        _links[child.id] = {"parent": parent.id, "call": key}
        _link_child(parent.id, child.id)
        t0 = time.time()
        info = {"id": child.id, "model": f"{who} ({ep_label})" if who != ep_label else ep_label, "model_name": ep_label,
                "endpoint": ep["id"], "preset": ometa.get("sub_preset") or "", "description": desc,
                "depth": int(ometa.get("depth") or depth + 1), "status": "running", "started": t0, "reused": True}
        _record(parent, key, info)
        agent.emit(parent.id, {"type": "sub", "call": key, "sub": child.id, "ev": {"type": "sub_start", **info}})
        notify_parent_chip(child.id, status="running")
        return _drive(parent, child, ep, key, t0, cancel, s, prompt, gone=gone)
    child_title = f"Subagent: {preset['name']} - {desc}" if preset else f"Subagent: {desc}"
    child = store.create(project, ep["id"], title=child_title, reasoning=pmeta.get("reasoning"))
    names = [n for n in (pmeta.get("tools") or agent.TOOL_NAMES) if n != "Agent" or depth + 1 < s["max_depth"]]
    for extra_t in ("ParentContext", "ReportToMain"):
        if extra_t not in names:
            names.append(extra_t)
    child_eff = dict(pmeta.get("effort") or {})
    if preset and preset.get("effort"):
        child_eff[ep["id"]] = preset["effort"]
    parent_ctx, ctx_k, _, _, _, hist_path = build_parent_context(parent.id, child, ep)
    cmeta = child.update_meta(sub_of=parent.id, sub_call=call_id, sub_origin="agent",
                              sub_name=(preset["name"] if preset else ep_label),
                              sub_desc=desc, sub_instructions=(preset.get("instructions") if preset else ""),
                              sub_status="running", sub_forked=bool(parent_ctx), sub_ctx_k=ctx_k, sub_owes=True,
                              depth=depth + 1, tools=names,
                              agent_models=model_lines(), agent_presets=presets(), seen_subagents=presets_digest(),
                              effort=child_eff, sub_preset=(preset["name"] if preset else ""),
                              parent_context=parent_ctx,
                              parent_history_path=str(hist_path) if hist_path else "",
                              **({"group_room": groom_id} if groom_id else {}))
    sys_extra = sub_note_for(cmeta)
    if parent_ctx and ep.get("kind") not in ("claude-code", "codex", "agy"):
        sys_extra += "\n\n" + parent_ctx
    child.update_meta(system=agent.system_prompt(cmeta, ep, names) + sys_extra)
    if preset and preset.get("instructions") and ep.get("kind") in ("claude-code", "codex", "agy"):
        prompt = f"[Subagent {preset['name']} instructions: {preset['instructions']}]\n\n{prompt}"
    key = call_id or "sub-" + child.id
    _links[child.id] = {"parent": parent.id, "call": key}
    _link_child(parent.id, child.id)
    t0 = time.time()
    info = {"id": child.id, "model": display_model, "model_name": ep_label, "endpoint": ep["id"],
            "preset": preset["name"] if preset else "", "description": desc,
            "depth": depth + 1, "status": "running", "started": t0}
    _record(parent, key, info)
    agent.emit(parent.id, {"type": "sub", "call": key, "sub": child.id, "ev": {"type": "sub_start", **info}})
    notify_parent_chip(child.id, status="running")
    return _drive(parent, child, ep, key, t0, cancel, s, prompt, gone=gone, fresh=True)


def carry_on(ctx, call_id, rec):
    """An Agent call the app restarted in the middle of (agent._recover_call): its subagent's chat is on disk, so it is
    carried on there, like its parent, and its answer becomes the call's result. None when that subagent never started
    (the caller runs the call afresh)."""
    parent = ctx.chat
    info = (parent.meta().get("subagents") or {}).get(call_id) or {}
    if not info.get("id") or info.get("status") != "running":
        return None
    child = store.Chat(info["id"])
    cmeta = child.meta()
    if not cmeta:
        return None
    try:
        ep = config.endpoint(cmeta.get("endpoint"))
    except LookupError:
        return None
    _links[child.id] = {"parent": parent.id, "call": call_id}
    carry = {"started": info.get("started"), "server": (rec or {}).get("server") or {}}
    return _drive(parent, child, ep, call_id, float(info.get("started") or time.time()), ctx.cancel, settings(), None, carry)


def _drive(parent, child, ep, key, t0, cancel, s, prompt, carry=None, gone=None, fresh=False):
    """Run the subagent's turn in its own chat (prompt), or carry a cut one on (carry, after a restart).
    fresh: the chat was made for this call (taken out of the list again if the call never runs)."""
    from . import agent
    sub_run = agent.Run(child.id)
    sub_run.sub = True
    sub_run.thread = threading.current_thread()
    why_stop = {}
    done = threading.Event()
    ran = False

    def watch():                 # the parent's Stop stops the subagent (and its own subagents); nothing else does
        while not done.wait(0.3):
            if cancel is not None and cancel.is_set():
                why_stop.setdefault("why", "stopped")
                sub_run.cancel.set()
                return
            if gone is not None and gone.is_set() and not _detached.get(key):
                detach_calls(parent.id, only=key)    # the caller gave up waiting: go on, deliver the report later
    threading.Thread(target=watch, daemon=True).start()
    # one task at a time in one subagent chat: a second call to the same subagent, or the user talking to it, waits
    turn_lock = _child_locks.setdefault(child.id, threading.Lock())
    told = False
    while not turn_lock.acquire(timeout=0.5):
        if sub_run.cancel.is_set():
            break
        if not told:
            told = True
            agent.emit(child.id, {"type": "waiting", "text": "Waiting: this subagent is finishing its earlier task"})
    else:
        try:
            while _busy(child.id) and not sub_run.cancel.is_set():     # a turn the user started in this chat
                time.sleep(0.5)
            # an ancestor holding this connection's slot is waiting for this subagent: it may use the slot (no deadlock)
            slot = _Free() if _ancestor_holds(parent.id, ep["id"]) else _slot(ep)
            _holding[child.id] = ep["id"]
            try:
                waited = False
                while not slot.acquire(timeout=0.5):
                    if sub_run.cancel.is_set():
                        break
                    if not waited:
                        waited = True
                        agent.emit(child.id, {"type": "waiting", "text": f"Waiting for {ep['name']}: another subagent is using it"})
                else:
                    try:
                        if not sub_run.cancel.is_set():
                            with agent.runs_lock:
                                agent.runs[child.id] = sub_run
                            ran = True
                            agent._turn(sub_run, child, prompt, [], carry=carry)
                    finally:
                        slot.release()
            finally:
                _holding.pop(child.id, None)
        finally:
            turn_lock.release()
    done.set()
    if fresh and not ran and not child.messages():
        _links.pop(child.id, None)       # the call never ran (stopped while it waited): no empty subagent left listed
        _detached.pop(key, None)
        _unlink_child(parent, child.id, key)
        agent.emit(parent.id, {"type": "sub", "call": key, "sub": child.id, "ev": {"type": "sub_end", "status": "stopped", "answer": ""}})
        return {"text": "Stopped by the user before the subagent started.", "ok": False}
    return _finish(parent, child, ep, key, t0, why_stop.get("why"), s)


_holding = {}                    # running subagent's chat id -> connection id whose slot it holds


class _Free:
    def acquire(self, timeout=None):
        return True

    def release(self):
        pass


def _ancestor_holds(cid, ep_id):
    seen = set()
    while cid and cid not in seen:
        seen.add(cid)
        if _holding.get(cid) == ep_id:
            return True
        cid = (_links.get(cid) or {}).get("parent")
    return False


def _finish(parent, child, ep, key, t0, why, s):
    from . import agent
    msgs = child.messages()
    answer = next((m.get("content") for m in reversed(msgs) if m.get("role") == "assistant" and (m.get("content") or "").strip()), "")
    tail = [m for m in msgs if m.get("role") in ("assistant", "error", "notice")]
    err = tail[-1].get("content") if tail and tail[-1].get("role") == "error" else None
    use = child.meta().get("usage") or {}
    secs = round(time.time() - t0, 1)
    status = "stopped" if why == "stopped" else "failed" if (err or not answer) else "done"
    info = {"status": status, "secs": secs, "usage": {k: use.get(k) for k in ("prompt_new", "prompt_cached", "out_tokens", "steps")}}
    if err:
        info["error"] = _cut(err, 600)
    _record(parent, key, info)
    agent.emit(parent.id, {"type": "sub", "call": key, "sub": child.id,
                           "ev": {"type": "sub_end", **info, "answer": _cut(answer, 8000)}})
    _links.pop(child.id, None)
    notify_parent_chip(child.id, status=status)
    pname = child.meta().get("sub_preset") or ""
    ep_label = ep.get("name") or ep["id"]
    who = f"{pname} ({ep_label})" if pname else ep_label
    head = f"[Subagent {who}: {use.get('steps') or 0} steps, {secs:.0f} s]\n\n"
    body = tools.clip(answer.strip(), 24_000) if answer else ""
    if status == "done":
        res = {"text": head + body, "ok": True}
    elif status == "stopped":
        res = {"text": "Stopped by the user before the subagent finished.", "ok": False}
    else:
        res = {"text": head + f"The subagent failed: {err or 'it ended without an answer'}" + (f"\n\nIts last reply:\n\n{body}" if body else ""),
               "ok": False}
    with _det_lock:
        late = _detached.pop(key, None)
        _recent.setdefault(parent.id, []).append({"text": res["text"], "child": child.id, "who": who,
                                                  "late": bool(late), "stopped": status == "stopped"})
        del _recent[parent.id][:-8]
    child.update_meta(touch=False, sub_owes=False)     # answered: returned to the caller, or handed over just below
    if late and status != "stopped":         # nobody is waiting for this result any more: hand it over as a message
        _deliver_late(parent, child, who, res)
    return res


# ------------------------------------------------------------------- a subagent that owes its main chat an answer
# A subagent chat that was given a TASK (by the main chat's model through the tool, by a side chat started with a
# prompt, or by a message sent with "report") owes the main chat its answer (meta sub_owes). However its turn ends,
# the answer is handed to the main chat; a restart in the middle carries the subagent on and the answer still arrives.
RESUME_NOTE = ("Flock restarted while you were working on the task above. Carry on from where you stopped and "
               "finish with your complete report.")


def owe(child_id, on=True):
    """This subagent chat's next answer is (or is no longer) owed to its main chat."""
    try:
        ch = store.Chat(child_id)
        if (ch.meta() or {}).get("sub_of"):
            ch.update_meta(touch=False, sub_owes=bool(on))
            return True
    except ValueError:
        pass
    return False


def turn_ended(child_id, stopped=False):
    """A subagent chat's own turn ended (agent._turn; not one a tool call is waiting on, which _finish answers).
    If it owes the main chat an answer, hand it over now. Returns True when something was delivered."""
    try:
        ch = store.Chat(child_id)
        cm = ch.meta() or {}
    except ValueError:
        return False
    if not cm.get("sub_of") or not cm.get("sub_owes"):
        return False
    link = _links.get(child_id)
    if link and not _detached.get(link.get("call")):
        return False                         # a call is still waiting for it: that call gets the answer
    if stopped:
        ch.update_meta(touch=False, sub_owes=False)      # the user stopped it: nothing is owed
        return False
    try:
        parent = store.Chat(cm["sub_of"])
        if not parent.meta():
            return False
    except ValueError:
        return False
    msgs = ch.messages()
    answer = next((m.get("content") for m in reversed(msgs) if m.get("role") == "assistant" and (m.get("content") or "").strip()), "")
    tail = [m for m in msgs if m.get("role") in ("assistant", "error")]
    err = tail[-1].get("content") if tail and tail[-1].get("role") == "error" else None
    who = cm.get("sub_preset") or cm.get("sub_name") or cm.get("endpoint") or "subagent"
    if answer and not err:
        text = tools.clip(answer.strip(), 24_000)
    else:
        text = f"The subagent ended without a report: {err or 'it gave no answer'}" + (f"\n\nIts last reply:\n\n{tools.clip(answer.strip(), 8000)}" if answer else "")
    ch.update_meta(touch=False, sub_owes=False)
    _deliver_late(parent, ch, who, {"text": text, "ok": bool(answer and not err)}, cut=False)
    return True


def resume_owed():
    """At start (server.main, after agent.resume_turns): a subagent that was working on a task for its main chat when
    the app went down, and that nothing is carrying on, is started again in its own chat. Returns the chat ids."""
    from . import agent
    started = []
    for parent_id, kids in list(_load_index().items()):
        for cid in [k for k in kids if isinstance(k, str)]:
            try:
                ch = store.Chat(cid)
                cm = ch.meta() or {}
            except ValueError:
                continue
            if not cm.get("sub_owes") or cm.get("deleted") or cm.get("sub_of") != parent_id or _busy(cid):
                continue
            if time.time() - float(cm.get("updated") or 0) > 6 * 3600:
                ch.update_meta(touch=False, sub_owes=False)          # too old to pick up by itself
                continue
            try:
                agent.start(cid, RESUME_NOTE, [])
                started.append(cid)
            except Exception:
                pass
    return started


_recent = {}                     # parent chat id -> the last few results (undelivered looks one up by its text)


def undelivered(body, res):
    """The app could not write a harness_agent result back (server.py): the engine that asked is gone. The report is
    handed to the parent chat as a message instead of being lost."""
    pid = str((body or {}).get("chat") or "")
    text = (res or {}).get("text") if isinstance(res, dict) else None
    with _det_lock:
        rec = next((r for r in reversed(_recent.get(pid) or []) if r["text"] == text and not r["late"]), None)
        if rec:
            rec["late"] = True
    if not rec or rec["stopped"]:
        return False
    try:
        _deliver_late(store.Chat(pid), store.Chat(rec["child"]), rec["who"], res)
    except ValueError:
        return False
    return True


STILL_RUNNING = ("The subagent is still working in its own chat: it was not stopped. The user sent a message, so this "
                 "call stopped waiting for it. Its report will be delivered to you here as a message when it finishes. "
                 "Read the user's message now; do not start this work again and do not wait in a loop for it.")


def tool(ctx, args, call_id):
    """The Agent tool in the harness's own loop. A message from the user ends the wait, not the subagent: the call
    returns at once (agent.STEER_GRACE after the message) and the report follows later (_deliver_late)."""
    meta = ctx.chat.meta()
    steer = getattr(ctx, "steer", None)
    if steer is None:
        return run(ctx.chat, meta, call_id, args, ctx.cancel)
    box, done = {}, threading.Event()

    def work():
        try:
            box["res"] = run(ctx.chat, meta, call_id, args, ctx.cancel)
        except tools.ToolError as e:
            box["res"] = {"text": f"Error: {e}", "ok": False}
        except Exception as e:
            box["res"] = {"text": f"Error: {e.__class__.__name__}: {e}", "ok": False}
        finally:
            done.set()
    t0 = time.time()
    threading.Thread(target=work, daemon=True).start()
    while not done.wait(0.3):
        if steer(t0) and not ctx.cancel.is_set() and detach_calls(ctx.chat.id, only=call_id):
            return {"text": STILL_RUNNING, "ok": True, "steered": True}
    return box["res"]


# ------------------------------------------------------------------------------------------------ engines (MCP)
def _python():
    """A console Python for the MCP server (pythonw has no standard streams of its own)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def mcp_server(chat, meta):
    """The harness MCP server for a Claude Code or Codex session of this chat: provides harness_agent (when allowed),
    harness_chats, and (inside a subagent chat) harness_parent_context + harness_report_to_main."""
    can_spawn = offered(meta)
    is_sub = bool((meta or {}).get("sub_of"))
    if not can_spawn and not is_sub:
        return None
    spec_file = chat.dir / "harness-agent-tool.json"
    if can_spawn and not spec_file.exists():
        spec_file.parent.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(json.dumps(spec(meta, engine=True), ensure_ascii=False), encoding="utf-8")
    return {"command": _python(), "args": [str(config.APP / "harness" / "servers" / "agents.py")],
            "env": {"KCH_AGENT_URL": f"http://127.0.0.1:{config.PORT}/api/subagent", "KCH_AGENT_TOKEN": TOKEN,
                    "KCH_AGENT_CHAT": chat.id, "KCH_AGENT_SPEC": str(spec_file),
                    "KCH_AGENT_CAN_SPAWN": "1" if can_spawn else "0",
                    "KCH_AGENT_IS_SUB": "1" if is_sub else "0",
                    "KCH_AGENT_TIMEOUT": str(CLI_WAIT_S)}}


_cancels = {}                    # MCP request key -> Event (the CLI cancelled its tool call)


def http(path, headers, body):
    """POST /api/subagent (+ /cancel, /parent-context, /report-main), from servers/agents.py."""
    from . import agent
    if not secrets.compare_digest(str(headers.get("X-KC-Agent-Token") or ""), TOKEN):
        raise PermissionError("not allowed")
    if path.endswith("/cancel"):
        ev = _cancels.get(str(body.get("key")))
        if ev:
            ev.set()
        return {"ok": bool(ev)}
    if path.endswith("/parent-context"):
        ch = store.Chat(str(body.get("chat") or ""))
        cm = ch.meta()
        pid = (cm or {}).get("sub_of")
        if not pid:
            return {"ok": False, "error": "this chat has no parent chat"}
        txt = search_parent(pid, action=body.get("action") or "search", query=body.get("query") or "",
                            start=int(body.get("start") or 1), limit=int(body.get("limit") or 10))
        return {"ok": True, "text": txt}
    if path.endswith("/report-main"):
        cid = str(body.get("chat") or "")
        try:
            res = report_to_main(cid, summary=body.get("summary") or "", wake_main=bool(body.get("wake_main")), source="agent")
            return {"ok": True, "text": res.get("message") or "Reported to main chat."}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    parent = store.Chat(str(body.get("chat") or ""))
    pmeta = parent.meta()
    if not pmeta:
        raise ValueError("no such chat")
    key = str(body.get("key") or secrets.token_hex(6))
    mine = _cancels[key] = threading.Event()
    prun = agent.runs.get(parent.id)
    call_id = _claim(parent.id, str(body.get("prompt") or ""))
    try:
        # Only the parent's Stop stops the subagent. When the CLI cancels its own tool call (it was interrupted for a
        # message from the user, or gave up waiting), `mine` is set: the subagent goes on and reports later.
        return run(parent, pmeta, call_id, body, _Any(prun.cancel if prun else None), gone=mine)
    except tools.ToolError as e:
        return {"text": f"Error: {e}", "ok": False}
    finally:
        _cancels.pop(key, None)

