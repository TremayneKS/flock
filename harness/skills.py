"""Skills: folders with a SKILL.md (Claude Code's format: front matter with name and description, then instructions).
The model sees one line per enabled skill and loads one with the Skill tool when the task calls for it."""
import re
from pathlib import Path

from . import config

HOME_SKILLS = config.DATA / "skills"       # skills made for the harness itself
BUNDLED_SKILLS = Path(__file__).resolve().parent.parent / "skills"   # skills shipped with Flock (e.g. charts)


def parse(path):
    """name and description from SKILL.md's front matter."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name, desc = Path(path).parent.name, ""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if m:
        fm = m.group(1)
        n = re.search(r"^name:\s*(.+)$", fm, re.M)
        d = re.search(r"^description:\s*(.+(?:\n[ \t]+.+)*)", fm, re.M)
        if n:
            name = n.group(1).strip().strip('"\'')
        if d:
            desc = re.sub(r"\s+", " ", d.group(1)).strip().strip('"\'')
    return {"name": name, "description": desc, "dir": str(Path(path).parent)}


def enabled():
    out, seen = [], set()
    items = list(config.get().get("skills") or [])
    if HOME_SKILLS.exists():
        items += [{"path": str(p.parent)} for p in HOME_SKILLS.glob("*/SKILL.md")]
    if BUNDLED_SKILLS.exists():
        items += [{"path": str(p.parent), "source": "bundled"} for p in BUNDLED_SKILLS.glob("*/SKILL.md")]
    for it in items:
        if it.get("enabled") is False:
            continue
        info = parse(Path(it["path"]) / "SKILL.md")
        if info and info["name"] not in seen:
            seen.add(info["name"])
            info["source"] = it.get("source", "harness")
            out.append(info)
    return out


def get(name):
    name = (name or "").strip().lower()
    for s in enabled():
        if s["name"].lower() == name:
            return s
    return None


def prompt_block(limit=60):
    lst = enabled()[:limit]
    if not lst:
        return ""
    lines = [f"- {s['name']}: {s['description'][:240]}" for s in lst]
    return "Skills (load one with the Skill tool when a task matches it):\n" + "\n".join(lines)
