"""Website-building tests for Flock: give the agent a site to build through the app's own API (as a user would from the
window), wait for the turn to end, then report what happened: time, steps, tool calls and failures, tokens read vs taken
from cache, decode speed, the final answer, and which files it made.

  python tools/sitebench.py --port 8780 --endpoint ID --reasoning fast [--only bakery,todo] [--cap-min 25] [--tag run1]

--port is a running app's port; --endpoint a connection id (empty = the app's default). --root (default: KCH_HOME, else
~/.flock) gets the results, ROOT/sitebench/<tag>/<task>.json (+ summary.json), and the projects, ROOT/workspace/sb-<tag>-<task>.
Run it against a test copy of the app (KCH_HOME=another folder, KCH_PORT=8790) so the chats stay out of your own list.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

TASKS = {
    "bakery": (
        "Build a one-page website for 'Crumb & Co', a small artisan bakery. It needs: a hero with a headline and a call to "
        "action button, a menu section with 6 items and prices, opening hours, a contact form (name, email, message) with "
        "client-side validation that shows an error under each bad field, and a footer. It must look good on a phone and on a "
        "laptop. Use index.html, styles.css and script.js. When it is done, open it with Browser and check that submitting the "
        "empty form shows the errors."),
    "todo": (
        "Build a to-do app in index.html, app.js and style.css: add tasks, edit a task by double-clicking it, tick tasks done, "
        "delete them, filter All / Active / Done, show how many are left, and keep everything in localStorage. Then test it "
        "with Browser: add three tasks, tick one, reload the page, and check that all three are still there and one is done."),
    "portfolio": (
        "Build a three-page portfolio site for a photographer called Mira Okafor: index.html (intro and featured work), "
        "gallery.html (a responsive grid of 9 pictures that open in a lightbox when clicked, with next/previous and Escape to "
        "close) and contact.html. All pages share one header with navigation that highlights the current page, and a dark/light "
        "theme switch remembered in localStorage. No external images: make the pictures with CSS gradients or inline SVG. "
        "Check every page in Browser and make sure there are no console errors."),
    "dashboard": (
        "Make a small sales dashboard. Create data.csv with 12 months of sales for 3 products. The page (index.html + "
        "dashboard.js + style.css, no libraries) loads the CSV with fetch, draws a line chart of monthly sales per product and "
        "a bar chart of the yearly totals (canvas or SVG), shows a table that sorts when you click a column header, and has a "
        "product filter that updates the charts and the table. Open it with Browser, check the charts render and there are no "
        "console errors, and fix anything that is broken."),
}


def api(port, path, body=None, timeout=60):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json", "X-KC-App": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_turn(port, chat_id, cap_s):
    """Follow the event stream until this chat's turn ends (or the cap)."""
    t0 = time.time()
    seen = {"steps": 0, "tools": 0}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/events")
    with urllib.request.urlopen(req, timeout=cap_s + 60) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if time.time() - t0 > cap_s:
                api(port, f"/api/chats/{chat_id}/stop", {})
                return "capped", time.time() - t0
            if not line.startswith("data:"):
                continue
            ev = json.loads(line[5:])
            if ev.get("chat") != chat_id:
                continue
            if ev["type"] == "step":
                seen["steps"] += 1
                print(f"  [{time.time() - t0:6.0f}s] step {seen['steps']}", flush=True)
            elif ev["type"] == "tool_start":
                seen["tools"] += 1
                c = ev["call"]
                print(f"  [{time.time() - t0:6.0f}s]   {c['name']} {c['args'][:110]!r}", flush=True)
            elif ev["type"] == "message" and ev["message"].get("role") in ("error", "notice"):
                print(f"  [{time.time() - t0:6.0f}s] {ev['message']['role']}: {ev['message']['content'][:300]}", flush=True)
            elif ev["type"] == "turn_end":
                return "done", time.time() - t0
    return "stream ended", time.time() - t0


