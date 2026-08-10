"""Postgres storage for the Plaid self-contained link service."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import delete, exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from finance.plaid.db.schema import (
    AccountRow,
    BalanceSnapshotRow,
    HoldingSnapshotRow,
    InvestmentTransactionRow,
    LiabilityCreditSnapshotRow,
    LiabilityMortgageSnapshotRow,
    LiabilityStudentSnapshotRow,
    LinkRow,
    PlaidApiEventRow,
    SecurityRow,
    SyncRunRow,
    TransactionRow,
    async_session_factory,
    utcnow,
)

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _run_alembic_migrations(conn: Any) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = conn
    alembic_command.upgrade(cfg, "head")


@dataclass(frozen=True)
class StoredLink:
    item_id: str
    label: str | None
    institution_id: str | None
    institution_name: str | None
    products_requested: list[str]
    transaction_days_requested: int | None
    products_authorized: list[str]
    products_billed: list[str]
    status: str
    access_token_secret: str
    last_synced_at: datetime | None
    earliest_transaction_date: date | None = None
    latest_transaction_date: date | None = None
    synced_transaction_count: int = 0


@dataclass(frozen=True)
class ApiEvent:
    endpoint: str
    status: str
    request_json: dict[str, Any]
    response_json: dict[str, Any] | None = None
    sync_run_id: UUID | None = None
    item_id: str | None = None
    account_id: str | None = None
    request_id: str | None = None
    duration_ms: int | None = None
    error_type: str | None = None
    error_code: str | None = None


class PlaidLinkStorage:
    """Async PostgreSQL storage for Plaid link metadata and mirrored data."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], engine: AsyncEngine) -> None:
        self._session_factory = session_factory
        self._engine = engine

    @classmethod
    async def initialize(cls, db_url: str) -> PlaidLinkStorage:
        engine, session_factory = async_session_factory(db_url)
        async with engine.begin() as conn:
            await conn.run_sync(_run_alembic_migrations)
        return cls(session_factory, engine)

    async def close(self) -> None:
        await self._engine.dispose()

    async def upsert_link(
        self,
        *,
        item_id: str,
        access_token_secret: str,
        products_requested: list[str],
        institution_id: str | None,
        institution_name: str | None,
        label: str | None,
        transaction_days_requested: int | None = None,
        products_authorized: list[str] | None = None,
        products_billed: list[str] | None = None,
        status: str = "active",
    ) -> StoredLink:
        now = utcnow()
        values: dict[str, object] = {
            "item_id": item_id,
            "institution_id": institution_id,
            "institution_name": institution_name,
            "label": label,
            "products_requested": products_requested,
            "products_authorized": products_authorized or products_requested,
            "products_billed": products_billed or [],
            "status": status,
            "access_token_secret": access_token_secret,
            "updated_at": now,
        }
        if transaction_days_requested is not None:
            values["transaction_days_requested"] = transaction_days_requested
        insert_values = values | {"created_at": now}
        update_values = {k: v for k, v in values.items() if k != "item_id"}
        stmt = (
            pg_insert(LinkRow)
            .values(**insert_values)
            .on_conflict_do_update(index_elements=["item_id"], set_=update_values)
            .returning(LinkRow)
        )
        async with self._session_factory() as session:
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
        return _stored_link(row)

    async def mark_link_revoked(self, item_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(LinkRow, item_id)
            if row is not None:
                row.status = "revoked"
                row.updated_at = utcnow()
                await session.commit()

    async def purge_link_data(self, item_id: str) -> None:
        security_ids = (
            select(HoldingSnapshotRow.security_id)
            .where(HoldingSnapshotRow.item_id == item_id)
            .union(
                select(InvestmentTransactionRow.security_id).where(
                    InvestmentTransactionRow.item_id == item_id, InvestmentTransactionRow.security_id.is_not(None)
                )
            )
        )
        async with self._session_factory() as session:
            security_ids_to_check = list((await session.execute(security_ids)).scalars())
            for row_type in (
                LiabilityCreditSnapshotRow,
                LiabilityMortgageSnapshotRow,
                LiabilityStudentSnapshotRow,
                HoldingSnapshotRow,
                InvestmentTransactionRow,
                TransactionRow,
                BalanceSnapshotRow,
                AccountRow,
            ):
                await session.execute(delete(row_type).where(row_type.item_id == item_id))
            await session.execute(delete(LinkRow).where(LinkRow.item_id == item_id))
            if security_ids_to_check:
                await session.execute(
                    delete(SecurityRow).where(
                        SecurityRow.security_id.in_(security_ids_to_check),
                        ~exists().where(HoldingSnapshotRow.security_id == SecurityRow.security_id),
                        ~exists().where(InvestmentTransactionRow.security_id == SecurityRow.security_id),
                    )
                )
            await session.commit()

    async def mark_link_update_succeeded(self, *, item_id: str, products_requested: list[str]) -> StoredLink | None:
        async with self._session_factory() as session:
            row = await session.get(LinkRow, item_id)
            if row is None:
                return None
            row.products_requested = products_requested
            row.products_authorized = _merge_products(list(row.products_authorized), products_requested)
            row.status = "active"
            row.updated_at = utcnow()
            await session.commit()
            return _stored_link(row)

    async def list_active_links(self) -> list[StoredLink]:
        transaction_stats = (
            select(
                TransactionRow.item_id.label("item_id"),
                func.min(TransactionRow.date).label("earliest_transaction_date"),
                func.max(TransactionRow.date).label("latest_transaction_date"),
                func.count(TransactionRow.transaction_id).label("synced_transaction_count"),
            )
            .where(TransactionRow.removed.is_(False))
            .group_by(TransactionRow.item_id)
            .subquery()
        )
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        LinkRow,
                        transaction_stats.c.earliest_transaction_date,
                        transaction_stats.c.latest_transaction_date,
                        transaction_stats.c.synced_transaction_count,
                    )
                    .outerjoin(transaction_stats, transaction_stats.c.item_id == LinkRow.item_id)
                    .where(LinkRow.status != "revoked")
                    .order_by(LinkRow.institution_name)
                )
            ).all()
            return [
                _stored_link(
                    row,
                    earliest_transaction_date=earliest_transaction_date,
                    latest_transaction_date=latest_transaction_date,
                    synced_transaction_count=synced_transaction_count or 0,
                )
                for row, earliest_transaction_date, latest_transaction_date, synced_transaction_count in rows
            ]

    async def get_link(self, item_id: str) -> StoredLink | None:
        async with self._session_factory() as session:
            row = await session.get(LinkRow, item_id)
            return _stored_link(row) if row is not None else None

    async def begin_sync_run(self, *, trigger: str, item_id: str | None, configured_windows: dict[str, Any]) -> UUID:
        run_id = uuid4()
        async with self._session_factory() as session:
            if item_id is not None:
                running = await session.execute(
                    select(func.count())
                    .select_from(SyncRunRow)
                    .where(SyncRunRow.item_id == item_id, SyncRunRow.status == "running")
                )
                if running.scalar_one() > 0:
                    raise RuntimeError(f"sync already running for Plaid item {item_id}")
            session.add(
                SyncRunRow(
                    run_id=run_id,
                    trigger=trigger,
                    mode="v0_full_refresh",
                    item_id=item_id,
                    configured_windows=configured_windows,
                    status="running",
                    started_at=utcnow(),
                )
            )
            await session.commit()
        return run_id

    async def finish_sync_run(self, run_id: UUID, *, status: str, error_summary: str | None = None) -> None:
        async with self._session_factory() as session:
            row = await session.get(SyncRunRow, run_id)
            if row is None:
                raise ValueError(f"sync run not found: {run_id}")
            finished_at = utcnow()
            row.status = status
            row.finished_at = finished_at
            row.error_summary = error_summary
            if status == "succeeded" and row.item_id is not None:
                link = await session.get(LinkRow, row.item_id)
                if link is not None:
                    link.last_synced_at = finished_at
                    link.updated_at = finished_at
            await session.commit()

    async def record_api_event(self, event: ApiEvent) -> None:
        async with self._session_factory() as session:
            session.add(
                PlaidApiEventRow(
                    sync_run_id=event.sync_run_id,
                    endpoint=event.endpoint,
                    item_id=event.item_id,
                    account_id=event.account_id,
                    request_id=event.request_id,
                    status=event.status,
                    duration_ms=event.duration_ms,
                    error_type=event.error_type,
                    error_code=event.error_code,
                    request_json=event.request_json,
                    response_json=event.response_json,
                    created_at=utcnow(),
                )
            )
            await session.commit()

    async def apply_accounts(self, *, item_id: str, accounts: list[dict[str, Any]], captured_at: datetime) -> None:
        async with self._session_factory() as session:
            for account in accounts:
                balances = account.get("balances") or {}
                values = {
                    "account_id": account["account_id"],
                    "item_id": item_id,
                    "name": account["name"],
                    "official_name": account.get("official_name"),
                    "mask": account.get("mask"),
                    "type": account["type"],
                    "subtype": account.get("subtype"),
                    "iso_currency_code": balances.get("iso_currency_code"),
                    "raw_json": account,
                    "updated_at": captured_at,
                }
                stmt = pg_insert(AccountRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["account_id"], set_={k: v for k, v in values.items() if k != "account_id"}
                )
                await session.execute(stmt)
                session.add(
                    BalanceSnapshotRow(
                        account_id=account["account_id"],
                        item_id=item_id,
                        captured_at=captured_at,
                        available=balances.get("available"),
                        current=balances.get("current"),
                        limit=balances.get("limit"),
                        iso_currency_code=balances.get("iso_currency_code"),
                    )
                )
            await session.commit()

    async def reconcile_transactions(
        self,
        *,
        item_id: str,
        start_date: date,
        end_date: date,
        transactions: list[dict[str, Any]],
        captured_at: datetime,
    ) -> None:
        seen = {txn["transaction_id"] for txn in transactions}
        async with self._session_factory() as session:
            for txn in transactions:
                pfc = txn.get("personal_finance_category") or {}
                values = {
                    "transaction_id": txn["transaction_id"],
                    "account_id": txn["account_id"],
                    "item_id": item_id,
                    "date": date.fromisoformat(txn["date"]) if isinstance(txn["date"], str) else txn["date"],
                    "amount": txn["amount"],
                    "iso_currency_code": txn.get("iso_currency_code"),
                    "name": txn["name"],
                    "merchant_name": txn.get("merchant_name"),
                    "pending": txn["pending"],
                    "pending_transaction_id": txn.get("pending_transaction_id"),
                    "pfc_primary": pfc.get("primary"),
                    "pfc_detailed": pfc.get("detailed"),
                    "removed": False,
                    "removed_at": None,
                    "raw_json": txn,
                    "updated_at": captured_at,
                }
                stmt = pg_insert(TransactionRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["transaction_id"], set_={k: v for k, v in values.items() if k != "transaction_id"}
                )
                await session.execute(stmt)

            existing = (
                await session.execute(
                    select(TransactionRow).where(
                        TransactionRow.item_id == item_id,
                        TransactionRow.date >= start_date,
                        TransactionRow.date <= end_date,
                        TransactionRow.removed.is_(False),
                    )
                )
            ).scalars()
            for row in existing:
                if row.transaction_id not in seen:
                    row.removed = True
                    row.removed_at = captured_at
                    row.updated_at = captured_at
            await session.commit()

    async def apply_holdings(
        self, *, item_id: str, securities: list[dict[str, Any]], holdings: list[dict[str, Any]], captured_at: datetime
    ) -> None:
        async with self._session_factory() as session:
            for security in securities:
                values = {
                    "security_id": security["security_id"],
                    "name": security.get("name"),
                    "ticker_symbol": security.get("ticker_symbol"),
                    "type": security.get("type"),
                    "iso_currency_code": security.get("iso_currency_code"),
                    "raw_json": security,
                    "updated_at": captured_at,
                }
                stmt = pg_insert(SecurityRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["security_id"], set_={k: v for k, v in values.items() if k != "security_id"}
                )
                await session.execute(stmt)
            for holding in holdings:
                session.add(
                    HoldingSnapshotRow(
                        account_id=holding["account_id"],
                        security_id=holding["security_id"],
                        item_id=item_id,
                        captured_at=captured_at,
                        quantity=holding.get("quantity"),
                        cost_basis=holding.get("cost_basis"),
                        institution_price=holding.get("institution_price"),
                        institution_value=holding.get("institution_value"),
                        iso_currency_code=holding.get("iso_currency_code"),
                        raw_json=holding,
                    )
                )
            await session.commit()

    async def upsert_investment_transactions(
        self, *, item_id: str, transactions: list[dict[str, Any]], captured_at: datetime
    ) -> None:
        async with self._session_factory() as session:
            for txn in transactions:
                txn_date = txn["date"]
                values = {
                    "investment_transaction_id": txn["investment_transaction_id"],
                    "account_id": txn["account_id"],
                    "security_id": txn.get("security_id"),
                    "item_id": item_id,
                    "date": date.fromisoformat(txn_date) if isinstance(txn_date, str) else txn_date,
                    "amount": txn.get("amount"),
                    "quantity": txn.get("quantity"),
                    "price": txn.get("price"),
                    "fees": txn.get("fees"),
                    "type": txn.get("type"),
                    "subtype": txn.get("subtype"),
                    "iso_currency_code": txn.get("iso_currency_code"),
                    "removed": False,
                    "removed_at": None,
                    "raw_json": txn,
                    "updated_at": captured_at,
                }
                stmt = pg_insert(InvestmentTransactionRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["investment_transaction_id"],
                    set_={k: v for k, v in values.items() if k != "investment_transaction_id"},
                )
                await session.execute(stmt)
            await session.commit()

    async def append_liability_snapshots(
        self, *, item_id: str, liabilities: dict[str, list[dict[str, Any]] | None], captured_at: datetime
    ) -> None:
        row_by_type = {
            "credit": LiabilityCreditSnapshotRow,
            "mortgage": LiabilityMortgageSnapshotRow,
            "student": LiabilityStudentSnapshotRow,
        }
        async with self._session_factory() as session:
            for key, row_type in row_by_type.items():
                for entry in liabilities.get(key) or []:
                    session.add(
                        row_type(
                            account_id=entry["account_id"], item_id=item_id, captured_at=captured_at, raw_json=entry
                        )
                    )
            await session.commit()


def _stored_link(
    row: LinkRow,
    *,
    earliest_transaction_date: date | None = None,
    latest_transaction_date: date | None = None,
    synced_transaction_count: int = 0,
) -> StoredLink:
    return StoredLink(
        item_id=row.item_id,
        label=row.label,
        institution_id=row.institution_id,
        institution_name=row.institution_name,
        products_requested=list(row.products_requested),
        transaction_days_requested=row.transaction_days_requested,
        products_authorized=list(row.products_authorized),
        products_billed=list(row.products_billed),
        status=row.status,
        access_token_secret=row.access_token_secret,
        last_synced_at=row.last_synced_at,
        earliest_transaction_date=earliest_transaction_date,
        latest_transaction_date=latest_transaction_date,
        synced_transaction_count=synced_transaction_count,
    )


def _merge_products(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for product in group:
            if product not in merged:
                merged.append(product)
    return merged
