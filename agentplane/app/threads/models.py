"""The app's own Thread table: the Thread → Session link and what an operator sets on a Thread.

The app reads a Thread's fold from the History Service.

The schema is owned by the Alembic migrations under `migrations/`, applied by `database_migrate.py`
as a separate deploy step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from agentplane.app.database import Base


class Thread(Base):
    """A Thread the app opened, keyed by its Session's public ID, and what an operator set on it."""

    __tablename__ = "thread"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    # NULL while unnamed; never the empty string.
    name: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
