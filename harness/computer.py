"""The Computer tool: any model in the app sees the user's screen and, once the user allows it, uses the mouse and
keyboard. Shapes follow Claude's computer use (screenshot, clicks, type, key, scroll, zoom...). The desktop itself is
desktop.py (Windows, macOS, X11).

Permission, in the app's window (web/computer.js):
- Nothing touches the mouse or keyboard until the user answers "<model> wants to control your mouse and keyboard" with
  Allow once (this reply) or Allow for this chat ("always ask" in Settings > Tools offers only Allow once) or Deny.
- Screenshots alone get a lighter question the first time in a chat (they can show anything on the screen).
- While a model controls the computer the window shows a banner with a big Stop. Stop, Esc in the app, or Esc anywhere
  (Windows and macOS: the physical key, polled while it runs) stops the turn at once, lets go of any held key or button
  and takes the permission back.
Grants live in memory, per chat (a subagent shares its top chat's), so a restart of the app asks again.

Every model receives screenshots natively as images.
Claude Code and Codex get the same tool through servers/computer.py (stdio MCP), which calls back to /api/computer/call
with a token kept in memory, so their calls go through the same permission and Stop."""
import json
import re
import secrets
import shlex
import threading
import time
from pathlib import Path

from . import config, desktop, resume, store, tools

DEFAULTS = {"enabled": True, "mode": "chat", "max_width": 1280, "max_height": 800}
MODES = ("chat", "ask")              # chat: Allow once / Allow for this chat / Deny; ask: Allow once / Deny every time
TOKEN = resume.local_token("computer")    # proves a call to /api/computer/call comes from an MCP server this app started (kept across restarts)
ASK_TIMEOUT = None                   # wait for the user's answer as long as it takes; Stop ends it
CLI_TOOL_WAIT_S = 7 * 24 * 3600     # what Claude Code / Codex are told a computer tool call may take (they need a number)
LOOK = {"screenshot", "zoom"}
CONTROL = {"mouse_move", "left_click", "right_click", "middle_click", "double_click", "triple_click", "left_click_drag",
           "left_mouse_down", "left_mouse_up", "scroll", "type", "key", "hold_key"}
FREE = {"wait", "cursor_position"}
ACTIONS = ["screenshot", "zoom", "left_click", "double_click", "triple_click", "right_click", "middle_click", "mouse_move",
           "left_click_drag", "left_mouse_down", "left_mouse_up", "scroll", "type", "key", "hold_key", "cursor_position", "wait"]
NAME = "Computer"
MCP_TOOL = "computer"

DESCRIPTION = (
    "See the user's screen and, once they allow it, use their mouse and keyboard in any app. The call waits until the user "
    "answers; they can stop you at any time. Only for things that need an app on screen: use the other tools for files, "
    "commands and pages you build. Take a screenshot first, act with coordinates from it, then take another to check.\n"
    "Actions: screenshot (display: which screen, 0 = main), zoom (region [x0, y0, x1, y1]: a closer look), left_click, "
    "double_click, triple_click, right_click, middle_click (coordinate [x, y]; text: keys to hold, e.g. \"ctrl\"), "
    "mouse_move, left_click_drag (start_coordinate to coordinate), left_mouse_down, left_mouse_up, scroll (coordinate, "
    "scroll_direction up/down/left/right, scroll_amount), type (text), key (text, e.g. \"Return\", \"ctrl+s\", "
    "\"alt+Tab\"), hold_key (text, duration), cursor_position, wait (duration in seconds).\n"
    "Coordinates are pixels in the latest screenshot of that display.")


def _params():
    num2 = {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}
    return {"type": "object", "properties": {
        "action": {"type": "string", "enum": ACTIONS, "description": "What to do"},
        "coordinate": {**num2, "description": "[x, y] in screenshot pixels"},
        "start_coordinate": {**num2, "description": "[x, y] where a drag starts"},
        "region": {"type": "array", "items": {"type": "integer"}, "minItems": 4, "maxItems": 4,
                   "description": "[x0, y0, x1, y1] to zoom into"},
        "text": {"type": "string", "description": "Text to type, keys to press or hold"},
        "scroll_direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "scroll_amount": {"type": "integer", "description": "Notches to scroll (default 3)"},
        "duration": {"type": "number", "description": "Seconds, for wait and hold_key"},
        "display": {"type": "integer", "description": "Which display (0 = main); kept for later actions"},
        "question": {"type": "string", "description": "For screenshot and zoom: what you are looking for"}},
        "required": ["action"]}


