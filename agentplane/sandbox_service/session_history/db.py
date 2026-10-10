"""Durable Session history, independent of a Sandbox CR and runner PVC.

The migration is owned by Sandbox Service. No app tables or app-issued identities are used.
"""

from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, LargeBinary, Sequence, String, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Allocated only by `Store`, under a lock that orders positions by commit (WatchSessions resumes after one).
CHANGE_POSITION = Sequence("session_change_position", metadata=Base.metadata)


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
        Index("ux_history_change_position", "change_position", unique=True),
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
    # Position of this Session's latest change in the WatchSessions feed. Every write
    # that changes the Session's identity or binding takes a new one.
    change_position: Mapped[int] = mapped_column(BigInteger)


class SessionEvent(Base):
    __tablename__ = "session_event"

    session_id: Mapped[UUID] = mapped_column(ForeignKey("session_history.id"), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Preserve the complete wire entry (including unknown fields and native frames). JSONB
    # cannot represent arbitrary native bytes, and a parsed fold is not a replayable history.
    payload: Mapped[bytes] = mapped_column(LargeBinary)
