"""Dictation worker: run with a Python that has faster-whisper. Reads one JSON request per line on stdin and answers each
with one JSON line on stdout that carries the request's "id". Requests sent with "async": true (the polish pass) run on a
thread of their own and may be answered out of order, so they never hold up the live words. Exits after 10 idle minutes
so the GPU (or CPU) memory is given back.
Settings come from KCH_WHISPER_CFG (see dictation.py).

Two models: "live" (small and fast, decoded every few tenths of a second while the user speaks) and "final" (accurate, for
the polish pass and for whole clips). They are the same model when both names are the same.

Requests:
  {"op": "load", "which": "live" | "final", "wait": true|false}   load a model (wait false: in the background)
  {"op": "status"}                                                  {"live": "ready"|"loading"|"none"|"error: ...", "final": ...}
  {"op": "decode", "which": "live"|"final", "pcm": path, "start": sample, "end": sample, "prompt": "...",
   "language": "en"|null, "beam": 1, "words": true}               {"words": [[s, e, text]], "language", "prob", "secs"}
     pcm is 16 kHz mono 16-bit little-endian; times in the answer are seconds from `start`.
  {"file": path, "prompt": "..."}                                  a whole clip with the final model: {"text", "secs", "language"}"""
import glob
import json
import os
import queue
import site
import sys
import threading
import time

CFG = json.loads(os.environ.get("KCH_WHISPER_CFG") or "{}")
out = sys.stdout
sys.stdout = sys.stderr            # libraries that print must not break the protocol

if os.name == "nt":                # faster-whisper's CUDA libraries from the nvidia-* wheels, when they are installed
    roots = list(site.getsitepackages()) + [site.getusersitepackages()]
    for d in [d for r in roots for d in glob.glob(os.path.join(r, "nvidia", "*", "bin"))]:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
if CFG.get("pre_import"):          # a profile's own setup (e.g. another app's CUDA loader); the profile is the user's file
    exec(compile(CFG["pre_import"], "<profile dictation.pre_import>", "exec"), {})

import numpy as np  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

IDLE = 600
last = time.time()
models = {}                        # which -> WhisperModel
state = {}                         # which -> "loading" | "ready" | "error: ..."
cond = threading.Condition()
_send = threading.Lock()


def send(obj):
    with _send:
        out.write(json.dumps(obj) + "\n")
        out.flush()


def has_cuda():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001
        return False


def name_of(which):
    final = CFG.get("model") or ("large-v3-turbo" if has_cuda() else "small")
    if which == "final":
        return final
    return CFG.get("live_model") or ("small" if has_cuda() else "base")


def build(name):
    local = bool(CFG.get("local_files_only"))
    device = CFG.get("device") or "auto"
    if device in ("auto", "cuda"):
        for compute in ([CFG["compute_type"]] if CFG.get("compute_type") else ["float16", "int8_float16"]):
            try:
                return WhisperModel(name, device="cuda", compute_type=compute, local_files_only=local)
            except Exception as e:  # noqa: BLE001
                print(f"GPU {compute} for {name} failed: {e}", file=sys.stderr)
        if device == "cuda":
            raise RuntimeError("CUDA was asked for and did not load")
    return WhisperModel(name, device="cpu", compute_type="int8", local_files_only=local)


def load(which):
    """The model for `which`, loading it here if nobody is loading it yet; waits for a load running elsewhere."""
    with cond:
        if which == "live" and name_of("live") == name_of("final"):
            which = "final"
        while state.get(which) == "loading":
            cond.wait()
        if state.get(which) == "ready":
            return models[which]
        state[which] = "loading"
    try:
        t0 = time.time()
        m = build(name_of(which))
        print(f"{which} model {name_of(which)} loaded in {time.time() - t0:.1f}s", file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001
        with cond:
            state[which] = f"error: {e.__class__.__name__}: {e}"
            cond.notify_all()
        raise
    with cond:
        models[which], state[which] = m, "ready"
        cond.notify_all()
    return m


def status():
    same = name_of("live") == name_of("final")
    return {"live": state.get("final" if same else "live", "none"), "final": state.get("final", "none"),
            "live_model": name_of("live"), "final_model": name_of("final")}


def read_pcm(path, start, end):
    with open(path, "rb") as f:
        f.seek(int(start) * 2)
        raw = f.read(max(0, int(end) - int(start)) * 2)
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


def decode(req):
    which = req.get("which") or "live"
    model = load(which)
    audio = read_pcm(req["pcm"], req.get("start", 0), req["end"])
    t1 = time.time()
    if audio.size < 1600:                       # under 0.1 s
        return {"words": [], "secs": 0}
    final = which == "final"
    # a cut-off last word can send Whisper into a loop ("stop and ____...") until 448 tokens: speech is at most ~7 tokens a second
    cap = min(440, int(audio.size / 16000 * 7) + 16)
    segs, info = model.transcribe(
        audio, language=req.get("language") or None, beam_size=int(req.get("beam") or (5 if final else 1)),
        word_timestamps=bool(req.get("words", True)), vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
        condition_on_previous_text=False, initial_prompt=req.get("prompt") or None, without_timestamps=False,
        no_speech_threshold=0.6, log_prob_threshold=-1.0, temperature=0.0 if not final else [0.0, 0.2, 0.4],
        max_new_tokens=cap)
    words = []
    for s in segs:
        if s.no_speech_prob > 0.6 and s.avg_logprob < -0.8:      # Whisper's guess at speech in noise
            continue
        if s.words:
            words += [[round(w.start, 3), round(w.end, 3), w.word] for w in s.words if any(c.isalnum() for c in w.word)]
        elif s.text.strip():
            words.append([round(s.start, 3), round(s.end, 3), " " + s.text.strip()])
    return {"words": words, "language": info.language, "prob": round(info.language_probability or 0, 3),
            "secs": round(time.time() - t1, 3)}


def clip(req):
    model = load("final")
    t1 = time.time()
    segs, info = model.transcribe(req["file"], language=req.get("language") or None, vad_filter=True, beam_size=1,
                                  initial_prompt=req.get("prompt") or None)
    text = " ".join(s.text.strip() for s in segs).strip()
    return {"text": text, "secs": round(time.time() - t1, 2), "language": info.language}


def handle(req):
    op = req.get("op") or ("clip" if req.get("file") else "")
    if op == "load":
        which = req.get("which") or "live"
        if req.get("wait", True):
            load(which)
        else:
            def bg(w=which):
                try:
                    load(w)
                except Exception:  # noqa: BLE001  (recorded in state)
                    pass
            threading.Thread(target=bg, daemon=True).start()
        return status()
    if op == "status":
        return status()
    if op == "decode":
        return decode(req)
    if op == "clip":
        return clip(req)
    return {"error": f"unknown request {op!r}"}


def answer(req):
    global last
    try:
        res = handle(req)
    except Exception as e:  # noqa: BLE001
        res = {"error": f"{e.__class__.__name__}: {e}"}
    if req.get("id") is not None:
        res["id"] = req["id"]
    send(res)
    last = time.time()


def main():
    global last
    send({"ready": True, "cuda": has_cuda()})
    slow = queue.Queue()

    def slow_lane():
        while True:
            answer(slow.get())
    threading.Thread(target=slow_lane, daemon=True).start()

    def idle_watch():
        while True:
            time.sleep(15)
            if time.time() - last > IDLE and slow.empty():
                os._exit(0)
    threading.Thread(target=idle_watch, daemon=True).start()
    for line in sys.stdin:
        last = time.time()
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if req.get("async"):
            slow.put(req)
        else:
            answer(req)


if __name__ == "__main__":
    main()
