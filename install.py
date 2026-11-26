"""Set up Flock. The app itself needs only Python 3.11+; this script adds what its built-in tools use, checks the
rest, and starts the app once to prove it works.

  python install.py                    the usual install: web search + the Browser tool
  python install.py --check            report what is there, install nothing
  python install.py --with-dictation   also an optional part (all of them: see OPTIONAL below, or --help)
  python install.py --with-meetings    also Meetings for the Meeting button (live transcript, notes, an assistant per
                                       project; about 1.5 GB, Python 3.12, Whisper downloads on first use)
  python install.py --no-search --no-browser   skip those parts

Everything goes in KCH_HOME/tools (default ~/.flock/tools; KCH_TOOLS or the profile's tools_dir move it). Nothing
is installed globally except Playwright's Chromium (in Playwright's shared cache, only when Chrome and Edge are both
missing). Safe to run again: finished parts are checked and skipped; a part whose pinned versions changed is redone.

Pinned and verified: Python packages come from deps/*.lock (every package and dependency at an exact version with the
sha256 of its files: pip --require-hashes), the Browser tool from deps/browser (package-lock.json with npm's sha512
integrity: npm ci). tools/lock_deps.py regenerates them.

Output: one line per part: OK, INFO (optional and not set up), WARN (a tool the agent normally has is missing; the line
says how to fix it), FAIL (the app cannot run). Exit code 0 = the app works (warnings allowed), 1 = it does not.
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if sys.version_info < (3, 11):
    print(f"FAIL  Python {sys.version.split()[0]}: Flock needs Python 3.11 or newer (python.org, or `uv python install 3.12`).")
    sys.exit(1)
sys.path.insert(0, str(ROOT))
from harness import config, mcp, meetings_setup, osutil  # noqa: E402  (reads KCH_HOME and KCH_PROFILE exactly like the app)

DEPS = ROOT / "deps"
NODE_MIN = 20                                  # Playwright 1.5x needs Node 20+
RESULTS = []
CHECK_ONLY = False


def report(status, name, detail=""):
    RESULTS.append((status, name, detail))
    print(f"{status:<5} {name}" + (f": {detail}" if detail else ""), flush=True)


def run(cmd, timeout=1800, cwd=None):
    """Run a step with its output shown (pip and npm print their own progress)."""
    cmd = [str(c) for c in cmd]
    print("      $ " + " ".join(cmd), flush=True)
    p = subprocess.run(cmd, timeout=timeout, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError(f"`{Path(cmd[0]).name} {' '.join(cmd[1:3])} ...` failed (exit {p.returncode}); see the output above")


def works(cmd, timeout=120):
    try:
        return subprocess.run([str(c) for c in cmd], capture_output=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def output(cmd, timeout=60):
    try:
        return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def fingerprint(*files):
    """sha256 of the lock files a part was installed from (kept next to it, so a changed lock is noticed)."""
    h = hashlib.sha256()
    for f in files:
        h.update(Path(f).read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()


def stamp_of(folder):
    try:
        return (Path(folder) / ".kch-lock").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def make_venv(venv):
    py = osutil.venv_python(venv)
    if not py.exists():
        try:
            run([sys.executable, "-m", "venv", venv])
        except RuntimeError:
            raise RuntimeError("could not create a virtual environment. Debian/Ubuntu: sudo apt install python3-venv")
    return py


def pip_locked(py, venv, *locks):
    """Install exactly the packages in the lock files, each file checked against its sha256 (pip --require-hashes)."""
    for lock in locks:
        run([py, "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes", "-r", lock])
    (Path(venv) / ".kch-lock").write_text(fingerprint(*locks), encoding="utf-8")


# ---- the parts ---------------------------------------------------------------------------------------------------------

def check_python():
    report("OK", "Python", f"{sys.version.split()[0]} at {sys.executable}")
    if not works([sys.executable, "-m", "ensurepip", "--version"], timeout=60):   # Debian/Ubuntu: in python3-venv
        v = f"{sys.version_info[0]}.{sys.version_info[1]}"
        report("WARN", "Python venv support", f"missing, so web search and dictation cannot be set up. Debian/Ubuntu: "
               f"sudo apt install python{v}-venv, then run install.py again")


def check_files():
    """The page's own files, shipped in the repository (a broken download or checkout would lack them)."""
    need = ["web/index.html", "web/app.js", "web/editor/cm.js", "web/vendor/marked.min.js", "web/vendor/purify.min.js",
            "harness/server.py", "VERSION"]
    missing = [f for f in need if not (ROOT / f).is_file()]
    if missing:
        report("FAIL", "App files", "missing " + ", ".join(missing) + ": download or clone Flock again")
    else:
        report("OK", "App files", f"version {(ROOT / 'VERSION').read_text(encoding='utf-8').strip()}")


