"""Real handover test: one chat, a few turns of content on one model, then switch to each other model and back.

    python tools/testhome.py --mark /tmp/kch-test-home                   (once: a test home, never the live data)
    set KCH_HOME=/tmp/kch-test-home & set KCH_PROFILE=/tmp/kch-test-home/test-profile.json
    python -X utf8 tools/test_handover.py --home claude --others neon,deepseek,chatgpt,fable

(Without a marked KCH_HOME it runs in a fresh temp folder: tools/testhome.py.)

Each switch asks the same three questions that only the earlier turns can answer, and asks the model to pick a number;
back on the home model it is asked for that number (does it get what it missed?). Prints, per turn: model, the turn's
tokens (in, cached, written, out), handover notices, and the reply. Spends real (small) turns: use low effort.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KCH_PORT", "8799")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testhome  # noqa: F401,E402  (first: a data folder of its own, never the live app's)
from harness import agent, config, store  # noqa: E402

SETUP = [
    "We're planning a small project together. Facts to remember: the cat mascot is called Biscuit, the dev server runs on "
    "port 4321, and the launch date is 3 December. Create a file plan.txt in the project folder with the single line "
    "'codeword: TANGERINE'. Then reply in one short sentence.",
    "In one step, with two tool calls at the same time, create a.txt containing 'alpha' and b.txt containing 'beta'. "
    "Reply in one short sentence.",
    "One more fact: we chose Postgres over SQLite because we need many writers at once. Reply with the single word noted.",
]
QUESTIONS = ("Answer from our conversation so far, in four short numbered lines, without tools: 1) the mascot's name and "
             "the dev port; 2) which files have been created in this chat; 3) why we chose Postgres; 4) pick a random "
             "4-digit number and state it.")
BACK = "What 4-digit number did the other assistant pick in its last reply? Answer in one line, without tools."


def run_turn(chat, text, endpoint):
    q = agent.hub.subscribe()
    n0 = len(store.Chat(chat.id).messages())      # a fresh read: a Chat object keeps the list it first read
    agent.start(chat.id, text, endpoint=endpoint)
    t0 = time.time()
    while True:
        ev = q.get(timeout=900)
        if ev.get("chat") not in (None, chat.id):
            continue
        if ev["type"] == "turn_end":
            break
    agent.hub.unsubscribe(q)
    new = store.Chat(chat.id).messages()[n0:]
    calls = {}
    for m in new:
        st = m.get("stats")
        if m.get("role") == "assistant" and st:
            k = st.get("api_id") or m["id"]
            if k not in calls or (st.get("out_tokens") or 0) >= (calls[k].get("out_tokens") or 0):
                calls[k] = st
    tot = {k: sum((s.get(k) or 0) for s in calls.values())
           for k in ("prompt_new", "prompt_uncached", "prompt_written", "prompt_cached", "out_tokens")}
    tot["in"] = tot["prompt_new"] + tot["prompt_cached"]
    tot["calls"] = len(calls)
    tot["secs"] = round(time.time() - t0, 1)
    notices = [m["content"] for m in new if m.get("role") in ("notice", "error")]
    reply = "\n".join(m.get("content") or "" for m in new if m.get("role") == "assistant" and m.get("content"))
    return tot, notices, reply


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default="claude")
    ap.add_argument("--others", default="neon,deepseek,chatgpt,fable")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    config.load()
    proj = str(config.WORKSPACE / f"handover-{time.strftime('%H%M%S')}")
    os.makedirs(proj, exist_ok=True)
    store.add_project(proj)
    chat = store.create(proj, a.home, reasoning="fast")
    eps = [a.home] + a.others.split(",")
    chat.update_meta(effort={e: a.effort for e in eps})
    print("chat", chat.id, "folder", proj)
    log = []

    def step(ep, text, label):
        tot, notices, reply = run_turn(chat, text, ep)
        row = {"turn": len(log) + 1, "endpoint": ep, "label": label, **tot, "notices": notices, "reply": reply[-700:]}
        log.append(row)
        print(f"\n#{row['turn']} [{ep}] {label}: in {tot['in']} (cached {tot['prompt_cached']}, written "
              f"{tot['prompt_written']}, uncached {tot['prompt_uncached'] or tot['prompt_new'] - tot['prompt_written']}) "
              f"out {tot['out_tokens']} calls {tot['calls']} {tot['secs']}s")
        for n in notices:
            print("   NOTICE:", n[:300])
        print("   REPLY:", reply[-500:].replace("\n", " | "))

    for i, t in enumerate(SETUP):
        step(a.home, t, f"setup {i + 1}")
    for ep in a.others.split(","):
        step(ep, QUESTIONS, f"switch to {ep}")
        step(a.home, BACK, f"back from {ep}")
    meta = store.Chat(chat.id).meta()
    print("\nchat usage:", json.dumps(meta.get("usage")), "\nsessions:", meta.get("claude_session"),
          meta.get("codex_thread"), meta.get("agy_conversation"))
    if a.out:
        json.dump({"chat": chat.id, "log": log, "meta_usage": meta.get("usage"), "claude_session": meta.get("claude_session")},
                  open(a.out, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
