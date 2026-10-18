"""The decision-history tables as the Alembic migrations in `migrations/` create them."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class EgressDecision(Base):
    __tablename__ = "egress_decision"
    __table_args__ = (
        # A subject is a namespace and a name together; the constraint keeps the pair whole.
        CheckConstraint("(subject_namespace IS NULL) = (subject_name IS NULL)", name="egress_decision_subject_whole"),
        Index("egress_decision_subject_recent", "subject_namespace", "subject_name", "decided_at", "event_id"),
        Index("egress_decision_denials_recent", "outcome", "decided_at", "event_id"),
        Index("egress_decision_retention", "decided_at", "event_id"),
    )

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    producer_id: Mapped[UUID]
    subject_namespace: Mapped[str | None] = mapped_column(Text)
    subject_name: Mapped[str | None] = mapped_column(Text)
    source_pod_uid: Mapped[str | None] = mapped_column(Text)
    connection_id: Mapped[str] = mapped_column(Text)
    phase: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(Text)
    host: Mapped[str] = mapped_column(Text)
    port: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    binding: Mapped[str | None] = mapped_column(Text)
    policy: Mapped[str | None] = mapped_column(Text)
    rule: Mapped[int | None] = mapped_column(Integer)
    substituted: Mapped[bool] = mapped_column(Boolean)
    address: Mapped[str | None] = mapped_column(INET)
