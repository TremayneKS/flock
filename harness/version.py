"""The app's version and the repository it updates from.

Version scheme: the VERSION file at the top of the repository holds MAJOR.MINOR.PATCH (semantic versioning). A release is
a commit on the default branch whose VERSION was bumped, tagged vMAJOR.MINOR.PATCH, with a GitHub release of that tag
(its notes are what the app shows as "what's new"). Git checkouts follow the default branch; zip installs follow
releases (or the branch's newest commit while the repository has no releases yet). See selfupdate.py.
"""
from pathlib import Path

APP = Path(__file__).resolve().parent.parent

# The public repository. tools/publish.py --repo-url writes the real address over this placeholder in the public copy;
# while it is the placeholder there are no update checks. profile.json or config.json can override it:
# "updates": {"repo": "https://github.com/TremayneKS/flock"}.
REPO_URL = "https://github.com/TremayneKS/flock"


def version():
    """The installed version from the VERSION file ("0.0.0" if it is missing)."""
    try:
        return (APP / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def parse(v):
    """'v1.2.3' or '1.2.3-beta' -> (1, 2, 3, 0 for a pre-release / 1 for a final release); None if it is not a version."""
    import re
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?(-[0-9A-Za-z.-]+)?", (v or "").strip())
    if not m:
        return None
    return int(m[1]), int(m[2]), int(m[3] or 0), 0 if m[4] else 1
