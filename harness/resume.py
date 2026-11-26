"""Turns that carry on after the app restarts (an update, a crash, a restart from Settings, the PC rebooting).

While a turn runs it is written down in DATA/turns/<chat id>/turn.json, and that record is removed when the turn ends
for a reason of its own: it finished, the user pressed Stop, the model or a tool failed. A process that is killed never
gets to remove it, so when the app starts it looks for the turns its last copy on this port left running and carries
each one on in the same chat (agent.resume): the harness's own loop from the messages on disk, Claude Code, Codex and
agy in their own sessions. The model is told in one line, and the page shows that line as one quiet "The harness: ..."
line (it is an auto message).

What a turn needs to pick up cleanly, and where it is kept while the turn runs:
- turn.json: who runs it (pid, process start time, port, code build), the connection, the turn's first message, the
  messages the user sent while it worked that the model has not had yet (the steer queue), a CLI engine's process,
  and which files the turn has Read (Edit and Write refuse a file that was not Read first).
- <call id>.json: each tool call's result as soon as it is known. A step's results only reach messages.jsonl when the
  whole step is done, so a restart in the middle of a step would lose the calls that had already finished.
- <call id>.out / .exit / .run.json: a Bash command writes its output to a file (not a pipe to the app), its exit code
  when it ends, and its process. A command that was still running keeps running through the restart and is waited for;
  one that ended while the app was down is read back.

At resume, the calls of the cut step are settled (agent._settle_open_calls): finished ones get their recorded result,
calls that never started run now, read-only ones (Read, Grep, a web search ...) run again, Write runs again (same text,
same file), and anything else that was cut part way (an Edit, a phone action, the Computer, a subagent) is answered
"cut off by the restart: check whether it happened before doing it again".

Two copies of the app can share one data folder (the desktop copy and the phone copy): each resumes only the turns that
a copy on its own port left, and never one whose app is still running.
"""
import json
import os
import shutil
import time
from pathlib import Path

from . import config, osutil

DIR = config.DATA / "turns"
MAX_AGE_H = 24              # a turn whose app has been gone longer than this is not carried on by itself
MAX_RESUMES = 3             # resumes of one turn in a row with no model step in between: it is the turn that kills the app

leaving = False             # set when the app is shutting down for a restart: turns stop writing, records stay


def _dir(cid):
    return DIR / cid


def _file(cid):
    return _dir(cid) / "turn.json"


