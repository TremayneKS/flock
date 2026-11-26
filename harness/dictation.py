"""Speak instead of typing. The composer's mic streams 16 kHz audio here while the user talks; a worker process with
faster-whisper transcribes it live (a small fast model, re-run on the growing audio every few tenths of a second, see
stream_policy.py) and the accurate model polishes each settled stretch in the pauses and at the end. The worker runs in its
own Python (one that has faster-whisper), loads on first use (or when the mouse first rests on the mic) and quits after 10
idle minutes so its memory is given back.

profile.json (all optional):
  "dictation": {"enabled": true, "python": "path/to/python with faster-whisper",
                "model": "large-v3-turbo",   accurate model: polish pass and whole clips (default: large-v3-turbo on an
                                             NVIDIA GPU, small on the CPU)
                "live_model": "small",       fast model for the words shown while speaking (default: small on a GPU, base
                                             on the CPU; the same name as "model" loads one model for both)
                "language": "en",            skip language detection (default: detected once per dictation)
                "device": "auto", "local_files_only": false, "env": {"HF_HOME": "..."}, "pre_import": "python code",
                "prompt": "words Whisper should expect (names, jargon)"}
Without one, tools/whisper-venv is used if install.py made it (python install.py --with-dictation).

The audio of a live dictation is kept on disk (DATA/dictation) only while it runs, so a worker that stops mid-dictation
loses nothing: it is started again and carries on from the same audio."""
import array
import json
import math
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, osutil
from .stream_policy import RATE, Stream, accept_polish

_lock = threading.Lock()           # one request at a time to the worker
_proc = None
_warming = threading.Lock()


def cfg():
    return config.get().get("dictation") or {}


def python():
    c = cfg()
    if c.get("enabled") is False:
        return None
    py = config.expand(c["python"]) if c.get("python") else osutil.venv_python(config.TOOLS / "whisper-venv")
    return py if py.exists() else None


def available():
    return python() is not None


def folder():
    d = config.DATA / "dictation"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- the worker process
# Requests carry an id and each answer comes back with it, so the polish pass (answered on the worker's own thread) and the
# live decodes can be in flight at once.
_pending = {}                      # id -> [Event, answer or None]
_ids = iter(range(1, 1 << 62))


def _reader(proc):
    for line in proc.stdout:
        try:
            res = json.loads(line)
        except ValueError:
            continue
        slot = _pending.get(res.get("id", 0))
        if slot:
            slot[1] = res
            slot[0].set()
    for slot in list(_pending.values()):          # it stopped: everyone waiting gets an error
        slot[0].set()


