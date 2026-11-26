"""A meetings app inside Flock (optional). It is a separate local web app (live transcript, notes and an assistant
per project); the Meetings button starts it if needed and shows it in the main area through an iframe
(URL ?embed=1&theme=dark|light&project=<name of the open project>&model=<connection id>&model_name=<its name>).

Its assistant uses the model picked in Flock: Flock hands Meetings its local model endpoint and token
(llm.py) when it starts it (environment) or finds it running (POST /api/harness/link, again whenever Meetings has
restarted), and the window tells the frame when the picker changes. Meetings never gets the connections' keys.

Meetings ships in this repository (meetings/). `python install.py --with-meetings` gives it its own Python and speech
engine in FLOCK_HOME/tools, and then it is found with no profile at all: data in FLOCK_HOME/meetings, port 8770. A profile
can change that, or describe any other meetings app:

profile.json (all optional):
  "meetings": {"enabled": true, "label": "Meetings", "port": 8770, "data": "~/Meetings",       (the bundled app)
               "settings": "~/meetings-settings.json",      its settings.json (user name, Whisper model, ...)
               "url": "http://127.0.0.1:8770", "start": ["python", "~/meetings/app.py", "serve"], "cwd": "~/meetings",
               "env": {"NAME": "value"},                    (another app: url + start replace the bundled one)
               "app": "meetings"}                           (it is the bundled Meetings app: something else on its port is an error)
"""
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, llm, osutil, probe

BUNDLED = config.APP / "meetings"                 # bundled Meetings code
SERVER_NAMES = ("FlockMeetings", "NestMeetings", "KCMeetings")    # publish-scan: ok (Server header, including older installs without /api/hello)
SERVER_NAME = SERVER_NAMES[0]
BUNDLED_APP_IDS = ("meetings", "kc-meetings")
START_WAIT = 90                                   # seconds; the first start after a reboot can be slow (antivirus, disk)
_start_lock = threading.Lock()


def venv():
    return config.TOOLS / "meetings-venv"


def engine_dir():
    return config.TOOLS / "meetings-engine"


def installed():
    """install.py --with-meetings has run: the app's Python and the speech engine are in FLOCK_HOME/tools."""
    return osutil.venv_python(venv()).exists() and (BUNDLED / "server.py").exists() and (engine_dir() / "meeting").is_dir()


def cfg():
    prof = config.get().get("meetings") or {}
    if prof.get("url") or prof.get("start"):
        return prof                                # a hand-written setup for some other meetings app
    if (config.PROFILE.get("meetings") or {}).get("enabled") is False:    # (the built-in default says False too: ignore it)
        return {"enabled": False}
    if not installed():
        return {"enabled": False, "missing": True}
    port = int(prof.get("port") or 8770)
    py = str(osutil.venv_python(venv()))
    data_dir = str(config.expand(prof.get("data") or config.DATA / "meetings"))
    env = {"FLOCK_MEETINGS_ROOT": data_dir, "KC_MEETINGS_ROOT": data_dir,
           "FLOCK_MEETINGS_PORT": str(port), "KC_MEETINGS_PORT": str(port),
           "FLOCK_MEETINGS_PYTHON": py, "KC_MEETINGS_PYTHON": py,
           "FLOCK_MEETINGS_ENGINE": str(engine_dir()), "KC_MEETINGS_ENGINE": str(engine_dir())}
    if prof.get("settings"):
        settings_path = str(config.expand(prof["settings"]))
        env["FLOCK_MEETINGS_SETTINGS"] = settings_path
        env["KC_MEETINGS_SETTINGS"] = settings_path
    env.update(link_env())
    return {"enabled": True, "app": "meetings", "label": prof.get("label") or "Meetings", "url": f"http://127.0.0.1:{port}",
            "start": [py, "-X", "utf8", str(BUNDLED / "server.py")], "cwd": str(BUNDLED),
            "env": {**env, **(prof.get("env") or {})}}


