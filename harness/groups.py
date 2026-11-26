"""Chat groups: named folders in the chat list, for any chat (made here or imported), nested as deep as the user likes.

DATA/groups.json holds the groups ({id, name, parent, order, collapsed, project, key}); each chat's own chat.json says
which group it is in ("group": id). Both live in KCH_HOME, so they survive restarts and app updates. Deleting a group
never deletes a chat: its chats and subgroups move up to the group it was in (or out of groups altogether).
`project` is the project the group was made in: an empty group shows there, a group with chats shows wherever they are.
`key` lets an importer find the group it made before (a renamed group keeps its chats on the next import)."""
import json
import os
import secrets
import threading
import time

from . import config, store

_lock = threading.RLock()


def _path():
    return config.DATA / "groups.json"


def _load():
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [g for g in d.get("groups") or [] if isinstance(g, dict) and g.get("id")]


def _save(gs):
    config.DATA.mkdir(parents=True, exist_ok=True)
    tmp = config.DATA / store._tmp_name("groups.json")
    tmp.write_text(json.dumps({"version": 1, "groups": gs}, indent=1, ensure_ascii=False), encoding="utf-8")
    store._replace(tmp, _path())


def _name(name):
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise ValueError("give the group a name")
    return name


def all_groups():
    with _lock:
        return _load()


def get(gid):
    return next((g for g in all_groups() if g["id"] == gid), None)


def by_key(key):
    return next((g for g in all_groups() if key and g.get("key") == key), None)


def _descendants(gs, gid):
    out, todo = set(), [gid]
    while todo:
        cur = todo.pop()
        for g in gs:
            if g.get("parent") == cur and g["id"] not in out:
                out.add(g["id"])
                todo.append(g["id"])
    return out


def _renumber(gs, parent):
    sib = sorted((g for g in gs if g.get("parent") == parent), key=lambda g: g.get("order", 0))
    for i, g in enumerate(sib):
        g["order"] = i


def create(name, parent=None, project=None, key=None, first=True):
    with _lock:
        gs = _load()
        if parent and not any(g["id"] == parent for g in gs):
            raise ValueError("no such group")
        sib = [g.get("order", 0) for g in gs if g.get("parent") == parent]
        g = {"id": "g" + time.strftime("%Y%m%d") + "-" + secrets.token_hex(4), "name": _name(name), "parent": parent or None,
             "order": (min(sib) - 1 if first else max(sib) + 1) if sib else 0, "collapsed": False,
             "project": str(project) if project else None, "created": time.time()}
        if key:
            g["key"] = key
        gs.append(g)
        _renumber(gs, g["parent"])
        _save(gs)
        return g


_KEEP = object()


def update(gid, name=None, collapsed=None, parent=_KEEP, before=_KEEP):
    """Rename, collapse or expand, nest under another group (parent), or move before a sibling (before=id, None = last)."""
    with _lock:
        gs = _load()
        g = next((x for x in gs if x["id"] == gid), None)
        if not g:
            raise ValueError("no such group")
        if name is not None:
            g["name"] = _name(name)
        if collapsed is not None:
            g["collapsed"] = bool(collapsed)
        old_parent = g.get("parent")
        if before is not _KEEP and before:
            ref = next((x for x in gs if x["id"] == before), None)
            if not ref or ref["id"] == gid:
                raise ValueError("no such group")
            parent = ref.get("parent")
        if parent is not _KEEP:
            parent = parent or None
            if parent and (parent == gid or parent in _descendants(gs, gid) or not any(x["id"] == parent for x in gs)):
                raise ValueError("a group cannot go inside itself")
            g["parent"] = parent
        if before is not _KEEP or parent is not _KEEP:
            # before a sibling, or last among the (new) parent's groups
            sib = sorted((x for x in gs if x.get("parent") == g["parent"] and x is not g), key=lambda x: x.get("order", 0))
            at = next((i for i, x in enumerate(sib) if before is not _KEEP and x["id"] == before), len(sib))
            sib.insert(at, g)
            for i, x in enumerate(sib):
                x["order"] = i
            if old_parent != g["parent"]:
                _renumber(gs, old_parent)
        _save(gs)
        return g


def _chats_in(gids):
    return [c for c in store.list_chats() if c.get("group") in gids]


def delete(gid):
    """The group goes; its chats and subgroups move up a level. Returns how many chats moved."""
    with _lock:
        gs = _load()
        g = next((x for x in gs if x["id"] == gid), None)
        if not g:
            raise ValueError("no such group")
        up = g.get("parent") or None
        for x in gs:
            if x.get("parent") == gid:
                x["parent"] = up
        gs = [x for x in gs if x["id"] != gid]
        _renumber(gs, up)
        _save(gs)
    moved = _chats_in({gid})
    for c in moved:
        store.Chat(c["id"]).update_meta(touch=False, group=up)
    return len(moved)


def move_chats(ids, gid):
    """Put chats in a group (gid None: out of any group). Not activity: their order in the list stays."""
    if gid and not get(gid):
        raise ValueError("no such group")
    n = 0
    for cid in ids or []:
        chat = store.Chat(cid)
        if not chat.meta_path.exists():
            continue
        chat.update_meta(touch=False, group=gid or None)
        n += 1
    return n


# ------------------------------------------------------------------------------------------------ HTTP (from server.py)
def http_post(h, path, b):
    act = path.rsplit("/", 1)[-1]
    if act == "create":
        g = create(b.get("name"), b.get("parent"), b.get("project"))
        if b.get("section") and not g.get("parent"):
            from . import sections
            sections.assign_new_group(g["id"], b.get("section"))
        if b.get("chats"):
            move_chats(b["chats"], g["id"])
        return h._json({"group": g, "groups": all_groups()})
    if act == "update":
        kw = {k: b[k] for k in ("name", "collapsed") if k in b}
        if "parent" in b:
            kw["parent"] = b["parent"]
        if "before" in b:
            kw["before"] = b["before"]
        return h._json({"group": update(b.get("id"), **kw), "groups": all_groups()})
    if act == "delete":
        return h._json({"moved": delete(b.get("id")), "groups": all_groups()})
    if act == "move":
        return h._json({"moved": move_chats(b.get("chats") or [], b.get("group") or None)})
    h._json({"error": "unknown"}, 404)
