"""Image preparation before vision: decode limits, resize, EXIF strip.

Money and privacy both live here:
- resizing to <=1280 px is what keeps a 12 Mpx phone photo from costing
  several times more than it needs to (image tokens scale with pixels);
- EXIF is applied (orientation) and then DROPPED, so GPS coordinates and
  device serials from the customer's camera never reach OpenAI or our
  own logs.

Pillow work is CPU-bound: callers run `prepare_image` in a thread
(asyncio.to_thread), never on the event loop (SPEC §16).
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import structlog
from PIL import Image, ImageOps, UnidentifiedImageError

logger = structlog.get_logger(__name__)

# A decompression bomb (a 100 MB PNG that unpacks to 40000x40000) would
# eat the whole VPS. Pillow raises above this pixel count.
MAX_DECODED_PIXELS = 40_000_000
# SPEC §13: 1024-1280 px on the longest side.
TARGET_LONGEST_SIDE = 1280
_JPEG_QUALITY = 82
# Cheap "is this a scan of a card/ID" hint: a tightly cropped card is
# much wider than tall. A phone photo of a card is NOT caught by this —
# documented weakness, which is exactly why real documents are never
# analyzed at all and captions are checked too.
CARD_ASPECT_MIN = 1.5
CARD_ASPECT_MAX = 1.7


class ImageError(Exception):
    """Undecodable, too large, or a decompression bomb."""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    original_width: int
    original_height: int

    @property
    def aspect_ratio(self) -> float:
        shorter = min(self.original_width, self.original_height)
        if shorter <= 0:
            return 0.0
        return max(self.original_width, self.original_height) / shorter

    @property
    def looks_like_card_scan(self) -> bool:
        return CARD_ASPECT_MIN <= self.aspect_ratio <= CARD_ASPECT_MAX


def prepare_image(data: bytes) -> PreparedImage:
    """Decode, orient, shrink and re-encode as JPEG without metadata.

    Blocking: run in a thread. Raises ImageError for anything that is not
    a single-frame decodable image within the pixel budget — the caller
    then hands the message to a human instead of guessing.

    The size is read from the HEADER and rejected before any decode: a
    10 MB JPEG can be 100+ megapixels, which is ~300 MB decoded and an
    OOM-kill of the worker (and with it polling and the outbox).
    """
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_DECODED_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as image:
            original_width, original_height = image.size
            if original_width * original_height > MAX_DECODED_PIXELS:
                msg = "image_failed:too_many_pixels"
                raise ImageError(msg)
            if getattr(image, "n_frames", 1) > 1:
                # Animations are video-shaped: a human deals with them.
                msg = "image_failed:multi_frame"
                raise ImageError(msg)
            # JPEG only: decode straight into a reduced scale instead of
            # materializing the full-size bitmap first.
            image.draft("RGB", (TARGET_LONGEST_SIDE, TARGET_LONGEST_SIDE))
            # Applies the EXIF orientation tag, then returns a copy that
            # no longer carries EXIF at all.
            oriented = ImageOps.exif_transpose(image) or image
            oriented = oriented.convert("RGB")
            oriented.thumbnail((TARGET_LONGEST_SIDE, TARGET_LONGEST_SIDE), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            # No exif= argument: metadata is deliberately not carried over.
            oriented.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            width, height = oriented.size
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        logger.warning("image_prepare_failed", error=type(exc).__name__)
        msg = f"image_failed:{type(exc).__name__}"
        raise ImageError(msg) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit

    return PreparedImage(
        data=buffer.getvalue(),
        mime_type="image/jpeg",
        width=width,
        height=height,
        original_width=original_width,
        original_height=original_height,
    )
