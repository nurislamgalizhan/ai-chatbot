from __future__ import annotations

from datetime import datetime

from sqlalchemy import TIMESTAMP, MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names so Alembic migrations stay reviewable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    # Fetch server defaults (created_at, updated_at, ...) via RETURNING on
    # INSERT/UPDATE. Without this they stay unloaded after flush and any
    # later attribute read raises MissingGreenlet in async code.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # All timestamps in the schema are timestamptz (UTC); rendering in the
    # organization's timezone is the UI's job.
    type_annotation_map = {datetime: TIMESTAMP(timezone=True)}  # noqa: RUF012