def check_git():
    git = osutil.which("git")
    checkout = (ROOT / ".git").exists()
    if git:
        report("OK", "git", output([git, "--version"]).replace("git version ", "") +
               (" (this copy is a git checkout: Settings > Updates pulls new versions)" if checkout else ""))
    elif checkout:
        report("WARN", "git", "not found, but this copy is a git checkout: updates need git (Windows: winget install "
               "Git.Git; macOS: xcode-select --install; Linux: sudo apt install git)")
    else:
        report("INFO", "git", "not found (optional: this copy is a zip install and updates by download)")


def check_updates():
    from harness import selfupdate, version
    r = selfupdate.repo()
    how = "a fast-forward git pull" if (ROOT / ".git").exists() else "downloading the release"
    if r:
        report("OK", "Updates", f"v{version.version()}; checks github.com/{r[0]}/{r[1]} for newer versions (Settings > "
               f"Updates installs them by {how})")
    else:
        report("INFO", "Updates", f"v{version.version()}; no GitHub repository set, so no update checks "
               "(profile.json: \"updates\": {\"repo\": \"https://github.com/owner/flock\"})")


def check_shell():
    b = osutil.bash()
    if b and (Path(b).exists() or shutil.which(b)):
        report("OK", "Shell for the Bash tool", b)
    elif osutil.WINDOWS:
        report("WARN", "Shell for the Bash tool", "Git for Windows not found: install it (git-scm.com, or "
               "`winget install Git.Git`), or set KCH_BASH to a bash.exe. Without it the agent cannot run commands.")
    else:
        report("WARN", "Shell for the Bash tool", "no bash on PATH")
    if osutil.which("rg"):
        report("OK", "ripgrep (fast Grep)", osutil.which("rg"))
    else:
        report("INFO", "ripgrep (fast Grep)", "not found, Grep uses its slower built-in search (optional: install ripgrep)")


def check_node():
    """Node.js for the Browser tool: its path, or None after a WARN that says how to get it."""
    node = osutil.which("node")
    how = ("`winget install OpenJS.NodeJS.LTS`" if osutil.WINDOWS else "`brew install node`" if sys.platform == "darwin"
           else "nodejs.org or NodeSource (Debian/Ubuntu's own nodejs package is often too old)")
    if not node:
        report("WARN", "Node.js", f"not found: the Browser tool needs Node {NODE_MIN} or newer ({how}); then run install.py again")
        return None
    ver = output([node, "--version"]).lstrip("v")
    if not ver or int(ver.split(".")[0]) < NODE_MIN:
        report("WARN", "Node.js", f"{ver or '?'} is too old: the Browser tool needs Node {NODE_MIN} or newer ({how})")
        return None
    if not osutil.npm():
        report("WARN", "Node.js", f"{ver} at {node}, but npm is missing (it normally comes with Node.js)")
        return None
    report("OK", "Node.js", f"{ver} at {node}")
    return node


def setup_search(skip):
    name = "Web search server (WebSearch, WebFetch)"
    if skip:
        return report("OK", name, "skipped (--no-search)")
    custom = config.get().get("search_python")
    venv = config.TOOLS / "search-venv"
    py = config.expand(custom) if custom else osutil.venv_python(venv)
    lock = DEPS / "search.lock"
    test = [py, "-c", "import ddgs, httpx, mcp, trafilatura, playwright, pypdf"]
    ok = py.exists() and works(test)
    pinned = custom or stamp_of(venv) == fingerprint(lock)
    if ok and pinned:
        report("OK", name, str(py))
    elif CHECK_ONLY or custom:
        if ok:
            report("OK", name, f"{py} (not the pinned versions in deps/search.lock: python install.py pins them)")
        else:
            return report("WARN", name, f"not installed ({py})" + ("" if custom else ": run python install.py"))
    else:
        try:
            py = make_venv(venv)
            pip_locked(py, venv, lock)
            if not works(test):
                raise RuntimeError("the packages installed but do not import")
            report("OK", name, f"{py} (pinned: deps/search.lock)")
        except Exception as e:  # noqa: BLE001
            return report("WARN", name, f"{e}. The agent works without web search; run install.py again to retry.")
    # WebFetch falls back to a real browser for pages that refuse plain downloads: Chrome or Edge if installed. Without
    # either, Playwright's own Chromium would be close to 1 GB, so it is left as a choice.
    if osutil.browser_channel() == "chromium":
        report("INFO", "WebFetch browser fallback", "no Chrome or Edge: pages that refuse plain downloads will fail. "
               f"Optional: install Chrome, or run `{py} -m playwright install chromium` (about 1 GB)")


