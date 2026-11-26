"""The screen, mouse and keyboard for the Computer tool, one backend per desktop, standard library only:
- Windows: ctypes (GDI StretchBlt capture, SendInput, per-monitor DPI awareness so every number is a real pixel).
- macOS: `screencapture` + `sips` for pictures, Quartz events through ctypes for input (needs the Screen Recording and
  Accessibility permissions for the Python that runs the app).
- Linux: X11 through `xdotool` (input) and `xwd` (pictures). Wayland sessions are not supported: their compositors do
  not let one app read the screen or send input to the others.
A dry-run backend records every input it would send (tests, and a look at what a model would do) and draws a blank
screen instead of capturing one.

Coordinates here are always real pixels on the virtual desktop (all monitors). The Computer tool (computer.py) scales
the model's screenshot coordinates to these. Nothing here is called unless the user has approved it there."""
import ctypes
import json
import operator
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"
NOWIN = 0x08000000 if WINDOWS else 0


class Unsupported(Exception):
    """This computer's desktop cannot be used (Wayland, no display, a missing program)."""


# ------------------------------------------------------------------------------------------------ pictures
def png(w, h, rgb):
    """RGB bytes (w*h*3) as a PNG file."""
    stride = w * 3
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def png_size(data):
    """(width, height) of a PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", data[16:24])


def bgrx_to_rgb(buf, n):
    """n pixels of 4 bytes (blue, green, red, unused) as RGB, without a Python loop per pixel."""
    rgb = bytearray(n * 3)
    rgb[0::3] = buf[2:n * 4:4]
    rgb[1::3] = buf[1:n * 4:4]
    rgb[2::3] = buf[0:n * 4:4]
    return rgb


def scale_rgb(w, h, rgb, ow, oh):
    """Nearest-neighbour resize of RGB bytes (used where the desktop cannot scale for us)."""
    if (ow, oh) == (w, h):
        return bytes(rgb)
    stride = w * 3
    cols = [(x * w // ow) * 3 + c for x in range(ow) for c in range(3)]
    pick = operator.itemgetter(*cols)
    rows = []
    for y in range(oh):
        sy = y * h // oh
        rows.append(bytes(pick(rgb[sy * stride:(sy + 1) * stride])))
    return b"".join(rows)


def crop_rgb(w, h, rgb, x, y, cw, ch):
    stride = w * 3
    return b"".join(bytes(rgb[(y + r) * stride + x * 3:(y + r) * stride + (x + cw) * 3]) for r in range(ch))


def blank(w, h):
    """A plain grey picture with a faint grid (dry runs never capture the real screen)."""
    row = bytearray()
    for x in range(w):
        v = 90 if x % 64 == 0 else 60
        row += bytes((v, v, v + 8))
    line = bytes((90, 90, 98)) * w
    return b"".join(line if y % 64 == 0 else bytes(row) for y in range(h))


# ------------------------------------------------------------------------------------------------ keys
ALIASES = {"return": "enter", "ret": "enter", "kp_enter": "enter", "escape": "esc", "control": "ctrl", "cmd": "super",
           "command": "super", "win": "super", "windows": "super", "meta": "super", "option": "alt", "opt": "alt",
           "altgr": "alt", "pgup": "pageup", "page_up": "pageup", "prior": "pageup", "pgdn": "pagedown",
           "page_down": "pagedown", "next": "pagedown", "arrowup": "up", "arrowdown": "down", "arrowleft": "left",
           "arrowright": "right", "back_space": "backspace", "bksp": "backspace", "bs": "backspace", "del": "delete",
           "ins": "insert", "print": "printscreen", "prtsc": "printscreen", "print_screen": "printscreen",
           "caps_lock": "capslock", "caps": "capslock", "apps": "menu", "spacebar": "space", "plus": "+", "minus": "-"}
NAMED = {"enter", "esc", "ctrl", "alt", "shift", "super", "tab", "space", "backspace", "delete", "insert", "home", "end",
         "pageup", "pagedown", "up", "down", "left", "right", "printscreen", "capslock", "menu",
         *[f"f{i}" for i in range(1, 25)]}
MODIFIERS = {"ctrl", "alt", "shift", "super"}


def key_name(k):
    """A key as the model may write it ("Return", "ctrl", "Page_Down", "a", "F5") -> a name every backend knows."""
    k = str(k or "").strip()
    if len(k) == 1:
        return k
    low = k.lower()
    for side in ("_l", "_r"):
        if low.endswith(side) and low[:-2] in ("control", "ctrl", "alt", "shift", "super", "meta"):
            low = low[:-2]
    low = ALIASES.get(low, low)
    if low in NAMED or len(low) == 1:
        return low
    raise ValueError(f"unknown key {k!r}")


def combos(text):
    """'ctrl+shift+t' or several separated by spaces ('ctrl+a Delete') -> [[key, ...], ...]."""
    out = []
    for part in str(text or "").split():
        keys = [p for p in part.split("+") if p] if part != "+" else ["+"]
        if part.endswith("++"):
            keys.append("+")
        out.append([key_name(k) for k in keys])
    if not out:
        raise ValueError("no key given")
    return out


# ------------------------------------------------------------------------------------------------ backends
class Display(dict):
    """{index, x, y, w, h, primary, name}: real pixels on the virtual desktop."""


class Backend:
    name = "none"
    kind = "none"
    can_watch_esc = False

    def displays(self):
        raise NotImplementedError

    def capture(self, x, y, w, h, ow, oh):
        """PNG bytes of the real-pixel region (x, y, w, h), resized to ow x oh."""
        raise NotImplementedError

    def move(self, x, y):
        raise NotImplementedError

    def button(self, which, down):
        raise NotImplementedError

    def wheel(self, dx, dy):
        """Scroll by notches: dy > 0 up, dx > 0 right."""
        raise NotImplementedError

    def key(self, name, down):
        raise NotImplementedError

    def text(self, s):
        raise NotImplementedError

    def cursor(self):
        raise NotImplementedError

    def esc_down(self):
        """Is the physical Esc key held right now? (watching for the user taking control back)"""
        return False

    def flush(self):
        """Send what is queued (backends that batch their input)."""


# ---- Windows
if WINDOWS:
    from ctypes import wintypes

    class _MI(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _KI(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _HI(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("mi", _MI), ("ki", _KI), ("hi", _HI)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    class _BIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class _MONITORINFOEX(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

VK = {"enter": 0x0D, "esc": 0x1B, "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "super": 0x5B, "tab": 0x09, "space": 0x20,
      "backspace": 0x08, "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "printscreen": 0x2C, "capslock": 0x14, "menu": 0x5D,
      **{f"f{i}": 0x6F + i for i in range(1, 25)}}
VK_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D, 0x2C}
MOUSE = {"move": 0x0001, "left_down": 0x0002, "left_up": 0x0004, "right_down": 0x0008, "right_up": 0x0010,
         "middle_down": 0x0020, "middle_up": 0x0040, "wheel": 0x0800, "hwheel": 0x1000, "absolute": 0x8000,
         "virtualdesk": 0x4000}
KEYUP, UNICODE, EXTENDED = 0x0002, 0x0004, 0x0001


class WindowsDesktop(Backend):
    """SendInput and GDI. dry=True: inputs are recorded in self.sent (and KCH_COMPUTER_LOG) instead of sent, and the
    screen is a blank picture, so tests never touch the real mouse, keyboard or screen."""
    name = "Windows"
    kind = "windows"

    def __init__(self, dry=False, monitors=None, log=None):
        self.dry = dry
        self.sent = []
        self.log = log
        self._fixed = monitors
        self._ready = False
        self._esc_ignore_until = 0.0
        self.can_watch_esc = not dry

    def _init(self):
        if self._ready:
            return
        self._ready = True
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        g = self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        if not self.dry:          # real pixels everywhere (otherwise a 150% display reports 2/3 of its size)
            try:
                u.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
                u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                if not u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):         # per-monitor v2
                    raise OSError
            except (AttributeError, OSError):
                try:
                    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
                except (AttributeError, OSError):
                    u.SetProcessDPIAware()
        H = ctypes.c_void_p
        for fn, res, args in [
            ("GetDC", H, [H]), ("ReleaseDC", ctypes.c_int, [H, H]), ("GetSystemMetrics", ctypes.c_int, [ctypes.c_int]),
            ("SendInput", wintypes.UINT, [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]),
            ("GetCursorPos", wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)]),
            ("GetAsyncKeyState", ctypes.c_short, [ctypes.c_int]), ("VkKeyScanW", ctypes.c_short, [wintypes.WCHAR]),
            ("MapVirtualKeyW", wintypes.UINT, [wintypes.UINT, wintypes.UINT]),
            ("GetMonitorInfoW", wintypes.BOOL, [H, ctypes.c_void_p])]:
            f = getattr(u, fn)
            f.restype, f.argtypes = res, args
        for fn, res, args in [
            ("CreateCompatibleDC", H, [H]), ("CreateCompatibleBitmap", H, [H, ctypes.c_int, ctypes.c_int]),
            ("SelectObject", H, [H, H]), ("DeleteObject", wintypes.BOOL, [H]), ("DeleteDC", wintypes.BOOL, [H]),
            ("SetStretchBltMode", ctypes.c_int, [H, ctypes.c_int]), ("SetBrushOrgEx", wintypes.BOOL, [H, ctypes.c_int, ctypes.c_int, H]),
            ("StretchBlt", wintypes.BOOL, [H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, H, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]),
            ("GetDIBits", ctypes.c_int, [H, H, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT])]:
            f = getattr(g, fn)
            f.restype, f.argtypes = res, args

    # -- geometry
    def displays(self):
        if self._fixed:
            return [Display(index=i, x=m[0], y=m[1], w=m[2], h=m[3], primary=bool(m[4]) if len(m) > 4 else i == 0,
                            name=f"Display {i + 1}") for i, m in enumerate(self._fixed)]
        self._init()
        found = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), ctypes.c_void_p)

        def visit(hmon, _dc, _rect, _data):
            info = _MONITORINFOEX()
            info.cbSize = ctypes.sizeof(info)
            if self.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                found.append((r.left, r.top, r.right - r.left, r.bottom - r.top, bool(info.dwFlags & 1), info.szDevice))
            return 1
        cb = proc(visit)
        self.user32.EnumDisplayMonitors(None, None, cb, None)
        if not found:
            found = [(0, 0, self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1), True, "")]
        found.sort(key=lambda m: (not m[4], m[0], m[1]))          # the main display first, then left to right
        return [Display(index=i, x=m[0], y=m[1], w=m[2], h=m[3], primary=m[4], name=f"Display {i + 1}")
                for i, m in enumerate(found)]

    def _virtual(self):
        if self._fixed:
            ds = self.displays()
            x0, y0 = min(d["x"] for d in ds), min(d["y"] for d in ds)
            return x0, y0, max(d["x"] + d["w"] for d in ds) - x0, max(d["y"] + d["h"] for d in ds) - y0
        self._init()
        m = self.user32.GetSystemMetrics
        return m(76), m(77), m(78), m(79)          # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN

    # -- pictures
    def capture(self, x, y, w, h, ow, oh):
        if self.dry:
            return png(ow, oh, blank(ow, oh))
        self._init()
        u, g = self.user32, self.gdi32
        screen = u.GetDC(None)
        mem = g.CreateCompatibleDC(screen)
        bmp = g.CreateCompatibleBitmap(screen, ow, oh)
        old = g.SelectObject(mem, bmp)
        try:
            g.SetStretchBltMode(mem, 4)                  # HALFTONE: a smooth resize, so text stays readable
            g.SetBrushOrgEx(mem, 0, 0, None)
            if not g.StretchBlt(mem, 0, 0, ow, oh, screen, x, y, w, h, 0x00CC0020 | 0x40000000):   # SRCCOPY | CAPTUREBLT
                raise OSError(f"the screen could not be captured (error {ctypes.get_last_error()})")
            bih = _BIH()
            bih.biSize, bih.biWidth, bih.biHeight, bih.biPlanes, bih.biBitCount = ctypes.sizeof(_BIH), ow, -oh, 1, 32
            buf = ctypes.create_string_buffer(ow * oh * 4)
            g.SelectObject(mem, old)
            if g.GetDIBits(mem, bmp, 0, oh, buf, ctypes.byref(bih), 0) != oh:
                raise OSError("the screen picture could not be read")
            return png(ow, oh, bgrx_to_rgb(buf.raw, ow * oh))
        finally:
            g.SelectObject(mem, old)
            g.DeleteObject(bmp)
            g.DeleteDC(mem)
            u.ReleaseDC(None, screen)

    # -- input
    def _send(self, *items):
        if self.dry:
            for it in items:
                rec = ({"type": "mouse", "dx": it.mi.dx, "dy": it.mi.dy, "data": ctypes.c_int32(it.mi.mouseData).value,
                        "flags": it.mi.dwFlags} if it.type == 0 else
                       {"type": "key", "vk": it.ki.wVk, "scan": it.ki.wScan, "flags": it.ki.dwFlags})
                self.sent.append(rec)
                if self.log:
                    with open(self.log, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec) + "\n")
            return
        self._init()
        arr = (INPUT * len(items))(*items)
        n = self.user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))
        if n != len(items):
            raise OSError("Windows blocked the input. The window in front may be running as administrator, or the screen "
                          "is locked; the app cannot control it.")

    def _mouse(self, flags, dx=0, dy=0, data=0):
        it = INPUT(type=0)
        it.mi = _MI(dx, dy, ctypes.c_uint32(data & 0xFFFFFFFF).value, flags, 0, 0)
        return it

    def _key(self, vk=0, scan=0, flags=0):
        it = INPUT(type=1)
        it.ki = _KI(vk, scan, flags, 0, 0)
        return it

    def move(self, x, y):
        self._pos = (x, y)
        vx, vy, vw, vh = self._virtual()
        nx = round((x - vx) * 65535 / max(1, vw - 1))
        ny = round((y - vy) * 65535 / max(1, vh - 1))
        self._send(self._mouse(MOUSE["move"] | MOUSE["absolute"] | MOUSE["virtualdesk"], nx, ny))

    def button(self, which, down):
        self._send(self._mouse(MOUSE[f"{which}_{'down' if down else 'up'}"]))

    def wheel(self, dx, dy):
        items = []
        if dy:
            items.append(self._mouse(MOUSE["wheel"], data=int(dy) * 120))
        if dx:
            items.append(self._mouse(MOUSE["hwheel"], data=int(dx) * 120))
        if items:
            self._send(*items)

    def _vk(self, name):
        if name in VK:
            return VK[name], 0
        if self.dry and len(name) == 1 and name.isalnum():
            return ord(name.upper()), 0
        self._init()
        r = self.user32.VkKeyScanW(name)
        if r == -1:
            raise ValueError(f"no key for {name!r} on this keyboard layout; use type for text")
        return r & 0xFF, (r >> 8) & 0xFF                   # shift state: 1 shift, 2 ctrl, 4 alt

    def key(self, name, down):
        vk, _ = self._vk(name)
        if vk == 0x1B and not self.dry:
            self._esc_ignore_until = time.time() + 0.6       # our own Esc is not the user taking control back
        scan = 0 if self.dry else self.user32.MapVirtualKeyW(vk, 0)
        self._send(self._key(vk, scan, (0 if down else KEYUP) | (EXTENDED if vk in VK_EXTENDED else 0)))

    def needs_shift(self, name):
        return len(name) == 1 and not self.dry and bool(self._vk(name)[1] & 1)

    def text(self, s):
        items = []
        for ch in s:
            if ch == "\n":
                items += [self._key(0x0D), self._key(0x0D, 0, KEYUP)]
                continue
            if ch == "\r":
                continue
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):
                unit = int.from_bytes(data[i:i + 2], "little")
                items += [self._key(0, unit, UNICODE), self._key(0, unit, UNICODE | KEYUP)]
        if items:
            self._send(*items)

    def cursor(self):
        if self.dry:
            return getattr(self, "_pos", (0, 0))
        self._init()
        p = wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def esc_down(self):
        if self.dry or time.time() < self._esc_ignore_until:
            return False
        self._init()
        return bool(self.user32.GetAsyncKeyState(0x1B) & 0x8000)


# ---- macOS
MAC_KEYS = {"enter": 36, "tab": 48, "space": 49, "backspace": 51, "esc": 53, "super": 55, "shift": 56, "capslock": 57,
            "alt": 58, "ctrl": 59, "delete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "left": 123,
            "right": 124, "down": 125, "up": 126, "insert": 114, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
            "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
            **dict(zip("asdfhgzxcv", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])), "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16,
            "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
            "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
            ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50}
MAC_FLAGS = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "super": 0x100000}


class MacDesktop(Backend):
    """Quartz events (CoreGraphics through ctypes) and the screencapture tool. Not yet tried on a real Mac."""
    name = "macOS"
    kind = "mac"
    can_watch_esc = True

    def __init__(self):
        self._cg = None
        self._mods = 0
        self._pos = (0, 0)
        self._esc_ignore_until = 0.0

    class _P(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class _R(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]

    def cg(self):
        if self._cg is None:
            cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            V, P = ctypes.c_void_p, self._P
            for fn, res, args in [("CGEventCreateMouseEvent", V, [V, ctypes.c_uint32, P, ctypes.c_uint32]),
                                  ("CGEventCreateKeyboardEvent", V, [V, ctypes.c_uint16, ctypes.c_bool]),
                                  ("CGEventKeyboardSetUnicodeString", None, [V, ctypes.c_ulong, ctypes.c_void_p]),
                                  ("CGEventSetFlags", None, [V, ctypes.c_uint64]), ("CGEventPost", None, [ctypes.c_uint32, V]),
                                  ("CGEventSetIntegerValueField", None, [V, ctypes.c_uint32, ctypes.c_int64]),
                                  ("CGEventCreate", V, [V]), ("CGEventGetLocation", P, [V]), ("CFRelease", None, [V]),
                                  ("CGGetActiveDisplayList", ctypes.c_int32, [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]),
                                  ("CGDisplayBounds", self._R, [ctypes.c_uint32]), ("CGMainDisplayID", ctypes.c_uint32, []),
                                  ("CGEventSourceKeyState", ctypes.c_bool, [ctypes.c_int32, ctypes.c_uint16])]:
                f = getattr(cg, fn)
                f.restype, f.argtypes = res, args
            try:
                f = cg.CGEventCreateScrollWheelEvent2       # macOS 13+: not variadic, so ctypes can call it on Apple silicon
                f.restype, f.argtypes = V, [V, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
            except AttributeError:
                pass
            self._cg = cg
        return self._cg

    def displays(self):
        cg = self.cg()
        ids = (ctypes.c_uint32 * 16)()
        n = ctypes.c_uint32()
        cg.CGGetActiveDisplayList(16, ids, ctypes.byref(n))
        main = cg.CGMainDisplayID()
        out = []
        for i in sorted(ids[:n.value], key=lambda d: d != main):
            r = cg.CGDisplayBounds(i)
            out.append(Display(index=len(out), x=int(r.x), y=int(r.y), w=int(r.w), h=int(r.h), primary=i == main,
                               name=f"Display {len(out) + 1}"))
        return out

    def capture(self, x, y, w, h, ow, oh):
        d = Path(tempfile.mkdtemp(prefix="kch-shot-"))
        f = d / "shot.png"
        try:
            r = subprocess.run(["screencapture", "-x", "-t", "png", f"-R{x},{y},{w},{h}", str(f)], capture_output=True, timeout=20)
            if r.returncode or not f.exists():
                raise OSError("screencapture failed: allow Screen Recording for this app's Python in System Settings > "
                              "Privacy & Security")
            subprocess.run(["sips", "-z", str(oh), str(ow), str(f)], capture_output=True, timeout=20)
            return f.read_bytes()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _post(self, ev):
        cg = self.cg()
        if not ev:
            raise OSError("macOS refused the input: allow Accessibility for this app's Python in System Settings > "
                          "Privacy & Security")
        if self._mods:
            cg.CGEventSetFlags(ev, self._mods)
        cg.CGEventPost(0, ev)
        cg.CFRelease(ev)

    def move(self, x, y):
        self._pos = (x, y)
        held = getattr(self, "_held", None)
        t = {None: 5, "left": 6, "right": 7}.get(held, 27)          # moved, or dragged with a button held
        self._post(self.cg().CGEventCreateMouseEvent(None, t, self._P(x, y), {"right": 1, "middle": 2}.get(held, 0)))

    def button(self, which, down):
        t = {("left", True): 1, ("left", False): 2, ("right", True): 3, ("right", False): 4,
             ("middle", True): 25, ("middle", False): 26}[(which, down)]
        self._held = which if down else None
        self._post(self.cg().CGEventCreateMouseEvent(None, t, self._P(*self._pos), {"right": 1, "middle": 2}.get(which, 0)))

    def wheel(self, dx, dy):
        cg = self.cg()
        if not hasattr(cg, "CGEventCreateScrollWheelEvent2"):
            raise OSError("scrolling needs macOS 13 or later")
        self._post(cg.CGEventCreateScrollWheelEvent2(None, 1, 2, int(dy) * 3, int(dx) * 3, 0))

    def key(self, name, down):
        if name in MAC_FLAGS:
            self._mods = (self._mods | MAC_FLAGS[name]) if down else (self._mods & ~MAC_FLAGS[name])
        code = MAC_KEYS.get(name.lower() if len(name) == 1 else name)
        if code is None:
            raise ValueError(f"no key for {name!r}; use type for text")
        if code == 53:
            self._esc_ignore_until = time.time() + 0.6
        self._post(self.cg().CGEventCreateKeyboardEvent(None, code, down))

    def text(self, s):
        cg = self.cg()
        for ch in s:
            units = ch.encode("utf-16-le")
            buf = ctypes.create_string_buffer(units)
            for down in (True, False):
                ev = cg.CGEventCreateKeyboardEvent(None, 0, down)
                cg.CGEventKeyboardSetUnicodeString(ev, len(units) // 2, buf)
                self._post(ev)

    def cursor(self):
        cg = self.cg()
        ev = cg.CGEventCreate(None)
        p = cg.CGEventGetLocation(ev)
        cg.CFRelease(ev)
        return int(p.x), int(p.y)

    def esc_down(self):
        return time.time() >= self._esc_ignore_until and bool(self.cg().CGEventSourceKeyState(1, 53))   # the keyboard itself


# ---- Linux (X11)
X_KEYS = {"enter": "Return", "esc": "Escape", "ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "super", "tab": "Tab",
          "space": "space", "backspace": "BackSpace", "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
          "pageup": "Prior", "pagedown": "Next", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
          "printscreen": "Print", "capslock": "Caps_Lock", "menu": "Menu", **{f"f{i}": f"F{i}" for i in range(1, 25)},
          "+": "plus", "-": "minus", " ": "space"}


class X11Desktop(Backend):
    """xdotool for input (queued, sent as one xdotool run per action) and xwd for pictures. `wrap` runs them through
    another command (e.g. a WSL distribution with a virtual X display) and `display` picks the X display."""
    name = "Linux (X11)"
    kind = "x11"

    def __init__(self, display=None, wrap=None):
        self.display = display or os.environ.get("DISPLAY")
        self.wrap = list(wrap or [])
        self.queue = []
        self.lock = threading.Lock()
        if not self.wrap:
            missing = [p for p in ("xdotool", "xwd") if not shutil.which(p)]
            if missing:
                raise Unsupported(f"computer use on Linux needs {' and '.join(missing)} (for example: sudo apt install "
                                  "xdotool x11-apps)")

    def _run(self, args, timeout=30, data=None):
        env = None
        cmd = self.wrap + args
        if self.display and not self.wrap:
            env = {**os.environ, "DISPLAY": self.display}
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, input=data, creationflags=NOWIN)
        if r.returncode:
            raise OSError(f"{args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[:300]}")
        return r.stdout

    def displays(self):
        try:
            out = self._run(["xrandr", "--listactivemonitors"], timeout=10).decode()
            mons = []
            for line in out.splitlines()[1:]:          # " 0: +*HDMI-1 1920/530x1080/300+0+0  HDMI-1"
                parts = line.split()
                geo = next((p for p in parts if "x" in p and "+" in p and "/" in p), None)
                if not geo:
                    continue
                wpart, rest = geo.split("x", 1)
                hpart, ox, oy = rest.split("+")
                mons.append((int(ox), int(oy), int(wpart.split("/")[0]), int(hpart.split("/")[0]), "*" in parts[1]))
            if mons:
                mons.sort(key=lambda m: (not m[4], m[0], m[1]))
                return [Display(index=i, x=m[0], y=m[1], w=m[2], h=m[3], primary=m[4], name=f"Display {i + 1}")
                        for i, m in enumerate(mons)]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        w, h = self._run(["xdotool", "getdisplaygeometry"]).decode().split()
        return [Display(index=0, x=0, y=0, w=int(w), h=int(h), primary=True, name="Display 1")]

    def capture(self, x, y, w, h, ow, oh):
        data = self._run(["xwd", "-root", "-silent"], timeout=60)
        W, H, rgb = parse_xwd(data)
        x, y = max(0, min(x, W - 1)), max(0, min(y, H - 1))
        w, h = min(w, W - x), min(h, H - y)
        if (x, y, w, h) != (0, 0, W, H):
            rgb = crop_rgb(W, H, rgb, x, y, w, h)
        return png(ow, oh, scale_rgb(w, h, rgb, ow, oh))

    def move(self, x, y):
        self.queue += ["mousemove", str(int(x)), str(int(y))]

    def button(self, which, down):
        self.queue += ["mousedown" if down else "mouseup", {"left": "1", "middle": "2", "right": "3"}[which]]

    def wheel(self, dx, dy):
        for n, btn in ((dy, "4" if dy > 0 else "5"), (dx, "7" if dx > 0 else "6")):
            if n:
                self.queue += ["click", "--repeat", str(abs(int(n))), "--delay", "30", btn]

    def key(self, name, down):
        self.queue += ["keydown" if down else "keyup", X_KEYS.get(name, name)]

    def text(self, s):
        self.flush()
        self._run(["xdotool", "type", "--delay", "8", "--", s], timeout=120)

    def flush(self):
        with self.lock:
            q, self.queue = self.queue, []
        if q:
            self._run(["xdotool", *q])

    def cursor(self):
        out = self._run(["xdotool", "getmouselocation", "--shell"]).decode()
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        return int(vals["X"]), int(vals["Y"])


def parse_xwd(data):
    """An xwd dump of a 24/32-bit TrueColor screen -> (width, height, RGB bytes)."""
    hdr = struct.unpack(">25I", data[:100])
    header_size, _ver, fmt, depth, w, h = hdr[:6]
    byte_order, bpp, bpl, ncolors = hdr[7], hdr[11], hdr[12], hdr[19]
    red_mask = hdr[14]
    if fmt != 2 or bpp != 32:
        raise OSError(f"unsupported screen format (xwd format {fmt}, {bpp} bits per pixel, depth {depth})")
    off = header_size + ncolors * 12
    px = data[off:off + bpl * h]
    if bpl != w * 4:
        px = b"".join(px[r * bpl:r * bpl + w * 4] for r in range(h))
    if byte_order == 1:                          # MSBFirst: X R G B
        px = bytes(px)
        rgb = bytearray(w * h * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = px[1::4], px[2::4], px[3::4]
        return w, h, rgb
    rgb = bgrx_to_rgb(px, w * h)                 # LSBFirst: B G R X
    if red_mask == 0xFF:                         # an RGB-ordered visual: swap back
        rgb[0::3], rgb[2::3] = rgb[2::3], rgb[0::3]
    return w, h, rgb


# ------------------------------------------------------------------------------------------------ choosing one
_backend = None
_backend_lock = threading.Lock()


def _env_list(name):
    v = os.environ.get(name) or ""
    if not v:
        return None
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else None
    except ValueError:
        return v.split()


def make(kind=None):
    """The backend for this computer, or for KCH_COMPUTER_BACKEND (windows, mac, x11, dryrun) when set.
    KCH_COMPUTER_DISPLAY / KCH_COMPUTER_WRAP point the X11 one at another X display (a WSL one, for tests);
    KCH_COMPUTER_MONITORS (JSON [[x, y, w, h, primary], ...]) and KCH_COMPUTER_LOG set up a dry run."""
    kind = (kind or os.environ.get("KCH_COMPUTER_BACKEND") or "").lower()
    if kind == "dryrun":
        mons = _env_list("KCH_COMPUTER_MONITORS") or [[0, 0, 1920, 1080, 1]]
        return WindowsDesktop(dry=True, monitors=mons, log=os.environ.get("KCH_COMPUTER_LOG")) if WINDOWS else \
            _DryAnywhere(mons, os.environ.get("KCH_COMPUTER_LOG"))
    if kind == "x11" or (not kind and not WINDOWS and not MAC):
        if not kind:
            if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
                raise Unsupported("this is a Wayland desktop, which does not let apps read the screen or send input to "
                                  "other apps; computer use works on X11 sessions (pick \"on Xorg\" at the login screen)")
            if not os.environ.get("DISPLAY") and not os.environ.get("KCH_COMPUTER_DISPLAY"):
                raise Unsupported("no desktop to control (no X11 display: DISPLAY is not set)")
        return X11Desktop(os.environ.get("KCH_COMPUTER_DISPLAY"), _env_list("KCH_COMPUTER_WRAP"))
    if kind == "mac" or (not kind and MAC):
        return MacDesktop()
    if kind == "windows" or (not kind and WINDOWS):
        if not WINDOWS:
            raise Unsupported("the Windows backend only runs on Windows")
        return WindowsDesktop()
    raise Unsupported(f"unknown computer-use backend {kind!r}")


class _DryAnywhere(WindowsDesktop):
    """The dry run on macOS and Linux: the same recorder, no Windows calls."""

    def __init__(self, mons, log):
        super().__init__(dry=True, monitors=mons, log=log)

    def _init(self):
        self._ready = True


def get():
    """The shared backend (made on first use). Raises Unsupported with a reason the user can act on."""
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = make()
        return _backend


def reset():
    global _backend
    with _backend_lock:
        _backend = None


def support():
    """(ok, label or reason) without touching the screen."""
    try:
        b = make()
        return True, b.name + (" (dry run: nothing is really sent)" if getattr(b, "dry", False) else "")
    except Unsupported as e:
        return False, str(e)


def capture_screen(chat_id="", display=0):
    """Capture a desktop screenshot as PNG for the composer's 'See my screen' button."""
    from . import config, store
    try:
        b = get()
    except Unsupported as e:
        return {"ok": False, "error": str(e)}
    try:
        disp = int(display or 0)
    except (TypeError, ValueError):
        disp = 0
    if chat_id:
        try:
            folder = store.Chat(chat_id).dir / "attachments"
        except ValueError:
            folder = config.DATA / "cache" / "screens"
    else:
        folder = config.DATA / "cache" / "screens"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"screen-{int(time.time() * 1000)}.png"
    out = folder / name
    try:
        ds = b.displays()
        if not ds:
            return {"ok": False, "error": "No displays available"}
        d = ds[disp] if 0 <= disp < len(ds) else ds[0]
        w, h = int(d["w"]), int(d["h"])
        scale = min(1.0, 1920.0 / max(1, w), 1200.0 / max(1, h))
        ow, oh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        png_bytes = b.capture(int(d["x"]), int(d["y"]), w, h, ow, oh)
        out.write_bytes(png_bytes)
        return {"ok": True, "path": str(out), "width": ow, "height": oh, "size": len(png_bytes)}
    except Exception as e:
        return {"ok": False, "error": f"Screen capture failed: {e}"}
