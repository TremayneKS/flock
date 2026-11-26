"""Fork any chat (regular Harness chat, CLI-backed chat, imported chat, or Neon worker chat)
either in full or up to a specific message.

Every fork is a fresh, independent store.Chat with:
- Messages copied up to the fork point (with synthesized text/model_content for parts-only
  turns and dangling trailing tool_calls cleaned up so every OpenAI-compatible and CLI
  engine accepts the forked history cleanly).
- Parent notes.md and attachments copied (attachment paths rewritten to the fork's own
  attachments/ directory so deleting the parent never breaks the fork).
- ZERO shared mutable CLI session/thread/conversation IDs (claude_session, codex_thread,
  agy_conversation, engine_cursor, sub_of, neon_companion, source_* are never copied).
  On the first turn in the fork, OpenAI-compatible engines read chat.context_messages()
  directly, and CLI engines (Claude Code, Codex, Agy/Neon) automatically run
  chat_import.handover_prefix() from the forked messages.jsonl.
"""
import copy
import json
import re
import shutil
import time
from pathlib import Path

from . import config, groups, sections, store


def _fork_title(parent_title):
    base = (parent_title or "").strip()
    if not base or base.lower() == "new chat":
        return "New chat"
    m = re.match(r"^(.*?)\s*\(fork(?:\s+(\d+))?\)\s*$", base, flags=re.I)
    if m:
        stem = m.group(1).strip() or base
        n = int(m.group(2) or 1) + 1
        return f"{stem} (fork {n})"
    return f"{base} (fork)"


def _parts_to_text(parts):
    """Extract clean plain text and concise tool-step summary from parts-based messages (Neon / imported)."""
    texts = []
    steps = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        kind = p.get("kind")
        if kind == "text":
            t = str(p.get("text") or "").strip()
            if t:
                texts.append(t)
        elif kind == "tool":
            nm = str(p.get("name") or "tool")
            inp = str(p.get("input") or "").strip().replace("\n", " ")[:120]
            steps.append(f"{nm}({inp})" if inp else nm)
        elif kind in ("file", "image"):
            nm = str(p.get("name") or kind)
            txt = str(p.get("text") or "").strip()
            if txt:
                texts.append(f"[{kind}: {nm}]\n{txt[:4000]}")
            else:
                texts.append(f"[{kind}: {nm}]")
    content = "\n\n".join(texts).strip()
    model_content = content
    if steps:
        note = "[Tools used: " + "; ".join(steps[:20]) + (f"; +{len(steps) - 20} more" if len(steps) > 20 else "") + "]"
        model_content = (content + "\n\n" + note).strip() if content else note
    return content, model_content


def _copy_attachments(src_chat, dst_chat, msgs):
    """Copy parent attachments folder into the fork and rewrite any file paths inside it."""
    src_att = src_chat.dir / "attachments"
    if not src_att.is_dir():
        return
    dst_att = dst_chat.dir / "attachments"
    dst_att.mkdir(parents=True, exist_ok=True)
    src_prefix = str(src_att.resolve())
    for p in src_att.iterdir():
        if p.is_file():
            try:
                shutil.copy2(p, dst_att / p.name)
            except OSError:
                pass
    for m in msgs:
        for f in m.get("files") or []:
            if not isinstance(f, dict) or not f.get("path"):
                continue
            try:
                rp = str(Path(f["path"]).resolve())
                if rp.lower().startswith(src_prefix.lower()):
                    rel = Path(rp).relative_to(src_att.resolve())
                    new_p = dst_att / rel
                    if new_p.exists():
                        f["path"] = str(new_p)
            except Exception:
                pass


def _sanitize_messages(raw_msgs, from_msg_id=None):
    """Slice messages up to `from_msg_id` (inclusive) and ensure tool_call/tool pairing is valid."""
    if from_msg_id:
        idx = next((i for i, m in enumerate(raw_msgs) if m.get("id") == from_msg_id), None)
        if idx is None:
            raise ValueError("message not found in chat")
        end = idx + 1
        # If forking on an assistant message that issued tool_calls, include any immediately
        # following tool results for those calls so the tool step stays intact.
        target = raw_msgs[idx]
        if target.get("role") == "assistant" and target.get("tool_calls"):
            call_ids = {c.get("id") for c in target.get("tool_calls") or [] if c.get("id")}
            while end < len(raw_msgs) and raw_msgs[end].get("role") == "tool" and raw_msgs[end].get("tool_call_id") in call_ids:
                end += 1
        sliced = raw_msgs[:end]
    else:
        sliced = list(raw_msgs)

    # Filter transient/control cards that should not live as pending state in a fork
    keep_roles = {"user", "assistant", "tool", "compaction", "notice", "sub_report", "group_post"}
    copied = []
    for m in sliced:
        role = m.get("role")
        if role not in keep_roles:
            continue
        if m.get("hidden_handover"):
            continue
        c = copy.deepcopy(m)
        # Ensure parts-based messages (Neon / imported) also carry content + model_content
        # so both OpenAI-compatible APIs and CLI handover_prefix read them completely.
        if c.get("parts") and not (c.get("model_content") or c.get("content")):
            txt, mtxt = _parts_to_text(c["parts"])
            if txt:
                c["content"] = txt
            if mtxt and mtxt != txt:
                c["model_content"] = mtxt
        copied.append(c)

    # Verify every assistant tool_call has a matching tool result; if a fork cut mid-tool-call
    # or an external source omitted tool outputs, move unmatched tool_calls into model_content
    # so OpenAI-compatible providers never reject the next request with a 400.
    answered_ids = {m.get("tool_call_id") for m in copied if m.get("role") == "tool" and m.get("tool_call_id")}
    valid_call_ids = set()
    for m in copied:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            kept_calls = []
            dropped_notes = []
            for tc in m["tool_calls"]:
                tid = tc.get("id")
                if tid and tid in answered_ids:
                    kept_calls.append(tc)
                    valid_call_ids.add(tid)
                else:
                    dropped_notes.append(str(tc.get("name") or "tool"))
            if dropped_notes:
                if kept_calls:
                    m["tool_calls"] = kept_calls
                else:
                    m.pop("tool_calls", None)
                note = "[Tools called earlier: " + ", ".join(dropped_notes) + "]"
                base = (m.get("model_content") or m.get("content") or "").strip()
                m["model_content"] = (base + "\n\n" + note).strip() if base else note

    # Also drop orphan tool messages whose assistant call wasn't kept
    copied = [
        m for m in copied
        if m.get("role") != "tool" or (m.get("tool_call_id") in valid_call_ids)
    ]
    return copied


