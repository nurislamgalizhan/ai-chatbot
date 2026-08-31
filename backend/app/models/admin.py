from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Identity, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AdminUser(Base):
    """The single administrator account. More than one active row is a bug
    and the application refuses to start (checked at startup)."""

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    phone: Mapped[str] = mapped_column(Text, unique=True)  # E.164
    full_name: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)  # argon2id
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    failed_login_count: Mapped[int] = mapped_column(server_default=text("0"))
    locked_until: Mapped[datetime | None]
    password_changed_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class TotpRecoveryCode(Base):
    """One-time recovery codes; only argon2 hashes are stored."""

    __tablename__ = "admin_totp_recovery_codes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="CASCADE")
    )
    code_hash: Mapped[str] = mapped_column(Text)
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AdminSession(Base):
    """Server-side sessions. The cookie carries a random token; only its
    SHA-256 hash is stored here."""

    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="CASCADE")
    )
    session_token_hash: Mapped[str] = mapped_column(Text, unique=True)
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]


class LoginChallenge(Base):
    """Intermediate 'password accepted, waiting for TOTP' state.

    Deliberately NOT a session: possessing a challenge token grants no API
    access whatsoever (covered by tests).
    """

    __tablename__ = "login_challenges"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("admin_user.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    ip: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
