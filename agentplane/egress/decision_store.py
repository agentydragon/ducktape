"""Shared PostgreSQL diagnostic history. Schema changes run only through Alembic."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Text, delete, func, select
from sqlalchemy.dialects.postgresql import INET, insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentplane.egress.decisions import DecisionRecord, Outcome, Phase
from agentplane.egress.policy import DenyReason
from agentplane.subjects import ServiceAccountRef

# gazelle:include_dep @pypi//asyncpg


class Base(DeclarativeBase):
    pass


class DecisionRecordRow(Base):
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


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(
        make_url(database_url).set(drivername="postgresql+asyncpg"),
        pool_size=2,
        max_overflow=0,
        pool_timeout=2,
        connect_args={"timeout": 2, "command_timeout": 2},
        hide_parameters=True,
    )


class DecisionStore:
    def __init__(self, engine: AsyncEngine, *, retention: timedelta, capacity: int = 200) -> None:
        if retention <= timedelta(0) or not 1 <= capacity <= 1000:
            raise ValueError("positive retention and capacity between 1 and 1000 required")
        self.engine = engine
        self._sessions = async_sessionmaker(engine)
        self.retention = retention
        self.capacity = capacity

    async def append(self, records: list[DecisionRecord]) -> None:
        values = []
        for record in records:
            value = record.model_dump(exclude={"path", "at", "subject"})
            value["decided_at"] = record.at
            value["subject_namespace"] = None if record.subject is None else record.subject.namespace
            value["subject_name"] = None if record.subject is None else record.subject.name
            values.append(value)
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(DecisionRecordRow).values(values).on_conflict_do_nothing(index_elements=["event_id"])
            )

    async def recent(self, subject: ServiceAccountRef | None) -> list[DecisionRecord]:
        """One subject's recent decisions, or -- for `None` -- the refusals that never authenticated."""
        query = (
            select(DecisionRecordRow)
            .where(
                DecisionRecordRow.subject_namespace == (None if subject is None else subject.namespace),
                DecisionRecordRow.subject_name == (None if subject is None else subject.name),
                DecisionRecordRow.decided_at >= datetime.now(UTC) - self.retention,
            )
            .order_by(DecisionRecordRow.decided_at.desc(), DecisionRecordRow.event_id.desc())
            .limit(self.capacity)
        )
        async with self._sessions() as session:
            rows = list(await session.scalars(query))
        return [
            DecisionRecord(
                event_id=row.event_id,
                producer_id=row.producer_id,
                at=row.decided_at,
                subject=(
                    None
                    if row.subject_namespace is None or row.subject_name is None
                    else ServiceAccountRef(namespace=row.subject_namespace, name=row.subject_name)
                ),
                source_pod_uid=row.source_pod_uid,
                connection_id=row.connection_id,
                phase=Phase(row.phase),
                method=row.method,
                host=row.host,
                port=row.port,
                outcome=Outcome(row.outcome),
                reason=DenyReason(row.reason) if row.reason is not None else None,
                binding=row.binding,
                policy=row.policy,
                rule=row.rule,
                substituted=row.substituted,
                address=str(row.address) if row.address is not None else None,
            )
            for row in reversed(rows)
        ]

    async def cleanup(self) -> None:
        # Fixed-size, SKIP LOCKED batches keep concurrent replica cleanup bounded and non-exclusive.
        expired = (
            select(DecisionRecordRow.event_id)
            .where(DecisionRecordRow.decided_at < datetime.now(UTC) - self.retention)
            .order_by(DecisionRecordRow.decided_at, DecisionRecordRow.event_id)
            .limit(1000)
            .with_for_update(skip_locked=True)
        )
        async with self.engine.begin() as connection:
            await connection.execute(delete(DecisionRecordRow).where(DecisionRecordRow.event_id.in_(expired)))
