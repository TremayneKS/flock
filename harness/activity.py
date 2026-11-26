"""What a working chat is doing right now, so the page never shows the single word "Working" for half an hour.

Every engine's events already pass through agent.emit: a tool call starting, its result, the turn ending. The harness
keeps, per chat, the steps that have started and not finished (the command, when it started, and for a command its
output file), and the page shows the oldest one: "Bash: python bench.py · 31m" with the command's last output line
under it. A real long job (a remote GPU run, a 40-minute benchmark) then looks like what it is, and a chat that
really has nothing running shows how long since its last step.

Standard library only. Nothing here writes anything the model sees.
"""
import os
import re
import threading
import time

_lock = threading.Lock()
_act = {}                      # chat id -> {key: {"what", "since", "log", "until"}}
_last = {}                     # chat id -> time of the chat's last event of any kind (a step, a delta, a tool row)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
WHAT_CHARS = 90
LINE_CHARS = 160


def touch(cid, t=None):
    _last[cid] = t or time.time()


def start(cid, key, what, log=None, until=None, since=None, bg=False):
    """A step has started. bg: work that goes on after the turn has ended (a Neon session's background command or
    timer, which acp keeps running while the chat is free): the turn ending does not clear it."""
    with _lock:
        _act.setdefault(cid, {})[str(key)] = {"what": short(what), "since": since or time.time(), "log": log,
                                              "until": until, "bg": bool(bg)}


def done(cid, key):
    with _lock:
        steps = _act.get(cid)
        if steps:
            steps.pop(str(key), None)
            if not steps:
                _act.pop(cid, None)


def clear(cid, keep_bg=False):
    with _lock:
        if not keep_bg:
            _act.pop(cid, None)
            return
        steps = {k: v for k, v in (_act.get(cid) or {}).items() if v.get("bg")}
        if steps:
            _act[cid] = steps
        else:
            _act.pop(cid, None)


def short(text, n=WHAT_CHARS):
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def last_line(path, max_bytes=8192):
    """The last line a command wrote (its progress), cleaned of terminal codes; '' when there is none."""
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for ln in reversed(re.split(r"[\r\n]+", ANSI.sub("", tail))):
        ln = ln.strip()
        if ln:
            return short(ln, LINE_CHARS)
    return ""


def now(cid):
    """What the page shows for a working chat: the step running longest, or None when no step is running."""
    with _lock:
        steps = list((_act.get(cid) or {}).values())
    if not steps:
        return None
    s = min(steps, key=lambda x: x["since"])
    out = {"what": s["what"], "since": s["since"], "n": len(steps)}
    if all(x.get("bg") for x in steps):
        out["bg"] = True                   # only background work: the chat itself may be free
    if s.get("until"):
        out["until"] = s["until"]
    line = last_line(s.get("log"))
    if line:
        out["line"] = line
    return out


def background(exclude=()):
    """Chats that are free but still have background work running (for the list: "Background · 12m")."""
    with _lock:
        cids = [c for c, steps in _act.items() if c not in exclude and steps and all(v.get("bg") for v in steps.values())]
    return {c: a for c, a in ((c, now(c)) for c in cids) if a}


def snapshot(cids):
    """For /api/busy: what each working chat is doing, and when it last showed anything."""
    out = {}
    for cid in cids:
        a = now(cid)
        if a:
            out[cid] = a
        elif cid in _last:
            out[cid] = {"what": "", "since": _last[cid], "idle": True}
    return out


def summary(name, args):
    """A tool call in a few words: the command for Bash, the path for files, the query for searches."""
    import json
    try:
        a = json.loads(args) if isinstance(args, str) else (args or {})
    except ValueError:
        a = {}
    if not isinstance(a, dict):
        a = {}
    for k in ("toolSummary", "description", "command", "CommandLine", "cmd", "file_path", "path", "Path", "pattern",
              "query", "url", "prompt", "DurationSeconds"):
        v = a.get(k)
        if v:
            v = str(v).strip().splitlines()[0] if str(v).strip() else ""
            if k == "DurationSeconds":
                v = "%s s" % v
            return short("%s: %s" % (name, v))
    return short(name)
