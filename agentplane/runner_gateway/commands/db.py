"""The Runner Gateway's own schema, in the `sandbox_commands` database: its Session registry,
command admission and runner connections.

Session history lives in another database, keyed by the same public Session ID with no foreign key.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, LargeBinary, String, UniqueConstraint, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2 as control_pb2
from util.sqlalchemy_protobuf import ProtobufColumn

# gazelle:include_dep @pypi//protobuf


class Base(DeclarativeBase):
    pass


class Session(Base):
    """A public Session in one Sandbox incarnation, which is also the runner's Session ID, and its Open
    reservation."""

    __tablename__ = "session"
    __table_args__ = (
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
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_namespace: Mapped[str] = mapped_column(String)
    sandbox_name: Mapped[str] = mapped_column(String)
    # Absent for imported Sessions whose original Sandbox UID was not retained in app history.
    sandbox_uid: Mapped[UUID | None]
    # The caller provides an opaque key, never a runner session ID. Absent for imported Sessions.
    caller_namespace: Mapped[str | None] = mapped_column(String)
    caller_name: Mapped[str | None] = mapped_column(String)
    open_key: Mapped[str | None] = mapped_column(String)
    # Original Open inputs identify a retry; the effective launch spec is frozen
    # before runner contact, so a changed default cannot alter the retried launch.
    open_request: Mapped[bytes | None] = mapped_column(LargeBinary)
    launch_spec: Mapped[bytes | None] = mapped_column(LargeBinary)


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
    )

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    runner_command: Mapped[command_pb2.Command] = mapped_column(ProtobufColumn(command_pb2.Command))
    caller_namespace: Mapped[str] = mapped_column(String)
    caller_name: Mapped[str] = mapped_column(String)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    state: Mapped[str] = mapped_column(String)
    # TODO: consider trimming the stored admission entry; the runner journal and the History Service
    # hold it too. Kept for now: commands are few next to model output.
    admission: Mapped[event_log_pb2.EventEntry | None] = mapped_column(ProtobufColumn(event_log_pb2.EventEntry))
    rejection: Mapped[str | None] = mapped_column(String(512))
    # Delivery over the runner control stream (`agentplane/docs/runner_control.md` § Dispatch attempts), absent
    # until the first control-stream submit. A pending submission is sent only while its attempt is open,
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
    attempt_refusal: Mapped[control_pb2.CommandRefused | None] = mapped_column(
        ProtobufColumn(control_pb2.CommandRefused)
    )
