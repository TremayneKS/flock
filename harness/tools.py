"""The agent's tools. Names and shapes follow Claude Code's (Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch,
TodoWrite, Skill), plus Browser (Playwright) and Notes (the chat's memory).
Descriptions are kept short: every word here is in every request's prefix."""
import base64
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from . import config, mcp, osutil

NOWIN = osutil.NOWIN
MAX_OUT = 30_000                 # characters of tool output the model gets (the full text goes to a file)
READ_LINES = 2000
RG = shutil.which("rg") or "rg"

# Credential stores the agent must never read, edit or send anywhere. A profile adds its own with "deny_extra".
DENY = [r"[\\/]\.ssh([\\/]|$)", r"\bid_(rsa|ed25519|ecdsa|dsa)\b", r"\.pem$", r"\.p12$", r"\.pfx$", r"\.kdbx$",
        r"[\\/]\.aws[\\/]credentials", r"[\\/]\.azure([\\/]|$)", r"[\\/]gcloud[\\/](credentials|legacy_credentials|access_tokens)",
        r"application_default_credentials\.json", r"[\\/]\.docker[\\/]config\.json", r"[\\/]\.kube[\\/]config",
        r"\.git-credentials", r"[\\/]\.netrc", r"[\\/]\.npmrc$", r"[\\/]\.pypirc$", r"rclone\.conf",
        r"[\\/]\.claude[\\/]\.credentials\.json", r"[\\/]\.codex[\\/]auth\.json", r"[\\/]\.config[\\/]gh[\\/]hosts\.yml",
        r"[\\/](Login Data|Cookies|Web Data|Local State)$", r"\.keychain-db$"]


def _deny_re():
    extra = [str(x) for x in (config.PROFILE.get("deny_extra") or [])]
    own = [re.escape(str(config.KEYS)), re.escape(str(config.KEYS).replace("\\", "/"))]   # the harness's own key folder
    return re.compile("|".join(DENY + extra + own), re.I)


DENY_RE = _deny_re()


def _p(desc, **props):
    req = [k for k, v in props.items() if v.pop("_req", False)]
    return {"type": "object", "properties": props, "required": req}, desc


def S(desc, req=False, **kw):
    d = {"type": "string", "description": desc, **kw}
    if req:
        d["_req"] = True
    return d


def I(desc, req=False):
    d = {"type": "integer", "description": desc}
    if req:
        d["_req"] = True
    return d


def B(desc):
    return {"type": "boolean", "description": desc}


