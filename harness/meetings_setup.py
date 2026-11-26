"""install.py's part for Meetings (the Meetings button): its speech engine and its own Python, in FLOCK_HOME/tools.

  tools/meetings-engine   the speech engine's source (OpenWhisper, MIT; live capture, Whisper, voice prints), pinned   publish-scan: ok
  tools/meetings-venv     Python 3.12 with the engine's packages at the engine's own pinned versions (+ CUDA 12 on NVIDIA)

Meetings' own code is meetings/ in this repository. Models are not downloaded here: Whisper (1.6 GB on a GPU,
480 MB on the CPU) and the voice model (26 MB) download the first time a meeting or an import needs them, with progress
shown in the app. The assistant needs Claude Code signed in (`claude auth login`).
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import config, meetings, osutil

ENGINE_REPO = "https://github.com/Knuckles92/OpenWhisper"                  # publish-scan: ok
ENGINE_TAG = "v2.8.3"
ENGINE_COMMIT = "4b09b889d16e7991c3d24b811f774be436ebe9c4"
ENGINE_ZIP_SHA256 = "74799bd21928f00afb5ef11a54efe9d985cd5cb510447185aa887f42c329a643"
NAME = "Meetings"


def _python312():
    """A Python 3.12 to build the venv from (the engine supports 3.12 only). Not the Microsoft Store Python on Windows:
    everything it starts sees a private copy of AppData, which hides the engine's voice model from the app."""
    def ok(exe):
        if not exe:
            return None
        try:
            out = subprocess.run([str(exe), "-c", "import sys; print(sys.version_info[:2] == (3, 12), sys.executable)"],
                                 capture_output=True, text=True, timeout=60).stdout.split(maxsplit=1)
        except (OSError, subprocess.TimeoutExpired):
            return None
        good = out and out[0] == "True" and "WindowsApps" not in out[1]
        return out[1].strip() if good else None

    for exe in (sys.executable, osutil.which("python3.12"), osutil.which("python3")):
        if ok(exe):
            return ok(exe)
    if osutil.WINDOWS and osutil.which("py"):
        found = subprocess.run(["py", "-3.12", "-c", "import sys; print(sys.executable)"], capture_output=True, text=True).stdout.strip()
        if ok(found):
            return ok(found)
    uv = osutil.which("uv")
    if uv:
        found = subprocess.run([uv, "python", "find", "3.12"], capture_output=True, text=True).stdout.strip()
        if not ok(found):
            subprocess.run([uv, "python", "install", "3.12"])
            found = subprocess.run([uv, "python", "find", "3.12"], capture_output=True, text=True).stdout.strip()
        if ok(found):
            return ok(found)
    return None


def _rmtree(path):
    """Delete a folder, including git's read-only files (Windows refuses those otherwise)."""
    import stat

    def unlock(fn, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        fn(p)
    if Path(path).exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=unlock)
        else:
            shutil.rmtree(path, onerror=unlock)