def read(cid):
    try:
        return json.loads(_file(cid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(cid, rec):
    try:
        d = _dir(cid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "turn.json.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, d / "turn.json")
    except OSError:
        pass


def build():
    """Which code this copy runs: the git commit of the checkout (read from .git, no subprocess), else VERSION."""
    try:
        g = config.APP / ".git"
        if g.is_file():                                     # a worktree: "gitdir: <path>"
            g = Path(g.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            for root in (g, Path(str(g).split(os.sep + "worktrees" + os.sep)[0])):
                f = root / ref
                if f.is_file():
                    return f.read_text(encoding="utf-8").strip()[:12]
                packed = root / "packed-refs"
                if packed.is_file():
                    for ln in packed.read_text(encoding="utf-8").splitlines():
                        if ln.endswith(" " + ref):
                            return ln.split(" ", 1)[0][:12]
            return ref
        return head[:12]
    except (OSError, IndexError):
        pass
    from . import version
    return version.version()


_server = {}


def server():
    if not _server:
        _server.update(pid=os.getpid(), created=osutil.created(os.getpid()), port=config.PORT, build=build())
    return dict(_server)


# ------------------------------------------------------------------------------------------------ while a turn runs
def begin(run, chat_id, endpoint, kind, first=None, carry=None):
    """The turn has started (or a cut one is being carried on: carry = its old record)."""
    if run.sub:
        return
    rec = dict(carry or {})
    rec.update(v=1, chat=chat_id, token=run.token, server=server(), endpoint=endpoint, kind=kind, beat=time.time())
    rec.setdefault("started", time.time())
    rec.setdefault("first", first)
    rec.setdefault("queue", [])
    rec.setdefault("resumes", 0)
    rec["engine"] = None
    _write(chat_id, rec)


def _mine(run):
    if run.sub or leaving:
        return None
    rec = read(run.chat_id)
    return rec if rec and rec.get("token") == run.token else None


def note(run, **kw):
    rec = _mine(run)
    if rec is not None:
        rec.update(kw)
        rec["beat"] = time.time()
        _write(run.chat_id, rec)


def stepped(run, state=None):
    """A model step came back: the turn is making progress (a resume that gets this far was no crash loop)."""
    reads = (state or {}).get("read") or {}
    note(run, resumes=0, read=dict(list(reads.items())[-400:]))


def save_queue(run):
    note(run, queue=[{k: q.get(k) for k in ("id", "text", "files", "reasoning", "when")} for q in list(run.queue)])


def engine(run, proc, **extra):
    """A CLI engine's process (Claude Code, Codex, agy): ended before the turn is carried on, so two never share a session.
    extra: kept with it (Claude Code's `inbox`: its input outlives the app, cli_input.py)."""
    info = {"pid": proc.pid, "created": osutil.created(proc.pid), **extra} if proc else None
    if run.sub:                                         # a subagent has no turn.json: its own small file
        if not leaving and info and call_dir(run.chat_id):
            try:
                (_dir(run.chat_id) / "engine.json").write_text(json.dumps(info), encoding="utf-8")
            except OSError:
                pass
        return
    note(run, engine=info, engine_pos=0, completed=False)


def engine_files(chat_id):
    """Where a CLI engine that can outlive the app (Codex) writes its events and its errors."""
    d = call_dir(chat_id) or _dir(chat_id)
    return d / "engine.out", d / "engine.err"


def follow(path, alive, pos=0, tick=0.05):
    """The lines a process writes to a file, as they come, until it has ended and every line is read: (line, byte
    position after it). From byte pos on (a copy taking over after a restart starts where the old one stopped)."""
    with open(path, "rb") as f:
        f.seek(pos)
        buf = b""
        while True:
            chunk = f.readline()
            if chunk:
                buf += chunk
                if buf.endswith(b"\n"):
                    pos += len(buf)
                    yield buf, pos
                    buf = b""
                continue
            if alive():
                time.sleep(tick)
                continue
            chunk = f.readline()                            # written just before it ended
            if chunk:
                buf += chunk
                if buf.endswith(b"\n"):
                    pos += len(buf)
                    yield buf, pos
                    buf = b""
                continue
            if buf:
                pos += len(buf)
                yield buf, pos
            return


class Running:
    """A process this copy did not start (an engine the last copy left running), with as much of Popen as the engines
    use. created=-1: it is known to have ended."""
    stdin = None

    def __init__(self, pid, created=None):
        self.pid, self.created = int(pid), created

    def poll(self):
        if self.created == -1 or not osutil.same_process(self.pid, self.created):
            return 0                                        # its real exit code is not ours to read
        return None

    @property
    def returncode(self):
        return self.poll()

    def wait(self, timeout=None):
        t0 = time.time()
        while self.poll() is None:
            if timeout is not None and time.time() - t0 > timeout:
                raise TimeoutError
            time.sleep(0.25)
        return 0


def tail_lines(path, n=20):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16_000))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def local_token(name):
    """The token that proves a call to the app comes from an MCP server it started (subagents, Computer). Kept in the
    key folder (fenced off from the models), so the servers a CLI engine started before a restart can still reach the
    new copy."""
    import secrets
    f = config.KEYS / f"local-{name}.token"
    try:
        t = f.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    except OSError:
        pass
    t = secrets.token_hex(16)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(t, encoding="utf-8")
    except OSError:
        pass
    return t


def end(run):
    """The turn ended by itself, by Stop, or by an error: nothing to carry on."""
    if leaving:
        return
    if run.sub:
        drop(run.chat_id)
        return
    rec = read(run.chat_id)
    if rec and rec.get("token") == run.token:
        drop(run.chat_id)


def drop(cid):
    """The record first (it is what says "carry this on"), then the rest. On Windows a command that was just killed can
    hold its output file open for a moment: that folder is swept later (sweep)."""
    try:
        _file(cid).unlink()
    except OSError:
        pass
    shutil.rmtree(_dir(cid), ignore_errors=True)


def sweep(days=1):
    """Folders with no record left over from ended turns (a file was still open when they ended)."""
    try:
        dirs = [d for d in DIR.iterdir() if d.is_dir() and not (d / "turn.json").exists()]
    except OSError:
        return
    old = time.time() - days * 86400
    for d in dirs:
        try:
            if max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.iterdir()]) < old:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def hold():
    """The app is going down for a restart: a turn that gets here writes nothing more (its record stays for the new copy)."""
    while True:
        time.sleep(3600)


