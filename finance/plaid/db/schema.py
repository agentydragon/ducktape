"""Shared SQLAlchemy schema for the synced Plaid mirror database."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalise_async_db_url(db_url: str) -> str:
    # gazelle:include_dep @pypi//asyncpg
    # (SQLAlchemy loads the asyncpg dialect at runtime via the URL scheme below;
    # nothing imports it, so gazelle cannot see the dependency.)
    return db_url.replace("postgresql://", "postgresql+asyncpg://", 1)


def async_session_factory(db_url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Construct a fresh AsyncEngine + sessionmaker for `db_url`.

    Each call returns a new engine with its own connection pool, so callers that issue
    repeated queries against the same database (every API request, in the budget path)
    should construct one at process startup and reuse it -- the SSL handshake + pool
    init costs ~500ms per engine over the cluster port-forward. Callers own disposal."""
    engine = create_async_engine(normalise_async_db_url(db_url), pool_pre_ping=True, pool_recycle=1800)
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
    transactions_update_status: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountRow(Base):
    __tablename__ = "accounts"

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


class TransactionRow(Base):
    __tablename__ = "transactions"

    transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available: Mapped[float | None] = mapped_column(Float, nullable=True)
    current: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit: Mapped[float | None] = mapped_column(Float, nullable=True)
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


class HoldingSnapshotRow(Base):
    __tablename__ = "holding_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    security_id: Mapped[str] = mapped_column(String, ForeignKey("securities.security_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_basis: Mapped[float | None] = mapped_column(Float, nullable=True)
    institution_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    institution_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class InvestmentTransactionRow(Base):
    __tablename__ = "investment_transactions"

    investment_transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    security_id: Mapped[str | None] = mapped_column(String, nullable=True)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float | None] = mapped_column(Float, nullable=True)
    type: Mapped[str | None] = mapped_column(String, nullable=True)
    subtype: Mapped[str | None] = mapped_column(String, nullable=True)
    iso_currency_code: Mapped[str | None] = mapped_column(String, nullable=True)
    removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class LiabilityCreditSnapshotRow(Base):
    __tablename__ = "liability_credit_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class LiabilityMortgageSnapshotRow(Base):
    __tablename__ = "liability_mortgage_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.account_id"), nullable=False)
    item_id: Mapped[str] = mapped_column(String, ForeignKey("links.item_id"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class LiabilityStudentSnapshotRow(Base):
    __tablename__ = "liability_student_snapshots"

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
