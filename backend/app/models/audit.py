from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    """Who changed what and when. Never contains secrets or customer texts."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