# ------------------------------------------------------------------------------------------------ tool calls
def call_dir(chat_id):
    d = _dir(chat_id)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d


def call_done(chat_id, call_id, res):
    """One tool call's result, kept until the step's results are in messages.jsonl."""
    if leaving or not call_id:
        return
    try:
        keep = {k: res.get(k) for k in ("text", "ok", "secs", "ui", "image", "path") if res.get(k) is not None}
        if isinstance(keep.get("image"), Path):
            keep["image"] = str(keep["image"])
        f = _dir(chat_id) / f"{call_id}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(keep, ensure_ascii=False, default=str), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def call_result(chat_id, call_id):
    try:
        return json.loads((_dir(chat_id) / f"{call_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def call_clear(chat_id, call_id):
    """The call's result is in messages.jsonl now: its files are not needed any more."""
    if leaving:
        return
    for f in (f"{call_id}.json", *[p.name for p in bash_files(chat_id, call_id)]):
        try:
            (_dir(chat_id) / f).unlink()
        except OSError:
            pass


def bash_files(chat_id, call_id):
    """Where a Bash call keeps its output, exit code and process while it runs."""
    d = _dir(chat_id)
    return d / f"{call_id}.out", d / f"{call_id}.exit", d / f"{call_id}.run.json"


# ------------------------------------------------------------------------------------------------ at start
def orphans():
    """Records left by a copy of the app on this port that is no longer running, oldest first."""
    out = []
    try:
        dirs = list(DIR.iterdir())
    except OSError:
        return out
    me = server()
    for d in dirs:
        rec = read(d.name) if d.is_dir() else None
        if not rec or not rec.get("chat"):
            continue
        srv = rec.get("server") or {}
        if int(srv.get("port") or 0) != me["port"] or srv.get("pid") == me["pid"]:
            continue
        if osutil.same_process(srv.get("pid"), srv.get("created")):
            continue                                        # its app is still up (shutting down, or a second copy)
        out.append(rec)
    out.sort(key=lambda r: r.get("started") or 0)
    return out


def resume_text(rec):
    """The one line the model gets (and the page shows, quietly) when its turn is carried on."""
    old = (rec.get("server") or {}).get("build")
    if old and old != server()["build"]:
        return "Flock was updated and restarted while you were working. Carry on from where you stopped."
    return "Flock restarted while you were working. Carry on from where you stopped."


def too_old(rec):
    return time.time() - float(rec.get("beat") or rec.get("started") or 0) > MAX_AGE_H * 3600


def kill_engine(rec, chat_id=None):
    """End a CLI engine process the old copy left behind (it may still be writing to its session)."""
    e = rec.get("engine") or {}
    if not e and chat_id:
        try:
            e = json.loads((_dir(chat_id) / "engine.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            e = {}
    if e.get("pid") and osutil.same_process(e["pid"], e.get("created")):
        osutil.kill_tree(e["pid"])
        for _ in range(20):
            if not osutil.alive(e["pid"]):
                break
            time.sleep(0.25)
