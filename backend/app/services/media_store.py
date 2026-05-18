from __future__ import annotations

import re
import secrets
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

MEDIA_ROOT = Path("var/media")
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 100 * 1024 * 1024

ALLOWED_MEDIA_MIME: dict[str, set[str]] = {
    "image": {"image/jpeg", "image/png", "image/webp", "image/gif"},
    "video": {"video/mp4", "video/quicktime", "video/3gpp", "video/webm"},
}

EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/3gpp": ".3gp",
    "video/webm": ".webm",
}


async def persist_upload(upload: UploadFile, *, team_id: int, kind: str) -> dict:
    mime = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if kind not in ALLOWED_MEDIA_MIME or mime not in ALLOWED_MEDIA_MIME[kind]:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported media type")
    raw = await upload.read()
    max_bytes = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty media file")
    if len(raw) > max_bytes:
        limit = "15MB" if kind == "image" else "100MB"
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"media file exceeds {limit}")

    original = _safe_original_name(upload.filename or f"upload{EXT_BY_MIME.get(mime, '')}")
    ext = EXT_BY_MIME.get(mime) or Path(original).suffix.lower()
    filename = f"{uuid.uuid4().hex}{ext}"
    root = MEDIA_ROOT / str(team_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    path.write_bytes(raw)
    token = secrets.token_urlsafe(32)
    return {
        "kind": kind,
        "mime": mime,
        "filename": original,
        "storage_path": str(path),
        "bytes": len(raw),
        "download_token": token,
        "url": f"/media/{token}",
    }


async def persist_upload_bytes(
    raw: bytes,
    *,
    team_id: int,
    kind: str,
    mime: str,
    filename: str,
) -> dict:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if kind not in ALLOWED_MEDIA_MIME or mime not in ALLOWED_MEDIA_MIME[kind]:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "unsupported media type")
    max_bytes = MAX_IMAGE_BYTES if kind == "image" else MAX_VIDEO_BYTES
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty media file")
    if len(raw) > max_bytes:
        limit = "15MB" if kind == "image" else "100MB"
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"media file exceeds {limit}")

    original = _safe_original_name(filename or f"upload{EXT_BY_MIME.get(mime, '')}")
    ext = EXT_BY_MIME.get(mime) or Path(original).suffix.lower()
    stored = f"{uuid.uuid4().hex}{ext}"
    root = MEDIA_ROOT / str(team_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / stored
    path.write_bytes(raw)
    token = secrets.token_urlsafe(32)
    return {
        "kind": kind,
        "mime": mime,
        "filename": original,
        "storage_path": str(path),
        "bytes": len(raw),
        "download_token": token,
        "url": f"/media/{token}",
    }


def resolve_media_path(meta: dict) -> Path | None:
    raw = str(meta.get("storage_path") or "")
    if not raw:
        return None
    path = Path(raw)
    try:
        path.resolve().relative_to(MEDIA_ROOT.resolve())
    except ValueError:
        return None
    return path


def _safe_original_name(name: str) -> str:
    # Browser uploads should already strip local paths, but Windows clients can
    # still expose backslashes in older UAs. Normalize both forms defensively.
    base = Path(name.replace("\\", "/")).name.strip() or "upload"
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", base)[:160] or "upload"
