"""The Sandbox Service's command admission schema, in the `sandbox_commands` database.

Sessions are referenced by public Session ID only: their history lives in another database, so
there is no foreign key, and destination authorization happens before a row is written.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentplane.protocol import command_pb2, event_log_pb2
from util.sqlalchemy_protobuf import ProtobufColumn

# gazelle:include_dep @pypi//protobuf


class Base(DeclarativeBase):
    pass


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


class AdmissionReconciliation(Base):
    """How far a Session's runner journal has been reconciled against its submissions.

    Its own cursor, committed with the admissions it reconciled, so reconciliation neither waits on
    nor shares a transaction with whoever stores the history.
    """

    __tablename__ = "admission_reconciliation"
    __table_args__ = (CheckConstraint("reconciled_through >= 0", name="reconciled_through_nonnegative"),)

    session_id: Mapped[UUID] = mapped_column(primary_key=True)
    # The runner journal the cursor counts in; a different source is a conflict, not a reset.
    source_id: Mapped[str] = mapped_column(String)
    reconciled_through: Mapped[int] = mapped_column(BigInteger)
