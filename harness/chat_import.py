"""Import chats made elsewhere: ChatGPT and Claude.ai data exports (the .zip, a conversations*.json, or the unzipped
folder) and the Claude Code and Codex sessions on this computer.

Every source is turned into the same shape (user and assistant turns made of text, thinking, tool and attachment parts)
and saved as a normal chat (store.py), so it shows in the list, opens, and can be continued with any model. Tool calls from
other apps cannot run here: they are kept for display (collapsed rows) and the model gets them as short text
(`model_content`), with long outputs trimmed, so continuing an old chat does not cost a fortune. A chat that is still too
long starts from a compaction message built here (no model call).

Exports can be hundreds of MB: conversations*.json is read as a stream (one conversation at a time), zips are read in
place (nothing is extracted except the images and files a chat points at, under our own names), and the work runs in a
background job that the page polls. A remembered index (DATA/imports/index.json, source + conversation id -> chat id) stops
a second import of the same export from making copies. Imported text is untrusted: the page renders it through the same
sanitising path as any model answer."""
import base64
import difflib
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime
from pathlib import Path

from . import config, osutil, store

HOME = Path.home()
LABEL = {"chatgpt": "ChatGPT", "claude": "Claude.ai", "claude-code": "Claude Code", "codex": "Codex"}

SHOW_TEXT = 200_000              # one message's text, kept in full up to this
SHOW_TOOL = 12_000               # a tool's output as shown (a long session holds thousands of them)
SHOW_INPUT = 12_000              # a tool's input as shown
SHOW_THINK = 30_000
MODEL_IN = 600                   # what the model gets of an old tool call's input ...
MODEL_OUT = 1_500                # ... and of its output
MODEL_FILE = 20_000              # an attached file's text
COMPACT_CHARS = 350_000          # longer than this (about 100k tokens): the chat starts from a summary built here
ASSET_MAX = 50 * 1024 * 1024     # one image or file taken out of an export
ASSETS_PER_CHAT = 300
UPLOAD_MAX = 8 * 1024 ** 3       # an export dropped on the page (a path on disk has no limit: it is streamed)
INNER_ZIP_MAX = 8 * 1024 ** 3    # Claude.ai's conversations-000.zip inside the download

SUB_SHOW = 4_000                 # a subagent run's tool output and input as shown (opened on demand, never sent to a model)
SUB_TOOLS = ("Agent", "Task", "Workflow")

IMPORTS = config.DATA / "imports"
UPLOADS = IMPORTS / "uploads"


