"""Durable Session history, independent of a Sandbox CR and runner PVC.

The migration is owned by Sandbox Service. No app tables or app-issued identities are used.
"""

from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionHistory(Base):
    __tablename__ = "session_history"
    __table_args__ = (
        CheckConstraint("last_cursor >= 0", name="history_last_cursor_nonnegative"),
        # A runner locator is a physical binding, not a durable identity. Reservations
        # have no runner yet; imported locators with no UID still cannot be claimed twice.
        Index(
            "ux_history_runner_locator",
            "sandbox_namespace",
            "sandbox_name",
            "sandbox_uid",
            "runner_session_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("runner_session_id IS NOT NULL"),
        ),
        Index(
            "ux_history_open_key",
            "caller_namespace",
            "caller_name",
            "sandbox_namespace",
            "sandbox_name",
            "sandbox_uid",
            "open_key",
            unique=True,
            postgresql_where=text("open_key IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_namespace: Mapped[str] = mapped_column(String)
    sandbox_name: Mapped[str] = mapped_column(String)
    # Nullable for imported records whose original Sandbox UID was not retained in app history.
    sandbox_uid: Mapped[UUID | None]
    runner_session_id: Mapped[str | None] = mapped_column(String)
    # The caller provides an opaque key, never a runner session ID. Nullable for imported histories.
    caller_namespace: Mapped[str | None] = mapped_column(String)
    caller_name: Mapped[str | None] = mapped_column(String)
    open_key: Mapped[str | None] = mapped_column(String)
    # Original Open inputs identify a retry; the effective launch spec is frozen
    # before runner contact, so a changed default cannot alter the retried launch.
    open_request: Mapped[bytes | None] = mapped_column(LargeBinary)
    launch_spec: Mapped[bytes | None] = mapped_column(LargeBinary)
    source_id: Mapped[str | None] = mapped_column(String)
    last_cursor: Mapped[int] = mapped_column(BigInteger)
    # Complete SessionFeedState protobuf, absent until a runner prefix is observed.
    feed_state: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Per-session override of the service's delta settlement default; NULL inherits it.
    settle_deltas: Mapped[bool | None] = mapped_column(Boolean)


class SessionEvent(Base):
    __tablename__ = "session_event"

    session_id: Mapped[UUID] = mapped_column(ForeignKey("session_history.id"), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Preserve the complete wire entry (including unknown fields and native frames). JSONB
    # cannot represent arbitrary native bytes, and a parsed fold is not a replayable history.
    payload: Mapped[bytes] = mapped_column(LargeBinary)


class SessionEventSettlement(Base):
    """One item field whose streamed delta entries were removed from `session_event`.

    Authored by the Sandbox Service, never by the runner: `session_event` keeps only runner entries,
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
