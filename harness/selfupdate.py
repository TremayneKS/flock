"""Flock updating itself from its GitHub repository: Settings > Updates, and a dot on the Settings button.

Where from: version.REPO_URL (tools/publish.py --repo-url writes it into the public copy), or "updates": {"repo": URL} in
profile.json or config.json. No URL (or the OWNER placeholder) = no checks. "updates": {"auto": false} turns the
automatic checks off; "Check now" still works. FLOCK_GITHUB_API / KCH_GITHUB_API points the checks at another API server (tests).

Version scheme (version.py): VERSION holds MAJOR.MINOR.PATCH; a release is the tag vX.Y.Z with a GitHub release of it.

Checks: on start, then at most every 12 hours (an hour after a failed one), in a background thread. Unauthenticated
GitHub API (60 requests an hour per address) with ETags: an unchanged answer comes back as 304, which does not count.
A rate limit waits for GitHub's reset time; no connection is shown as "couldn't check". Three requests per check:
the repository (default branch), the latest release, the branch's newest commit (plus a compare for git checkouts).

Updating:
- git checkout (the app folder has .git): follows its branch. Refuses with local changes, a detached HEAD, no upstream,
  or commits of its own that would stop a fast-forward; otherwise git fetch + git merge --ff-only. Undo = git reset
  --keep to the commit before (refuses if that would lose local changes).
- zip install: follows releases (or the default branch's newest commit while there are none). Downloads the release's
  flock-*.zip asset (checked against GitHub's sha256 digest or a SHA256SUMS asset) or else the release's source
  zip (checked by its layout and VERSION), only from GitHub's hosts, unpacks it in a temporary folder, compiles every
  Python file, then backs up each file it will replace or delete into FLOCK_HOME/updates/backup-<old version>-<time>.zip
  and copies the new files in. An error half way puts the old files back. FLOCK_HOME (chats, keys, config), the profile
  and the tools folder are never touched, even when they sit inside the app folder. Rollback restores the newest backup.
- Restart: a new copy of run.py waits for this one to exit and takes over the same port. A model-server launcher keeps
  running; the new copy adopts it from state/runtime.json like any start.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from . import config, osutil
from . import version as V

APP = V.APP
API = (os.environ.get("FLOCK_GITHUB_API") or os.environ.get("NEST_GITHUB_API") or os.environ.get("KCH_GITHUB_API") or "https://api.github.com").rstrip("/")
CACHE = config.DATA / "app-update.json"
WORK = config.DATA / "updates"                  # backups of zip updates, one zip file each
RECORD = APP / ".kch-install.json"             # zip installs: the version, commit and files this update installed
EVERY = 12 * 3600
RETRY = 3600
MAX_ZIP = 300 * 2 ** 20
KEEP_BACKUPS = 3
GITHUB_HOSTS = {"github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com",
                "release-assets.githubusercontent.com", "github-releases.githubusercontent.com"}
NEEDED = ("VERSION", "run.py", "harness/server.py", "harness/selfupdate.py", "web/index.html")

_lock = threading.RLock()                      # one check or update at a time
_bg = {"running": False, "loop": False}
restarting = False
on_restart = None                              # server.py: stops serving so the process can exit


class Failed(Exception):
    def __init__(self, msg, state="error", retry_at=None):
        super().__init__(msg)
        self.state, self.retry_at = state, retry_at


# ---- settings, what is installed ---------------------------------------------------------------------------------------

def settings():
    u = config.get().get("updates") or {}
    return {"auto": u.get("auto", True) is not False, "repo": str(u.get("repo") or V.REPO_URL or "").strip()}


def repo():
    """(owner, name) of the GitHub repository, or None (none set, the placeholder, or not a GitHub address)."""
    url = settings()["repo"]
    if not url or "/OWNER/" in url:
        return None
    m = re.fullmatch(r"(?:https?://(?:www\.)?github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?", url)
    return (m[1], m[2]) if m else None


def _git(*args, timeout=60):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"}
    try:
        p = subprocess.run(["git", "-C", str(APP), *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, **osutil.hidden())
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _record():
    try:
        return json.loads(RECORD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def current():
    """The installed copy: version, git checkout or zip install, commit."""
    out = {"version": V.version(), "kind": "git" if (APP / ".git").exists() else "zip", "commit": "", "date": "",
           "branch": "", "upstream": ""}
    if out["kind"] == "git":
        c, o, _ = _git("log", "-1", "--format=%H %cs")
        if c == 0 and o:
            out["commit"], out["date"] = (o.split() + [""])[:2]
        c, o, _ = _git("symbolic-ref", "--short", "-q", "HEAD")
        out["branch"] = o if c == 0 else ""
        c, o, _ = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        out["upstream"] = o if c == 0 else ""
    else:
        out["commit"] = _record().get("commit", "")
    return out


def _load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(st):
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
    os.replace(tmp, CACHE)


# ---- the GitHub API ----------------------------------------------------------------------------------------------------

def _get(url, http):
    """GET a GitHub API address as JSON, sending last time's ETag (an unchanged answer is a 304: free). None = 404."""
    hdr = {"User-Agent": f"Flock/{V.version()}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}
    old = http.get(url)
    if old and old.get("etag"):
        hdr["If-None-Match"] = old["etag"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
            http[url] = {"etag": r.headers.get("ETag") or "", "body": body}
            return body
    except urllib.error.HTTPError as e:
        if e.code == 304 and old:
            return old["body"]
        if e.code == 404:
            return None
        remaining, reset, after = (e.headers.get(h) or "" for h in ("X-RateLimit-Remaining", "X-RateLimit-Reset", "Retry-After"))
        if e.code == 429 or (e.code == 403 and (remaining == "0" or after)):
            at = int(reset) if reset.isdigit() else time.time() + (int(after) if after.isdigit() else RETRY)
            raise Failed("GitHub's limit for checks without an account is used up until "
                         + time.strftime("%H:%M", time.localtime(at)), "rate-limited", at)
        raise Failed(f"GitHub answered {e.code} {e.reason}")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise Failed("no connection to GitHub (offline?)", "offline")
    except ValueError:
        raise Failed("GitHub sent an answer that is not JSON")


def _commit(c):
    msg = ((c.get("commit") or {}).get("message") or "").split("\n")[0]
    return {"sha": c.get("sha", ""), "title": msg[:200], "date": ((c.get("commit") or {}).get("committer") or {}).get("date", "")}


def check(force=False):
    """Ask GitHub what is newest and compare it with this copy. Saves and returns the result."""
    with _lock:
        st, cur, s, r = _load(), current(), settings(), repo()
        if not force and not _due(st):                 # another thread just did it
            return st
        _bg["running"] = True
        http, used = st.get("http") or {}, set()
        now = time.time()

        def get(url):
            used.add(url)
            return _get(url, http)
        st.update(current=cur, repo=s["repo"], at=now)
        try:
            if not r:
                st.update(state="no-repo", available=False, error="", latest=None, tip=None, next=now + EVERY)
                return st
            base = f"{API}/repos/{r[0]}/{r[1]}"
            info = get(base)
            if info is None:
                raise Failed(f"github.com/{r[0]}/{r[1]} was not found (deleted, renamed or private)", "error")
            track = info.get("default_branch") or "main"
            if cur["kind"] == "git" and "/" in cur["upstream"]:
                track = cur["upstream"].split("/", 1)[1]
            rel = get(base + "/releases/latest")          # None = no releases yet (drafts and pre-releases skipped)
            latest = None
            if rel and rel.get("tag_name"):
                latest = {"version": rel["tag_name"].lstrip("vV"), "tag": rel["tag_name"],
                          "name": rel.get("name") or rel["tag_name"], "notes": (rel.get("body") or "")[:6000],
                          "url": rel.get("html_url") or "", "published": rel.get("published_at") or "",
                          "zipball": rel.get("zipball_url") or "",
                          "assets": [{"name": a.get("name", ""), "url": a.get("browser_download_url", ""),
                                      "digest": a.get("digest") or "", "size": a.get("size", 0)} for a in rel.get("assets") or []]}
            head = get(f"{base}/commits/{urllib.parse.quote(track, safe='')}")
            if head is None and track != info.get("default_branch"):
                track = info.get("default_branch") or "main"
                head = get(f"{base}/commits/{urllib.parse.quote(track, safe='')}")
            tip = _commit(head) if head else None
            new, commits, unknown = 0, [], False
            mine = cur["commit"]
            if tip and mine and tip["sha"] != mine:
                cmp = get(f"{base}/compare/{mine}...{tip['sha']}")
                if cmp:
                    new = int(cmp.get("ahead_by") or 0)
                    commits = [_commit(c) for c in (cmp.get("commits") or [])][-20:][::-1]
                elif cur["kind"] == "git":             # this commit is not on GitHub (local commits): is the tip here?
                    new = 0 if _git("merge-base", "--is-ancestor", tip["sha"], "HEAD")[0] == 0 else -1
            elif tip and not mine:
                unknown = True                         # a zip install whose commit is not known
            newer_release = bool(latest and V.parse(latest["version"]) and V.parse(cur["version"]) and
                                 V.parse(latest["version"]) > V.parse(cur["version"]))
            if cur["kind"] == "git":
                available = new != 0
            elif latest:
                available = newer_release
            else:
                available = new != 0 or unknown
            http = {k: v for k, v in http.items() if k in used}     # keep only what this check asked for
            st.update(state="ok", error="", latest=latest, tip=tip, track=track, new_commits=new, commits=commits,
                      newer_release=newer_release, unknown=unknown, available=available, next=now + EVERY)
        except Failed as e:
            st.update(state=e.state, error=str(e), next=e.retry_at or now + RETRY)
        except Exception as e:                        # never let a check break the app
            st.update(state="error", error=f"{e.__class__.__name__}: {e}", next=now + RETRY)
        finally:
            st["http"] = http
            _save(st)
            _bg["running"] = False
        return st


def _due(st):
    s = settings()
    return bool(s["auto"] and repo() and (time.time() >= st.get("next", 0) or st.get("repo") != s["repo"]
                                         or (st.get("current") or {}).get("commit") != current()["commit"]
                                         or (st.get("current") or {}).get("version") != V.version()))


def check_later():
    """Background checks: now if one is due, then every half hour see whether one is due again."""
    if _bg["loop"]:
        return
    _bg["loop"] = True

    def loop():
        while True:
            try:
                if _due(_load()):
                    check()
            except Exception:
                pass
            time.sleep(1800)
    threading.Thread(target=loop, daemon=True, name="update-check").start()


# ---- what the page shows -----------------------------------------------------------------------------------------------

def _backup():
    """The newest undo point: a zip backup, or for a git checkout the commit before the last update."""
    files = sorted(WORK.glob("backup-*.zip"), key=lambda p: p.stat().st_mtime) if WORK.exists() else []
    if files:
        try:
            with zipfile.ZipFile(files[-1]) as z:
                meta = json.loads(z.read(".kch-backup.json"))
            return {"version": meta.get("from"), "at": meta.get("at"), "kind": "zip"}
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            return None
    undo = _load().get("git_undo")
    if undo and current()["commit"] == undo.get("to"):
        return {"version": undo.get("version"), "commit": undo["from"][:7], "at": undo.get("at"), "kind": "git"}
    return None


def _blocker(cur):
    """Why a one-click update cannot run here ("" = it can)."""
    if cur["kind"] != "git":
        return ""
    if not osutil.which("git"):
        return "git is not installed, so this git checkout cannot update itself."
    if not cur["branch"]:
        return "This checkout is not on a branch (detached HEAD): check out a branch, e.g. git checkout main."
    if not cur["upstream"]:
        return f"Branch {cur['branch']} does not follow a remote branch: git branch --set-upstream-to=origin/{cur['branch']}"
    dirty = _git("status", "--porcelain", "--untracked-files=no")[1]
    if dirty:
        return "You have local changes, so it will not update by itself. Commit or stash them first:\n" + \
               "\n".join(dirty.splitlines()[:8])
    return ""


def status(force=False):
    if force:
        check(True)
    elif _due(_load()) and not _bg["running"]:
        threading.Thread(target=check, daemon=True).start()
    st, cur, s = _load(), current(), settings()
    if not repo():
        st = {"state": "no-repo"}
    available = bool(st.get("available")) and (st.get("current") or {}).get("commit") == cur["commit"] \
        and (st.get("current") or {}).get("version") == cur["version"]
    latest, tip, new = st.get("latest"), st.get("tip"), st.get("new_commits", 0)
    if cur["kind"] == "git":
        what = (f"{new} new commit{'s' if new != 1 else ''} on {st.get('track')}" if new > 0 else
                f"new commits on {st.get('track')}") if available else ""
        if available and st.get("newer_release"):
            what = f"v{latest['version']} ({what})"
    elif latest:
        what = f"v{latest['version']}" if available else ""
    else:
        what = f"the newest commit on {st.get('track')} ({(tip or {}).get('sha', '')[:7]})" if available else ""
    state = st.get("state") or "never"
    if _bg["running"]:
        message = "Checking for updates…"
    elif state == "no-repo":
        message = "Update checks are off: this copy does not know its GitHub repository."
    elif state not in ("ok", "never"):
        message = "Couldn't check: " + (st.get("error") or "unknown error") + \
                  (f". The check before found an update: {what}" if available else "")
    elif available:
        message = "Update available: " + what
    elif state == "ok":
        message = "Up to date" + (f" (v{cur['version']})" if cur["version"] != "0.0.0" else "")
    else:
        message = "Not checked yet."
    blocker = _blocker(cur) if available else ""
    return {"current": cur, "repo": s["repo"] if repo() else "", "auto": s["auto"], "state": state, "checking": _bg["running"],
            "message": message, "available": available, "unknown": bool(st.get("unknown")) and available,
            "latest": latest, "tip": tip, "new_commits": new, "commits": st.get("commits") or [],
            "error": st.get("error", ""), "checked_at": st.get("at"), "next_at": st.get("next") if s["auto"] else None,
            "can_update": available and not blocker, "blocked": blocker, "backup": _backup(), "restart_needed": _restart_needed()}


_updated = {"at": 0}


def _restart_needed():
    return _updated["at"] > 0


# ---- updating ----------------------------------------------------------------------------------------------------------

def apply():
    with _lock:
        try:
            cur = current()
            res = _apply_git(cur) if cur["kind"] == "git" else _apply_zip(cur)
        except Failed as e:
            return {"ok": False, "out": str(e)}
        except Exception as e:
            return {"ok": False, "out": f"The update failed ({e.__class__.__name__}: {e}). Nothing was changed."}
        if res.get("restart"):
            _updated["at"] = time.time()
            st = _load()
            st.update(available=False, next=0)
            _save(st)
        return res


def _tools_changed(paths):
    if any(p.startswith(("deps/", "requirements-")) or p == "install.py" for p in paths):
        return "\nIts tools changed too: run `python install.py` before you restart."
    return ""


def _apply_git(cur):
    b = _blocker({**cur, "kind": "git"})
    if b:
        raise Failed(b + "\nNothing was changed.")
    c, _, err = _git("fetch", "--quiet", timeout=300)
    if c:
        raise Failed("git fetch failed, nothing was changed:\n" + err[-1500:])
    behind = int(_git("rev-list", "--count", "HEAD..@{u}")[1] or 0)
    ahead = int(_git("rev-list", "--count", "@{u}..HEAD")[1] or 0)
    if behind == 0:
        return {"ok": True, "out": f"Already up to date with {cur['upstream']}.", "restart": False}
    if ahead:
        raise Failed(f"This checkout has {ahead} commit{'s' if ahead != 1 else ''} that {cur['upstream']} does not, so it "
                     "cannot fast-forward. Nothing was changed: merge or rebase by hand (git pull --rebase).")
    old = _git("rev-parse", "HEAD")[1]
    c, out, err = _git("merge", "--ff-only", "@{u}", timeout=300)
    if c:
        raise Failed("git refused the fast-forward, nothing was changed:\n" + (err or out)[-1500:])
    new = _git("rev-parse", "HEAD")[1]
    log = _git("log", "--format=%h %s", "-n", "20", f"{old}..{new}")[1]
    changed = _git("diff", "--name-only", old, new)[1].splitlines()
    st = _load()
    st["git_undo"] = {"from": old, "to": new, "version": cur["version"], "at": time.time()}
    _save(st)
    return {"ok": True, "restart": True, "version": V.version(),
            "out": f"Updated {old[:7]} to {new[:7]} ({behind} commit{'s' if behind != 1 else ''}):\n{log}" + _tools_changed(changed)}


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _allowed(url):
    u = urllib.parse.urlparse(url)
    api = urllib.parse.urlparse(API)
    if u.hostname == api.hostname and u.port == api.port and u.scheme == api.scheme:
        return
    if u.scheme != "https" or u.hostname not in GITHUB_HOSTS:
        raise Failed(f"refusing to download from {u.scheme}://{u.hostname}: updates come only from GitHub")


def _download(url, dest):
    """Download to dest (size-limited, from GitHub's hosts only, every redirect checked); returns its sha256."""
    _allowed(url)
    opener = urllib.request.build_opener(_Redirects)
    req = urllib.request.Request(url, headers={"User-Agent": f"Flock/{V.version()}", "Accept": "*/*"})
    h, size = hashlib.sha256(), 0
    try:
        with opener.open(req, timeout=60) as r, open(dest, "wb") as f:
            _allowed(r.geturl())
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ZIP:
                    raise Failed("the download is larger than an update can be; stopped")
                h.update(chunk)
                f.write(chunk)
    except urllib.error.HTTPError as e:
        raise Failed(f"the download failed: GitHub answered {e.code} {e.reason}. Nothing was changed.")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise Failed(f"the download failed ({getattr(e, 'reason', e)}). Nothing was changed.", "offline")
    return h.hexdigest()


def _expected_sha(latest, asset):
    """The checksum GitHub publishes for a release asset (its digest), or the one in a SHA256SUMS asset."""
    if (asset.get("digest") or "").startswith("sha256:"):
        return asset["digest"].split(":", 1)[1].lower()
    for a in latest.get("assets") or []:
        if a["name"].upper() in ("SHA256SUMS", "SHA256SUMS.TXT") or a["name"] == asset["name"] + ".sha256":
            with tempfile.TemporaryDirectory() as t:
                _download(a["url"], Path(t) / "sums")
                for line in (Path(t) / "sums").read_text(encoding="utf-8", errors="replace").splitlines():
                    parts = line.split()
                    if parts and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]) and (len(parts) == 1 or parts[-1].lstrip("*") == asset["name"]):
                        return parts[0].lower()
    return ""