def spec(engine=False):
    return {"name": MCP_TOOL if engine else NAME, "description": DESCRIPTION, "parameters": _params()}


def settings():
    s = {**DEFAULTS, **(config.get().get("computer") or {})}
    if s.get("mode") not in MODES:
        s["mode"] = "chat"
    return s


def offered():
    """Does a new chat get the Computer tool? (On, and a desktop this computer can drive.)"""
    return bool(settings()["enabled"]) and desktop.support()[0]


# ------------------------------------------------------------------------------------------------ permission state
_lock = threading.RLock()
_chats = {}              # top chat id -> {"screen": bool, "control": None|"turn"|"chat", "denied": bool, "display": int}
_asks = {}               # ask id -> {"id", "chat", "kind", "model", "detail", "choices", "event", "answer", "t", "title"}
_active = {}             # top chat id -> {"model", "since", "abort": Event, "title"}
_watcher = None


def _publish(ev):
    from . import agent
    agent.hub.publish(ev)


def top(chat):
    """The chat the user sees: a subagent's permission and Stop are its top chat's."""
    cid, seen = chat.id if hasattr(chat, "id") else str(chat), set()
    while cid and cid not in seen:
        seen.add(cid)
        cm = store.Chat(cid).meta() or {}
        up = cm.get("sub_of") or cm.get("group_member_of")
        if not up:
            return cid
        cid = up
    return cid


def _st(cid):
    return _chats.setdefault(cid, {"screen": False, "control": None, "denied": False, "display": 0})


def _busy(cid):
    from . import agent
    return agent.busy(cid)


def _title(cid):
    return store.Chat(cid).meta().get("title") or "a chat"


def _pub_ask(a):
    return {k: a[k] for k in ("id", "chat", "kind", "model", "detail", "choices", "t", "title")}


def ask(cid, kind, model, detail, cancel):
    """Show the question in the window and wait for the answer: once, chat, allow, deny, timeout or stopped."""
    mode = settings()["mode"]
    choices = ["allow", "deny"] if kind == "screen" else (["once", "chat", "deny"] if mode == "chat" else ["once", "deny"])
    a = {"id": secrets.token_hex(6), "chat": cid, "kind": kind, "model": model, "detail": detail, "choices": choices,
         "event": threading.Event(), "answer": None, "t": time.time(), "title": _title(cid)}
    with _lock:
        _asks[a["id"]] = a
    _publish({"type": "computer_ask", **_pub_ask(a)})
    _ensure_watcher()
    try:
        while not a["event"].wait(0.2):
            if cancel is not None and cancel.is_set():
                a["answer"] = "stopped"
                break
    finally:
        with _lock:
            _asks.pop(a["id"], None)
        _publish({"type": "computer_ask_done", "id": a["id"], "chat": cid, "answer": a["answer"]})
    return a["answer"]


def answer(aid, choice):
    """The user's answer from the window."""
    with _lock:
        a = _asks.get(aid)
        if not a:
            return {"ok": False, "error": "that question has already been answered or has expired"}
        if choice not in a["choices"]:
            raise ValueError(f"choose one of {', '.join(a['choices'])}")
        st = _st(a["chat"])
        if choice == "allow":
            st["screen"] = True
        elif choice in ("once", "chat"):
            st["screen"] = True
            st["control"] = "chat" if choice == "chat" else "turn"
            st["denied"] = False
        elif choice == "deny" and a["kind"] == "control":
            st["denied"] = True
        a["answer"] = choice
        a["event"].set()
    return {"ok": True}


def _start_session(cid, model):
    with _lock:
        if cid in _active:
            return _active[cid]
        s = _active[cid] = {"model": model, "since": time.time(), "abort": threading.Event(), "title": _title(cid)}
    _publish({"type": "computer_session", "chat": cid, "active": True, "model": model, "title": s["title"]})
    _ensure_watcher()
    return s


