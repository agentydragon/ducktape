"""The Runner Gateway's own schema, in the `sandbox_commands` database: its Session registry.

Session history lives in another database, keyed by the same public Session ID with no foreign key.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentplane.proto import command_pb2, event_log_pb2, runner_control_pb2
from util.sqlalchemy_protobuf import ProtobufColumn

# gazelle:include_dep @pypi//protobuf


class Base(DeclarativeBase):
    pass


class Session(Base):
    """A public Session in one Sandbox incarnation, which is also the runner's Session ID."""

    __tablename__ = "session"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    sandbox_namespace: Mapped[str] = mapped_column(String)
    sandbox_name: Mapped[str] = mapped_column(String)
    # Absent for imported Sessions whose original Sandbox UID was not retained in app history.
    sandbox_uid: Mapped[UUID | None]


# CLEANUP(added 2026-10-12): Nothing reads or writes this table. Drop it, and this model, in a
#   migration once every Runner Gateway replica runs an image built without command submissions.
class CommandSubmission(Base):
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
    admission: Mapped[event_log_pb2.EventEntry | None] = mapped_column(ProtobufColumn(event_log_pb2.EventEntry))
    rejection: Mapped[str | None] = mapped_column(String(512))
    attempt_sandbox_uid: Mapped[UUID | None] = mapped_column()
    attempt_runner_session_id: Mapped[str | None] = mapped_column(String)
    attempt_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_sent_epoch: Mapped[int | None] = mapped_column(BigInteger)
    attempt_refusal: Mapped[runner_control_pb2.CommandRefused | None] = mapped_column(
        ProtobufColumn(runner_control_pb2.CommandRefused)
    )