def _worker():
    global _proc
    if _proc is not None and _proc.poll() is None:
        return _proc
    py = python()
    if not py:
        raise RuntimeError("dictation is not set up: run `python install.py --with-dictation`, or name a Python with "
                           "faster-whisper in profile.json (\"dictation\": {\"python\": ...})")
    c = cfg()
    conf = {k: c.get(k) for k in ("model", "live_model", "device", "compute_type", "local_files_only", "pre_import")
            if c.get(k) is not None}
    env = {**os.environ, **{k: str(v) for k, v in (c.get("env") or {}).items()}, "KCH_WHISPER_CFG": json.dumps(conf),
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    ready = [threading.Event(), None]
    _pending[0] = ready                              # the worker's first line, {"ready": true}, has no id
    _proc = subprocess.Popen([str(py), "-X", "utf8", str(Path(__file__).with_name("whisper_worker.py"))], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=open(config.LOGS / "whisper.log", "a", encoding="utf-8"),
                             text=True, encoding="utf-8", bufsize=1, env=env, **osutil.hidden())
    threading.Thread(target=_reader, args=(_proc,), daemon=True).start()
    ready[0].wait(120)
    _pending.pop(0, None)
    if not ready[1]:
        _kill()
        raise RuntimeError("Whisper did not start (see logs/whisper.log)")
    return _proc


def _kill():
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.kill()
    _proc = None


def call(req, timeout=60, slow=False):
    """One request to the worker (started if needed); its answer. slow: the worker runs it on its polish thread.
    Raises RuntimeError if it fails or the worker stops."""
    rid = next(_ids)
    slot = [threading.Event(), None]
    with _lock:
        p = _worker()
        _pending[rid] = slot
        try:
            p.stdin.write(json.dumps({**req, "id": rid, **({"async": True} if slow else {})}) + "\n")
            p.stdin.flush()
        except OSError:
            _pending.pop(rid, None)
            _kill()
            raise RuntimeError("Whisper stopped (see logs/whisper.log)")
    done = slot[0].wait(timeout)
    _pending.pop(rid, None)
    if not done:
        with _lock:
            if _proc is p:
                _kill()                               # ours, and stuck: the next request starts a fresh one
        raise RuntimeError("Whisper did not answer in time")
    res = slot[1]
    if res is None:
        with _lock:
            if _proc is p:
                _kill()
        raise RuntimeError("Whisper stopped (see logs/whisper.log)")
    if res.get("error"):
        raise RuntimeError(res["error"])
    return res


def warm():
    """Load the models in the background (the mouse is on the mic): the first words then show without a wait."""
    if not available() or not _warming.acquire(blocking=False):
        return

    def go():
        try:
            call({"op": "load", "which": "live", "wait": True}, timeout=300)
            call({"op": "load", "which": "final", "wait": False})
        except RuntimeError:
            pass
        finally:
            _warming.release()
    threading.Thread(target=go, daemon=True).start()


def transcribe(audio_bytes, suffix=".webm"):
    """A whole recorded clip (the older mic path, kept for other callers)."""
    f = folder() / f"clip-{int(time.time() * 1000)}{suffix}"
    f.write_bytes(audio_bytes)
    try:
        return call({"file": str(f), "prompt": cfg().get("prompt") or ""}, timeout=600)
    finally:
        try:
            f.unlink()
        except OSError:
            pass


def stop():
    for s in list(_sessions.values()):
        s.cancel()
    _kill()


# ---------------------------------------------------------------- live sessions
STEP = int(0.3 * RATE)             # decode again once this much new audio has come in
ABANDON_S = 30                     # no audio for this long while listening: the page has gone
MAX_S = 30 * 60                    # one dictation at most 30 minutes
KEEP_S = 120                       # a finished dictation's answer stays this long for the page
_sessions = {}
_slock = threading.Lock()


class Session:
    def __init__(self, chat_id=""):
        self.id = secrets.token_hex(8)
        self.chat_id = str(chat_id or "")
        self.path = folder() / f"live-{self.id}.pcm"
        self.file = open(self.path, "wb")
        self.n = 0                       # samples received
        self.levels = []                 # loudness of each 0.1 s, to tell a pause from speech
        self._rest = b""
        self.seq = 0
        self.stream = Stream()
        self.cleaned_text = None         # set after local Gemma 12B cleanup finishes
        self.phase = "loading"           # loading -> live -> finishing -> polishing -> done (or error)
        self.message = "Loading speech model..."
        self.rev = 0
        self.cv = threading.Condition()
        self.stopping = False
        self.cancelled = False
        self.ended_at = None
        self.decoded_to = 0
        self.language = cfg().get("language") or None
        self.t0 = time.time()
        self.last_audio = time.time()
        self.stats = {"decodes": 0, "decode_s": 0.0, "first_text_s": None, "stop_to_text_s": None, "stop_to_done_s": None,
                      "polished": 0, "polish_rejected": 0}
        self.stop_t = None
        self.no_polish = False           # the accurate model failed to load: the live text stands
        self.live_done = False
        threading.Thread(target=self._run, daemon=True, name=f"dictation-{self.id}").start()

    # ---- from the page
    def add(self, data, seq):
        with self.cv:
            if self.stopping or self.live_done or seq < self.seq:
                return
            self.seq = seq + 1
            data = data[: len(data) // 2 * 2]
            self.file.write(data)
            self.file.flush()
            self.n += len(data) // 2
            self._measure(data)
            self.last_audio = time.time()
            if self.n > MAX_S * RATE:
                self.stopping = True
                self.message = "Stopped at 30 minutes"
            self.cv.notify_all()

    def _measure(self, data):
        buf, block = self._rest + data, 3200
        whole = len(buf) // block * block
        for i in range(0, whole, block):
            a = array.array("h", buf[i:i + block])
            if sys.byteorder == "big":
                a.byteswap()
            self.levels.append(math.sqrt(sum(x * x for x in a) / len(a)))
        self._rest = buf[whole:]

    def quiet_since(self):
        """When the audio went quiet, if it is quiet now (seconds), else None. Quiet: under three times the room's own
        noise (the quietest fifth of the last 10 s), and never above about -47 dBFS."""
        lv = self.levels
        if not lv:
            return None
        recent = sorted(lv[-100:])
        floor = recent[len(recent) // 5]
        limit = min(150.0, max(3 * floor, 40.0))
        i = len(lv)
        while i > 0 and lv[i - 1] < limit:
            i -= 1
        return None if i == len(lv) else i * 0.1

    def stop(self):
        with self.cv:
            if not self.stopping:
                self.stopping = True
                self.stop_t = time.time()
            self.cv.notify_all()

    def cancel(self):
        with self.cv:
            self.cancelled = self.stopping = True
            self.cv.notify_all()

    def wait(self, rev=None, phases=None, timeout=3.0):
        """Block until the text changes (rev), or the dictation reaches one of `phases`, or timeout."""
        end = time.time() + timeout
        with self.cv:
            while time.time() < end:
                if phases and self.phase in phases:
                    break
                if not phases and rev is not None and self.rev != rev:
                    break
                if not phases and rev is None:
                    break
                self.cv.wait(max(0.01, end - time.time()))
            return self.state()

    def state(self):
        with self.cv:
            return self._state()

    def _state(self):
        s = self.stream
        settled = self.cleaned_text if self.cleaned_text is not None else s.settled_text()
        return {"id": self.id, "phase": self.phase, "message": self.message, "rev": self.rev,
                "settled": settled, "tentative": "" if self.cleaned_text is not None else s.tentative_text(),
                "audio_s": round(self.n / RATE, 2), "stats": self.stats}

    # ---- the work
    def _set(self, phase=None, message=None):
        with self.cv:
            if phase:
                self.phase = phase
            if message is not None:
                self.message = message
            self.rev += 1
            self.cv.notify_all()

    def _prompt(self):
        base = (cfg().get("prompt") or "").strip()
        before = " ".join(r.text() for r in self.stream.regions)[-200:]
        return (base + " " + before).strip()

    def _decode(self, which, start, end, prompt, tries=3):
        """One decode, starting the worker again if it stopped (the audio is on disk, so nothing is lost)."""
        for attempt in range(tries):
            try:
                res = call({"op": "decode", "which": which, "pcm": str(self.path), "start": start, "end": end,
                            "prompt": prompt, "language": self.language}, timeout=120, slow=which == "final")
                if attempt:
                    self._set(message="")
                return res
            except RuntimeError:
                if attempt == tries - 1:
                    raise
                self._set(message="The speech engine stopped. Starting it again; keep talking, nothing is lost...")
                time.sleep(0.5 + attempt)
        return None

    def _live(self, final=False):
        with self.cv:
            end = self.n
            start = int(self.stream.offset * RATE)
        self.decoded_to = end
        t = time.time()
        res = self._decode("live", start, end, self._prompt())
        self.stats["decodes"] += 1
        self.stats["decode_s"] += time.time() - t
        off = start / RATE
        words = [(off + a, off + b, w) for a, b, w in res.get("words") or []]
        if not self.language and (res.get("prob") or 0) >= 0.8 and end - start >= 2 * RATE:
            self.language = res.get("language")
        with self.cv:
            quiet = self.quiet_since()
            changed = self.stream.finish(words, end / RATE) if final else self.stream.insert(words, end / RATE, quiet)
            if (final or changed) and self.stats["first_text_s"] is None and (self.stream.settled_text() or self.stream.tentative_text()):
                self.stats["first_text_s"] = round(time.time() - self.t0, 2)
        if final or changed:
            self._set()

    def _final_ready(self, wait_s=0.0):
        end = time.time() + wait_s
        while True:
            try:
                st = call({"op": "status"}, timeout=10)
            except RuntimeError:
                return False
            if st.get("final") == "ready":
                return True
            if st.get("final") != "loading":
                self.no_polish = True
                return False
            if time.time() >= end or self.cancelled:
                return False
            time.sleep(0.3)

    def _polish(self, region):
        with self.cv:
            prev = [r.text() for r in self.stream.regions if r is not region and r.end <= region.start]
        base = (cfg().get("prompt") or "").strip()
        prompt = (base + " " + " ".join(prev)[-200:]).strip()
        res = self._decode("final", int(region.start * RATE), int(region.end * RATE), prompt, tries=1)
        text = "".join(w for _a, _b, w in res.get("words") or []).strip()
        with self.cv:
            if accept_polish(region.live, text):
                region.polished = text
                self.stats["polished"] += 1
            else:
                region.polished = region.live
                self.stats["polish_rejected"] += 1
        self._set()

    def _run(self):
        """The live pass: decode the growing audio every STEP while the user speaks, then once more after Stop."""
        threading.Thread(target=self._polisher, daemon=True, name=f"dictation-polish-{self.id}").start()
        try:
            try:
                call({"op": "load", "which": "live", "wait": True}, timeout=300)
                call({"op": "load", "which": "final", "wait": False})
            except RuntimeError as e:
                self._set("error", f"The speech model did not load: {e}")
                return
            self._set("live", "")
            while True:
                with self.cv:
                    while not self.stopping and self.n - self.decoded_to < STEP:
                        self.cv.wait(0.5)
                        if time.time() - self.last_audio > ABANDON_S:
                            self.stopping = True
                    if self.stopping:
                        break
                self._live()
            if self.cancelled:
                return
            self._set("finishing", "Finishing...")
            try:
                self._live(final=True)
            except RuntimeError as e:
                with self.cv:
                    self.stream.give_up_tentative()
                self._set("error", f"Speech engine stopped: {e}. The text so far is kept.")
                return
            if self.stop_t:
                self.stats["stop_to_text_s"] = round(time.time() - self.stop_t, 2)
            with self.cv:
                more = bool(self.stream.unpolished()) and not self.no_polish
            self._set("polishing" if more else None, "Polishing..." if more else None)   # the page has all the words now
        except Exception as e:  # noqa: BLE001
            (config.LOGS / "errors.log").open("a", encoding="utf-8").write(time.strftime("%F %T ") + f"dictation: {e!r}\n")
            with self.cv:
                self.stream.give_up_tentative()
            self._set("error", f"Dictation stopped: {e}. The text so far is kept.")
        finally:
            with self.cv:
                self.live_done = True
                try:
                    self.file.close()
                except OSError:
                    pass
                self.cv.notify_all()

    def _polisher(self):
        """The accurate pass, beside the live one: each stretch that ends in a pause, then the rest after Stop."""
        try:
            while True:
                with self.cv:
                    while True:
                        if self.cancelled or self.phase == "error":
                            return
                        todo = self.stream.unpolished() if not self.no_polish else []
                        if self.live_done and not todo:
                            break
                        if todo and (self.live_done or any(r.pause for r in todo)):
                            break
                        self.cv.wait(0.5)
                    final = self.live_done
                    if not todo:
                        break
                if not self._final_ready(wait_s=20 if final else 0):
                    if final:
                        break                                # not loaded in time: the live text stands
                    with self.cv:
                        self.cv.wait(0.5)
                    continue
                with self.cv:
                    group = self.stream.ready_group(final=final)
                if group is not None:
                    self._polish(group)
        except Exception as e:  # noqa: BLE001  (the live text stands)
            (config.LOGS / "errors.log").open("a", encoding="utf-8").write(time.strftime("%F %T ") + f"dictation polish: {e!r}\n")
        finally:
            with self.cv:
                while not self.live_done and not self.cancelled:     # an error above: wait for the live pass to end
                    self.cv.wait(0.5)
            if self.stop_t and self.phase != "error":
                self.stats["stop_to_done_s"] = round(time.time() - self.stop_t, 2)
            if self.phase != "error" and not self.cancelled:
                self._set("done", "")
            with self.cv:
                self.ended_at = time.time()
                self.cv.notify_all()

    def remove(self):
        try:
            self.path.unlink()
        except OSError:
            pass


def _sweep():
    now = time.time()
    with _slock:
        for sid, s in list(_sessions.items()):
            if s.ended_at and now - s.ended_at > KEEP_S:
                _sessions.pop(sid).remove()
    for f in folder().glob("live-*.pcm"):                 # left by a crash
        try:
            if f.stem[5:] not in _sessions and now - f.stat().st_mtime > 3600:
                f.unlink()
        except OSError:
            pass


def start(chat_id=""):
    if not available():
        raise RuntimeError("Dictation is not set up on this computer")
    _sweep()
    with _slock:
        if sum(1 for s in _sessions.values() if not s.ended_at) >= 4:
            raise RuntimeError("Too many dictations at once")
        s = Session(chat_id=chat_id)
        _sessions[s.id] = s
    return s.state()


def get(sid):
    s = _sessions.get(sid or "")
    if not s:
        raise FileNotFoundError("That dictation has ended")
    return s


def _json_body(handler, limit=65536):
    raw = handler._body(limit)
    if not raw:
        return {}
    try:
        d = json.loads(raw.decode("utf-8"))
        return d if isinstance(d, dict) else {}
    except (UnicodeDecodeError, ValueError):
        return {}


def http_post(handler, path, q):
    """/api/dictation/start | warm | clean | cleanup-config | <id>/audio?seq=N | <id>/stop | <id>/poll?rev=N | <id>/cancel"""
    parts = path.split("/")
    act = parts[3] if len(parts) > 3 else ""
    if act == "start":
        b = _json_body(handler, 4096)
        return handler._json(start(chat_id=b.get("chat") or ""))
    if act == "warm":
        handler._body(4096)
        warm()
        return handler._json({"ok": available()})
    s = get(act)
    sub = parts[4] if len(parts) > 4 else ""
    if sub == "audio":
        s.add(handler._body(2 * 1024 * 1024), int(q.get("seq") or 0))
        return handler._json(s.state())
    handler._body(4096)
    if sub == "stop":
        s.stop()
        return handler._json(s.wait(phases=("polishing", "done", "error"), timeout=15))
    if sub == "poll":
        rev = int(q["rev"]) if q.get("rev") not in (None, "") else None
        if s.ended_at:
            return handler._json(s.state())
        return handler._json(s.wait(rev=rev, timeout=3))
    if sub == "cancel":
        s.cancel()
        return handler._json({"ok": True})
    raise ValueError("unknown dictation request")
