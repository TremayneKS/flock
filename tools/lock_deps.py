"""Regenerate the pinned dependency locks in deps/ (for maintainers; needs uv and npm).

  python tools/lock_deps.py [--exclude-newer YYYY-MM-DD]

deps/search.lock, deps/dictation.lock, deps/dictation-cuda.lock: every package and dependency at an exact version with
the sha256 of each of its files on PyPI, resolved for every OS and Python 3.11+ (`uv pip compile --universal
--generate-hashes`). install.py installs them with `pip install --require-hashes`, so a changed file fails to install.
deps/browser/package.json + package-lock.json: @playwright/mcp at mcp.PLAYWRIGHT_MCP with npm's sha512 integrity for
each package; install.py runs `npm ci` on them.

--exclude-newer (default: 14 days ago) leaves out releases newer than that date, so a just-published (possibly
hijacked) version is never picked up. Edit requirements-search.txt / requirements-dictation.txt or mcp.PLAYWRIGHT_MCP,
then run this, then `python install.py` to install what it wrote.
"""
import argparse
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPS = ROOT / "deps"
sys.path.insert(0, str(ROOT))
CUDA = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*"]      # faster-whisper on an NVIDIA GPU (CTranslate2 wants CUDA 12)


def run(cmd, cwd=ROOT):
    print("$ " + " ".join(map(str, cmd)), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, cwd=cwd)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--exclude-newer", default=(datetime.date.today() - datetime.timedelta(days=14)).isoformat())
    a = ap.parse_args()
    uv, npm = shutil.which("uv"), shutil.which("npm.cmd") or shutil.which("npm")
    if not uv or not npm:
        sys.exit("needs uv (https://docs.astral.sh/uv/) and npm on PATH")
    DEPS.mkdir(exist_ok=True)
    common = ["pip", "compile", "--universal", "--generate-hashes", "--python-version", "3.11", "--quiet",
              "--exclude-newer", a.exclude_newer, "--custom-compile-command", "python tools/lock_deps.py"]
    # relative paths only: uv writes them into the locks' "via" comments
    run([uv, *common, "requirements-search.txt", "-o", "deps/search.lock"])
    run([uv, *common, "requirements-dictation.txt", "-o", "deps/dictation.lock"])
    with tempfile.TemporaryDirectory() as t:
        (Path(t) / "cuda.txt").write_text("\n".join(CUDA) + "\n", encoding="utf-8")
        run([uv, *common, "cuda.txt", "-o", DEPS / "dictation-cuda.lock"], cwd=t)
    from harness import mcp
    browser = DEPS / "browser"
    browser.mkdir(exist_ok=True)
    (browser / "package.json").write_text(json.dumps({
        "name": "flock-tools", "private": True,
        "description": "The Browser tool for Flock: install.py copies this and package-lock.json into KCH_HOME/tools "
                       "and runs npm ci. Regenerate with python tools/lock_deps.py.",
        "dependencies": {"@playwright/mcp": mcp.PLAYWRIGHT_MCP}}, indent=2) + "\n", encoding="utf-8")
    (browser / "package-lock.json").unlink(missing_ok=True)
    run([npm, "install", "--package-lock-only", "--no-audit", "--no-fund", "--loglevel", "error"], cwd=browser)
    print("\nwritten:", ", ".join(str(p.relative_to(ROOT)) for p in sorted(DEPS.rglob("*")) if p.is_file()))


if __name__ == "__main__":
    main()
