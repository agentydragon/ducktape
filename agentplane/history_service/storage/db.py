"""Durable Session history, independent of a Sandbox CR and runner PVC.

The migration is owned by Sandbox Service. No app tables or app-issued identities are used.
"""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionHistory(Base):
    __tablename__ = "session_history"
    __table_args__ = (
        CheckConstraint("last_cursor >= 0", name="history_last_cursor_nonnegative"),
        CheckConstraint(
            "num_nulls(writer_namespace, writer_name, writer_uid) IN (0, 3)", name="history_writer_complete"
        ),
        Index("ux_history_opened_position", "opened_position", unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # The ServiceAccount whose first accepted ingress push bound the Session; absent until then.
    # Pushes from any other account are refused.
    writer_namespace: Mapped[str | None] = mapped_column(String)
    writer_name: Mapped[str | None] = mapped_column(String)
    writer_uid: Mapped[str | None] = mapped_column(String)
    source_id: Mapped[str | None] = mapped_column(String)
    last_cursor: Mapped[int] = mapped_column(BigInteger)
    # Complete SessionFeedState protobuf, absent until a runner prefix is observed.
    feed_state: Mapped[bytes | None] = mapped_column(LargeBinary)
    # The order the Session was recorded in. `Store.bind_writer` draws it under a lock held to commit,
    # so positions become visible in ascending order.
    opened_position: Mapped[int] = mapped_column(BigInteger, Identity())


class SessionEvent(Base):
    __tablename__ = "session_event"

    session_id: Mapped[UUID] = mapped_column(ForeignKey("session_history.id"), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Preserve the complete wire entry (including unknown fields and native frames). JSONB
    # cannot represent arbitrary native bytes, and a parsed fold is not a replayable history.
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class SessionEventSettlement(Base):
    """One item field whose streamed delta entries were removed from `session_event`.

    Authored by the history writer, never by the runner: `session_event` keeps only runner entries,
    complete except for the cursors these settlements name.
    """

    __tablename__ = "session_event_settlement"

    session_id: Mapped[UUID] = mapped_column(ForeignKey("session_history.id"), primary_key=True)
    # The retained runner entry holding the content; one settlement per completing entry.
    completion_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Complete SettledDeltas protobuf.
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class SessionEventSettledRange(Base):
    """A run of runner cursors a settlement removed. Ranges never overlap and never cover a
    retained `session_event` row."""

    __tablename__ = "session_event_settled_range"
    __table_args__ = (
        CheckConstraint("first_cursor <= last_cursor", name="settled_range_ordered"),
        ForeignKeyConstraint(
            ["session_id", "completion_cursor"],
            ["session_event_settlement.session_id", "session_event_settlement.completion_cursor"],
            name="settled_range_settlement",
        ),
    )

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    first_cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_cursor: Mapped[int] = mapped_column(BigInteger)
    completion_cursor: Mapped[int] = mapped_column(BigInteger)