def chromium_dirs(node, pw_cli):
    """Where Playwright keeps the Chromium builds this Playwright version wants."""
    out = output([node, pw_cli, "install", "--dry-run", "chromium"], timeout=60)
    return [Path(ln.split(":", 1)[1].strip()) for ln in out.splitlines() if ln.strip().lower().startswith("install location")]


def chromium_ready(node, pw_cli):
    """Is Playwright's own Chromium downloaded?"""
    dirs = chromium_dirs(node, pw_cli)
    return bool(dirs) and all(d.is_dir() for d in dirs)


def chromium_missing_libs(node, pw_cli):
    """Linux: system libraries Playwright's Chromium needs but the system lacks (ldd 'not found')."""
    missing = set()
    for d in chromium_dirs(node, pw_cli):
        for exe in list(d.glob("*/chrome")) + list(d.glob("*/chrome-headless-shell")) + list(d.glob("*/headless_shell")):
            for ln in output(["ldd", exe], timeout=30).splitlines():
                if "not found" in ln:
                    missing.add(ln.split("=>")[0].strip())
    return sorted(missing)


def setup_browser(skip):
    name = "Browser tool (Playwright MCP)"
    if skip:
        return report("OK", name, "skipped (--no-browser)")
    node = check_node()
    if not node:
        return report("WARN", name, "needs Node.js (see the line above)")
    cfg = config.get()
    custom = cfg.get("playwright_mcp")
    cli = config.expand(custom or config.TOOLS / "node_modules" / "@playwright" / "mcp" / "cli.js")
    pj = cli.parent / "package.json"
    pkg, plock = DEPS / "browser" / "package.json", DEPS / "browser" / "package-lock.json"
    want = json.loads(pkg.read_text(encoding="utf-8"))["dependencies"]["@playwright/mcp"]
    if want != mcp.PLAYWRIGHT_MCP:
        report("WARN", name, f"deps/browser pins {want} but the code expects {mcp.PLAYWRIGHT_MCP}: run tools/lock_deps.py")
    have = json.loads(pj.read_text(encoding="utf-8")).get("version") if pj.exists() else None
    channel = cfg.get("browser_channel") or osutil.browser_channel()
    pw_cli = cli.parent.parent.parent / "playwright" / "cli.js"       # the Playwright that came with the MCP
    try:
        fresh = custom or (have == mcp.PLAYWRIGHT_MCP and stamp_of(config.TOOLS) == fingerprint(pkg, plock))
        note = ""
        if not fresh and CHECK_ONLY and have == mcp.PLAYWRIGHT_MCP:     # the right version, put there before deps/browser
            fresh, note = True, " (not from deps/browser's lock yet: python install.py reinstalls it from there)"
        if not fresh:
            if CHECK_ONLY:
                return report("WARN", name, f"not installed (want @playwright/mcp {mcp.PLAYWRIGHT_MCP}): run python install.py"
                              if have is None else f"@playwright/mcp {have}, not the pinned {mcp.PLAYWRIGHT_MCP}: run python install.py")
            config.TOOLS.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pkg, config.TOOLS / "package.json")
            shutil.copyfile(plock, config.TOOLS / "package-lock.json")
            # npm ci: exactly the lock's versions, each tarball checked against its sha512; replaces tools/node_modules
            run([osutil.npm(), "ci", "--prefix", config.TOOLS, "--no-audit", "--no-fund", "--loglevel", "error"])
            (config.TOOLS / ".kch-lock").write_text(fingerprint(pkg, plock), encoding="utf-8")
            have = json.loads(pj.read_text(encoding="utf-8")).get("version")
        if not works([node, cli, "--version"], timeout=60):
            raise RuntimeError(f"{cli} does not run")
        browser = {"chrome": "Google Chrome", "msedge": "Microsoft Edge", "chromium": "Playwright's Chromium"}.get(channel, channel)
        if channel == "chromium" and pw_cli.exists() and not chromium_ready(node, pw_cli):
            if CHECK_ONLY:
                return report("WARN", name, f"@playwright/mcp {have}, but no Chrome or Edge and Playwright's Chromium is not "
                              "downloaded: run python install.py (about 300 MB), or install Chrome")
            run([node, pw_cli, "install", "chromium"])
        libs = chromium_missing_libs(node, pw_cli) if channel == "chromium" and sys.platform.startswith("linux") else []
        if libs:
            return report("WARN", name, f"@playwright/mcp {have}, but Chromium cannot start: the system lacks "
                          f"{', '.join(libs[:6])}{' ...' if len(libs) > 6 else ''}. Install them once with: "
                          f"sudo {node} {pw_cli} install-deps chromium")
        report("OK", name, f"@playwright/mcp {have}, drives {browser}{note}")
    except Exception as e:  # noqa: BLE001
        report("WARN", name, f"{e}. The agent works without the Browser tool; run install.py again to retry.")