def summarise(msgs):
    steps = [m for m in msgs if m.get("role") == "assistant"]
    tools = [m for m in msgs if m.get("role") == "tool"]
    st = [m.get("stats") or {} for m in steps]
    out = {
        "steps": len(steps), "tool_calls": len(tools), "tool_fail": sum(1 for t in tools if t.get("ok") is False),
        "tools_by_name": {}, "fail_examples": [],
        "prompt_new": sum(s.get("prompt_new") or 0 for s in st), "prompt_cached": sum(s.get("prompt_cached") or 0 for s in st),
        "out_tokens": sum(s.get("out_tokens") or 0 for s in st),
        "decode_tps_mean": round(sum(s.get("tps") or 0 for s in st) / max(1, sum(1 for s in st if s.get("tps"))), 1),
        "model_secs": round(sum(s.get("secs") or 0 for s in st), 1),
        "tool_secs": round(sum(t.get("secs") or 0 for t in tools), 1),
        "max_ctx": max([s.get("ctx_tokens") or 0 for s in st] or [0]),
        "final": next((m.get("content") for m in reversed(steps) if m.get("content") and not m.get("tool_calls")), ""),
        "errors": [m.get("content") for m in msgs if m.get("role") == "error"],
    }
    for t in tools:
        out["tools_by_name"][t["name"]] = out["tools_by_name"].get(t["name"], 0) + 1
        if t.get("ok") is False and len(out["fail_examples"]) < 6:
            out["fail_examples"].append(f"{t['name']}: {t['content'][:220]}")
    tot = out["prompt_new"] + out["prompt_cached"]
    out["cache_share"] = round(out["prompt_cached"] / tot, 3) if tot else None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--endpoint", default="")
    ap.add_argument("--reasoning", default="fast")
    ap.add_argument("--only", default="")
    ap.add_argument("--cap-min", type=float, default=25)
    ap.add_argument("--tag", default=time.strftime("%H%M"))
    ap.add_argument("--root", default=os.path.expanduser(os.environ.get("FLOCK_HOME") or os.environ.get("NEST_HOME") or os.environ.get("RAVEN_HOME") or os.environ.get("KCH_HOME") or "~/.flock"))
    a = ap.parse_args()
    names = [n for n in (a.only.split(",") if a.only else TASKS) if n]
    outdir = Path(a.root) / "sitebench" / a.tag
    outdir.mkdir(parents=True, exist_ok=True)
    summary = []
    for name in names:
        proj = str(Path(a.root) / "workspace" / f"sb-{a.tag}-{name}")
        api(a.port, "/api/projects", {"path": proj})
        chat = api(a.port, "/api/chats", {"project": proj, "endpoint": a.endpoint, "reasoning": a.reasoning})
        cid = chat["id"]
        print(f"== {name} ({a.endpoint}, {a.reasoning}) chat {cid} in {proj}", flush=True)
        t0 = time.time()
        api(a.port, f"/api/chats/{cid}/send", {"text": TASKS[name], "reasoning": a.reasoning})
        how, secs = wait_turn(a.port, cid, a.cap_min * 60)
        hist = api(a.port, f"/api/chats/{cid}")
        res = {"task": name, "endpoint": a.endpoint, "reasoning": a.reasoning, "chat": cid, "project": proj, "end": how,
               "wall_s": round(secs, 1), **summarise(hist["messages"]),
               "files": sorted(str(p.relative_to(proj)) for p in Path(proj).rglob("*") if p.is_file())[:60]}
        (outdir / f"{name}.json").write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        summary.append(res)
        print(json.dumps({k: res[k] for k in ("task", "end", "wall_s", "steps", "tool_calls", "tool_fail", "decode_tps_mean",
                                              "prompt_new", "prompt_cached", "cache_share", "max_ctx", "files")}, ensure_ascii=False), flush=True)
        print("  final:", (res["final"] or "")[:600].replace("\n", " "), flush=True)
    (outdir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
