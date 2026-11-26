"""Live Claude burn-rate monitor: tracks call rate, token burn, and repeated cache rewrites per chat and overall.

When a chat (or overall Claude usage) spikes past configured thresholds, records an active alert,
logs to logs/burn.log, and emits an in-app SSE `burn_alert` event so the user sees a warning banner with
the chat name and a quick Stop button.
Standard library only.
"""
import json
import threading
import time
import uuid
from collections import deque

from . import config

# Sliding window limits (all configurable under config/profile `"burn_alert": {...}`).
DEFAULTS = {
    "enabled": True,
    "cooldown_s": 600,                  # 10 min per (scope, chat, reason) so alerts never spam
    "chat_calls_10m": 45,               # >= 45 Claude calls in 10 min (~270/hr pace)
    "chat_calls_60m": 150,              # >= 150 Claude calls in 60 min
    "cache_rewrite_min_tokens": 50_000, # a single cache creation >= 50k tokens counts as a rewrite
    "chat_cache_rewrites_30m": 4,       # >= 4 large cache rewrites in 30 min (repeated 5-min misses)
    "chat_written_tokens_30m": 600_000, # >= 600k cache-write tokens in 30 min
    "chat_total_tokens_15m": 15_000_000,# >= 15M total tokens (cached + new + out) in 15 min
    "global_calls_15m": 80,             # >= 80 Claude calls across all chats in 15 min
    "global_written_tokens_30m": 1_000_000,  # >= 1M cache-write tokens across all chats in 30 min
}

_WINDOW_MAX_S = 3600.0
_ALERT_TTL_S = 3600.0
_MAX_ACTIVE_ALERTS = 12

_lock = threading.Lock()
# cid -> deque of [ts, api_id, model, uncached, written, cached, out_tokens, ctx_tokens]
_chat_calls = {}
# (cid, api_id) -> record list (for deduplicating multi-block assistant events and updating out_tokens)
_by_api = {}
# cid -> {"title": str, "sub_of": str, "endpoint": str}
_chat_info = {}
# (scope, cid, reason) -> last alert unix ts
_last_alert = {}
# alert_id -> alert dict
_active_alerts = {}


def _cfg():
    raw = config.get().get("burn_alert")
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out:
                out[k] = v
    return out


