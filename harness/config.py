"""Where things live and how settings are layered. Nothing about any one person's machine belongs in this file.

FLOCK_HOME (default ~/.flock, or an existing ~/.nest / ~/.raven / ~/.kc-harness install; KCH_HOME / RAVEN_HOME / NEST_HOME also work) holds everything the app writes:
    config.json    choices made in the app (default model, connections added in Settings, imported skills, ...)
    keys/          API keys, one file per connection id (typed into Settings, or put there by hand). Never in a JSON file.
    chats/ logs/ workspace/ browser/ cache/ state/ tools/

profile.json (FLOCK_HOME/profile.json, or the file FLOCK_PROFILE / KCH_PROFILE points to) is this machine's own setup. It is written by hand
and never by the app: connections only this machine has, a launcher for a model server, a meetings app, the dictation
Python, extra PATH entries, extra credential paths to fence off. examples/profile.example.json explains every field.

Layers: built-in defaults  <-  profile.json  <-  config.json. The app writes config.json only, and only what differs from
the layers below it, so a profile edit is never shadowed by a stale copy.
"""
import copy
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
APP = Path(__file__).resolve().parent.parent                  # the repository folder (harness/, web/)


def _default_data_dir():
    """Prefer ~/.flock for fresh installs, while preserving existing ~/.nest, ~/.raven, or ~/.kc-harness data folders."""
    for name in (".flock", ".nest", ".raven", ".kc-harness"):
        cand = HOME / name
        if cand.exists():
            return cand
    return HOME / ".flock"


DATA = Path(os.path.expanduser(
    os.environ.get("FLOCK_HOME")
    or os.environ.get("NEST_HOME")
    or os.environ.get("RAVEN_HOME")
    or os.environ.get("KCH_HOME")
    or os.environ.get("KCH_DATA")
    or str(_default_data_dir())
))
PROFILE_PATH = Path(os.path.expanduser(
    os.environ.get("FLOCK_PROFILE")
    or os.environ.get("NEST_PROFILE")
    or os.environ.get("RAVEN_PROFILE")
    or os.environ.get("KCH_PROFILE")
    or str(DATA / "profile.json")
))
PORT = int(os.environ.get("FLOCK_PORT") or os.environ.get("NEST_PORT") or os.environ.get("RAVEN_PORT") or os.environ.get("KCH_PORT") or 8780)
PREVIEW_PORT = PORT + 1                                       # project files for the preview pane: another origin, no API
CHATS = DATA / "chats"
LOGS = DATA / "logs"
WORKSPACE = DATA / "workspace"                                # default home for new projects
CONFIG_PATH = DATA / "config.json"

# A test never writes to the live app's data. In a test process -- a tools/test_*.py
# script, or anything started with FLOCK_TEST / KCH_TEST set (tools/testhome.py sets it, so a server a test starts is covered) --
# the data folder must be a temp folder or one marked as a test's own (TEST_MARK in it); anything else stops here,
# before a single file is written. The app itself is never a test process.
TEST_MARK = ".kch-test-home"


def _is_test():
    return bool(os.environ.get("FLOCK_TEST") or os.environ.get("NEST_TEST") or os.environ.get("KCH_TEST")) or bool(re.search(r"(^|[\\/])test_[^\\/]*\.py$", sys.argv[0] or ""))


def test_home_ok(path):
    """True for a folder a test may write to: inside the system temp folder, or marked as a test's own."""
    import tempfile
    try:
        p = Path(os.path.expanduser(str(path))).resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return p == tmp or tmp in p.parents or (p / TEST_MARK).is_file()


def guard_test_data(path=None):
    if _is_test() and not test_home_ok(path or DATA):
        raise RuntimeError("a test may not use the data folder %s: it is not a temp folder or a test home (%s). Import "
                           "tools/testhome.py first; it gives the test a temp KCH_HOME." % (path or DATA, TEST_MARK))


guard_test_data()