SPECS = {
    "Read": _p("Read a file (numbered lines) or list a folder. Use offset/limit for long files.",
               file_path=S("Path, relative to the project folder or absolute", True),
               offset=I("First line to read (1-based)"), limit=I(f"Number of lines (default {READ_LINES})")),
    "Write": _p("Create or overwrite a file with the full content. Read an existing file first.",
                file_path=S("Path", True), content=S("The whole file", True)),
    "Edit": _p("Replace exact text in a file. old_string must match the file exactly (indentation too) and be unique, "
               "unless replace_all is true. Read the file first.",
               file_path=S("Path", True), old_string=S("Text to find", True), new_string=S("Replacement text", True),
               replace_all=B("Replace every match")),
    "Bash": _p("Run a shell command (Git Bash on Windows, or phone Linux VM/Termux when target is 'phone') in the project folder. Output is cut at 30k characters. "
               "Use background for servers, watchers and anything that takes minutes (benchmarks, long jobs); then "
               "check their log with quick commands or Read. Never wait for a job inside one command: a wait loop is "
               "stopped after 60 s.",
               command=S("The command", True), timeout=I("Seconds before it is stopped (default 120; give a long build or test run "
                                                         "as many as it needs)"),
               background=B("Start it and return at once (for dev servers)"),
               target=S('"desktop" or "phone" (default: this chat\'s run target)')),
    "Glob": _p("Find files by name pattern, newest first (respects .gitignore).",
               pattern=S('Glob such as "**/*.js" or "src/**/*.css"', True), path=S("Folder to search (default: project)")),
    "Grep": _p("Search file contents with a regular expression (ripgrep).",
               pattern=S("Regular expression", True), path=S("File or folder (default: project)"),
               glob=S('Only files matching this glob, e.g. "*.py"'),
               output_mode=S('"content" (matching lines, default), "files_with_matches" or "count"'),
               case_insensitive=B("Ignore case"), context=I("Lines of context around each match")),
    "WebSearch": _p("Search the web. Returns titles, links and snippets.",
                    query=S("What to search for", True), kind=S('"web" (default), "news" or "papers" (research papers)')),
    "WebFetch": _p("Fetch a web page as clean text.", url=S("http(s) URL", True),
                   start=I("Character offset to continue from")),
    "Browser": _p("Drive a headless browser to check pages you built. Actions: open (a url, or a file path: it is served "
                  "over http for you), snapshot (page structure with "
                  "refs), click (ref), type (ref, text, submit), press (key), console (errors and logs), eval (script: "
                  "a JS function body that returns a value), screenshot, resize (width, height), back, close.",
                  action=S("One of the actions above", True), url=S("For open"),
                  ref=S("Element ref from the latest snapshot (click, type)"), element=S("What the element is, in words"),
                  text=S("Text to type"), submit=B("Press Enter after typing"), key=S('Key for press, e.g. "Enter"'),
                  script=S("For eval, e.g. return document.title"), width=I("For resize"), height=I("For resize")),
    "TodoWrite": _p("Keep a short task list for multi-step work. Send the whole list each time.",
                    todos={"type": "array", "description": "The tasks", "_req": True,
                           "items": {"type": "object", "properties": {
                               "content": {"type": "string"},
                               "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                               "required": ["content", "status"]}}),
    "Notes": _p("This chat's memory file. Add short bullets for things worth keeping: the goal, decisions, key files, "
                "what works, open tasks. It survives when the chat is compacted.",
                add=S("Text to append"), replace=S("Replace the whole file with this")),
    "Skill": _p("Load a skill (instructions for a kind of task) by name.", name=S("Skill name", True)),
    "View": _p("Look at an image file (a screenshot, a plot, a picture the user sent) directly.",
               file_path=S("Path of the image", True)),
    "OrganizeChats": _p("List, move, archive, unarchive or clean up chats and chat groups across sections (e.g. General, Work, Personal), or undo a change.",
                        action=S('"list", "move", "archive", "unarchive", "cleanup" or "undo"', True),
                        section=S('Target section name for move (e.g. "General", "Work", "Personal")'),
                        query=S("Keyword to match chat titles, folders or group names"),
                        chats={"type": "array", "items": {"type": "string"}, "description": "Chat ids or title matches"},
                        groups={"type": "array", "items": {"type": "string"}, "description": "Group ids or names"},
                        undo_id=S("Undo id from an earlier change (optional for undo)")),
    "ParentContext": _p("Search or read the main (parent) chat's full history, notes and live updates without loading the whole transcript into context.",
                        action=S('"search" (keyword/regex across all parent messages), "latest" (newest turns), "read" (by 1-based turn index), or "notes"', True),
                        query=S("Keyword or regex for search"),
                        start=I("1-based message index for read"),
                        limit=I("Max matches or messages to return (default 10)")),
    "ReportToMain": _p("Send a concise summary of your findings back to the main (parent) chat.",
                       summary=S("Concise findings, file paths and conclusions for the main chat", True),
                       wake_main=B("True to send to the main model right now (steers if busy); false (default) to hand off quietly for its next turn")),
}

READ_ONLY = {"Read", "Glob", "Grep", "WebSearch", "WebFetch", "Skill", "View"}


def schemas(names):
    out = []
    for n in names:
        if n not in SPECS:
            # A tool this copy of the app does not have (the chat was last run by a newer copy: 21 Nov 2026, the phone
            # copy's OrganizeChats reached the desktop copy and the turn died with KeyError). Left out, never an error.
            continue
        params, desc = SPECS[n]
        out.append({"type": "function", "function": {"name": n, "description": desc, "parameters": params}})
    return out


# ------------------------------------------------------------------------------------------------ helpers
class Ctx:
    """What a tool call can see: the chat, its folder, the endpoint, a Stop flag and a way to tell the UI."""

    def __init__(self, chat, cwd, ep, emit, cancel, state):
        self.chat, self.cwd, self.ep, self.emit, self.cancel, self.state = chat, Path(cwd), ep, emit, cancel, state
        self.state.setdefault("read", {})       # path -> mtime when read or written (Edit/Write need it)
        self.state.setdefault("jobs", {})


class ToolError(Exception):
    pass


def resolve(ctx, path):
    if not path or not isinstance(path, str):
        raise ToolError("a path is needed")
    path = path.strip().strip('"').strip("'")
    m = re.match(r"^/([a-zA-Z])/(.*)$", path)            # Git Bash style /c/Users/...
    if m:
        path = f"{m.group(1).upper()}:/{m.group(2)}"
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = ctx.cwd / p
    p = Path(os.path.normpath(str(p)))
    if DENY_RE.search(str(p)):
        raise ToolError(f"{p} is a credential store; it is off limits")
    return p


def clip(text, limit=MAX_OUT, ctx=None, label="output"):
    if len(text) <= limit:
        return text
    note = ""
    if ctx is not None:
        d = ctx.chat.dir / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{label}-{int(time.time() * 1000)}.txt"
        f.write_text(text, encoding="utf-8")
        note = f" Full text: {f}"
    head, tail = text[: limit * 2 // 3], text[-limit // 3:]
    return f"{head}\n\n[... {len(text) - len(head) - len(tail):,} characters cut.{note} ...]\n\n{tail}"


def is_binary(sample):
    return b"\x00" in sample[:8192]


def read_text(p):
    data = p.read_bytes()
    if is_binary(data):
        raise ToolError(f"{p} is a binary file ({len(data):,} bytes)")
    for enc in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def numbered(lines, start):
    out = []
    for i, line in enumerate(lines, start):
        if len(line) > 2000:
            line = line[:2000] + " [line cut]"
        out.append(f"{i:>6}\t{line}")
    return "\n".join(out)


def mark_read(ctx, p):
    try:
        ctx.state["read"][os.path.normcase(str(p))] = p.stat().st_mtime
    except OSError:
        pass


def was_read(ctx, p):
    return os.path.normcase(str(p)) in ctx.state["read"]


# ------------------------------------------------------------------------------------------------ tools
def t_read(ctx, a):
    p = resolve(ctx, a.get("file_path") or a.get("path"))
    if not p.exists():
        near = difflib.get_close_matches(p.name, os.listdir(p.parent) if p.parent.is_dir() else [], n=3)
        raise ToolError(f"{p} does not exist" + (f". Similar names here: {', '.join(near)}" if near else ""))
    if p.is_dir():
        items = []
        for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))[:400]:
            if e.is_dir():
                items.append(e.name + "/")
            else:
                try:
                    items.append(f"{e.name}  ({e.stat().st_size:,} B)")
                except OSError:
                    items.append(e.name)
        return f"{p} (folder, {len(items)} entries)\n" + "\n".join(items)
    ext = p.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return {"text": f"{p} is an image ({p.stat().st_size:,} bytes): the image follows.", "image": p, "ui": {"image": str(p)}}
    text = read_text(p)
    lines = text.splitlines()
    off = max(1, int(a.get("offset") or 1))
    lim = max(1, min(int(a.get("limit") or READ_LINES), 5000))
    chunk = lines[off - 1: off - 1 + lim]
    mark_read(ctx, p)
    head = ""
    if len(lines) > len(chunk):
        head = f"[{p.name}: lines {off}-{off + len(chunk) - 1} of {len(lines)}]\n"
    if not lines:
        return f"{p} is empty."
    return clip(head + numbered(chunk, off), ctx=ctx, label="read")


def t_write(ctx, a):
    p = resolve(ctx, a.get("file_path") or a.get("path"))
    content = a.get("content")
    if content is None:
        raise ToolError("content is missing")
    if p.exists() and p.is_file() and not was_read(ctx, p):
        raise ToolError(f"{p} already exists. Read it first (then Edit, or Write the whole new version).")
    live = live_note(p)
    existed = p.exists()
    old = read_text(p) if existed and p.stat().st_size < 2_000_000 else ""
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    mark_read(ctx, p)
    n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    ctx.emit({"type": "file", "path": str(p), "change": "modified" if existed else "created"})
    return {"text": f"{'Updated' if existed else 'Created'} {p} ({n} lines).{live}", "ui": {"diff": _diff(old, content, p.name)},
            "path": str(p)}


def live_note(p):
    """A note for the tool result when p is this app's own code: it changed for real, and the user reloads it."""
    from . import checks
    hit = checks.live_for(p)
    return f"\n(This is {hit[0]}'s live code. Do not stop or restart it: the harness tells the user to reload it.)" if hit else ""


def _diff(old, new, name, limit=400):
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=name, tofile=name, lineterm="", n=2))
    return "\n".join(lines[:limit]) + ("\n…" if len(lines) > limit else "")


def _loose(s):
    return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").split("\n"))


def t_edit(ctx, a):
    p = resolve(ctx, a.get("file_path") or a.get("path"))
    old_s, new_s = a.get("old_string"), a.get("new_string")
    if old_s is None or new_s is None:
        raise ToolError("old_string and new_string are needed")
    if not p.exists():
        raise ToolError(f"{p} does not exist (use Write to create it)")
    if not was_read(ctx, p):
        raise ToolError(f"Read {p} first.")
    if old_s == new_s:
        raise ToolError("old_string and new_string are the same")
    live = live_note(p)
    raw = p.read_bytes()
    crlf = b"\r\n" in raw
    text = read_text(p)
    work = text.replace("\r\n", "\n")
    o, n = old_s.replace("\r\n", "\n"), new_s.replace("\r\n", "\n")
    count = work.count(o) if o else 0
    if count == 0 and o:
        # forgive trailing spaces on lines, which small models often drop
        lw, lo = _loose(work), _loose(o)
        if lo and lw.count(lo) >= 1:
            work, o = lw, lo
            count = work.count(o)
    if count == 0:
        raise ToolError("old_string was not found." + _closest(work, o))
    if count > 1 and not a.get("replace_all"):
        raise ToolError(f"old_string appears {count} times. Add more surrounding lines to make it unique, or set replace_all.")
    pos = work.find(o)
    out = work.replace(o, n) if a.get("replace_all") else work.replace(o, n, 1)
    if crlf:
        out = out.replace("\n", "\r\n")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    mark_read(ctx, p)
    first = work[:pos].count("\n") + 1
    new_lines = out.replace("\r\n", "\n").split("\n")
    lo, hi = max(1, first - 2), min(len(new_lines), first + n.count("\n") + 2)
    ctx.emit({"type": "file", "path": str(p), "change": "modified"})
    snippet = numbered(new_lines[lo - 1: hi], lo)
    reps = f" ({count} places)" if a.get("replace_all") and count > 1 else ""
    return {"text": f"Edited {p}{reps}. Lines {lo}-{hi} now:\n{snippet}{live}",
            "ui": {"diff": _diff(text.replace('\r\n', '\n'), out.replace('\r\n', '\n'), p.name)}, "path": str(p)}


def _closest(text, needle):
    lines = text.split("\n")
    want = needle.strip("\n").split("\n")
    if not want or not lines:
        return ""
    best, at = 0.0, 0
    first = want[0].strip()
    for i in range(len(lines)):
        if first and difflib.SequenceMatcher(None, lines[i].strip(), first).quick_ratio() < 0.5:
            continue
        r = difflib.SequenceMatcher(None, "\n".join(lines[i:i + len(want)]), needle).ratio()
        if r > best:
            best, at = r, i
    if best < 0.5:
        return " Read the file again to see its current text."
    return f" The closest text is at lines {at + 1}-{at + len(want)}:\n" + numbered(lines[at:at + len(want)], at + 1)


def _env():
    env = dict(os.environ)
    extra = [str(config.expand(x)) for x in (config.get().get("path_extra") or [])]
    extra.append(os.path.dirname(RG) if os.path.isabs(RG) else "")
    env["PATH"] = os.pathsep.join([x for x in extra if x] + [env.get("PATH", "")])
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "TERM": "dumb", "NO_COLOR": "1", "FORCE_COLOR": "0",
                "GIT_PAGER": "cat", "PAGER": "cat", "CI": "1", "MSYS_NO_PATHCONV": "0"})
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def kill_tree(pid):
    osutil.kill_tree(pid)


