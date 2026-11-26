"""Computer use for the project assistant: a small MCP server (JSON-RPC over stdio) that can look at the screen and
drive the mouse and keyboard. Standard library plus Pillow, Windows only.

The app only lets the assistant call these while it is answering something the user typed, never while it is just
listening to a meeting (see OpusEngine in engines.py).
"""
import base64
import ctypes
import io
import json
import subprocess
import sys
import time
from ctypes import wintypes

import settings

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)      # real pixels, not scaled ones
except OSError:
    user32.SetProcessDPIAware()

MAX_SHOT_WIDTH = 1400
_scale = [1.0, 0, 0]                                    # last screenshot: factor, left, top

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSE = {'move': 0x0001, 'left_down': 0x0002, 'left_up': 0x0004, 'right_down': 0x0008, 'right_up': 0x0010,
         'middle_down': 0x0020, 'middle_up': 0x0040, 'wheel': 0x0800, 'hwheel': 0x1000}
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDED = 0x0002, 0x0004, 0x0001
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}
VK = {'ctrl': 0x11, 'control': 0x11, 'alt': 0x12, 'shift': 0x10, 'win': 0x5B, 'windows': 0x5B, 'enter': 0x0D, 'return': 0x0D,
      'tab': 0x09, 'esc': 0x1B, 'escape': 0x1B, 'space': 0x20, 'backspace': 0x08, 'delete': 0x2E, 'del': 0x2E, 'insert': 0x2D,
      'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22, 'up': 0x26, 'down': 0x28, 'left': 0x25, 'right': 0x27,
      **{f'f{i}': 0x6F + i for i in range(1, 13)}}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('u',)
    _fields_ = [('type', wintypes.DWORD), ('u', _U)]


def _send(*inputs):
    array = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))


def _mouse_event(flag, data=0):
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(0, 0, ctypes.c_ulong(data & 0xFFFFFFFF).value, MOUSE[flag], 0, 0)
    _send(event)


def _key_event(vk=0, up=False, unicode_char=None):
    event = INPUT(type=INPUT_KEYBOARD)
    if unicode_char is not None:
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        event.ki = KEYBDINPUT(0, unicode_char, flags, 0, 0)
    else:
        flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDED if vk in EXTENDED else 0)
        event.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    _send(event)


def _real_xy(x, y):
    """Screenshot pixels to real screen pixels."""
    factor, left, top = _scale
    return int(round(x / factor)) + left, int(round(y / factor)) + top


def _move(x, y):
    rx, ry = _real_xy(x, y)
    user32.SetCursorPos(rx, ry)
    time.sleep(0.03)


def _monitors():
    rects = []
    proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def visit(_monitor, _dc, rect, _data):
        rects.append((rect.contents.left, rect.contents.top, rect.contents.right, rect.contents.bottom))
        return 1

    user32.EnumDisplayMonitors(0, 0, proc_type(visit), 0)
    return rects


def _active_monitor(monitors):
    rect = wintypes.RECT()
    if user32.GetWindowRect(user32.GetForegroundWindow(), ctypes.byref(rect)):
        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        for index, (left, top, right, bottom) in enumerate(monitors):
            if left <= x < right and top <= y < bottom:
                return index
    return 0


def screenshot(monitor=None):
    from PIL import ImageGrab
    monitors = _monitors() or [(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))]
    index = int(monitor) if monitor is not None and 0 <= int(monitor) < len(monitors) else _active_monitor(monitors)
    left, top, right, bottom = monitors[index]
    origin_x, origin_y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    image = ImageGrab.grab(all_screens=True).crop((left - origin_x, top - origin_y, right - origin_x, bottom - origin_y))
    factor = min(1.0, MAX_SHOT_WIDTH / image.width)
    if factor < 1.0:
        image = image.resize((int(image.width * factor), int(image.height * factor)))
    _scale[:] = [factor, left, top]
    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, 'JPEG', quality=75)
    return [{'type': 'image', 'data': base64.b64encode(buffer.getvalue()).decode(), 'mimeType': 'image/jpeg'},
            {'type': 'text', 'text': f'Monitor {index} of {len(monitors)} (numbered from 0), {image.width}x{image.height}. '
                                     'Use these pixels for x and y on your next actions.'}]


def click(x, y, button='left', double=False):
    _move(x, y)
    for _ in range(2 if double else 1):
        _mouse_event(f'{button}_down')
        _mouse_event(f'{button}_up')
        time.sleep(0.05)
    return 'Clicked.'


def drag(x1, y1, x2, y2):
    _move(x1, y1)
    _mouse_event('left_down')
    time.sleep(0.05)
    for step in range(1, 11):
        _move(x1 + (x2 - x1) * step / 10, y1 + (y2 - y1) * step / 10)
    _mouse_event('left_up')
    return 'Dragged.'


def move(x, y):
    _move(x, y)
    return 'Moved.'


def scroll(x, y, clicks):
    """Positive scrolls up, negative scrolls down."""
    _move(x, y)
    _mouse_event('wheel', int(clicks) * 120)
    return 'Scrolled.'