def _fmt_k(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _prune_locked(now):
    cutoff = now - _WINDOW_MAX_S
    dead_cids = []
    for cid, q in _chat_calls.items():
        while q and q[0][0] < cutoff:
            old = q.popleft()
            if old[1]:
                _by_api.pop((cid, old[1]), None)
        if not q:
            dead_cids.append(cid)
    for cid in dead_cids:
        _chat_calls.pop(cid, None)
    # Expire old alerts so stale banners do not linger forever
    expired = [aid for aid, a in _active_alerts.items() if now - float(a.get("ts") or 0) > _ALERT_TTL_S]
    for aid in expired:
        _active_alerts.pop(aid, None)


def _log_burn(line):
    try:
        config.LOGS.mkdir(parents=True, exist_ok=True)
        with (config.LOGS / "burn.log").open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except OSError:
        pass


def _publish_alerts(alert=None):
    try:
        from . import agent
        ev = {"type": "burn_alert", "alerts": active_alerts()}
        if alert is not None:
            ev["alert"] = dict(alert)
        agent.hub.publish(ev)
    except Exception:
        pass


def _chat_metrics_locked(cid, now, cfg):
    q = _chat_calls.get(cid)
    if not q:
        return {
            "calls_10m": 0, "calls_15m": 0, "calls_60m": 0,
            "cache_rewrites_30m": 0, "written_30m": 0, "total_15m": 0, "out_15m": 0,
        }
    t10 = now - 600.0
    t15 = now - 900.0
    t30 = now - 1800.0
    rw_min = int(cfg["cache_rewrite_min_tokens"])
    c10 = c15 = c60 = rw30 = w30 = tot15 = out15 = 0
    for rec in q:
        ts, _aid, _mod, unc, w, cached, out_t, _ctx = rec
        c60 += 1
        if ts >= t30:
            w30 += w
            if w >= rw_min:
                rw30 += 1
        if ts >= t15:
            c15 += 1
            tot15 += unc + w + cached + out_t
            out15 += out_t
        if ts >= t10:
            c10 += 1
    return {
        "calls_10m": c10,
        "calls_15m": c15,
        "calls_60m": c60,
        "cache_rewrites_30m": rw30,
        "written_30m": w30,
        "total_15m": tot15,
        "out_15m": out15,
    }


def _make_alert(scope, cid, title, sub_of, reason, summary, metrics, now):
    return {
        "id": "burn-" + uuid.uuid4().hex[:10],
        "ts": now,
        "scope": scope,
        "chat": cid,
        "title": title,
        "sub_of": sub_of,
        "reason": reason,
        "summary": summary,
        "metrics": metrics,
        "dismissed": False,
    }


def record_call(cid, meta, stats, ep=None, api_id=None, now=None, sync_notify=False):
    """Record one Claude API call (deduped per api_id) and trigger alerts if burn thresholds are exceeded.

    Returns a list of newly triggered alert dicts (usually empty).
    """
    if not cid or not isinstance(stats, dict):
        return []
    now = float(now if now is not None else time.time())
    cfg = _cfg()
    if not cfg.get("enabled", True):
        return []

    m = meta if isinstance(meta, dict) else {}
    title = (m.get("sub_preset") or m.get("sub_name") or m.get("title") or "").strip() or cid
    sub_of = str(m.get("sub_of") or "")
    ep_id = str((ep or {}).get("id") or m.get("endpoint") or "claude")
    model = str(stats.get("model") or (ep or {}).get("model") or "claude")
    unc = int(stats.get("prompt_uncached") or 0)
    written = int(stats.get("prompt_written") or 0)
    cached = int(stats.get("prompt_cached") or 0)
    out_t = int(stats.get("out_tokens") or 0)
    ctx_t = int(stats.get("ctx_tokens") or (unc + written + cached))
    aid = str(api_id or stats.get("api_id") or "")

    triggered = []
    with _lock:
        _prune_locked(now)
        _chat_info[cid] = {"title": title, "sub_of": sub_of, "endpoint": ep_id}
        key = (cid, aid) if aid else None
        if key and key in _by_api:
            rec = _by_api[key]
            # Multi-block assistant / message_delta update for the same API call: keep largest counts, do not add a call.
            rec[3] = max(rec[3], unc)
            rec[4] = max(rec[4], written)
            rec[5] = max(rec[5], cached)
            rec[6] = max(rec[6], out_t)
            rec[7] = max(rec[7], ctx_t)
            return []

        rec = [now, aid, model, unc, written, cached, out_t, ctx_t]
        q = _chat_calls.setdefault(cid, deque())
        q.append(rec)
        if key:
            _by_api[key] = rec

        cm = _chat_metrics_locked(cid, now, cfg)
        cooldown = float(cfg.get("cooldown_s") or 600)

        def _maybe_fire(scope, target_cid, target_title, target_sub, reason, summary, metrics):
            ck = (scope, target_cid, reason)
            if now - _last_alert.get(ck, 0.0) < cooldown:
                return
            _last_alert[ck] = now
            # Replace any older active alert for the same (scope, chat, reason) so banners stay clean
            for old_id, old in list(_active_alerts.items()):
                if old.get("scope") == scope and old.get("chat") == target_cid and old.get("reason") == reason:
                    _active_alerts.pop(old_id, None)
            al = _make_alert(scope, target_cid, target_title, target_sub, reason, summary, metrics, now)
            _active_alerts[al["id"]] = al
            if len(_active_alerts) > _MAX_ACTIVE_ALERTS:
                oldest = sorted(_active_alerts.values(), key=lambda x: x["ts"])[0]["id"]
                _active_alerts.pop(oldest, None)
            triggered.append(al)

        # 1. Per-chat call rate spike
        if cm["calls_10m"] >= int(cfg["chat_calls_10m"]):
            pace = int(round(cm["calls_10m"] * 6))
            _maybe_fire(
                "chat", cid, title, sub_of, "call_rate",
                f"{title}: {cm['calls_10m']} Claude calls in 10 min (~{pace}/hr pace, {_fmt_k(cm['written_30m'])} cache writes/30m)",
                cm,
            )
        elif cm["calls_60m"] >= int(cfg["chat_calls_60m"]):
            _maybe_fire(
                "chat", cid, title, sub_of, "call_rate",
                f"{title}: {cm['calls_60m']} Claude calls in the last hour ({_fmt_k(cm['written_30m'])} cache writes/30m)",
                cm,
            )

        # 2. Per-chat repeated 5-min cache rewrites
        if cm["cache_rewrites_30m"] >= int(cfg["chat_cache_rewrites_30m"]) or cm["written_30m"] >= int(cfg["chat_written_tokens_30m"]):
            _maybe_fire(
                "chat", cid, title, sub_of, "cache_rewrites",
                f"{title}: {cm['cache_rewrites_30m']} large cache rewrites ({_fmt_k(cm['written_30m'])} written tokens) in 30 min",
                cm,
            )

        # 3. Per-chat total token burn spike
        if cm["total_15m"] >= int(cfg["chat_total_tokens_15m"]):
            _maybe_fire(
                "chat", cid, title, sub_of, "token_burn",
                f"{title}: {_fmt_k(cm['total_15m'])} Claude tokens in 15 min ({cm['calls_15m']} calls)",
                cm,
            )

        # 4. Global burn across all Claude chats (only when >1 chat is active or global threshold exceeded)
        g_calls_15m = 0
        g_written_30m = 0
        top_cid, top_calls = cid, cm["calls_15m"]
        for ocid in _chat_calls:
            om = _chat_metrics_locked(ocid, now, cfg)
            g_calls_15m += om["calls_15m"]
            g_written_30m += om["written_30m"]
            if om["calls_15m"] > top_calls:
                top_cid, top_calls = ocid, om["calls_15m"]
        top_info = _chat_info.get(top_cid) or {"title": title, "sub_of": sub_of}
        g_metrics = {
            "global_calls_15m": g_calls_15m,
            "global_written_30m": g_written_30m,
            "active_chats": len(_chat_calls),
            "top_chat": top_cid,
            "top_title": top_info["title"],
        }
        if g_calls_15m >= int(cfg["global_calls_15m"]) or g_written_30m >= int(cfg["global_written_tokens_30m"]):
            _maybe_fire(
                "global", top_cid, top_info["title"], top_info.get("sub_of") or "", "global_burn",
                f"Overall Claude burn high: {g_calls_15m} calls/15m, {_fmt_k(g_written_30m)} cache writes/30m (top chat: {top_info['title']})",
                g_metrics,
            )

    if triggered:
        for al in triggered:
            _log_burn(f"ALERT scope={al['scope']} reason={al['reason']} chat={al['chat']} {al['summary']}")
            _publish_alerts(al)
    return triggered


def active_alerts(now=None):
    """Return active (non-dismissed, non-expired) burn alerts, newest first."""
    now = float(now if now is not None else time.time())
    with _lock:
        _prune_locked(now)
        rows = [dict(a) for a in _active_alerts.values() if not a.get("dismissed")]
    rows.sort(key=lambda x: x["ts"], reverse=True)
    return rows


def dismiss(alert_id=None, all_alerts=False):
    """Dismiss one alert (or all active alerts) and broadcast the updated banner state."""
    changed = False
    with _lock:
        if all_alerts:
            if _active_alerts:
                _active_alerts.clear()
                changed = True
        elif alert_id and alert_id in _active_alerts:
            _active_alerts.pop(alert_id, None)
            changed = True
    if changed:
        _publish_alerts(None)
    return active_alerts()


def snapshot(now=None):
    """Live summary of per-chat and overall Claude burn rates + active alerts (for GET /api/burn)."""
    now = float(now if now is not None else time.time())
    cfg = _cfg()
    chats = []
    g_calls_10m = g_calls_15m = g_calls_60m = g_written_30m = g_rewrites_30m = g_total_15m = 0
    with _lock:
        _prune_locked(now)
        for cid in list(_chat_calls):
            cm = _chat_metrics_locked(cid, now, cfg)
            info = _chat_info.get(cid) or {}
            g_calls_10m += cm["calls_10m"]
            g_calls_15m += cm["calls_15m"]
            g_calls_60m += cm["calls_60m"]
            g_written_30m += cm["written_30m"]
            g_rewrites_30m += cm["cache_rewrites_30m"]
            g_total_15m += cm["total_15m"]
            chats.append({
                "id": cid,
                "title": info.get("title") or cid,
                "sub_of": info.get("sub_of") or "",
                "endpoint": info.get("endpoint") or "claude",
                **cm,
            })
        alerts = [dict(a) for a in _active_alerts.values() if not a.get("dismissed")]
    chats.sort(key=lambda c: (c["calls_10m"], c["written_30m"], c["calls_60m"]), reverse=True)
    alerts.sort(key=lambda x: x["ts"], reverse=True)
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "thresholds": {k: cfg[k] for k in DEFAULTS if k != "enabled"},
        "global": {
            "calls_10m": g_calls_10m,
            "calls_15m": g_calls_15m,
            "calls_60m": g_calls_60m,
            "cache_rewrites_30m": g_rewrites_30m,
            "written_30m": g_written_30m,
            "total_15m": g_total_15m,
        },
        "chats": chats,
        "alerts": alerts,
    }


def reset():
    """Clear in-memory state (for isolated unit tests)."""
    with _lock:
        _chat_calls.clear()
        _by_api.clear()
        _chat_info.clear()
        _last_alert.clear()
        _active_alerts.clear()
