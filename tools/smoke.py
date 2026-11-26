"""Drive one chat turn without the UI and print the events: python tools/smoke.py ENDPOINT "message" [project folder]
ENDPOINT is a connection id (Settings > Connections), or "" for the default one. Uses your KCH_HOME and KCH_PROFILE like
the app, on its own ports (KCH_PORT, default 8797) so it can run while the app is open. The chat it makes shows in the app."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KCH_PORT", "8797")
from harness import agent, config, store, preview
config.load()
ep = sys.argv[1]; text = sys.argv[2]
proj = sys.argv[3] if len(sys.argv) > 3 else str(config.WORKSPACE / "smoke")
store.add_project(proj)
preview.serve()
chat = store.create(proj, ep, reasoning=os.environ.get("KCH_REASONING", "fast"))
q = agent.hub.subscribe()
agent.start(chat.id, text)
t0 = time.time(); deltas = 0
while True:
    ev = q.get(timeout=900)
    t = ev["type"]
    if t == "delta":
        deltas += 1; continue
    if t == "reasoning":
        continue
    if t == "tool_draft":
        continue
    if t == "message":
        m = ev["message"]
        if m["role"] == "tool":
            print(f"[{time.time()-t0:6.1f}s] TOOL {m['name']} ok={m.get('ok')} {m.get('secs')}s :: {m['content'][:300]!r}")
        else:
            print(f"[{time.time()-t0:6.1f}s] {m['role'].upper()}: {str(m.get('content'))[:300]!r}")
    elif t == "assistant":
        m = ev["message"]
        calls = [(c['name'], c['arguments'][:160]) for c in m.get('tool_calls') or []]
        print(f"[{time.time()-t0:6.1f}s] ASSISTANT text={m.get('content','')[:300]!r} calls={calls} stats={m.get('stats')}")
    elif t == "turn_end":
        print(f"[{time.time()-t0:6.1f}s] END usage={ev['meta'].get('usage')} ctx={ev['meta'].get('ctx')}")
        break
    else:
        print(f"[{time.time()-t0:6.1f}s] {t} {json.dumps({k:v for k,v in ev.items() if k not in ('chat','type')})[:200]}")
print("chat dir:", chat.dir)
