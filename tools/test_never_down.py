#!/usr/bin/env python3
"""The chats page must always start, whatever corrupted or half-written files exist on disk.

Breaks chat, sections, and groups records in every way (wrong types, half-written JSON, records of the
wrong shape, a failing subsystem) and verifies that start_state(), chat_list(), and selfcheck() never raise
and good chats stay listed. Scratch folder only, no network.

    python -X utf8 tools/test_never_down.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="kch-never-down-"))
os.environ["KCH_HOME"] = str(TMP)
os.environ["KCH_PROFILE"] = str(TMP / "profile.json")
(TMP / "profile.json").write_text(json.dumps({"endpoints": [
    {"id": "claude", "name": "Claude", "kind": "claude-code", "model": "opus"},
]}), encoding="utf-8")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import config, groups, sections, server, store  # noqa: E402

for d in (config.CHATS, config.LOGS, config.WORKSPACE):
    d.mkdir(parents=True, exist_ok=True)
config.load()
FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name, ("  " + str(detail)[:300]) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def fresh():
    store._list_cache.clear()
    for mod in (sections,):
        for name in ("_cache", "_state", "_loaded"):
            if isinstance(getattr(mod, name, None), dict):
                getattr(mod, name).clear()


def listing(label):
    fresh()
    try:
        st = server.start_state()
        cl = server.chat_list(None)
        json.dumps(st, default=str), json.dumps(cl, default=str)
        sc = server.selfcheck()
        return cl, sc
    except Exception as e:
        check("%s: the page still starts" % label, False, "%s: %s" % (e.__class__.__name__, e))
        return {"chats": [], "trouble": []}, {"ok": False, "problems": [str(e)]}


def ids(cl):
    return {c.get("id") for c in cl["chats"]}


now = time.time()
for cid, title in (("good-1", "A saved chat"), ("good-2", "Another saved chat")):
    write(config.CHATS / cid / "chat.json", {"id": cid, "title": title, "project": str(config.WORKSPACE), "endpoint": "claude",
                                              "created": now - 500, "updated": now - 100})
GOOD = {"good-1", "good-2"}

cl, sc = listing("healthy")
check("a healthy app lists every chat", GOOD <= ids(cl), ids(cl))
check("a healthy app has no trouble and passes its own check", not cl["trouble"] and sc["ok"], (cl["trouble"], sc))

# ---- saved chats of the wrong shape
for i, bad in enumerate(([1, 2], "", '{"id": "half', {"id": "odd-1", "title": 5, "project": 7, "updated": "tomorrow", "created": []},
                         {"id": "odd-2", "updated": None, "group": {"a": 1}, "parent": 3, "pinned": "yes"}, {"title": "no id"})):
    write(config.CHATS / ("odd-%d" % i) / "chat.json", bad)
cl, sc = listing("odd saved chats")
check("saved chats of the wrong shape do not hide the good ones", GOOD <= ids(cl), (ids(cl), cl["trouble"]))
check("a chat whose details could not be worked out is still listed, and named as trouble",
      "odd-2" in ids(cl) and any(t["part"] == "chat details" for t in cl["trouble"]), (ids(cl), cl["trouble"]))
for i in range(6):
    shutil.rmtree(config.CHATS / ("odd-%d" % i), ignore_errors=True)

# ---- the files beside them
for fname in ("sections.json", "groups.json"):
    f = config.DATA / fname
    keep = f.read_text(encoding="utf-8") if f.exists() else None
    for label, bad in (("half written", '{"sections": [{"id": "ge'), ("a list", "[1, 2]"), ("empty", ""),
                       ("wrong types", json.dumps({"sections": "all", "chats": [1], "groups": 4, "archived": "no"}))):
        write(f, bad)
        cl, sc = listing("%s %s" % (fname, label))
        check("%s %s: the chats are still listed" % (fname, label), GOOD <= ids(cl), (ids(cl), cl["trouble"]))
    if keep is None:
        f.unlink()
    else:
        write(f, keep)

# ---- a part that raises outright is left out, named, and comes back by itself
real = groups.all_groups
groups.all_groups = lambda: (_ for _ in ()).throw(RuntimeError("a bug nobody has met yet"))
cl, sc = listing("groups raise")
check("a part that raises is left out, the rest is listed", GOOD <= ids(cl), ids(cl))
check("the answer names the part", any(t["part"] == "groups" for t in cl["trouble"]), cl["trouble"])
check("the self-check fails and says why", not sc["ok"] and any("groups" in p for p in sc["problems"]), sc)
check("the trace is written down", "a bug nobody has met yet" in (config.LOGS / "errors.log").read_text(encoding="utf-8"))
groups.all_groups = real
cl, sc = listing("groups back")
check("when the part works again the trouble clears", not cl["trouble"] and sc["ok"], (cl["trouble"], sc))

shutil.rmtree(TMP, ignore_errors=True)
print("\n%s (%d failed)" % ("ALL PASS" if not FAILS else "FAILED: " + ", ".join(FAILS), len(FAILS)))
sys.exit(1 if FAILS else 0)
