"""How well Flock chats use Claude's prompt cache, measured from Claude Code's own session files (no model calls).

    python -X utf8 tools/claude_cache_report.py                 # chats in FLOCK_HOME / KCH_HOME (default ~/.flock), last 30 days
    python -X utf8 tools/claude_cache_report.py --home ~/.flock --days 2 --calls
    python -X utf8 tools/claude_cache_report.py --session <path or id>     # one session file, any origin

Each chat's Claude session id is chat.json `claude_session` (or `claude_sessions`, one per model); the file is
~/.claude/projects/*/<id>.jsonl. Every assistant record there carries `usage`: input_tokens (uncached),
cache_creation_input_tokens (written, split 5m/1h), cache_read_input_tokens, output_tokens. One API call shows up as
several records (one per content block) with the same message id: the last one counts.

A "cache break" is a call that reads less than half of what the previous call had in context. The likely cause is
named: a gap longer than the cache lifetime, a model change, compaction, or (none of those) a changed prompt start.
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECTS = Path(os.path.expanduser("~/.claude/projects"))


def session_file(sid):
    if os.path.isfile(sid):
        return Path(sid)
    hits = glob.glob(str(PROJECTS / "*" / f"{sid}.jsonl"))
    return Path(hits[0]) if hits else None


def _ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def calls(path):
    """One row per API call, in order, plus the turn starts (real user prompts) and compactions."""
    by_id, order, events = {}, [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = _ts(e.get("timestamp"))
            if e.get("type") == "assistant":
                m = e.get("message") or {}
                u = m.get("usage") or {}
                if not u or m.get("model") == "<synthetic>":
                    continue
                mid = m.get("id") or f"anon-{len(order)}"
                if mid not in by_id:
                    order.append(mid)
                cc = u.get("cache_creation") or {}
                by_id[mid] = {"t": t, "model": m.get("model"), "in": u.get("input_tokens") or 0,
                              "write": u.get("cache_creation_input_tokens") or 0,
                              "w1h": cc.get("ephemeral_1h_input_tokens") or 0, "w5m": cc.get("ephemeral_5m_input_tokens") or 0,
                              "read": u.get("cache_read_input_tokens") or 0,
                              "out": max(u.get("output_tokens") or 0, (by_id.get(mid) or {}).get("out", 0))}
            elif e.get("type") == "user" and not e.get("isMeta"):
                c = (e.get("message") or {}).get("content")
                tool = isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
                if e.get("isCompactSummary"):
                    events.append((t, "compact"))
                elif not tool:
                    events.append((t, "turn"))
            elif e.get("type") == "system" and e.get("subtype") == "compact_boundary":
                events.append((t, "compact"))
    rows = [by_id[i] for i in order]
    rows.sort(key=lambda r: r["t"] or 0)
    return rows, events


def analyse(rows, events, ttl=3600):
    tot = {k: sum(r[k] for r in rows) for k in ("in", "write", "w1h", "w5m", "read", "out")}
    tot["calls"] = len(rows)
    tot["turns"] = sum(1 for _, k in events if k == "turn")
    breaks, turn_writes = [], []
    turn_times = [t for t, k in events if k == "turn" and t]
    comp_times = [t for t, k in events if k == "compact" and t]
    prev = None
    ti = 0
    for r in rows:
        first_of_turn = False
        while ti < len(turn_times) and r["t"] and turn_times[ti] <= r["t"]:
            first_of_turn = True
            ti += 1
        if first_of_turn:
            turn_writes.append(r["write"] + r["in"])
        if prev:
            had = prev["read"] + prev["write"] + prev["in"]
            if had > 4000 and r["read"] < had * 0.5:
                gap = (r["t"] or 0) - (prev["t"] or 0)
                if r["model"] != prev["model"]:
                    why = f"model changed ({prev['model']} -> {r['model']})"
                elif any(prev["t"] <= c <= r["t"] for c in comp_times):
                    why = "compaction"
                elif gap > ttl:
                    why = f"gap {gap / 60:.0f} min (cache lifetime {ttl // 60} min)"
                elif r["read"] > 0:
                    why = "prompt start changed (system prompt, tools, settings or effort)"
                else:
                    why = "nothing read: new session or whole prompt changed"
                breaks.append({"t": r["t"], "lost": had - r["read"], "why": why, "rewritten": r["write"] + r["in"]})
        prev = r
    tot["breaks"] = breaks
    tot["turn_writes"] = turn_writes
    return tot


def share(t):
    allin = t["in"] + t["write"] + t["read"]
    return t["read"] / allin if allin else 0.0


def fmt(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def report(name, path, since, show_calls=False):
    rows, events = calls(path)
    rows = [r for r in rows if (r["t"] or 0) >= since]
    events = [e for e in events if (e[0] or 0) >= since]
    if not rows:
        return None
    t = analyse(rows, events)
    tw = t["turn_writes"]
    print(f"\n{name}  ({path.name}, {time.strftime('%m-%d %H:%M', time.localtime(rows[0]['t']))} to "
          f"{time.strftime('%m-%d %H:%M', time.localtime(rows[-1]['t']))})")
    print(f"  {t['turns']} turns, {t['calls']} API calls | input: {fmt(t['in'])} uncached, {fmt(t['write'])} written "
          f"(1h {fmt(t['w1h'])}, 5m {fmt(t['w5m'])}), {fmt(t['read'])} read | output {fmt(t['out'])}")
    print(f"  cache share {share(t):.1%} | written per turn: median {fmt(sorted(tw)[len(tw) // 2]) if tw else '-'}, "
          f"max {fmt(max(tw)) if tw else '-'} | written per call {fmt(t['write'] // max(1, t['calls']))}")
    for b in t["breaks"]:
        print(f"  break {time.strftime('%m-%d %H:%M', time.localtime(b['t']))}: {fmt(b['lost'])} not read, "
              f"{fmt(b['rewritten'])} rewritten - {b['why']}")
    if show_calls:
        for r in rows:
            print(f"    {time.strftime('%H:%M:%S', time.localtime(r['t']))} {r['model']:<22} in {r['in']:>6} "
                  f"write {r['write']:>7} read {r['read']:>8} out {r['out']:>6}")
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.environ.get("FLOCK_HOME") or os.environ.get("NEST_HOME") or os.environ.get("RAVEN_HOME") or os.environ.get("KCH_HOME") or str(Path.home() / ".nest"))
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--session", help="one session file or id instead of a Harness home")
    ap.add_argument("--calls", action="store_true", help="list every API call")
    a = ap.parse_args()
    since = time.time() - a.days * 86400
    if a.session:
        p = session_file(a.session)
        if not p:
            sys.exit("session file not found")
        report(a.session, p, 0, a.calls)
        return
    totals = []
    for cj in sorted(glob.glob(os.path.join(a.home, "chats", "*", "chat.json"))):
        try:
            m = json.load(open(cj, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sids = dict(m.get("claude_sessions") or {})
        if m.get("claude_session"):
            sids.setdefault("claude", m["claude_session"])
        for ep, sid in sids.items():
            p = session_file(sid)
            if p:
                t = report(f"{Path(cj).parent.name} [{ep}] {(m.get('title') or '')[:50]}", p, since, a.calls)
                if t:
                    totals.append(t)
    if not totals:
        print("no Claude sessions in that home and period")
        return
    s = {k: sum(t[k] for t in totals) for k in ("in", "write", "read", "out", "calls", "turns")}
    nb = sum(len(t["breaks"]) for t in totals)
    print(f"\nALL: {len(totals)} sessions, {s['turns']} turns, {s['calls']} calls | cache share {share(s):.1%} | "
          f"uncached {fmt(s['in'])}, written {fmt(s['write'])}, read {fmt(s['read'])}, output {fmt(s['out'])} | {nb} breaks")


if __name__ == "__main__":
    main()