def shell():
    sh = osutil.bash()
    if not sh:
        raise ToolError("no bash found: on Windows install Git for Windows (git-scm.com), or set KCH_BASH to a bash.exe")
    return sh


WIN_PATH = re.compile(r"[A-Za-z]:\\(?:[^\s'\"\\;|&<>()`$]|\\(?=[^\s'\"]))*")


def fix_windows_paths(cmd):
    """Unquoted C:\\a\\b in a bash command loses its backslashes (bash escapes), so models' Windows paths break:
    turn them into C:/a/b. Text inside quotes is left alone (there the backslashes already survive)."""
    out, i, quote = [], 0, None
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            out.append(ch)
            if ch == quote and (quote == "'" or cmd[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
        elif ch.isalpha() and (i == 0 or not (cmd[i - 1].isalnum() or cmd[i - 1] in "/\\_.-")):
            m = WIN_PATH.match(cmd, i)
            if m:
                out.append(m.group(0).replace("\\", "/"))
                i = m.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# Commands that would end the app itself or its model server's launcher (which may hold a paid remote GPU): every
# Python process by name, those two by process number, or the profile's own "protect_commands" (regexes).
_KILL_ALL = re.compile(
    r"\btaskkill\b[^;&|\n]*?[/-]{1,2}im\s+[\"']?(?:python\w*|py)\b"
    r"|\btaskkill\b[^;&|\n]*imagename\s+eq\s+(?:python|py\b)"
    r"|\b(?:stop-process|spps)\b[^;&|\n]*?-name\s+[\"']?(?:python|py\b)"
    r"|\b(?:get-process|gps|ps)\s+[\"']?(?:-name\s+)?python[^|;\n]*\|\s*(?:stop-process|spps|kill)\b"
    r"|\b(?:pkill|killall)\b[^;&|\n]*\b(?:python|pythonw|run\.py)"
    r"|\bwmic\b[^;&|\n]*python[^;&|\n]*\b(?:delete|terminate)\b"
    r"|\bkill\s+(?:-\w+\s+)*-1\b", re.I)
_KILL_VERB = re.compile(r"\b(?:taskkill|tskill|kill|pkill|stop-process|spps|wmic|killpg|terminate)\b", re.I)


def kill_guard(cmd):
    """Why this command must not run, or None."""
    from .runtime import runtime
    hit = bool(_KILL_ALL.search(cmd))
    if not hit and _KILL_VERB.search(cmd):
        mine = {str(os.getpid()), str(runtime.info.get("pid") or "")} - {""}
        hit = any(n in mine for n in re.findall(r"\b\d{2,7}\b", cmd))
    for pat in config.get().get("protect_commands") or []:
        try:
            hit = hit or bool(re.search(pat, cmd, re.I))
        except re.error:
            pass
    if hit:
        return ("refused: this would stop this app (Flock), the launcher of its model server, or every Python process "
                "(which includes both). Never stop, kill or restart the app or the model server. If that seems needed, "
                "say so and ask the user to do it.")
    return None


def t_bash(ctx, a):
    cmd = a.get("command")
    if not cmd or not isinstance(cmd, str):
        raise ToolError("command is missing")
    if osutil.WINDOWS:
        cmd = fix_windows_paths(cmd)
    if DENY_RE.search(cmd):
        raise ToolError("that command touches a credential store; it is off limits")
    why = kill_guard(cmd)
    if why:
        raise ToolError(why)
    from .computer import gui_automation_refusal       # mouse, keyboard and screen go through the Computer tool
    gui = gui_automation_refusal(cmd, ctx)
    if gui:
        raise ToolError(gui)
    ctx.cwd.mkdir(parents=True, exist_ok=True)
    if a.get("background"):
        jobs = ctx.state["jobs"]
        d = ctx.chat.dir / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        # numbered after the chat's existing logs: after a restart the job list starts empty, and job1.log may still be
        # the log of a job that kept running through it
        nums = [int(m[1]) for p in d.glob("job*.log") if (m := re.fullmatch(r"job(\d+)\.log", p.name))]
        n = max([len(jobs), *nums]) + 1
        log = d / f"job{n}.log"
        f = open(log, "w", encoding="utf-8")
        proc = subprocess.Popen([shell(), "-c", cmd], cwd=str(ctx.cwd), env=_env(), stdin=subprocess.DEVNULL,
                                stdout=f, stderr=subprocess.STDOUT, **osutil.group())
        jobs[n] = {"pid": proc.pid, "cmd": cmd, "log": str(log), "proc": proc}
        _all_jobs.append(proc)
        time.sleep(2.0)
        early = log.read_text(encoding="utf-8", errors="replace")[-3000:]
        state = f"exited with code {proc.returncode}" if proc.poll() is not None else "running"
        return (f"Job {n} {state} (pid {proc.pid}). Log: {log}\nFirst output:\n{early or '(none yet)'}\n"
                + (f"Stop it with: taskkill //F //T //PID {proc.pid}" if osutil.WINDOWS else f"Stop it with: kill -- -{proc.pid}"))
    timeout = max(1, int(a.get("timeout") or 120))      # no ceiling: the model sets what a long run needs
    waits = bool(WAIT_LOOP.search(cmd))
    if waits:                        # a loop that sleeps until something happens: a quick wait is fine, minutes are not
        timeout = min(timeout, WAIT_LOOP_MAX)
    t0 = time.time()
    files = _bash_files(ctx)
    if files:
        # A call of a turn: output to a file and the exit code to another (not a pipe to this process), and the process
        # written down, so the command keeps running if the app restarts and the new copy can wait for it (bash_resume).
        out_f, exit_f, run_f = files
        env = _env()
        env["KCH_EXIT_FILE"] = str(exit_f).replace("\\", "/")
        with open(out_f, "wb") as fh:
            proc = subprocess.Popen([shell(), "-c", "trap 'echo $? > \"$KCH_EXIT_FILE\"' EXIT; " + cmd], cwd=str(ctx.cwd),
                                    env=env, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, **osutil.group())
        try:
            run_f.write_text(json.dumps({"pid": proc.pid, "created": osutil.created(proc.pid), "t0": t0, "timeout": timeout,
                                         "waits": waits}), encoding="utf-8")
        except OSError:
            pass
        why, steered = _wait_cmd(ctx, lambda: proc.poll() is None, lambda: kill_tree(proc.pid), t0, timeout, waits)
        proc.wait()
        out = _read_out(out_f)
    else:                            # outside a turn (the gate's test run): a plain pipe
        proc = subprocess.Popen([shell(), "-c", cmd], cwd=str(ctx.cwd), env=_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **osutil.group())
        chunks = []

        def pump():
            for b in iter(lambda: proc.stdout.read(4096), b""):
                chunks.append(b)
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        why, steered = _wait_cmd(ctx, lambda: proc.poll() is None, lambda: kill_tree(proc.pid), t0, timeout, waits)
        th.join(1 if why else 3)     # a killed tree can keep the pipe open on Windows: do not wait long for its last bytes
        out = b"".join(chunks)
    return _bash_result(ctx, out, why, steered, proc.poll(), t0)


def _bash_files(ctx):
    """Where this call's command keeps its output, exit code and process (resume.py), or None outside a turn's call."""
    call = getattr(ctx, "call_id", None)
    if not call or not getattr(ctx, "chat", None):
        return None
    from . import resume
    return resume.bash_files(ctx.chat.id, call) if resume.call_dir(ctx.chat.id) else None


def _read_out(f, limit=8 * 1024 * 1024):
    try:
        with open(f, "rb") as fh:
            data = fh.read(limit + 1)
    except OSError:
        return b""
    return data[:limit] + (b"\n[... output cut at 8 MB]" if len(data) > limit else b"")


def _wait_cmd(ctx, running, kill, t0, timeout, waits, tick=0.05):
    """Wait for a command: Stop, a message from the user (steer) and the time limit cut it short. Returns (why, steered)."""
    why = None
    steer = getattr(ctx, "steer", None)
    steered = False
    while running():
        if ctx.cancel.is_set():
            why = "stopped by the user"
            break
        if steer and steer(t0):
            steered = True
            why = (f"stopped after {time.time() - t0:.0f} s because the user sent a message (it follows). If this "
                   "command still needs to run, start it with background: true and check on it with quick commands")
            break
        if time.time() - t0 > timeout:
            why = (f"timed out after {timeout} s. " + ("A loop that waits for something gets at most "
                   f"{WAIT_LOOP_MAX} s. " if waits else "") + "For anything long, run it with background: true, then "
                   "check its log with quick commands and tell the user what you are waiting for")
            break
        time.sleep(tick)
    if why:
        kill()
    return why, steered


def _bash_result(ctx, out, why, steered, code, t0, note=None):
    out = out.decode("utf-8", "replace").replace("\r\n", "\n")
    out = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", out)
    tail = f"\n[{why}]" if why else f"\n[{note}]" if note else ("" if code == 0 else f"\n[exit code {code}]")
    took = time.time() - t0
    res = {"text": clip(out.strip() + tail, ctx=ctx, label="bash") or "(no output)", "ui": {"exit": code, "secs": round(took, 1)}}
    if steered:
        res["steered"] = True
    return res


def bash_resume(ctx, call_id):
    """A Bash call the app restarted in the middle of (resume.py). The command ran with its output going to a file, so
    it kept running: wait for it as if nothing happened (Stop, a message from the user and its time limit still apply),
    then answer with its output and exit code. None when it never started (the caller runs it now)."""
    from . import resume
    out_f, exit_f, run_f = resume.bash_files(ctx.chat.id, call_id)
    try:
        info = json.loads(run_f.read_text(encoding="utf-8"))
        pid, t0 = int(info["pid"]), float(info["t0"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    created = info.get("created")
    why = steered = None
    if osutil.same_process(pid, created):
        why, steered = _wait_cmd(ctx, lambda: osutil.same_process(pid, created), lambda: kill_tree(pid), t0,
                                 int(info.get("timeout") or 120), bool(info.get("waits")), tick=0.25)
    try:
        code = int(exit_f.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        code = None
    note = None
    if code is None and not why:
        note = ("cut off: Flock restarted while this ran and the command ended without an exit code; the output "
                "so far is above. Check what it did before running it again")
    return _bash_result(ctx, _read_out(out_f), why, steered, code, t0, note)


# `until ...; do sleep 20; done`, `while ...; do ... sleep ...`: waiting inside one command. Allowed for a short wait (a
# dev server coming up), cut at WAIT_LOOP_MAX seconds so a model cannot sit in one for minutes.
WAIT_LOOP = re.compile(r"\b(?:until|while)\b[\s\S]*?\bdo\b[\s\S]*?\bsleep\b|\bwatch\s+-n\b|\btail\s+(?:-\w+\s+)*-f\b")
WAIT_LOOP_MAX = 60


_all_jobs = []


def stop_jobs():
    for p in _all_jobs:
        if p.poll() is None:
            kill_tree(p.pid)


def t_glob(ctx, a):
    pat = a.get("pattern") or "*"
    base = resolve(ctx, a.get("path")) if a.get("path") else ctx.cwd
    if not base.exists():
        raise ToolError(f"{base} does not exist")
    try:
        out = subprocess.run([RG, "--files", "--glob", pat, "--hidden", "--glob", "!.git"], cwd=str(base), capture_output=True,
                             timeout=60, creationflags=NOWIN)
        files = [f for f in out.stdout.decode("utf-8", "replace").splitlines() if f]
    except (OSError, subprocess.TimeoutExpired):
        files = [str(p.relative_to(base)) for p in base.glob(pat) if p.is_file()]
    full = []
    for f in files:
        fp = base / f
        try:
            full.append((fp.stat().st_mtime, str(fp)))
        except OSError:
            pass
    full.sort(reverse=True)
    if not full:
        return f"No files match {pat} in {base}."
    shown = [p for _, p in full[:200]]
    more = f"\n[{len(full) - 200} more not shown]" if len(full) > 200 else ""
    return "\n".join(shown) + more


def t_grep(ctx, a):
    pat = a.get("pattern")
    if not pat:
        raise ToolError("pattern is missing")
    base = resolve(ctx, a.get("path")) if a.get("path") else ctx.cwd
    mode = (a.get("output_mode") or "content").lower()
    args = [RG, "--color", "never", "--no-heading", "--hidden", "--glob", "!.git", "--max-columns", "300"]
    if a.get("case_insensitive") or a.get("-i"):
        args.append("-i")
    if a.get("glob"):
        args += ["--glob", a["glob"]]
    if mode.startswith("files"):
        args.append("-l")
    elif mode == "count":
        args.append("-c")
    else:
        args.append("-n")
        if a.get("context"):
            args += ["-C", str(min(int(a["context"]), 10))]
    args += ["-e", pat, str(base)]
    try:
        out = subprocess.run(args, capture_output=True, timeout=60, creationflags=NOWIN, cwd=str(ctx.cwd))
    except OSError:                                   # no ripgrep on this machine: slower, same answers
        text = _py_grep(base, pat, mode, bool(a.get("case_insensitive") or a.get("-i")), a.get("glob"),
                        min(int(a.get("context") or 0), 10))
        if not text:
            return f"No matches for {pat!r} in {base}."
        lines = text.splitlines()
        if len(lines) > 250:
            text = "\n".join(lines[:250]) + f"\n[{len(lines) - 250} more lines; narrow the search]"
        return clip(text, ctx=ctx, label="grep")
    text = out.stdout.decode("utf-8", "replace")
    if out.returncode == 1:
        return f"No matches for {pat!r} in {base}."
    if out.returncode > 1:
        raise ToolError(out.stderr.decode("utf-8", "replace")[:800] or "ripgrep failed")
    lines = text.splitlines()
    if len(lines) > 250:
        text = "\n".join(lines[:250]) + f"\n[{len(lines) - 250} more lines; narrow the search]"
    return clip(text, ctx=ctx, label="grep")


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "dist", "build", ".next"}


def _py_grep(base, pat, mode, ci, glob_pat, context):
    """Grep without ripgrep: walks the folder (skipping .git, node_modules, venvs), text files only."""
    import fnmatch
    try:
        rx = re.compile(pat, re.I if ci else 0)
    except re.error as e:
        raise ToolError(f"bad pattern: {e}")
    files = [base] if base.is_file() else []
    if not files:
        for root, dirs, names in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                if not glob_pat or fnmatch.fnmatch(n, glob_pat) or fnmatch.fnmatch(os.path.join(root, n), glob_pat):
                    files.append(Path(root) / n)
            if len(files) > 20_000:
                break
    out = []
    for f in files:
        try:
            if f.stat().st_size > 4_000_000:
                continue
            with open(f, "rb") as fh:
                head = fh.read(4096)
            if b"\0" in head:
                continue
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not hits:
            continue
        if mode.startswith("files"):
            out.append(str(f))
        elif mode == "count":
            out.append(f"{f}:{len(hits)}")
        else:
            shown = set()
            for i in hits:
                for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                    if j not in shown:
                        shown.add(j)
                        out.append(f"{f}:{j + 1}:{lines[j][:300]}")
        if len(out) > 2000:
            break
    return "\n".join(out)


def _mcp_text(server, tool, args, ctx, timeout=180):
    try:
        res = mcp.server(server).call(tool, args, timeout=timeout, cancel=ctx.cancel)
    except mcp.MCPError as e:
        raise ToolError(str(e))
    text, images, is_err = mcp.text_of(res)
    if is_err:
        raise ToolError(text[:2000] or f"{tool} failed")
    return text, images


def t_websearch(ctx, a):
    q = a.get("query") or a.get("queries")
    if not q:
        raise ToolError("query is missing")
    kind = (a.get("kind") or "web").lower()
    if kind == "papers":
        text, _ = _mcp_text("web", "papers", {"query": q if isinstance(q, str) else q[0], "max_results": 6}, ctx)
    else:
        qs = q if isinstance(q, list) else [q]
        text, _ = _mcp_text("web", "search", {"queries": qs[:4], "max_results": 6, "kind": "news" if kind == "news" else "web"}, ctx)
    return clip(text, 16_000, ctx, "search")


def t_webfetch(ctx, a):
    url = a.get("url")
    if not url or not re.match(r"^https?://", url):
        raise ToolError("an http(s) url is needed")
    text, _ = _mcp_text("web", "fetch", {"url": url, "max_chars": 12_000, "start": int(a.get("start") or 0)}, ctx, timeout=120)
    return text


BROWSER_MAP = {"open": "browser_navigate", "navigate": "browser_navigate", "snapshot": "browser_snapshot",
               "click": "browser_click", "type": "browser_type", "press": "browser_press_key", "console": "browser_console_messages",
               "eval": "browser_evaluate", "screenshot": "browser_take_screenshot", "resize": "browser_resize",
               "back": "browser_navigate_back", "close": "browser_close", "wait": "browser_wait_for"}


BROWSER_HUNG = ("The page stopped responding, so the browser was restarted (fresh tab, nothing open). When a page you made does "
                "this, its JavaScript is almost always stuck: a loop that runs far too many times (e.g. one grid line per unit "
                "of a value like 25000), a while loop that never ends, or a redraw that triggers itself again. Find and fix "
                "that in the code, then open the page again.")


def t_browser(ctx, a):
    act = (a.get("action") or "").lower().strip()
    tool = BROWSER_MAP.get(act)
    if not tool:
        raise ToolError(f"unknown action {act!r}; use one of {', '.join(sorted(set(BROWSER_MAP) - {'navigate'}))}")
    args = {}
    if act in ("open", "navigate"):
        url = a.get("url") or ""
        if not url:
            raise ToolError("url is needed")
        if url.lower().startswith("file:"):            # Playwright blocks file://: serve the file over http instead
            url = urllib.parse.unquote(re.sub(r"^file:/*", "", url, flags=re.I))
        is_path = re.match(r"^[a-zA-Z]:[\\/]", url) or url.startswith("/") or \
            (not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) and (ctx.cwd / url.split("?")[0]).exists())
        if is_path:
            url = preview_url(ctx, str(resolve(ctx, url)))
        args = {"url": url}
    elif act in ("click", "type"):
        if not a.get("ref"):
            raise ToolError("ref is needed (take a snapshot first)")
        args = {"target": str(a["ref"]), "element": a.get("element") or str(a["ref"])}
        if act == "type":
            args.update(text=a.get("text") or "", submit=bool(a.get("submit")))
    elif act == "press":
        args = {"key": a.get("key") or "Enter"}
    elif act == "eval":
        js = (a.get("script") or "").strip()
        if not js:
            raise ToolError("script is needed")
        if not js.startswith(("(", "function", "async")):
            js = "() => { " + (js if "return" in js else "return " + js) + " }"
        args = {"function": js}
    elif act == "resize":
        args = {"width": int(a.get("width") or 1280), "height": int(a.get("height") or 800)}
    elif act == "wait":
        args = {"time": min(float(a.get("seconds") or 2), 30)}
    elif act == "console":
        args = {"level": "info"}
    elif act == "screenshot":
        args = {"scale": "css", "type": "png"}
    try:
        text, images = _mcp_text("browser", tool, args, ctx, timeout=90)
    except ToolError as e:
        if not re.search(r"Timeout \d+ ?ms exceeded|took longer than|Target crashed|Page crashed", str(e), re.I):
            raise
        mcp.restart("browser")          # a hung page blocks every later call on that tab: start again with a fresh browser
        raise ToolError(BROWSER_HUNG) from None

    def inline_snapshot(m):          # Playwright MCP writes snapshots to files: put the page structure back in the reply
        p = config.DATA / m.group(1).replace("\\", "/")
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return m.group(0)
        return "```yaml\n" + (body[:9000] + "\n[... snapshot cut]" if len(body) > 9000 else body) + "\n```"
    text = re.sub(r"- \[Snapshot\]\(([^)]+\.ya?ml)\)", inline_snapshot, text)

    def inline_console(m):           # "New console entries: browser\console-....log#L1-L5" -> the lines themselves
        try:
            lines = (config.DATA / m.group(1).replace("\\", "/")).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return m.group(0)
        a = int(m.group(2))
        got = [re.sub(r"^\[\s*\d+ms\]\s*", "", ln) for ln in lines[a - 1:int(m.group(3) or a)]]
        got = [g for g in got if "favicon.ico" not in g][:30]
        return "New console entries:\n" + ("\n".join("  " + g[:400] for g in got) if got else "  (none that matter)")
    text = re.sub(r"New console entries: ([^\s#]+?\.log)#L(\d+)(?:-L(\d+))?", inline_console, text)
    shot_files = [config.DATA / s.replace("\\", "/") for s in re.findall(r"\[Screenshot[^\]]*\]\(([^)]+\.(?:png|jpe?g))\)", text)]
    text = re.sub(r"(^|\n)### (Ran Playwright code|Open tabs)\n.*?(?=\n### |\Z)", "", text, flags=re.S)
    if act in CANVAS_ACTS:
        text += canvas_report(ctx)
    result = {"text": clip(text.strip() or "done", 14_000, ctx, "browser")}
    f = None
    if images:
        d = config.DATA / "browser"
        d.mkdir(parents=True, exist_ok=True)
        mime, data = images[0]
        f = d / f"shot-{int(time.time() * 1000)}.{'jpg' if 'jpeg' in mime else 'png'}"
        f.write_bytes(base64.b64decode(data))
    elif shot_files and shot_files[0].exists():
        f = shot_files[0]
    if f is not None:
        result["text"] += f"\nScreenshot saved: {f}"
        result["ui"] = {"image": str(f)}
        result["image"] = f
    return result


CANVAS_ACTS = {"open", "navigate", "snapshot", "click", "type", "press", "back", "wait"}


def canvas_report(ctx):
    """What each <canvas> on the page holds, in words: charts are pixels, so DOM snapshots cannot inspect their drawn
    values directly. Uses window.__kchCanvas from browser_init.js (fill share, colours, drawn text)."""
    try:
        res = mcp.server("browser").call("browser_evaluate", {"function": "() => window.__kchCanvas ? window.__kchCanvas() : []"},
                                         timeout=15, cancel=ctx.cancel)
        raw, _, err = mcp.text_of(res)
        m = re.search(r"### Result\s*\n(.*?)(?=\n### |\Z)", raw, re.S)
        items = json.loads(m.group(1).strip()) if (m and not err) else []
    except Exception:
        return ""
    if not isinstance(items, list) or not items:
        return ""
    lines = []
    for c in items[:10]:
        s = f"- {c.get('name')} {c.get('w')}x{c.get('h')} (on screen {c.get('shown')}): "
        if c.get("error"):
            s += f"could not be read ({c['error']})"
        elif c.get("filled") is not None:
            s += f"{c['filled']}% of pixels drawn, {c.get('colours')} colours" + (" (looks empty)" if c["filled"] < 1.5 else "")
        texts = [str(t) for t in c.get("text") or []]
        if texts:
            s += "; text drawn: " + ", ".join(texts[:30])
            bad = [t for t in texts if re.search(r"NaN|undefined|null|Infinity", t)]
            if bad:
                s += f"  <- {', '.join(bad[:3])}: a value went wrong"
        else:
            s += "; no text drawn"
        lines.append(s)
    return "\n\n### Canvas check (what the charts hold)\n" + "\n".join(lines)


def preview_url(ctx, path):
    """A file in a project folder, as the preview server's http URL (scripts work there; file:// is blocked)."""
    from . import preview
    return preview.url_for(path)


def t_todo(ctx, a):
    todos = a.get("todos")
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except ValueError:
            raise ToolError("todos must be a list of {content, status}")
    if not isinstance(todos, list):
        raise ToolError("todos must be a list of {content, status}")
    clean = []
    for t in todos[:30]:
        if isinstance(t, dict) and t.get("content"):
            st = t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"
            clean.append({"content": str(t["content"])[:200], "status": st})
    ctx.chat.update_meta(todos=clean)
    ctx.emit({"type": "todos", "todos": clean})
    done = sum(t["status"] == "completed" for t in clean)
    return f"Task list saved ({done}/{len(clean)} done)."


def t_notes(ctx, a):
    cur = ctx.chat.notes()
    if a.get("replace") is not None:
        cur = a["replace"].strip() + "\n"
    elif a.get("add"):
        cur = (cur.rstrip() + "\n" if cur.strip() else "") + a["add"].strip() + "\n"
    else:
        log = ctx.chat.log().strip()
        return (cur or "(no notes yet)") + ("\n\nTurn log (kept by the harness):\n" + log[-6000:] if log else "")
    if len(cur) > 12_000:
        raise ToolError("the notes are over 12,000 characters; replace them with a shorter version")
    ctx.chat.set_notes(cur)
    ctx.emit({"type": "notes", "text": cur})
    return f"Notes saved ({len(cur):,} characters)."


def t_skill(ctx, a):
    from . import skills
    s = skills.get(a.get("name") or "")
    if not s:
        names = ", ".join(x["name"] for x in skills.enabled()) or "none"
        raise ToolError(f"no skill called {a.get('name')!r}; available: {names}")
    files = [str(p.relative_to(s["dir"])) for p in Path(s["dir"]).rglob("*") if p.is_file() and p.name != "SKILL.md"][:40]
    body = Path(s["dir"], "SKILL.md").read_text(encoding="utf-8", errors="replace")
    return clip(f"Skill {s['name']} (folder {s['dir']}):\n\n{body}" + (f"\n\nOther files in the skill folder: {', '.join(files)}" if files else ""),
                24_000, ctx, "skill")


def t_view(ctx, a):
    from . import vision
    return vision.view(ctx, a)


def t_organize_chats(ctx, a):
    from . import sections
    return sections.ai_manage(a, chat=ctx.chat, emit=ctx.emit)


def t_parent_context(ctx, a):
    from . import subagents
    parent_id = (ctx.chat.meta() or {}).get("sub_of")
    if not parent_id:
        raise ToolError("this chat does not have a parent chat")
    return subagents.search_parent(
        parent_id,
        action=a.get("action") or "search",
        query=a.get("query") or "",
        start=int(a.get("start") or 1),
        limit=int(a.get("limit") or 10),
    )


def t_report_to_main(ctx, a):
    from . import subagents
    if not (ctx.chat.meta() or {}).get("sub_of"):
        raise ToolError("this chat does not have a parent chat")
    res = subagents.report_to_main(
        ctx.chat.id,
        summary=a.get("summary") or "",
        wake_main=bool(a.get("wake_main")),
        source="agent",
    )
    return res.get("message") or "Reported findings to the main chat."


RUN = {"Read": t_read, "Write": t_write, "Edit": t_edit, "Bash": t_bash, "Glob": t_glob, "Grep": t_grep,
       "WebSearch": t_websearch, "WebFetch": t_webfetch, "Browser": t_browser, "TodoWrite": t_todo, "Notes": t_notes,
       "Skill": t_skill, "View": t_view, "OrganizeChats": t_organize_chats,
       "ParentContext": t_parent_context, "ReportToMain": t_report_to_main}


def parse_args(raw):
    """Tool arguments as a dict. Small models sometimes send broken JSON; repair the common cases."""
    if isinstance(raw, dict):
        return raw
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        pass
    fixed = re.sub(r",\s*([}\]])", r"\1", raw)
    for tail in ("", "}", '"}', '"}}', "]}", "}}"):
        try:
            v = json.loads(fixed + tail)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    raise ToolError("the arguments were not valid JSON: " + raw[:300])


def run(ctx, name, raw_args):
    """Run one tool call. Returns {"text", "ok", "ui"?, "image"?}."""
    t0 = time.time()
    fn = RUN.get(name)
    if not fn:
        return {"text": f"There is no tool called {name}. Tools: {', '.join(RUN)}", "ok": False}
    try:
        args = parse_args(raw_args)
        out = fn(ctx, args)
        res = out if isinstance(out, dict) else {"text": out}
        res["ok"] = True
    except ToolError as e:
        res = {"text": f"Error: {e}", "ok": False}
    except Exception as e:                     # a bug in a tool must not end the chat
        res = {"text": f"Error: {e.__class__.__name__}: {e}", "ok": False}
    res["secs"] = round(time.time() - t0, 2)
    return res