def check_subscriptions():
    claude = config.claude_exe()
    report("OK" if claude else "INFO", "Claude plan (Claude Code CLI)", claude if claude else
           "not found (optional: npm i -g @anthropic-ai/claude-code, then run `claude` once to sign in)")
    codex = config.codex_exe()
    report("OK" if codex else "INFO", "ChatGPT plan (Codex CLI)", codex if codex else
           "not found (optional: npm i -g @openai/codex, then run `codex login` once)")


# ---- optional parts ----------------------------------------------------------------------------------------------------
# Each optional part is one setup function taking `wanted` (its --with-NAME flag was given). Without the flag it only
# reports: OK when it is set up, INFO when not. With it, it installs unless --check. Register it in OPTIONAL below; the
# flag, --help text and the run order come from there.

def setup_dictation(wanted):
    name = "Dictation (mic button)"
    c = config.get().get("dictation") or {}
    custom = c.get("python")
    venv = config.TOOLS / "whisper-venv"
    py = config.expand(custom) if custom else osutil.venv_python(venv)
    test = [py, "-c", "import faster_whisper"]
    gpu = sys.platform != "darwin" and bool(osutil.which("nvidia-smi"))
    locks = [DEPS / "dictation.lock"] + ([DEPS / "dictation-cuda.lock"] if gpu else [])
    if py.exists() and works(test):
        note = "" if custom or stamp_of(venv) == fingerprint(*locks) else " (not the pinned versions: python install.py --with-dictation pins them)"
        if not (wanted and note and not CHECK_ONLY):
            models = f"live words {c.get('live_model') or 'small'}, final text {c.get('model') or 'large-v3-turbo'}" if gpu else                 f"live words {c.get('live_model') or 'base'}, final text {c.get('model') or 'small'}"
            return report("OK", name, f"{py} ({models}; models download on first use){note}")
    if not wanted or CHECK_ONLY or custom:
        return report("INFO", name, "not set up (optional: python install.py --with-dictation)")
    try:
        py = make_venv(venv)
        pip_locked(py, venv, *locks)               # on an NVIDIA GPU also NVIDIA's CUDA 12 libraries for CTranslate2
        where = "NVIDIA GPU (live words: small, final text: large-v3-turbo)" if gpu else "CPU (live words: base, final text: small)"
        if not works(test):
            raise RuntimeError("faster-whisper installed but does not import")
        report("OK", name, f"{py}, runs on the {where}")
    except Exception as e:  # noqa: BLE001
        report("WARN", name, str(e))


def setup_meetings(wanted):
    """Meetings (bundled in meetings/): its speech engine at a pinned commit and its own Python 3.12 venv, in
    harness/meetings_setup.py."""
    meetings_setup.setup(wanted, CHECK_ONLY, report, run)


