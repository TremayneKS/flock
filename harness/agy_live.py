"""agy's own files beside its stdout, for a turn that runs background work (a remote GPU job, a timer loop).

Three facts about agy print mode (Antigravity CLI 1.5), each reproduced on a scratch conversation on 22 Nov 2026:

- stdout carries the steps in step order, and a background command or a timer stays ACTIVE until it ends, so nothing
  the model does after it (its words, its checks, its next timer) reaches stdout until then. In the GPU chat Neon
  wrote status updates to the user at 10:17, 10:43 and 10:51 and the chat showed none of them. agy also writes each
  step, as it finishes, to brain/<conversation>/.system_generated/logs/transcript.jsonl: the turn follows that file
  and shows the model's words from it while stdout is held (Transcript).
- ending the agy process ends every command it started: agy holds them in a job object, and killing agy.exe alone
  (no tree kill) stopped a background command at once. So cutting a turn to hand it the user's message ends its jobs,
  and a remote job whose controller dies is reaped as an orphan by the next caller.
- agy reads an inbox, brain/<conversation>/.system_generated/messages/<id>.json with an undelivered/<id> marker, at its
  next wake (a timer firing, a task ending, the next step), and the model answers it inside the same turn. A message
  the user sends while background work runs goes in there instead, and the turn is not cut (send_inbox).

Standard library only.
"""
import calendar
import json
import os
import time
import uuid


def sysdir(root, conv):
    return os.path.join(root, conv, ".system_generated") if conv else None


class Transcript:
    """New entries of transcript.jsonl as agy writes them. Started at the end of the file for a conversation that is
    being resumed (its earlier steps were shown long ago), at the start for a new one."""

    def __init__(self, path, from_end=True):
        self.path = path
        self.buf = b""
        try:
            self.pos = os.path.getsize(path) if from_end else 0
        except OSError:
            self.pos = 0

    def new(self):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.pos:                        # rewritten from scratch: read it again
            self.pos, self.buf = 0, b""
        if size == self.pos:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
        except OSError:
            return []
        self.pos += len(data)
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        out = []
        for ln in lines:
            try:
                e = json.loads(ln.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out


def idx_of(entry):
    try:
        return int(entry.get("step_index"))
    except (TypeError, ValueError):
        return None


def words(entry):
    """What the model wrote to the user in this step ('' for a step that is only tool calls or not the model's)."""
    if entry.get("type") != "PLANNER_RESPONSE" or entry.get("source") != "MODEL":
        return ""
    c = entry.get("content")
    return c.strip() if isinstance(c, str) else ""


def _arg(v):
    s = str(v if v is not None else "").strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    return s


def timers(entry):
    """Epoch times the timers set in this step fire (schedule calls: created_at + DurationSeconds)."""
    out = []
    try:
        t0 = calendar.timegm(time.strptime(str(entry.get("created_at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return out
    calls = entry.get("tool_calls")
    if not isinstance(calls, list):
        return out
    for c in calls:
        if isinstance(c, dict) and c.get("name") == "schedule":
            try:
                out.append(t0 + float(_arg((c.get("args") or {}).get("DurationSeconds"))))
            except (TypeError, ValueError):
                pass
    return out


def send_inbox(sd, conv, text, title="Message from the user"):
    """Leave a message in the conversation's inbox; agy hands it to the model at its next wake. Returns its id."""
    mdir = os.path.join(sd, "messages")
    os.makedirs(os.path.join(mdir, "undelivered"), exist_ok=True)
    mid = str(uuid.uuid4())
    msg = {"id": mid, "recipient": conv, "sender": "flock-user", "priority": "MESSAGE_PRIORITY_HIGH",
           "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime()),
           "renderDetails": {"messageTitle": title}, "content": text}
    tmp = os.path.join(mdir, mid + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(msg, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(mdir, mid + ".json"))
    with open(os.path.join(mdir, "undelivered", mid), "w"):
        pass
    return mid


def delivered(sd, mid):
    return not os.path.exists(os.path.join(sd, "messages", "undelivered", mid))


def withdraw(sd, mid):
    """Take back a message agy has not read (its process ended first). True when it was still undelivered, so the
    caller hands the text to the next turn instead; False when agy had already taken it."""
    try:
        os.remove(os.path.join(sd, "messages", "undelivered", mid))
    except OSError:
        return False
    try:
        os.remove(os.path.join(sd, "messages", mid + ".json"))
    except OSError:
        pass
    return True
