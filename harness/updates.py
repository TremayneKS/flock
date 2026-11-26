"""Checks what the harness depends on for newer versions (in the background, at most every 12 hours).

Safe ones can be updated from the app: the search server's Python packages. Playwright MCP is reported only: the Browser
tool's argument mapping is tested against one version (mcp.PLAYWRIGHT_MCP), so a newer one is a code change, not a click.
llama.cpp is reported for connections that run on a llama-server (its build comes from the server itself).
Flock itself is checked by selfupdate.py."""
import json
import re
import subprocess
import threading
import time
import urllib.request

from . import config, mcp, osutil

CACHE = config.DATA / "updates.json"
_lock = threading.Lock()


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Flock/1.0", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _run(args, timeout=60, cwd=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd, shell=False, **osutil.hidden())
        return (p.stdout or "").strip()
    except Exception:
        return ""


def _ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:4])


def _search_python():
    spec = mcp.specs().get("web") or {}
    return spec.get("command") if spec.get("builtin") else None


def check(force=False):
    with _lock:
        if not force and CACHE.exists():
            try:
                old = json.loads(CACHE.read_text(encoding="utf-8"))
                if time.time() - old.get("at", 0) < 12 * 3600:
                    return old
            except ValueError:
                pass
        items = []
        cfg = config.get()
        # llama.cpp, for connections served by llama-server: the running build vs the newest numbered build
        from . import providers
        for ep in [e for e in cfg["endpoints"] if e.get("llama")]:
            h = providers.health(ep, timeout=3)
            cur = (h.get("build") or "").split("-")[0]
            try:
                rels = _get_json("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20")
                builds = [r.get("tag_name", "") for r in rels if re.fullmatch(r"b\d+", r.get("tag_name", ""))]
                latest = builds[0] if builds else ""
            except Exception:
                latest = ""
            behind = int(latest[1:]) - int(cur[1:]) if latest and re.fullmatch(r"b\d+", cur) else 0
            items.append({"id": "llama.cpp-" + ep["id"], "name": f"llama.cpp ({ep['name']})",
                          "current": cur or "server off", "latest": latest + (f" ({behind} builds on)" if behind > 0 else ""),
                          "outdated": behind > 0, "action": None,
                          "note": "Benchmark a new build on your model before switching: speed settings are tuned per build."})
        # Playwright MCP (the Browser tool)
        cli = (mcp.specs().get("browser") or {}).get("args", [""])[0]
        if cli:
            try:
                pj = config.expand(cli).parent / "package.json"
                cur = json.loads(pj.read_text(encoding="utf-8")).get("version")
                latest = _run([osutil.npm() or "npm", "view", "@playwright/mcp", "version"], timeout=60)
                items.append({"id": "playwright-mcp", "name": "Playwright MCP (Browser tool)", "current": cur, "latest": latest,
                              "outdated": bool(latest) and _ver(latest) > _ver(cur), "action": None,
                              "note": f"The Browser tool is tested with {mcp.PLAYWRIGHT_MCP}; newer versions may rename arguments."})
            except Exception as e:
                items.append({"id": "playwright-mcp", "name": "Playwright MCP", "error": str(e)[:120]})
        else:
            items.append({"id": "playwright-mcp", "name": "Playwright MCP (Browser tool)", "current": "not installed",
                          "latest": "", "outdated": False, "action": None, "note": "python install.py sets it up"})
        # the web search server's packages
        py = _search_python()
        if py:
            out = _run([py, "-m", "pip", "list", "--outdated", "--format", "json", "--disable-pip-version-check"], timeout=120)
            try:
                pk = [p for p in json.loads(out or "[]") if p["name"].lower() in ("ddgs", "mcp", "trafilatura", "playwright", "httpx")]
                items.append({"id": "search", "name": "Web search server packages",
                              "current": ", ".join(f"{p['name']} {p['version']}" for p in pk) or "up to date",
                              "latest": ", ".join(f"{p['name']} {p['latest_version']}" for p in pk), "outdated": bool(pk),
                              "action": "update" if pk else None})
            except ValueError:
                items.append({"id": "search", "name": "Web search server packages", "error": "pip list failed"})
        # the subscription CLIs (both update themselves)
        if config.claude_exe():
            items.append({"id": "claude", "name": "Claude Code CLI", "current": _run([config.claude_exe(), "--version"]) or "?",
                          "latest": "", "outdated": False, "action": None, "note": "updates itself"})
        if config.codex_exe():
            items.append({"id": "codex", "name": "Codex CLI", "current": _run([config.codex_exe(), "--version"]) or "?",
                          "latest": "", "outdated": False, "action": None, "note": "updates with its app or npm"})
        res = {"at": time.time(), "items": items}
        CACHE.write_text(json.dumps(res, indent=1), encoding="utf-8")
        return res


def update(item_id):
    if item_id == "search":
        py = _search_python()
        if not py:
            return {"ok": False, "out": "the search server is not installed"}
        out = _run([py, "-m", "pip", "install", "-U", "ddgs", "trafilatura", "--disable-pip-version-check"], timeout=600)
        mcp.stop_all()
        return {"ok": True, "out": out[-2000:]}
    return {"ok": False, "out": "no automatic update for this one"}


def check_later():
    threading.Thread(target=lambda: check(False), daemon=True).start()
