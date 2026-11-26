"""Reasoning levels. Most connections use the harness's own three (Fast / Think / Deep, mapped to each API's switch in
providers.build_body). The Claude Code and Codex CLIs have their own effort settings, so a chat on one of them picks
from that CLI's native levels instead: `claude --effort <level>` and `codex exec -c model_reasoning_effort="<level>"`.
Both CLIs start a new process for every turn (--resume / exec resume), so a change applies from the next turn and the
session (and its prompt cache) carries on.

Where the level comes from, first match wins:
  1. the chat's own pick for this connection (chat meta `effort`: {connection id: level}, set from the top bar);
  2. the connection's default (`effort` on the connection: Settings > Connections, or the profile);
  3. the chat's Fast / Think / Deep translated (FROM_GENERIC), which is how chats from before native levels behave."""

GENERIC = [
    {"id": "fast", "label": "Fast", "hint": "No thinking: fastest"},
    {"id": "think", "label": "Think", "hint": "Thinks before it answers"},
    {"id": "deep", "label": "Deep", "hint": "Thinks hard and checks its work"},
]

NATIVE = {
    # claude --help (2.1.x): "Effort level for the current session (low, medium, high, xhigh, max)"
    "claude-code": [
        {"id": "low", "label": "Low", "hint": "Quickest and lightest on your plan; little thinking"},
        {"id": "medium", "label": "Medium", "hint": "Balanced speed and depth"},
        {"id": "high", "label": "High", "hint": "Thinks more before acting; slower, uses more of your plan"},
        {"id": "xhigh", "label": "X-High", "hint": "Extra deep thinking for hard problems; slower still"},
        {"id": "max", "label": "Max", "hint": "Deepest thinking; slowest, uses the most of your plan"},
    ],
    # Codex's model list (supported_reasoning_levels) for current GPT models; the API takes these values
    "codex": [
        {"id": "low", "label": "Low", "hint": "Fast answers, lighter reasoning; lightest on your plan"},
        {"id": "medium", "label": "Medium", "hint": "Balanced speed and depth (Codex's usual default)"},
        {"id": "high", "label": "High", "hint": "More reasoning for complex work; slower, uses more of your plan"},
        {"id": "xhigh", "label": "X-High", "hint": "Extra reasoning depth; slower still"},
        {"id": "max", "label": "Max", "hint": "Maximum reasoning for the hardest problems; slowest, uses the most"},
    ],
    # Antigravity CLI (agy): model id carries the effort suffix (e.g. gemini-4-neon-high)
    "agy": [
        {"id": "low", "label": "Low", "hint": "Fastest response with light thinking"},
        {"id": "medium", "label": "Medium", "hint": "Balanced speed and thinking"},
        {"id": "high", "label": "High", "hint": "Deep thinking for harder tasks"},
        {"id": "xhigh", "label": "X-High", "hint": "Maximum thinking (Neon and Hydrogen)"},
    ],
}

# Fast / Think / Deep on a native connection (chats from before native levels, and connections with no default)
FROM_GENERIC = {
    "claude-code": {"fast": "low", "think": "high", "deep": "xhigh"},
    "codex": {"fast": "low", "think": "medium", "deep": "high"},
    "agy": {"fast": "low", "think": "high", "deep": "xhigh"},
}


def levels(ep):
    """The levels the UI offers for this connection. A connection may list its own (`effort_levels`: ids from the
    built-in list, e.g. for a model that has fewer), or set `no_thinking: true` when it has no thinking switch."""
    if (ep or {}).get("no_thinking"):
        return [{"id": "fast", "label": "Off", "hint": "This model has no thinking toggle"}]
    native = NATIVE.get((ep or {}).get("kind"))
    if not native:
        return GENERIC
    only = ep.get("effort_levels")
    if not only:
        return native
    return [x for x in native if x["id"] in only] or native


def is_native(ep):
    return (ep or {}).get("kind") in NATIVE


def default(ep, reasoning="fast"):
    """The connection's default native level (None for connections that use Fast / Think / Deep)."""
    if not is_native(ep):
        return None
    ids = [x["id"] for x in levels(ep)]
    for cand in (ep.get("effort"), FROM_GENERIC[ep["kind"]].get(reasoning or "fast")):
        if cand in ids:
            return cand
    return ids[0]


def resolve(ep, meta, reasoning=None):
    """The native level this chat's next turn uses on this connection."""
    ids = [x["id"] for x in levels(ep)]
    pick = ((meta or {}).get("effort") or {}).get(ep.get("id"))
    if pick in ids:
        return pick
    return default(ep, reasoning or (meta or {}).get("reasoning") or "fast")


def label(ep, level):
    return next((x["label"] for x in levels(ep) if x["id"] == level), level)


def used(ep, reasoning):
    """What an OpenAI-compatible request at this Fast / Think / Deep level actually asks for, for the stats line:
    "Think · effort high", "Fast · thinking off", "Deep · deepseek-reasoner"."""
    from .providers import build_body
    name = next((x["label"] for x in GENERIC if x["id"] == reasoning), reasoning or "Fast")
    try:
        body = build_body(ep, [], None, reasoning)
    except Exception:
        return name
    bits = []
    tk = body.get("chat_template_kwargs") or {}
    if "enable_thinking" in tk:
        bits.append("thinking " + ("on" if tk["enable_thinking"] else "off"))
    if (body.get("thinking") or {}).get("type") == "disabled":
        bits.append("thinking off")
    if body.get("reasoning_effort"):
        bits.append("effort " + str(body["reasoning_effort"]))
    if (ep.get("level_models") or {}).get(reasoning):
        bits.append(body.get("model"))
    return " · ".join([name, *bits])


def log_cmd(chat_id, cmd):
    """One line per CLI turn in logs/engines.log, so the flag a turn really got can be checked. Arguments that carry an
    environment (the subagent tool's token) are hidden; long ones are cut."""
    import shlex
    import time
    from . import config
    shown = ["<hidden>" if ("TOKEN" in str(a) or ".env=" in str(a)) else (str(a)[:80] + "..." if len(str(a)) > 80 else str(a))
             for a in cmd]
    try:
        with (config.LOGS / "engines.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {chat_id} {' '.join(shlex.quote(a) for a in shown)}\n")
    except OSError:
        pass
