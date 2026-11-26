"""The verification gate and live folders.

- Fast checks on the code files a turn changed (Python compile, `node --check`, JSON, a basic HTML and CSS parse),
  run by the harness itself when the model ends its turn: no model call, nothing in the prompt.
- A project's own test command: `.flock.json` (or legacy `.nest.json` / `.raven.json` / `.kc-harness.json`) {"test": "npm test"} in the project (or a parent of a changed
  file), or a line `Test: npm test` in the project notes.
- This app's own code (or `app_code` in the settings, for tests): after a turn that changed it the harness tells the
  user to reload the page or restart the app (agent._live_notice); it never restarts anything itself.
"""
import html.parser
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, osutil, store

CODE_EXT = {".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json", ".html", ".htm", ".css", ".sh", ".bash",
            ".ps1", ".bat", ".cmd", ".rs", ".go", ".java", ".kt", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php",
            ".vue", ".svelte", ".lua", ".r", ".jl", ".swift", ".scss", ".sql", ".toml", ".yaml", ".yml"}


def is_code(p):
    return Path(p).suffix.lower() in CODE_EXT


# ------------------------------------------------------------------------------------------------ this app's code
def _norm(p):
    try:
        p = Path(p).resolve()
    except OSError:
        p = Path(os.path.abspath(p))
    return os.path.normcase(str(p)).rstrip("\\/")


def app_code():
    """This app's own code folder: the repository it runs from, or `app_code` in the settings (tests use a clone)."""
    own = config.get().get("app_code")
    return Path(config.expand(own)) if own else config.APP


def live_for(path):
    """("Flock", folder) when path is inside this app's own code, else None."""
    folder = app_code()
    n, f = _norm(path), _norm(folder)
    return ("Flock", folder) if n == f or n.startswith(f + os.sep) else None


# ------------------------------------------------------------------------------------------------ file checks
def _node():
    return shutil.which("node")


def _run(args, timeout=20, cwd=None):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, cwd=cwd, creationflags=osutil.NOWIN, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 0, ""                                  # too slow to call it broken
    except OSError as e:
        return None, str(e)


def _node_error(text, tmp, label):
    """node --check's message, short: the location line, the code line, the caret and the error."""
    text = text.replace(str(tmp), label).replace(str(tmp).replace("\\", "/"), label)
    lines = [ln for ln in text.replace("\r\n", "\n").split("\n") if ln.strip() and not ln.startswith("Node.js v")]
    keep = [ln for ln in lines if not re.match(r"\s+at ", ln)]
    return "\n".join(keep[:6])


def _check_js_text(src, label, module=False, line0=0):
    node = _node()
    if not node:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".mjs" if module else ".js", prefix="kch-check-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n" * line0 + src)          # keep line numbers those of the real file
        code, out = _run([node, "--check", tmp])
        if code not in (0, None) and not module and re.search(r"Cannot use import statement|Unexpected token 'export'|"
                                                             r"import\.meta|await is only valid", out):
            return _check_js_text(src, label, True, line0)
        return _node_error(out, tmp, label) if code not in (0, None) else None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def check_python(p, text):
    try:
        compile(text, str(p), "exec", dont_inherit=True)
    except SyntaxError as e:
        line = (e.text or "").rstrip("\n")
        return f"{p.name}, line {e.lineno}: {e.msg}" + (f"\n    {line.strip()}" if line.strip() else "")
    except ValueError as e:                        # null bytes
        return f"{p.name}: {e}"
    return None


def check_json(p, text):
    if re.match(r"(ts|js)config.*\.json$", p.name, re.I) or p.parent.name == ".vscode":
        return "skip"                              # JSON with comments
    try:
        json.loads(text)
    except ValueError as e:
        if re.search(r"^\s*//", text, re.M):         # // comment lines, as in Flock profiles: read like config._jsonc
            try:
                json.loads("\n".join("" if ln.lstrip().startswith("//") else ln for ln in text.splitlines()))
                return None
            except ValueError as e2:
                return f"{p.name}: {e2}"
        return f"{p.name}: {e}"
    return None


