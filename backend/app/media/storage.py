"""Media files on disk: safe paths, writes, retention sweeps.

Two rules govern everything here (SPEC §13):
1. File names are OURS (uuid + an extension from a mime allowlist). A
   name from the provider payload is untrusted input and never becomes a
   path component — that is how traversal ("../../etc/passwd") happens.
2. Every read/delete resolves the path and verifies it stays inside the
   media root, so even a corrupted media_ref in the database cannot
   reach a file outside the volume.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


class MediaStorageError(Exception):
    """Unsafe path or a failed write; never carries the raw provider path."""


# Extensions are decided by us from the mime type, never taken from the
# provider's file name. Anything unknown gets .bin — it is only ever
# handed to a human, never executed or interpreted.
_MIME_EXTENSIONS: dict[str, str] = {
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "application/pdf": ".pdf",
}
_FALLBACK_EXTENSION = ".bin"


def extension_for(mime_type: str | None) -> str:
    if not mime_type:
        return _FALLBACK_EXTENSION
    return _MIME_EXTENSIONS.get(mime_type.split(";", 1)[0].strip().lower(), _FALLBACK_EXTENSION)


def _root(base_dir: str) -> Path:
    return Path(base_dir).resolve()


def resolve_ref(base_dir: str, media_ref: str) -> Path:
    """Absolute path for a stored relative ref, or raise.

    Rejects absolute refs, traversal and symlinks pointing out of the
    volume: the check is done on the RESOLVED path, so a symlink planted
    inside the volume cannot smuggle a read of /etc/passwd either.
    """
    root = _root(base_dir)
    candidate = Path(media_ref)
    if candidate.is_absolute():
        msg = "media_ref_absolute"
        raise MediaStorageError(msg)
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        msg = "media_ref_outside_root"
        raise MediaStorageError(msg)
    return resolved


def save_bytes(base_dir: str, chat_id: int, data: bytes, mime_type: str | None) -> str:
    """Write the file under {root}/{chat_id}/ and return its relative ref."""
    root = _root(base_dir)
    directory = root / str(int(chat_id))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{extension_for(mime_type)}"
        path = directory / name
        # Written 0600: only the service user reads customer media.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    except OSError as exc:
        # Disk full / permissions: the caller hands the message to a human.
        logger.error("media_write_failed", error=type(exc).__name__)
        msg = f"media_write_failed:{type(exc).__name__}"
        raise MediaStorageError(msg) from exc
    return f"{int(chat_id)}/{name}"


def read_bytes(base_dir: str, media_ref: str) -> bytes:
    path = resolve_ref(base_dir, media_ref)
    try:
        return path.read_bytes()
    except OSError as exc:
        msg = f"media_read_failed:{type(exc).__name__}"
        raise MediaStorageError(msg) from exc


def delete_ref(base_dir: str, media_ref: str) -> bool:
    """Delete one stored file; True if it was there. Never raises for a
    missing file — retention must be idempotent."""
    try:
        path = resolve_ref(base_dir, media_ref)
    except MediaStorageError:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.error("media_delete_failed", error=type(exc).__name__)
        return False
    return True


def sweep_orphans(base_dir: str, retention_days: int) -> int:
    """Delete files older than the retention window that no longer belong
    to any message (the DB-driven cleanup nulls media_ref first). Returns
    the number of files removed.

    A file whose row was deleted outright (chat removal cascade) would
    otherwise sit on the volume forever — on a 124 GB VPS shared with
    other projects that matters.
    """
    root = _root(base_dir)
    if not root.exists():
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=max(retention_days, 0))).timestamp()
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


def disk_free_ratio(base_dir: str) -> float:
    """Free share of the media filesystem (0..1); 1.0 if unknown.

    Used as a circuit breaker: on a small shared VPS, downloading media
    until the disk is full would take the neighbouring projects down too.
    """
    try:
        usage = os.statvfs(base_dir)
    except OSError:
        return 1.0
    total = usage.f_blocks * usage.f_frsize
    if total <= 0:
        return 1.0
    return (usage.f_bavail * usage.f_frsize) / total