def _end_session(cid, why="done"):
    with _lock:
        s = _active.pop(cid, None)
    if s:
        s["abort"].set()
        _publish({"type": "computer_session", "chat": cid, "active": False, "why": why})


def stop(cid=None, why="stopped"):
    """Stop (the banner), Esc: end control at once, take the permission back and stop the chat's turn."""
    from . import agent
    with _lock:
        cids = [cid] if cid else list(set(_active) | {a["chat"] for a in _asks.values()})
        for c in cids:
            st = _st(c)
            st["control"] = None
            st["denied"] = True
            for a in list(_asks.values()):
                if a["chat"] == c and not a["event"].is_set():
                    a["answer"] = "stopped"
                    a["event"].set()
    for c in cids:
        _end_session(c, why)
        if agent.busy(c):
            agent.stop(c)
    return {"ok": True, "stopped": cids}


def _ensure_watcher():
    global _watcher
    with _lock:
        if _watcher and _watcher.is_alive():
            return
        _watcher = threading.Thread(target=_watch, daemon=True, name="computer-watch")
        _watcher.start()


def _watch():
    """Ends "this reply" grants and the banner when the turn ends; watches the physical Esc key while a model is in
    control (on desktops that can tell)."""
    while True:
        with _lock:
            active = list(_active)
            asked = {a["chat"] for a in _asks.values()}
            watched = [c for c, st in _chats.items() if st["control"] == "turn" or st["denied"]]
        for c in set(active) | set(watched):
            if c in asked or _busy(c):
                continue
            with _lock:
                st = _st(c)
                if st["control"] == "turn":
                    st["control"] = None
                st["denied"] = False
            _end_session(c, "done")
        if active:
            try:
                b = desktop.get()
                if b.can_watch_esc and b.esc_down():
                    stop(None, "esc")
            except Exception:
                pass
            time.sleep(0.04)
        else:
            time.sleep(0.3)


def state():
    with _lock:
        return {"settings": settings(), "support": dict(zip(("ok", "label"), desktop.support())),
                "asks": [_pub_ask(a) for a in _asks.values()],
                "sessions": [{"chat": c, "model": s["model"], "since": s["since"], "title": s["title"]} for c, s in _active.items()],
                "grants": {c: {"screen": st["screen"], "control": st["control"]} for c, st in _chats.items()}}


# ------------------------------------------------------------------------------------------------ geometry
def fit(w, h, max_w, max_h, up=1.0):
    """The screenshot size for a w x h region: as large as fits max_w x max_h, never more than `up` times its size."""
    f = min(up, max_w / w, max_h / h)
    return max(1, round(w * f)), max(1, round(h * f))


def to_real(d, size, x, y):
    """Screenshot pixel (x, y) of display d (shown at size) -> a real pixel on the virtual desktop."""
    ow, oh = size
    if not (0 <= x < ow and 0 <= y < oh):
        raise tools.ToolError(f"({x}, {y}) is outside the screenshot ({ow}x{oh}: x 0-{ow - 1}, y 0-{oh - 1})")
    rx = d["x"] + min(d["w"] - 1, int((x + 0.5) * d["w"] / ow))
    ry = d["y"] + min(d["h"] - 1, int((y + 0.5) * d["h"] / oh))
    return rx, ry


def to_shot(d, size, rx, ry):
    ow, oh = size
    return int((rx - d["x"]) * ow / d["w"]), int((ry - d["y"]) * oh / d["h"])


def _xy(v, what="coordinate"):
    if isinstance(v, str):
        v = [p for p in re.split(r"[\s,\[\]()]+", v) if p]
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise tools.ToolError(f"{what} must be [x, y]")
    try:
        return int(round(float(v[0]))), int(round(float(v[1])))
    except (TypeError, ValueError):
        raise tools.ToolError(f"{what} must be two numbers")


# ------------------------------------------------------------------------------------------------ images for the model
def _image_result(ctx, path, text, size, question):
    return {"text": text, "image": path, "ui": {"image": str(path)}}


# ------------------------------------------------------------------------------------------------ the tool
class _Stopped(tools.ToolError):
    pass


def _check(sess, ctx):
    if (sess and sess["abort"].is_set()) or ctx.cancel.is_set():
        raise _Stopped("stopped: the user took back control")


