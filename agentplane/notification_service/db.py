"""Service-owned rows; PostgreSQL is the queue, prefix allocator, and recovery authority."""

from datetime import datetime
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, LargeBinary, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Inbox(Base):
    __tablename__ = "inbox"
    __table_args__ = (UniqueConstraint("owner_namespace", "owner_name", "destination_key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_namespace: Mapped[str]
    owner_name: Mapped[str]
    destination_key: Mapped[str]
    destination_ref: Mapped[dict[str, str]] = mapped_column(JSONB)
    session_id: Mapped[str]
    last_cursor: Mapped[int] = mapped_column(BigInteger)
    acknowledged: Mapped[int] = mapped_column(BigInteger)
    covered: Mapped[int] = mapped_column(BigInteger)
    expired_through: Mapped[int] = mapped_column(BigInteger)
    retired: Mapped[bool]
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_poll: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    claim: Mapped[UUID | None]
    claim_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivery_error: Mapped[str | None]


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = (
        UniqueConstraint("inbox_id", "idempotency_key"),
        CheckConstraint("creation ? 'source'", name="subscription_creation_source"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    inbox_id: Mapped[UUID] = mapped_column(ForeignKey("inbox.id", ondelete="CASCADE"))
    request_id: Mapped[UUID]
    idempotency_key: Mapped[str]
    creation: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    creator: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    version: Mapped[int]
    after_sequence: Mapped[int] = mapped_column(BigInteger)
    cancelled: Mapped[bool]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_poll: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None]


class Entry(Base):
    __tablename__ = "entry"
    __table_args__ = (UniqueConstraint("inbox_id", "request_id", "source_sequence"),)
    inbox_id: Mapped[UUID] = mapped_column(ForeignKey("inbox.id", ondelete="CASCADE"), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    request_id: Mapped[UUID]
    source_sequence: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Match(Base):
    __tablename__ = "subscription_match"
    subscription_id: Mapped[UUID] = mapped_column(ForeignKey("subscription.id", ondelete="CASCADE"), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class Notice(Base):
    __tablename__ = "notice"
    inbox_id: Mapped[UUID] = mapped_column(ForeignKey("inbox.id", ondelete="CASCADE"), primary_key=True)
    command_id: Mapped[UUID]
    through_cursor: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str]
    attempted: Mapped[bool]
    admitted: Mapped[bool]
    confirmed: Mapped[bool]
    error: Mapped[str | None]
    runner_cursor: Mapped[int] = mapped_column(BigInteger)
    runner_entry: Mapped[bytes | None] = mapped_column(LargeBinary)
