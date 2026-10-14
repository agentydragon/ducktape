"""The Dispatcher's rows. PostgreSQL is the queue and the delivery record."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Identity, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentplane.proto import command_pb2, event_log_pb2, runner_pb2
from agentplane.session_dispatcher.models import DeliveryState, Operation
from util.sqlalchemy_protobuf import ProtobufColumn
from util.sqlalchemy_types import StringBackedStrEnumColumn

# gazelle:include_dep @pypi//protobuf


class Base(DeclarativeBase):
    pass


class Dispatch(Base):
    __tablename__ = "dispatch"
    __table_args__ = (
        Index("ix_dispatch_session_state", "session_id", "state"),
        Index("ix_dispatch_sandbox_uid_state", "sandbox_uid", "state"),
        Index("ix_dispatch_sandbox_name", "sandbox_name"),
    )
    # The order a Session's commands are delivered in.
    position: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    command_id: Mapped[str] = mapped_column(unique=True)
    session_id: Mapped[str]
    sandbox_name: Mapped[str]
    # The Sandbox the command was queued for: a Sandbox recreated under its name is another one.
    sandbox_uid: Mapped[UUID]
    wake_eligible: Mapped[bool] = mapped_column(default=False)
    wake_caller_namespace: Mapped[str | None]
    wake_caller_name: Mapped[str | None]
    # Null until the Dispatcher requested a wake; empty string records a wake before any incarnation existed.
    wake_from_incarnation: Mapped[str | None]
    # Queried by the queue cap and by a stop, which the command carries too.
    operation: Mapped[Operation] = mapped_column(StringBackedStrEnumColumn(Operation))
    command: Mapped[command_pb2.Command] = mapped_column(ProtobufColumn(command_pb2.Command))
    state: Mapped[DeliveryState] = mapped_column(StringBackedStrEnumColumn(DeliveryState))
    reason: Mapped[str | None]
    admission: Mapped[event_log_pb2.EventEntry | None] = mapped_column(ProtobufColumn(event_log_pb2.EventEntry))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionOpening(Base):
    """A Session's creation, kept as long as its Sandbox exists."""

    __tablename__ = "session_opening"
    __table_args__ = (Index("ix_session_opening_sandbox_uid", "sandbox_uid"),)
    session_id: Mapped[str] = mapped_column(primary_key=True)
    sandbox_name: Mapped[str]
    sandbox_uid: Mapped[UUID]
    wake_eligible: Mapped[bool] = mapped_column(default=False)
    wake_caller_namespace: Mapped[str | None]
    wake_caller_name: Mapped[str | None]
    # Null until the Dispatcher requested a wake; empty string records a wake before any incarnation existed.
    wake_from_incarnation: Mapped[str | None]
    spec: Mapped[runner_pb2.SessionSpec] = mapped_column(ProtobufColumn(runner_pb2.SessionSpec))
    setup_script: Mapped[str | None]
    state: Mapped[DeliveryState] = mapped_column(StringBackedStrEnumColumn(DeliveryState))
    reason: Mapped[str | None]
    attached: Mapped[runner_pb2.Attached | None] = mapped_column(ProtobufColumn(runner_pb2.Attached))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SandboxOwnership(Base):
    """The database fence for the Dispatcher replica currently scheduling one Sandbox."""

    __tablename__ = "sandbox_ownership"
    sandbox_uid: Mapped[UUID] = mapped_column(primary_key=True)
    # This is the settlement fence. It increases only after a Lease acquisition wins a DB CAS.
    generation: Mapped[int] = mapped_column(BigInteger)
    lease_uid: Mapped[str]
    lease_transitions: Mapped[int]
    holder_identity: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WakeAttempt(Base):
    """One authenticated caller's distinct guarded wake-generation request."""

    __tablename__ = "wake_attempt"
    __table_args__ = (Index("ix_wake_attempt_caller_time", "caller_namespace", "caller_name", "attempted_at"),)
    caller_namespace: Mapped[str] = mapped_column(primary_key=True)
    caller_name: Mapped[str] = mapped_column(primary_key=True)
    sandbox_uid: Mapped[UUID] = mapped_column(primary_key=True)
    wake_generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
