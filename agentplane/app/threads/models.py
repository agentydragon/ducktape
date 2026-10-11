"""The app's own Thread tables: the Thread → Session link and what an operator sets on a Thread.

The app reads a Thread's fold from the History Service.

The schema is owned by the Alembic migrations under `migrations/`, applied by `database_migrate.py`
as a separate deploy step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from agentplane.app.database import Base


class EventLog(Base):
    """App-side Thread identity: the link from a Thread to its service-owned Session."""

    # TODO(session-schema-cleanup): Rename EventLog/event_log to describe the app's
    # Session reference, not archive ownership; migrate referencing FKs together.
    # See plans/task_dag.md#app_session_schema_rename--name-the-app-session-reference-accurately.

    __tablename__ = "event_log"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Thread(Base):
    """What an operator has set on an event log's thread, and the app's record of a confirmed resume.
    No row means the defaults: unnamed, not archived and not resumed, so discovery never has to create one."""

    __tablename__ = "thread"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    # NULL while unnamed; never the empty string.
    name: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # A runner-confirmed restart: a terminal feed at or before this cursor belongs to the previous incarnation.
    resumed_after_cursor: Mapped[int | None] = mapped_column(BigInteger)