def _safe_rel(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
        raise Failed(f"the download holds an unsafe path ({name!r}); nothing was changed")
    return str(p)


def _unpack(zpath, root):
    """Unpack a release zip (one top folder, as GitHub makes them, or none) into root; returns its files (a/b paths)."""
    try:
        z = zipfile.ZipFile(zpath)
    except zipfile.BadZipFile:
        raise Failed("the download is not a zip file; nothing was changed")
    with z:
        if z.testzip() is not None:
            raise Failed("the downloaded zip is damaged; nothing was changed")
        entries = [i for i in z.infolist() if not i.is_dir()]
        if sum(i.file_size for i in entries) > 4 * MAX_ZIP:
            raise Failed("the download unpacks to more than an update can be; nothing was changed")
        tops = {i.filename.split("/", 1)[0] for i in entries}
        strip = len(tops) == 1 and all("/" in i.filename for i in entries)
        files = []
        for i in entries:
            rel = i.filename.split("/", 1)[1] if strip else i.filename
            if not rel:
                continue
            rel = _safe_rel(rel)
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise Failed(f"the download holds a symbolic link ({rel}); nothing was changed")
            out = root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            files.append(rel)
    missing = [n for n in NEEDED if n not in files]
    if missing:
        raise Failed("the download is not a Flock release (missing " + ", ".join(missing) + "); nothing was changed")
    for f in [f for f in files if f.endswith(".py")]:          # a file that does not compile never replaces a working one
        try:
            compile((root / f).read_text(encoding="utf-8"), f, "exec")
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            raise Failed(f"{f} in the download does not compile with this Python ({e}); nothing was changed")
    return files


def _protected():
    return [Path(p).resolve() for p in (config.DATA, config.PROFILE_PATH, config.TOOLS, config.KEYS, config.STATE,
                                        config.CONFIG_PATH, WORK)]


def _is_protected(rel, prot):
    parts = PurePosixPath(rel).parts
    if not parts or parts[0] == ".git" or "__pycache__" in parts or rel == RECORD.name:
        return True
    target = (APP / rel).resolve()
    return any(target == p or p in target.parents for p in prot)


def _put(src_bytes_or_path, dst):
    """Replace one file: write next to it, then swap in one step."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".kch-new")
    if isinstance(src_bytes_or_path, Path):
        shutil.copyfile(src_bytes_or_path, tmp)
    else:
        tmp.write_bytes(src_bytes_or_path)
    os.replace(tmp, dst)


def _apply_zip(cur):
    st = _load()
    latest, tip, r = st.get("latest"), st.get("tip"), repo()
    if not r:
        raise Failed("This copy does not know its GitHub repository, so it cannot update itself.")
    if not st.get("available") or (st.get("current") or {}).get("version") != cur["version"]:
        raise Failed("No update to install: check again first.")
    expect, want, commit = "", None, ""
    if latest:
        want, label = latest["version"], "v" + latest["version"]
        assets = latest.get("assets") or []
        asset = (
            next((a for a in assets if re.fullmatch(r"flock[-_].*\.zip", a["name"], re.I)), None)
            or next((a for a in assets if re.fullmatch(r"(?:nest|raven|kc-harness)[-_].*\.zip", a["name"], re.I)), None)
        )
        if asset:
            url, expect = asset["url"], _expected_sha(latest, asset)
            if not expect:
                raise Failed(f"The release's {asset['name']} has no published checksum, so it was not installed.")
        else:
            url = latest["zipball"] or f"{API}/repos/{r[0]}/{r[1]}/zipball/{latest['tag']}"
    elif tip:
        url, commit, label = f"{API}/repos/{r[0]}/{r[1]}/zipball/{tip['sha']}", tip["sha"], tip["sha"][:7]
    else:
        raise Failed("No update to install.")
    tmp = Path(tempfile.mkdtemp(prefix="kch-update-"))
    try:
        got = _download(url, tmp / "update.zip")
        if expect and got != expect:
            raise Failed(f"The download's checksum does not match the one GitHub published ({got[:12]}… is not "
                         f"{expect[:12]}…). Nothing was changed.")
        root = tmp / "new"
        files = _unpack(tmp / "update.zip", root)
        ver = (root / "VERSION").read_text(encoding="utf-8").strip()
        if not V.parse(ver) or (want and V.parse(ver) != V.parse(want)):
            raise Failed(f"The download says it is version {ver!r}, not {want}. Nothing was changed.")
        old_ver = cur["version"]
        changed = _install(root, files, old_ver, ver, commit)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, "restart": True, "version": ver,
            "out": f"Updated from v{old_ver} to {label}. Your data, keys, settings and profile were not touched; "
                   f"the replaced files are backed up (Roll back undoes it)." + _tools_changed(changed)}


def _install(root, files, old_ver, new_ver, commit):
    """Back up, then copy the new files in; on any error put the old ones back. Returns the paths that changed."""
    prot = _protected()
    files = [f for f in files if not _is_protected(f, prot)]
    keep = set(files)
    stale = [f for f in _record().get("files") or [] if f not in keep and not _is_protected(f, prot) and (APP / f).is_file()]
    changed = [f for f in files if not (APP / f).is_file() or (APP / f).read_bytes() != (root / f).read_bytes()] + stale
    WORK.mkdir(parents=True, exist_ok=True)
    bpath = WORK / f"backup-{old_ver}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    added = []
    with zipfile.ZipFile(bpath, "w", zipfile.ZIP_DEFLATED) as bz:
        for rel in changed:
            if (APP / rel).is_file():
                bz.write(APP / rel, rel)
            else:
                added.append(rel)
        if RECORD.exists():
            bz.write(RECORD, RECORD.name)
        bz.writestr(".kch-backup.json", json.dumps({"from": old_ver, "to": new_ver, "added": added, "at": time.time(),
                                                    "had_record": RECORD.exists()}))
    try:
        for rel in changed:
            if rel in keep:
                _put(root / rel, APP / rel)
        for rel in stale:
            (APP / rel).unlink(missing_ok=True)
        _put(json.dumps({"version": new_ver, "commit": commit, "installed_at": time.time(), "files": sorted(files)},
                        indent=1).encode("utf-8"), RECORD)
    except Exception as e:
        try:
            _restore(bpath)
            bpath.unlink(missing_ok=True)
        except Exception as e2:
            raise Failed(f"The update failed ({e}) and putting the old files back failed too ({e2}): restore them from {bpath}")
        raise Failed(f"The update failed ({e.__class__.__name__}: {e}); the old files were put back.")
    backups = sorted(WORK.glob("backup-*.zip"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return changed


def _restore(bpath):
    prot = _protected()
    with zipfile.ZipFile(bpath) as bz:
        meta = json.loads(bz.read(".kch-backup.json"))
        for i in bz.infolist():
            if i.filename == ".kch-backup.json" or i.is_dir():
                continue
            rel = _safe_rel(i.filename)
            if rel != RECORD.name and _is_protected(rel, prot):
                continue
            _put(bz.read(i), APP / rel)
        for rel in meta.get("added") or []:
            rel = _safe_rel(rel)
            if not _is_protected(rel, prot):
                (APP / rel).unlink(missing_ok=True)
        if not meta.get("had_record"):
            RECORD.unlink(missing_ok=True)
    return meta


def rollback():
    """Undo the last update: restore the newest zip backup, or for a git checkout go back to the commit before."""
    with _lock:
        b = _backup()
        if not b:
            return {"ok": False, "out": "There is no earlier version to go back to."}
        try:
            if b["kind"] == "zip":
                path = sorted(WORK.glob("backup-*.zip"), key=lambda p: p.stat().st_mtime)[-1]
                meta = _restore(path)
                path.unlink(missing_ok=True)
                out = f"Back to v{meta.get('from')}."
            else:
                st = _load()
                undo = st["git_undo"]
                c, o, e = _git("reset", "--keep", undo["from"])
                if c:
                    raise Failed("git would not go back (local changes in the way?), nothing was changed:\n" + (e or o)[-1500:])
                st.pop("git_undo", None)
                _save(st)
                out = f"Back to {undo['from'][:7]}."
        except Failed as e:
            return {"ok": False, "out": str(e)}
        _updated["at"] = time.time()
        st = _load()
        st["next"] = 0
        _save(st)
        return {"ok": True, "restart": True, "out": out + " Restart Flock to use it."}


# ---- restarting --------------------------------------------------------------------------------------------------------

def restart():
    """Start a new copy of the app that takes over this port once this process has exited, then stop serving."""
    global restarting
    args, skip = [], False
    for a in sys.argv[1:]:
        if skip:
            skip = False
        elif a == "--after":
            skip = True
        elif a != "--open":
            args.append(a)
    cmd = [sys.executable, "-X", "utf8", str(APP / "run.py"), "--after", str(os.getpid()), *args]
    log = open(config.LOGS / "restart.log", "a", encoding="utf-8")
    log.write(time.strftime("%F %T ") + "restart: " + " ".join(cmd) + "\n")
    log.flush()
    kw = dict(cwd=str(APP), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    try:
        subprocess.Popen(cmd, **kw, **osutil.detached(breakaway=True))
    except OSError:                                             # a job that forbids breaking away
        subprocess.Popen(cmd, **kw, **osutil.detached())
    restarting = True
    if on_restart:
        threading.Timer(0.5, on_restart).start()
    return {"ok": True, "out": "Restarting…"}
