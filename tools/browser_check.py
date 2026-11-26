"""Try the Browser tool by hand, outside a chat: python tools/browser_check.py PAGE [action ...]
PAGE is a file path (served by the preview server) or a URL. Each extra argument is one more action as JSON, e.g.
'{"action":"eval","script":"document.title"}'. Uses your KCH_HOME (for the installed Playwright MCP) on its own ports
(KCH_PORT, default 8796), and removes the scratch chat it makes when it ends."""
import json
import os
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KCH_PORT", "8796")
from harness import config, mcp, preview, store, tools  # noqa: E402

config.load()
preview.serve()
page = sys.argv[1]
folder = os.path.dirname(page) if os.path.exists(page) else str(config.WORKSPACE)
chat = store.create(folder, "check", title="browser_check (scratch)")
ctx = tools.Ctx(chat, folder, {"id": "check", "vision": False}, lambda ev: None, threading.Event(), {})
try:
    for raw in [json.dumps({"action": "open", "url": page})] + sys.argv[2:]:
        r = tools.run(ctx, "Browser", raw)
        print(f"== {json.loads(raw)['action']} ok={r.get('ok')} {r.get('secs')}s")
        print(r["text"][-2500:])
finally:
    mcp.stop_all()
    shutil.rmtree(chat.dir, ignore_errors=True)
