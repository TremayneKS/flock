"""Bring things over from Claude Code and Codex: their skills and MCP servers.

Skills are linked by folder (so an update in Claude Code shows up here too). MCP servers are linked by name: the command
and its environment are read from Claude Code's or Codex's own config each time the server starts, so any keys in them are
never copied. Scanning reports names and commands only, never environment values."""
import json
import re
import tomllib
from pathlib import Path

from . import config

HOME = Path.home()


def _skills_under(root, source, label):
    out = []
    root = Path(root)
    if not root.exists():
        return out
    from . import skills
    for p in sorted(root.glob("**/SKILL.md")):
        if any(part in (".git", "node_modules") for part in p.parts):
            continue
        info = skills.parse(p)
        if info:
            out.append({"kind": "skill", "source": source, "where": label, "name": info["name"],
                        "description": info["description"][:300], "path": str(p.parent)})
    return out


def _claude_json():
    try:
        return json.loads((HOME / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _codex_toml():
    try:
        return tomllib.loads((HOME / ".codex" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return {}


def mcp_spec(source, name):
    """The live stdio spec of an MCP server defined in Claude Code or Codex (None if missing or not stdio)."""
    if source == "claude":
        cj = _claude_json()
        srv = (cj.get("mcpServers") or {}).get(name)
        if not srv:
            for proj in (cj.get("projects") or {}).values():
                srv = (proj.get("mcpServers") or {}).get(name)
                if srv:
                    break
        if srv and srv.get("command"):
            return {"command": srv["command"], "args": srv.get("args") or [], "env": srv.get("env") or {}}
    if source == "codex":
        srv = (_codex_toml().get("mcp_servers") or {}).get(name)
        if srv and srv.get("command"):
            return {"command": srv["command"], "args": srv.get("args") or [], "env": srv.get("env") or {}}
    return None


def scan():
    """Everything that could be imported, marked with whether it already is."""
    items = []
    items += _skills_under(HOME / ".claude" / "skills", "claude", "Claude Code skills")
    items += _skills_under(HOME / ".claude" / "plugins" / "marketplaces", "claude", "Claude Code plugins")
    items += _skills_under(HOME / ".codex" / "skills", "codex", "Codex skills")
    items += _skills_under(HOME / ".codex" / "plugins" / "cache", "codex", "Codex plugins")
    cj = _claude_json()
    names = set((cj.get("mcpServers") or {}).keys())
    for proj in (cj.get("projects") or {}).values():
        names |= set((proj.get("mcpServers") or {}).keys())
    for n in sorted(names):
        spec = mcp_spec("claude", n)
        items.append({"kind": "mcp", "source": "claude", "where": "Claude Code MCP", "name": n,
                      "description": (" ".join([spec["command"], *spec["args"]])[:200] if spec else "not a local (stdio) server"),
                      "importable": bool(spec)})
    for n, srv in sorted((_codex_toml().get("mcp_servers") or {}).items()):
        if not isinstance(srv, dict):
            continue
        spec = mcp_spec("codex", n)
        items.append({"kind": "mcp", "source": "codex", "where": "Codex MCP", "name": n,
                      "description": (" ".join([spec["command"], *map(str, spec["args"])])[:200] if spec else "not a local (stdio) server"),
                      "importable": bool(spec)})
    cfg = config.get()
    have_sk = {str(Path(s["path"])).lower() for s in cfg.get("skills") or []}
    have_mcp = {(m["source"], m["name"]) for m in cfg.get("imported_mcp") or []}
    seen = set()
    out = []
    for it in items:
        key = (it["kind"], it.get("path") or it["name"], it["source"])
        if key in seen:
            continue
        seen.add(key)
        it["imported"] = (str(Path(it["path"])).lower() in have_sk) if it["kind"] == "skill" else ((it["source"], it["name"]) in have_mcp)
        out.append(it)
    return out


def apply(selected):
    """selected: [{kind, source, name, path?, on: bool}]"""
    def fn(cfg):
        sk = cfg.setdefault("skills", [])
        mc = cfg.setdefault("imported_mcp", [])
        for it in selected:
            if it["kind"] == "skill":
                p = str(Path(it["path"]))
                sk[:] = [s for s in sk if str(Path(s["path"])).lower() != p.lower()]
                if it.get("on"):
                    sk.append({"path": p, "source": it["source"], "name": it.get("name")})
            elif it["kind"] == "mcp":
                mc[:] = [m for m in mc if not (m["source"] == it["source"] and m["name"] == it["name"])]
                if it.get("on"):
                    mc.append({"name": it["name"], "source": it["source"], "ref": it["name"], "enabled": True})
    config.update(fn)
    return scan()


def imported_tools():
    """Tool schemas of imported MCP servers (named mcp__<server>__<tool>), started on demand."""
    from . import mcp
    out = []
    for item in config.get().get("imported_mcp") or []:
        if not item.get("enabled", True):
            continue
        try:
            srv = mcp.server(item["name"])
            srv.ensure()
        except Exception:
            continue
        for t in srv.tools[:40]:
            name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"mcp__{item['name']}__{t['name']}")[:64]
            out.append({"type": "function", "function": {"name": name, "description": (t.get("description") or "")[:500],
                                                         "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}},
                        "_mcp": (item["name"], t["name"])})
    return out
