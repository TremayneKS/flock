"""What differs between Windows, macOS and Linux: hidden child processes, process groups, whether a process is alive,
killing a process tree, and which bash to use."""
import os
import shutil
import signal
import subprocess
from pathlib import Path

WINDOWS = os.name == "nt"
NOWIN = 0x08000000 if WINDOWS else 0          # CREATE_NO_WINDOW: no console window flashes up
_GROUP = 0x00000200                            # CREATE_NEW_PROCESS_GROUP
_DETACHED = 0x00000008                         # DETACHED_PROCESS
_BREAKAWAY = 0x01000000                        # CREATE_BREAKAWAY_FROM_JOB


def hidden():
    """Popen keyword arguments for a helper process with no window."""
    return {"creationflags": NOWIN} if WINDOWS else {}


def group():
    """A child in its own process group, so a Stop can end it and everything it started."""
    return {"creationflags": NOWIN | _GROUP} if WINDOWS else {"start_new_session": True}


def detached(breakaway=False):
    """A child that outlives the harness (a model server launcher)."""
    if WINDOWS:
        return {"creationflags": NOWIN | _DETACHED | _GROUP | (_BREAKAWAY if breakaway else 0)}
    return {"start_new_session": True}


def alive(pid):
    if not pid:
        return False
    if WINDOWS:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return code.value == 259                            # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                                    # our own finished child: reap it so it stops "existing"
        done, _ = os.waitpid(int(pid), os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def created(pid):
    """When a running process started (seconds since the epoch); None if it is not running or the OS does not say."""
    if not pid:
        return None
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            t = [wintypes.FILETIME() for _ in range(4)]
            code = ctypes.c_ulong()
            if not k.GetProcessTimes(h, *(ctypes.byref(x) for x in t)) or not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            if code.value != 259:                               # STILL_ACTIVE
                return None
            return ((t[0].dwHighDateTime << 32) | t[0].dwLowDateTime) / 1e7 - 11644473600
        finally:
            k.CloseHandle(h)
    try:                                                        # Linux: start time in clock ticks after boot
        with open(f"/proc/{int(pid)}/stat") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat") as f:
            boot = next(int(ln.split()[1]) for ln in f if ln.startswith("btime"))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def same_process(pid, started_at=None):
    """Is pid still the process that was started around started_at? Windows soon hands a finished process's number to
    another one, so a bare "is pid alive" can mistake a stranger for our launcher."""
    if not alive(pid):
        return False
    if not started_at:
        return True
    c = created(pid)
    return c is None or abs(c - float(started_at)) < 20


def kill_tree(pid, timeout=20):
    if not pid:
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NOWIN, timeout=timeout)
        return
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except OSError:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass


def terminate_only(pid, timeout=10):
    """Terminate ONLY the given process without tree-killing detached/background children (e.g. preview servers)."""
    if not pid:
        return
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/PID", str(int(pid))], capture_output=True, creationflags=NOWIN, timeout=timeout)
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        pass


def bash():
    """The Bash tool's shell: FLOCK_BASH / KCH_BASH if set, Git Bash on Windows (never WSL's System32\\bash.exe), else bash."""
    env_bash = os.environ.get("FLOCK_BASH") or os.environ.get("NEST_BASH") or os.environ.get("KCH_BASH")
    if env_bash:
        return env_bash
    if WINDOWS:
        for c in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe")):
            if os.path.exists(c):
                return c
        git = shutil.which("git")
        if git:                                             # ...\Git\cmd\git.exe -> ...\Git\bin\bash.exe
            c = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if c.exists():
                return str(c)
        return ""
    return shutil.which("bash") or "/bin/sh"


def browser_channel():
    """The browser the Browser tool drives (Playwright's --browser): Google Chrome if it is installed, else Microsoft
    Edge, else "chromium" = Playwright's own build, which install.py downloads when neither is there. Same places
    Playwright itself looks."""
    import sys
    if WINDOWS:
        roots = [os.environ.get(k) for k in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")]
        chrome = [os.path.join(r, "Google", "Chrome", "Application", "chrome.exe") for r in roots if r]
        edge = [os.path.join(r, "Microsoft", "Edge", "Application", "msedge.exe") for r in roots if r]
    elif sys.platform == "darwin":
        chrome = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        edge = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        chrome = ["/opt/google/chrome/chrome"]
        edge = ["/opt/microsoft/msedge/msedge"]
    if any(os.path.exists(p) for p in chrome):
        return "chrome"
    if any(os.path.exists(p) for p in edge):
        return "msedge"
    return "chromium"


def which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


def npm():
    return which("npm.cmd", "npm") if WINDOWS else which("npm")


def venv_python(venv):
    return Path(venv) / ("Scripts/python.exe" if WINDOWS else "bin/python")


def os_name():
    import platform
    if WINDOWS:
        return f"Windows {platform.release()}"
    if platform.system() == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{platform.system()} {platform.release()}"


def long_path(p, room=0):
    """A path Windows can open past 260 characters (the extended-length form); unchanged elsewhere and when short.
    room: how much longer the paths under it get (a folder whose files run past the limit)."""
    p = Path(p)
    s = str(p)
    if not WINDOWS or s.startswith(EXTENDED) or len(s) + room < 240:
        return p
    s = os.path.abspath(s)
    return Path(EXTENDED + "UNC" + s[1:] if s.startswith("\\\\") else EXTENDED + s)


EXTENDED = "\\\\?\\"