def check_css(p, text):
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    body = re.sub(r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", "''", body)
    depth = 0
    for i, ln in enumerate(body.split("\n"), 1):
        for ch in ln:
            depth += (ch == "{") - (ch == "}")
            if depth < 0:
                return f"{p.name}, line {i}: a '}}' with no '{{' before it"
    if depth > 0:
        return f"{p.name}: {depth} '{{' never closed"
    return None


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr", "param"}
STRICT = {"div", "section", "main", "header", "footer", "nav", "article", "aside", "form", "table", "ul", "ol", "script",
          "style", "template", "button", "select", "textarea", "span", "a", "svg", "canvas", "label", "dialog", "details",
          "summary", "pre", "code", "h1", "h2", "h3", "h4", "h5", "h6", "iframe", "video", "audio"}


class _Html(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.problems, self.scripts = [], [], []
        self._script = None

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        if tag in STRICT:
            self.stack.append((tag, self.getpos()[0]))
        if tag == "script":
            a = dict(attrs)
            typ = (a.get("type") or "").lower()
            run = "src" not in a and typ in ("", "text/javascript", "application/javascript", "module")
            self._script = {"line": self.getpos()[0], "module": typ == "module", "text": "", "run": run}

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag == "script" and self._script is not None:
            if self._script["run"] and self._script["text"].strip():
                self.scripts.append(self._script)
            self._script = None
        if tag not in STRICT:
            return
        if not any(t == tag for t, _ in self.stack):
            self.problems.append(f"line {self.getpos()[0]}: </{tag}> closes nothing")
            return
        while self.stack:
            t, ln = self.stack.pop()
            if t == tag:
                break
            self.problems.append(f"line {ln}: <{t}> is not closed before </{tag}> on line {self.getpos()[0]}")

    def handle_data(self, data):
        if self._script is not None:
            self._script["text"] += data


def check_html(p, text):
    h = _Html()
    try:
        h.feed(text)
        h.close()
    except Exception as e:                         # the parser is lenient; this is rare
        return f"{p.name}: could not be parsed ({e})"
    probs = h.problems + [f"line {ln}: <{t}> is never closed" for t, ln in h.stack]
    for s in h.scripts:
        err = _check_js_text(s["text"], f"{p.name} (script on line {s['line']})", s["module"], s["line"] - 1)
        if err:
            probs.append(err)
    if probs:
        return f"{p.name}: " + ("\n".join(probs[:4]) + (f"\n(and {len(probs) - 4} more)" if len(probs) > 4 else ""))
    return None


def check_js(p, text):
    if _node() is None:
        return "skip"
    return _check_js_text(text, p.name, p.suffix.lower() == ".mjs")


def check_sh(p, text):
    sh = osutil.bash()
    if not sh:
        return "skip"
    code, out = _run([sh, "-n", str(p)])
    return (f"{p.name}: " + out.strip()[:600]) if code not in (0, None) else None


CHECKERS = {".py": check_python, ".pyw": check_python, ".js": check_js, ".mjs": check_js, ".cjs": check_js,
            ".json": check_json, ".html": check_html, ".htm": check_html, ".css": check_css, ".sh": check_sh, ".bash": check_sh}


def check_file(path):
    """{"path", "status": ok | error | skip, "error"?}: skip when there is no fast check for this kind of file."""
    p = Path(path)
    fn = CHECKERS.get(p.suffix.lower())
    if not fn:
        return {"path": str(p), "status": "skip"}
    if not p.is_file():
        return {"path": str(p), "status": "skip"}       # deleted or moved since
    try:
        if p.stat().st_size > 3_000_000:
            return {"path": str(p), "status": "skip"}
        text = p.read_bytes().decode("utf-8", "replace")
        err = fn(p, text)
    except Exception as e:
        return {"path": str(p), "status": "skip", "error": f"{e.__class__.__name__}: {e}"}
    if err == "skip":
        return {"path": str(p), "status": "skip"}
    return {"path": str(p), "status": "error", "error": err} if err else {"path": str(p), "status": "ok"}


def check_files(paths):
    return [check_file(p) for p in paths]


# ------------------------------------------------------------------------------------------------ test commands
NOTES_TEST = re.compile(r"^\s*(?:[-*]\s*)?test(?:s| command)?\s*:\s*`?([^`\n]+?)`?\s*$", re.I | re.M)


PROJECT_CONFIG_NAMES = (".flock.json", ".nest.json", ".raven.json", ".kc-harness.json")


def test_commands(project, files):
    """[(folder, command)] for the changed files: `.flock.json` (or legacy `.nest.json` / `.raven.json` / `.kc-harness.json`)
    {"test": ...} in the project or a parent of a changed file, else a `Test: <command>` line in the project notes."""
    out, seen = [], set()
    project = Path(project)
    starts = [Path(f).parent for f in files]
    if any(_norm(f).startswith(_norm(project) + os.sep) for f in files):
        starts.insert(0, project)
    for s in starts:
        for d in [s, *s.parents]:
            f = next((d / name for name in PROJECT_CONFIG_NAMES if (d / name).is_file()), None)
            if f is not None:
                key = _norm(d)
                if key not in seen:
                    seen.add(key)
                    try:
                        cmd = (json.loads(f.read_text(encoding="utf-8-sig")) or {}).get("test")
                    except (ValueError, OSError, AttributeError):
                        cmd = None
                    if isinstance(cmd, str) and cmd.strip():
                        out.append((d, cmd.strip()))
                break
    if not any(_norm(d) == _norm(project) for d, _ in out) and project in starts:
        pn = store.project_notes_path(str(project))
        if pn.exists():
            m = NOTES_TEST.search(pn.read_text(encoding="utf-8", errors="replace"))
            if m:
                out.append((project, m.group(1).strip()))
    return out


INSPECT = re.compile(r"^\s*(?:cd\s+[^;&|]+(?:&&|;)\s*)*(cat|head|tail|less|ls|dir|pwd|echo|wc|grep|rg|find|which|type|stat|"
                     r"file|tree|sed\s+-n|git\s+(?:diff|status|log|show|blame|branch))\b[^;&|]*(?:\|\s*(?:head|tail|grep|wc|"
                     r"sort|less|cat)\b[^;&|]*)*$", re.I)


def runs_something(name, args):
    """A tool call that tries the code out, rather than looking at it (git diff and cat do not check behaviour)."""
    if name in ("Browser", "Computer"):
        return True
    if name == "Bash":
        cmd = (args or {}).get("command") or ""
        return bool(cmd.strip()) and not INSPECT.match(cmd.strip())
    return False


def summary(results, test=None):
    """'Checked: 2 files OK' / 'Checked 3 files: 1 error' for the line under the reply."""
    done = [r for r in results if r["status"] != "skip"]
    bad = [r for r in done if r["status"] == "error"]
    parts = []
    if done:
        parts.append(f"Checked: {len(done)} file{'s' if len(done) != 1 else ''} OK" if not bad else
                     f"Checked {len(done)} file{'s' if len(done) != 1 else ''}: {len(bad)} error{'s' if len(bad) != 1 else ''}")
    if test:
        parts.append(f"Tests ({test['cmd']}): " + ("passed" if test["ok"] else "failed"))
    return " / ".join(parts)
