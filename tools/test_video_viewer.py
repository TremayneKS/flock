#!/usr/bin/env python3
"""Playwright verification for embedded video sizing and fullscreen video viewer.

Runs an isolated temp KCH_HOME server on an ephemeral port with real 1920x1080 (wide) and
1080x1920 (portrait) MP4 clips, testing both narrow (390x844) and desktop (1280x800) viewports.

    python -X utf8 tools/test_video_viewer.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="kch-video-"))
os.environ["KCH_HOME"] = str(TMP)
os.environ["KCH_PROFILE"] = str(TMP / "profile.json")
os.environ["KCH_TEST"] = "1"

(TMP / "profile.json").write_text(json.dumps({
    "endpoints": [
        {"id": "mock", "name": "Mock Model", "kind": "openai", "base": "http://127.0.0.1:1/v1", "model": "mock"},
    ],
}), encoding="utf-8")

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import testhome  # noqa: F401,E402
from harness import config, server, store  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

for d in (config.CHATS, config.LOGS, config.WORKSPACE):
    d.mkdir(parents=True, exist_ok=True)
config.load()

FAILS = []


def check(name, cond, detail=""):
    print("%s %s%s" % ("ok  " if cond else "FAIL", name, ("  " + str(detail)[:260]) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_mp4(path: Path, width: int, height: int):
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg is required to generate test MP4 clips"
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c=#223344:s={width}x{height}:d=1.2:r=15",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def launch_browser(p):
    for ch in ("chrome", "msedge", None):
        try:
            return p.chromium.launch(channel=ch, headless=True) if ch else p.chromium.launch(headless=True)
        except Exception:
            continue
    raise RuntimeError("Could not launch Chromium/Chrome/Edge via Playwright")


def main():
    wide_mp4 = config.WORKSPACE / "wide_1080p.mp4"
    tall_mp4 = config.WORKSPACE / "tall_portrait.mp4"
    table_mp4 = config.WORKSPACE / "table_clip.mp4"
    make_mp4(wide_mp4, 1920, 1080)
    make_mp4(tall_mp4, 1080, 1920)
    make_mp4(table_mp4, 1920, 1080)

    ch = store.create(str(config.WORKSPACE), "mock", title="Video Viewer Test")
    ch.append({"role": "user", "content": "Show me the three videos"})
    md = (
        f"Here is the landscape render: `{wide_mp4.as_posix()}`\n\n"
        f"- Portrait clip inside a list item: `{tall_mp4.as_posix()}`\n\n"
        f"| Clip | Path |\n|---|---|\n| Table clip | `{table_mp4.as_posix()}` |\n"
    )
    ch.append({"role": "assistant", "content": md})

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    server.ALLOWED_HOSTS.add(f"127.0.0.1:{port}")
    server.ALLOWED_ORIGINS.add(f"http://127.0.0.1:{port}")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        with sync_playwright() as p:
            browser = launch_browser(p)
            try:
                for label, vp in (("phone", {"width": 390, "height": 844}), ("desktop", {"width": 1280, "height": 800})):
                    ctx = browser.new_context(viewport=vp)
                    page = ctx.new_page()
                    page_errors = []
                    page.on("pageerror", lambda err: page_errors.append(str(err)))
                    page.goto(base_url, wait_until="domcontentloaded")
                    page.wait_for_function("() => window.KCH && typeof window.KCH.openChat === 'function'", timeout=10000)
                    page.evaluate("(cid) => window.KCH.openChat(cid)", ch.id)
                    page.wait_for_selector(".m-media.video video", timeout=10000)
                    # Wait until all 3 videos load metadata (real 1920x1080 / 1080x1920 dimensions)
                    page.wait_for_function(
                        """() => {
                            const vids = [...document.querySelectorAll('.m-media.video video')];
                            return vids.length === 3 && vids.every((v) => v.readyState >= 1 && v.videoWidth > 0);
                        }""",
                        timeout=15000,
                    )

                    metrics = page.evaluate("""() => {
                        const vw = window.innerWidth;
                        const vh = window.innerHeight;
                        const docOverflow = document.documentElement.scrollWidth - vw;
                        const conv = document.getElementById('conv');
                        const convOverflow = conv ? conv.scrollWidth - conv.clientWidth : 0;
                        const items = [...document.querySelectorAll('.m-media.video')].map((w) => {
                            const v = w.querySelector('video');
                            const vr = v.getBoundingClientRect();
                            const wr = w.getBoundingClientRect();
                            const fab = w.querySelector('.m-media-fs-fab');
                            const btn = w.querySelector('.m-media-fs');
                            return {
                                name: w.querySelector('.m-media-name')?.textContent || '',
                                inTd: !!w.closest('td'),
                                intrinsic: [v.videoWidth, v.videoHeight],
                                vw, vh,
                                left: vr.left, right: vr.right, width: vr.width, height: vr.height,
                                wrapWidth: wr.width,
                                hasFab: !!(fab && fab.getBoundingClientRect().width >= 30),
                                hasBtn: !!(btn && btn.getBoundingClientRect().width >= 60),
                            };
                        });
                        return { vw, vh, docOverflow, convOverflow, items };
                    }""")

                    check(f"{label}: zero page JS errors", not page_errors, page_errors)
                    check(f"{label}: zero horizontal doc overflow", metrics["docOverflow"] <= 1, metrics)
                    check(f"{label}: zero horizontal conv overflow", metrics["convOverflow"] <= 1, metrics)
                    check(f"{label}: all 3 videos rendered", len(metrics["items"]) == 3, metrics["items"])
                    for it in metrics["items"]:
                        nm = it["name"]
                        check(f"{label} {nm}: not trapped in <td>", not it["inTd"], it)
                        check(f"{label} {nm}: width clamped within viewport",
                              it["left"] >= -1 and it["right"] <= it["vw"] + 1 and it["width"] <= it["vw"] and it["width"] > 100, it)
                        check(f"{label} {nm}: height clamped so controls stay visible",
                              it["height"] > 60 and it["height"] <= it["vh"] * 0.65 + 2, it)
                        check(f"{label} {nm}: fullscreen buttons visible", it["hasFab"] and it["hasBtn"], it)

                    # Seek first video to 0.4s and click its Fullscreen button
                    page.evaluate("() => { const v = document.querySelector('.m-media.video video'); v.currentTime = 0.4; }")
                    page.locator(".m-media.video .m-media-fs").first.click()
                    page.wait_for_selector(".zoom.zoom-video video", timeout=5000)
                    page.wait_for_function(
                        "() => { const v = document.querySelector('.zoom.zoom-video video'); return v && v.readyState >= 1 && v.currentTime >= 0.2; }",
                        timeout=8000,
                    )
                    fs_box = page.evaluate("""() => {
                        const v = document.querySelector('.zoom.zoom-video video');
                        const r = v.getBoundingClientRect();
                        return { w: r.width, h: r.height, vw: window.innerWidth, vh: window.innerHeight, t: v.currentTime };
                    }""")
                    check(f"{label}: fullscreen video fits viewport & synced currentTime",
                          fs_box["w"] <= fs_box["vw"] + 2 and fs_box["h"] <= fs_box["vh"] + 2 and fs_box["t"] >= 0.2, fs_box)

                    # Flip landscape inside fullscreen viewer
                    page.locator('.zoom.zoom-video [data-act="flip"]').click()
                    land_info = page.evaluate("""() => {
                        const z = document.querySelector('.zoom.zoom-video');
                        const v = z.querySelector('video');
                        const r = v.getBoundingClientRect();
                        return {
                            land: z.classList.contains('land'),
                            pressed: z.querySelector('[data-act="flip"]').getAttribute('aria-pressed'),
                            w: r.width, h: r.height, vw: window.innerWidth, vh: window.innerHeight,
                        };
                    }""")
                    check(f"{label}: landscape flip rotates cleanly within viewport",
                          land_info["land"] and land_info["pressed"] == "true"
                          and land_info["w"] <= land_info["vw"] + 2 and land_info["h"] <= land_info["vh"] + 2,
                          land_info)

                    # Seek inside fullscreen and close; verify time synced back to inline video
                    page.evaluate("() => { document.querySelector('.zoom.zoom-video video').currentTime = 0.7; }")
                    page.locator('.zoom.zoom-video [data-act="close"]').click()
                    closed_ok = page.evaluate("""() => ({
                        zoomGone: !document.querySelector('.zoom.zoom-video'),
                        inlineTime: document.querySelector('.m-media.video video').currentTime,
                    })""")
                    check(f"{label}: fullscreen closes cleanly & syncs time back",
                          closed_ok["zoomGone"] and closed_ok["inlineTime"] >= 0.5, closed_ok)

                    # Also verify top-right FAB opens fullscreen and Escape closes it
                    page.locator(".m-media.video .m-media-fs-fab").nth(1).click()
                    page.wait_for_selector(".zoom.zoom-video", timeout=5000)
                    page.keyboard.press("Escape")
                    check(f"{label}: FAB opens fullscreen & Escape closes it",
                          page.locator(".zoom.zoom-video").count() == 0)

                    ctx.close()
            finally:
                browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\nSummary: %d failed" % len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
