"""Local audio inspection: duration without paying anyone.

Transcription is billed per minute, so the duration check has to happen
BEFORE the call (SPEC §13). GREEN-API payloads do not carry a reliable
duration, so it is read from the file container itself with mutagen —
pure Python, no ffmpeg on the VPS.
"""

from __future__ import annotations

import io

import structlog

logger = structlog.get_logger(__name__)


class AudioError(Exception):
    """Unreadable container: the caller hands the message to a human
    rather than sending an unknown number of paid minutes to OpenAI."""


def probe_duration_seconds(data: bytes) -> float:
    """Duration of a voice message in seconds.

    Blocking but trivial (header parsing only); safe to call directly.
    """
    from mutagen import File as MutagenFile  # noqa: PLC0415 - optional-ish heavy import

    try:
        parsed = MutagenFile(io.BytesIO(data))
    except Exception as exc:  # mutagen raises a family of format errors
        logger.warning("audio_probe_failed", error=type(exc).__name__)
        msg = f"audio_probe_failed:{type(exc).__name__}"
        raise AudioError(msg) from exc
    info = getattr(parsed, "info", None)
    duration = getattr(info, "length", None)
    if not isinstance(duration, int | float) or duration <= 0:
        # An unknown duration is NOT treated as "probably short": that is
        # exactly how a 40-minute file slips into a paid call.
        msg = "audio_probe_failed:no_duration"
        raise AudioError(msg)
    return float(duration)
