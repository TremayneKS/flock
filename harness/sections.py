"""Chat sections: top-level tabs in the chat list (default: General, Work, Personal; fully editable).

Stored in KCH_HOME/sections.json so sorting, moving and one-tap Undo never need to rewrite hundreds of chat.json files.
Rules:
  - Every root group belongs to one section; all its subgroups and chats inherit that section and move as one whole.
  - Every loose chat belongs to one section; a fork chain moves together with its root chat.
  - First-time initialisation sorts existing groups and loose chats using optional profile rules,
    records an undo snapshot, and exposes a dismissible banner so the whole sorting is one tap to undo.
  - Manual moves and AI moves (OrganizeChats tool) push onto the same undo stack and publish `sections_changed` over SSE.
"""
import copy
import json
import os
import re
import secrets
import threading
import time

from . import config, groups, store

_lock = threading.RLock()

DEFAULT_SECTIONS = [
    {"id": "general", "name": "General"},
    {"id": "work", "name": "Work"},
    {"id": "personal", "name": "Personal"},
]

# Generic automation / test / benchmark residue patterns (machine-specific rules go in profile.json `cleanup_rules`).
_TEST_PATH_RE = re.compile(
    r"(?:^|[\\/])(?:\.tmp|tmp|temp|scratchpad|kch-smoke|kch-engine-test|flock-test[^\\/]*|nest-test[^\\/]*|raven-test[^\\/]*|"
    r"claude-smoke|glm-smoke-test|quick-runs|runs)(?:[\\/]|$)",
    re.I,
)
_PROBE_TEXT_RE = re.compile(
    r"^(?:"
    r"reply with (?:the single word|just the word|one short sentence|one line)\b|"
    r"say hi in two words\b|"
    r"configuration check only\.|"
    r"efficiency check:|"
    r"run a small integration smoke test only\.|"
    r"what colour is the square in the image\?|"
    r"quote,\s*word for word,\s*the sentence in your instructions|"
    r"run (?:this exact bash command|`sleep\s+\d+|bash to run)|"
    r"use bash to run `sleep\s+\d+|"
    r"make three separate bash calls|"
    r"call your harness_agent tool once|"
    r"in the project folder \([^)]*(?:test|smoke|tmp)[^)]*\),\s*create a file named|"
    r"/doctor\b|"
    r"\(you are running inside (?:flock|nest|raven|kc harness)\.\s*this chat's notes file is\s+[^)]*(?:test|smoke|temp|tmp)|"
    r"<image name=\[image #\d+\] path=\"[^\"]*(?:test|smoke|temp|tmp)|"
    r"<scheduled-task\b|"
    r"<request source=\"assistant\">|"
    r"you are an ai [a-z0-9 _-]+ bot named\b|"
    r"start the job$"
    r")",
    re.I,
)
_HANDOFF_AUTO_RE = re.compile(
    r"^\(now:[^)]+\)\s*<(?:handoff|updates|transcript|helper_result)\b",
    re.I,
)
_ONE_WORD_GREETING_RE = re.compile(r"^(?:hi|hello|hey|test|ping|ok)\.?$", re.I)
_AUTO_ORIGINS = frozenset({"automation", "runner", "benchmark", "test", "subagent", "scheduled"})

MAX_UNDO = 25


def _path():
    return config.DATA / "sections.json"


def _slug(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:24] or "section"
    sid, n = base, 1
    while sid in taken:
        n += 1
        sid = f"{base}-{n}"
    return sid


def _default_sections():
    prof = config.get().get("sections") or []
    out, seen = [], set()
    for item in prof:
        if isinstance(item, str) and item.strip():
            sid = _slug(item.strip(), seen)
            seen.add(sid)
            out.append({"id": sid, "name": item.strip()[:40]})
        elif isinstance(item, dict) and item.get("name"):
            sid = re.sub(r"[^a-z0-9_-]+", "-", str(item.get("id") or item["name"]).lower()).strip("-")[:24] or _slug(item["name"], seen)
            if sid in seen:
                sid = _slug(item["name"], seen)
            seen.add(sid)
            out.append({"id": sid, "name": str(item["name"]).strip()[:40]})
    return out or copy.deepcopy(DEFAULT_SECTIONS)


def _load_raw():
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("sections"), list) and d["sections"]:
            d.setdefault("chats", {})
            d.setdefault("groups", {})
            d.setdefault("archived", {})
            d.setdefault("tidy_titles", {})
            d.setdefault("undo", [])
            return d
    except (OSError, ValueError):
        pass
    return None


def _save(d):
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = config.DATA / store._tmp_name("sections.json")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    store._replace(tmp, _path())


def _publish():
    try:
        from . import agent
        agent.hub.publish({"type": "sections_changed"})
    except Exception:
        pass


def _profile_rules():
    out = []
    for r in config.get().get("section_rules") or []:
        if isinstance(r, dict) and r.get("section") and r.get("pattern"):
            try:
                out.append((str(r["section"]).lower(), re.compile(str(r["pattern"]), re.I)))
            except re.error:
                pass
    return out


def _root_group(gid, g_by_id):
    seen, cur = set(), gid
    while cur and cur in g_by_id and cur not in seen:
        seen.add(cur)
        p = g_by_id[cur].get("parent")
        if not p or p not in g_by_id:
            return cur
        cur = p
    return gid


def _root_chat(cid, c_by_id):
    seen, cur = set(), cid
    while cur and cur in c_by_id and cur not in seen:
        seen.add(cur)
        p = c_by_id[cur].get("parent")
        if not p or p not in c_by_id:
            return cur
        cur = p
    return cid


def _chat_snippet(cid):
    """Fast read of a chat's opening user/assistant messages and closing messages (seek tail for huge chats)."""
    p = config.CHATS / cid / "messages.jsonl"
    if not p.is_file():
        return ""
    first_u = ""
    first_a = ""
    tail_msgs = []
    try:
        with p.open("rb") as f:
            for _ in range(40):
                raw = f.readline()
                if not raw:
                    break
                try:
                    m = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if m.get("auto"):
                    continue
                txt = (m.get("content") or "").strip()
                if not txt:
                    continue
                if m.get("role") == "user" and not first_u:
                    first_u = txt[:500]
                elif m.get("role") == "assistant" and not first_a:
                    first_a = txt[:350]
                if first_u and first_a:
                    break
            f.seek(0, os.SEEK_END)
            sz = f.tell()
            f.seek(max(0, sz - 24576))
            chunk = f.read().decode("utf-8", "replace").splitlines()
            if sz > 24576 and chunk:
                chunk = chunk[1:]
            for line in reversed(chunk):
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if m.get("role") in ("user", "assistant") and not m.get("auto"):
                    txt = (m.get("content") or "").strip()
                    if txt:
                        tail_msgs.append(txt[:300])
                        if len(tail_msgs) >= 2:
                            break
    except OSError:
        pass
    parts = [x for x in (first_u, first_a, *reversed(tail_msgs)) if x]
    return " ".join(parts)


