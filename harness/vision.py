"""Native image utilities for every model.

Every connected model receives images and screenshots directly through its own native vision pathway (Anthropic base64
blocks, OpenAI-compatible image_url parts, Claude Code base64 blocks, Codex --image flags, and Antigravity/Gemini ACP
image blocks or native read_file on attached paths). No model ever describes images on behalf of another.
"""
import base64
import mimetypes
import re
import time
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_BYTES = 15 * 1024 * 1024         # bigger files go as a path only
MAX_FILE_BYTES = 25 * 1024 * 1024    # general file uploads (picker, drag and drop)


def is_image(p):
    return Path(p).suffix.lower() in IMAGE_EXT


def mime(p):
    ext = Path(p).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
            ".bmp": "image/bmp"}.get(ext) or mimetypes.guess_type(str(p))[0] or "image/png"


def data_url(p):
    return f"data:{mime(p)};base64," + base64.b64encode(Path(p).read_bytes()).decode()


def sees(ep):
    """Every connected model reads images natively."""
    return bool(ep)


def view(ctx, a):
    """The View tool: returns the image itself for the model to inspect natively."""
    from .tools import ToolError, resolve, DENY_RE
    p = resolve(ctx, a.get("file_path") or a.get("path"))
    if DENY_RE.search(str(p)):
        raise ToolError("that is a credential store")
    if not p.is_file():
        raise ToolError(f"{p} does not exist")
    if not is_image(p):
        raise ToolError(f"{p} is not an image ({', '.join(sorted(IMAGE_EXT))})")
    size = p.stat().st_size
    if size > MAX_BYTES:
        raise ToolError(f"{p.name} is {size:,} bytes, over the {MAX_BYTES:,} byte limit for one picture; make a smaller copy and View that")
    return {"text": f"{p} ({size:,} bytes): the image follows.", "image": p, "ui": {"image": str(p)}}


def save_attachment(chat, data, mime_type="", name=""):
    """A pasted image, saved into the chat's attachments folder (it stays with the chat, so it shows again on reload)."""
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp"}.get(mime_type)
    stem = re.sub(r"[^\w.-]+", "-", Path(name).stem)[:60].strip("-.") if name else ""
    if not ext and Path(name).suffix.lower() in IMAGE_EXT:
        ext = Path(name).suffix.lower()
    if not ext:
        raise ValueError("only images can be pasted (png, jpg, webp, gif, bmp)")
    raw = base64.b64decode(data or "")
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("the image is empty or over 15 MB")
    folder = chat.dir / "attachments"
    folder.mkdir(parents=True, exist_ok=True)
    base = stem or "pasted-" + time.strftime("%Y%m%d-%H%M%S")
    p, n = folder / (base + ext), 2
    while p.exists():
        p, n = folder / f"{base}-{n}{ext}", n + 1
    p.write_bytes(raw)
    return p


def save_any_attachment(folder, raw, mime_type="", name=""):
    """Save any picked photo or file into `folder` (usually chat.dir / 'attachments'). Images capped at 15 MB,
    other files at 25 MB. Returns the saved Path."""
    if not raw:
        raise ValueError("the file is empty")
    ext = Path(name).suffix.lower() if name else ""
    if not ext and mime_type:
        ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
               "image/bmp": ".bmp", "image/heic": ".heic", "application/pdf": ".pdf",
               "text/plain": ".txt", "text/markdown": ".md", "application/json": ".json",
               "text/csv": ".csv"}.get(mime_type.split(";")[0].strip().lower(), "")
        if not ext:
            ext = mimetypes.guess_extension(mime_type.split(";")[0].strip().lower()) or ""
    if ext and not re.fullmatch(r"\.[a-z0-9]{1,12}", ext):
        ext = ""
    is_img = ext in IMAGE_EXT or (mime_type or "").lower().startswith("image/")
    limit = MAX_BYTES if is_img and ext in IMAGE_EXT else MAX_FILE_BYTES
    if len(raw) > limit:
        raise ValueError(f"file is over {limit // (1024 * 1024)} MB")
    stem = re.sub(r"[^\w.-]+", "-", Path(name).stem)[:60].strip("-.") if name else ""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    base = stem or (("photo-" if is_img else "file-") + time.strftime("%Y%m%d-%H%M%S"))
    p, n = folder / (base + ext), 2
    while p.exists():
        p, n = folder / f"{base}-{n}{ext}", n + 1
    p.write_bytes(raw)
    return p
