"""The Sandbox Service's own schema, in the `sandbox_commands` database: its Session registry,
command admission and runner connections.

Session history lives in another database, keyed by the same public Session ID with no foreign key.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    LargeBinary,
    Sequence,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner_channel import protocol_pb2 as channel_pb2
from util.sqlalchemy_protobuf import ProtobufColumn

# gazelle:include_dep @pypi//protobuf


class Base(DeclarativeBase):
    pass


# Allocated only by `SessionRegistry`, under a lock that orders positions by commit (WatchSessions resumes after one).
CHANGE_POSITION = Sequence("session_change_position", metadata=Base.metadata)


class Session(Base):
    """A public Session: its runner locator in one Sandbox incarnation, Open reservation and feed position."""

    __tablename__ = "session"
    __table_args__ = (
        # A runner locator is a physical binding, not a durable identity. Imported locators with
        # no Sandbox UID still cannot be claimed twice.
        Index(
            "ux_session_runner_locator",
            "sandbox_namespace",
            "sandbox_name",
            "sandbox_uid",
            "runner_session_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("runner_session_id IS NOT NULL"),
        ),
        Index(
            "ux_session_open_key",
            "caller_namespace",
            "caller_name",
            "sandbox_namespace",
            "sandbox_name",
            "sandbox_uid",
            "open_key",
            unique=True,
            postgresql_where=text("open_key IS NOT NULL"),
        ),
        Index("ux_session_change_position", "change_position", unique=True),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_namespace: Mapped[str] = mapped_column(String)
    sandbox_name: Mapped[str] = mapped_column(String)
    # Absent for imported Sessions whose original Sandbox UID was not retained in app history.
    sandbox_uid: Mapped[UUID | None]
    runner_session_id: Mapped[str | None] = mapped_column(String)
    # The caller provides an opaque key, never a runner session ID. Absent for imported Sessions.
    caller_namespace: Mapped[str | None] = mapped_column(String)
    caller_name: Mapped[str | None] = mapped_column(String)
    open_key: Mapped[str | None] = mapped_column(String)
    # Original Open inputs identify a retry; the effective launch spec is frozen
    # before runner contact, so a changed default cannot alter the retried launch.
    open_request: Mapped[bytes | None] = mapped_column(LargeBinary)
    launch_spec: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Position of this Session's latest change in the WatchSessions feed.
    change_position: Mapped[int] = mapped_column(BigInteger)


class CommandSubmission(Base):
    """Service-owned command envelope, distinct from the runner's execution journal."""

    __tablename__ = "command_submission"
    __table_args__ = (
        CheckConstraint("state IN ('pending_admission', 'admitted', 'rejected')", name="submission_state"),
        CheckConstraint(
            "(state = 'admitted' AND admission IS NOT NULL AND rejection IS NULL) OR "
            "(state = 'pending_admission' AND admission IS NULL AND rejection IS NULL) OR "
            "(state = 'rejected' AND admission IS NULL AND rejection IS NOT NULL)",
            name="submission_evidence",
        ),
        CheckConstraint(
            "(attempt_sandbox_uid IS NULL AND attempt_runner_session_id IS NULL AND attempt_deadline IS NULL "
            "AND attempt_sent_epoch IS NULL AND attempt_refusal IS NULL) OR "
            "(attempt_sandbox_uid IS NOT NULL AND attempt_runner_session_id IS NOT NULL "
            "AND attempt_deadline IS NOT NULL)",
            name="submission_attempt",
        ),
        CheckConstraint("attempt_sent_epoch IS NULL OR attempt_sent_epoch > 0", name="attempt_sent_epoch_positive"),
        # A receipt names its command by the runner's Session and the command ID.
        UniqueConstraint(
            "attempt_sandbox_uid", "attempt_runner_session_id", "command_id", name="submission_attempt_destination"
        ),
        # CLEANUP(added 2026-10-11): Unread. Remove it with the migration that drops it and the
        #   `admission_reconciliation` table, once no Sandbox Service replica runs an image that reads
        #   them. Until then it stays declared: the migrate step refuses an index the models lack.
        Index("ix_command_submission_pending", "session_id", postgresql_where=text("state = 'pending_admission'")),
    )

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    runner_command: Mapped[command_pb2.Command] = mapped_column(ProtobufColumn(command_pb2.Command))
    caller_namespace: Mapped[str] = mapped_column(String)
    caller_name: Mapped[str] = mapped_column(String)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    state: Mapped[str] = mapped_column(String)
    admission: Mapped[event_log_pb2.EventEntry | None] = mapped_column(ProtobufColumn(event_log_pb2.EventEntry))
    rejection: Mapped[str | None] = mapped_column(String(512))
    # Delivery over the runner channel (`agentplane/docs/runner_channel.md` § Dispatch attempts), absent
    # until the first channel submit. A pending submission is sent only while its attempt is open,
    # before `attempt_deadline`, so a reconnecting runner is never sent stale work. Each submit
    # reopens the same attempt. A submission whose receipt was lost and is never retried stays
    # `pending_admission`; nothing settles it in the background.
    attempt_sandbox_uid: Mapped[UUID | None] = mapped_column()
    # The runner's own Session identifier, which `DeliverCommand` carries.
    attempt_runner_session_id: Mapped[str | None] = mapped_column(String)
    attempt_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The connection epoch the open attempt was sent on; it is sent at most once per epoch.
    attempt_sent_epoch: Mapped[int | None] = mapped_column(BigInteger)
    # The runner's refusal of the open attempt. The submission stays pending.
    attempt_refusal: Mapped[channel_pb2.CommandRefused | None] = mapped_column(
        ProtobufColumn(channel_pb2.CommandRefused)
    )


class RunnerConnection(Base):
    """The channel operations a Sandbox's runner connections have agreed."""

    __tablename__ = "runner_connection"
    __table_args__ = (CheckConstraint("connection_epoch > 0", name="connection_epoch_positive"),)

    sandbox_uid: Mapped[UUID] = mapped_column(primary_key=True)
    # Unread and no longer written: the Sandbox's Lease holds the connection. They go with the table.
    pod_uid: Mapped[str | None] = mapped_column(String)
    connection_epoch: Mapped[int | None] = mapped_column(BigInteger)
    owner_replica: Mapped[str | None] = mapped_column(String)
    runner_boot_id: Mapped[str | None] = mapped_column(String)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Every channel operation a connection of this Sandbox has agreed in its handshake. It only
    # grows, so once a runner carries an operation over the channel it never falls back inbound.
    operations: Mapped[list[str]] = mapped_column(ARRAY(String))
