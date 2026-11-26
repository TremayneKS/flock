"""Copy the standalone Meetings app into meetings/ from its own git repository, so the harness ships the app with its Meeting button.

  python tools/sync_meetings.py PATH_TO_MEETINGS [--ref BRANCH_OR_COMMIT]

Takes the files git tracks at REF (default HEAD), never the working tree, so nothing uncommitted or ignored comes along
(settings.json, data). The agent notes (CLAUDE.md) stay behind. meetings/SOURCE records the commit it came from.
Files are replaced in place (the app may be running from this folder), and files that are gone at REF are deleted.
Then review `git diff meetings/` and run `python tools/publish.py scan` before committing.
"""
import argparse
import io
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "meetings"
LEAVE_OUT = {"CLAUDE.md", "AGENTS.md", ".gitignore", "settings.json"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source")
    ap.add_argument("--ref", default="HEAD")
    a = ap.parse_args()
    git = ["git", "-c", "safe.directory=*", "-C", a.source]
    commit = subprocess.run(git + ["rev-parse", a.ref], capture_output=True, text=True, check=True).stdout.strip()
    archive = subprocess.run(git + ["archive", "--format=tar", commit], capture_output=True, check=True).stdout
    DEST.mkdir(exist_ok=True)
    wanted = {"SOURCE"}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for m in tar.getmembers():
            parts = m.name.split("/")
            if not m.isfile() or parts[0] in LEAVE_OUT or ".." in parts:
                continue
            target = DEST / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            data = tar.extractfile(m).read()
            if not target.exists() or target.read_bytes() != data:
                target.write_bytes(data)
            wanted.add(m.name)
    for old in list(DEST.rglob("*")):
        rel = old.relative_to(DEST).as_posix()
        if old.is_file() and rel not in wanted and "__pycache__" not in rel and rel != "settings.json":
            old.unlink()
    (DEST / "SOURCE").write_text(f"Meetings at commit {commit}\n", encoding="utf-8")
    print(f"{len(wanted) - 1} files from {commit[:12]} into {DEST}")


if __name__ == "__main__":
    sys.exit(main())
