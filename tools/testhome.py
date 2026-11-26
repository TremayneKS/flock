"""Every tools/test_*.py imports this before anything from harness: the test gets a data folder of its own.

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import testhome  # noqa: F401,E402

- KCH_HOME is kept if it already is a temp folder or a test home (a folder with harness.config.TEST_MARK in it, for a
  test with real models that needs its own connections: `python tools/testhome.py --mark <folder>` makes one);
  otherwise it becomes a fresh temp folder. So a test run from a shell the app started (KCH_HOME = the live data)
  writes nothing there.
- KCH_DATA is removed (KCH_HOME wins over it in harness/config.py; a test that set only KCH_DATA still used the live
  folder).
- KCH_TEST=1, so harness/config.py refuses any other data folder in this process and in every process it starts
  (a copy of the app a test runs).

harness/config.py enforces it either way: a test_*.py process whose data folder is not a temp folder or a test home
stops at the import. tools/test_testhome.py checks that every test imports this first.
"""
import os
import sys
import tempfile
from pathlib import Path

MARK = ".kch-test-home"           # the same as harness.config.TEST_MARK (this file must not import harness)


def ok(path):
    try:
        p = Path(os.path.expanduser(str(path))).resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return p == tmp or tmp in p.parents or (p / MARK).is_file()


def mark(folder):
    p = Path(folder)
    p.mkdir(parents=True, exist_ok=True)
    (p / MARK).write_text("A test's own data folder (tools/testhome.py). Never the live app's.\n", encoding="utf-8")
    return p


def _setup():
    cur = os.environ.get("FLOCK_HOME") or os.environ.get("NEST_HOME") or os.environ.get("RAVEN_HOME") or os.environ.get("KCH_HOME")
    if cur and ok(cur):
        home = Path(cur)
    else:
        home = Path(tempfile.mkdtemp(prefix="kch-test-"))
    os.environ["KCH_HOME"] = str(home)
    os.environ.pop("FLOCK_HOME", None)
    os.environ.pop("NEST_HOME", None)
    os.environ.pop("RAVEN_HOME", None)
    os.environ.pop("KCH_DATA", None)
    os.environ["KCH_TEST"] = "1"
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    return home


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "--mark":
    print("marked as a test home:", mark(sys.argv[2]))
    sys.exit(0)

HOME = _setup()