def _ts(v):
    """Seconds since 1970 from a number (seconds or ms) or an ISO date; None if unknown."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) / (1000 if v > 1e11 else 1)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def _cut(text, n):
    text = text or ""
    return text if len(text) <= n else text[:n] + f"\n[... {len(text) - n:,} more characters not kept]"


def _cut_model(text, n):
    """Head and tail of a long output (errors and results are often at the end)."""
    text = text or ""
    if len(text) <= n:
        return text
    head = int(n * 0.7)
    return text[:head] + f"\n[... {len(text) - n:,} characters cut ...]\n" + text[-(n - head):]


def _jtext(v):
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False, indent=1)
    except (TypeError, ValueError):
        return str(v)


# ------------------------------------------------------------------------------------------------ the common shape
class Turns:
    """Collects one conversation as turns: user messages and assistant turns (text, thinking, tool calls with results)."""

    def __init__(self):
        self.msgs = []
        self.open = {}               # tool call id -> its part, until the result comes

    def _last(self, role):
        return self.msgs[-1] if self.msgs and self.msgs[-1]["role"] == role else None

    def user(self, parts, ts=None):
        parts = [p for p in parts if p and (p["kind"] != "text" or p["text"].strip())]
        if not parts:
            return
        m = self._last("user")
        if m:
            m["parts"] += parts
        else:
            self.msgs.append({"role": "user", "ts": ts, "parts": parts})

    def add(self, part, ts=None):
        if part["kind"] == "text" and not part["text"].strip():
            return part
        m = self._last("assistant")
        if not m:
            m = {"role": "assistant", "ts": ts, "parts": []}
            self.msgs.append(m)
        if part["kind"] == "text" and m["parts"] and m["parts"][-1]["kind"] == "text":
            m["parts"][-1]["text"] += "\n\n" + part["text"]
        else:
            m["parts"].append(part)
        return part

    def call(self, cid, name, inp, ts=None):
        p = self.add({"kind": "tool", "name": str(name or "tool"), "input": _cut(_jtext(inp), SHOW_INPUT),
                      "model_input": _cut_model(_jtext(inp) if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False), MODEL_IN)}, ts)
        if isinstance(inp, dict) and name in ("Edit", "MultiEdit"):
            p["diff"] = _diff(inp)
        if cid:
            self.open[cid] = p
            if name in SUB_TOOLS:            # its subagent runs are found by this id
                p["call_id"] = cid
        return p

    def result(self, cid, output, error=False, ts=None, name=None):
        p = self.open.pop(cid, None) if cid else None
        if p is None:
            p = self.add({"kind": "tool", "name": str(name or "tool"), "input": "", "model_input": ""}, ts)
        p["output"] = _cut(output, SHOW_TOOL)
        p["model_output"] = _cut_model(output, MODEL_OUT)
        p["error"] = bool(error)

    def notice(self, text, ts=None):
        self.msgs.append({"role": "notice", "ts": ts, "content": text})


def _diff(inp):
    edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
    out = []
    for e in edits[:20]:
        if not isinstance(e, dict):
            continue
        a, b = str(e.get("old_string") or "").splitlines(), str(e.get("new_string") or "").splitlines()
        out += list(difflib.unified_diff(a, b, lineterm="", n=2))[2:]
    return _cut("\n".join(out), SHOW_INPUT)


# ------------------------------------------------------------------------------------------------ ChatGPT
CITE_RE = re.compile(r"\ue200[^\ue201]*\ue201|\u3010\d+(?::\d+)?\u2020[^\u3011]*\u3011")


def _gpt_text(c):
    ct = c.get("content_type")
    if ct in ("text", "multimodal_text"):
        out = []
        for p in c.get("parts") or []:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict) and p.get("content_type") == "audio_transcription" and p.get("text"):
                out.append(p["text"])
        return "\n".join(x for x in out if x)
    if ct == "code":
        return c.get("text") or ""
    if ct == "execution_output":
        return c.get("text") or ""
    if ct == "tether_quote":
        return "\n".join(x for x in (c.get("title"), c.get("url"), "", c.get("text")) if x is not None)
    if ct == "tether_browsing_display":
        return "\n".join(x for x in (c.get("summary"), c.get("result")) if x)
    if ct == "system_error":
        return f"{c.get('name') or 'Error'}: {c.get('text') or ''}"
    if ct == "sonic_webpage":
        return "\n".join(x for x in (c.get("title"), c.get("url"), c.get("text")) if x)
    if c.get("text"):
        return str(c["text"])
    if c.get("parts"):
        return "\n".join(p for p in c["parts"] if isinstance(p, str))
    rest = {k: v for k, v in c.items() if k != "content_type"}
    return json.dumps(rest, ensure_ascii=False)[:4000] if rest else ""


def _gpt_images(c, assets):
    out = []
    for p in (c.get("parts") or []) if c.get("content_type") == "multimodal_text" else []:
        if isinstance(p, dict) and p.get("content_type") == "image_asset_pointer":
            ref = p.get("asset_pointer") or ""
            prompt = ((p.get("metadata") or {}).get("dalle") or {}).get("prompt")
            out.append({"kind": "image", "name": prompt or "image", "asset": assets.find(ref) if assets else None, "ref": ref})
    return out


def chatgpt(conv, assets=None):
    mapping = conv.get("mapping") or {}
    node = conv.get("current_node")
    chain, seen = [], set()
    while node and node in mapping and node not in seen:
        seen.add(node)
        chain.append(mapping[node])
        node = mapping[node].get("parent")
    chain.reverse()
    if not chain and mapping:            # no current node: from the root, always the newest child
        n = next((v for v in mapping.values() if not v.get("parent")), None)
        while n and n.get("id") not in seen:
            seen.add(n.get("id"))
            chain.append(n)
            kids = n.get("children") or []
            n = mapping.get(kids[-1]) if kids else None
    others = sum(max(0, len(v.get("children") or []) - 1) for v in mapping.values())
    T = Turns()
    for n in chain:
        m = n.get("message")
        if not m:
            continue
        role = (m.get("author") or {}).get("role")
        md = m.get("metadata") or {}
        c = m.get("content") or {}
        ct = c.get("content_type")
        ts = _ts(m.get("create_time"))
        if role == "system" or md.get("is_visually_hidden_from_conversation") or md.get("is_user_system_message"):
            continue
        if ct in ("user_editable_context", "model_editable_context", "reasoning_recap"):
            continue
        if role == "user":
            parts = [{"kind": "text", "text": _gpt_text(c)}] + _gpt_images(c, assets)
            shown = {p.get("ref", "").rsplit("/", 1)[-1] for p in parts if p["kind"] == "image"}
            for a in md.get("attachments") or []:
                aid = a.get("id") or ""
                if not aid or any(aid in s for s in shown):
                    continue
                kind = "image" if str(a.get("mime_type") or "").startswith("image/") else "file"
                parts.append({"kind": kind, "name": a.get("name") or aid, "asset": assets.find(aid) if assets else None})
            T.user(parts, ts)
        elif role == "assistant":
            recipient = m.get("recipient") or "all"
            if ct == "thoughts":
                txt = "\n\n".join(((t.get("summary") or "") + "\n" + (t.get("content") or "")).strip()
                                  for t in c.get("thoughts") or [] if isinstance(t, dict))
                if txt.strip():
                    T.add({"kind": "thinking", "text": _cut(txt, SHOW_THINK)}, ts)
            elif recipient != "all":
                T.call(recipient, recipient, _gpt_text(c), ts)
            else:
                T.add({"kind": "text", "text": _cut(CITE_RE.sub("", _gpt_text(c)), SHOW_TEXT)}, ts)
                for im in _gpt_images(c, assets):
                    T.add(im, ts)
        elif role == "tool":
            name = (m.get("author") or {}).get("name") or "tool"
            imgs = _gpt_images(c, assets)
            text = _gpt_text(c)
            if text.strip() or not imgs:
                key = name if name in T.open else next((k for k in T.open if name.startswith(k) or k.startswith(name)), name)
                T.result(key, text, error=ct == "system_error", ts=ts, name=name)
            for im in imgs:
                T.add(im, ts)
    notes = [f"{others} other version{'s' if others != 1 else ''} (edits or regenerated answers) not imported: this is the branch you last saw."] if others else []
    return {"source": "chatgpt", "sid": str(conv.get("conversation_id") or conv.get("id") or ""), "title": conv.get("title") or "",
            "created": _ts(conv.get("create_time")), "updated": _ts(conv.get("update_time")), "turns": T.msgs, "notes": notes}


# ------------------------------------------------------------------------------------------------ Claude.ai
ROOT_UUID = "00000000-0000-4000-8000-000000000000"


def _claude_result_text(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text") or "")
            elif b.get("type") in ("image", "document"):
                out.append(f"[{b['type']}]")
            else:
                out.append(_jtext({k: v for k, v in b.items() if k != "type"})[:2000])
    return "\n".join(out)


def claude_ai(conv):
    msgs = [m for m in conv.get("chat_messages") or [] if isinstance(m, dict)]
    others = 0
    if any(m.get("parent_message_uuid") for m in msgs):
        by = {m.get("uuid"): m for m in msgs}
        leaf = max(range(len(msgs)), key=lambda i: (_ts(msgs[i].get("created_at")) or 0, i))
        chain, seen, cur = [], set(), msgs[leaf]
        while cur and cur.get("uuid") not in seen:
            seen.add(cur.get("uuid"))
            chain.append(cur)
            p = cur.get("parent_message_uuid")
            cur = by.get(p) if p and p != ROOT_UUID else None
        chain.reverse()
        others = len(msgs) - len(chain)
        msgs = chain
    T = Turns()
    for m in msgs:
        ts = _ts(m.get("created_at"))
        blocks = m.get("content") or []
        if not blocks and m.get("text"):
            blocks = [{"type": "text", "text": m["text"]}]
        if m.get("sender") == "human":
            parts = []
            for b in blocks:
                if b.get("type") == "text":
                    parts.append({"kind": "text", "text": _cut(b.get("text") or "", SHOW_TEXT)})
                elif b.get("type") in ("image", "document"):
                    parts.append({"kind": "image" if b["type"] == "image" else "file", "name": b.get("file_name") or b["type"], "missing": True})
            for a in m.get("attachments") or []:
                parts.append({"kind": "file", "name": a.get("file_name") or "file", "text": a.get("extracted_content") or ""})
            for f in (m.get("files_v2") or m.get("files") or []):
                if isinstance(f, dict) and not any(p.get("name") == f.get("file_name") for p in parts):
                    parts.append({"kind": "image" if f.get("file_kind") == "image" else "file", "name": f.get("file_name") or "file", "missing": True})
            T.user(parts, ts)
        else:
            for b in blocks:
                t = b.get("type")
                if t == "text":
                    T.add({"kind": "text", "text": _cut(b.get("text") or "", SHOW_TEXT)}, ts)
                elif t == "thinking" and (b.get("thinking") or "").strip():
                    T.add({"kind": "thinking", "text": _cut(b["thinking"], SHOW_THINK)}, ts)
                elif t == "tool_use":
                    T.call(b.get("id"), b.get("name"), b.get("input") or {}, ts)
                elif t == "tool_result":
                    T.result(b.get("tool_use_id"), _claude_result_text(b.get("content")), b.get("is_error"), ts, b.get("name"))
    notes = [f"{others} message{'s' if others != 1 else ''} from other branches (edits or retries) not imported: this is the newest branch."] if others else []
    return {"source": "claude", "sid": str(conv.get("uuid") or ""), "title": conv.get("name") or "",
            "created": _ts(conv.get("created_at")), "updated": _ts(conv.get("updated_at")), "turns": T.msgs, "notes": notes}


# ------------------------------------------------------------------------------------------------ reading exports
def iter_array(fp, progress=None):
    """Yield the items of a JSON array read from a text stream, one at a time (the file can be bigger than memory).
    A top-level object is read whole and its first list of objects is used."""
    dec = json.JSONDecoder()
    buf, pos, eof, want = "", 0, False, 1 << 20

    def more(n):
        nonlocal buf, eof
        chunk = fp.read(n)
        if not chunk:
            eof = True
        buf += chunk
        if progress:
            progress()

    def skip(chars):
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in chars:
                pos += 1
            if pos < len(buf) or eof:
                return
            more(want)

    skip(" \t\r\n\ufeff")
    if pos >= len(buf):
        return
    if buf[pos] == "{":
        while not eof:
            more(1 << 24)
        whole = json.loads(buf[pos:])
        for v in whole.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                yield from v
                return
        return
    if buf[pos] != "[":
        raise ValueError("not a JSON list")
    pos += 1
    while True:
        skip(" \t\r\n,")
        if pos >= len(buf):
            raise ValueError("the file ends early (cut off?)")
        if buf[pos] == "]":
            return
        try:
            obj, end = dec.raw_decode(buf, pos)
        except json.JSONDecodeError:
            if eof:
                raise ValueError("the file ends early or is not valid JSON")
            more(want)
            want = min(want * 2, 1 << 28)
            continue
        want = 1 << 20
        pos = end
        if pos > (1 << 22):
            buf, pos = buf[pos:], 0
        yield obj


class _Counting(io.RawIOBase):
    """A binary stream that counts what was read (for progress)."""

    def __init__(self, raw):
        self.raw, self.n = raw, 0

    def readable(self):
        return True

    def readinto(self, b):
        data = self.raw.read(len(b))
        b[:len(data)] = data
        self.n += len(data)
        return len(data)


class Export:
    """A ChatGPT or Claude.ai export: a zip, a folder, or one JSON file. Knows its conversations files and its assets."""

    def __init__(self, path):
        self.path = Path(path)
        self.zip = None
        self.inner = None            # a temporary copy of Claude.ai's conversations-000.zip
        self.files = []              # (name, size) of conversations*.json, in order
        self.names = {}              # asset id -> member name (zip) or path (folder)
        self.kind = None
        if self.path.is_dir():
            self._folder()
        elif zipfile.is_zipfile(self.path):
            self._zip(zipfile.ZipFile(self.path))
        else:
            self.files = [(str(self.path), self.path.stat().st_size)]
        if not self.files:
            raise ValueError("No conversations in it. Use the .zip from ChatGPT (Settings > Data controls > Export) or "
                             "Claude.ai (Settings > Privacy > Export data), its conversations.json, or the unzipped folder.")
        self.kind = self._sniff()

    def _zip(self, zf):
        names = [i for i in zf.infolist() if not i.is_dir()]
        convs = [i for i in names if re.fullmatch(r"conversations(-\d+)?\.json", Path(i.filename).name, re.I)]
        if not convs:            # Claude.ai: a zip of zips; the chats are in conversations-000.zip
            inner = [i for i in names if re.fullmatch(r"conversations(-\d+)?\.zip", Path(i.filename).name, re.I)]
            if inner and inner[0].file_size <= INNER_ZIP_MAX:
                UPLOADS.mkdir(parents=True, exist_ok=True)
                self.inner = UPLOADS / f"inner-{secrets.token_hex(6)}.zip"
                with zf.open(inner[0]) as src, open(self.inner, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                zf.close()
                return self._zip(zipfile.ZipFile(self.inner))
        self.zip = zf
        self.files = [(i.filename, i.file_size) for i in sorted(convs, key=lambda i: i.filename)]
        for i in names:
            self._index(Path(i.filename).name, i.filename, i.file_size)

    def _folder(self):
        n = 0
        for dirpath, dirs, files in os.walk(self.path):
            for f in files:
                full = os.path.join(dirpath, f)
                if re.fullmatch(r"conversations(-\d+)?\.json", f, re.I):
                    self.files.append((full, os.path.getsize(full)))
                elif re.fullmatch(r"conversations(-\d+)?\.zip", f, re.I) and not self.files and not self.zip:
                    self._zip(zipfile.ZipFile(full))
                    return
                else:
                    self._index(f, full, None)
                n += 1
                if n > 300_000:
                    break
        self.files.sort()

    def _index(self, base, ref, size):
        m = re.match(r"(file[-_][A-Za-z0-9]+)", base)
        if m and (size is None or size <= ASSET_MAX):
            self.names.setdefault(m.group(1), ref)

    def find(self, ref):
        """An asset pointer (file-service://file-X, sediment://file_X) or attachment id -> a member name, or None."""
        key = str(ref or "").rsplit("/", 1)[-1]
        return self.names.get(key)

    def read_asset(self, name):
        if self.zip:
            info = self.zip.getinfo(name)
            if info.file_size > ASSET_MAX:
                return None
            with self.zip.open(info) as f:
                data = f.read(ASSET_MAX + 1)
            return data if len(data) <= ASSET_MAX else None
        p = Path(name)
        return p.read_bytes() if p.is_file() and p.stat().st_size <= ASSET_MAX else None

    def total(self):
        return sum(s for _, s in self.files) or 1

    def _open(self, name):
        raw = self.zip.open(name) if self.zip else open(name, "rb")
        counting = _Counting(raw)
        return raw, counting, io.TextIOWrapper(io.BufferedReader(counting, 1 << 20), encoding="utf-8", errors="replace")

    def conversations(self, progress=None):
        """Yield each conversation (dict); progress(bytes_read_so_far) is called as the files are read."""
        before = 0
        for name, size in self.files:
            raw, counting, text = self._open(name)
            try:
                for conv in iter_array(text, (lambda: progress(before + counting.n)) if progress else None):
                    if isinstance(conv, dict):
                        yield conv
            finally:
                raw.close()
            before += size

    def _sniff(self):
        name = self.files[0][0]
        raw, _, text = self._open(name)
        try:
            head = text.read(1 << 16)
        finally:
            raw.close()
        if '"mapping"' in head or '"current_node"' in head:
            return "chatgpt"
        if '"chat_messages"' in head:
            return "claude"
        for conv in self.conversations():      # unusual key order: look at the first one
            return "chatgpt" if "mapping" in conv else "claude" if "chat_messages" in conv else None
        return None

    def close(self):
        if self.zip:
            self.zip.close()
        if self.inner:
            try:
                self.inner.unlink()
            except OSError:
                pass


# ------------------------------------------------------------------------------------------------ Claude Code
SKIP_TAGS = ("system-reminder", "task-notification", "local-command-caveat", "local-command-stdout", "local-command-stderr",
             "bash-stdout", "bash-stderr", "user-prompt-submit-hook", "command-message")
TAG_ONLY = re.compile(r"\s*<([a-zA-Z_][\w -]*)>[\s\S]*</\1>\s*")


def _cc_user_text(text):
    """What the user typed, without what Claude Code adds around it; '' for lines that are only its additions."""
    text = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", text or "")
    name = re.search(r"<command-name>([\s\S]*?)</command-name>", text)
    if name:
        args = re.search(r"<command-args>([\s\S]*?)</command-args>", text)
        return (name.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
    bash = re.search(r"<bash-input>([\s\S]*?)</bash-input>", text)
    if bash:
        return "! " + bash.group(1).strip()
    m = TAG_ONLY.fullmatch(text)
    if m and (m.group(1) in SKIP_TAGS or "-" in m.group(1) or "_" in m.group(1)):
        return ""
    return text.strip()


def _b64_image(src):
    if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
        try:
            return base64.b64decode(src["data"]), src.get("media_type") or "image/png"
        except (ValueError, TypeError):
            return None
    return None


WANT_TYPES = (b'{"type":"user"', b'{"type":"assistant"', b'{"type":"system"', b'{"type":"attachment"', b'{"type":"custom-title"',
              b'{"type":"summary"', b'{"type":"ai-title"')


def _cc_lines(path, progress=None):
    """The lines of a Claude Code session worth parsing (the request blobs and snapshots are most of a big file)."""
    with open(path, "rb") as f:
        n = 0
        for raw in f:
            n += len(raw)
            if progress and n > (1 << 24):
                progress(n)
                n = 0
            # messages start with "parentUuid"; the bookkeeping lines (request blobs, snapshots...) start with their type
            if raw.startswith(b'{"type":"') and not raw.startswith(WANT_TYPES):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                yield d
        if progress:
            progress(n)


def claude_code(path, progress=None, extra=(), sub=False):
    """One Claude Code session. `extra`: older transcripts the same session went on from (oldest first).

    A fork's transcript starts with a copy of the session it came from (the same message ids, each line still carrying
    that session's id). That copy is left out of this chat and described in conv["fork"] instead (where it came from,
    when, the last request before it), so a family of forks does not repeat itself; the model still gets a summary of it.
    sub=True: a subagent's run, whose lines are all sidechains."""
    path = Path(path)
    files = [Path(x) for x in extra] + [path]
    own = {f.stem for f in files}
    recs, parent, title, cwd, last_leaf, seen = [], {}, None, None, None, set()
    for f in files:
        for d in _cc_lines(f, progress):
            t = d.get("type")
            if t == "custom-title" and d.get("customTitle"):
                title = d["customTitle"]
            elif t in ("summary", "ai-title") and (d.get("summary") or d.get("aiTitle")) and not title:
                title = d.get("summary") or d.get("aiTitle")
            u = d.get("uuid")
            if u:
                if u in seen:                # a later transcript of the same session repeats what came before
                    continue
                seen.add(u)
                parent[u] = d.get("parentUuid") or d.get("logicalParentUuid")
            if t not in ("user", "assistant", "system") or (d.get("isSidechain") and not sub):
                continue
            cwd = d.get("cwd") or cwd
            if t == "assistant":         # keep only what is shown, trimmed, so a 1 GB session does not fill the memory
                last_leaf = u
            elif t == "user" and not d.get("isMeta"):
                last_leaf = u
            recs.append(_cc_slim(d))
    chain = set()
    cur, guard = last_leaf, 0
    while cur and guard < 2_000_000:
        chain.add(cur)
        cur = parent.get(cur)
        guard += 1
    # parallel tool calls branch off the chain: keep the other blocks of a reply that is on it, and the results of its calls
    mids = {r["mid"] for r in recs if r["uuid"] in chain and r.get("mid")}
    calls, on = set(), []
    for r in recs:
        ok = r["uuid"] in chain or (r["type"] == "assistant" and r.get("mid") in mids)
        if not ok and r["type"] == "user" and isinstance(r["content"], list) and r["content"]:
            ok = all(b.get("type") == "tool_result" and b.get("tool_use_id") in calls for b in r["content"])
        if ok:
            on.append(r)
            if r["type"] == "assistant" and isinstance(r["content"], list):
                calls |= {b.get("id") for b in r["content"] if b.get("type") == "tool_use"}
    if len(on) < len(recs) * 0.5:     # a broken chain: keep the file's order instead
        on = recs
    kept = {id(r) for r in on}
    others = sum(1 for r in recs if id(r) not in kept and r["type"] in ("user", "assistant"))
    cut = 0
    if not sub:                       # the copy of the session this one was forked from
        while cut < len(on) and on[cut].get("sid") and on[cut]["sid"] not in own:
            cut += 1
    prefix, on = on[:cut], on[cut:]
    T, started = _cc_turns(on)
    fork = None
    if prefix:
        P, _ = _cc_turns(prefix)
        last = next((p["text"] for m in reversed(P.msgs) if m["role"] == "user" for p in m["parts"] if p["kind"] == "text"), "")
        fork = {"from": prefix[-1]["sid"], "root": prefix[0]["sid"], "at": _ts(prefix[-1].get("timestamp")),
                "after": re.sub(r"\s+", " ", last)[:160], "turns": P.msgs}
        started = started or fork["at"]
    subs = 0
    if not sub:
        subs = _cc_attach_subagents(T.msgs, files)
    notes = []
    if cwd and not sub:
        notes.append(f"It ran in {cwd}.")
    if others:
        notes.append(f"{others} message{'s' if others != 1 else ''} from rewound branches not imported.")
    if subs:
        notes.append(f"{subs} subagent run{'s' if subs != 1 else ''}: open them from their tool rows.")
    sts = [f.stat() for f in files if f.exists()]
    return {"source": "claude-code", "sid": path.stem, "title": title or "", "created": started or path.stat().st_mtime,
            "updated": max(st.st_mtime for st in sts), "cwd": cwd, "turns": T.msgs, "notes": notes, "fork": fork,
            "files": [str(f) for f in files], "size": sum(st.st_size for st in sts)}


def _cc_turns(recs):
    """Claude Code records (already on the kept branch, in order) as Turns. Returns (Turns, time of the first one)."""
    T = Turns()
    started = None
    for r in recs:
        ts = _ts(r.get("timestamp"))
        started = started or ts
        if r["type"] == "system":
            if r.get("subtype") == "compact_boundary":
                T.notice("Claude Code compacted the context here.", ts)
            continue
        content = r.get("content")
        if r["type"] == "user":
            if r.get("isCompactSummary"):
                continue
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
            parts = []
            for b in blocks:
                bt = b.get("type")
                if bt == "tool_result":
                    out = b.get("content")
                    T.result(b.get("tool_use_id"), _claude_result_text(out), b.get("is_error"), ts)
                elif bt == "text":
                    txt = _cc_user_text(b.get("text"))
                    if txt:
                        parts.append({"kind": "text", "text": _cut(txt, SHOW_TEXT)})
                elif bt == "image":
                    img = _b64_image(b.get("source"))
                    if img:
                        parts.append({"kind": "image", "name": "pasted image", "data": img[0], "mime": img[1]})
            if parts:
                T.user(parts, ts)
        else:
            for b in content or []:
                bt = b.get("type")
                if bt == "text":
                    T.add({"kind": "text", "text": _cut(b.get("text") or "", SHOW_TEXT)}, ts)
                elif bt == "thinking" and (b.get("thinking") or "").strip():
                    T.add({"kind": "thinking", "text": _cut(b["thinking"], SHOW_THINK)}, ts)
                elif bt == "tool_use":
                    T.call(b.get("id"), b.get("name"), b.get("input") or {}, ts)
    return T, started


WF_RE = re.compile(r"wf_[0-9a-f]{8}-[0-9a-f]{3}")


def _cc_subagents(files):
    """The subagent runs kept next to a session: <session>/subagents/agent-*.jsonl, each with a .meta.json naming the
    tool call that started it; a workflow's runs sit in subagents/workflows/<run id>/. Returns (by tool call, by run)."""
    by_call, by_wf, seen = {}, {}, set()
    for f in files:
        d = osutil.long_path(f.parent / f.stem / "subagents", 100)        # often past Windows' 260 characters
        try:
            found = sorted(((a.stat().st_mtime, a) for a in d.rglob("agent-*.jsonl")), key=lambda x: x[0]) if d.is_dir() else []
        except OSError:
            found = []
        for _, a in found:
            if a.stem in seen:            # the same run in a second copy of the session's folder
                continue
            seen.add(a.stem)
            try:
                meta = json.loads((a.parent / (a.stem + ".meta.json")).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            info = {"id": a.stem, "path": a, "type": str(meta.get("agentType") or "subagent"),
                    "description": str(meta.get("description") or meta.get("workflowPhase") or "")[:200]}
            if meta.get("toolUseId"):
                by_call.setdefault(meta["toolUseId"], info)
            elif a.parent.parent.name == "workflows":
                by_wf.setdefault(a.parent.name, []).append(info)
    return by_call, by_wf


def _cc_attach_subagents(msgs, files):
    """Point each Agent / Task / Workflow tool part at its subagent runs (read when the chat is saved, shown collapsed)."""
    by_call, by_wf = _cc_subagents(files)
    if not by_call and not by_wf:
        return 0
    n = 0
    for m in msgs:
        for p in m.get("parts") or []:
            cid = p.get("call_id")
            if not cid:
                continue
            runs = [by_call[cid]] if cid in by_call else []
            if p.get("name") == "Workflow":
                for wf in dict.fromkeys(WF_RE.findall((p.get("output") or "") + (p.get("input") or ""))):
                    runs += by_wf.get(wf, [])
            if runs:
                p["subagents"] = runs
                n += len(runs)
    return n


def _cc_slim(d):
    msg = d.get("message") or {}
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        slim = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                txt = _claude_result_text(b.get("content"))
                slim.append({"type": "tool_result", "tool_use_id": b.get("tool_use_id"), "is_error": b.get("is_error"),
                             "content": _cut(txt, max(SHOW_TOOL, MODEL_OUT) + 2000)})
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if isinstance(inp, dict):
                    inp = {k: (_cut(v, SHOW_INPUT) if isinstance(v, str) else v) for k, v in inp.items()}
                slim.append({"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": inp})
            else:
                slim.append(b)
        content = slim
    return {"type": d.get("type"), "uuid": d.get("uuid"), "sid": d.get("sessionId"), "timestamp": d.get("timestamp"), "subtype": d.get("subtype"),
            "isCompactSummary": d.get("isCompactSummary"), "isMeta": d.get("isMeta"), "content": content,
            "mid": msg.get("id") if isinstance(msg, dict) else None}


def _cc_head(path):
    """Folder, first prompt and title from the start and end of the file only (a session can be gigabytes), and `origin`:
    the session id on its first message, which is another session's when this one began as a fork of it."""
    info = {"cwd": None, "first": "", "title": "", "started": None, "origin": None}
    try:
        with open(path, "rb") as f:
            read = 0
            for raw in f:
                read += len(raw)
                if read > (8 << 20):
                    break
                if b'"type":"user"' not in raw[:400] and b'"type":"user"' not in raw[-400:]:
                    continue
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("type") != "user" or d.get("isSidechain"):
                    continue
                if info["origin"] is None:
                    info["origin"] = d.get("sessionId") or ""
                if d.get("isMeta"):
                    continue
                info["cwd"] = info["cwd"] or d.get("cwd")
                info["started"] = info["started"] or _ts(d.get("timestamp"))
                c = (d.get("message") or {}).get("content")
                texts = [c] if isinstance(c, str) else [b.get("text") or "" for b in c or [] if isinstance(b, dict) and b.get("type") == "text"]
                txt = next((x for x in map(_cc_user_text, texts) if x), "")
                if txt:
                    info["first"] = re.sub(r"\s+", " ", txt)[:240]
                    break
            size = os.path.getsize(path)
            f.seek(max(0, size - (256 << 10)))
            for raw in f.read().splitlines():
                if b'"customTitle"' in raw or b'"aiTitle"' in raw:
                    try:
                        d = json.loads(raw)
                        info["title"] = d.get("customTitle") or d.get("aiTitle") or info["title"]
                    except ValueError:
                        pass
    except OSError:
        pass
    return info


# ------------------------------------------------------------------------------------------------ Codex
CODEX_SKIP = re.compile(r"\s*(# AGENTS\.md instructions|<environment_context>|<user_instructions>|<permissions instructions>)")


def _codex_user_text(text):
    if not text or CODEX_SKIP.match(text):
        return ""
    ask = re.search(r"^## My request for Codex:\s*\n([\s\S]+)", text, re.M)     # the IDE puts mentioned files first
    if ask:
        return ask.group(1).strip()
    m = TAG_ONLY.fullmatch(text)
    if m and re.search(r"[-_ ]", m.group(1)):          # <environment_context>, <turn_aborted>... added by Codex
        return ""
    return text.strip()


def _codex_output(out):
    if isinstance(out, list):
        return "\n".join((b.get("text") or "[image]") if isinstance(b, dict) else str(b) for b in out)
    if isinstance(out, str) and out.startswith("{") and '"output"' in out[:200]:
        try:
            d = json.loads(out)
            if isinstance(d, dict) and "output" in d:
                code = (d.get("metadata") or {}).get("exit_code")
                return str(d["output"]) + (f"\n(exit {code})" if code not in (None, 0) else "")
        except ValueError:
            pass
    return _jtext(out) if out is not None else ""


def codex(path, progress=None):
    path = Path(path)
    T = Turns()
    events = Turns()                 # older rollouts: only event messages
    cwd, sid, started, subagent, n = None, None, None, False, 0
    with open(path, "rb") as f:
        for raw in f:
            n += len(raw)
            if progress and n > (1 << 24):
                progress(n)
                n = 0
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            ts = _ts(d.get("timestamp"))
            typ, pl = d.get("type"), d.get("payload") if isinstance(d.get("payload"), dict) else d
            if typ == "session_meta":
                sid = sid or pl.get("id") or pl.get("session_id")
                cwd = pl.get("cwd") or cwd
                started = started or _ts(pl.get("timestamp")) or ts
                src = pl.get("source")
                if pl.get("parent_thread_id") or pl.get("agent_path") or (isinstance(src, dict) and src.get("subagent")):
                    subagent = True
                continue
            if typ == "turn_context":
                cwd = pl.get("cwd") or cwd
                continue
            if typ == "compacted":
                T.notice("Codex compacted the context here.", ts)
                continue
            pt = pl.get("type")
            if typ == "event_msg":
                if pt == "user_message" and pl.get("message"):
                    txt = _codex_user_text(pl["message"])
                    if txt:
                        events.user([{"kind": "text", "text": _cut(txt, SHOW_TEXT)}], ts)
                elif pt == "agent_message" and pl.get("message"):
                    events.add({"kind": "text", "text": _cut(pl["message"], SHOW_TEXT)}, ts)
                continue
            if typ not in ("response_item", None):
                continue
            if pt == "message":
                role = pl.get("role")
                if role == "user":
                    parts = []
                    for b in pl.get("content") or []:
                        if b.get("type") == "input_text":
                            txt = _codex_user_text(b.get("text"))
                            if txt:
                                parts.append({"kind": "text", "text": _cut(txt, SHOW_TEXT)})
                        elif b.get("type") == "input_image":
                            url = str(b.get("image_url") or "")
                            m = re.match(r"data:(image/[\w.+-]+);base64,(.*)", url, re.S)
                            if m:
                                try:
                                    parts.append({"kind": "image", "name": "image", "data": base64.b64decode(m.group(2)), "mime": m.group(1)})
                                except ValueError:
                                    pass
                    T.user(parts, ts)
                elif role == "assistant":
                    for b in pl.get("content") or []:
                        if b.get("type") in ("output_text", "text"):
                            T.add({"kind": "text", "text": _cut(b.get("text") or "", SHOW_TEXT)}, ts)
            elif pt == "reasoning":
                txt = "\n\n".join(s.get("text") or "" for s in pl.get("summary") or [] if isinstance(s, dict))
                if txt.strip():
                    T.add({"kind": "thinking", "text": _cut(txt, SHOW_THINK)}, ts)
            elif pt in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
                name = pl.get("name") or {"local_shell_call": "shell", "web_search_call": "web_search"}.get(pt, "tool")
                if pl.get("namespace") and not str(pl["namespace"]).startswith("mcp__"):
                    name = f"{pl['namespace']}.{name}"
                inp = pl.get("arguments") if pt == "function_call" else pl.get("input") if pt == "custom_tool_call" else pl.get("action")
                if isinstance(inp, str) and inp.startswith("{"):
                    try:
                        inp = json.loads(inp)
                    except ValueError:
                        pass
                T.call(pl.get("call_id") or pl.get("id"), name, inp if inp is not None else "", ts)
            elif pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                T.result(pl.get("call_id"), _codex_output(pl.get("output")), ts=ts, name=pl.get("name"))
        if progress:
            progress(n)
    if not any(m["role"] == "user" for m in T.msgs) and events.msgs:
        T = events
    st = path.stat()
    return {"source": "codex", "sid": _codex_id(path, sid), "title": "", "created": started or st.st_mtime, "updated": st.st_mtime,
            "cwd": cwd, "turns": T.msgs, "notes": [f"It ran in {cwd}."] if cwd else [], "size": st.st_size, "subagent": subagent}


def _codex_id(path, meta_id=None):
    """The thread id in the file name (rollout-<time>-<uuid>.jsonl): a forked thread starts with its parent's meta line."""
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", path.stem, re.I)
    return m.group(1).lower() if m else str(meta_id or path.stem)


def _codex_head(path):
    info = {"cwd": None, "first": "", "title": "", "started": None, "sid": None, "subagent": False}
    try:
        with open(path, "rb") as f:
            read = 0
            for raw in f:
                read += len(raw)
                if read > (8 << 20):
                    break
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                pl = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                if d.get("type") == "session_meta":
                    info["sid"] = info["sid"] or pl.get("id") or pl.get("session_id")
                    info["cwd"] = pl.get("cwd") or info["cwd"]
                    info["started"] = info["started"] or _ts(pl.get("timestamp")) or _ts(d.get("timestamp"))
                    src = pl.get("source")
                    if pl.get("parent_thread_id") or pl.get("agent_path") or (isinstance(src, dict) and src.get("subagent")):
                        info["subagent"] = True
                    continue
                txt = ""
                if pl.get("type") == "message" and pl.get("role") == "user":
                    txt = next((x for x in (_codex_user_text(b.get("text")) for b in pl.get("content") or [] if b.get("type") == "input_text") if x), "")
                elif pl.get("type") == "user_message":
                    txt = _codex_user_text(pl.get("message"))
                if txt:
                    info["first"] = re.sub(r"\s+", " ", txt)[:240]
                    break
    except OSError:
        pass
    return info


# ------------------------------------------------------------------------------------------------ sessions on this computer
def local_roots(source):
    if source == "claude-code":
        return [HOME / ".claude" / "projects"]
    if source == "codex":
        return [HOME / ".codex" / "sessions", HOME / ".codex" / "archived_sessions"]
    raise ValueError("unknown source")


def local_files(source):
    out = []
    for root in local_roots(source):
        if not root.is_dir():
            continue
        if source == "claude-code":
            out += [p for p in root.glob("*/*.jsonl")]           # subagent runs live deeper: they go inside their session
        else:
            out += [p for p in root.rglob("rollout-*.jsonl")]
    return out


_codex_sub_cache = {"stamp": None, "ids": frozenset()}


def codex_subagent_ids():
    """Session ids of Codex subagent side-chains on this computer (cached by file count and newest mtime)."""
    files = local_files("codex")
    if not files:
        return frozenset()
    try:
        stamp = (len(files), max(p.stat().st_mtime for p in files))
    except OSError:
        stamp = (len(files), 0)
    if _codex_sub_cache["stamp"] == stamp:
        return _codex_sub_cache["ids"]
    subs = set()
    for p in files:
        if _codex_head(p).get("subagent"):
            subs.add(_codex_id(p).lower())
    _codex_sub_cache["stamp"] = stamp
    _codex_sub_cache["ids"] = frozenset(subs)
    return _codex_sub_cache["ids"]


def is_local(source, path):
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    return p.suffix == ".jsonl" and any(p.is_relative_to(r.resolve()) for r in local_roots(source) if r.exists())


DESKTOP_DIRS = None              # tests point this at their own claude-code-sessions folders


def desktop_dirs():
    """Where the Claude desktop app keeps the sessions of its Code tab: claude-code-sessions/<account>/<org>/local_*.json,
    one small file per session with its title, folder, and the session it was forked from or started by. Windows:
    %APPDATA%\\Claude (or a profile folder under %LOCALAPPDATA%\\Claude-Data), macOS: ~/Library/Application Support/Claude,
    Linux: ~/.config/Claude. Only those per-session files are ever read there."""
    if DESKTOP_DIRS is not None:
        return [Path(x) for x in DESKTOP_DIRS]
    if os.name == "nt":
        bases = [Path(os.environ[v]) / n for v in ("APPDATA", "LOCALAPPDATA") if os.environ.get(v) for n in ("Claude", "Claude-Data")]
    elif sys.platform == "darwin":
        bases = [HOME / "Library" / "Application Support" / "Claude"]
    else:
        bases = [Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "Claude"]
    out = []
    for b in bases:
        try:
            subs = [x for x in b.iterdir() if x.is_dir()] if b.is_dir() else []
        except OSError:
            subs = []
        for d in [b, *subs]:
            if (d / "claude-code-sessions").is_dir():
                out.append(d / "claude-code-sessions")
    return out


def desktop_sessions():
    """What the desktop app knows of its Code sessions, by Claude Code session id (the transcript's file name). Two
    accounts or a moved data folder can list a session twice: the copy with the latest activity wins.
    parent: the session it was forked from or started by (what the app nests it under); fork_cli: the one it copied."""
    by_local = {}
    for root in desktop_dirs():
        for f in root.glob("*/*/local_*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            sid, cli = d.get("sessionId"), d.get("cliSessionId")
            if not isinstance(sid, str) or not isinstance(cli, str) or not cli:
                continue
            last = _ts(d.get("lastActivityAt")) or 0
            if sid in by_local and by_local[sid]["last"] >= last:
                continue
            sf = d.get("spawnedFrom") if isinstance(d.get("spawnedFrom"), dict) else {}
            fork = d.get("forkedFromSessionId") if isinstance(d.get("forkedFromSessionId"), str) else None
            kind = "handoff" if sf.get("handOff") else "fork" if sf.get("userFork") or (fork and not sf) else "side" if sf else None
            by = sf.get("sessionId") if isinstance(sf.get("sessionId"), str) else fork
            by_local[sid] = {"local": sid, "cli": cli, "title": str(d.get("title") or "").strip(), "created": _ts(d.get("createdAt")),
                             "cwd": d.get("cwd") or d.get("originCwd"), "last": last, "archived": bool(d.get("isArchived")),
                             "by": by, "fork": fork, "kind": kind,
                             "prior": [x for x in d.get("priorCliSessionIds") or [] if isinstance(x, str)]}
    out = {}
    for v in by_local.values():
        v["parent"] = by_local[v["by"]]["cli"] if v["by"] in by_local else None
        v["fork_cli"] = by_local[v["fork"]]["cli"] if v["fork"] in by_local else None
        out[v["cli"]] = v
    return out


def _folder_group(cwd):
    if not cwd:
        return "claude-code:folder:", "Claude Code"
    return "claude-code:folder:" + os.path.normcase(os.path.normpath(cwd)), Path(cwd).name or str(cwd)


def cc_catalog():
    """Every Claude Code session on this computer once (a moved data folder can leave two copies of one: the longest
    is used), with what the desktop app knows of it and how the sessions hang together:
      parent  the session it was forked from or started by (the desktop app's link, else a fork's copied start)
      root    the top of its family; depth: how far below it
      group   (key, name): a family's group is named after its root; a session on its own from the desktop app stays
              ungrouped; other sessions (run in a terminal) are grouped by the folder they ran in."""
    files = {}
    for p in local_files("claude-code"):
        try:
            st = p.stat()
        except OSError:
            continue
        if p.stem not in files or st.st_size > files[p.stem][1]:
            files[p.stem] = (p, st.st_size, st.st_mtime)
    desk = desktop_sessions()
    went_on = {old for cli, d in desk.items() if cli in files for old in d["prior"]}
    cat = {}
    for sid, (p, size, mtime) in files.items():
        if sid in went_on:               # an older transcript of a desktop session that went on in a new one
            continue
        d = desk.get(sid)
        extra = [files[x][0] for x in (d["prior"] if d else []) if x in files]
        head = _cc_head(p)
        cat[sid] = {"sid": sid, "path": p, "extra": extra, "size": size + sum(files[x.stem][1] for x in extra),
                    "mtime": max([mtime] + [files[x.stem][2] for x in extra]), "desk": d, "head": head}
    for sid, e in cat.items():
        d = e["desk"]
        par = d["parent"] if d and d["parent"] in cat and d["parent"] != sid else None
        kind = d["kind"] if par else None
        if not par:
            o = e["head"].get("origin")
            if o and o != sid and o in cat and o not in {x.stem for x in e["extra"]}:
                par, kind = o, "fork"
        e["parent"], e["kind"] = par, kind
        e["title"] = (d["title"] if d else "") or e["head"]["title"]
        e["cwd"] = e["head"]["cwd"] or (d["cwd"] if d else None)
        e["started"] = e["head"]["started"] or (d["created"] if d else None) or e["mtime"]
        e["last"] = max(e["mtime"], (d or {}).get("last") or 0)
    for sid, e in cat.items():           # a loop of links (never expected) is cut where it closes
        cur, path = sid, [sid]
        while cat[cur]["parent"]:
            nxt = cat[cur]["parent"]
            if nxt in path:
                cat[cur]["parent"] = cat[cur]["kind"] = None
                break
            path.append(nxt)
            cur = nxt
        e["root"], e["depth"] = cur, len(path) - 1
    members = {}
    for e in cat.values():
        members.setdefault(e["root"], []).append(e)
    # groups are (key, name, parent key, parent name). With the desktop app's list there, the sessions it does not know
    # (run in a terminal, or by another program) go in one group, a subgroup for each folder that has several
    desk_known = any(e["desk"] for e in cat.values())
    alone = [e for e in cat.values() if len(members[e["root"]]) == 1 and not e["desk"]]
    per_folder = {}
    for e in alone:
        per_folder[_folder_group(e["cwd"])[0]] = per_folder.get(_folder_group(e["cwd"])[0], 0) + 1
    for e in cat.values():
        r = cat[e["root"]]
        if len(members[e["root"]]) > 1:
            e["group"] = ("claude-code:family:" + r["sid"], r["title"] or r["head"]["first"][:60] or "Claude Code", None, None)
        elif e["desk"]:
            e["group"] = None
        else:
            fk, fname = _folder_group(e["cwd"])
            if not desk_known:
                e["group"] = (fk, fname, None, None)
            elif per_folder[fk] > 1:
                e["group"] = (fk, fname, *OTHER)
            else:
                e["group"] = (*OTHER, None, None)
        e["family_last"] = max(x["last"] for x in members[e["root"]])
    return cat


OTHER = ("claude-code:other", "Other Claude Code sessions")


def _tree_order(entries):
    """Families together, newest family first, each as root then its children (oldest first), depth-first."""
    kids = {}
    sids = {e["sid"] for e in entries}
    for e in entries:
        kids.setdefault(e["parent"] if e["parent"] in sids else None, []).append(e)
    out = []

    def walk(e, depth):
        out.append((e, depth))
        for k in sorted(kids.get(e["sid"], []), key=lambda x: x["started"] or 0):
            walk(k, depth + 1)
    for r in sorted(kids.get(None, []), key=lambda x: -(x.get("family_last") or x["last"])):
        walk(r, 0)
    return out


def _app_sessions():
    """Claude Code sessions this app ran itself (a chat on the Claude Code engine): already chats here."""
    return {c.get("claude_session") for c in store.list_chats() if c.get("claude_session")}


def local_sessions(source):
    idx = _index()
    out = []
    if source == "claude-code":
        made = _app_sessions()
        cat = cc_catalog()
        shown = [e for e in cat.values() if e["sid"] not in made and (e["head"]["first"] or e["desk"])]
        for e, depth in _tree_order(shown):
            have = _have(idx, f"claude-code:{e['sid']}")
            state = "new"
            if have:
                state = "imported" if (store.Chat(have).meta().get("imported") or {}).get("size") == e["size"] else "grown"
            out.append({"path": str(e["path"]), "sid": e["sid"], "project": e["cwd"] or "", "title": e["title"] or "",
                        "first": e["head"]["first"], "started": e["started"], "updated": e["last"], "size": e["size"],
                        "imported": state == "imported", "state": state, "depth": depth, "kind": e["kind"],
                        "family": cat[e["root"]]["title"] if e["root"] != e["sid"] or depth else "",
                        "desktop": bool(e["desk"])})
        return out
    for p in local_files(source):
        try:
            st = p.stat()
        except OSError:
            continue
        info = _codex_head(p)
        if not info["first"] or info.get("subagent"):
            continue
        sid = _codex_id(p, info.get("sid"))
        out.append({"path": str(p), "sid": sid, "project": info["cwd"] or "", "title": info["title"] or "", "first": info["first"],
                    "started": info["started"] or st.st_mtime, "updated": st.st_mtime, "size": st.st_size,
                    "imported": bool(_have(idx, f"{source}:{sid}"))})
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


# ------------------------------------------------------------------------------------------------ remembering imports
_ilock = threading.Lock()


def _index():
    try:
        return json.loads((IMPORTS / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(idx):
    IMPORTS.mkdir(parents=True, exist_ok=True)
    tmp = IMPORTS / "index.json.tmp"
    tmp.write_text(json.dumps(idx, indent=0), encoding="utf-8")
    os.replace(tmp, IMPORTS / "index.json")


def _have(idx, key):
    """The chat a conversation was imported into, if it is still there (a deleted chat can be imported again)."""
    cid = idx.get(key)
    if not cid:
        return None
    try:
        m = json.loads((config.CHATS / cid / "chat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None if m.get("deleted") else cid


# ------------------------------------------------------------------------------------------------ saving a chat
def imported_project():
    return config.WORKSPACE / "Imported"


def _model_part(p):
    if p["kind"] == "text":
        return p["text"]
    if p["kind"] == "tool":
        s = f"[{p['name']}]"
        if p.get("model_input"):
            s += " " + p["model_input"]
        if "model_output" in p:
            s += ("\n[error] " if p.get("error") else "\n[result] ") + (p["model_output"] or "(empty)")
        return s
    if p["kind"] == "image":
        return f"[image: {p.get('name') or 'image'}]"
    if p["kind"] == "file":
        s = f"[attached file: {p.get('name') or 'file'}]"
        if p.get("text"):
            s += "\n" + _cut_model(p["text"], MODEL_FILE)
        return s
    return ""                       # thinking is shown, not sent


def _save_assets(chat_dir, parts, conv_assets, budget):
    for p in parts:
        if p["kind"] not in ("image", "file") or budget[0] <= 0:
            continue
        data = p.pop("data", None)
        name = p.get("asset")
        if data is None and name and conv_assets:
            try:
                data = conv_assets.read_asset(name)
            except (KeyError, OSError, zipfile.BadZipFile):
                data = None
        if not data:
            if not p.get("text"):
                p["missing"] = True
            continue
        ext = Path(str(name or "")).suffix.lower() if name else "." + str(p.pop("mime", "image/png")).split("/")[-1].split("+")[0]
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext or ""):
            ext = ".bin"
        stem = re.sub(r"[^\w.-]+", "_", Path(str(name or p.get("name") or "file")).stem)[:60] or "file"
        dest = chat_dir / "files" / f"{budget[1]:03d}-{stem}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        p["path"] = str(dest)
        p.pop("asset", None)
        budget[0] -= 1
        budget[1] += 1
    for p in parts:
        p.pop("data", None)
        p.pop("mime", None)
        p.pop("ref", None)
        if "asset" in p:
            p.pop("asset")


def _save_subagents(chat_dir, parts, src, budget):
    """Each subagent run a tool part points at becomes one line of the chat's subagents.jsonl (opened from the tool
    row, never at chat open). The part keeps its name, kind and size."""
    for p in parts:
        runs = p.get("subagents") if p.get("kind") == "tool" else None
        if not runs:
            continue
        done = []
        for r in runs:
            if "path" not in r:
                done.append(r)
                continue
            info = {"id": r["id"], "type": r.get("type") or "subagent", "description": r.get("description") or ""}
            try:
                c = claude_code(r["path"], sub=True)
                msgs, _ = _convert(c["turns"], src, chat_dir, None, budget)
                for m in msgs:
                    m.pop("model_content", None)
                    for q in m.get("parts") or []:
                        if q.get("kind") == "tool":
                            q["input"] = _cut(q.get("input"), SUB_SHOW)
                            if q.get("output") is not None:
                                q["output"] = _cut(q["output"], SUB_SHOW)
                            if len(q.get("diff") or "") > SUB_SHOW:
                                q["diff"] = _cut(q["diff"], SUB_SHOW)
                with open(chat_dir / "subagents.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"id": r["id"], "messages": msgs}, ensure_ascii=False) + "\n")
                info["messages"] = sum(1 for m in msgs if m["role"] in ("user", "assistant"))
                info["tools"] = sum(1 for m in msgs for q in m.get("parts") or [] if q.get("kind") == "tool")
            except Exception:              # an unreadable run is listed, not fatal
                info["missing"] = True
            done.append(info)
        p["subagents"] = done


def subagent_messages(cid, aid):
    """One subagent run of an imported chat, for its tool row."""
    chat = store.Chat(cid)
    want = ('{"id": ' + json.dumps(str(aid))).encode("utf-8")
    try:
        with open(chat.dir / "subagents.jsonl", "rb") as f:
            for raw in f:
                if raw.startswith(want):
                    return json.loads(raw)["messages"]
    except (OSError, ValueError):
        pass
    raise ValueError("that subagent run was not imported")


def subagent_window(cid, aid, limit=None, before=None):
    """Paged slice of an imported subagent run so opening huge (15-30 MB) subagent transcripts stays instant."""
    msgs = subagent_messages(cid, aid)
    total = len(msgs)
    if not limit:
        return {"messages": msgs, "start": 0, "total": total}
    lim = max(1, int(limit))
    end = total if before is None else max(0, min(int(before), total))
    start = max(0, end - lim)
    return {"messages": msgs[start:end], "start": start, "total": total}


def _convert(turns, src, chat_dir, assets, budget, intro=None, first=0, fallback=None):
    """Turns -> stored messages. Each carries `turn` (its place in the source), so a later import of the same session
    can tell what came from it. Returns (messages, characters the model would get)."""
    out, chars = [], 0
    for i, m in enumerate(turns):
        ts = m.get("ts") or fallback
        if m["role"] == "notice":
            out.append({"role": "notice", "content": m["content"], "ts": ts, "id": store.new_id("m"), "imported": src, "turn": first + i})
            continue
        parts = m["parts"]
        _save_assets(chat_dir, parts, assets, budget)
        _save_subagents(chat_dir, parts, src, budget)
        text = "\n\n".join(p["text"] for p in parts if p["kind"] == "text")
        model = "\n\n".join(x for x in map(_model_part, parts) if x)
        msg = {"role": m["role"], "content": text, "ts": ts, "id": store.new_id("m"), "imported": src, "turn": first + i}
        if any(p["kind"] != "text" for p in parts):
            msg["parts"] = [{k: v for k, v in p.items() if not k.startswith("model_")} for p in parts]
        if intro and m["role"] == "user":
            model, intro = intro + model, None
        if model != text:
            msg["model_content"] = model or "(empty)"
        chars += len(model)
        out.append(msg)
    return out, chars


def _intro(label):
    return (f"(This chat was imported from {label}. Tool calls in it ran there: they are shown as text and cannot be re-run "
            "here.)\n\n")


def _turn_hashes(turns):
    return [hashlib.sha1(json.dumps(m, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8", "replace")).hexdigest()
            for m in turns]


def _hash(hs):
    return hashlib.sha1("".join(hs).encode()).hexdigest()


def _fork_message(fork, label, created):
    """Where a fork came from, as the compaction its own part starts from: the model gets a summary of the session before
    the fork (no model call); the page shows it as a line pointing at that chat."""
    pre = []
    for m in fork["turns"]:
        if m["role"] in ("user", "assistant"):
            pre.append({"role": m["role"], "content": "\n\n".join(p["text"] for p in m["parts"] if p["kind"] == "text"),
                        "model_content": "\n\n".join(x for x in map(_model_part, m["parts"]) if x)})
    chars = sum(len(x["model_content"]) for x in pre)
    s = _summary(pre, label, chars, f"This {label} session was forked from another one. What was said there before the fork "
                                    f"({chars:,} characters), in short. What the user asked there, oldest first:")
    s.update(why="fork", ts=fork.get("at") or created, fork_from=fork["from"], fork_at=fork.get("at"), fork_after=fork.get("after") or "",
             before=len(pre))
    return s


def _imported_meta(conv, hs, at=None):
    f = conv.get("fork") or {}
    return {"source": conv["source"], "id": conv["sid"], "at": at or time.time(), "cwd": conv.get("cwd"), "count": len(hs),
            "head": _hash(hs[:-1]), "tail": _hash(hs[-1:]), "size": conv.get("size"), "files": conv.get("files"),
            "fork_from": f.get("from"), "fork_at": f.get("at")}


def session_model(path):
    """The model a Claude Code session was last on (read from the end of its transcript), or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = re.findall(r'"model"\s*:\s*"(claude-[a-z0-9.-]+)"', tail)
    return found[-1] if found else None


def source_endpoint(source, model=None):
    """The connection an imported chat belongs on, so it opens on the model it was really using: a Claude Code session
    on the Claude Code connection (the one with the session's own model when there are several, e.g. Opus and Fable),
    a Codex session on the Codex connection. None when there is no such connection."""
    kind = {"claude-code": "claude-code", "codex": "codex"}.get(source)
    same = [e for e in (config.get().get("endpoints") or []) if e.get("kind") == kind]
    if not same:
        return None
    return next((e["id"] for e in same if model and e.get("model") == model), same[0]["id"])


def fix_imported_endpoints():
    """Imported Claude Code and Codex chats used to be tied to whichever model happened to be selected at import time
    (so a Claude session opened on the local model). Put every one that was never carried on here back on its own
    model. A chat with a turn made in this app keeps the model that turn used. Returns how many were changed."""
    changed = 0
    for c in store.list_chats():
        src = {LABEL["claude-code"]: "claude-code", LABEL["codex"]: "codex"}.get(c.get("source"))
        if not src:
            continue
        chat = store.Chat(c["id"])
        meta = chat.meta()
        native = None
        try:
            with open(chat.dir / "messages.jsonl", encoding="utf-8") as f:
                for line in f:
                    if '"endpoint"' in line and '"imported"' not in line:
                        try:
                            m = json.loads(line)
                        except ValueError:
                            continue
                        if m.get("role") == "assistant" and m.get("endpoint"):
                            native = m["endpoint"]
        except OSError:
            pass
        model = None
        if src == "claude-code":
            sid = (meta.get("imported") or {}).get("id") or meta.get("claude_session")
            hit = next((p for p in local_files("claude-code") if p.stem == sid), None) if sid else None
            model = session_model(hit) if hit else None
        want = native or source_endpoint(src, model)
        if want and want != meta.get("endpoint") and config.endpoint(want):
            chat.update_meta(endpoint=want)
            changed += 1
    return changed


def save_chat(conv, project, endpoint, assets=None, extra=None):
    """Write one converted conversation as a chat. Returns (chat id, number of messages)."""
    src = conv["source"]
    label = LABEL[src]
    turns = conv["turns"]
    hs = _turn_hashes(turns) if src in ("claude-code", "codex") else []
    first_user = next((m for m in turns if m["role"] == "user"), None)
    title = conv.get("title") or (next((p["text"] for p in first_user["parts"] if p["kind"] == "text"), "") if first_user else "")
    if not title and conv.get("fork"):
        title = f"Fork: {conv['fork'].get('after') or label}"
    title = store.clean_title(title or f"{label} chat")
    times = [m["ts"] for m in turns if m.get("ts")]
    created = conv.get("created") or (min(times) if times else time.time())
    updated = conv.get("updated") or (max(times) if times else created)
    chat = store.Chat(store.new_id())          # written once at the end (store.create would write chat.json twice)
    chat.dir.mkdir(parents=True, exist_ok=True)
    budget = [ASSETS_PER_CHAT, 1]
    head = f"Imported from {label}. Started {_day(created)}, last message {_day(updated)}."
    if conv.get("notes"):
        head += " " + " ".join(conv["notes"])
    out = [{"role": "notice", "content": head, "ts": created, "id": store.new_id("m")}]
    if conv.get("fork"):
        out.append(_fork_message(conv["fork"], label, created))
    body, model_chars = _convert(turns, src, chat.dir, assets, budget, _intro(label), 0, updated)
    out += body
    if model_chars > COMPACT_CHARS:
        out.append(_summary(out, label, model_chars))
    chat.rewrite(out)
    meta = {"id": chat.id, "title": title, "project": str(project), "endpoint": endpoint, "created": created,
            "updated": updated, "reasoning": None, "usage": {}, "source": label,
            "imported": {"source": src, "id": conv["sid"], "at": time.time(), "cwd": conv.get("cwd")}}
    if hs:
        meta["imported"] = _imported_meta(conv, hs)
    meta.update(extra or {})
    chat.save_meta(meta)
    return chat.id, sum(1 for m in out if m["role"] in ("user", "assistant"))


def _said_here(m, i):
    """A message added in this app after the import (the user carried the chat on here), not one that came from the
    session: imported messages carry `imported`; the chat's first line and the fork/size summaries are the import's own."""
    if "imported" in m or "turn" in m:
        return False
    if i == 0 and m.get("role") == "notice":
        return False
    return not (m.get("role") == "compaction" and m.get("why") in ("fork", "import"))


def update_chat(cid, conv):
    """A session that went on after it was imported. The rule (also when the chat was carried on here):
      - nothing new in the session (only bookkeeping lines): nothing changes ("same");
      - its history up to the last imported message is unchanged: the last imported message is redone where it is (it may
        have grown) and the new ones go on the end ("grown"). If something was said here after the last imported
        message, the new ones come after it, behind a line saying they came later from the session ("continued");
        nothing already in the chat moves, nothing is repeated, and the model sees the chat in that order;
      - its history changed (rewound or edited there): the imported part is rebuilt, and everything said here is kept
        after it, behind a line saying so ("rebuilt").
    Returns (messages added, how)."""
    chat = store.Chat(cid)
    with chat.lock:
        meta = chat.meta()
        imp = meta.get("imported") or {}
        src = conv["source"]
        label = LABEL[src]
        turns = conv["turns"]
        hs = _turn_hashes(turns)
        n = int(imp.get("count") or 0)
        old = [m for m in chat.messages() if not (m.get("role") == "compaction" and m.get("why") == "import")]
        before = sum(1 for m in old if m.get("role") in ("user", "assistant"))
        mine = [i for i, m in enumerate(old) if "turn" in m]
        here_all = [i for i, m in enumerate(old) if _said_here(m, i) and (not mine or i > mine[0])]
        here_after = [i for i in here_all if not mine or i > mine[-1]]
        files = chat.dir / "files"
        budget = [ASSETS_PER_CHAT, 1 + (len(list(files.iterdir())) if files.is_dir() else 0)]
        same = n > 0 and len(hs) >= n and _hash(hs[:n - 1]) == imp.get("head")
        how = "same"
        if same and len(hs) == n and _hash(hs[-1:]) == imp.get("tail"):
            out = None                                      # only bookkeeping lines were added to the transcript
        elif same:
            out = list(old)
            if _hash(hs[n - 1:n]) != imp.get("tail"):       # the last imported message grew: redone where it is
                redo, _ = _convert(turns[n - 1:n], src, chat.dir, None, budget, None, n - 1, conv.get("updated"))
                at = next((i for i in range(len(out) - 1, -1, -1) if out[i].get("turn") == n - 1), None)
                if at is not None and redo:
                    redo[0]["id"] = out[at]["id"]
                    out[at:at + 1] = redo
                elif redo:
                    out += redo
            new, _ = _convert(turns[n:], src, chat.dir, None, budget, None, n, conv.get("updated"))
            if new and here_after:
                how = "continued"
                out.append({"role": "notice", "ts": new[0]["ts"] or time.time(), "id": store.new_id("m"), "imported": src,
                            "content": f"{len(new)} later message{'s' if len(new) != 1 else ''} from the {label} session, "
                                       "after what was said here."})
            else:
                how = "grown"
            out += new
        else:                                               # rewound or edited there: rebuild what came from it
            how = "rebuilt"
            head = [m for i, m in enumerate(old[:mine[0] if mine else len(old)]) if not _said_here(m, i)]
            here = [old[i] for i in here_all] if mine else [m for i, m in enumerate(old) if _said_here(m, i)]
            try:
                (chat.dir / "subagents.jsonl").unlink()
            except OSError:
                pass
            body, _ = _convert(turns, src, chat.dir, None, budget, _intro(label), 0, conv.get("updated"))
            out = head + body
            if here:
                out.append({"role": "notice", "ts": time.time(), "id": store.new_id("m"), "imported": src,
                            "content": f"The {label} session's history changed there (rewound or edited), so it was "
                                       "imported again above. What was said here follows."})
                out += here
        if out is not None:
            chars = sum(len(m.get("model_content") or m.get("content") or "") for m in out
                        if m.get("role") in ("user", "assistant") and "turn" in m)
            if chars > COMPACT_CHARS:
                at = max(i for i, m in enumerate(out) if "turn" in m) + 1 if any("turn" in m for m in out) else len(out)
                out.insert(at, _summary(out[:at], label, chars))
            chat.rewrite(out)
        meta["imported"] = {**imp, **_imported_meta(conv, hs, imp.get("at"))}
        meta["imported"]["updated_at"] = time.time()
        if here_all:
            meta["imported"]["continued"] = True            # carried on here: the list and the update summary say so
        meta["updated"] = max(meta.get("updated") or 0, conv.get("updated") or 0)
        chat.save_meta(meta)
        after = sum(1 for m in (out or old) if m.get("role") in ("user", "assistant"))
        return max(0, after - before), how


def _summary(msgs, label, chars, lead=None):
    """A compaction made without a model: the user's requests and the end of the chat, so a continued chat fits."""
    reqs = [m["content"] for m in msgs if m["role"] == "user" and m.get("content")]
    lines = ["- " + re.sub(r"\s+", " ", r)[:400] for r in reqs]
    if len(lines) > 80:
        lines = lines[:20] + [f"- ({len(lines) - 80} more requests)"] + lines[-60:]
    tail, n = [], 0
    for m in reversed(msgs):
        if m["role"] not in ("user", "assistant"):
            continue
        t = (m.get("model_content") or m.get("content") or "")[:12_000]
        tail.append(("User: " if m["role"] == "user" else "Assistant: ") + t)
        n += len(t)
        if n > 40_000:
            break
    fork = next((m for m in msgs if m.get("role") == "compaction" and m.get("why") == "fork"), None) if lead is None else None
    lead = lead or (f"This chat was imported from {label} and is too long to send in full ({chars:,} characters). "
                    "What the user asked, oldest first:")
    text = lead + "\n" + "\n".join(lines) + "\n\nThe end of the chat, word for word:\n\n" + "\n\n".join(reversed(tail))
    if fork:                          # a long fork: what came before the fork, in short, goes first
        text = fork["summary"][:8_000] + "\n\n---\n\n" + text
    return {"role": "compaction", "summary": text, "why": "import", "tokens_before": int(chars / 3.3), "notes": "", "log": "",
            "ts": time.time(), "id": store.new_id("m")}


# ------------------------------------------------------------------------------------------------ jobs
JOBS = {}
_jlock = threading.Lock()


class Job:
    def __init__(self, kind):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.state = "running"
        self.phase = ""
        self.done = 0
        self.total = 0
        self.t0 = time.time()
        self.result = {}
        self.error = None
        self.cancel = threading.Event()

    def public(self):
        return {"id": self.id, "kind": self.kind, "state": self.state, "phase": self.phase, "done": self.done, "total": self.total,
                "secs": round(time.time() - self.t0, 1), "result": self.result, "error": self.error}


def _run(kind, fn, *args):
    job = Job(kind)
    with _jlock:
        for k in [k for k, j in JOBS.items() if j.state != "running" and time.time() - j.t0 > 3600]:
            JOBS.pop(k, None)
        JOBS[job.id] = job

    def go():
        try:
            fn(job, *args)
            if job.state == "running":
                job.state = "cancelled" if job.cancel.is_set() else "done"
        except Exception as e:
            job.state, job.error = "error", str(e) or e.__class__.__name__
        finally:
            job.result["secs"] = round(time.time() - job.t0, 1)
    threading.Thread(target=go, daemon=True, name=f"chat-import-{job.id}").start()
    return job


def job(jid):
    j = JOBS.get(jid)
    if not j:
        raise ValueError("no such import")
    return j


def scan_export(path):
    """Look through an export: what it is, how many chats, from when to when, how many are already here."""
    def fn(job):
        ex = Export(path)
        try:
            if not ex.kind:
                raise ValueError("This does not look like a ChatGPT or Claude.ai export.")
            job.total = ex.total()
            job.phase = f"Reading the {LABEL[ex.kind]} export"
            idx = _index()
            n = have = 0
            first = last = None

            def prog(b):
                job.done = b
                if job.cancel.is_set():
                    raise InterruptedError
            try:
                for conv in ex.conversations(prog):
                    if ex.kind == "chatgpt":
                        sid, a, b = conv.get("conversation_id") or conv.get("id"), _ts(conv.get("create_time")), _ts(conv.get("update_time"))
                    else:
                        sid, a, b = conv.get("uuid"), _ts(conv.get("created_at")), _ts(conv.get("updated_at"))
                    n += 1
                    have += bool(_have(idx, f"{ex.kind}:{sid}"))
                    for t in (a, b):
                        if t:
                            first, last = min(first or t, t), max(last or t, t)
            except InterruptedError:
                return
            job.done = job.total
            job.result = {"kind": ex.kind, "label": LABEL.get(ex.kind, "?"), "count": n, "already": have, "first": first, "last": last,
                          "assets": len(ex.names), "path": str(path)}
        finally:
            ex.close()
    return _run("scan", fn)


def import_export(path, endpoint=None):
    def fn(job):
        ex = Export(path)
        try:
            if not ex.kind:
                raise ValueError("This does not look like a ChatGPT or Claude.ai export.")
            job.total = ex.total()
            job.phase = f"Importing from {LABEL[ex.kind]}"
            project = str(store.add_project(imported_project(), "Imported"))
            ep = endpoint or config.get().get("endpoint") or ""
            idx = _index()
            res = job.result = {"kind": ex.kind, "label": LABEL[ex.kind], "imported": 0, "skipped": 0, "failed": 0, "messages": 0,
                                "errors": [], "project": project}

            def prog(b):
                job.done = b
            for conv in ex.conversations(prog):
                if job.cancel.is_set():
                    break
                try:
                    c = chatgpt(conv, ex) if ex.kind == "chatgpt" else claude_ai(conv)
                    key = f"{ex.kind}:{c['sid']}"
                    if not c["sid"] or _have(idx, key):
                        res["skipped"] += 1
                        continue
                    if not any(m["role"] in ("user", "assistant") for m in c["turns"]):
                        res["skipped"] += 1
                        continue
                    cid, nmsg = save_chat(c, project, ep, ex)
                    idx[key] = cid
                    try:
                        from . import sections
                        sections.assign_new_chat(cid)
                    except Exception:
                        pass
                    res["imported"] += 1
                    res["messages"] += nmsg
                    if res["imported"] % 25 == 0:
                        _save_index(idx)
                except Exception as e:          # one bad conversation does not stop the rest
                    res["failed"] += 1
                    if len(res["errors"]) < 20:
                        res["errors"].append(f"{str(conv.get('title') or conv.get('name') or '?')[:60]}: {e.__class__.__name__}: {e}")
            job.done = job.total
        finally:
            if "idx" in locals():
                _save_index(idx)
            ex.close()
            _forget_upload(path)
    return _run("import", fn)


_work = threading.Lock()          # one Claude Code / Codex import or update at a time (they share the index and groups)


def _sources():
    try:
        return json.loads((IMPORTS / "sources.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember(src, own_folders):
    """What an update needs to know about how a source was imported: since when (sessions that start later are new and
    are imported by an update too) and whether chats went in the folder they ran in."""
    d = _sources()
    e = d.setdefault(src, {})
    e.setdefault("since", time.time())
    e["own_folders"] = bool(own_folders)
    IMPORTS.mkdir(parents=True, exist_ok=True)
    tmp = IMPORTS / "sources.json.tmp"
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, IMPORTS / "sources.json")


def _record_sync(src):
    d = _sources()
    e = d.setdefault(src, {})
    e["last_sync"] = time.time()
    IMPORTS.mkdir(parents=True, exist_ok=True)
    tmp = IMPORTS / "sources.json.tmp"
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.replace(tmp, IMPORTS / "sources.json")


def last_sync_map():
    """Return {display_label: unix_ts} of when each live local source was last checked."""
    d = _sources()
    out = {}
    for src in ("claude-code", "codex"):
        ts = (d.get(src) or {}).get("last_sync") or _last_auto()
        if ts:
            out[LABEL[src]] = float(ts)
    return out


def _since(src, idx):
    """When this source was first imported (sources.json, else the oldest chat imported from it); None: never."""
    s = (_sources().get(src) or {}).get("since")
    if s:
        return s
    ats = []
    for k, cid in idx.items():
        if k.startswith(src + ":"):
            try:
                ats.append((json.loads((config.CHATS / cid / "chat.json").read_text(encoding="utf-8")).get("imported") or {}).get("at"))
            except (OSError, ValueError):
                pass
    ats = [a for a in ats if a]
    return min(ats) if ats else None


def import_local(source, files=None, own_folders=False, endpoint=None, everything=False):
    files = [str(f) for f in files or []]
    for f in files:
        if not is_local(source, f):
            raise PermissionError("only sessions under ~/.claude/projects or ~/.codex/sessions")
    if source == "claude-code":
        return _run("import", _import_claude_code, files, own_folders, endpoint, everything)

    def fn(job):
        with _work:
            if files:
                _remember(source, own_folders)
            job.result = {**_new_result(source), "label": LABEL[source]}
            sizes = {f: (os.path.getsize(f) if os.path.exists(f) else 0) for f in files}
            job.total = sum(sizes.values()) or 1
            job.phase = f"Importing {LABEL[source]} sessions"
            _codex_run(job, files, own_folders, job.result, endpoint, update=False)
    return _run("import", fn)


def _import_claude_code(job, files, own_folders, endpoint, everything):
    """Claude Code sessions with their structure: a session already here is updated if it went on since (new messages on
    the end), forks and side sessions are nested under the session they came from, each family in a group named after
    the session at its top, sessions run in a terminal grouped by their folder. Parents go first."""
    with _work:
        job.phase = "Looking at the Claude Code sessions"
        cat = cc_catalog()
        if everything:
            made = _app_sessions()
            todo = [e for e in cat.values() if e["sid"] not in made and (e["head"]["first"] or e["desk"])]
        else:
            stems = {Path(f).stem for f in files}
            todo = [e for e in cat.values() if e["sid"] in stems]
            for f in files:                      # a file the catalog passed over (an older copy of a session) still imports
                p = Path(f)
                if p.stem not in cat:
                    todo.append({"sid": p.stem, "path": p, "extra": [], "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                                 "desk": None, "head": _cc_head(p), "parent": None, "kind": None, "title": "", "cwd": None,
                                 "root": p.stem, "depth": 0, "group": None, "started": None, "last": p.stat().st_mtime})
        if todo:
            _remember("claude-code", own_folders)
        job.result = _new_result("claude-code")
        _cc_run(job, cat, todo, own_folders, endpoint, job.result)


def _new_result(kind):
    return {"kind": kind, "label": LABEL.get(kind, ""), "imported": 0, "updated": 0, "continued": 0, "skipped": 0, "busy": 0,
            "failed": 0, "messages": 0, "errors": [], "project": "", "groups": 0, "nested": 0, "changed": []}


def _updated(res, cid, added, how):
    """Count what update_chat did to one chat (and list it for the summary's links)."""
    res["messages"] += added
    if how == "same":
        res["skipped"] += 1
        return
    res["updated"] += 1
    res["continued"] += how != "grown" and bool((store.Chat(cid).meta().get("imported") or {}).get("continued"))
    _changed(res, cid, how, added)


def _changed(res, cid, how, added):
    if len(res["changed"]) < 300:
        res["changed"].append({"id": cid, "how": how, "added": added})


def _busy(cid):
    from . import agent                 # a chat with a turn running is left alone: the next update gets it
    return agent.busy(cid)


def _cc_run(job, cat, todo, own_folders, endpoint, res):
    from . import groups
    todo.sort(key=lambda e: (e["depth"], e["started"] or 0))
    idx = _index()
    for e in todo:                       # a desktop session that went on in a new transcript: the chat of its earlier one
        key = f"claude-code:{e['sid']}"
        if key not in idx:
            prior = next(filter(None, (_have(idx, f"claude-code:{x.stem}") for x in e["extra"])), None)
            if prior:
                idx[key] = prior

    def work(e):
        cid = _have(idx, f"claude-code:{e['sid']}")
        return not cid or (store.Chat(cid).meta().get("imported") or {}).get("size") != e["size"]
    need = {e["sid"]: work(e) for e in todo}
    job.total = sum(e["size"] for e in todo if need[e["sid"]]) or 1
    job.phase = "Importing Claude Code sessions"
    fallback = config.get().get("endpoint") or ""
    default = None

    def project_for(cwd, e=None):
        nonlocal default
        par = _have(idx, f"claude-code:{e['parent']}") if e and e.get("parent") else None
        if par:                                   # a fork or side session goes where the session it came from is
            pm = store.Chat(par).meta()
            if pm.get("project"):
                return pm["project"]
        if own_folders and cwd and os.path.isdir(cwd):
            return str(store.add_project(cwd))
        default = default or str(store.add_project(imported_project(), "Imported"))
        return default
    # the groups this import needs, newest family first, after the user's own
    gids = {}
    want = {}
    for e in todo:
        if e.get("group"):
            want[e["group"]] = max(want.get(e["group"], 0), e.get("family_last") or e["last"])

    def ensure(key, name, parent):
        if key not in gids:
            g = groups.by_key(key)
            if not g:
                g = groups.create(name, parent=parent, project=project_for(None), key=key, first=False)
                res["groups"] += 1
            gids[key] = g["id"]
        return gids[key]
    for (key, name, pkey, pname), _ in sorted(want.items(), key=lambda kv: (OTHER[0] in kv[0], -kv[1])):   # "Other" last
        ensure(key, name, ensure(pkey, pname, None) if pkey else None)
    before = 0
    for e in todo:
        if job.cancel.is_set():
            break
        key = f"claude-code:{e['sid']}"
        size = e["size"] if need[e["sid"]] else 0

        def prog(n, _b=before, _s=size):
            job.done = min(job.done + n, _b + _s)
        try:
            cid = _have(idx, key)
            if not need[e["sid"]]:
                res["skipped"] += 1
            elif cid and _busy(cid):
                res["busy"] += 1
            else:
                job.phase = f"Reading {e['title'] or 'a Claude Code session'}"
                c = claude_code(e["path"], prog, e["extra"])
                if e.get("title"):
                    c["title"] = e["title"]
                if cid:
                    _updated(res, cid, *update_chat(cid, c))
                elif not c["turns"] and not c.get("fork") and not e["desk"]:
                    res["skipped"] += 1
                else:
                    project = project_for(c.get("cwd") or e.get("cwd"), e)
                    ep = endpoint or source_endpoint("claude-code", session_model(e["path"])) or fallback
                    cid, nmsg = save_chat(c, project, ep, extra={"title_auto": store.clean_title(c["title"]) if c["title"] else None})
                    idx[key] = cid
                    for x in e["extra"]:
                        idx[f"claude-code:{x.stem}"] = cid
                    _save_index(idx)
                    res["imported"] += 1
                    res["messages"] += nmsg
                    res["project"] = res["project"] or project
                    _changed(res, cid, "new", nmsg)
            if cid:
                _place(cid, e, gids.get(e["group"][0]) if e.get("group") else None)
        except Exception as ex:
            res["failed"] += 1
            if len(res["errors"]) < 20:
                res["errors"].append(f"{e['title'] or e['sid']}: {ex.__class__.__name__}: {ex}")
        before += size
        job.done = before
    res["nested"] = _relink(cat)
    res["project"] = res["project"] or (default or "")
    _save_index(idx)
    try:
        from . import sections
        for ch in res.get("changed") or []:
            if ch.get("how") == "new":
                sections.assign_new_chat(ch["id"])
    except Exception:
        pass


def _codex_run(job, files, own_folders, res, endpoint=None, update=True):
    """Codex sessions: a new one is imported, one already here that grew gets its new messages (update_chat's rule)."""
    idx = _index()
    todo = []
    for f in files:
        size = os.path.getsize(f) if os.path.exists(f) else 0
        cid = _have(idx, f"codex:{_codex_id(Path(f))}")
        if cid and (store.Chat(cid).meta().get("imported") or {}).get("size") == size:
            res["skipped"] += 1
        else:
            todo.append((f, size))
    if update:
        job.total += sum(s for _, s in todo)
        job.phase = "Reading Codex sessions"
    ep = endpoint or source_endpoint("codex") or config.get().get("endpoint") or ""
    default = None
    for f, size in todo:
        if job.cancel.is_set():
            break
        base = job.done
        try:
            c = codex(f, lambda n, _b=base, _s=size: setattr(job, "done", min(job.done + n, _b + _s)))
            key = f"codex:{c['sid']}"
            cid = _have(idx, key)
            if cid and _busy(cid):
                res["busy"] += 1
            elif cid:
                _updated(res, cid, *update_chat(cid, c))
            elif c.get("subagent") or (key in idx and update) or not any(m["role"] in ("user", "assistant") for m in c["turns"]):
                res["skipped"] += 1
            else:
                if own_folders and c.get("cwd") and os.path.isdir(c["cwd"]):
                    project = str(store.add_project(c["cwd"]))
                else:
                    default = default or str(store.add_project(imported_project(), "Imported"))
                    project = default
                cid, nmsg = save_chat(c, project, ep)
                idx[key] = cid
                _save_index(idx)
                res["imported"] += 1
                res["messages"] += nmsg
                res["project"] = res["project"] or project
                _changed(res, cid, "new", nmsg)
                try:
                    from . import sections
                    sections.assign_new_chat(cid)
                except Exception:
                    pass
        except Exception as e:
            res["failed"] += 1
            if len(res["errors"]) < 20:
                res["errors"].append(f"{Path(f).name}: {e.__class__.__name__}: {e}")
        finally:
            job.done = base + size


# ------------------------------------------------------------------------------------------------ updating imported chats
_ulock = threading.Lock()
_last_update = None                 # the update running now, or the last one


def _scope_chats(scope, target):
    """The chats an update of one group (with its subgroups) or one chat looks at; None: all of them. Also the groups."""
    if scope == "chat":
        return {target}, set()
    if scope == "group":
        from . import groups
        gs = groups.all_groups()
        want, todo = {target}, [target]
        while todo:
            cur = todo.pop()
            for g in gs:
                if g.get("parent") == cur and g["id"] not in want:
                    want.add(g["id"])
                    todo.append(g["id"])
        return {c["id"] for c in store.list_chats() if c.get("group") in want}, {g.get("key") for g in gs if g["id"] in want and g.get("key")}
    return None, set()


def _cc_update_todo(cat, idx, scope, chats, gkeys):
    """Which Claude Code sessions an update looks at: every imported one in scope, and the ones never imported that belong
    with them: a new fork or side session of one of them, and (a whole update, or a group made by the import) a session
    started after the first import, put where an import would put it. A chat deleted here is not brought back."""
    made = _app_sessions()
    since = _since("claude-code", idx)
    have = {sid: _have(idx, f"claude-code:{sid}") for sid in cat}
    mine = {sid for sid, cid in have.items() if cid and (chats is None or cid in chats)}
    todo = []
    for sid, e in cat.items():
        if sid in made:
            continue
        if sid in mine:
            todo.append(e)
            continue
        if f"claude-code:{sid}" in idx:
            continue                     # imported once and deleted here, or out of scope
        prior = [_have(idx, f"claude-code:{x.stem}") for x in e["extra"] if f"claude-code:{x.stem}" in idx]
        if prior:                        # a desktop session that went on in a new transcript: it updates that chat
            if any(p and (chats is None or p in chats) for p in prior):
                todo.append(e)
            continue
        if not (e["head"]["first"] or e["desk"]):
            continue
        kin = e["parent"] in mine or (scope != "chat" and e["root"] in mine)
        started = (e["desk"] or {}).get("created") or e["started"] or 0
        fresh = bool(since) and started >= since and scope != "chat"
        if fresh and scope == "group":
            fresh = bool(e.get("group")) and (e["group"][0] in gkeys or e["group"][2] in gkeys)
        if kin or fresh:
            todo.append(e)
    return todo


def _codex_update_todo(idx, chats):
    since = _since("codex", idx)
    files = {}
    for p in local_files("codex"):
        try:
            st = p.stat()
        except OSError:
            continue
        sid = _codex_id(p)
        if sid not in files or st.st_size > files[sid][1]:
            files[sid] = (p, st.st_size, st.st_mtime)
    out = []
    for sid, (p, size, mtime) in files.items():
        key = f"codex:{sid}"
        cid = _have(idx, key)
        if cid:
            if chats is None or cid in chats:
                out.append(str(p))
        elif key not in idx and chats is None and since and mtime >= since:
            info = _codex_head(p)
            if info["first"] and not info["subagent"] and (info["started"] or mtime) >= since:
                out.append(str(p))
    return out


def update_imports(scope="all", target=None, auto=False):
    """One click: bring the imported Claude Code and Codex chats (all, one group's, or one) up to date with their sessions
    on this computer, and import the sessions that belong with them. A background job, one at a time."""
    global _last_update
    if scope not in ("all", "group", "chat"):
        raise ValueError("scope must be all, group or chat")
    if scope != "all" and not target:
        raise ValueError("which group or chat?")
    with _ulock:
        if _last_update and _last_update.state == "running":
            return _last_update
        _last_update = _run("update", _update, scope, target, auto)
        return _last_update


def _update(job, scope, target, auto):
    res = job.result = {**_new_result("update"), "scope": scope, "target": target, "auto": auto, "sources": []}
    job.phase = "Waiting for another import to finish" if _work.locked() else "Looking for sessions"
    with _work:
        job.phase = "Looking for sessions"
        idx = _index()
        chats, gkeys = _scope_chats(scope, target)
        for src in ("claude-code", "codex"):
            keys = [k for k in idx if k.startswith(src + ":")]
            if job.cancel.is_set() or not keys or (chats is not None and not any(idx[k] in chats for k in keys)):
                continue
            res["sources"].append(src)
            own = (_sources().get(src) or {}).get("own_folders", False)
            if src == "claude-code":
                cat = cc_catalog()
                sub = _new_result(src)
                _cc_run(job, cat, _cc_update_todo(cat, idx, scope, chats, gkeys), own, None, sub)
                for k in ("imported", "updated", "continued", "skipped", "busy", "failed", "messages", "groups", "nested"):
                    res[k] += sub[k]
                res["errors"] += sub["errors"]
                res["changed"] += sub["changed"]
                res["project"] = res["project"] or sub["project"]
                job.done = job.total = 0
            else:
                _codex_run(job, _codex_update_todo(idx, chats), own, res)
            _record_sync(src)
        from . import sections
        for c in res["changed"]:             # titles, projects and automatic section assignment for new imports
            try:
                if c.get("kind") == "new":
                    sections.assign_new_chat(c["id"])
                m = store.Chat(c["id"]).meta()
                c.update(title=m.get("title") or "", project=m.get("project") or "",
                         continued=bool((m.get("imported") or {}).get("continued")))
            except Exception:
                pass
        job.total = job.done = job.total or 1
        job.phase = "Done"


AUTO = ("off", "5min", "start", "hourly", "daily")
DAY = 24 * 3600


def auto_mode():
    m = (config.get().get("chat_import") or {}).get("auto") or "off"
    return m if m in AUTO else "off"


def _auto_stamp():
    return config.DATA / "imports" / "auto.json"


def _last_auto():
    try:
        return float(json.loads(_auto_stamp().read_text(encoding="utf-8")).get("last") or 0)
    except (OSError, ValueError):
        return 0.0


def auto_later(delay=20):
    """Settings > Import > update imported chats by itself: every 5 minutes ("5min"), once after startup ("start"),
    every hour ("hourly"), or once a day ("daily"). Runs in the background, one update at a time."""
    def loop():
        time.sleep(delay)
        first = True
        while True:
            mode = auto_mode()
            due = (mode in ("5min", "hourly") or (first and mode == "start")
                   or (mode == "daily" and time.time() - _last_auto() >= DAY))
            if due:
                try:
                    if any(k.startswith(("claude-code:", "codex:")) for k in _index()):
                        update_imports(auto=True)
                        _auto_stamp().parent.mkdir(parents=True, exist_ok=True)
                        _auto_stamp().write_text(json.dumps({"last": time.time()}), encoding="utf-8")
                except Exception:
                    pass
            first = False
            time.sleep(300 if mode == "5min" else 3600)
    threading.Thread(target=loop, daemon=True, name="chat-import-auto").start()


def update_status():
    """For the chat list: how many imported Claude Code / Codex chats there are, the update running or last run, the
    automatic mode, and when each live source was last checked."""
    n = sum(1 for c in store.list_chats() if c.get("source") in (LABEL["claude-code"], LABEL["codex"]))
    j = _last_update
    return {"imported": n, "auto": auto_mode(), "sources_synced": last_sync_map(), "job": j.public() if j else None}


def _place(cid, e, gid):
    """Title and group as the import sees them, unless the user changed them here (renamed, or moved it)."""
    chat = store.Chat(cid)
    m = chat.meta()
    changed = False
    title = store.clean_title(e["title"]) if e.get("title") else None
    if title and m.get("title") != title and m.get("title") == (m.get("title_auto") or m.get("title")):
        m["title"] = m["title_auto"] = title
        changed = True
    if m.get("group") == m.get("group_auto") and (m.get("group") != gid or m.get("group_auto") != gid):
        m["group"] = m["group_auto"] = gid
        changed = True
    if changed:
        chat.save_meta(m)


def _relink(cat):
    """Point each imported Claude Code chat at the chat it came from (now that both are here) and say how
    (meta.parent for the list's nesting, meta.branch for the fork line). Returns how many are nested."""
    idx = _index()
    n = 0
    for sid, e in cat.items():
        cid = _have(idx, f"claude-code:{sid}")
        if not cid:
            continue
        chat = store.Chat(cid)
        m = chat.meta()
        imp = m.get("imported") or {}
        par = _have(idx, f"claude-code:{e['parent']}") if e.get("parent") else None
        branch = None
        if e.get("parent"):
            src = (e["desk"] or {}).get("fork_cli") or imp.get("fork_from") or e["parent"]
            src = src if src in cat else e["parent"]
            branch = {"kind": e.get("kind") or "fork", "by": cat[e["parent"]]["title"] or None, "of": cat[src]["title"] or None,
                      "of_chat": _have(idx, f"claude-code:{src}"), "at": imp.get("fork_at")}
        n += bool(par)
        if m.get("parent") != par or m.get("branch") != branch:
            m["parent"], m["branch"] = par, branch
            chat.save_meta(m)
    return n


# ------------------------------------------------------------------------------------------------ uploads (a file dropped on the page)
def receive_upload(rfile, length, name):
    if length <= 0:
        raise ValueError("empty file")
    if length > UPLOAD_MAX:
        raise ValueError("too large to upload: give its path on disk instead")
    ext = ".zip" if str(name).lower().endswith(".zip") else ".json"
    UPLOADS.mkdir(parents=True, exist_ok=True)
    for old in UPLOADS.iterdir():                    # leftovers from an import that never ran
        try:
            if time.time() - old.stat().st_mtime > 86400:
                old.unlink()
        except OSError:
            pass
    dest = UPLOADS / f"{secrets.token_hex(8)}{ext}"
    left = length
    with open(dest, "wb") as f:
        while left > 0:
            chunk = rfile.read(min(left, 1 << 20))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left:
        dest.unlink()
        raise ValueError("the upload was cut off")
    return dest


def _forget_upload(path):
    try:
        p = Path(path).resolve()
        if p.parent == UPLOADS.resolve():
            p.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------------------------------------ for the CLI engines
# A model joining mid-chat gets the newest text up to HANDOVER_CHARS (~20k tokens, once; later turns read it from its
# cache). One message may fill most of it: at 8,000 a pasted 44k-character log lost its middle and Neon could not
# answer a question about it (2026-11-19 test).
HANDOVER_CHARS = 80_000
HANDOVER_MSG = 48_000


def _endpoint_kind_map():
    """Map endpoint id -> engine kind from current config."""
    return {e["id"]: (e.get("kind") or "") for e in (config.get().get("endpoints") or []) if e.get("id")}


def _msg_engine_kind(m, ep_kinds=None):
    """Best-effort CLI/API engine kind for an assistant message, or None if unknown."""
    if not isinstance(m, dict) or m.get("role") != "assistant":
        return None
    ep = str(m.get("endpoint") or "").strip()
    if ep:
        kinds = ep_kinds if ep_kinds is not None else _endpoint_kind_map()
        if ep in kinds and kinds[ep]:
            return kinds[ep]
        low = ep.lower()
        if low in ("neon", "agy") or low.startswith(("neon", "agy", "gemini")):
            return "agy"
        if low in ("claude", "sonnet", "opus", "haiku") or "claude" in low:
            return "claude-code"
        if low in ("codex", "chatgpt") or "codex" in low:
            return "codex"
        return "other"
    return None


def _last_own_assistant_id(msgs, engine_key, ep_kinds):
    """Return the id of the most recent assistant message written by `engine_key`, or None."""
    for m in reversed(msgs):
        if m.get("role") == "assistant" and m.get("id") and _msg_engine_kind(m, ep_kinds) == engine_key:
            return m["id"]
    return None


def prior_transcript_info(chat, limit=HANDOVER_CHARS, after_id=None, exclude_engine=None):
    """Return (text, is_summary) of earlier messages in `chat` for a CLI engine starting or catching up after another model.
    If `after_id` is given, only messages after that id are included (for switching back to a CLI that ran earlier).
    If `exclude_engine` is given with `after_id`, assistant messages already produced by that engine are skipped.
    `is_summary` is True if a compaction summary was used or any message/history was trimmed."""
    summary, msgs = chat.context_messages()
    if after_id:
        idx = next((i for i, m in enumerate(msgs) if m.get("id") == after_id), None)
        if idx is not None:
            msgs = msgs[idx + 1:]
            summary = None
    msgs = [m for m in msgs if m.get("role") in ("user", "assistant")]
    if msgs and msgs[-1].get("role") == "user":
        msgs = msgs[:-1]
    if after_id and exclude_engine:
        ep_kinds = _endpoint_kind_map()
        msgs = [m for m in msgs if not (m.get("role") == "assistant" and _msg_engine_kind(m, ep_kinds) == exclude_engine)]
    if not msgs and not summary:
        return "", False
    ep_names = {e["id"]: e.get("name") or e["id"] for e in config.get().get("endpoints") or []}

    def who_of(m):
        if m["role"] == "user":
            return "User"
        ml = m.get("model_label") or ep_names.get(m.get("endpoint") or "")
        return f"Assistant ({ml})" if ml else "Assistant"

    # Each message as a line; a model's tool steps (files it edited, commands it ran) as one short note, so the next
    # model knows what changed on disk and not only what was said. Steps in a row by the same model share one note.
    lines = []
    for m in msgs:
        t = (m.get("model_content") or m.get("content") or "").strip()
        if t:
            lines.append([who_of(m), _cut_model(t, HANDOVER_MSG), len(t) > HANDOVER_MSG])
        steps = [_step_note(c) for c in m.get("tool_calls") or []]
        if steps:
            who = who_of(m)
            if lines and lines[-1][0] == who + " steps":
                lines[-1][1] += "; " + "; ".join(steps)
            else:
                lines.append([who + " steps", "; ".join(steps), False])
    for ln in lines:
        if ln[0].endswith(" steps") and len(ln[1]) > 600:
            ln[1] = ln[1][:600] + " ..."
    out, n, trimmed = [], 0, bool(summary)
    for who, t, cut in reversed(lines):
        line = f"{who}: {t}"
        trimmed = trimmed or cut
        if n + len(line) > limit and out:
            trimmed = True
            break
        out.append(line)
        n += len(line)
    if trimmed and not summary and out:
        first = next((f"{w}: {t}" for w, t, _ in lines if w == "User"), None)     # the goal: the chat's first request
        if first and first not in out:
            out.append(first[:4000] + "\n\n(... earlier messages left out ...)")
    if not out and not summary:
        return "", False
    text = "\n\n".join(reversed(out))
    if summary:
        text = summary["summary"][:20_000] + ("\n\n" + text if text else "")
    tag = "while_another_model_replied" if after_id else "earlier_in_this_chat"
    return f"<{tag}>\n{text}\n</{tag}>\n\n", trimmed


def _step_note(call):
    """One tool call in a few words: "Edit app.py", "ran `npm test`", "Read notes.md"."""
    name = str(call.get("name") or "tool")
    try:
        a = json.loads(call.get("arguments") or "{}")
    except ValueError:
        a = {}
    a = a if isinstance(a, dict) else {}
    short = name.split("__")[-1]
    if a.get("command"):
        cmd = str(a["command"]).strip().splitlines()[0] if str(a["command"]).strip() else ""
        return f"ran `{cmd[:120]}`"
    for k in ("file_path", "path", "pattern", "url", "query", "description"):
        if a.get(k):
            return f"{short} {str(a[k])[:120]}"
    return short


def prior_transcript(chat, limit=HANDOVER_CHARS, after_id=None):
    """The chat so far as text, for a Claude Code, Codex or agy session starting mid-chat or catching up after a model switch."""
    return prior_transcript_info(chat, limit=limit, after_id=after_id)[0]


def handover_prefix(chat, engine_key, has_session, ep_name, emit):
    """Return prior context text for a CLI engine starting mid-chat or resuming after another model spoke in between.
    Posts a plain notice when the model is receiving a summary or trimmed transcript rather than every word."""
    meta = chat.meta()
    cursor = (meta.get("engine_cursor") or {}).get(engine_key) if has_session else None
    ep_kinds = _endpoint_kind_map()
    if has_session:
        _, ctx_msgs = chat.context_messages()
        if not cursor:
            # Session exists (e.g. older chat or restart) without a stored cursor: recover from this engine's last
            # own assistant message so any intervening other-model turns are still handed over.
            cursor = _last_own_assistant_id(ctx_msgs, engine_key, ep_kinds)
        else:
            # Self-heal a poisoned cursor that points to an assistant message written by a different engine.
            cur_idx = next((i for i, m in enumerate(ctx_msgs) if m.get("id") == cursor), None)
            if cur_idx is not None:
                cur_msg = ctx_msgs[cur_idx]
                cur_kind = _msg_engine_kind(cur_msg, ep_kinds)
                if cur_msg.get("role") == "assistant" and cur_kind and cur_kind != engine_key:
                    cursor = _last_own_assistant_id(ctx_msgs[:cur_idx], engine_key, ep_kinds)
    prior, is_summary = prior_transcript_info(chat, after_id=cursor, exclude_engine=(engine_key if cursor else None))
    if prior and emit:
        # Say what the model was handed and roughly what it costs (about 4 characters a token), every time: a switch
        # is the one moment a turn reads a lot without the cache.
        k = max(1, round(len(prior) / 4 / 1000))
        what = ("what was said while it was away" if cursor else "the chat so far")
        how = "a condensed handover (long messages and the oldest part shortened)" if is_summary else "every message as text, tool steps as short notes"
        nm = chat.append({"role": "notice", "content": f"Switched to {ep_name}: handed it {what}, {how}, about {k}k tokens."})
        emit(chat.id, {"type": "message", "message": nm})
    return prior


def mark_engine_cursor(chat, engine_key, msg_id=None, only_if_own=False, only_if_contiguous=False):
    """Record the latest message id seen by this CLI engine so later switches back catch only missed messages.
    Never advances past another engine's assistant messages. With `only_if_own`, stamps only when the latest assistant
    message belongs to `engine_key`. With `only_if_contiguous`, advances only when no user or other-engine assistant
    message occurred since the engine's previous cursor (safe for background ACP wakes)."""
    msgs = chat.messages()
    ep_kinds = _endpoint_kind_map()
    cur = dict(chat.meta().get("engine_cursor") or {})
    prev_id = cur.get(engine_key)

    if only_if_own:
        last_asst = next((m for m in reversed(msgs) if m.get("role") == "assistant" and m.get("id")), None)
        if not last_asst or _msg_engine_kind(last_asst, ep_kinds) != engine_key:
            return
        last_id = last_asst["id"]
    elif msg_id:
        last_id = msg_id
    else:
        last_id = None
        for m in reversed(msgs):
            if not m.get("id"):
                continue
            role = m.get("role")
            if role == "assistant":
                mk = _msg_engine_kind(m, ep_kinds)
                if mk and mk != engine_key:
                    continue
                last_id = m["id"]
                break
            if role == "user":
                last_id = m["id"]
                break

    if not last_id:
        return

    if only_if_contiguous:
        target_idx = next((i for i, m in enumerate(msgs) if m.get("id") == last_id), None)
        if target_idx is None:
            return
        prev_idx = next((i for i, m in enumerate(msgs) if m.get("id") == prev_id), None) if prev_id else None
        if prev_idx is None:
            # Without a prior cursor, only advance if no foreign assistant message exists before this wake.
            window = msgs[:target_idx]
            if any(m.get("role") == "assistant" and _msg_engine_kind(m, ep_kinds) not in (None, engine_key) for m in window):
                return
        elif target_idx > prev_idx:
            window = msgs[prev_idx + 1:target_idx + 1]
            for m in window:
                role = m.get("role")
                if role == "user":
                    return
                if role == "assistant" and _msg_engine_kind(m, ep_kinds) not in (None, engine_key):
                    return

    if cur.get(engine_key) != last_id:
        cur[engine_key] = last_id
        chat.update_meta(engine_cursor=cur)


# ------------------------------------------------------------------------------------------------ HTTP (called from server.py)
def http_get(h, path, q):
    if path == "/api/chat-import/job":
        return h._json(job(q.get("id")).public())
    if path == "/api/chat-import/status":
        return h._json(update_status())
    if path == "/api/chat-import/local":
        src = q.get("source") or ""
        if src not in ("claude-code", "codex"):
            raise ValueError("source must be claude-code or codex")
        return h._json({"sessions": local_sessions(src), "roots": [str(r) for r in local_roots(src)]})
    h._json({"error": "unknown"}, 404)


def http_post(h, path, q):
    if path == "/api/chat-import/upload":           # the body is the file itself, streamed to disk
        name = urllib.parse.unquote(h.headers.get("X-File-Name") or "export.zip")
        dest = receive_upload(h.rfile, int(h.headers.get("Content-Length") or 0), name)
        return h._json({"path": str(dest)})
    b = h._jbody()
    if path == "/api/chat-import/scan":
        return h._json(scan_export(_export_path(b.get("path"))).public())
    if path == "/api/chat-import/run":
        if b.get("source") in ("claude-code", "codex"):
            return h._json(import_local(b["source"], b.get("files") or [], bool(b.get("own_folders")), b.get("endpoint"),
                                        bool(b.get("all")) and b["source"] == "claude-code").public())
        return h._json(import_export(_export_path(b.get("path")), b.get("endpoint")).public())
    if path == "/api/chat-import/update":            # Update imported chats: all, one group's (+ subgroups), or one
        return h._json(update_imports(b.get("scope") or "all", b.get("id")).public())
    if path == "/api/chat-import/auto":
        mode = b.get("auto")
        if mode not in AUTO:
            raise ValueError("auto must be off, start, hourly or daily")
        config.update(lambda c: c.setdefault("chat_import", {}).__setitem__("auto", mode))
        return h._json(update_status())
    if path == "/api/chat-import/fix-models":        # put imported chats back on the model their session really used
        return h._json({"changed": fix_imported_endpoints()})
    if path == "/api/chat-import/cancel":
        job(b.get("id")).cancel.set()
        return h._json({"ok": True})
    h._json({"error": "unknown"}, 404)


def _export_path(p):
    from . import tools
    p = str(p or "").strip().strip('"')
    if not p:
        raise ValueError("no file")
    path = Path(os.path.expanduser(p))
    if not path.is_absolute():
        raise ValueError("give the full path of the file or folder")
    if tools.DENY_RE.search(str(path)):
        raise PermissionError("that is a credential store")
    if not path.exists():
        raise FileNotFoundError(f"not found: {path}")
    return path
