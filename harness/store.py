"""Chats on disk. One folder per chat in DATA/chats/<id>/:
  chat.json      title, project folder, endpoint, settings, the system prompt it started with, totals
  messages.jsonl every message in OpenAI chat format plus our own fields (id, ts, stats, ui); never rewritten
  notes.md       the chat's memory: what matters in this chat, kept by the model (Notes tool) and editable by the user

A compaction is a message with role "compaction" holding the summary; the model's context starts at the last one.
"""
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import config

_locks = {}
_locks_guard = threading.Lock()

# ---- reading a long messages.jsonl without reading it all
# Opening a long chat reads only the tail instead of the full history so the page stays instant.
# chat read its whole history file (15 MB here) and each of its subagents' whole files, every time. The file only ever
# grows by whole lines, so what was read once is remembered and only the new end is read again.
_seen_guard = threading.Lock()
_LINES = {}                      # chat id -> {"size", "head", "edge", "offs": where each non-empty line starts}


def file_state(path, st):
    """How the file relates to what `st` remembers of it ({"size", "head", "edge"}): ("same" | "more" | "new" | "gone",
    its size now). "more": the remembered part is untouched and there is more after it; "new": another file (rewritten,
    imported again), start over."""
    try:
        size = path.stat().st_size
    except OSError:
        return "gone", 0
    if not st or size < st["size"]:
        return "new", size
    try:
        with open(path, "rb") as f:
            head = f.read(len(st["head"]))
            f.seek(max(0, st["size"] - len(st["edge"])))
            edge = f.read(len(st["edge"]))
    except OSError:
        return "gone", 0
    if head != st["head"] or edge != st["edge"]:
        return "new", size
    return ("same" if size == st["size"] else "more"), size


def file_stamp(path, st, size):
    """Remember the file up to `size` bytes: its first and last few bytes are what file_state checks next time."""
    with open(path, "rb") as f:
        st["head"] = f.read(min(128, size))
        f.seek(max(0, size - 64))
        st["edge"] = f.read(min(64, size))
    st["size"] = size
    return st


def read_more(path, st):
    """The bytes after what `st` remembers, cut at the last whole line: (bytes, where they start in the file)."""
    with open(path, "rb") as f:
        f.seek(st["size"])
        data = f.read()
    return data[:data.rfind(b"\n") + 1], st["size"]


def _line_starts(cid, path):
    """Where each non-empty line of a chat's messages file starts, and where the last one ends: (offsets, count, end).
    Kept in memory and extended as the file grows, so a call costs two small reads when nothing changed."""
    with _seen_guard:
        st = _LINES.get(cid)
        mode, _size = file_state(path, st)
        if mode == "gone":
            _LINES.pop(cid, None)
            return [], 0, 0
        if mode == "same":
            return st["offs"], len(st["offs"]), st["size"]
        if mode == "new":
            st = {"size": 0, "head": b"", "edge": b"", "offs": []}
        data, pos = read_more(path, st)
        at = 0
        while at < len(data):
            nl = data.index(b"\n", at)
            if data[at:nl].strip():
                st["offs"].append(pos + at)
            at = nl + 1
        file_stamp(path, st, pos + len(data))
        _LINES[cid] = st
        return st["offs"], len(st["offs"]), st["size"]


def count_messages(chat):
    """Fast cached count of non-empty message lines in a chat without parsing JSON."""
    try:
        return _line_starts(chat.id, chat.messages_path)[1]
    except OSError:
        return 0


def _lock(cid):
    with _locks_guard:
        return _locks.setdefault(cid, threading.RLock())


def new_id(prefix=""):
    return prefix + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def clean_title(text, n=60):
    text = re.sub(r"\s+", " ", text or "").strip()
    return (text[: n - 1] + "…") if len(text) > n else (text or "New chat")


def _tmp_name(name):
    """A temp name of this write's own. Two processes writing the same chat (the app and a second copy of it, a test)
    used one shared "chat.json.tmp": the first swap took it away and the second ended a turn with FileNotFoundError
    (22 Nov 2026)."""
    return "%s.%d.%s.tmp" % (name, os.getpid(), secrets.token_hex(3))