def type_text(text):
    for char in text:
        if char == '\n':
            _key_event(0x0D)
            _key_event(0x0D, up=True)
            continue
        code = char.encode('utf-16-le')
        for i in range(0, len(code), 2):
            unit = int.from_bytes(code[i:i + 2], 'little')
            _key_event(unicode_char=unit)
            _key_event(unicode_char=unit, up=True)
        time.sleep(0.004)
    return 'Typed.'


def press(keys):
    """Keys like 'ctrl+c', 'alt+tab', 'enter'."""
    codes = []
    for part in keys.lower().replace(' ', '').split('+'):
        if part in VK:
            codes.append(VK[part])
        elif len(part) == 1:
            scan = user32.VkKeyScanW(ord(part))
            if scan == -1:
                raise ValueError(f'Unknown key: {part}')
            codes.append(scan & 0xFF)
        else:
            raise ValueError(f'Unknown key: {part}')
    for code in codes:
        _key_event(code)
    for code in reversed(codes):
        _key_event(code, up=True)
    return f'Pressed {keys}.'


def _windows():
    found = []
    get_text = user32.GetWindowTextW
    buffer = ctypes.create_unicode_buffer(300)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and get_text(hwnd, buffer, 300) and buffer.value.strip():
            found.append((hwnd, buffer.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def list_windows():
    return '\n'.join(title for _, title in _windows()) or 'No windows.'


def focus_window(title):
    wanted = title.lower()
    for hwnd, name in _windows():
        if wanted in name.lower():
            user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)
            user32.SetForegroundWindow(hwnd)
            return f'Focused: {name}'
    return f'No window title contains: {title}'


def open_item(target):
    """Open an app, file or web address the normal Windows way."""
    subprocess.Popen(['cmd', '/c', 'start', '', target], creationflags=settings.NO_WINDOW)
    return f'Opened {target}.'


def wait(seconds):
    time.sleep(min(float(seconds), 15))
    return 'Waited.'


def _schema(**props):
    required = [k for k, v in props.items() if not v.pop('optional', False)]
    return {'type': 'object', 'properties': props, 'required': required}


NUM = {'type': 'number'}
TOOLS = {
    'screenshot': (screenshot, 'See one of the user\'s screens: the one with the active window unless you name a monitor number. '
                               'Take one before clicking.', _schema(monitor={'type': 'number', 'optional': True})),
    'click': (click, 'Click at x, y in screenshot pixels.', _schema(
        x=dict(NUM), y=dict(NUM), button={'type': 'string', 'enum': ['left', 'right', 'middle'], 'optional': True},
        double={'type': 'boolean', 'optional': True})),
    'move': (move, 'Move the mouse to x, y.', _schema(x=dict(NUM), y=dict(NUM))),
    'drag': (drag, 'Drag the mouse with the left button held.', _schema(x1=dict(NUM), y1=dict(NUM), x2=dict(NUM), y2=dict(NUM))),
    'scroll': (scroll, 'Scroll at x, y. Positive clicks go up, negative go down.', _schema(x=dict(NUM), y=dict(NUM), clicks=dict(NUM))),
    'type_text': (type_text, 'Type text into whatever has focus.', _schema(text={'type': 'string'})),
    'press': (press, 'Press keys, such as ctrl+c, alt+tab or enter.', _schema(keys={'type': 'string'})),
    'list_windows': (list_windows, 'List the titles of open windows.', _schema()),
    'focus_window': (focus_window, 'Bring a window to the front by part of its title.', _schema(title={'type': 'string'})),
    'open_item': (open_item, 'Open an app, file or web address the normal Windows way.', _schema(target={'type': 'string'})),
    'wait': (wait, 'Wait a few seconds for the screen to settle.', _schema(seconds=dict(NUM))),
}


def reply(request_id, result=None, error=None):
    message = {'jsonrpc': '2.0', 'id': request_id}
    message.update({'error': error} if error else {'result': result})
    sys.stdout.write(json.dumps(message) + '\n')
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        method, request_id = request.get('method'), request.get('id')
        if method == 'initialize':
            reply(request_id, {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                               'capabilities': {'tools': {}}, 'serverInfo': {'name': 'computer', 'version': '1.0'}})
        elif method == 'tools/list':
            reply(request_id, {'tools': [{'name': name, 'description': tool[1], 'inputSchema': tool[2]}
                                         for name, tool in TOOLS.items()]})
        elif method == 'tools/call':
            params = request.get('params') or {}
            tool = TOOLS.get(params.get('name'))
            try:
                if tool is None:
                    raise ValueError('Unknown tool')
                result = tool[0](**(params.get('arguments') or {}))
                content = result if isinstance(result, list) else [{'type': 'text', 'text': str(result)}]
                reply(request_id, {'content': content})
            except Exception as exc:
                reply(request_id, {'content': [{'type': 'text', 'text': f'{type(exc).__name__}: {exc}'}], 'isError': True})
        elif method == 'ping':
            reply(request_id, {})
        elif request_id is not None:
            reply(request_id, error={'code': -32601, 'message': 'Method not found'})


if __name__ == '__main__':
    main()
