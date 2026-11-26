"""The chat store's file swap outlasts a reader holding the target (Windows refuses os.replace while it is open)."""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import store  # noqa: E402


def test_swap_waits_for_a_reader():
    d = tempfile.mkdtemp()
    dst, src = os.path.join(d, "chat.json"), os.path.join(d, "chat.json.tmp")
    Path(dst).write_text("old", encoding="utf-8")
    Path(src).write_text("new", encoding="utf-8")
    h = open(dst, "r")                               # a reader holds the target
    threading.Timer(0.4, h.close).start()
    t = time.time()
    store._replace(src, dst)
    assert Path(dst).read_text(encoding="utf-8") == "new"
    return time.time() - t


def test_swap_gives_up_in_the_end():
    d = tempfile.mkdtemp()
    dst, src = os.path.join(d, "chat.json"), os.path.join(d, "chat.json.tmp")
    Path(dst).write_text("old", encoding="utf-8")
    Path(src).write_text("new", encoding="utf-8")
    if os.name != "nt":
        return
    with open(dst, "r"):
        try:
            store._replace(src, dst, tries=3, wait=0.01)
        except PermissionError:
            return
    raise AssertionError("a held target should end in PermissionError after the tries run out")


if __name__ == "__main__":
    print("swap waited %.2f s for the reader" % test_swap_waits_for_a_reader())
    test_swap_gives_up_in_the_end()
    print("2 of 2 passed")