def _clean_project_for_sort(project):
    """Keep meaningful project paths, ignoring generic Imported/scratch/workspace containers."""
    p = (project or "").replace("\\", "/").strip().rstrip("/")
    low = p.lower()
    if not low:
        return ""
    # Generic harness workspace / imported / test scratch folders do not imply a specific topic
    tail = low.rsplit("/", 1)[-1]
    if low.endswith("/workspace/imported") or low.endswith("/flock/workspace") or low.endswith("/nest/workspace") or low.endswith("/raven/workspace") or tail in ("imported", "scratch", "smoke", "steer", "temp", "tmp", "proj", "cla", "im", "cha", "ple"):
        return ""
    return p


def _match_text(blob, valid, fallback, rules):
    """Apply profile rules against a text blob. Returns section id or None."""
    if not blob:
        return None
    for sid, rx in rules:
        if sid in valid and rx.search(blob):
            return sid
    return None


def classify_item(title="", project="", endpoint="", mode="", group_name="", snippet="", sec_ids=None):
    """Decide the best section id for one group or loose chat from its title, folder, mode and message content."""
    sec_list = list(sec_ids or [s["id"] for s in DEFAULT_SECTIONS])
    valid = set(sec_list)
    fallback = "general" if "general" in valid else (sec_list[0] if sec_list else "general")
    rules = _profile_rules()

    _inside = ("(You are running inside Flock", "(You are running inside Nest", "(You are running inside Raven", "(You are running inside KC Harness")
    if title.lstrip().startswith(_inside) or snippet.lstrip().startswith(_inside) or "-test-paste" in title.lower():
        return fallback

    p_clean = _clean_project_for_sort(project)
    head_blob = f"{group_name} {title} {p_clean}".strip()
    hit = _match_text(head_blob, valid, fallback, rules)
    if hit:
        return hit

    if snippet:
        hit = _match_text(snippet, valid, fallback, rules)
        if hit:
            return hit

    return fallback