def _fetch_engine(target, run):
    """The engine at its pinned commit: fetched by commit with git when there is git, else the release zip (checked
    against its hash)."""
    tmp = Path(tempfile.mkdtemp(prefix="kch-engine-", dir=target.parent))
    try:
        src = tmp / "engine"
        git = osutil.which("git")
        if git:
            # safe.directory: on drives that record no owner (exFAT, some network drives) git refuses the folder otherwise
            g = [git, "-c", "safe.directory=*", "-c", "advice.detachedHead=false", "-C", src]
            src.mkdir()
            run(g + ["init", "--quiet"])
            run(g + ["fetch", "--quiet", "--depth", "1", ENGINE_REPO, ENGINE_COMMIT])
            run(g + ["checkout", "--quiet", "FETCH_HEAD"])
            head = subprocess.run([str(x) for x in g] + ["rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            if head != ENGINE_COMMIT:
                raise RuntimeError(f"the speech engine checkout is {head[:12] or 'missing'}, expected {ENGINE_COMMIT[:12]}; not using it")
            _rmtree(src / ".git")
        else:
            print(f"      downloading {ENGINE_REPO}/archive/refs/tags/{ENGINE_TAG}.zip (52 MB)", flush=True)
            with urllib.request.urlopen(f"{ENGINE_REPO}/archive/refs/tags/{ENGINE_TAG}.zip", timeout=600) as r:
                data = r.read()
            if hashlib.sha256(data).hexdigest() != ENGINE_ZIP_SHA256:
                raise RuntimeError("the speech engine download does not match its pinned hash; not using it (install git and retry)")
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extractall(tmp)
            top = next(p for p in tmp.iterdir() if p.is_dir() and p.name != "engine")
            top.rename(src)
        (src / "KC_PINNED.json").write_text(json.dumps({"repo": ENGINE_REPO, "tag": ENGINE_TAG, "commit": ENGINE_COMMIT}))
        _rmtree(target)
        src.rename(target)
    finally:
        _rmtree(tmp)


def _pinned(engine):
    try:
        return json.loads((engine / "KC_PINNED.json").read_text()).get("commit") == ENGINE_COMMIT
    except (OSError, ValueError):
        return False


def _import_error(py):
    """'' when the app's packages import, else the last line of the error."""
    test = "import faster_whisper, onnxruntime, sounddevice, av, PIL, numpy, sqlalchemy"
    try:
        p = subprocess.run([str(py), "-c", test], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if p.returncode == 0:
        return ""
    last = (p.stderr.strip().splitlines() or ["(no message)"])[-1]
    if "PortAudio" in last:
        last += ". Linux: sudo apt install libportaudio2 (Fedora: portaudio), then run install.py --with-meetings again"
    return last


def _imports(py):
    return not _import_error(py)


def _devices(py):
    """Ask the capture worker for the microphones, as the app does (no audio is opened)."""
    env = {**os.environ, "KC_MEETINGS_ENGINE": str(meetings.engine_dir()), "QT_QPA_PLATFORM": "offscreen", "PYTHONUTF8": "1"}
    try:
        out = subprocess.run([str(py), "-X", "utf8", str(meetings.BUNDLED / "capture_host.py"), "--devices"], env=env,
                             capture_output=True, text=True, timeout=120, cwd=str(meetings.BUNDLED)).stdout
        line = next((ln for ln in out.splitlines() if ln.startswith("{")), "")
        return json.loads(line) if line else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def setup(wanted, check_only, report, run):
    engine, venv = meetings.engine_dir(), meetings.venv()
    py = osutil.venv_python(venv)
    own = config.get().get("meetings") or {}
    if (own.get("url") or own.get("start")) and own.get("enabled", True) is not False:
        return report("OK", NAME, f"the profile's own meetings app at {own.get('url') or '(no url)'}; the bundled one is not used")
    if not (meetings.BUNDLED / "server.py").exists():
        return report("WARN", NAME, f"{meetings.BUNDLED} is missing from this copy of Flock")
    if py.exists() and _pinned(engine) and _imports(py):
        devices = _devices(py)
        mics = f"{len(devices.get('mics') or [])} microphone(s)" if devices else "microphones not listed (see the app)"
        return report("OK", NAME, f"{venv}, {mics}; Whisper and the voice model download on first use"
                      + ("" if config.claude_exe() else ". The assistant needs Claude Code: npm i -g @anthropic-ai/claude-code, then claude auth login"))
    if not wanted or check_only:
        return report("INFO", NAME, "not set up (optional: python install.py --with-meetings; about 1.5 GB, plus Whisper on first use)")
    try:
        config.TOOLS.mkdir(parents=True, exist_ok=True)
        if not _pinned(engine):
            print(f"      fetching the speech engine {ENGINE_TAG} into {engine}", flush=True)
            _fetch_engine(engine, run)
        if not py.exists():
            base = _python312()
            if not base:
                raise RuntimeError("needs Python 3.12 for its speech engine. Install it (python.org, or `uv python install 3.12`; "
                                   "on Windows not the Microsoft Store one) and run install.py --with-meetings again")
            run([base, "-m", "venv", venv])
            run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"])
        pins = ["-c", engine / "requirements-release-constraints.txt"]
        run([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", engine / "requirements.txt", *pins, "pillow"])
        if sys.platform != "darwin" and osutil.which("nvidia-smi"):       # NVIDIA: CUDA 12 runtime libraries for Whisper
            run([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", engine / "requirements-gpu.txt", *pins])
        problem = _import_error(py)
        if problem:
            raise RuntimeError(f"its packages installed but do not import: {problem}")
        devices = _devices(py)
        where = "an NVIDIA GPU" if osutil.which("nvidia-smi") and sys.platform != "darwin" else "the CPU (Whisper small)"
        report("OK", NAME, f"{venv}, Whisper on {where}; "
               + (f"{len(devices.get('mics') or [])} microphone(s)" if devices else "microphones not listed yet")
               + (". The assistant needs Claude Code: npm i -g @anthropic-ai/claude-code, then claude auth login"
                  if not config.claude_exe() else ""))
    except Exception as e:  # noqa: BLE001
        report("WARN", NAME, f"{e}. The rest of Flock works without it; run install.py --with-meetings again to retry.")