def _sleep(sess, ctx, secs):
    end = time.time() + secs
    while time.time() < end:
        _check(sess, ctx)
        time.sleep(min(0.05, max(0.0, end - time.time())))


DECLINED = ("The user did not allow control of their mouse and keyboard. Do not try again in this reply, and never "
            "through Bash or scripts: ask them, or do it another way.")


def _permit(ctx, cid, kind, detail):
    st = _st(cid)
    model = (ctx.ep or {}).get("name") or "The model"
    if kind == "screen":
        if st["screen"] or st["control"]:
            return
        got = ask(cid, "screen", model, detail, ctx.cancel)
        if got == "allow":
            return
        raise tools.ToolError({"deny": "The user did not allow screenshots in this chat. Ask them, or do it another way.",
                               "timeout": "The user did not answer within 10 minutes, so no screenshot was taken.",
                               "stopped": "Stopped by the user."}.get(got, "Not allowed."))
    if st["control"]:
        return
    if st["denied"]:
        raise tools.ToolError(DECLINED)
    got = ask(cid, "control", model, detail, ctx.cancel)
    if got in ("once", "chat"):
        return
    raise tools.ToolError({"deny": DECLINED, "timeout": "The user did not answer within 10 minutes, so nothing was done.",
                           "stopped": "Stopped by the user."}.get(got, "Not allowed."))


def _display(b, st, want):
    ds = b.displays()
    i = st.get("display", 0) if want is None else int(want)
    if not 0 <= i < len(ds):
        raise tools.ToolError(f"there is no display {i}; this computer has {len(ds)} (0-{len(ds) - 1})")
    st["display"] = i
    return ds, ds[i]


def _detail(act, a):
    bits = [act.replace("_", " ")]
    if a.get("coordinate") is not None:
        bits.append(f"at {a['coordinate']}")
    if act in ("type", "key", "hold_key") and a.get("text"):
        t = str(a["text"])
        bits.append(repr(t[:80] + ("..." if len(t) > 80 else "")))
    return " ".join(bits)


def t_computer(ctx, a):
    s = settings()
    if not s["enabled"]:
        raise tools.ToolError("computer use is turned off (Settings > Tools)")
    act = str(a.get("action") or "").strip().lower().replace(" ", "_")
    act = {"click": "left_click", "move": "mouse_move", "drag": "left_click_drag", "press": "key", "type_text": "type",
           "screen": "screenshot", "sleep": "wait"}.get(act, act)
    if act not in ACTIONS:
        raise tools.ToolError(f"unknown action {act!r}; use one of: {', '.join(ACTIONS)}")
    try:
        b = desktop.get()
    except desktop.Unsupported as e:
        raise tools.ToolError(f"computer use is not available here: {e}")
    cid = top(ctx.chat)
    st = _st(cid)
    if act == "wait":
        secs = max(0.0, min(float(a.get("duration") or a.get("seconds") or 1), 30))
        _sleep(None, ctx, secs)
        return f"Waited {secs:g} s."
    kind = "screen" if act in LOOK else "control" if act in CONTROL else None
    if kind:
        _permit(ctx, cid, kind, _detail(act, a))
    ds, d = _display(b, st, a.get("display"))
    size = fit(d["w"], d["h"], s["max_width"], s["max_height"])
    if act == "cursor_position":
        rx, ry = b.cursor()
        if not (d["x"] <= rx < d["x"] + d["w"] and d["y"] <= ry < d["y"] + d["h"]):
            other = next((x for x in ds if x["x"] <= rx < x["x"] + x["w"] and x["y"] <= ry < x["y"] + x["h"]), None)
            return f"The pointer is on display {other['index'] if other else '?'}, not on display {d['index']}."
        x, y = to_shot(d, size, rx, ry)
        return f"The pointer is at ({x}, {y}) on display {d['index']} ({size[0]}x{size[1]} screenshot pixels)."
    if kind == "screen":
        return _look(ctx, b, ds, d, size, act, a)
    sess = _start_session(cid, (ctx.ep or {}).get("name") or "A model")
    return _control(ctx, b, sess, d, size, act, a)


