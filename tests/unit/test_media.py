"""Media primitives: safe storage, image preparation, audio probing and
the sensitive-content filter.

Real bytes throughout — a real JPEG made with Pillow, a real WAV made
with the stdlib — because the whole point of these modules is to survive
real files, and a mocked decoder would prove nothing.
"""

from __future__ import annotations

import io
import os
import struct
import wave
from pathlib import Path

import pytest
from PIL import Image

from app.media import images, sensitive, storage
from app.media.audio import AudioError, probe_duration_seconds
from app.media.images import ImageError, prepare_image
from app.media.storage import MediaStorageError

# ------------------------------ fixtures -------------------------------


def make_jpeg(width: int, height: int, *, with_exif: bool = False) -> bytes:
    image = Image.new("RGB", (width, height), color=(120, 90, 200))
    buffer = io.BytesIO()
    if with_exif:
        exif = image.getexif()
        exif[274] = 6  # orientation: rotate 90
        exif[271] = "TestCamera"  # make — must not survive
        image.save(buffer, format="JPEG", exif=exif)
    else:
        image.save(buffer, format="JPEG")
    return buffer.getvalue()


def make_wav(seconds: float, *, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return buffer.getvalue()


# ------------------------------ storage --------------------------------


def test_save_read_delete_roundtrip(tmp_path: Path) -> None:
    ref = storage.save_bytes(str(tmp_path), 42, b"voice-bytes", "audio/ogg")

    assert ref.startswith("42/")
    assert ref.endswith(".ogg")  # extension from the mime allowlist
    assert storage.read_bytes(str(tmp_path), ref) == b"voice-bytes"
    # Customer media is not world-readable.
    mode = os.stat(storage.resolve_ref(str(tmp_path), ref)).st_mode
    assert mode & 0o077 == 0

    assert storage.delete_ref(str(tmp_path), ref) is True
    assert storage.delete_ref(str(tmp_path), ref) is False  # idempotent


def test_provider_file_name_never_becomes_the_path(tmp_path: Path) -> None:
    """The name is ours: a provider payload claiming to be
    "../../etc/passwd.jpg" cannot influence where bytes land."""
    ref = storage.save_bytes(str(tmp_path), 7, b"x", "image/jpeg")
    assert ".." not in ref
    assert storage.resolve_ref(str(tmp_path), ref).is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize(
    "bad_ref",
    [
        "../outside.jpg",
        "7/../../outside.jpg",
        "/etc/passwd",
        "/tmp/evil.jpg",
    ],
)
def test_traversal_refs_are_rejected(tmp_path: Path, bad_ref: str) -> None:
    with pytest.raises(MediaStorageError):
        storage.resolve_ref(str(tmp_path), bad_ref)


def test_symlink_out_of_volume_is_rejected(tmp_path: Path) -> None:
    """The check runs on the RESOLVED path, so a symlink planted inside
    the volume cannot smuggle a read from outside it."""
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_bytes(b"secret")
    volume = tmp_path / "media"
    volume.mkdir()
    (volume / "link.txt").symlink_to(outside)

    with pytest.raises(MediaStorageError):
        storage.resolve_ref(str(volume), "link.txt")


def test_unknown_mime_falls_back_to_bin(tmp_path: Path) -> None:
    ref = storage.save_bytes(str(tmp_path), 1, b"x", "application/x-something-weird")
    assert ref.endswith(".bin")


def test_sweep_orphans_deletes_only_old_files(tmp_path: Path) -> None:
    old_ref = storage.save_bytes(str(tmp_path), 1, b"old", "image/jpeg")
    fresh_ref = storage.save_bytes(str(tmp_path), 1, b"fresh", "image/jpeg")
    old_path = storage.resolve_ref(str(tmp_path), old_ref)
    ancient = old_path.stat().st_mtime - 30 * 24 * 3600
    os.utime(old_path, (ancient, ancient))

    assert storage.sweep_orphans(str(tmp_path), retention_days=7) == 1
    assert not old_path.exists()
    assert storage.resolve_ref(str(tmp_path), fresh_ref).exists()


def test_disk_free_ratio_is_a_fraction(tmp_path: Path) -> None:
    ratio = storage.disk_free_ratio(str(tmp_path))
    assert 0.0 <= ratio <= 1.0


# ------------------------------ images ---------------------------------


def test_large_image_is_shrunk_for_money(tmp_path: Path) -> None:
    """Image tokens scale with pixels: a 4000 px phone photo must come
    out at the 1280 px budget, or every photo costs several times more."""
    prepared = prepare_image(make_jpeg(4000, 3000))

    assert max(prepared.width, prepared.height) == images.TARGET_LONGEST_SIDE
    assert prepared.original_width == 4000
    assert prepared.mime_type == "image/jpeg"
    assert len(prepared.data) < len(make_jpeg(4000, 3000))


def test_small_image_is_not_upscaled() -> None:
    prepared = prepare_image(make_jpeg(300, 200))
    assert (prepared.width, prepared.height) == (300, 200)


def test_exif_is_applied_then_stripped() -> None:
    """Orientation must be honoured (otherwise the model reads a sideways
    photo) but GPS/device metadata must not leave our server."""
    prepared = prepare_image(make_jpeg(400, 200, with_exif=True))

    with Image.open(io.BytesIO(prepared.data)) as result:
        assert dict(result.getexif()) == {}
        # Orientation 6 = rotate 90: the 400x200 source becomes portrait.
        assert result.size == (200, 400)


def test_garbage_bytes_are_rejected() -> None:
    with pytest.raises(ImageError):
        prepare_image(b"this is not an image")


def test_decompression_bomb_is_refused(monkeypatch) -> None:
    """A crafted file that unpacks to hundreds of megapixels must not be
    decoded on a 3.8 GiB VPS."""
    monkeypatch.setattr(images, "MAX_DECODED_PIXELS", 1000)
    with pytest.raises(ImageError):
        prepare_image(make_jpeg(2000, 2000))


def test_pixel_limit_is_restored_after_failure(monkeypatch) -> None:
    """The global Pillow limit is process-wide: leaking a lowered value
    would silently break every later image."""
    before = Image.MAX_IMAGE_PIXELS
    with pytest.raises(ImageError):
        prepare_image(b"garbage")
    assert before == Image.MAX_IMAGE_PIXELS


def test_card_like_crop_is_flagged() -> None:
    card = prepare_image(make_jpeg(1600, 1000))  # ratio 1.6
    photo = prepare_image(make_jpeg(4000, 3000))  # ratio 1.33
    assert card.looks_like_card_scan is True
    assert photo.looks_like_card_scan is False


# ------------------------------ audio ----------------------------------


def test_duration_is_read_locally_before_paying() -> None:
    assert probe_duration_seconds(make_wav(2.0)) == pytest.approx(2.0, abs=0.05)
    assert probe_duration_seconds(make_wav(150.0)) == pytest.approx(150.0, abs=0.5)


def test_unreadable_audio_raises_instead_of_guessing() -> None:
    """An unknown duration must never be treated as "probably short" —
    that is how a 40-minute file reaches a per-minute paid call."""
    with pytest.raises(AudioError):
        probe_duration_seconds(b"not audio at all")


# --------------------------- sensitive filter ---------------------------


@pytest.mark.parametrize(
    "caption",
    [
        "вот мой паспорт",
        "Удостоверение личности прикрепил",
        "моя карта для оплаты",
        "справка от врача",
        "рентген снимок колена",
        "договор подписанный",
        "жеке куәлік",
        "анықтама дәрігерден",
        "here is my passport",
        "my credit card photo",
        "bank statement attached",
        "medical prescription",
    ],
)
def test_sensitive_captions_block_vision(caption: str) -> None:
    assert sensitive.caption_is_sensitive(caption) is not None


@pytest.mark.parametrize(
    "caption",
    [
        "вот фото зала",
        "картина на стене красивая",  # "картина" must not trip "карта"
        "это тренажерный зал",
        "photo of the gym",
        "мына жаттығу залы",
        None,
        "",
    ],
)
def test_benign_captions_pass(caption: str | None) -> None:
    assert sensitive.caption_is_sensitive(caption) is None


def test_card_number_in_caption_is_blocked() -> None:
    assert sensitive.caption_is_sensitive("4400 4301 2345 6789") == "caption_card_number"
    assert sensitive.caption_is_sensitive("мой иин 950101300123") == "caption_keyword"
    assert sensitive.caption_is_sensitive("номер 123456789012") == "caption_long_digits"


def test_verdict_combines_caption_and_shape() -> None:
    assert sensitive.image_is_sensitive("фото зала", looks_like_card_scan=False) is None
    assert sensitive.image_is_sensitive("фото зала", looks_like_card_scan=True) == "card_like_crop"
    assert (
        sensitive.image_is_sensitive("мой паспорт", looks_like_card_scan=False) == "caption_keyword"
    )