def link_env():
    """How Meetings reaches Flock's model endpoint (llm.py). Sets legacy env vars too for older Meetings installs."""
    url, tok = llm.base_url(), llm.token()
    return {
        "FLOCK_URL": url, "FLOCK_TOKEN": tok,
        "NEST_URL": url, "NEST_TOKEN": tok,
        "RAVEN_URL": url, "RAVEN_TOKEN": tok,
        "KC_HARNESS_URL": url, "KC_HARNESS_TOKEN": tok,
    }


def _selected():
    eid = config.get().get("endpoint")
    ep = next((e for e in config.get()["endpoints"] if e["id"] == eid), None)
    return {"id": eid, "name": ep.get("name") or eid} if ep else None


def _link(hello):
    """Meetings found running: give it the model endpoint and token, unless it already has this Flock's."""
    have = hello.get("harness") if isinstance(hello.get("harness"), dict) else None
    if have is None and "harness" not in hello:
        return                                          # an older Meetings install that cannot use Flock's model
    if have and have.get("url") == llm.base_url() and have.get("key") == llm.fingerprint():
        return
    body = json.dumps({"url": llm.base_url(), "token": llm.token(), "model": _selected()}).encode("utf-8")
    req = urllib.request.Request(base() + "/api/harness/link", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Flock-App": "1", "X-KC-App": "1"})
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except (urllib.error.URLError, OSError, ValueError):
        pass


def enabled():
    c = cfg()
    return bool(c.get("enabled", True) and c.get("url"))


def label():
    return cfg().get("label") or "Meetings"


def base():
    return (cfg().get("url") or "").rstrip("/")


def port():
    return urllib.parse.urlsplit(base()).port or 80


def origins():
    """Origins the window may frame: the configured one, under both loopback names."""
    if not enabled():
        return []
    u = urllib.parse.urlsplit(base())
    out = {f"{u.scheme}://{u.netloc}"}
    if u.hostname in ("127.0.0.1", "localhost"):
        p = f":{u.port}" if u.port else ""
        out |= {f"{u.scheme}://127.0.0.1{p}", f"{u.scheme}://localhost{p}"}
    return sorted(out)


def status():
    """Probe now (and refresh the cache)."""
    return _probe.refresh() or {"enabled": enabled(), "running": False, "projects": [], "url": base(), "label": label()}


def status_cached():
    """The last answer at once (the window's state call); a stale one is refreshed in the background."""
    if not enabled():
        return {"enabled": False, "running": False, "projects": []}
    st = _probe.get(wait=0.3) or {"enabled": True, "running": False, "projects": [], "url": base(), "label": label(),
                                  "checking": True}
    return {**st, "starting": _start_lock.locked()}


def _get(path, timeout):
    with urllib.request.urlopen(base() + path, timeout=timeout) as r:
        return r.headers.get("Server") or "", json.loads(r.read() or b"{}")


def _status():
    if not enabled():
        return {"enabled": False, "running": False, "projects": []}
    out = {"enabled": True, "running": False, "projects": [], "url": base(), "label": label()}
    hello = {}
    try:
        server, hello = _get("/api/hello", 4)
        ours = hello.get("app") in BUNDLED_APP_IDS or any(s in server for s in SERVER_NAMES)
    except urllib.error.HTTPError as e:            # an older Meetings install has no /api/hello but says its name
        hdr = e.headers.get("Server") or ""
        ours = any(s in hdr for s in SERVER_NAMES)
    except (urllib.error.URLError, OSError, ValueError) as e:
        if "timed out" in str(getattr(e, "reason", e)):
            out["not_answering"] = True              # running but stuck, or still starting
        return out
    if not ours and cfg().get("app") in BUNDLED_APP_IDS:
        return {**out, "conflict": True}             # something else has Meetings' port
    out["running"] = True                            # (another meetings app: answering at its address is enough)
    if ours:
        _link(hello if isinstance(hello, dict) else {})
    try:                                             # project names are a nicety: never fail on them
        _, data = _get("/api/projects", 10)
        rows = data.get("projects") if isinstance(data, dict) else data
        out["projects"] = [p.get("name") for p in rows or [] if isinstance(p, dict)]
    except Exception:
        pass
    return out


_probe = probe.Cached(_status, ttl=10)


def _command():
    c = cfg()
    cmd = [str(config.expand(a)) if (i == 0 or str(a).startswith("~")) else str(a) for i, a in enumerate(c.get("start") or [])]
    if not cmd:
        raise RuntimeError(f"{label()} is not running and the profile has no start command for it")
    exe = cmd[0]
    if not (Path(exe).exists() or shutil.which(exe)):
        raise RuntimeError(f"{label()} is not installed here ({Path(exe).name} was not found). "
                           "Run: python install.py --with-meetings")
    for a in cmd[1:]:
        if a.endswith(".py") and not Path(a).exists() and not (c.get("cwd") and (config.expand(c["cwd"]) / a).exists()):
            raise RuntimeError(f"{label()} is not installed here ({a} was not found)")
    return cmd


def _log_tail(path, since):
    try:
        with open(path, "rb") as f:
            f.seek(since)
            lines = [ln.strip() for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return lines[-1][:300] if lines else ""


def start():
    """Start the app if it is not running, and wait until it answers. One start at a time: a second click (or a second
    window) waits for the first start instead of launching another copy."""
    if not enabled():
        if cfg().get("missing"):
            raise RuntimeError("Meetings is not installed. Run: python install.py --with-meetings")
        raise RuntimeError("no meetings app is set up (profile.json, \"meetings\")")
    with _start_lock:
        st = status()
        if st["running"]:
            return st
        if st.get("conflict"):
            raise RuntimeError(f"Port {port()} is used by another program, so {label()} cannot start there. Close that "
                               "program, or give the meetings app another port (profile.json, \"meetings\": {\"port\": ...}).")
        if st.get("not_answering"):
            raise RuntimeError(f"{label()} is running but not answering. Wait a moment and try again; if it stays stuck, "
                               "stop it and press Meetings again.")
        cmd = _command()
        c = cfg()
        cwd = str(config.expand(c["cwd"])) if c.get("cwd") else None
        env = {**os.environ, **{k: str(config.expand(v)) if str(v).startswith("~") else str(v) for k, v in (c.get("env") or {}).items()},
               "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        for k in [k for k in env if k.startswith(("CLAUDE", "ANTHROPIC"))]:
            env.pop(k)                               # its assistant signs in as the user, not as whatever started the harness
        if c.get("app") in BUNDLED_APP_IDS:
            env.update(link_env())                   # its assistant can use the model picked here (llm.py)
        logf = config.LOGS / "meetings.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        since = logf.stat().st_size if logf.exists() else 0
        with open(logf, "ab") as log:
            log.write(time.strftime("\n%F %T starting: ").encode() + " ".join(cmd).encode("utf-8", "replace") + b"\n")
            log.flush()
            # Out of the harness's job object where Windows allows it, so restarting the harness never ends a live meeting.
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=cwd, env=env,
                                        **osutil.detached(breakaway=True))
            except OSError:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=cwd, env=env, **osutil.detached())
        t0 = time.time()
        while time.time() - t0 < START_WAIT:
            time.sleep(0.5)
            code = proc.poll()
            st = status()
            if st["running"]:
                return {**st, "started": True}
            if code not in (None, 0):           # 0: a launcher that handed over to the app and left
                tail = _log_tail(logf, since)
                raise RuntimeError(f"{label()} stopped while starting" + (f": {tail}" if tail else f" (exit code {code})")
                                   + ". More in logs/meetings.log.")
        raise RuntimeError(f"{label()} did not start within {START_WAIT} seconds. More in logs/meetings.log.")