def setup_computer(wanted):
    """Computer use needs nothing installed on Windows and macOS (standard library); X11 needs xdotool and xwd."""
    from harness import desktop
    ok, label = desktop.support()
    if ok:
        return report("OK", "Computer use (screen, mouse, keyboard)", label + ("; macOS asks for Screen Recording and "
                      "Accessibility the first time" if sys.platform == "darwin" else ""))
    report("INFO", "Computer use (screen, mouse, keyboard)", label)


OPTIONAL = [
    # (name for --with-NAME, help text, setup function)
    ("dictation", "also speech to text for the mic button (faster-whisper; 1-3 GB with its model)", setup_dictation),
    ("computer-use", "check computer use (nothing to install on Windows and macOS; Linux X11 needs xdotool and xwd from "
                     "the system's packages)", setup_computer),
    ("meetings", "also Meetings for the Meeting button (live transcript, notes, an assistant per project; about 1.5 GB, "
                 "Python 3.12, Whisper downloads on first use)", setup_meetings),
]


# ---- the self-test -----------------------------------------------------------------------------------------------------

def free_port():
    for port in range(8790, 8890, 2):              # the app also takes port+1 for previews
        ok = True
        for p in (port, port + 1):
            with socket.socket() as s:
                try:
                    s.bind(("127.0.0.1", p))
                except OSError:
                    ok = False
        if ok:
            return port
    raise RuntimeError("no free port between 8790 and 8890")


def self_test():
    """Start the app on a spare port, ask it for its settings, stop it."""
    name = "App starts"
    try:
        port = free_port()
        env = {**os.environ, "KCH_PORT": str(port), "PYTHONUTF8": "1"}
        proc = subprocess.Popen([sys.executable, "-X", "utf8", ROOT / "run.py"], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **osutil.hidden())
        try:
            data, t0 = None, time.time()
            while time.time() - t0 < 30 and proc.poll() is None:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
                        data = json.loads(r.read())["config"]
                    break
                except OSError:
                    time.sleep(0.5)
            if data is None:
                out = proc.stdout.read()[-1500:] if proc.poll() is not None else ""
                raise RuntimeError("it did not answer within 30 s" + (f":\n{out}" if out else ""))
            eps = [e.get("name") for e in data.get("endpoints") or []]
            report("OK", name, "connections: " + (", ".join(eps) if eps else "none yet (add one in Settings > Connections)"))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as e:  # noqa: BLE001
        report("FAIL", name, str(e))


def main():
    global CHECK_ONLY
    ap = argparse.ArgumentParser(description="Set up Flock's tools in KCH_HOME/tools.")
    ap.add_argument("--check", action="store_true", help="report only, install nothing")
    for opt, text, _ in OPTIONAL:
        ap.add_argument(f"--with-{opt}", action="store_true", help=text)
    ap.add_argument("--no-search", action="store_true", help="skip the web search server")
    ap.add_argument("--no-browser", action="store_true", help="skip the Browser tool")
    ap.add_argument("--no-test", action="store_true", help="do not start the app at the end")
    a = ap.parse_args()
    CHECK_ONLY = a.check
    config.load()
    print(f"Flock: {ROOT}\nData folder (KCH_HOME): {config.DATA}\nProfile: {config.PROFILE_PATH}"
          f"{'' if config.PROFILE_PATH.exists() else ' (none: optional)'}\nTools folder: {config.TOOLS}\n", flush=True)
    check_python()
    check_files()
    check_git()
    check_shell()
    setup_search(a.no_search)
    setup_browser(a.no_browser)
    for opt, _, setup in OPTIONAL:
        setup(getattr(a, "with_" + opt.replace("-", "_")))
    check_subscriptions()
    check_updates()
    if not a.no_test:
        self_test()
    failed = [r for r in RESULTS if r[0] == "FAIL"]
    warned = [r for r in RESULTS if r[0] == "WARN"]
    print()
    if failed:
        print("Flock is NOT ready: fix the FAIL lines above and run install.py again.")
        sys.exit(1)
    print("Flock is ready" + (f" ({len(warned)} optional part{'s' if len(warned) != 1 else ''} missing, see WARN above)"
                                   if warned else "") + ".")
    print(f"  Start it:  {Path(sys.executable).stem} run.py --open      (address: http://127.0.0.1:{config.PORT})")
    print("  Then:      Settings > Connections > Add, pick your model (a plan, an API key or a local server).")


if __name__ == "__main__":
    main()