def _replace(src, dst, tries=40, wait=0.05):
    """os.replace that outlasts a reader: on Windows the swap is refused (WinError 5) while any other process has the
    target open, even for a moment (a subagent reading its own chat.json ended a night run this way, 22 Nov 2026)."""
    for n in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if n == tries - 1:
                raise
            time.sleep(wait)


class Chat:
    def __init__(self, cid):
        if not re.fullmatch(r"[0-9A-Za-z_.-]{6,64}", cid or "") or ".." in (cid or ""):
            raise ValueError("bad chat id")
        self.id = cid
        self.dir = config.CHATS / cid
        self.lock = _lock(cid)
        self._messages = None
        self._messages_stat = None

    # ---- meta
    @property
    def meta_path(self):
        return self.dir / "chat.json"

    def _read_meta(self, tries=12, wait=0.03):
        """(meta, ok). ok=False: the file is there but could not be read as a chat's settings (locked or half swapped
        by another writer for a moment, or damaged). A chat with no file at all is ({}, True): a new chat."""
        for n in range(tries):
            try:
                m = json.loads(self.meta_path.read_text(encoding="utf-8"))
                if isinstance(m, dict):
                    return m, True
            except FileNotFoundError:
                return {}, True                      # the swap into place is atomic: missing means there is none
            except (OSError, ValueError):
                pass
            time.sleep(wait)
        return {}, False

    def meta(self):
        return self._read_meta()[0]

    def save_meta(self, meta):
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / _tmp_name("chat.json")
            tmp.write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
            _replace(tmp, self.meta_path)

    def update_meta(self, touch=True, **kw):
        """touch=False: a change that is not activity (moving it to a group), so its place in the list stays.

        Never writes over settings it could not read: a failed read used to count as an empty chat, and the write
        that followed left a subagent's chat.json with two keys (its id, session and parent gone; 22 Nov 2026)."""
        with self.lock:
            m, ok = self._read_meta()
            if not ok or (not m and self.messages_path.exists() and self.messages_path.stat().st_size > 0 and "id" not in kw):
                raise OSError("chat settings could not be read; not overwriting %s" % self.meta_path)
            m.update(kw)
            if touch:
                m["updated"] = time.time()
            self.save_meta(m)
            return m

    # ---- messages
    @property
    def messages_path(self):
        return self.dir / "messages.jsonl"

    def _stat_messages(self):
        try:
            st = self.messages_path.stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def messages(self):
        with self.lock:
            cur = self._stat_messages()
            if self._messages is not None and cur == self._messages_stat:
                return list(self._messages)
            # Incremental tail read when another process (phone copy or two-way sync) appended new lines
            if (self._messages is not None and cur is not None and self._messages_stat is not None
                    and cur[0] > self._messages_stat[0]):
                try:
                    with open(self.messages_path, "rb") as f:
                        f.seek(self._messages_stat[0])
                        tail = f.read()
                    if tail:
                        have = {m.get("id") for m in self._messages if isinstance(m, dict) and m.get("id")}
                        for line in tail.decode("utf-8", "replace").split("\n"):
                            if not line.strip():
                                continue
                            try:
                                m = json.loads(line)
                            except ValueError:
                                continue
                            mid = m.get("id") if isinstance(m, dict) else None
                            if mid and mid in have:
                                continue
                            self._messages.append(m)
                            if mid:
                                have.add(mid)
                    self._messages_stat = cur
                    return list(self._messages)
                except OSError:
                    pass
            out = []
            if cur is not None:
                try:
                    for line in self.messages_path.read_text(encoding="utf-8").split("\n"):   # not splitlines: text may hold U+2028
                        if line.strip():
                            try:
                                out.append(json.loads(line))
                            except ValueError:
                                pass
                except OSError:
                    pass
            self._messages = out
            self._messages_stat = cur
            return list(self._messages)

    def window(self, limit, before=None):
        """The last `limit` messages before line `before` (default: the end), widened back to the user message that
        starts that turn, so a turn's tool calls and their results always come together. Only those lines are parsed:
        a chat imported from a long session can hold thousands of messages. Returns (start line, total lines, messages)."""
        with self.lock:
            try:
                offs, total, last = _line_starts(self.id, self.messages_path)
                f = open(self.messages_path, "rb") if total else None
            except OSError:
                return 0, 0, []
        if not f:
            return 0, 0, []
        end = total if before is None else max(0, min(int(before), total))
        out, i = [], end - 1
        with f:
            while i >= 0:
                f.seek(offs[i])
                try:
                    m = json.loads(f.read((offs[i + 1] if i + 1 < total else last) - offs[i]))
                except ValueError:
                    i -= 1
                    continue
                out.append(m)
                i -= 1
                if len(out) >= limit and m.get("role") == "user" and not m.get("auto"):
                    break
                if len(out) >= limit * 4:               # no turn start anywhere near: cut here
                    break
        out.reverse()
        return i + 1, total, out

    def tail_info(self):
        """Fast latest-message metadata: {'total': count, 'last_id': id_or_none} without reading the whole file."""
        with self.lock:
            try:
                offs, total, last = _line_starts(self.id, self.messages_path)
                if not total:
                    return {"total": 0, "last_id": None}
                with open(self.messages_path, "rb") as f:
                    for i in range(total - 1, max(-1, total - 8), -1):
                        f.seek(offs[i])
                        raw = f.read((offs[i + 1] if i + 1 < total else last) - offs[i])
                        try:
                            m = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(m, dict) and m.get("id"):
                            return {"total": total, "last_id": m["id"]}
                return {"total": total, "last_id": None}
            except OSError:
                return {"total": 0, "last_id": None}

    def since(self, after_id, max_msgs=120):
        """Messages strictly after `after_id` (newest tail only): (total, messages, truncated)."""
        with self.lock:
            try:
                offs, total, last = _line_starts(self.id, self.messages_path)
                if not total:
                    return 0, [], False
                f = open(self.messages_path, "rb")
            except OSError:
                return 0, [], False
        out = []
        found = False
        with f:
            for i in range(total - 1, max(-1, total - 1 - max_msgs - 1), -1):
                f.seek(offs[i])
                raw = f.read((offs[i + 1] if i + 1 < total else last) - offs[i])
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                if after_id and m.get("id") == after_id:
                    found = True
                    break
                out.append(m)
                if len(out) > max_msgs:
                    break
        if not found:
            return total, [], True
        out.reverse()
        return total, out, False

    def head_raw_lines(self, max_bytes=65_536):
        """Read non-empty raw JSONL lines from the start of messages.jsonl (at most max_bytes)."""
        try:
            with open(self.messages_path, "rb") as f:
                raw = f.read(max_bytes)
        except OSError:
            return []
        if len(raw) == max_bytes:
            nl = raw.rfind(b"\n")
            if nl > 0:
                raw = raw[:nl]
        return [ln for ln in raw.split(b"\n") if ln.strip()]

    def tail_raw_lines(self, max_bytes=1_500_000):
        """Read non-empty raw JSONL lines from the end of messages.jsonl (at most max_bytes). Returns (lines, hit_start)."""
        try:
            size = self.messages_path.stat().st_size
            start = max(0, size - max_bytes)
            with open(self.messages_path, "rb") as f:
                f.seek(start)
                raw = f.read()
        except OSError:
            return [], True
        if start > 0:
            nl = raw.find(b"\n")
            raw = raw[nl + 1:] if nl >= 0 else b""
        return [ln for ln in raw.split(b"\n") if ln.strip()], start == 0

    def append(self, msg):
        with self.lock:
            msg.setdefault("id", new_id("m"))
            msg.setdefault("ts", time.time())
            self.dir.mkdir(parents=True, exist_ok=True)
            if self._messages is not None:
                # Pull in any externally appended lines first so our in-memory cache never drops synced messages
                if self._stat_messages() != self._messages_stat:
                    self.messages()
            with open(self.messages_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            if self._messages is not None:
                self._messages.append(msg)
                self._messages_stat = self._stat_messages()
            return msg

    def rewrite(self, msgs):
        """Only for deleting a chat's tail (edit and resend); keeps the file a plain list of lines."""
        with self.lock:
            tmp = self.dir / _tmp_name("messages.jsonl")
            tmp.write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in msgs), encoding="utf-8")
            _replace(tmp, self.messages_path)
            self._messages = list(msgs)
            self._messages_stat = self._stat_messages()
            with _seen_guard:
                _LINES.pop(self.id, None)

    def context_messages(self):
        """What the model sees: everything after the last compaction (the summary goes first)."""
        msgs = self.messages()
        start, summary = 0, None
        for i, m in enumerate(msgs):
            if m.get("role") == "compaction":
                start, summary = i + 1, m
        return summary, [m for m in msgs[start:] if m.get("role") in ("user", "assistant", "tool")]

    # ---- notes (chat memory)
    @property
    def notes_path(self):
        return self.dir / "notes.md"

    def notes(self):
        try:
            return self.notes_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def set_notes(self, text):
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.notes_path.write_text(text, encoding="utf-8")

    # ---- log (kept by the harness after every turn: what was asked, what was done, the answer's first line)
    @property
    def log_path(self):
        return self.dir / "log.md"

    def log(self):
        try:
            return self.log_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def append_log(self, entry):
        with self.lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            text = self.log() + entry
            if len(text) > 60_000:                  # keep the latest part
                text = text[-40_000:]
                text = text[text.find("\n- ") + 1:]
            self.log_path.write_text(text, encoding="utf-8")


