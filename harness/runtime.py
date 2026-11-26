"""A launcher for the model server behind one connection. Optional: it is described in profile.json under "runtime"
(examples/profile.example.json shows a local llama-server and a cloud-GPU script).

The chat only ever talks to the connection's endpoint. When the profile says how to start that endpoint's server, the
harness can start it when a message needs it, show its progress, switch between choices (e.g. GPU types, each with a
cost per hour) and stop it. The launcher runs fully detached, so closing or restarting the harness never kills it
mid-run; any later harness picks it up again from state/runtime.json.

Fields (all but command optional): endpoint, label, command (list; {placeholders} come from settings, choice, stop_file),
cwd, settings + setting_labels (editable in Settings > Runtime), choices [{id, label, rate, speed, note}] + choice,
unit (what rate counts, e.g. "compute units"), stop ("kill" the process tree, or "file": write {stop_file} and let the
launcher shut itself down cleanly), ready / failed / stopping / ended / ignore (regexes on its output), progress
[[regex, text], ...], warm (send two tiny prompts once it is up: some GPUs compile kernels on first use), start_note,
keep_up (start it again when it ends without a Stop). Only Stop stops it: picking another choice while it runs does not.

remote (for a launcher that rents something elsewhere, e.g. a cloud GPU): label (where, e.g. "Cloud GPU"), id (regex on the
launcher's output; group 1 is the remote runtime's id), status / stop / orphans (commands; {remote_id} and the settings
are filled in): status exits 0 while that runtime is up, 1 once it is gone, anything else = cannot tell; stop ends it
(exit 0 = gone); orphans prints the ids of runtimes the launcher left up with no launcher. check_min: how often to look.
With it, a launcher that dies (killed from outside, a crash) no longer makes the app forget its GPU: the run record is
kept until the remote side says the runtime is gone, the window shows "still running without its launcher" with Stop,
and keep_up starts a new one only after the old one is confirmed gone (never two at once).
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

from . import config, osutil, probe

STATE = config.STATE
INFO = STATE / "runtime.json"
STOP = STATE / "runtime.stop"
# CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB (not DETACHED_PROCESS: the launcher's own
# console helpers would then each open a visible console window). The middle process exits at once, so the launcher has
# no living parent in the app's process tree and a `taskkill /T` of the app never reaches it.
LAUNCH = (
    "import json, os, subprocess, sys\n"
    "a = json.loads(sys.argv[1])\n"
    "f = open(a['log'], 'w', encoding='utf-8')\n"
    "kw = dict(stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=a['cwd'] or None)\n"
    "if os.name == 'nt':\n"
    "    try:\n"
    "        p = subprocess.Popen(a['cmd'], creationflags=0x08000000 | 0x00000200 | 0x01000000, **kw)\n"
    "    except OSError:\n"
    "        p = subprocess.Popen(a['cmd'], creationflags=0x08000000 | 0x00000200, **kw)\n"
    "else:\n"
    "    p = subprocess.Popen(a['cmd'], start_new_session=True, **kw)\n"
    "open(a['pid'], 'w').write(str(p.pid))\n"
)
alive = osutil.alive
RUNNING = ("starting", "warming", "ready")


def _same(info):
    """The launcher in info is still running (and is not a stranger that got its process number since)."""
    info = info or {}
    return osutil.same_process(info.get("pid"), info.get("created") or info.get("started"))


def cfg():
    return config.get().get("runtime") or {}


def remote():
    r = cfg().get("remote")
    return r if isinstance(r, dict) and (r.get("stop") or r.get("status")) else {}


def where():
    return remote().get("label") or "the remote side"


def enabled():
    c = cfg()
    return bool(c.get("enabled") and c.get("command") and c.get("endpoint"))


def serves(endpoint_id):
    return enabled() and cfg().get("endpoint") == endpoint_id


def _rx(c, key, default=None):
    pat = c.get(key, default)
    try:
        return re.compile(pat, re.I) if pat else None
    except re.error:
        return None


def _fill(c, args, **extra):
    """A profile command with its {placeholders} filled in. No shell runs it, so ~ is expanded here."""
    values = {**{k: str(v) for k, v in (c.get("settings") or {}).items()}, "stop_file": str(STOP), "python": sys.executable,
              **{k: str(v) for k, v in extra.items()}}
    out = [re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), str(a)) for a in args]
    return [str(config.expand(a)) if a.startswith("~") else a for a in out]


def _remote_cmd(key, rid="", timeout=300):
    """Run the profile's remote status / stop / orphans command: (exit code or None, its output)."""
    c, r = cfg(), remote()
    if not r.get(key):
        return None, ""
    try:
        p = subprocess.run(_fill(c, r[key], remote_id=rid), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL,
                           cwd=str(config.expand(c["cwd"])) if c.get("cwd") else None,
                           env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, **osutil.hidden())
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)


