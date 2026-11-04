"""Postgres storage for the Plaid self-contained link service."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from pydantic import BaseModel
from sqlalchemy import Date as SQLDate, and_, delete, exists, func, literal, or_, select, text, union_all, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from finance.plaid.db.models import (
    AccountRow,
    BalanceSnapshotRow,
    HoldingCaptureAccountRow,
    HoldingCaptureRow,
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
from finance.plaid.models import AccountDeletionPreview, AccountInventory, ItemInventory
from finance.plaid.payloads import (
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

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_SPEND_CHANGED_CHANNEL = "plaid_spend_changed"


async def _notify_spend_changed(session: AsyncSession) -> None:
    """Queue an empty, transaction-scoped wake-up; this is not a durable change stream."""
    await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": _SPEND_CHANGED_CHANNEL})


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


class SyncLeaseLostError(RuntimeError):
    """The run lost ownership while waiting for Plaid or the database."""


class AccountNotFoundError(LookupError):
    """The selected account is not part of the selected Plaid Item."""


class AccountNotDetachedError(RuntimeError):
    """The last complete account listing does not confirm that this account is detached."""


class AccountDeletionPreviewChangedError(RuntimeError):
    """The account or its mirrored data changed since deletion was previewed."""


class ItemSyncQueuedError(RuntimeError):
    """An Item sync is queued and can start after the current request."""


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


@dataclass
class _ItemInventoryAccumulator:
    link: LinkRow
    latest_sync_status: str | None
    latest_sync_started_at: datetime | None
    latest_sync_finished_at: datetime | None
    accounts: list[AccountInventory] = field(default_factory=list)


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
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended('finance.plaid.db:migrations', 0))")
                )
                await conn.run_sync(_run_alembic_migrations)
        except BaseException:
            await engine.dispose()
            raise
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
        run_id: UUID | None,
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
            if run_id is not None:
                await _require_active_sync_run(session, item_id=item_id, run_id=run_id)
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
        return _stored_link(row)

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
            link_exists = bool(await session.scalar(select(exists().where(LinkRow.item_id == item_id))))
            for row_type in (
                LiabilityCreditSnapshotRow,
                LiabilityMortgageSnapshotRow,
                LiabilityStudentSnapshotRow,
                HoldingSnapshotRow,
                InvestmentTransactionRow,
                TransactionRow,
                BalanceSnapshotRow,
            ):
                await session.execute(delete(row_type).where(row_type.item_id == item_id))
            capture_ids = select(HoldingCaptureRow.id).where(HoldingCaptureRow.item_id == item_id)
            await session.execute(
                delete(HoldingCaptureAccountRow).where(HoldingCaptureAccountRow.capture_id.in_(capture_ids))
            )
            await session.execute(delete(HoldingCaptureRow).where(HoldingCaptureRow.item_id == item_id))
            await session.execute(delete(AccountRow).where(AccountRow.item_id == item_id))
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
            if link_exists:
                await _notify_spend_changed(session)
            await session.commit()

    async def mark_link_update_succeeded(self, *, item_id: str, products_requested: list[str]) -> StoredLink | None:
        async with self._session_factory() as session:
            row = await session.get(LinkRow, item_id)
            if row is None:
                return None
            changed = (
                row.status != "active"
                or list(row.products_requested) != products_requested
                or list(row.products_authorized) != _merge_products(list(row.products_authorized), products_requested)
            )
            row.products_requested = products_requested
            row.products_authorized = _merge_products(list(row.products_authorized), products_requested)
            row.status = "active"
            row.updated_at = utcnow()
            if changed:
                await _notify_spend_changed(session)
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

    async def list_account_inventory(self) -> list[ItemInventory]:
        activity = union_all(
            select(
                TransactionRow.account_id.label("account_id"),
                TransactionRow.date.label("activity_date"),
                literal(False).label("is_investment"),
            ).where(TransactionRow.removed.is_(False)),
            select(
                InvestmentTransactionRow.account_id.label("account_id"),
                InvestmentTransactionRow.date.label("activity_date"),
                literal(True).label("is_investment"),
            ).where(InvestmentTransactionRow.removed.is_(False)),
        ).subquery()
        activity_stats = (
            select(
                activity.c.account_id,
                func.count().filter(activity.c.is_investment.is_(False)).label("transaction_count"),
                func.count().filter(activity.c.is_investment.is_(True)).label("investment_transaction_count"),
                func.min(activity.c.activity_date).label("earliest_transaction_date"),
                func.max(activity.c.activity_date).label("latest_transaction_date"),
            )
            .group_by(activity.c.account_id)
            .subquery()
        )
        latest_balances = select(
            BalanceSnapshotRow.id.label("balance_id"),
            BalanceSnapshotRow.account_id.label("account_id"),
            func.row_number()
            .over(
                partition_by=BalanceSnapshotRow.account_id,
                order_by=(BalanceSnapshotRow.captured_at.desc(), BalanceSnapshotRow.id.desc()),
            )
            .label("row_number"),
        ).subquery()
        latest_syncs = (
            select(
                SyncRunRow.item_id.label("item_id"),
                SyncRunRow.status.label("sync_status"),
                SyncRunRow.started_at.label("sync_started_at"),
                SyncRunRow.finished_at.label("sync_finished_at"),
                func.row_number()
                .over(partition_by=SyncRunRow.item_id, order_by=SyncRunRow.started_at.desc())
                .label("row_number"),
            )
            .where(SyncRunRow.item_id.isnot(None))
            .subquery()
        )
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        LinkRow,
                        AccountRow,
                        BalanceSnapshotRow,
                        activity_stats.c.transaction_count,
                        activity_stats.c.investment_transaction_count,
                        activity_stats.c.earliest_transaction_date,
                        activity_stats.c.latest_transaction_date,
                        latest_syncs.c.sync_status,
                        latest_syncs.c.sync_started_at,
                        latest_syncs.c.sync_finished_at,
                    )
                    .select_from(LinkRow)
                    .outerjoin(AccountRow, AccountRow.item_id == LinkRow.item_id)
                    .outerjoin(activity_stats, activity_stats.c.account_id == AccountRow.account_id)
                    .outerjoin(
                        latest_balances,
                        and_(latest_balances.c.account_id == AccountRow.account_id, latest_balances.c.row_number == 1),
                    )
                    .outerjoin(BalanceSnapshotRow, BalanceSnapshotRow.id == latest_balances.c.balance_id)
                    .outerjoin(
                        latest_syncs, and_(latest_syncs.c.item_id == LinkRow.item_id, latest_syncs.c.row_number == 1)
                    )
                    .where(LinkRow.status != "revoked")
                    .order_by(LinkRow.institution_name, LinkRow.item_id, AccountRow.name, AccountRow.account_id)
                )
            ).all()

        grouped: dict[str, _ItemInventoryAccumulator] = {}
        for (
            link,
            account,
            balance,
            transaction_count,
            investment_transaction_count,
            earliest_transaction_date,
            latest_transaction_date,
            latest_sync_status,
            latest_sync_started_at,
            latest_sync_finished_at,
        ) in rows:
            if link.item_id not in grouped:
                grouped[link.item_id] = _ItemInventoryAccumulator(
                    link, latest_sync_status, latest_sync_started_at, latest_sync_finished_at
                )
            if account is None:
                continue
            grouped[link.item_id].accounts.append(
                AccountInventory(
                    account_id=account.account_id,
                    name=account.name,
                    official_name=account.official_name,
                    mask=account.mask,
                    type=account.type,
                    subtype=account.subtype,
                    iso_currency_code=account.iso_currency_code,
                    last_seen_at=account.updated_at,
                    detached_at=account.detached_at,
                    balance_captured_at=balance.captured_at if balance is not None else None,
                    balance_available=balance.available if balance is not None else None,
                    balance_current=balance.current if balance is not None else None,
                    balance_limit=balance.limit if balance is not None else None,
                    balance_iso_currency_code=balance.iso_currency_code if balance is not None else None,
                    transaction_count=transaction_count or 0,
                    investment_transaction_count=investment_transaction_count or 0,
                    earliest_transaction_date=earliest_transaction_date,
                    latest_transaction_date=latest_transaction_date,
                )
            )
        return [
            ItemInventory(
                item_id=item.link.item_id,
                label=item.link.label,
                institution_name=item.link.institution_name,
                status=item.link.status,
                accounts_observed_at=item.link.accounts_observed_at,
                last_successful_sync_at=item.link.last_synced_at,
                latest_sync_status=item.latest_sync_status,
                latest_sync_started_at=item.latest_sync_started_at,
                latest_sync_finished_at=item.latest_sync_finished_at,
                accounts=tuple(item.accounts),
            )
            for item in grouped.values()
        ]

    async def get_account_deletion_preview(self, *, item_id: str, account_id: str) -> AccountDeletionPreview:
        async with self._session_factory() as session:
            link, account = await _account_deletion_rows(session, item_id=item_id, account_id=account_id)
            _require_detached_account(link, account)
            if await _has_running_sync(session, item_id):
                raise SyncAlreadyRunningError(item_id)
            if await _has_queued_sync(session, item_id):
                raise ItemSyncQueuedError(item_id)
            return await _build_account_deletion_preview(session, link=link, account=account)

    async def delete_detached_account(
        self, *, item_id: str, account_id: str, expected_preview: AccountDeletionPreview
    ) -> None:
        if expected_preview.item_id != item_id or expected_preview.account.account_id != account_id:
            raise AccountDeletionPreviewChangedError

        async with self._session_factory() as session:
            link = await session.scalar(select(LinkRow).where(LinkRow.item_id == item_id).with_for_update())
            if link is None or link.status == "revoked":
                raise AccountNotFoundError(account_id)
            account = await session.scalar(
                select(AccountRow)
                .where(AccountRow.account_id == account_id, AccountRow.item_id == item_id)
                .with_for_update()
            )
            if account is None:
                raise AccountNotFoundError(account_id)
            _require_detached_account(link, account)
            if await _has_running_sync(session, item_id):
                raise SyncAlreadyRunningError(item_id)
            if await _has_queued_sync(session, item_id):
                raise ItemSyncQueuedError(item_id)

            current_preview = await _build_account_deletion_preview(session, link=link, account=account)
            if current_preview != expected_preview:
                raise AccountDeletionPreviewChangedError

            for row_type in (
                LiabilityCreditSnapshotRow,
                LiabilityMortgageSnapshotRow,
                LiabilityStudentSnapshotRow,
                HoldingSnapshotRow,
                InvestmentTransactionRow,
                TransactionRow,
                BalanceSnapshotRow,
            ):
                await session.execute(delete(row_type).where(row_type.account_id == account_id))
            await session.execute(
                delete(HoldingCaptureAccountRow).where(
                    HoldingCaptureAccountRow.item_id == item_id, HoldingCaptureAccountRow.account_id == account_id
                )
            )
            await session.execute(
                delete(AccountRow).where(AccountRow.account_id == account_id, AccountRow.item_id == item_id)
            )
            await _notify_spend_changed(session)
            await session.commit()

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
            await session.scalar(select(LinkRow.item_id).where(LinkRow.item_id == item_id).with_for_update())
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
            row = await session.get(ItemSyncQueueRow, claim.item_id, with_for_update=True)
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
            item_id = await session.scalar(select(SyncRunRow.item_id).where(SyncRunRow.run_id == run_id))
            if item_id is not None:
                await _require_active_sync_run(session, item_id=item_id, run_id=run_id)
            row = await session.get(SyncRunRow, run_id, with_for_update=True)
            if row is None:
                raise ValueError(f"sync run not found: {run_id}")
            if row.status != "running":
                raise SyncLeaseLostError(f"sync run is no longer active: {run_id}")
            finished_at = utcnow()
            row.status = status
            row.finished_at = finished_at
            row.error_summary = error_summary
            if status == "succeeded" and row.item_id is not None:
                link = await session.get(LinkRow, row.item_id)
                if link is not None:
                    link.last_synced_at = finished_at
                    link.updated_at = finished_at
            if status == "succeeded":
                await _notify_spend_changed(session)
            await session.commit()

    async def record_api_event(self, event: ApiEvent) -> int:
        async with self._session_factory() as session:
            superseded = False
            if event.sync_run_id is not None:
                item_id = await session.scalar(select(SyncRunRow.item_id).where(SyncRunRow.run_id == event.sync_run_id))
                if item_id != event.item_id:
                    raise ValueError(f"API event Item does not match sync run: {event.sync_run_id}")
                link = await session.get(LinkRow, item_id, with_for_update=True) if item_id is not None else None
                run = await session.get(SyncRunRow, event.sync_run_id, with_for_update=True)
                if run is None:
                    raise ValueError(f"sync run not found: {event.sync_run_id}")
                superseded = link is None or run.status != "running"
            # Keep Plaid's outcome while making a late receipt ineligible for mirror projections.
            row = PlaidApiEventRow(
                sync_run_id=event.sync_run_id,
                endpoint=event.endpoint,
                item_id=event.item_id,
                account_id=event.account_id,
                request_id=event.request_id,
                status=f"{event.status}_superseded" if superseded else event.status,
                duration_ms=event.duration_ms,
                error_type=event.error_type,
                error_code=event.error_code,
                request_json=event.request_json,
                response_json=event.response_json,
                created_at=utcnow(),
            )
            session.add(row)
            await session.flush()
            event_id = row.id
            await session.commit()
            return event_id

    async def apply_accounts(
        self, *, item_id: str, accounts: Sequence[PlaidAccount | dict[str, Any]], captured_at: datetime, run_id: UUID
    ) -> None:
        validated_accounts = [_validated_payload(PlaidAccount, account) for account in accounts]
        seen_account_ids = [account.account_id for account in validated_accounts]
        async with self._session_factory() as session:
            await _require_active_sync_run(session, item_id=item_id, run_id=run_id)
            link = await session.get(LinkRow, item_id)
            if link is None:
                raise ValueError(f"cannot observe accounts for missing Plaid Item: {item_id}")
            for account in validated_accounts:
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
                    "detached_at": None,
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
            absent_accounts = update(AccountRow).where(AccountRow.item_id == item_id, AccountRow.detached_at.is_(None))
            if seen_account_ids:
                absent_accounts = absent_accounts.where(~AccountRow.account_id.in_(seen_account_ids))
            absent_accounts = absent_accounts.values(detached_at=captured_at)
            await session.execute(absent_accounts)
            link.accounts_observed_at = captured_at
            await _notify_spend_changed(session)
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
        run_id: UUID,
    ) -> None:
        async with self._session_factory() as session:
            await _require_active_sync_run(session, item_id=item_id, run_id=run_id)
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
        accounts: Sequence[PlaidAccount | dict[str, Any]],
        api_event_id: int,
        captured_at: datetime,
        run_id: UUID,
    ) -> None:
        validated_securities = [_validated_payload(PlaidSecurity, security) for security in securities]
        validated_holdings = [_validated_payload(PlaidHolding, holding) for holding in holdings]
        validated_accounts = [_validated_payload(PlaidAccount, account) for account in accounts]
        account_ids = [account.account_id for account in validated_accounts]
        security_ids = [security.security_id for security in validated_securities]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("investments/holdings/get response contains duplicate accounts")
        if len(security_ids) != len(set(security_ids)):
            raise ValueError("investments/holdings/get response contains duplicate securities")
        if missing_security_ids := {holding.security_id for holding in validated_holdings} - set(security_ids):
            raise ValueError(f"holdings reference securities absent from the response: {sorted(missing_security_ids)}")
        if uncovered_account_ids := {holding.account_id for holding in validated_holdings} - set(account_ids):
            raise ValueError(f"holdings reference accounts absent from the response: {sorted(uncovered_account_ids)}")

        async with self._session_factory() as session:
            await _require_active_sync_run(session, item_id=item_id, run_id=run_id)
            link = await session.get(LinkRow, item_id)
            if link is None:
                raise ValueError(f"cannot capture holdings for missing Plaid Item: {item_id}")
            api_event = await session.get(PlaidApiEventRow, api_event_id)
            if (
                api_event is None
                or api_event.item_id != item_id
                or api_event.endpoint != "investments/holdings/get"
                or api_event.status != "ok"
            ):
                raise ValueError(f"API event {api_event_id} is not a successful holdings response for Item {item_id}")

            if account_ids:
                account_rows = (
                    await session.execute(
                        select(AccountRow.account_id, AccountRow.item_id).where(AccountRow.account_id.in_(account_ids))
                    )
                ).all()
                owned_accounts: dict[str, str] = {cast(str, row[0]): cast(str, row[1]) for row in account_rows}
                if any(owned_accounts.get(account_id) != item_id for account_id in account_ids):
                    raise ValueError(f"holdings response contains accounts not owned by Item {item_id}")

            for security in validated_securities:
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

            capture = HoldingCaptureRow(item_id=item_id, captured_at=captured_at, api_event_id=api_event_id)
            session.add(capture)
            await session.flush()
            for account in validated_accounts:
                balances = account.balances
                session.add(
                    HoldingCaptureAccountRow(
                        capture_id=capture.id,
                        account_id=account.account_id,
                        item_id=item_id,
                        current=balances.current if balances is not None else None,
                        iso_currency_code=balances.iso_currency_code if balances is not None else None,
                    )
                )
            for holding in validated_holdings:
                session.add(
                    HoldingSnapshotRow(
                        capture_id=capture.id,
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
            await _notify_spend_changed(session)
            await session.commit()

    async def upsert_investment_transactions(
        self,
        *,
        item_id: str,
        transactions: Sequence[PlaidInvestmentTransaction | dict[str, Any]],
        captured_at: datetime,
        run_id: UUID,
    ) -> None:
        async with self._session_factory() as session:
            await _require_active_sync_run(session, item_id=item_id, run_id=run_id)
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
        self, *, item_id: str, liabilities: PlaidLiabilities | dict[str, Any], captured_at: datetime, run_id: UUID
    ) -> None:
        row_by_type = {
            "credit": LiabilityCreditSnapshotRow,
            "mortgage": LiabilityMortgageSnapshotRow,
            "student": LiabilityStudentSnapshotRow,
        }
        payload = _validated_payload(PlaidLiabilities, liabilities)
        async with self._session_factory() as session:
            await _require_active_sync_run(session, item_id=item_id, run_id=run_id)
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


async def _require_active_sync_run(session: AsyncSession, *, item_id: str, run_id: UUID) -> None:
    # Recovery takes this link lock too. A write commits before takeover or observes
    # the recovered run's failed status; no lock spans a Plaid request.
    link = await session.get(LinkRow, item_id, with_for_update=True)
    if link is None:
        raise SyncLeaseLostError(f"Plaid link is missing for sync run: {item_id}")
    row = await session.get(SyncRunRow, run_id, with_for_update=True)
    if row is None or row.item_id != item_id or row.status != "running":
        raise SyncLeaseLostError(f"sync run is no longer active: {run_id}")


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


async def _account_deletion_rows(session: AsyncSession, *, item_id: str, account_id: str) -> tuple[LinkRow, AccountRow]:
    row = (
        await session.execute(
            select(LinkRow, AccountRow)
            .join(AccountRow, AccountRow.item_id == LinkRow.item_id)
            .where(LinkRow.item_id == item_id, AccountRow.account_id == account_id)
        )
    ).one_or_none()
    if row is None or row[0].status == "revoked":
        raise AccountNotFoundError(account_id)
    return row[0], row[1]


def _require_detached_account(link: LinkRow, account: AccountRow) -> None:
    if (
        link.accounts_observed_at is None
        or account.detached_at is None
        or account.detached_at > link.accounts_observed_at
    ):
        raise AccountNotDetachedError(account.account_id)


async def _has_running_sync(session: AsyncSession, item_id: str) -> bool:
    return bool(
        await session.scalar(select(exists().where(SyncRunRow.item_id == item_id, SyncRunRow.status == "running")))
    )


async def _has_queued_sync(session: AsyncSession, item_id: str) -> bool:
    return bool(await session.scalar(select(exists().where(ItemSyncQueueRow.item_id == item_id))))


async def _build_account_deletion_preview(
    session: AsyncSession, *, link: LinkRow, account: AccountRow
) -> AccountDeletionPreview:
    records = union_all(
        select(
            literal("transaction").label("record_type"),
            TransactionRow.date.label("history_date"),
            TransactionRow.removed.label("removed"),
        ).where(TransactionRow.account_id == account.account_id),
        select(
            literal("investment_transaction").label("record_type"),
            InvestmentTransactionRow.date.label("history_date"),
            InvestmentTransactionRow.removed.label("removed"),
        ).where(InvestmentTransactionRow.account_id == account.account_id),
        select(
            literal("balance").label("record_type"),
            literal(None, type_=SQLDate).label("history_date"),
            literal(False).label("removed"),
        ).where(BalanceSnapshotRow.account_id == account.account_id),
        select(
            literal("holding").label("record_type"),
            literal(None, type_=SQLDate).label("history_date"),
            literal(False).label("removed"),
        ).where(HoldingSnapshotRow.account_id == account.account_id),
        select(
            literal("liability_credit").label("record_type"),
            literal(None, type_=SQLDate).label("history_date"),
            literal(False).label("removed"),
        ).where(LiabilityCreditSnapshotRow.account_id == account.account_id),
        select(
            literal("liability_mortgage").label("record_type"),
            literal(None, type_=SQLDate).label("history_date"),
            literal(False).label("removed"),
        ).where(LiabilityMortgageSnapshotRow.account_id == account.account_id),
        select(
            literal("liability_student").label("record_type"),
            literal(None, type_=SQLDate).label("history_date"),
            literal(False).label("removed"),
        ).where(LiabilityStudentSnapshotRow.account_id == account.account_id),
    ).subquery()
    transaction_history = records.c.record_type.in_(("transaction", "investment_transaction"))
    stats = (
        (
            await session.execute(
                select(
                    func.count().filter(records.c.record_type == "transaction").label("transaction_records"),
                    func.count()
                    .filter(records.c.record_type == "investment_transaction")
                    .label("investment_transaction_records"),
                    func.count().filter(records.c.record_type == "balance").label("balance_snapshot_records"),
                    func.count().filter(records.c.record_type == "holding").label("holding_snapshot_records"),
                    func.count()
                    .filter(records.c.record_type.in_(("liability_credit", "liability_mortgage", "liability_student")))
                    .label("liability_snapshot_records"),
                    func.count()
                    .filter(and_(records.c.record_type == "transaction", records.c.removed.is_(False)))
                    .label("active_transactions"),
                    func.count()
                    .filter(and_(records.c.record_type == "investment_transaction", records.c.removed.is_(False)))
                    .label("active_investment_transactions"),
                    func.min(records.c.history_date)
                    .filter(and_(transaction_history, records.c.removed.is_(False)))
                    .label("active_earliest_history_date"),
                    func.max(records.c.history_date)
                    .filter(and_(transaction_history, records.c.removed.is_(False)))
                    .label("active_latest_history_date"),
                    func.min(records.c.history_date).filter(transaction_history).label("earliest_history_date"),
                    func.max(records.c.history_date).filter(transaction_history).label("latest_history_date"),
                ).select_from(records)
            )
        )
        .mappings()
        .one()
    )
    balance = await session.scalar(
        select(BalanceSnapshotRow)
        .where(BalanceSnapshotRow.account_id == account.account_id)
        .order_by(BalanceSnapshotRow.captured_at.desc(), BalanceSnapshotRow.id.desc())
        .limit(1)
    )
    latest_sync_run_id = await session.scalar(
        select(SyncRunRow.run_id)
        .where(SyncRunRow.item_id == link.item_id)
        .order_by(SyncRunRow.started_at.desc(), SyncRunRow.run_id.desc())
        .limit(1)
    )
    inventory = AccountInventory(
        account_id=account.account_id,
        name=account.name,
        official_name=account.official_name,
        mask=account.mask,
        type=account.type,
        subtype=account.subtype,
        iso_currency_code=account.iso_currency_code,
        last_seen_at=account.updated_at,
        detached_at=account.detached_at,
        balance_captured_at=balance.captured_at if balance is not None else None,
        balance_available=balance.available if balance is not None else None,
        balance_current=balance.current if balance is not None else None,
        balance_limit=balance.limit if balance is not None else None,
        balance_iso_currency_code=balance.iso_currency_code if balance is not None else None,
        transaction_count=stats["active_transactions"],
        investment_transaction_count=stats["active_investment_transactions"],
        earliest_transaction_date=stats["active_earliest_history_date"],
        latest_transaction_date=stats["active_latest_history_date"],
    )
    return AccountDeletionPreview(
        item_id=link.item_id,
        accounts_observed_at=link.accounts_observed_at,
        latest_sync_run_id=latest_sync_run_id,
        account=inventory,
        transaction_records=stats["transaction_records"],
        investment_transaction_records=stats["investment_transaction_records"],
        balance_snapshot_records=stats["balance_snapshot_records"],
        holding_snapshot_records=stats["holding_snapshot_records"],
        liability_snapshot_records=stats["liability_snapshot_records"],
        earliest_history_date=stats["earliest_history_date"],
        latest_history_date=stats["latest_history_date"],
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
