"""Shared SQLAlchemy schema for the synced Plaid mirror database."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    column,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from util.db_url import async_engine_url


def _finite_values(*names: str) -> ColumnElement[bool]:
    # CHECK already accepts NULL, so nullable columns need no separate branch.
    return and_(*(column(name).not_in(("NaN", "Infinity", "-Infinity")) for name in names))


def utcnow() -> datetime:
    return datetime.now(UTC)


# SQLAlchemy loads the asyncpg dialect at runtime from the URL scheme.
# gazelle:include_dep @pypi//asyncpg


def async_session_factory(
    db_url: str, *, pool_size: int = 5, max_overflow: int = 10
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Construct a fresh AsyncEngine + sessionmaker for `db_url`.

    Each call returns a new engine with its own connection pool, so callers that issue
    repeated queries against the same database (every API request, in the budget path)
    should construct one at process startup and reuse it -- the SSL handshake + pool
    init costs ~500ms per engine over the cluster port-forward. Callers own disposal."""
    engine = create_async_engine(
        async_engine_url(db_url), pool_pre_ping=True, pool_recycle=1800, pool_size=pool_size, max_overflow=max_overflow
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class LinkRow(Base):
    __tablename__ = "links"

    item_id: Mapped[str] = mapped_column(String, primary_key=True)
    institution_id: Mapped[str | None] = mapped_column(String, nullable=True)
    institution_name: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    products_requested: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    transaction_days_requested: Mapped[int | None] = mapped_column(Integer, nullable=True)
    products_authorized: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    products_billed: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    access_token_secret: Mapped[str] = mapped_column(String, nullable=False)
    transactions_cursor: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accounts_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountRow(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("account_id", "item_id", name="uq_accounts_account_item"),)

    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    official_name: Mapped[str | None] = mapped_column(String, nullable=True)
    mask: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False)
    subtype: Mapped[str | None] = mapped_column(String, nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    detached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TransactionRow(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(_finite_values("amount"), name="ck_transactions_finite_values"),
        ForeignKeyConstraint(
            ["account_id", "item_id"], ["accounts.account_id", "accounts.item_id"], name="fk_transactions_account_item"
        ),
    )

    transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(asdecimal=True), nullable=False)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    merchant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pending_transaction_id: Mapped[str | None] = mapped_column(String, nullable=True)
    pfc_primary: Mapped[str | None] = mapped_column(String, nullable=True)
    pfc_detailed: Mapped[str | None] = mapped_column(String, nullable=True)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BalanceSnapshotRow(Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = (
        CheckConstraint(_finite_values("available", "current", "limit"), name="ck_balance_snapshots_finite_values"),
        ForeignKeyConstraint(
            ["account_id", "item_id"],
            ["accounts.account_id", "accounts.item_id"],
            name="fk_balance_snapshots_account_item",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    current: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    limit: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)


class SecurityRow(Base):
    __tablename__ = "securities"

    security_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    ticker_symbol: Mapped[str | None] = mapped_column(String, nullable=True)
    type: Mapped[str | None] = mapped_column(String, nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class HoldingCaptureRow(Base):
    __tablename__ = "holding_captures"
    __table_args__ = (
        UniqueConstraint("api_event_id", name="uq_holding_captures_api_event"),
        UniqueConstraint("id", "item_id", name="uq_holding_captures_id_item"),
        Index("idx_holding_captures_item_latest", "item_id", "captured_at", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    api_event_id: Mapped[int] = mapped_column(Integer, ForeignKey("plaid_api_events.id"), nullable=False)


class HoldingCaptureAccountRow(Base):
    __tablename__ = "holding_capture_accounts"
    __table_args__ = (
        CheckConstraint(_finite_values("current"), name="ck_holding_capture_accounts_finite_values"),
        ForeignKeyConstraint(
            ["capture_id", "item_id"],
            ["holding_captures.id", "holding_captures.item_id"],
            name="fk_holding_capture_accounts_capture_item",
        ),
        ForeignKeyConstraint(
            ["account_id", "item_id"],
            ["accounts.account_id", "accounts.item_id"],
            name="fk_holding_capture_accounts_account_item",
        ),
    )

    capture_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, primary_key=True)
    item_id: Mapped[str] = mapped_column(String, nullable=False)
    current: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)


class HoldingSnapshotRow(Base):
    __tablename__ = "holding_snapshots"
    __table_args__ = (
        CheckConstraint(
            _finite_values("quantity", "cost_basis", "institution_price", "institution_value"),
            name="ck_holding_snapshots_finite_values",
        ),
        UniqueConstraint("capture_id", "account_id", "security_id", name="uq_holding_snapshots_capture_position"),
        ForeignKeyConstraint(
            ["account_id", "item_id"],
            ["accounts.account_id", "accounts.item_id"],
            name="fk_holding_snapshots_account_item",
        ),
        ForeignKeyConstraint(
            ["capture_id", "account_id"],
            ["holding_capture_accounts.capture_id", "holding_capture_accounts.account_id"],
            name="fk_holding_snapshots_capture_account",
        ),
        ForeignKeyConstraint(
            ["capture_id", "item_id"],
            ["holding_captures.id", "holding_captures.item_id"],
            name="fk_holding_snapshots_capture_item",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    capture_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    security_id: Mapped[str] = mapped_column(String, ForeignKey("securities.security_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    institution_price: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    institution_value: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class InvestmentTransactionRow(Base):
    __tablename__ = "investment_transactions"
    __table_args__ = (
        CheckConstraint(
            _finite_values("amount", "quantity", "price", "fees"), name="ck_investment_transactions_finite_values"
        ),
        ForeignKeyConstraint(
            ["account_id", "item_id"],
            ["accounts.account_id", "accounts.item_id"],
            name="fk_investment_transactions_account_item",
        ),
    )

    investment_transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    security_id: Mapped[str | None] = mapped_column(String, nullable=True)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    fees: Mapped[Decimal | None] = mapped_column(Numeric(asdecimal=True), nullable=True)
    type: Mapped[str | None] = mapped_column(String, nullable=True)
    subtype: Mapped[str | None] = mapped_column(String, nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LiabilityCreditSnapshotRow(Base):
    __tablename__ = "liability_credit_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "item_id"],
            ["accounts.account_id", "accounts.item_id"],
            name="fk_liability_credit_snapshots_account_item",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class LiabilityMortgageSnapshotRow(Base):
    __tablename__ = "liability_mortgage_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "item_id"],
            ["accounts.account_id", "accounts.item_id"],
            name="fk_liability_mortgage_snapshots_account_item",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class LiabilityStudentSnapshotRow(Base):
    __tablename__ = "liability_student_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "item_id"],
            ["accounts.account_id", "accounts.item_id"],
            name="fk_liability_student_snapshots_account_item",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class SyncRunRow(Base):
    __tablename__ = "sync_runs"

    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True, native_uuid=True), primary_key=True)
    trigger: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    configured_windows: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class ItemSyncQueueRow(Base):
    __tablename__ = "item_sync_queue"
    __table_args__ = (Index("idx_item_sync_queue_requested", "requested_at"),)

    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    full_sync: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class PlaidWebhookDeliveryRow(Base):
    __tablename__ = "plaid_webhook_deliveries"
    __table_args__ = (Index("idx_plaid_webhook_deliveries_received_at", "received_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    webhook_type: Mapped[str | None] = mapped_column(String, nullable=True)
    webhook_code: Mapped[str | None] = mapped_column(String, nullable=True)
    item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    disposition: Mapped[str] = mapped_column(String, nullable=False, default="received")


class PlaidApiEventRow(Base):
    __tablename__ = "plaid_api_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sync_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True, native_uuid=True), nullable=True)
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    account_id: Mapped[str | None] = mapped_column(String, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