def remote_up(rid):
    """True while the remote runtime is up, False once it is gone, None when the status command cannot tell."""
    if not remote().get("status"):
        return None
    for i in range(3):
        code, _ = _remote_cmd("status", rid, timeout=120)
        if code in (0, 1):
            return code == 0
        time.sleep(3 * (i + 1))
    return None


def remote_orphans():
    """Ids the profile's orphans command reports (runtimes up with no launcher), or None if it is missing or failed."""
    code, out = _remote_cmd("orphans", timeout=180)
    if code != 0:
        return None
    return [ln.strip() for ln in out.splitlines() if re.fullmatch(r"[\w.:@+-]+", ln.strip())]


class Runtime:
    def __init__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "logs").mkdir(exist_ok=True)
        self.lock = threading.RLock()     # re-entrant: start() reports status() while holding it
        self.busy = threading.Lock()      # one end-check / remote stop at a time
        self._up = probe.Cached(self._endpoint_up, ttl=5, default=False)
        self.info = {}
        self.orphans = {}                 # remote id -> {choice, started, log}: up on the remote side, its launcher gone
        self.checking = False             # a launcher just ended: asking the remote side whether its runtime is gone
        self.state, self.detail, self.ready_at = "off", "", None
        self.lines, self.pos = [], 0
        self.replaying = False
        self.stop_asked = False            # set by stop(): an end the user asked for is never undone by keep_up
        self.auto_restarts = 0
        if enabled():
            self._load()
        threading.Thread(target=self._watch, daemon=True).start()
        threading.Thread(target=self._sweeper, daemon=True).start()

    # ---- the run record (state/runtime.json): the launcher, plus remote runtimes still up without one
    def _save(self):
        with self.lock:
            data = dict(self.info)
            if self.orphans:
                data["orphans"] = self.orphans
        try:
            if data:
                tmp = INFO.with_suffix(".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                os.replace(tmp, INFO)
            elif INFO.exists():
                INFO.unlink()
        except OSError:
            pass

    def _load(self):
        try:
            data = json.loads(INFO.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.orphans = data.pop("orphans", None) or {}
        self.info = data
        if self.info and not _same(self.info):
            self._replay()            # finished while no harness was watching: read how it ended
            dead, self.info = self.info, {}
            if remote():
                self._orphan(dead)    # keep its record until the remote side says the runtime is gone
                self.checking = True
                self.state, self.detail = "checking", f"its launcher has ended; checking the GPU on {where()} is gone"
                threading.Thread(target=self._after_end, args=(dead, "off", None, False), daemon=True).start()
            else:
                self._save()
        elif self.info:
            self.state, self.detail = "starting", "picking up a running server"
            self._replay()

    def _orphan(self, dead):
        """Note the dead launcher's remote runtime as possibly still up (saved at once: the app may close meanwhile)."""
        rid = dead.get("remote")
        if not rid:
            if not remote().get("orphans"):
                return                  # no id and no way to look one up: nothing to keep
            rid = f"?{dead.get('pid')}"  # not seen in its output yet: the orphans command finds it
        with self.lock:
            self.orphans[rid] = {"choice": dead.get("choice"), "started": dead.get("started"), "log": dead.get("log"),
                                 **({"unknown_id": True} if rid.startswith("?") else {})}
        self._save()

    def _replay(self):
        log = self.info.get("log")
        if not log or not os.path.exists(log):
            return
        self.pos = 0
        self.replaying = True             # an earlier harness already warmed it up
        self._read_log()
        self.replaying = False
        if self.state == "warming":
            self.state, self.detail = "ready", "ready"

    def endpoint_up(self):
        """Probe now (start and stop decisions). status() uses the cached answer: the window must not wait for it."""
        return self._up.refresh()

    def _endpoint_up(self):
        if not enabled():
            return False
        from . import providers
        try:
            return bool(providers.health(config.endpoint(cfg()["endpoint"]), timeout=2).get("ok"))
        except Exception:
            return False

    def status(self):
        c = cfg()
        if not enabled():
            return {"enabled": False, "state": "off"}
        with self.lock:
            st, detail = self.state, self.detail
            running = _same(self.info)
            orphans = [{"id": k, "choice": v.get("choice"), "started": v.get("started")} for k, v in self.orphans.items()]
        if orphans and not running and st not in ("stopping", "checking"):
            st = "orphaned"
            detail = (detail if self.state == "orphaned" and detail else
                      f"A GPU is still running on {where()} without its launcher. Press Stop to end it.")
        if not running and st in ("off", "failed") and self._up.get(wait=0.3):
            st = "external"           # started by hand, outside the harness
        choices = {ch["id"]: ch for ch in c.get("choices") or [] if isinstance(ch, dict) and ch.get("id")}
        choice = self.info.get("choice") if running else next((o["choice"] for o in orphans if o["choice"]), None) or c.get("choice")
        ch = choices.get(choice or "", {})
        since = self.info.get("started") or self.ready_at       # cloud GPUs bill from allocation: count from the start
        if not running and orphans:
            since = min((o["started"] for o in orphans if o["started"]), default=None)
        up_h = ((time.time() - since) / 3600) if (since and st in ("ready", "warming", "starting", "stopping", "orphaned", "checking")) else 0
        return {"enabled": True, "label": c.get("label") or "Model server", "endpoint": c.get("endpoint"), "state": st,
                "choice": choice, "choice_label": ch.get("label") or choice, "rate": ch.get("rate"), "unit": c.get("unit") or "",
                "detail": detail, "up_min": round(up_h * 60, 1), "cost": round(up_h * (ch.get("rate") or 0), 2),
                "since": since, "log": self.info.get("log"), "choices": choices, "orphans": orphans,
                "remote": where() if remote() else None,
                "settings": c.get("settings") or {}, "setting_labels": c.get("setting_labels") or {},
                "start_note": c.get("start_note") or "", "tail": self.lines[-14:]}

    # ---- start / stop
    def _command(self, c, choice):
        return _fill(c, c["command"], choice=choice or "")

    def start(self, choice=None):
        c = cfg()
        if not enabled():
            raise RuntimeError("no model-server launcher is set up (profile.json, \"runtime\")")
        choices = [ch.get("id") for ch in c.get("choices") or []]
        choice = choice or c.get("choice") or (choices[0] if choices else "")
        if choices and choice not in choices:
            raise ValueError(f"unknown choice {choice}; the profile offers {', '.join(choices)}")
        with self.lock:
            if _same(self.info):
                if self.info.get("choice") == choice:
                    return self.status()
                raise RuntimeError("it is already running with another choice; switch stops it first")
            if self.checking:
                raise RuntimeError(f"the last run just ended; still checking its GPU on {where()} is gone. Try again in a minute.")
            if self.orphans:
                raise RuntimeError(f"a GPU from the last run is still up on {where()} without its launcher. Press Stop to "
                                   "end it first, so two are never billed at once.")
        if self.endpoint_up():
            raise RuntimeError("something already answers on the endpoint (a server started by hand?)")
        if STOP.exists():
            STOP.unlink()
        log = STATE / "logs" / f"runtime-{choice or 'server'}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        pidf = STATE / "runtime.pid"
        if pidf.exists():
            pidf.unlink()
        cmd = self._command(c, choice)
        cwd = str(config.expand(c["cwd"])) if c.get("cwd") else ""
        arg = json.dumps({"cmd": cmd, "log": str(log), "pid": str(pidf), "cwd": cwd})
        subprocess.run([sys.executable, "-c", LAUNCH, arg], timeout=60, **osutil.hidden(),
                       env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        for _ in range(50):
            if pidf.exists() and pidf.read_text().strip():
                break
            time.sleep(0.1)
        pid = int(pidf.read_text().strip())
        with self.lock:
            self.info = {"pid": pid, "created": osutil.created(pid), "choice": choice, "log": str(log), "started": time.time()}
            self.state, self.detail, self.ready_at, self.lines, self.pos = "starting", "starting", None, [], 0
            self.stop_asked = False
        self._save()
        if choice and choice != c.get("choice"):
            config.update(lambda x: x["runtime"].update(choice=choice))
        return self.status()

    def stop(self, wait=False):
        c = cfg()
        self.stop_asked = True
        with self.lock:
            pid = self.info.get("pid")
            running = _same(self.info)
        if running or self.endpoint_up():
            if (c.get("stop") or "kill") == "file":
                STOP.write_text("stop", encoding="utf-8")      # the launcher ends its loop, releases what it holds, exits
            elif running:
                osutil.kill_tree(pid)
            with self.lock:
                self.state, self.detail = "stopping", "stopping"
        if remote() and (self.orphans or not running):
            # a GPU whose launcher is gone (or one Stop must look for): end it on the remote side
            with self.lock:
                self.state = "stopping"
                self.detail = f"stopping the GPU on {where()}" if self.orphans else f"looking for a GPU left running on {where()}"
            threading.Thread(target=self._stop_remote, daemon=True).start()
        if wait:
            t0 = time.time()
            while _same(self.info) and time.time() - t0 < 300:
                time.sleep(1)
        return self.status()

    def _stop_remote(self):
        """Stop pressed with no launcher to tell: find and end what is left on the remote side, until it is confirmed gone."""
        if not self.busy.acquire(timeout=900):
            return
        try:
            if not self.orphans or any(v.get("unknown_id") for v in self.orphans.values()):
                self._sweep(add_only=False)
            if not self.orphans:
                with self.lock:
                    if self.state == "stopping" and not _same(self.info):
                        self.state, self.detail = "off", f"stopped; nothing of the harness's is running on {where()}"
                return
            self._stop_orphans()
        finally:
            self.busy.release()

    def _stop_orphans(self):
        """End each runtime left without a launcher; each one stays on the list until the remote side says it is gone."""
        with self.lock:
            self.state, self.detail = "stopping", f"stopping the GPU on {where()}"
        errors = []
        for rid in [k for k, v in self.orphans.items() if not v.get("unknown_id")]:
            code, out = _remote_cmd("stop", rid, timeout=900)
            gone = remote_up(rid) is False if remote().get("status") else code == 0
            if gone:
                with self.lock:
                    self.orphans.pop(rid, None)
            else:
                errors.append("asked it to stop, but could not check it is gone" if code == 0 else
                              (out.splitlines() or [f"exit {code}"])[-1][:160])
        self._save()
        for _ in range(10):             # its tunnel may answer for a moment after the GPU went: say "stopped" once it is quiet
            if not self.endpoint_up():
                break
            time.sleep(1)
        with self.lock:
            if self.orphans:
                self.state = "orphaned"
                self.detail = (f"Could not confirm the GPU on {where()} stopped ({'; '.join(errors) or 'no answer'}). "
                               "Press Stop to try again.")
            elif not _same(self.info):
                self.state, self.detail = "off", f"stopped; {where()} confirms the GPU is gone"
                self.lines = (self.lines + [self.detail])[-80:]

    def switch(self, choice):
        """Stop the current one cleanly, then start the new choice. The endpoint keeps its address."""
        def go():
            self.stop(wait=True)
            t0 = time.time()
            while (self.checking or self.orphans or self.busy.locked()) and time.time() - t0 < 900:
                time.sleep(2)           # the old GPU must be confirmed gone first: never two at once
            for _ in range(60):
                if not self.endpoint_up():
                    break
                time.sleep(2)
            try:
                self.start(choice)
            except Exception as e:
                with self.lock:
                    self.state, self.detail = "failed", str(e)
        threading.Thread(target=go, daemon=True).start()
        label = (next((ch for ch in cfg().get("choices") or [] if ch.get("id") == choice), {}) or {}).get("label", choice)
        self.detail = f"switching to {label}"
        return self.status()

    def _again(self, choice):
        """Restart after an end nobody asked for. One harness at a time (several may watch the same state folder)."""
        time.sleep(8)
        lock = STATE / "restart.lock"
        try:
            if lock.exists() and time.time() - lock.stat().st_mtime > 900:
                lock.unlink()
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
        except OSError:
            return                                  # another harness is restarting it
        try:
            if self.stop_asked or STOP.exists() or self.orphans:
                return
            try:
                other = json.loads(INFO.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                other = None
            if other and _same(other):              # another harness got there first: watch that one
                with self.lock:
                    other.pop("orphans", None)
                    self.info, self.state, self.detail, self.pos = other, "starting", "picking up a running server", 0
                return
            self.start(choice)
        except Exception as e:
            with self.lock:
                self.state, self.detail = "failed", f"restart failed: {e}"[:240]
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    def _warm(self):
        """Some GPUs (A100, T4, H100 with llama.cpp) compile kernels on the first real prompt (~50 s). Send one long-ish
        and one short throwaway prompt at once, so the user's first message is quick."""
        t0 = time.time()
        try:
            ep = config.endpoint(cfg()["endpoint"])
            key = config.read_key(ep)
            filler = " ".join(f"Line {i}: the harness warms up the prompt and decode kernels of the GPU." for i in range(260))
            for n in (1, 2):
                body = {"model": ep.get("model") or "default", "max_tokens": 24, "messages": [
                    {"role": "user", "content": (filler if n == 1 else "Say OK.")}]}
                if ep.get("llama"):
                    body["chat_template_kwargs"] = {"enable_thinking": False}
                req = urllib.request.Request(ep["base"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + key} if key else {})})
                with urllib.request.urlopen(req, timeout=600) as r:
                    r.read()
        except Exception as e:
            self.lines = (self.lines + [f"warm-up failed: {e}"])[-80:]
        with self.lock:
            if self.state == "warming":
                self.state, self.detail = "ready", "ready"
                self.lines = (self.lines + [f"warm-up done in {time.time() - t0:.0f} s"])[-80:]

    def _became_ready(self):
        if cfg().get("warm") and not self.replaying:
            self.state, self.detail = "warming", "warming up"
            threading.Thread(target=self._warm, daemon=True).start()
        else:
            self.state, self.detail = "ready", "ready"
        self.ready_at = self.ready_at or time.time()
        self.auto_restarts = 0

    # ---- the launcher's end
    def _end_state(self, was, c, was_detail=""):
        """State after a launcher ended (and its remote side, if any, is confirmed gone)."""
        if was in ("starting", "warming", "ready", "stopping", "checking", "off"):
            self.state = "off" if was in ("stopping", "ready", "warming", "checking", "off") else "failed"
            if self.state == "off":
                ended = _rx(c, "ended")
                self.detail = next((ln for ln in reversed(self.lines) if ended and ended.search(ln)), "stopped")
        else:
            self.state, self.detail = was, was_detail or self.detail      # e.g. failed: keep its own last error

    def _after_end(self, dead, was, choice, restart, was_detail=""):
        """A launcher with a remote side ended: keep its record until the remote runtime is confirmed gone (stopping it
        when a Stop was asked for or keep_up wants a new one), and only then start again."""
        c = cfg()
        with self.busy:
            try:
                self._confirm()
                asked = self.stop_asked or STOP.exists()
                if self.orphans and (asked or restart):
                    self._stop_orphans()        # keep_up: the old one must be gone before a new one starts
                with self.lock:
                    self.checking = False
                    if self.orphans:
                        if self.state != "orphaned":
                            self.state, self.detail = "orphaned", ""
                        self.lines = (self.lines + [f"its launcher ended, but the GPU on {where()} is still up"])[-80:]
                        return
                    self._end_state(was, c, was_detail)
                    if was in ("stopping", "checking") or asked:
                        self.detail = f"stopped; {where()} confirms the GPU is gone"
                    if restart and not (self.stop_asked or STOP.exists()):
                        self.detail = "ended by itself; starting it again"
                    else:
                        restart = False
                if restart:
                    for _ in range(30):         # the old tunnel may answer for a moment after its VM went
                        if not self.endpoint_up():
                            break
                        time.sleep(2)
                    self._again(choice)
            finally:
                self.checking = False

    def _confirm(self):
        """Ask the remote side about each runtime on the list; drop those it says are gone."""
        for rid, o in list(self.orphans.items()):
            if o.get("unknown_id"):
                ids = remote_orphans()
                if ids is not None:
                    with self.lock:
                        self.orphans.pop(rid, None)
                        for i in ids:
                            self.orphans.setdefault(i, {k: v for k, v in o.items() if k != "unknown_id"})
                continue
            if remote_up(rid) is False:
                with self.lock:
                    self.orphans.pop(rid, None)
        self._save()
        with self.lock:
            if not self.orphans and self.state == "orphaned":
                self.state, self.detail = "off", f"stopped; {where()} confirms the GPU is gone"
                self.lines = (self.lines + [self.detail])[-80:]

    def _sweep(self, add_only=False):
        """Re-check the runtimes on the list and ask for any the launcher left up that the list does not know."""
        if not add_only:
            self._confirm()
        ids = remote_orphans()
        if ids:
            with self.lock:
                mine = self.info.get("remote")
                for i in ids:
                    if i != mine and i not in self.orphans:
                        self.orphans[i] = {"choice": None, "started": None, "log": None, "found": time.time()}
                        self.lines = (self.lines + [f"found a GPU on {where()} with no launcher: {i}"])[-80:]
            self._save()

    def _sweeper(self):
        """At start and every few minutes while a run is expected: catch runtimes left up without their launcher."""
        time.sleep(5)
        first = True
        while True:
            try:
                if enabled() and remote() and not self.checking and not self.busy.locked():
                    if first or self.info or self.orphans or self._up.get():
                        self._sweep()
            except Exception:
                pass
            first = False
            time.sleep(60 * float(remote().get("check_min") or 5))

    # ---- log reading
    def _watch(self):
        while True:
            time.sleep(1.5)
            if not self.info:
                continue
            self._read_log()
            c = cfg()
            if self.state == "starting" and not _rx(c, "ready") and self.endpoint_up():
                with self.lock:
                    self._became_ready()       # no ready pattern: up as soon as the endpoint answers
            if not _same(self.info):
                again, check = None, None
                with self.lock:
                    was, choice, dead = self.state, self.info.get("choice"), dict(self.info)
                    self.info = {}
                    # "keep_up": it runs until the user presses Stop. Ended any other way (the cloud reclaimed it, the PC
                    # slept too long, a crash, killed from outside): start it again. Three failed starts in a row and it gives up.
                    asked = self.stop_asked or STOP.exists()        # the stop file: a Stop from any harness sharing this state
                    restart = bool(c.get("keep_up") and not asked and was in RUNNING and self.auto_restarts < 3)
                    if restart:
                        self.auto_restarts += 1
                        bad = _rx(c, "failed", r"traceback")              # the launcher's own last error line, if any
                        last = next((ln for ln in reversed(self.lines) if bad and bad.search(ln)), self.detail)
                        self.lines = (self.lines + [f"it ended without a Stop ({last[:160]}); "
                                                    + ("checking its GPU is gone, then starting it again" if remote() else "starting it again")])[-80:]
                    if remote():
                        check = (dead, was, choice, restart, self.detail)
                        self.checking = True
                        self.state = "stopping" if asked else "checking"
                        self.detail = (f"stopping; making sure the GPU on {where()} is gone" if asked else
                                       f"its launcher ended; checking the GPU on {where()} is gone")
                    else:
                        if was in RUNNING or was == "stopping":
                            self._end_state(was, c)
                        if restart:
                            again = choice
                            self.detail = "ended by itself; starting it again"
                if check:
                    self._orphan(dead)          # the record stays (as an orphan) until the remote side says it is gone
                    threading.Thread(target=self._after_end, args=check, daemon=True).start()
                else:
                    self._save()
                if again is not None:
                    threading.Thread(target=self._again, args=(again,), daemon=True).start()

    def _read_log(self):
        log = self.info.get("log")
        if not log:
            return
        try:
            with open(log, encoding="utf-8", errors="replace") as f:
                f.seek(self.pos)
                chunk = f.read()
                self.pos = f.tell()
        except OSError:
            return
        for line in chunk.splitlines():
            self._line(line)

    def _line(self, line):
        c = cfg()
        clean = re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
        ignore = _rx(c, "ignore")
        if ignore:
            clean = ignore.sub("", clean).strip()
        if not clean:
            return
        self.lines = (self.lines + [clean[:300]])[-80:]
        rid = _rx(remote(), "id")
        if rid and self.info and not self.info.get("remote"):
            m = rid.search(clean)
            if m:
                with self.lock:
                    self.info["remote"] = m.group(1) if m.groups() else m.group(0)
                self._save()           # known before the GPU bills: a killed launcher can no longer hide it
        ready, failed, stopping, ended = _rx(c, "ready"), _rx(c, "failed", r"traceback"), _rx(c, "stopping"), _rx(c, "ended")
        with self.lock:
            if ready and ready.search(clean):
                self._became_ready()
            elif failed and failed.search(clean) and self.state in ("starting", "warming"):
                self.state, self.detail = "failed", clean[:240]      # once it serves, only its end changes the state
            elif stopping and stopping.search(clean):
                self.state, self.detail = "stopping", clean[:200]
            elif self.state == "starting":
                for pat, text in c.get("progress") or []:
                    try:
                        if re.search(pat, clean, re.I):
                            self.detail = text
                            break
                    except re.error:
                        continue
            if ended and ended.search(clean):
                self.detail = clean[:200]


runtime = Runtime()