def create(project, endpoint, title="New chat", reasoning=None, group=None):
    cid = new_id()
    chat = Chat(cid)
    now = time.time()
    meta = {"id": cid, "title": title, "project": str(project), "endpoint": endpoint, "created": now, "updated": now,
            "reasoning": reasoning, "usage": {}}
    if group:
        meta["group"] = group
    chat.save_meta(meta)
    return chat


LIST_KEYS = ("id", "title", "project", "endpoint", "created", "updated", "pinned", "source", "group", "parent", "branch",
             "claude_session", "user_renamed", "mode")
_list_cache = {}                 # chat.json path -> (mtime_ns, size, the list entry or None when deleted)
_list_guard = threading.Lock()


def _list_entry(path):
    """One chat's list entry, re-read only when its chat.json changed (thousands of imported chats list in a blink)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = str(path)
    hit = _list_cache.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        m = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # a deleted chat is not listed, nor a subagent's or a group member's private chat
    entry = None if m.get("deleted") or m.get("sub_of") or m.get("group_member_of") or m.get("neon_companion") else {k: m.get(k) for k in LIST_KEYS}
    with _list_guard:
        _list_cache[key] = (st.st_mtime_ns, st.st_size, entry)
    return entry


def list_chats(project=None, limit=20_000):
    out = []
    if not config.CHATS.exists():
        return out
    want = os.path.normcase(str(project)) if project else None
    with os.scandir(config.CHATS) as it:
        for d in it:
            if not d.is_dir():
                continue
            try:                      # one unreadable chat is left out; it never empties the list
                e = _list_entry(os.path.join(d.path, "chat.json"))
                if not isinstance(e, dict) or not e:
                    continue
                if want and os.path.normcase(str(e.get("project") or "")) != want:
                    continue
                out.append(dict(e))
            except Exception:
                continue

    def when(m):
        try:
            return float(m.get("updated") or 0)
        except (TypeError, ValueError):
            return 0.0
    out.sort(key=when, reverse=True)
    return out[:limit]


# ---- projects = folders on disk
def projects():
    cfg = config.get()
    seen, out = set(), []
    for p in cfg.get("projects", []):
        key = os.path.normcase(p["path"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": p["path"], "name": p.get("name") or Path(p["path"]).name, "exists": os.path.isdir(p["path"])})
    return out


def add_project(path, name=None):
    path = str(Path(path).expanduser().resolve())
    os.makedirs(path, exist_ok=True)

    def fn(cfg):
        lst = [p for p in cfg.get("projects", []) if os.path.normcase(p["path"]) != os.path.normcase(path)]
        lst.insert(0, {"path": path, "name": name or Path(path).name})
        cfg["projects"] = lst[:60]
    config.update(fn)
    from . import preview
    preview.register(path)
    return path


def project_notes_path(project):
    """Notes about a project that the model reads at the start of every chat in it (kept by the harness, not in the repo)."""
    key = re.sub(r"[^0-9a-zA-Z]+", "-", os.path.normcase(str(project))).strip("-")[-80:]
    return config.DATA / "projects" / key / "notes.md"
