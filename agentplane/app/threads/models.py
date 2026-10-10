"""The app's own Thread tables: the Thread → Session link and what an operator sets on a Thread.

The fold's projection tables are `agentplane.thread_fold.models`.

The schema is owned by the Alembic migrations under `migrations/`, applied by `database_migrate.py`
as a separate deploy step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from agentplane.app.database import Base
from agentplane.runner.harness import Harness


class EventLog(Base):
    """App-side Thread identity and projected metadata for a service-owned Session."""

    # TODO(session-schema-cleanup): Rename EventLog/event_log to describe the app's
    # Session reference, not archive ownership; migrate referencing FKs together.
    # See plans/task_dag.md#app_session_schema_rename--name-the-app-session-reference-accurately.

    __tablename__ = "event_log"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    sandbox: Mapped[str] = mapped_column(Text)
    harness: Mapped[Harness] = mapped_column(
        SqlEnum(
            Harness,
            native_enum=False,
            create_constraint=False,
            values_callable=lambda values: [item.value for item in values],
        )
    )
    # TODO(session-schema-cleanup): Audit sandbox/harness/model/cwd as app display
    # projections versus redundant launch metadata; retain only actual app needs,
    # refreshed from service evidence, never as authoritative launch/routing inputs.
    # The launch spec's model; once a runner attaches, its spec in `ThreadHistorySummary.attached`
    # is the current one.
    model: Mapped[str] = mapped_column(Text)
    cwd: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    # CLEANUP(added 2026-10-10): neither written nor read since `ThreadHistorySummary` carries it;
    #   drop with the harness/model/cwd copies, once no replica runs an image that writes it.
    last_model_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Thread(Base):
    """What an operator has set on an event log's thread. No row means the defaults: unnamed and
    not archived, so ingestion never has to create one."""

    __tablename__ = "thread"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("event_log.id", ondelete="CASCADE"), primary_key=True
    )
    # NULL while unnamed; never the empty string.
    name: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
