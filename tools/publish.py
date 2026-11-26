"""Copy this repository's tracked files into a separate public repository, after a scan for secrets and personal details.
For maintainers who develop in a private repository (whose history may hold things that must not go public) and publish
from a clean one.

  python tools/publish.py scan [FOLDER]                 scan FOLDER (default: this repository's tracked files)
  python tools/publish.py export DEST [--repo-url URL] [--commit "message"]

export: DEST is the public repository's folder (made and `git init`-ed if it does not exist). Tracked files are copied
over, files DEST tracks that are gone here are deleted, `https://github.com/OWNER/flock` in the docs and in
harness/version.py (the address the app checks for updates) becomes --repo-url, then the scan runs on DEST. With findings, nothing is committed (exit 1). With --commit it commits in DEST;
pushing is left to you.

Scan: key and token shapes, private key blocks, e-mail addresses, home-folder paths, and one regex per line from
--patterns FILE or the KCH_PUBLISH_PATTERNS file (keep that file outside the repository: it lists what is personal).
A line can opt out with the marker "publish-scan: ok".
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER = "https://github.com/OWNER/flock"
URL_FILES = {"harness/version.py"}          # besides the .md files: where the repository address is written
GENERIC = [
    ("API key", r"\bsk-(?:ant-|proj-|orca-|or-v1-)?[A-Za-z0-9_-]{24,}"),
    ("Groq/xAI/OpenRouter key", r"\b(?:gsk_|xai-)[A-Za-z0-9_-]{24,}"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    ("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("AWS key id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("JWT token", r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("e-mail address", r"\b[A-Za-z0-9._%+-]+@(?!example\.|anthropic\.com\b|users\.noreply\.)[A-Za-z0-9-]+\.[A-Za-z0-9.-]*[a-z]{2,}\b"),
    ("home folder path", r"(?i)\b[a-z]:[\\/]+users[\\/]+(?!you\b|me\b|user\b|username\b|name\b|public\b|default\b|\.\.\.)[A-Za-z0-9._-]+"),
    ("home folder path", r"/(?:home|Users)/(?!you\b|me\b|user\b|runner\b|name\b|shared\b)[A-Za-z0-9._-]+/"),
]
SKIP = re.compile(r"\.(woff2?|ico|png|jpe?g|gif|webp|pdf|zip)$", re.I)


def tracked(folder):
    out = subprocess.run(["git", "-C", str(folder), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def patterns(path):
    rules = [(name, re.compile(rx)) for name, rx in GENERIC]
    path = path or os.environ.get("KCH_PUBLISH_PATTERNS")
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rules.append(("personal: " + line, re.compile(line, re.I)))
    return rules


def scan(folder, files, rules, allow=()):
    """allow: exact strings that are meant to be public (the --repo-url written into the copy), ignored by the rules."""
    hits = []
    for rel in files:
        if SKIP.search(rel):
            continue
        try:
            text = (folder / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if "publish-scan: ok" in line:
                continue
            for a in allow:
                line = line.replace(a, "")
            for name, rx in rules:
                m = rx.search(line)
                if m:
                    hits.append(f"{rel}:{n}: {name}: {m.group(0)[:60]!r}")
    return hits


def export(dest, repo_url, message, rules):
    dest = Path(dest).resolve()
    if ROOT == dest or ROOT in dest.parents:
        sys.exit("DEST must be outside this repository")
    dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        sys.exit("commit first: the public copy should match a commit here\n" + dirty)
    files = tracked(ROOT)
    if not repo_url:
        print("note: no --repo-url, so the copy keeps the OWNER placeholder and its app will not check for updates")
    if not (dest / ".git").exists():
        dest.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(dest), "init", "-q", "-b", "main"], check=True)
    keep = set(files)
    for rel in tracked(dest):                                  # removed or renamed here: remove there too
        if rel not in keep:
            (dest / rel).unlink(missing_ok=True)
    for rel in files:
        src, dst = ROOT / rel, dest / rel
        if not src.exists():                                   # deleted in the working tree, not yet committed
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if repo_url and (rel.endswith(".md") or rel in URL_FILES):
            t = dst.read_text(encoding="utf-8")
            if PLACEHOLDER in t:
                dst.write_text(t.replace(PLACEHOLDER, repo_url.rstrip("/").removesuffix(".git")), encoding="utf-8")
    copied = [rel for rel in files if (dest / rel).exists()]
    hits = scan(dest, copied, rules, [repo_url.rstrip("/").removesuffix(".git")] if repo_url else [])
    if hits:
        print("\n".join(hits))
        print(f"\n{len(hits)} finding(s) in {dest}: fix them here (or mark a line 'publish-scan: ok'), then export again. "
              "Nothing committed.")
        sys.exit(1)
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    status = subprocess.run(["git", "-C", str(dest), "status", "--short"], capture_output=True, text=True).stdout
    print(status or "no changes")
    print(f"{len(copied)} files in {dest}, scan clean.")
    if message and status:
        subprocess.run(["git", "-C", str(dest), "commit", "-q", "-m", message], check=True)
        print("committed: " + subprocess.run(["git", "-C", str(dest), "log", "-1", "--format=%h %s"],
                                              capture_output=True, text=True).stdout.strip())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("folder", nargs="?")
    s.add_argument("--patterns")
    e = sub.add_parser("export")
    e.add_argument("dest")
    e.add_argument("--repo-url", default="")
    e.add_argument("--commit", default="")
    e.add_argument("--patterns")
    a = ap.parse_args()
    rules = patterns(a.patterns)
    if a.cmd == "scan":
        folder = Path(a.folder).resolve() if a.folder else ROOT
        files = tracked(folder) if (folder / ".git").exists() else [str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()]
        hits = scan(folder, files, rules)
        print("\n".join(hits) if hits else f"clean: {len(files)} files")
        sys.exit(1 if hits else 0)
    export(a.dest, a.repo_url, a.commit, rules)


if __name__ == "__main__":
    main()