def _parent_section(cid, group_id=None):
    """Look up the parent chat's current section id (if any)."""
    try:
        d = sections._ensure()
        if cid in (d.get("chats") or {}):
            return d["chats"][cid]
        if group_id:
            g_by_id = {g["id"]: g for g in groups.all_groups()}
            rg = sections._root_group(group_id, g_by_id)
            if rg in (d.get("groups") or {}):
                return d["groups"][rg]
    except Exception:
        pass
    return None


def _resolve_endpoint(ep_id):
    if ep_id and config.endpoint(ep_id):
        return ep_id
    return config.get().get("endpoint") or "deepseek"


def fork_chat(cid, from_msg_id=None, title=None, endpoint=None, section=None):
    """Fork `cid` into a brand-new independent store.Chat.

    Returns {"id": new_id, "meta": meta_dict, "messages_copied": int}.
    """
    src_chat = store.Chat(cid)
    pmeta = src_chat.meta()
    if not pmeta:
        raise FileNotFoundError(f"chat not found: {cid}")
    raw_msgs = src_chat.messages()

    copied = _sanitize_messages(raw_msgs, from_msg_id=from_msg_id or None)

    project = pmeta.get("project") or str(config.WORKSPACE)
    store.add_project(project)

    ep = _resolve_endpoint(endpoint or pmeta.get("endpoint"))
    parent_title = (pmeta.get("title") or "New chat").strip()
    explicit_title = (title or "").strip()
    new_title = explicit_title or _fork_title(parent_title)

    group_id = pmeta.get("group")
    if group_id and not groups.get(group_id):
        group_id = None

    new_chat = store.create(
        project=project,
        endpoint=ep,
        title=new_title,
        reasoning=pmeta.get("reasoning"),
        group=group_id,
    )

    now = time.time()
    branch = {
        "kind": "fork",
        "of": parent_title,
        "of_chat": cid,
        "at": now,
    }
    if from_msg_id:
        branch["from_msg"] = from_msg_id

    extra = {
        "parent": cid,
        "branch": branch,
        "ctx": {},
        "usage": {},
    }
    if explicit_title or (parent_title and parent_title.lower() != "new chat"):
        extra["user_renamed"] = True
    if isinstance(pmeta.get("effort"), dict) and pmeta["effort"]:
        extra["effort"] = dict(pmeta["effort"])
    if pmeta.get("mode"):
        extra["mode"] = pmeta["mode"]
    if pmeta.get("mode") == "group" and isinstance(pmeta.get("group_chat"), dict):
        from . import group_chat
        # Pre-seed mode/effort on new_chat before creating fresh independent member/worker chats
        new_chat.update_meta(touch=False, mode="group",
                             **({"effort": dict(pmeta["effort"])} if isinstance(pmeta.get("effort"), dict) and pmeta["effort"] else {}))
        extra["group_chat"] = group_chat.fork_room_state(new_chat, pmeta["group_chat"], copied)
    if isinstance(pmeta.get("todos"), list) and pmeta["todos"] and not from_msg_id:
        extra["todos"] = copy.deepcopy(pmeta["todos"])

    # Copy parent attachments & rewrite paths before writing messages.jsonl
    if src_chat is not None:
        _copy_attachments(src_chat, new_chat, copied)
        notes = src_chat.notes()
        if notes:
            new_chat.set_notes(notes)
        if not from_msg_id:
            log_txt = src_chat.log()
            if log_txt:
                try:
                    new_chat.log_path.write_text(log_txt, encoding="utf-8")
                except OSError:
                    pass

    if copied:
        new_chat.rewrite(copied)

    meta = new_chat.update_meta(**extra)

    # Keep the fork in the same section as its parent (falling back to explicit section if parent has none)
    target_sec = _parent_section(cid, group_id) or section
    if not group_id and target_sec:
        sections.assign_new_chat(new_chat.id, target_sec, None)

    return {
        "id": new_chat.id,
        "meta": meta,
        "messages_copied": len(copied),
    }