def _shots_dir(ctx):
    d = ctx.chat.dir / "screens"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _look(ctx, b, ds, d, size, act, a):
    s = settings()
    if act == "zoom":
        reg = a.get("region")
        if not isinstance(reg, (list, tuple)) or len(reg) != 4:
            raise tools.ToolError("region must be [x0, y0, x1, y1] in screenshot pixels")
        x0, y0 = _xy(reg[:2], "region")
        x1, y1 = _xy(reg[2:], "region")
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x1, y1 = min(x1, size[0] - 1), min(y1, size[1] - 1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            raise tools.ToolError("the region is too small (at least 4 x 4 pixels)")
        to_real(d, size, x0, y0)                      # checks the corners are on the screenshot
        to_real(d, size, x1, y1)
        rx0, ry0 = d["x"] + x0 * d["w"] // size[0], d["y"] + y0 * d["h"] // size[1]       # whole real pixels under it
        rx1, ry1 = d["x"] + min(d["w"], (x1 + 1) * d["w"] // size[0]), d["y"] + min(d["h"], (y1 + 1) * d["h"] // size[1])
        rw, rh = max(1, rx1 - rx0), max(1, ry1 - ry0)
        out = fit(rw, rh, s["max_width"], s["max_height"], up=2.0)
        data = b.capture(rx0, ry0, rw, rh, *out)
        path = _shots_dir(ctx) / f"zoom-{int(time.time() * 1000)}.png"
        path.write_bytes(data)
        text = (f"Zoomed into [{x0}, {y0}, {x1}, {y1}] of display {d['index']}, shown at {out[0]}x{out[1]}. Coordinates "
                f"for actions still refer to the full {size[0]}x{size[1]} screenshot.\nSaved: {path}")
        return _image_result(ctx, path, text, out, a.get("question"))
    data = b.capture(d["x"], d["y"], d["w"], d["h"], *size)
    path = _shots_dir(ctx) / f"screen-{int(time.time() * 1000)}.png"
    path.write_bytes(data)
    text = f"Screenshot of display {d['index']}{' (main)' if d.get('primary') else ''}, {size[0]}x{size[1]}"
    if size != (d["w"], d["h"]):
        text += f" (the screen is {d['w']}x{d['h']}, scaled down)"
    text += ". Use pixels in this picture as coordinates."
    others = [x for x in ds if x["index"] != d["index"]]
    if others:
        text += " Other displays: " + ", ".join(f"{x['index']} ({x['w']}x{x['h']})" for x in others) + " (display: N)."
    text += f"\nSaved: {path}"
    return _image_result(ctx, path, text, size, a.get("question"))


def _control(ctx, b, sess, d, size, act, a):
    held_keys, held_btn = [], []
    where = ""

    def go(xy, what="coordinate"):
        nonlocal where
        x, y = _xy(xy, what)
        rx, ry = to_real(d, size, x, y)
        _check(sess, ctx)
        b.move(rx, ry)
        where = f" at ({x}, {y})"

    def mods_down():
        if a.get("text") and act.endswith("click"):
            for k in desktop.combos(a["text"])[0]:
                _check(sess, ctx)
                b.key(k, True)
                held_keys.append(k)

    try:
        if act in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            if a.get("coordinate") is not None:
                go(a["coordinate"])
                b.flush()
                _sleep(sess, ctx, 0.05)
            mods_down()
            btn = act.split("_")[0] if act in ("left_click", "right_click", "middle_click") else "left"
            n = {"double_click": 2, "triple_click": 3}.get(act, 1)
            for _ in range(n):
                _check(sess, ctx)
                b.button(btn, True)
                held_btn.append(btn)
                b.button(btn, False)
                held_btn.pop()
                if n > 1:
                    b.flush()
                    time.sleep(0.04)
            done = f"{act.replace('_', ' ')}{where}"
        elif act == "mouse_move":
            go(a.get("coordinate"))
            done = f"moved the pointer{where}"
        elif act == "left_click_drag":
            sx, sy = _xy(a.get("start_coordinate"), "start_coordinate")
            ex, ey = _xy(a.get("coordinate"))
            rs, re_ = to_real(d, size, sx, sy), to_real(d, size, ex, ey)
            _check(sess, ctx)
            b.move(*rs)
            b.button("left", True)
            held_btn.append("left")
            b.flush()
            for i in range(1, 13):
                _sleep(sess, ctx, 0.02)
                b.move(round(rs[0] + (re_[0] - rs[0]) * i / 12), round(rs[1] + (re_[1] - rs[1]) * i / 12))
                b.flush()
            b.button("left", False)
            held_btn.pop()
            done = f"dragged from ({sx}, {sy}) to ({ex}, {ey})"
        elif act in ("left_mouse_down", "left_mouse_up"):
            if a.get("coordinate") is not None:
                go(a["coordinate"])
            b.button("left", act == "left_mouse_down")
            done = f"{act.replace('_', ' ')}{where}"
        elif act == "scroll":
            if a.get("coordinate") is not None:
                go(a["coordinate"])
            direction = str(a.get("scroll_direction") or "down").lower()
            n = max(1, min(int(a.get("scroll_amount") or 3), 50))
            dx, dy = {"up": (0, n), "down": (0, -n), "left": (-n, 0), "right": (n, 0)}.get(direction, (None, None))
            if dx is None:
                raise tools.ToolError("scroll_direction must be up, down, left or right")
            _check(sess, ctx)
            b.wheel(dx, dy)
            done = f"scrolled {direction} {n}{where}"
        elif act == "type":
            text = str(a.get("text") or "")
            if not text:
                raise tools.ToolError("text is missing")
            if len(text) > 8000:
                raise tools.ToolError("at most 8,000 characters per type action")
            for i in range(0, len(text), 40):              # in pieces, so Stop and Esc cut it short
                _check(sess, ctx)
                b.text(text[i:i + 40])
            done = f"typed {len(text)} characters"
        elif act in ("key", "hold_key"):
            try:
                groups = desktop.combos(a.get("text"))
            except ValueError as e:
                raise tools.ToolError(str(e))
            for keys in groups:
                for k in keys:
                    _check(sess, ctx)
                    b.key(k, True)
                    held_keys.append(k)
                if act == "hold_key":
                    b.flush()
                    _sleep(sess, ctx, max(0.0, min(float(a.get("duration") or 1), 10)))
                while held_keys:
                    b.key(held_keys.pop(), False)
                b.flush()
            done = ("held " if act == "hold_key" else "pressed ") + " ".join("+".join(k) for k in groups)
        else:
            raise tools.ToolError(f"unknown action {act}")
        while held_keys:
            b.key(held_keys.pop(), False)
        b.flush()
        time.sleep(0.12)                               # let the screen catch up before the next screenshot
        return f"Done: {done}. Take a screenshot to check the result."
    except ValueError as e:
        raise tools.ToolError(str(e))
    finally:                                           # never leave a key or button held down (also after Stop)
        try:
            while held_btn:
                b.button(held_btn.pop(), False)
            while held_keys:
                b.key(held_keys.pop(), False)
            b.flush()
        except Exception:
            pass


tools.SPECS[NAME] = (_params(), DESCRIPTION)
tools.RUN[NAME] = t_computer


# ------------------------------------------------------------------------------------------------ Bash guard
GUI_MARKERS = [
    (r"\bpyautogui\b", "pyautogui"), (r"\bpynput\b", "pynput"), (r"\bpydirectinput\b", "pydirectinput"),
    (r"^\s*(import|from)\s+(keyboard|mouse)\b", "the keyboard/mouse modules"), (r"\bautoit\b", "AutoIt"),
    (r"\bSendKeys\b", "SendKeys"), (r"\bSendWait\s*\(", "SendKeys.SendWait"), (r"\bkeybd_event\b", "keybd_event"),
    (r"\bmouse_event\b", "mouse_event"), (r"\bSendInput\b", "SendInput"), (r"\bSetCursorPos\b", "SetCursorPos"),
    (r"\bAutoHotkey\w*\b|\.ahk\b", "AutoHotkey"), (r"\bxdotool\b", "xdotool"), (r"\bydotool\b", "ydotool"),
    (r"\bwtype\b", "wtype"), (r"\bxte\b", "xte"), (r"\bcliclick\b", "cliclick"),
    (r"\bosascript\b.*\b(keystroke|key code|click)\b", "AppleScript keystrokes"),
    (r"System Events.*\b(keystroke|key code|click)\b", "AppleScript keystrokes"),
    (r"\bnircmd(c)?(\.exe)?\s+(sendkey|sendkeypress|sendmouse|setcursor|movecursor)", "nircmd"),
    (r"\brobotjs\b|\bnut-?js\b|@nut-tree", "a desktop automation library"),
    (r"\bImageGrab\.grab\b|\bimport\s+mss\b|\bmss\.mss\b|\bscreencapture\b|\bscrot\b|\bgnome-screenshot\b|"
     r"\bimport\s+-window\s+root\b|\bCopyFromScreen\b|\bxwd\s+-root\b", "a screen capture"),
]
_GUI_RE = [(re.compile(p, re.I | re.M), label) for p, label in GUI_MARKERS]
READERS = {"grep", "egrep", "fgrep", "rg", "ag", "git", "cat", "head", "tail", "less", "more", "find", "ls", "wc", "pip",
           "pip3", "uv", "npm", "pnpm", "yarn", "conda", "apt", "apt-get", "brew", "winget", "choco", "man", "which",
           "type", "file", "diff", "stat"}
SCRIPT_EXT = {".py", ".pyw", ".ps1", ".psm1", ".ahk", ".ahk2", ".js", ".mjs", ".cjs", ".ts", ".vbs", ".jse", ".wsf",
              ".scpt", ".applescript", ".sh", ".bash", ".bat", ".cmd", ".au3"}


def _markers(text):
    for rx, label in _GUI_RE:
        if rx.search(text):
            return label
    return None


def gui_automation_refusal(cmd, ctx=None):
    """Bash steers GUI automation and screen capture to the Computer tool (it asks the user first and shows that a model
    is in control): the reason to refuse `cmd`, or None. Searching for or installing such packages is fine; running
    them, inline or from a script file the command runs, is not."""
    seen = None
    for seg in re.split(r"\n|&&|\|\||;|\|", cmd):
        seg = seg.strip()
        if not seg:
            continue
        try:
            words = shlex.split(seg, posix=True)
        except ValueError:
            words = seg.split()
        while words and (re.match(r"^\w+=", words[0]) or words[0] in ("sudo", "env", "exec", "nohup", "time", "command")):
            words = words[1:]
        prog = Path(words[0]).name.lower() if words else ""
        prog = re.sub(r"\.exe$", "", prog)
        if prog in READERS:
            continue
        label = _markers(seg)
        if not label and words:
            base = ctx.cwd if ctx is not None else Path.cwd()
            for w in words:
                p = Path(w if Path(w).is_absolute() else base / w)
                if p.suffix.lower() in SCRIPT_EXT:
                    try:
                        if p.is_file() and p.stat().st_size < 2_000_000:
                            label = _markers(p.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
                if label:
                    break
        if label:
            seen = label
            break
    if not seen:
        return None
    has = ctx is not None and NAME in (ctx.chat.meta().get("tools") or [])
    return (f"Refused: this command drives the mouse, keyboard or screen ({seen}). "
            + ("Use the Computer tool instead: it asks the user first and shows them that you are in control."
               if has else "Computer use is not available in this chat; ask the user to do it, or to start a new chat.")
            + " (If you only meant to write or search code that mentions it, use Write, Edit or Grep.)")


# ------------------------------------------------------------------------------------------------ Claude Code and Codex
def mcp_server(chat, meta):
    """The computer MCP server for a Claude Code or Codex session of this chat, or None when the chat has no Computer
    tool. {"command", "args", "env"}."""
    if NAME not in (meta.get("tools") or []) or not settings()["enabled"]:
        return None
    from . import subagents
    f = chat.dir / "harness-computer-tool.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(spec(engine=True), ensure_ascii=False), encoding="utf-8")
    return {"command": subagents._python(), "args": [str(config.APP / "harness" / "servers" / "computer.py")],
            "env": {"KCH_COMPUTER_URL": f"http://127.0.0.1:{config.PORT}/api/computer", "KCH_COMPUTER_TOKEN": TOKEN,
                    "KCH_COMPUTER_CHAT": chat.id, "KCH_COMPUTER_SPEC": str(f), "KCH_COMPUTER_TIMEOUT": str(CLI_TOOL_WAIT_S)}}


def claude_mcp_args(chat, meta, env):
    """--mcp-config for a Claude Code session (its own file; the token goes in env, never in the file)."""
    srv = mcp_server(chat, meta)
    if not srv:
        return []
    f = chat.dir / "computer-mcp.json"
    f.write_text(json.dumps({"mcpServers": {"computer": {"type": "stdio", "command": srv["command"], "args": srv["args"]}}}),
                 encoding="utf-8")
    env.update(srv["env"])
    env["MCP_TOOL_TIMEOUT"] = str(max(int(env.get("MCP_TOOL_TIMEOUT") or 0), CLI_TOOL_WAIT_S * 1000))
    return ["--mcp-config", str(f)]


def codex_mcp_args(chat, meta):
    srv = mcp_server(chat, meta)
    if not srv:
        return []
    env_t = "{" + ", ".join(f"{k} = {json.dumps(v)}" for k, v in srv["env"].items()) + "}"
    return ["-c", f"mcp_servers.computer.command={json.dumps(srv['command'])}",
            "-c", f"mcp_servers.computer.args={json.dumps(srv['args'])}", "-c", f"mcp_servers.computer.env={env_t}",
            "-c", "mcp_servers.computer.startup_timeout_sec=30", "-c", f"mcp_servers.computer.tool_timeout_sec={CLI_TOOL_WAIT_S}"]


class _Any:
    def __init__(self, *evs):
        self.evs = [e for e in evs if e is not None]

    def is_set(self):
        return any(e.is_set() for e in self.evs)

    def wait(self, timeout=None):
        end = time.time() + (timeout or 0)
        while not self.is_set():
            if timeout is not None and time.time() >= end:
                return False
            time.sleep(0.05)
        return True


_cancels = {}


def _mcp_call(headers, body):
    import base64
    from . import agent
    if not secrets.compare_digest(str(headers.get("X-KC-Computer-Token") or ""), TOKEN):
        raise PermissionError("not allowed")
    chat = store.Chat(str(body.get("chat") or ""))
    meta = chat.meta()
    if not meta:
        raise ValueError("no such chat")
    ep = config.endpoint(meta.get("endpoint"))
    key = str(body.get("key") or secrets.token_hex(6))
    mine = _cancels[key] = threading.Event()
    run = agent.runs.get(chat.id)
    state = agent._tool_state.setdefault(chat.id, {})
    ctx = tools.Ctx(chat, meta.get("project") or str(config.WORKSPACE), ep, lambda ev: agent.emit(chat.id, ev),
                    _Any(mine, run.cancel if run else None), state)
    try:
        res = tools.run(ctx, NAME, body.get("args") or {})
    finally:
        _cancels.pop(key, None)
    out = {"text": res.get("text") or "", "ok": res.get("ok", True)}
    if res.get("image"):
        out["image"] = base64.b64encode(Path(res["image"]).read_bytes()).decode()
        out["mime"] = "image/png"
    return out


def http_get(path, q):
    if path == "/api/computer/state":
        return state()
    raise ValueError("unknown")


def http_post(path, headers, body):
    """/api/computer/call and /cancel (servers/computer.py, with the token); answer, stop and settings (the window)."""
    if path == "/api/computer/call":
        return _mcp_call(headers, body)
    if path == "/api/computer/cancel":
        if not secrets.compare_digest(str(headers.get("X-KC-Computer-Token") or ""), TOKEN):
            raise PermissionError("not allowed")
        ev = _cancels.get(str(body.get("key")))
        if ev:
            ev.set()
        return {"ok": bool(ev)}
    if path == "/api/computer/answer":
        return answer(str(body.get("id") or ""), str(body.get("choice") or ""))
    if path == "/api/computer/stop":
        return stop(body.get("chat") or None, "stopped")
    if path == "/api/computer/settings":
        def fn(c):
            cur = c.setdefault("computer", {})
            if "enabled" in body:
                cur["enabled"] = bool(body["enabled"])
            if body.get("mode") in MODES:
                cur["mode"] = body["mode"]
        config.update(fn)
        if settings()["mode"] == "ask":                # "always ask": nothing stays allowed for a whole chat
            with _lock:
                for st in _chats.values():
                    if st["control"] == "chat":
                        st["control"] = None
        if not settings()["enabled"]:
            stop(None, "off")
        return state()
    raise ValueError("unknown")
