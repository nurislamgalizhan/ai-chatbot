from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppSetting(Base):
    """Runtime-editable settings (limits, modes, prompts live here from
    phase 6 on; phase 4 uses it for the emergency switch and pause policy).
    `.env` provides initial values and secrets only — never store secrets
    in this table."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    value_type: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
