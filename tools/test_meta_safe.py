"""A chat's settings file is never written over when it could not be read (22 Nov 2026: a failed read counted as an empty
chat, and the write that followed left a subagent's chat.json with two keys: its id, session and parent were gone)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["KCH_HOME"] = tempfile.mkdtemp(prefix="kch-meta-")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import config, store  # noqa: E402

config.load()
PROJ = os.path.join(os.environ["KCH_HOME"], "proj")
os.makedirs(PROJ, exist_ok=True)


def chat():
    c = store.create(PROJ, "fake", reasoning="fast")
    c.append({"role": "user", "content": "hello"})
    c.update_meta(title="Kept", sub_of="parent-1", claude_session="s-1")
    return c


def test_unreadable_settings_are_not_written_over():
    c = chat()
    c.meta_path.write_text("{ not json", encoding="utf-8")
    try:
        c.update_meta(engine_cursor={"x": 1})
    except OSError:
        assert c.meta_path.read_text(encoding="utf-8") == "{ not json"
        return
    raise AssertionError("wrote over settings it could not read")


def test_an_emptied_chat_is_not_given_two_keys():
    c = chat()
    c.meta_path.write_text("{}", encoding="utf-8")
    try:
        c.update_meta(engine_cursor={"x": 1})
    except OSError:
        assert c.meta_path.read_text(encoding="utf-8") == "{}"
        return
    raise AssertionError("turned an emptied chat with messages into a near-empty settings file")


def test_a_normal_update_keeps_everything():
    c = chat()
    m = c.update_meta(engine_cursor={"x": 1})
    assert (m.get("title"), m.get("sub_of"), m.get("claude_session"), m.get("engine_cursor")) == ("Kept", "parent-1", "s-1", {"x": 1}), m
    assert c.meta().get("id") == c.id


def test_a_new_chat_can_still_be_given_settings():
    n = store.Chat(store.new_id())
    n.dir.mkdir(parents=True)
    assert n.update_meta(id=n.id, title="new").get("title") == "new"


def test_each_write_has_its_own_temp_name():
    assert store._tmp_name("chat.json") != store._tmp_name("chat.json")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok  ", fn.__name__)
    print("%d of %d passed" % (len(fns), len(fns)))