def _compute_autosort(sec_ids):
    """Build {groups: {gid: sid}, chats: {cid: sid}} for all existing groups and loose chats using content + metadata."""
    gs = groups.all_groups()
    g_by_id = {g["id"]: g for g in gs}
    chats = store.list_chats()
    c_by_id = {c["id"]: c for c in chats}

    # Index chats under their root group so group classification considers both the group name and what its chats are about
    chats_by_root_grp = {}
    for c in chats:
        gid = c.get("group")
        if gid and gid in g_by_id:
            rg = _root_group(gid, g_by_id)
            chats_by_root_grp.setdefault(rg, []).append(c)

    g_map = {}
    fallback = "work" if "work" in sec_ids else sec_ids[0]
    for g in gs:
        if g.get("parent"):
            continue
        name = g.get("name") or ""
        members = chats_by_root_grp.get(g["id"]) or []
        # First check explicit group name unless it's a generic bucket ("Other Claude Code sessions")
        name_hit = classify_item(group_name=name, sec_ids=sec_ids)
        if "other claude" in name.lower() or "meeting manager" in name.lower():
            g_map[g["id"]] = fallback
            continue
        if name_hit != fallback or not members:
            g_map[g["id"]] = name_hit
            continue
        # Group name alone fell back to Work: inspect member chats' titles and first/last messages and vote!
        votes = {}
        for mc in members[:16]:
            snip = _chat_snippet(mc["id"])
            ms = classify_item(
                title=mc.get("title") or "",
                project=mc.get("project") or "",
                endpoint=mc.get("endpoint") or "",
                snippet=snip,
                sec_ids=sec_ids,
            )
            votes[ms] = votes.get(ms, 0) + 1
        non_fb = [(cnt, sid) for sid, cnt in votes.items() if sid != fallback]
        non_fb.sort(reverse=True)
        # Switch group away from fallback when a non-fallback section wins at least half of classified members (or >=2 clear hits)
        if non_fb and (non_fb[0][0] >= max(2, len(members[:16]) // 2) or non_fb[0][0] == len(members[:16])):
            g_map[g["id"]] = non_fb[0][1]
        else:
            g_map[g["id"]] = fallback

    c_map = {}
    for c in chats:
        gid = c.get("group")
        if gid and gid in g_by_id:
            continue
        root = c_by_id[_root_chat(c["id"], c_by_id)]
        if root["id"] not in c_map:
            mode = store.Chat(root["id"]).meta().get("mode") or ""
            snip = _chat_snippet(root["id"])
            c_map[root["id"]] = classify_item(
                title=root.get("title") or "",
                project=root.get("project") or "",
                endpoint=root.get("endpoint") or "",
                mode=mode,
                snippet=snip,
                sec_ids=sec_ids,
            )
    return g_map, c_map


def _profile_cleanup_rules():
    raw = config.get().get("cleanup_rules") or {}
    prompts, folders = [], []
    for pat in raw.get("archive_prompts") or []:
        try:
            prompts.append(re.compile(str(pat), re.I))
        except re.error:
            pass
    for pat in raw.get("archive_folders") or []:
        try:
            folders.append(re.compile(str(pat), re.I))
        except re.error:
            pass
    return prompts, folders


def _msg_plain(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            (b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") in ("text", "input_text")
        ).strip()
    return ""


def _user_turns(cid):
    """Return (list_of_user_texts, last_message_ts) for one chat on disk."""
    p = config.CHATS / cid / "messages.jsonl"
    if not p.is_file():
        return [], 0.0
    out, last_ts = [], 0.0
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    m = json.loads(ln)
                except ValueError:
                    continue
                ts = float(m.get("ts") or 0)
                if ts > last_ts:
                    last_ts = ts
                if m.get("role") != "user" or m.get("hidden") or m.get("hidden_handover") or m.get("auto"):
                    continue
                s = _msg_plain(m.get("content"))
                if s:
                    out.append(s)
    except OSError:
        pass
    return out, last_ts


def classify_residue(meta, user_texts, last_msg_ts=0.0, group_name="", codex_subs=None, extra_rules=None, now=None):
    """Classify one chat as automation/throwaway residue (returning a short reason string) or None (keep)."""
    if not meta or meta.get("neon"):
        return None
    if meta.get("automated") or meta.get("ephemeral") or str(meta.get("origin") or "").lower() in _AUTO_ORIGINS:
        return "automation"
    now = now or time.time()
    title = (meta.get("title") or "").strip()
    proj = (meta.get("project") or "").strip()
    imp = meta.get("imported") or {}
    src = (imp.get("source") or "harness").lower()
    sid = str(imp.get("id") or "").lower()
    nu = len(user_texts)
    u1 = re.sub(r"\s+", " ", user_texts[0]).strip() if user_texts else ""
    ref_ts = last_msg_ts or float(meta.get("created") or meta.get("updated") or 0)
    age_h = (now - ref_ts) / 3600.0 if ref_ts else 999.0

    # 1. Empty chat (no user messages), leaving brand-new chats (< 1 h) open and never misclassifying large transcripts
    if nu == 0:
        mp = config.CHATS / str(meta.get("id") or "") / "messages.jsonl"
        try:
            if mp.is_file() and mp.stat().st_size > 16384:
                return None
        except OSError:
            pass
        if age_h > 1.0 or imp:
            return "empty"
        return None

    # 2. Subagent / side-chain sessions
    if imp.get("subagent") or (src == "codex" and codex_subs and sid in codex_subs):
        return "subagent"

    # 3. Automation / test / probe / bot / scheduled / assistant-request prompts
    if _PROBE_TEXT_RE.search(u1) or title.startswith("KC:") or title.lower().startswith("[test"):
        return "automation"

    # 4. Profile-configured residue rules
    p_prompts, p_folders = extra_rules if extra_rules is not None else _profile_cleanup_rules()
    if any(rx.search(u1) or rx.search(title) for rx in p_prompts):
        return "automation"
    if nu <= 3 and any(rx.search(proj) for rx in p_folders):
        return "test-folder"

    # 5. Automated meeting-backend XML handoff/transcript sessions with zero human-typed turns
    if _HANDOFF_AUTO_RE.search(u1):
        if all(_HANDOFF_AUTO_RE.search(re.sub(r"\s+", " ", x).strip()) for x in user_texts):
            return "auto-handoff"

    # 6. Test/scratch/runs working folder with <= 3 turns
    if _TEST_PATH_RE.search(proj) and nu <= 3:
        return "test-folder"

    # 7. Old 1-turn bare greeting / ping (> 24 h) or classifier-aborted 1-turn side chat
    if nu == 1 and age_h > 24.0 and _ONE_WORD_GREETING_RE.match(u1):
        return "one-turn-old"
    if nu == 1 and "your response above was stopped by a safety classifier" in u1.lower():
        return "aborted-side"

    return None


_FILLER_LEAD_RE = re.compile(
    r"^(?:"
    r"you(?:'re|re) a fork[.,!:\s]+|"
    r"(?:hey|hi|hello|okay|ok|alright|right|please|well|so)\b[.,!:\s]+|"
    r"(?:chatgpt|claude)\s+(?:fking|fucking|is refusing to|please)?\s*|"
    r"i want you to (?:do a nice long search for me\.?\s*i want you to\s*)?|"
    r"i (?:wanna|want to|need you to|need to|need)\s+|"
    r"please (?:go work on|work on|help me)\s+|"
    r"can you (?:please\s+)?|"
    r"look up\s+|"
    r"im going to\s+"
    r")+",
    re.I,
)
_TRAILING_DANGLE = frozenset({
    "to", "of", "for", "with", "from", "the", "a", "an", "my", "your", "our", "their",
    "i", "we", "you", "and", "or", "but", "in", "on", "at", "by", "can", "is", "are",
    "that", "this", "it", "its", "me", "into", "before", "after", "about",
})
_GENERIC_TITLE_RE = re.compile(r"^(?:new chat|hi|hello|hey)$", re.I)
_RAW_SENTENCE_LEAD_RE = re.compile(
    r"^(?:you(?:'re|re) a fork|claude |chatgpt |please go |i want you to |i wanna |look up how |\(now:)",
    re.I,
)


def suggest_tidy_title(title, user_texts, user_renamed=False):
    """Suggest a short readable title only for 'New chat', bare greetings, or raw first-sentence titles."""
    if user_renamed or not user_texts:
        return None
    t = (title or "").strip()
    is_generic = not t or bool(_GENERIC_TITLE_RE.match(t))
    is_raw = t.endswith("…") or t.endswith("...") or (len(t) >= 44 and bool(_RAW_SENTENCE_LEAD_RE.match(t))) or (t and t[0].islower() and len(t) >= 34)
    if not is_generic and not is_raw:
        return None

    # Pick first substantive user message (skip bare greetings and 'Go on: ...'; prefer a richer prompt if u1 is tiny)
    cand = ""
    for u in user_texts[:12]:
        clean = re.sub(r"\s+", " ", u).strip()
        if not clean or _ONE_WORD_GREETING_RE.match(clean) or clean.lower().startswith("go on:"):
            continue
        if len(clean) >= 20:
            cand = clean
            break
        if not cand and len(clean) >= 8:
            cand = clean
    if not cand:
        return None

    s = _FILLER_LEAD_RE.sub("", cand).strip()
    s = re.sub(r"^[\W_]+", "", s).strip()
    if len(s) < 6:
        s = cand
    # Prefer the first sentence or comma-clause when it is already a clear phrase
    first_sent = re.split(r"[.?!\n]\s+", s, maxsplit=1)[0].strip()
    if len(first_sent) >= 14:
        s = first_sent
    if len(s) > 48:
        comma_head = s.split(",", 1)[0].strip()
        if 14 <= len(comma_head) <= 48:
            s = comma_head
        else:
            cut = s[:48].rsplit(" ", 1)[0].strip(" ,;:-")
            s = cut if len(cut) >= 14 else s[:48].strip(" ,;:-")
    # Drop trailing dangling prepositions/articles/pronouns so titles never look cut off mid-thought
    words = s.split()
    while len(words) > 3 and words[-1].rstrip(",;:-?!").lower() in _TRAILING_DANGLE:
        words.pop()
    s = " ".join(words).strip(" ,;:-")
    if not s:
        return None
    s = s[0].upper() + s[1:]
    if s.lower() == t.lower():
        return None
    return s


def _compute_cleanup(chats=None, gs=None, existing_archived=None, existing_titles=None):
    """Scan chats on disk and compute (archived_map, tidy_titles_map, reason_counts)."""
    chats = chats if chats is not None else store.list_chats()
    gs = gs if gs is not None else groups.all_groups()
    g_by_id = {g["id"]: g for g in gs}
    try:
        from . import chat_import
        codex_subs = chat_import.codex_subagent_ids()
    except Exception:
        codex_subs = frozenset()
    extra_rules = _profile_cleanup_rules()
    now = time.time()

    archived = dict(existing_archived or {})
    titles = dict(existing_titles or {})
    reasons = {}
    new_archived = 0
    new_tidied = 0

    # First pass: classify each chat and gather fingerprints for duplicate detection
    seen_fp = {}
    per_chat = []
    for c in chats:
        cid = c["id"]
        try:
            meta = store.Chat(cid).meta()
        except Exception:
            continue
        um, last_ts = _user_turns(cid)
        g = g_by_id.get(meta.get("group") or "")
        gname = g.get("name", "") if g else ""
        per_chat.append((cid, meta, um, last_ts, gname))

    # Duplicate check among imported chats (same source + same first & last user message + same count within 60 s)
    dup_ids = set()
    for cid, meta, um, last_ts, gname in sorted(per_chat, key=lambda x: (-len(x[2]), float(x[1].get("created") or 0))):
        imp = meta.get("imported") or {}
        src = (imp.get("source") or "").lower()
        if not src or not um:
            continue
        fp = (src, re.sub(r"\s+", " ", um[0])[:120], re.sub(r"\s+", " ", um[-1])[:120], len(um))
        created = float(meta.get("created") or 0)
        prev = seen_fp.get(fp)
        if prev is not None and abs(created - prev[1]) <= 90.0:
            dup_ids.add(cid)
        else:
            seen_fp[fp] = (cid, created)

    for cid, meta, um, last_ts, gname in per_chat:
        if cid in archived:
            continue
        why = classify_residue(meta, um, last_ts, gname, codex_subs=codex_subs, extra_rules=extra_rules, now=now)
        if not why and cid in dup_ids:
            why = "duplicate"
        if why:
            archived[cid] = why
            reasons[why] = reasons.get(why, 0) + 1
            new_archived += 1
        else:
            if cid not in titles and not meta.get("user_renamed"):
                nt = suggest_tidy_title(meta.get("title") or "", um, user_renamed=bool(meta.get("user_renamed")))
                if nt:
                    titles[cid] = nt
                    new_tidied += 1

    return archived, titles, reasons, new_archived, new_tidied


def _ensure():
    with _lock:
        d = _load_raw()
        if d is not None:
            if not d.get("cleaned_once"):
                prev_arch = dict(d.get("archived") or {})
                prev_titles = dict(d.get("tidy_titles") or {})
                arch, titles, reasons, n_arch, n_tidy = _compute_cleanup(
                    existing_archived=prev_arch, existing_titles=prev_titles
                )
                d["cleaned_once"] = True
                if n_arch or n_tidy:
                    uid = "u-" + secrets.token_hex(4)
                    label = f"Archived {n_arch} automated and throwaway chat{'s' if n_arch != 1 else ''}" if n_arch else f"Tidied {n_tidy} chat titles"
                    d["archived"] = arch
                    d["tidy_titles"] = titles
                    if n_arch:
                        d["cleanup_banner"] = {"count": n_arch, "tidied": n_tidy, "undo_id": uid, "text": label}
                    d["undo"].append({
                        "id": uid,
                        "ts": time.time(),
                        "label": label,
                        "prev_chats": dict(d.get("chats") or {}),
                        "prev_groups": dict(d.get("groups") or {}),
                        "prev_archived": prev_arch,
                        "prev_titles": prev_titles,
                        "source": "cleanup",
                    })
                    d["undo"] = d["undo"][-MAX_UNDO:]
                _save(d)
            return d
        secs = _default_sections()
        sec_ids = [s["id"] for s in secs]
        g_map, c_map = _compute_autosort(sec_ids)
        arch, titles, reasons, n_arch, n_tidy = _compute_cleanup()
        uid_sort = "u-" + secrets.token_hex(4)
        has_items = bool(g_map or c_map)
        undo_stack = []
        if has_items:
            undo_stack.append({
                "id": uid_sort,
                "ts": time.time(),
                "label": "Initial sorting of chats into sections",
                "prev_chats": {},
                "prev_groups": {},
                "prev_archived": {},
                "prev_titles": {},
                "source": "autosort",
            })
        cleanup_banner = None
        if n_arch or n_tidy:
            uid_clean = "u-" + secrets.token_hex(4)
            label = f"Archived {n_arch} automated and throwaway chat{'s' if n_arch != 1 else ''}" if n_arch else f"Tidied {n_tidy} chat titles"
            if n_arch:
                cleanup_banner = {"count": n_arch, "tidied": n_tidy, "undo_id": uid_clean, "text": label}
            undo_stack.append({
                "id": uid_clean,
                "ts": time.time(),
                "label": label,
                "prev_chats": dict(c_map),
                "prev_groups": dict(g_map),
                "prev_archived": {},
                "prev_titles": {},
                "source": "cleanup",
            })
        d = {
            "version": 2,
            "sections": secs,
            "default_section": sec_ids[0],
            "groups": g_map,
            "chats": c_map,
            "archived": arch,
            "tidy_titles": titles,
            "cleaned_once": True,
            "auto_sorted": True,
            "banner": has_items and not cleanup_banner,
            "banner_undo_id": uid_sort if has_items else None,
            "cleanup_banner": cleanup_banner,
            "undo": undo_stack,
        }
        _save(d)
        return d


def state():
    """Full section state for the UI (resolving every group & chat, plus archive, banners and undo availability)."""
    with _lock:
        d = _ensure()
        return {
            "sections": list(d["sections"]),
            "default_section": d.get("default_section") or d["sections"][0]["id"],
            "chats": dict(d.get("chats") or {}),
            "groups": dict(d.get("groups") or {}),
            "archived": dict(d.get("archived") or {}),
            "tidy_titles": dict(d.get("tidy_titles") or {}),
            "banner": bool(d.get("banner")),
            "banner_undo_id": d.get("banner_undo_id"),
            "cleanup_banner": d.get("cleanup_banner") or None,
            "undo_count": len(d.get("undo") or []),
            "last_undo": (d["undo"][-1]["label"] if d.get("undo") else ""),
        }


def resolve_section(name_or_id, d=None):
    """Find a section id from an id or human name (case-insensitive)."""
    d = d or _ensure()
    want = str(name_or_id or "").strip().lower()
    if not want:
        raise ValueError("section name is required")
    for s in d["sections"]:
        if s["id"].lower() == want or s["name"].lower() == want:
            return s
    # Prefix / substring fallback
    for s in d["sections"]:
        if want in s["name"].lower():
            return s
    names = ", ".join(s["name"] for s in d["sections"])
    raise ValueError(f"no section called {name_or_id!r} (available: {names})")


def chat_section(chat_entry, d=None, g_by_id=None, c_by_id=None):
    """Resolved section id for one chat entry (group inheritance -> root chat -> default)."""
    d = d or _ensure()
    valid = {s["id"] for s in d["sections"]}
    default = d.get("default_section") or d["sections"][0]["id"]
    if default not in valid:
        default = d["sections"][0]["id"]

    # Neon external chats default to the configured neon section (usually 'phone') unless explicitly moved
    if chat_entry.get("neon"):
        cid = chat_entry["id"]
        if cid in d["chats"] and d["chats"][cid] in valid:
            return d["chats"][cid]
        ns = (config.get().get("neon") or {}).get("section") or "phone"
        return ns if ns in valid else default

    if g_by_id is None:
        g_by_id = {g["id"]: g for g in groups.all_groups()}
    gid = chat_entry.get("group")
    if gid and gid in g_by_id:
        rg = _root_group(gid, g_by_id)
        sid = d["groups"].get(rg)
        if sid in valid:
            return sid
        return default

    cid = chat_entry["id"]
    if cid in d["chats"] and d["chats"][cid] in valid:
        return d["chats"][cid]
    if c_by_id:
        rc = _root_chat(cid, c_by_id)
        if rc in d["chats"] and d["chats"][rc] in valid:
            return d["chats"][rc]
    return default


def group_section(gid, d=None, g_by_id=None):
    d = d or _ensure()
    valid = {s["id"] for s in d["sections"]}
    default = d.get("default_section") or d["sections"][0]["id"]
    if g_by_id is None:
        g_by_id = {g["id"]: g for g in groups.all_groups()}
    rg = _root_group(gid, g_by_id)
    sid = d["groups"].get(rg)
    return sid if sid in valid else default


def is_archived(cid, d=None):
    d = d or _ensure()
    return cid in (d.get("archived") or {})


def archive_reason(cid, d=None):
    d = d or _ensure()
    return (d.get("archived") or {}).get(cid) or ""


def display_title(chat_entry, d=None):
    """Return tidy display title when available and the user has not explicitly renamed the chat."""
    d = d or _ensure()
    raw = chat_entry.get("title") or ""
    if chat_entry.get("user_renamed"):
        return raw
    return (d.get("tidy_titles") or {}).get(chat_entry["id"]) or raw


def clear_tidy_title(cid):
    with _lock:
        d = _ensure()
        if cid in (d.get("tidy_titles") or {}):
            d["tidy_titles"].pop(cid, None)
            _save(d)


def mark_automated(cid, reason="automation"):
    """Route a newly created automated/runner/benchmark session straight into Archived."""
    with _lock:
        d = _ensure()
        d.setdefault("archived", {})[cid] = str(reason or "automation")
        _save(d)
    _publish()


def _check_new_chat_residue(d, cid, meta, g_by_id=None):
    """Check one newly imported/created chat against residue rules; archive or tidy title in place. Returns changed bool."""
    if not meta or cid in (d.get("archived") or {}):
        return False
    if (meta.get("branch") or {}).get("kind") == "fork":
        return False
    um, last_ts = _user_turns(cid)
    gname = ""
    gid = meta.get("group")
    if gid:
        g_by_id = g_by_id or {g["id"]: g for g in groups.all_groups()}
        gname = (g_by_id.get(gid) or {}).get("name") or ""
    try:
        from . import chat_import
        codex_subs = chat_import.codex_subagent_ids()
    except Exception:
        codex_subs = frozenset()
    why = classify_residue(meta, um, last_ts, gname, codex_subs=codex_subs)
    if why:
        d.setdefault("archived", {})[cid] = why
        return True
    if not meta.get("user_renamed") and cid not in (d.get("tidy_titles") or {}):
        nt = suggest_tidy_title(meta.get("title") or "", um, user_renamed=False)
        if nt:
            d.setdefault("tidy_titles", {})[cid] = nt
            return True
    return False


def assign_new_chat(cid, section_id=None, group_id=None):
    """Assign a newly created or newly imported chat to an explicit section, its group/parent section, or auto-sort it.
    Also applies residue auto-archiving and title tidying for newly imported or automated sessions."""
    with _lock:
        d = _ensure()
        valid = {s["id"] for s in d["sections"]}
        if group_id:
            try:
                meta = store.Chat(cid).meta()
                if _check_new_chat_residue(d, cid, meta):
                    _save(d)
                    _publish()
            except Exception:
                pass
            return
        if section_id and section_id in valid:
            d["chats"][cid] = section_id
            _save(d)
            return section_id
        # Auto-classify newly imported chat (keeping group / fork-parent inheritance)
        try:
            meta = store.Chat(cid).meta()
        except Exception:
            return None
        g_by_id = {g["id"]: g for g in groups.all_groups()}
        changed = _check_new_chat_residue(d, cid, meta, g_by_id)
        gid = meta.get("group")
        if gid:
            rg = _root_group(gid, g_by_id)
            if rg not in d["groups"]:
                gname = (g_by_id.get(rg) or {}).get("name") or ""
                sec_ids = [s["id"] for s in d["sections"]]
                d["groups"][rg] = classify_item(
                    group_name=gname,
                    title=meta.get("title") or "",
                    project=meta.get("project") or "",
                    snippet=_chat_snippet(cid),
                    sec_ids=sec_ids,
                )
                changed = True
            if changed:
                _save(d)
                _publish()
            return d["groups"].get(rg)
        if cid in d["chats"]:
            if changed:
                _save(d)
                _publish()
            return d["chats"][cid]
        pid = meta.get("parent")
        if pid and pid in d["chats"]:
            d["chats"][cid] = d["chats"][pid]
            _save(d)
            _publish()
            return d["chats"][cid]
        sec_ids = [s["id"] for s in d["sections"]]
        sid = classify_item(
            title=meta.get("title") or "",
            project=meta.get("project") or "",
            endpoint=meta.get("endpoint") or "",
            mode=meta.get("mode") or "",
            snippet=_chat_snippet(cid),
            sec_ids=sec_ids,
        )
        d["chats"][cid] = sid
        _save(d)
        _publish()
        return sid


def assign_new_group(gid, section_id=None):
    with _lock:
        d = _ensure()
        valid = {s["id"] for s in d["sections"]}
        if section_id and section_id in valid:
            d["groups"][gid] = section_id
            _save(d)


def move(section_name_or_id, chat_ids=None, group_ids=None, source="user", label=None):
    """Move chats and/or groups to a target section. Groups move as a whole; loose fork trees move together.
    Returns {ok, section, moved_chats, moved_groups, undo_id, label, state}."""
    with _lock:
        d = _ensure()
        sec = resolve_section(section_name_or_id, d)
        sid = sec["id"]
        gs = groups.all_groups()
        g_by_id = {g["id"]: g for g in gs}
        all_chats = store.list_chats()
        c_by_id = {c["id"]: c for c in all_chats}

        prev_chats = dict(d["chats"])
        prev_groups = dict(d["groups"])

        target_groups = set()
        for gid in group_ids or []:
            if gid in g_by_id:
                target_groups.add(_root_group(gid, g_by_id))

        target_chats = set()
        for cid in chat_ids or []:
            if not cid:
                continue
            if str(cid).startswith("neon-"):
                target_chats.add(cid)
                continue
            c = c_by_id.get(cid)
            if not c:
                continue
            if c.get("group") and c["group"] in g_by_id:
                # Moving a chat that belongs to a group: move its whole group when requested via group,
                # or take the chat out of the group? Brief: "Every chat and every group of forks belongs to one section;
                # a group moves as a whole." So moving a grouped chat moves its root group cleanly!
                target_groups.add(_root_group(c["group"], g_by_id))
            else:
                root = _root_chat(cid, c_by_id)
                target_chats.add(root)
                # Include all descendants of that root chat
                for other in all_chats:
                    if not other.get("group") and _root_chat(other["id"], c_by_id) == root:
                        target_chats.add(other["id"])

        moved_groups = 0
        for rg in target_groups:
            if d["groups"].get(rg) != sid:
                d["groups"][rg] = sid
                moved_groups += 1

        moved_chats = 0
        for cid in target_chats:
            if d["chats"].get(cid) != sid:
                d["chats"][cid] = sid
                moved_chats += 1

        if not moved_groups and not moved_chats:
            return {"ok": True, "section": sec, "moved_chats": 0, "moved_groups": 0, "undo_id": None,
                    "label": f"Already in {sec['name']}", "state": state()}

        # Build human-readable summary label
        if not label:
            parts = []
            if moved_groups:
                gnames = [g_by_id[g]["name"] for g in target_groups if g in g_by_id]
                parts.append(", ".join(f'"{n}"' for n in gnames[:3]) + (f" (+{len(gnames) - 3})" if len(gnames) > 3 else ""))
            if moved_chats:
                parts.append(f"{moved_chats} chat{'s' if moved_chats != 1 else ''}")
            label = f"Moved {' and '.join(parts)} to {sec['name']}"

        uid = "u-" + secrets.token_hex(4)
        d["undo"].append({
            "id": uid,
            "ts": time.time(),
            "label": label,
            "prev_chats": prev_chats,
            "prev_groups": prev_groups,
            "prev_archived": dict(d.get("archived") or {}),
            "prev_titles": dict(d.get("tidy_titles") or {}),
            "source": source,
        })
        d["undo"] = d["undo"][-MAX_UNDO:]
        _save(d)
    _publish()
    return {"ok": True, "section": sec, "moved_chats": moved_chats, "moved_groups": moved_groups,
            "undo_id": uid, "label": label, "state": state()}


def _group_descendants(gid, gs):
    below = {gid}
    changed = True
    while changed:
        changed = False
        for g in gs:
            if g.get("parent") in below and g["id"] not in below:
                below.add(g["id"])
                changed = True
    return below


def archive(chat_ids=None, group_ids=None, archived=True, reason="manual", source="user", label=None):
    """Archive or unarchive chats and/or whole groups (never deletes anything on disk; 1-tap undoable)."""
    with _lock:
        d = _ensure()
        gs = groups.all_groups()
        g_by_id = {g["id"]: g for g in gs}
        all_chats = store.list_chats()
        c_by_id = {c["id"]: c for c in all_chats}

        prev_arch = dict(d.get("archived") or {})
        prev_titles = dict(d.get("tidy_titles") or {})
        targets = set()
        g_names = []
        for gid in group_ids or []:
            if gid in g_by_id:
                g_names.append(g_by_id[gid].get("name") or gid)
                below = _group_descendants(gid, gs)
                for c in all_chats:
                    if c.get("group") in below:
                        targets.add(c["id"])
        for cid in chat_ids or []:
            if not cid:
                continue
            if str(cid).startswith("neon-") or cid in c_by_id:
                targets.add(cid)

        arch_map = dict(prev_arch)
        changed = 0
        for cid in targets:
            if archived:
                if cid not in arch_map:
                    arch_map[cid] = str(reason or "manual")
                    changed += 1
            else:
                if cid in arch_map:
                    arch_map.pop(cid, None)
                    changed += 1

        if not changed:
            verb = "Already archived" if archived else "Not archived"
            return {"ok": True, "archived": bool(archived), "changed": 0, "undo_id": None, "label": verb, "state": state()}

        if not label:
            verb = "Archived" if archived else "Restored"
            if g_names and not chat_ids:
                g_str = ", ".join(f'"{n}"' for n in g_names[:2]) + (f" (+{len(g_names) - 2})" if len(g_names) > 2 else "")
                label = f"{verb} {changed} chat{'s' if changed != 1 else ''} in {g_str}"
            else:
                label = f"{verb} {changed} chat{'s' if changed != 1 else ''}"

        uid = "u-" + secrets.token_hex(4)
        d["archived"] = arch_map
        d["undo"].append({
            "id": uid,
            "ts": time.time(),
            "label": label,
            "prev_chats": dict(d.get("chats") or {}),
            "prev_groups": dict(d.get("groups") or {}),
            "prev_archived": prev_arch,
            "prev_titles": prev_titles,
            "source": source,
        })
        d["undo"] = d["undo"][-MAX_UNDO:]
        _save(d)
    _publish()
    return {"ok": True, "archived": bool(archived), "changed": changed, "undo_id": uid, "label": label, "state": state()}


def cleanup(source="user"):
    """Run residue archiving + title tidying across all chats (1-tap undoable)."""
    with _lock:
        d = _ensure()
        prev_arch = dict(d.get("archived") or {})
        prev_titles = dict(d.get("tidy_titles") or {})
        arch, titles, reasons, n_arch, n_tidy = _compute_cleanup(
            existing_archived=prev_arch, existing_titles=prev_titles
        )
        d["cleaned_once"] = True
        if not n_arch and not n_tidy:
            _save(d)
            return {"ok": True, "archived_count": 0, "tidied_count": 0, "reasons": {},
                    "undo_id": None, "label": "Already clean (no automated residue found)", "state": state()}
        uid = "u-" + secrets.token_hex(4)
        if n_arch and n_tidy:
            label = f"Archived {n_arch} automated and throwaway chat{'s' if n_arch != 1 else ''} and tidied {n_tidy} title{'s' if n_tidy != 1 else ''}"
        elif n_arch:
            label = f"Archived {n_arch} automated and throwaway chat{'s' if n_arch != 1 else ''}"
        else:
            label = f"Tidied {n_tidy} chat title{'s' if n_tidy != 1 else ''}"
        d["archived"] = arch
        d["tidy_titles"] = titles
        if n_arch:
            d["cleanup_banner"] = {"count": n_arch, "tidied": n_tidy, "undo_id": uid, "text": label}
        d["undo"].append({
            "id": uid,
            "ts": time.time(),
            "label": label,
            "prev_chats": dict(d.get("chats") or {}),
            "prev_groups": dict(d.get("groups") or {}),
            "prev_archived": prev_arch,
            "prev_titles": prev_titles,
            "source": source,
        })
        d["undo"] = d["undo"][-MAX_UNDO:]
        _save(d)
    _publish()
    return {"ok": True, "archived_count": n_arch, "tidied_count": n_tidy, "reasons": reasons,
            "undo_id": uid, "label": label, "state": state()}


def undo(undo_id=None):
    """Restore section assignments, archived state, and tidy titles from before the specified (or latest) action."""
    with _lock:
        d = _ensure()
        stack = d.get("undo") or []
        if not stack:
            return {"ok": False, "error": "Nothing to undo", "state": state()}
        idx = len(stack) - 1
        if undo_id:
            found = next((i for i, u in enumerate(stack) if u.get("id") == undo_id), None)
            if found is None:
                return {"ok": False, "error": "That change can no longer be undone", "state": state()}
            idx = found
        entry = stack[idx]
        d["chats"] = dict(entry.get("prev_chats") or {})
        d["groups"] = dict(entry.get("prev_groups") or {})
        if "prev_archived" in entry:
            d["archived"] = dict(entry.get("prev_archived") or {})
        if "prev_titles" in entry:
            d["tidy_titles"] = dict(entry.get("prev_titles") or {})
        d["undo"] = stack[:idx]
        if entry.get("id") == d.get("banner_undo_id"):
            d["banner"] = False
            d["banner_undo_id"] = None
        if d.get("cleanup_banner") and entry.get("id") == d["cleanup_banner"].get("undo_id"):
            d["cleanup_banner"] = None
        _save(d)
    _publish()
    return {"ok": True, "label": entry.get("label") or "Undone", "state": state()}


def dismiss_banner(which=None):
    with _lock:
        d = _ensure()
        if which == "cleanup":
            d["cleanup_banner"] = None
        elif which == "autosort":
            d["banner"] = False
        else:
            d["banner"] = False
            d["cleanup_banner"] = None
        _save(d)
    return {"ok": True, "state": state()}


def autosort():
    """Re-run automatic sorting across all groups and loose chats (undoable)."""
    with _lock:
        d = _ensure()
        prev_chats = dict(d["chats"])
        prev_groups = dict(d["groups"])
        sec_ids = [s["id"] for s in d["sections"]]
        g_map, c_map = _compute_autosort(sec_ids)
        uid = "u-" + secrets.token_hex(4)
        d["groups"] = g_map
        d["chats"] = c_map
        d["banner"] = True
        d["banner_undo_id"] = uid
        d["undo"].append({
            "id": uid,
            "ts": time.time(),
            "label": "Sorted chats into sections",
            "prev_chats": prev_chats,
            "prev_groups": prev_groups,
            "prev_archived": dict(d.get("archived") or {}),
            "prev_titles": dict(d.get("tidy_titles") or {}),
            "source": "autosort",
        })
        d["undo"] = d["undo"][-MAX_UNDO:]
        _save(d)
    _publish()
    return {"ok": True, "undo_id": uid, "state": state()}


def save_sections(items):
    """Update the list of sections (rename, add, delete, reorder). Chats in deleted sections move to the first section."""
    if not isinstance(items, list) or not items:
        raise ValueError("at least one section is needed")
    with _lock:
        d = _ensure()
        clean, seen = [], set()
        for it in items[:12]:
            if not isinstance(it, dict):
                continue
            name = " ".join(str(it.get("name") or "").split())[:32]
            if not name:
                continue
            raw_id = re.sub(r"[^a-z0-9_-]+", "-", str(it.get("id") or "").lower()).strip("-")[:24]
            sid = raw_id if (raw_id and raw_id not in seen) else _slug(name, seen)
            seen.add(sid)
            clean.append({"id": sid, "name": name})
        if not clean:
            raise ValueError("at least one named section is needed")
        first = clean[0]["id"]
        valid = {s["id"] for s in clean}
        d["sections"] = clean
        d["default_section"] = first
        for k, v in list(d["chats"].items()):
            if v not in valid:
                d["chats"][k] = first
        for k, v in list(d["groups"].items()):
            if v not in valid:
                d["groups"][k] = first
        _save(d)
    _publish()
    return {"ok": True, "state": state()}


# ------------------------------------------------------------------------------------------------ AI tool helper
def _resolve_targets(args, gs, g_by_id, chats, c_by_id, include_neon=True):
    g_ids = list(args.get("groups") or [])
    c_ids = list(args.get("chats") or [])
    query = str(args.get("query") or "").strip().lower()
    if isinstance(g_ids, str):
        g_ids = [g_ids]
    if isinstance(c_ids, str):
        c_ids = [c_ids]
    resolved_g = []
    for g in g_ids:
        if g in g_by_id:
            resolved_g.append(g)
        else:
            gl = str(g).strip().lower()
            for x in gs:
                if (x.get("name") or "").lower() == gl or (gl and gl in (x.get("name") or "").lower()):
                    resolved_g.append(x["id"])
    resolved_c = []
    for c in c_ids:
        if c in c_by_id or str(c).startswith("neon-"):
            resolved_c.append(c)
        else:
            cl = str(c).strip().lower()
            for x in chats:
                if cl and cl in (x.get("title") or "").lower():
                    resolved_c.append(x["id"])
    if query:
        for g in gs:
            if query in (g.get("name") or "").lower():
                resolved_g.append(g["id"])
        for c in chats:
            blob = f"{c.get('title', '')} {c.get('project', '')}".lower()
            if query in blob:
                resolved_c.append(c["id"])
    return resolved_g, resolved_c


def ai_manage(args, chat=None, emit=None):
    """Run an OrganizeChats action from a model (list | move | archive | unarchive | cleanup | undo)."""
    act = str(args.get("action") or "list").strip().lower()
    d = _ensure()
    gs = groups.all_groups()
    g_by_id = {g["id"]: g for g in gs}
    chats = store.list_chats()
    c_by_id = {c["id"]: c for c in chats}
    arch_map = d.get("archived") or {}

    if act == "list":
        q = str(args.get("query") or "").strip().lower()
        sec_names = {s["id"]: s["name"] for s in d["sections"]}
        lines = [f"Sections: {', '.join(s['name'] for s in d['sections'])} (archived: {len(arch_map)})"]
        root_gs = [g for g in gs if not g.get("parent")]
        if root_gs:
            lines.append("\nGroups:")
            for g in root_gs:
                if q and q not in (g.get("name") or "").lower():
                    continue
                cnt = sum(1 for c in chats if c.get("group") and _root_group(c["group"], g_by_id) == g["id"] and c["id"] not in arch_map)
                acnt = sum(1 for c in chats if c.get("group") and _root_group(c["group"], g_by_id) == g["id"] and c["id"] in arch_map)
                sec = sec_names.get(group_section(g["id"], d, g_by_id), "?")
                lines.append(f"- {g['name']} (id={g['id']}, {cnt} active / {acnt} archived, section={sec})")
        loose = [c for c in chats if not (c.get("group") and c["group"] in g_by_id)]
        if q:
            loose = [c for c in loose if q in f"{display_title(c, d)} {c.get('title', '')} {c.get('project', '')}".lower()]
        if loose:
            lines.append(f"\nLoose chats ({len(loose)} shown up to 40):")
            for c in loose[:40]:
                sec = sec_names.get(chat_section(c, d, g_by_id, c_by_id), "?")
                atag = ", archived" if c["id"] in arch_map else ""
                lines.append(f"- {display_title(c, d) or 'New chat'} (id={c['id']}, section={sec}{atag})")
        return {"text": "\n".join(lines), "ok": True}

    if act == "undo":
        res = undo(args.get("undo_id") or None)
        if not res.get("ok"):
            return {"text": res.get("error") or "Nothing to undo.", "ok": False}
        msg = f"Undid: {res['label']}."
        if chat and emit:
            m = chat.append({"role": "notice", "content": msg})
            emit({"type": "message", "message": m})
        return {"text": msg, "ok": True}

    if act == "cleanup":
        res = cleanup(source="ai")
        if res.get("undo_id") and chat and emit:
            m = chat.append({"role": "notice", "content": res["label"] + ".", "undo_section": res["undo_id"]})
            emit({"type": "message", "message": m})
        return {"text": f"{res['label']} (undo_id={res.get('undo_id') or 'none'}).", "ok": True,
                "ui": {"undo_section": res.get("undo_id")}}

    if act in ("archive", "unarchive"):
        want_arch = act == "archive"
        resolved_g, resolved_c = _resolve_targets(args, gs, g_by_id, chats, c_by_id, include_neon=True)
        if not resolved_g and not resolved_c:
            return {"text": f"No chats or groups matched (query={args.get('query')!r}). Use action='list' to see available names.", "ok": False}
        res = archive(chat_ids=resolved_c, group_ids=resolved_g, archived=want_arch, reason="ai", source="ai")
        if res.get("undo_id") and chat and emit:
            m = chat.append({"role": "notice", "content": res["label"] + ".", "undo_section": res["undo_id"]})
            emit({"type": "message", "message": m})
        return {"text": f"{res['label']} (undo_id={res.get('undo_id') or 'none'}).", "ok": True,
                "ui": {"undo_section": res.get("undo_id")}}

    if act == "move":
        sec_arg = args.get("section")
        if not sec_arg:
            return {"text": "Error: section is required (e.g. General, Work, Personal).", "ok": False}
        sec = resolve_section(sec_arg, d)
        resolved_g, resolved_c = _resolve_targets(args, gs, g_by_id, chats, c_by_id, include_neon=True)
        # For move, keep root groups whole
        move_g, move_c = [], []
        for g in resolved_g:
            move_g.append(_root_group(g, g_by_id))
        for cid in resolved_c:
            c = c_by_id.get(cid)
            if c and c.get("group") and c["group"] in g_by_id:
                move_g.append(_root_group(c["group"], g_by_id))
            else:
                move_c.append(cid)
        if not move_g and not move_c:
            return {"text": f"No chats or groups matched (query={args.get('query')!r}). Use action='list' to see available names.", "ok": False}
        res = move(sec["id"], chat_ids=move_c, group_ids=move_g, source="ai")
        if res.get("undo_id") and chat and emit:
            m = chat.append({"role": "notice", "content": res["label"] + ".", "undo_section": res["undo_id"]})
            emit({"type": "message", "message": m})
        return {"text": f"{res['label']} (undo_id={res.get('undo_id') or 'none'}).", "ok": True,
                "ui": {"undo_section": res.get("undo_id")}}

    return {"text": f"Unknown action {act!r}. Use list, move, archive, unarchive, cleanup or undo.", "ok": False}


# ------------------------------------------------------------------------------------------------ HTTP
def http_get(h, path, q):
    if path == "/api/sections":
        return h._json(state())
    h._json({"error": "unknown"}, 404)


def http_post(h, path, b):
    act = path.rsplit("/", 1)[-1]
    if act == "move":
        return h._json(move(b.get("section"), chat_ids=b.get("chats"), group_ids=b.get("groups"), label=b.get("label")))
    if act == "archive":
        return h._json(archive(chat_ids=b.get("chats"), group_ids=b.get("groups"),
                               archived=bool(b.get("archived", True)), reason=b.get("reason") or "manual",
                               label=b.get("label")))
    if act == "cleanup":
        return h._json(cleanup(source="user"))
    if act == "undo":
        return h._json(undo(b.get("id")))
    if act == "dismiss-banner":
        return h._json(dismiss_banner(b.get("which")))
    if act == "autosort":
        return h._json(autosort())
    if act == "config":
        return h._json(save_sections(b.get("sections")))
    h._json({"error": "unknown"}, 404)
