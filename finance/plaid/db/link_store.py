"""Postgres storage for the Plaid self-contained link service."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from pydantic import BaseModel
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from finance.plaid.db.models import (
    PlaidAccount,
    PlaidBalance,
    PlaidHolding,
    PlaidInvestmentTransaction,
    PlaidLiabilities,
    PlaidLiabilityEntry,
    PlaidRemovedTransaction,
    PlaidSecurity,
    PlaidTransaction,
)
from finance.plaid.db.schema import (
    AccountRow,
    BalanceSnapshotRow,
    HoldingSnapshotRow,
    InvestmentTransactionRow,
    ItemSyncQueueRow,
    LiabilityCreditSnapshotRow,
    LiabilityMortgageSnapshotRow,
    LiabilityStudentSnapshotRow,
    LinkRow,
    PlaidApiEventRow,
    PlaidWebhookDeliveryRow,
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


class SyncAlreadyRunningError(RuntimeError):
    """A sync is already in flight for this Item.

    A distinct type rather than a message the caller matches on: the web app answers 409 for this
    and only this, and a reworded string must not silently turn that into a 500.
    """

    def __init__(self, item_id: str) -> None:
        self.item_id = item_id
        super().__init__(f"sync already running for Plaid item {item_id}")


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
    transactions_cursor: str | None = None
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


@dataclass(frozen=True)
class ItemSyncClaim:
    item_id: str
    generation: int
    claimed_at: datetime
    full_sync: bool


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
            await session.execute(delete(ItemSyncQueueRow).where(ItemSyncQueueRow.item_id == item_id))
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

    async def running_sync_item_ids(self) -> set[str]:
        """Items with a sync in flight. The UI disables their sync button rather than letting a
        click become a guaranteed SyncAlreadyRunningError."""
        async with self._session_factory() as session:
            rows = await session.execute(
                select(SyncRunRow.item_id).where(SyncRunRow.status == "running", SyncRunRow.item_id.isnot(None))
            )
            return {item_id for (item_id,) in rows if item_id is not None}

    async def begin_sync_run(self, *, trigger: str, item_id: str | None, configured_windows: dict[str, Any]) -> UUID:
        run_id = uuid4()
        started_at = utcnow()
        async with self._session_factory() as session:
            if item_id is not None:
                link = await session.scalar(select(LinkRow.item_id).where(LinkRow.item_id == item_id).with_for_update())
                if link is None:
                    raise ValueError(f"cannot start sync for missing Plaid link: {item_id}")
                running = list(
                    (
                        await session.execute(
                            select(SyncRunRow)
                            .where(SyncRunRow.item_id == item_id, SyncRunRow.status == "running")
                            .with_for_update()
                        )
                    ).scalars()
                )
                stale_before = started_at - timedelta(hours=2)
                for row in running:
                    if row.started_at < stale_before:
                        row.status = "failed"
                        row.finished_at = started_at
                        row.error_summary = "sync exceeded the two-hour recovery lease"
                if any(row.started_at >= stale_before for row in running):
                    if any(row.status == "failed" for row in running):
                        await session.commit()
                    raise SyncAlreadyRunningError(item_id)
            session.add(
                SyncRunRow(
                    run_id=run_id,
                    trigger=trigger,
                    mode="v1_cursor_transactions",
                    item_id=item_id,
                    configured_windows=configured_windows,
                    status="running",
                    started_at=started_at,
                )
            )
            await session.commit()
        return run_id

    async def enqueue_item_sync(self, item_id: str, *, full_sync: bool = False) -> None:
        now = utcnow()
        statement = pg_insert(ItemSyncQueueRow).values(
            item_id=item_id,
            generation=1,
            requested_at=now,
            claimed_at=None,
            attempts=0,
            retry_after=None,
            full_sync=full_sync,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["item_id"],
            set_={
                "generation": ItemSyncQueueRow.generation + 1,
                "requested_at": now,
                "attempts": 0,
                "retry_after": None,
                "full_sync": or_(ItemSyncQueueRow.full_sync, statement.excluded.full_sync),
            },
        )
        async with self._session_factory() as session:
            await session.execute(statement)
            await session.commit()

    async def record_plaid_webhook_delivery(self, raw_body: str) -> int:
        """Persist the complete body of a signature-verified Plaid delivery before dispatch."""
        async with self._session_factory() as session:
            row = PlaidWebhookDeliveryRow(raw_body=raw_body, received_at=utcnow(), disposition="received")
            session.add(row)
            await session.flush()
            delivery_id = row.id
            await session.commit()
            return delivery_id

    async def update_plaid_webhook_delivery(
        self,
        delivery_id: int,
        *,
        webhook_type: str | None,
        webhook_code: str | None,
        item_id: str | None,
        disposition: str,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(PlaidWebhookDeliveryRow, delivery_id)
            if row is None:
                raise ValueError(f"Plaid webhook delivery not found: {delivery_id}")
            row.webhook_type = webhook_type
            row.webhook_code = webhook_code
            row.item_id = item_id
            row.disposition = disposition
            await session.commit()

    async def claim_item_sync(self) -> ItemSyncClaim | None:
        now = utcnow()
        stale_before = now - timedelta(minutes=30)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ItemSyncQueueRow)
                    .where(
                        or_(ItemSyncQueueRow.claimed_at.is_(None), ItemSyncQueueRow.claimed_at < stale_before),
                        or_(ItemSyncQueueRow.retry_after.is_(None), ItemSyncQueueRow.retry_after <= now),
                    )
                    .order_by(ItemSyncQueueRow.requested_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            row.claimed_at = now
            claim = ItemSyncClaim(
                item_id=row.item_id, generation=row.generation, claimed_at=now, full_sync=row.full_sync
            )
            await session.commit()
            return claim

    async def finish_item_sync(self, claim: ItemSyncClaim) -> None:
        async with self._session_factory() as session:
            row = await session.get(ItemSyncQueueRow, claim.item_id, with_for_update=True)
            if row is not None and row.claimed_at == claim.claimed_at:
                if row.generation == claim.generation:
                    await session.delete(row)
                else:
                    row.claimed_at = None
                    row.attempts = 0
                    row.retry_after = None
            await session.commit()

    async def retry_item_sync(self, claim: ItemSyncClaim) -> None:
        async with self._session_factory() as session:
            row = await session.get(ItemSyncQueueRow, claim.item_id)
            if row is None or row.claimed_at != claim.claimed_at:
                return
            row.claimed_at = None
            if row.generation != claim.generation:
                row.attempts = 0
                row.retry_after = None
            else:
                row.attempts += 1
                row.retry_after = utcnow() + timedelta(seconds=min(60 * 2 ** min(row.attempts, 6), 3600))
            await session.commit()

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

    async def apply_accounts(
        self, *, item_id: str, accounts: Sequence[PlaidAccount | dict[str, Any]], captured_at: datetime
    ) -> None:
        async with self._session_factory() as session:
            for account_value in accounts:
                account = _validated_payload(PlaidAccount, account_value)
                balances = account.balances or PlaidBalance()
                values = {
                    "account_id": account.account_id,
                    "item_id": item_id,
                    "name": account.name,
                    "official_name": account.official_name,
                    "mask": account.mask,
                    "type": account.type,
                    "subtype": account.subtype,
                    "iso_currency_code": balances.iso_currency_code,
                    "raw_json": account.model_dump(mode="json", exclude_unset=True),
                    "updated_at": captured_at,
                }
                stmt = pg_insert(AccountRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["account_id"], set_={k: v for k, v in values.items() if k != "account_id"}
                )
                await session.execute(stmt)
                session.add(
                    BalanceSnapshotRow(
                        account_id=account.account_id,
                        item_id=item_id,
                        captured_at=captured_at,
                        available=balances.available,
                        current=balances.current,
                        limit=balances.limit,
                        iso_currency_code=balances.iso_currency_code,
                    )
                )
            await session.commit()

    async def apply_transaction_delta(
        self,
        *,
        item_id: str,
        added: Sequence[PlaidTransaction | dict[str, Any]],
        modified: Sequence[PlaidTransaction | dict[str, Any]],
        removed: Sequence[PlaidRemovedTransaction | dict[str, Any]],
        next_cursor: str | None,
        captured_at: datetime,
    ) -> None:
        async with self._session_factory() as session:
            for transaction_value in [*added, *modified]:
                txn = _validated_payload(PlaidTransaction, transaction_value)
                pfc = txn.personal_finance_category
                values = {
                    "transaction_id": txn.transaction_id,
                    "account_id": txn.account_id,
                    "item_id": item_id,
                    "date": txn.date,
                    "amount": txn.amount,
                    "iso_currency_code": txn.iso_currency_code,
                    "name": txn.name,
                    "merchant_name": txn.merchant_name,
                    "pending": txn.pending,
                    "pending_transaction_id": txn.pending_transaction_id,
                    "pfc_primary": pfc.primary if pfc is not None else None,
                    "pfc_detailed": pfc.detailed if pfc is not None else None,
                    "removed": False,
                    "removed_at": None,
                    "raw_json": txn.model_dump(mode="json", exclude_unset=True),
                    "updated_at": captured_at,
                }
                statement = pg_insert(TransactionRow).values(**values)
                statement = statement.on_conflict_do_update(
                    index_elements=["transaction_id"],
                    set_={key: value for key, value in values.items() if key != "transaction_id"},
                )
                await session.execute(statement)

            for removed_value in removed:
                removed_txn = _validated_payload(PlaidRemovedTransaction, removed_value)
                await session.execute(
                    update(TransactionRow)
                    .where(
                        TransactionRow.item_id == item_id, TransactionRow.transaction_id == removed_txn.transaction_id
                    )
                    .values(removed=True, removed_at=captured_at, updated_at=captured_at)
                )

            link = await session.get(LinkRow, item_id)
            if link is None:
                raise ValueError(f"Plaid link disappeared during transaction sync: {item_id}")
            link.transactions_cursor = next_cursor
            await session.commit()

    async def apply_holdings(
        self,
        *,
        item_id: str,
        securities: Sequence[PlaidSecurity | dict[str, Any]],
        holdings: Sequence[PlaidHolding | dict[str, Any]],
        captured_at: datetime,
    ) -> None:
        async with self._session_factory() as session:
            for security_value in securities:
                security = _validated_payload(PlaidSecurity, security_value)
                values = {
                    "security_id": security.security_id,
                    "name": security.name,
                    "ticker_symbol": security.ticker_symbol,
                    "type": security.type,
                    "iso_currency_code": security.iso_currency_code,
                    "raw_json": security.model_dump(mode="json", exclude_unset=True),
                    "updated_at": captured_at,
                }
                stmt = pg_insert(SecurityRow).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["security_id"], set_={k: v for k, v in values.items() if k != "security_id"}
                )
                await session.execute(stmt)
            for holding_value in holdings:
                holding = _validated_payload(PlaidHolding, holding_value)
                session.add(
                    HoldingSnapshotRow(
                        account_id=holding.account_id,
                        security_id=holding.security_id,
                        item_id=item_id,
                        captured_at=captured_at,
                        quantity=holding.quantity,
                        cost_basis=holding.cost_basis,
                        institution_price=holding.institution_price,
                        institution_value=holding.institution_value,
                        iso_currency_code=holding.iso_currency_code,
                        raw_json=holding.model_dump(mode="json", exclude_unset=True),
                    )
                )
            await session.commit()

    async def upsert_investment_transactions(
        self,
        *,
        item_id: str,
        transactions: Sequence[PlaidInvestmentTransaction | dict[str, Any]],
        captured_at: datetime,
    ) -> None:
        async with self._session_factory() as session:
            for txn_value in transactions:
                txn = _validated_payload(PlaidInvestmentTransaction, txn_value)
                values = {
                    "investment_transaction_id": txn.investment_transaction_id,
                    "account_id": txn.account_id,
                    "security_id": txn.security_id,
                    "item_id": item_id,
                    "date": txn.date,
                    "amount": txn.amount,
                    "quantity": txn.quantity,
                    "price": txn.price,
                    "fees": txn.fees,
                    "type": txn.type,
                    "subtype": txn.subtype,
                    "iso_currency_code": txn.iso_currency_code,
                    "removed": False,
                    "removed_at": None,
                    "raw_json": txn.model_dump(mode="json", exclude_unset=True),
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
        self, *, item_id: str, liabilities: PlaidLiabilities | dict[str, Any], captured_at: datetime
    ) -> None:
        row_by_type = {
            "credit": LiabilityCreditSnapshotRow,
            "mortgage": LiabilityMortgageSnapshotRow,
            "student": LiabilityStudentSnapshotRow,
        }
        payload = _validated_payload(PlaidLiabilities, liabilities)
        async with self._session_factory() as session:
            for key, row_type in row_by_type.items():
                for entry_value in getattr(payload, key) or []:
                    entry = _validated_payload(PlaidLiabilityEntry, entry_value)
                    session.add(
                        row_type(
                            account_id=entry.account_id,
                            item_id=item_id,
                            captured_at=captured_at,
                            raw_json=entry.model_dump(mode="json", exclude_unset=True),
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
        transactions_cursor=row.transactions_cursor,
        last_synced_at=row.last_synced_at,
        earliest_transaction_date=earliest_transaction_date,
        latest_transaction_date=latest_transaction_date,
        synced_transaction_count=synced_transaction_count,
    )


def _validated_payload[PayloadT: BaseModel](model_type: type[PayloadT], value: PayloadT | dict[str, Any]) -> PayloadT:
    return cast(PayloadT, model_type.model_validate(value))


def _merge_products(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for product in group:
            if product not in merged:
                merged.append(product)
    return merged
