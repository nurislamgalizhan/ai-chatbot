"""Media work queue (phase 7).

One row per media message that needs a paid pre-processing step (voice ->
transcript, image -> description). It exists instead of reusing
messages.retry_count / next_attempt_at because those belong to the AI
pipeline: a voice note that spent two attempts on transcription would
otherwise reach the chat call already out of retries, and the budget
reservation's sub-correlation is keyed by that same counter.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, Numeric, Text, func, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import MediaJobState, MediaType


def _enum(enum_cls: type, name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda e: [member.value for member in e],
    )


class MediaJob(Base):
    __tablename__ = "media_jobs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # UNIQUE: a redelivered notification or a double enqueue can never
    # create a second paid unit of work for the same message.
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE")
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"))
    media_type: Mapped[MediaType] = mapped_column(_enum(MediaType, "media_type"))
    state: Mapped[MediaJobState] = mapped_column(
        _enum(MediaJobState, "media_job_state"), server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # Whoever is working this job holds a lease; a crashed worker's lease
    # expires and the job becomes claimable again (no orphaned work).
    lease_until: Mapped[datetime | None]
    next_attempt_at: Mapped[datetime | None]
    # The provider's signed download URL: kept here, never in
    # messages.media_ref, which holds our own path only (SPEC §13).
    download_url: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    file_bytes: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    sha256: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