def expand(p):
    """~ and %VARS% / $VARS in a path from a profile."""
    return Path(os.path.expandvars(os.path.expanduser(str(p))))


def _jsonc(text):
    """JSON with // comment lines (hand-written profiles); keys starting with _ are notes and ignored."""
    clean = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))
    data = json.loads(clean) if clean.strip() else {}

    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if not k.startswith("_")}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    return strip(data)


def _read_profile():
    try:
        return _jsonc(PROFILE_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except ValueError as e:
        print(f"Flock: {PROFILE_PATH} is not valid JSON ({e}); ignoring it", file=sys.stderr)
        return {}


PROFILE = _read_profile()
KEYS = expand(PROFILE.get("keys_dir") or DATA / "keys")      # API keys typed into Settings
STATE = expand(PROFILE.get("state_dir") or DATA / "state")   # the model-server launcher's state (survives restarts)
TOOLS = expand(os.environ.get("FLOCK_TOOLS") or os.environ.get("NEST_TOOLS") or os.environ.get("RAVEN_TOOLS") or os.environ.get("KCH_TOOLS") or PROFILE.get("tools_dir") or DATA / "tools")   # what install.py sets up
for _p in PROFILE.get("python_paths") or []:                  # where a profile's plugins (key providers) live
    if str(expand(_p)) not in sys.path:
        sys.path.append(str(expand(_p)))

# Connection kinds: openai = any OpenAI-compatible /v1 API (llama.cpp, vLLM, LM Studio, Ollama, OpenRouter, DeepSeek,
# Gemini, OpenAI ...); opencode-go = an OpenCode Go subscription; claude-code = the Claude Code CLI with the user's own
# Claude subscription; codex = the Codex CLI with the user's ChatGPT subscription. Presets for each are in presets.py.
DEFAULTS = {
    "endpoint": "",                   # the default connection; empty = the first one
    "reasoning": "fast",              # fast = no thinking, think = thinking on, deep = thinking + told to be thorough
    "compact_at": 0.85,               # share of the window at which a chat is compacted
    "endpoints": [],                  # filled from detect_endpoints(), the profile and config.json
    "runtime": {"enabled": False},    # a launcher for the model server behind one connection (see runtime.py)
    "meetings": {"enabled": False},   # a meetings web app shown inside the harness (see meetings.py)
    "dictation": {"enabled": "auto"}, # speech to text for the mic button (see dictation.py)
    "dictation_engine": "",           # the mic: "" (Whisper when set up), "whisper", "browser" (Chrome's, sends audio out), "off"
    "prompt": {"about": "", "extra": []},   # extra lines for the system prompt (who the user is, their own tools)
    "path_extra": [],                 # folders put in front of PATH for the Bash tool
    "deny_extra": [],                 # extra path patterns (regex) the tools must never read
    "playwright_mcp": "",             # path to @playwright/mcp's cli.js (default: tools/node_modules/...)
    "browser_channel": "",            # chrome, msedge or chromium for the Browser tool (default: whichever is installed)
    "search_python": "",              # a Python with the search server's packages (default: tools/search-venv)
    "mcp": {},                        # extra MCP servers: {"name": {"command", "args", "env", "enabled"}}
    "skills": [],                     # [{"name", "path" (folder with SKILL.md), "source"}]
    "imported_mcp": [],               # [{"name", "source": "claude"|"codex", "ref"}] read from that tool's config each time
    "projects": [],                   # [{"path", "name"}]
    "project_files": False,           # also read AGENTS.md from a project's folder at chat start
    "subagents": {},                  # the Agent tool: enabled, max_depth, start_servers, parallel
                                      # (defaults in subagents.DEFAULTS)
    "computer": {},                   # the Computer tool: enabled, mode ("chat" or "ask"), max_width, max_height of
                                      # screenshots (defaults in computer.DEFAULTS)
    "updates": {"auto": True, "repo": ""},   # check GitHub for a newer Flock (repo "" = version.REPO_URL)
    "chat_import": {"auto": "off"},   # update imported Claude Code / Codex chats by itself: off, start, hourly, daily
    "app_code": "",                   # "this app" for the agent; "" = where the app runs from (tests: a clone)
    "test_timeout": 300,              # seconds for a project's own test command, run by the verification gate
    "sections": [],                   # optional default chat sections: [{"id": "general", "name": "General"}, ...]
    "section_rules": [],              # optional first-sort regex rules: [{"section": "work", "pattern": "..."}]
}

_lock = threading.RLock()
_cfg = None
_base = None
_profile_mtime = 0.0
BOOT_ID = f"{int(time.time())}-{os.getpid()}"


def _git_head_sha():
    """Fast pure-file read of the current git commit SHA (works in normal repos and worktrees, no subprocess)."""
    try:
        dot = APP / ".git"
        if dot.is_file():
            raw = dot.read_text(encoding="utf-8", errors="replace").strip()
            if raw.lower().startswith("gitdir:"):
                gd = raw.split(":", 1)[1].strip()
                dot = Path(gd) if Path(gd).is_absolute() else (APP / gd)
        head = dot / "HEAD"
        if not head.is_file():
            return "nogit"
        ref = head.read_text(encoding="utf-8", errors="replace").strip()
        if ref.startswith("ref:"):
            rname = ref.split(":", 1)[1].strip()
            for base in (dot, dot.parent.parent if dot.parent.name == "worktrees" else dot):
                rf = base / rname
                if rf.is_file():
                    return rf.read_text(encoding="utf-8", errors="replace").strip()[:12]
                pf = base / "packed-refs"
                if pf.is_file():
                    for line in pf.read_text(encoding="utf-8", errors="replace").splitlines():
                        if not line.startswith("#") and line.endswith(" " + rname):
                            return line.split(" ", 1)[0][:12]
            return rname[-12:]
        return ref[:12]
    except OSError:
        return "nogit"


def profile_mtime():
    try:
        return PROFILE_PATH.stat().st_mtime if PROFILE_PATH.is_file() else 0.0
    except OSError:
        return 0.0


def build_id():
    """Fingerprint of app commit + web assets + profile mtime + process boot so browsers never stay on a stale page."""
    web_mt = 0
    web_dir = APP / "web"
    try:
        for f in web_dir.iterdir():
            if f.suffix in (".html", ".js", ".css"):
                mt = int(f.stat().st_mtime)
                if mt > web_mt:
                    web_mt = mt
    except OSError:
        pass
    pmt = int(profile_mtime())
    return f"{_git_head_sha()}-{web_mt}-{pmt}-{BOOT_ID}"


def reload_if_profile_changed():
    """Reload layered config if profile.json was edited on disk while the server was running."""
    mt = profile_mtime()
    if mt and mt != _profile_mtime:
        load()
        return True
    return False


_exe_cache = {}  # name -> (ts, env_val, path)
_EXE_TTL_S = 30.0


def claude_exe():
    """The Claude Code CLI: FLOCK_CLAUDE / KCH_CLAUDE, on PATH, or the native installer's place."""
    env = os.environ.get("FLOCK_CLAUDE") or os.environ.get("NEST_CLAUDE") or os.environ.get("KCH_CLAUDE") or ""
    hit = _exe_cache.get("claude")
    now = time.monotonic()
    if hit and hit[1] == env and now - hit[0] < _EXE_TTL_S and (not hit[2] or os.path.exists(hit[2])):
        return hit[2]
    found = ""
    for c in (env, shutil.which("claude"), str(HOME / ".local" / "bin" / "claude.exe"),
              str(HOME / ".local" / "bin" / "claude")):
        if c and os.path.exists(c):
            found = c
            break
    _exe_cache["claude"] = (now, env, found)
    return found


def codex_exe():
    """The Codex CLI: FLOCK_CODEX / KCH_CODEX, on PATH (npm i -g @openai/codex), or the newest build the Codex desktop app ships."""
    env = os.environ.get("FLOCK_CODEX") or os.environ.get("NEST_CODEX") or os.environ.get("KCH_CODEX") or ""
    hit = _exe_cache.get("codex")
    now = time.monotonic()
    if hit and hit[1] == env and now - hit[0] < _EXE_TTL_S and (not hit[2] or os.path.exists(hit[2])):
        return hit[2]
    found = ""
    if env and os.path.exists(env):
        found = env
    else:
        exe = shutil.which("codex")
        if exe:
            found = exe
        else:
            import glob
            cands = glob.glob(str(HOME / "AppData" / "Local" / "OpenAI" / "Codex" / "bin" / "*" / "codex.exe"))
            found = max(cands, key=os.path.getmtime) if cands else ""
    _exe_cache["codex"] = (now, env, found)
    return found


def agy_exe():
    """Google's Antigravity CLI (agy): FLOCK_AGY / KCH_AGY, on PATH, or the installer's default folder."""
    local = Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local"))
    for c in (os.environ.get("FLOCK_AGY") or os.environ.get("NEST_AGY") or os.environ.get("KCH_AGY"), shutil.which("agy"),
              str(local / "agy" / "bin" / "agy.exe"),
              str(HOME / ".local" / "bin" / "agy")):
        if c and os.path.exists(c):
            return c
    return ""


def detect_endpoints():
    """Subscriptions that plug in with no key: the Claude Code CLI (Claude plan) and the Codex CLI (ChatGPT plan)."""
    eps = []
    if claude_exe():
        eps.append({"id": "claude", "name": "Claude (your plan, via Claude Code)", "kind": "claude-code", "model": "",
                    "context": 200_000, "detected": True})
    if codex_exe():
        eps.append({"id": "chatgpt", "name": "ChatGPT (your plan, via Codex)", "kind": "codex", "model": "",
                    "context": 272_000, "detected": True})
    return eps


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if k in ("endpoints", "endpoints_removed"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _merge_endpoints(base_eps, layer):
    """A layer's endpoints change those with the same id field by field, or add new ones; endpoints_removed hides ids."""
    out = [copy.deepcopy(e) for e in base_eps if e["id"] not in set(layer.get("endpoints_removed") or [])]
    index = {e["id"]: i for i, e in enumerate(out)}
    for e in layer.get("endpoints") or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        if e["id"] in index:
            out[index[e["id"]]] = {**out[index[e["id"]]], **copy.deepcopy(e)}
        else:
            index[e["id"]] = len(out)
            out.append(copy.deepcopy(e))
    return out


def _diff(cur, base):
    """What cur changes relative to base: dicts compared key by key, anything else as a whole."""
    out = {}
    for k, v in cur.items():
        if k in ("endpoints", "endpoints_removed"):
            continue
        b = base.get(k, ...)
        if isinstance(v, dict) and isinstance(b, dict):
            d = _diff(v, b)
            if d:
                out[k] = d
        elif v != b:
            out[k] = copy.deepcopy(v)
    return out


def load():
    global _cfg, _base, PROFILE, _profile_mtime
    guard_test_data()                                # again: a test may have pointed DATA somewhere after the import
    with _lock:
        _profile_mtime = profile_mtime()
        PROFILE = _read_profile()
        saved = {}
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except ValueError:
                CONFIG_PATH.replace(CONFIG_PATH.with_suffix(f".bad-{int(time.time())}.json"))
        base = _merge(DEFAULTS, PROFILE)
        merged = _merge_endpoints(detect_endpoints(), PROFILE)
        order = [e.get("id") for e in PROFILE.get("endpoints") or [] if isinstance(e, dict)]
        base["endpoints"] = sorted(merged, key=lambda e: order.index(e["id"]) if e["id"] in order else len(order))
        cfg = _merge(base, saved)
        cfg["endpoints"] = _merge_endpoints(base["endpoints"], saved)
        if not cfg.get("endpoint") or not any(e["id"] == cfg["endpoint"] for e in cfg["endpoints"]):
            cfg["endpoint"] = cfg["endpoints"][0]["id"] if cfg["endpoints"] else ""
        _base, _cfg = base, cfg
        return copy.deepcopy(cfg)


def get():
    with _lock:
        if _cfg is None:
            load()
        return copy.deepcopy(_cfg)


def save(cfg):
    """Write config.json: only the differences from the defaults and the profile."""
    global _cfg
    with _lock:
        if _base is None:
            load()
        out = _diff(cfg, _base)
        base_eps = {e["id"]: e for e in _base["endpoints"]}
        eps = []
        for e in cfg.get("endpoints") or []:
            b = base_eps.get(e["id"])
            if b is None:
                eps.append(e)
            elif e != b:
                eps.append({"id": e["id"], **{k: v for k, v in e.items() if b.get(k, ...) != v}})
        removed = [i for i in base_eps if i not in {e["id"] for e in cfg.get("endpoints") or []}]
        if eps:
            out["endpoints"] = eps
        if removed:
            out["endpoints_removed"] = removed
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
        _cfg = copy.deepcopy(cfg)


def update(fn):
    """fn(cfg) changes the settings in place; saved at once."""
    with _lock:
        cfg = get()
        fn(cfg)
        save(cfg)
        return cfg


def endpoint(eid=None):
    cfg = get()
    eid = eid or cfg["endpoint"]
    for e in cfg["endpoints"]:
        if e["id"] == eid:
            return e
    if cfg["endpoints"]:
        return cfg["endpoints"][0]
    raise LookupError("no model is connected yet: add one in Settings > Connections")


def in_profile(eid):
    return any(e.get("id") == eid for e in PROFILE.get("endpoints") or []) or \
        any(e["id"] == eid for e in detect_endpoints())


# ---- keys: never in a JSON file, never sent to the browser, never logged
def _plugin(ref):
    """'module:attribute' from python_paths, e.g. a profile's own key provider."""
    import importlib
    mod, _, attr = ref.partition(":")
    return getattr(importlib.import_module(mod), attr)


def read_keys(ep):
    """All keys for a connection, first one first (several = fail over when one runs out). Sources, in order:
    the profile's key_provider plugin, keys/<id> (typed into Settings), key_file, key_env."""
    if ep.get("key_provider"):
        try:
            got = _plugin(ep["key_provider"])()
            keys = got[1] if isinstance(got, tuple) else got
            return [k for k in (keys if isinstance(keys, list) else [keys]) if k]
        except Exception as e:
            print(f"Flock: key provider {ep['key_provider']} failed: {e.__class__.__name__}", file=sys.stderr)
    for path in (KEYS / ep["id"], expand(ep["key_file"]) if ep.get("key_file") else None):
        if path and path.is_file():
            try:
                keys = [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip() and not ln.startswith("#")]
                if keys:
                    return keys
            except OSError:
                pass
    if ep.get("key_env") and os.environ.get(ep["key_env"]):
        return [os.environ[ep["key_env"]].strip()]
    return []


def read_key(ep):
    keys = read_keys(ep)
    return keys[0] if keys else ""


def key_used(ep, key):
    """Tell the profile's plugin which key worked after a failover (it may remember it for next time)."""
    if ep.get("key_success"):
        try:
            _plugin(ep["key_success"])(key)
        except Exception:
            pass


def save_key(eid, key):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", eid or ""):
        raise ValueError("bad connection id")
    KEYS.mkdir(parents=True, exist_ok=True)
    path = KEYS / eid
    path.write_text(key.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def delete_key(eid):
    try:
        (KEYS / eid).unlink()
    except OSError:
        pass


for d in (DATA, CHATS, LOGS, WORKSPACE):
    d.mkdir(parents=True, exist_ok=True)
