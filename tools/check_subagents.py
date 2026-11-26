"""Read-only check for named subagents and all-model subagent support on a live Flock server."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _fetch(base, path):
    req = urllib.request.Request(f"{base}{path}", headers={"X-KC-App": "1"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def main():
    ap = argparse.ArgumentParser(description="Verify named subagents and all-model subagent API on a running Harness.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("KCH_PORT") or 8785))
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"

    st_p, body_p = _fetch(base, "/api/subagents/presets")
    if st_p != 200:
        print(f"FAIL: GET /api/subagents/presets returned HTTP {st_p}: {body_p[:200]}")
        return 1
    try:
        data_p = json.loads(body_p)
    except ValueError as e:
        print(f"FAIL: /api/subagents/presets invalid JSON: {e}")
        return 1
    if not isinstance(data_p.get("presets"), list) or not isinstance(data_p.get("models"), list):
        print(f"FAIL: /api/subagents/presets missing presets/models lists: {list(data_p.keys())}")
        return 1

    st_s, body_s = _fetch(base, "/api/state")
    if st_s != 200:
        print(f"FAIL: GET /api/state returned HTTP {st_s}: {body_s[:200]}")
        return 1
    state = json.loads(body_s)
    cfg = state.get("config") or {}
    sa = cfg.get("subagents") or {}
    if not isinstance(sa.get("presets"), list):
        print("FAIL: /api/state config.subagents.presets is not a list")
        return 1

    model_ids = {m[0] for m in data_p["models"] if isinstance(m, list) and m}
    for ep in cfg.get("endpoints") or []:
        if ep.get("kind") == "agy" and ep.get("detected") is not False:
            if ep.get("id") not in model_ids:
                print(f"FAIL: agy endpoint {ep.get('id')!r} missing from subagent models: {sorted(model_ids)}")
                return 1

    st_m, body_m = _fetch(base, "/static/sheets.js")
    if st_m != 200 or "openSubagentsSheet" not in body_m or "openReportMainSheet" not in body_m or "/api/subagents/presets" not in body_m:
        print(f"FAIL: /static/sheets.js missing embedded Subagents/Report sheet wiring (HTTP {st_m})")
        return 1

    st_a, body_a = _fetch(base, "/static/app.js")
    if st_a != 200 or "subBackBar" not in body_a or "spawnSubagent" not in body_a or "report-main" not in body_a:
        print(f"FAIL: /static/app.js missing embedded subagent thread wiring (HTTP {st_a})")
        return 1

    st_set, body_set = _fetch(base, "/static/settings.js")
    if st_set != 200 or "/api/subagents/presets" not in body_set:
        print(f"FAIL: /static/settings.js missing Named subagents UI wiring (HTTP {st_set})")
        return 1

    st_b, body_b = _fetch(base, "/api/busy")
    if st_b != 200 or "subagents" not in body_b:
        print(f"FAIL: /api/busy missing subagents list (HTTP {st_b})")
        return 1

    print(f"OK: /api/subagents/presets live ({len(data_p['presets'])} presets, {len(model_ids)} models) and UI assets served on {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
