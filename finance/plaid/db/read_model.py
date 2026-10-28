"""Read-only portfolio projections over the synced Plaid mirror database."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.selectable import CTE

from finance.plaid.db.models import (
    AccountRow,
    BalanceSnapshotRow,
    HoldingCaptureAccountRow,
    HoldingCaptureRow,
    HoldingSnapshotRow,
    LinkRow,
    PlaidApiEventRow,
    SecurityRow,
    TransactionRow,
)


def latest_holding_captures(account_ids: Sequence[str]) -> CTE:
    """Return the latest capture row for each Item containing a selected account."""
    selected_items = (
        select(AccountRow.item_id)
        .where(AccountRow.account_id.in_(account_ids))
        .distinct()
        .cte("selected_holding_items")
    )
    ranked = (
        select(
            HoldingCaptureRow.id.label("capture_id"),
            HoldingCaptureRow.item_id,
            HoldingCaptureRow.captured_at,
            HoldingCaptureRow.api_event_id,
            func.row_number()
            .over(
                partition_by=HoldingCaptureRow.item_id,
                order_by=(desc(HoldingCaptureRow.captured_at), desc(HoldingCaptureRow.id)),
            )
            .label("capture_rank"),
        )
        .join(selected_items, selected_items.c.item_id == HoldingCaptureRow.item_id)
        .cte("ranked_holding_captures")
    )
    latest = (
        select(ranked.c.capture_id, ranked.c.item_id, ranked.c.captured_at, ranked.c.api_event_id)
        .where(ranked.c.capture_rank == 1)
        .cte("latest_holding_capture_rows")
    )
    unlinked_rows = (
        select(HoldingSnapshotRow.id)
        .where(
            HoldingSnapshotRow.item_id == latest.c.item_id,
            HoldingSnapshotRow.capture_id.is_(None),
            HoldingSnapshotRow.captured_at >= latest.c.captured_at,
        )
        .correlate(latest)
        .exists()
    )
    unapplied_receipts = (
        select(PlaidApiEventRow.id)
        .where(
            PlaidApiEventRow.item_id == latest.c.item_id,
            PlaidApiEventRow.endpoint == "investments/holdings/get",
            PlaidApiEventRow.status == "ok",
            PlaidApiEventRow.id > latest.c.api_event_id,
        )
        .correlate(latest)
        .exists()
    )
    return select(
        latest.c.capture_id,
        latest.c.item_id,
        latest.c.captured_at,
        latest.c.api_event_id,
        or_(unlinked_rows, unapplied_receipts).label("has_unapplied_observations"),
    ).cte("latest_holding_captures")


@dataclass(frozen=True)
class CurrentCashBalance:
    account_id: str
    account_name: str
    institution_name: str | None
    captured_at: datetime
    available: Decimal | None
    current: Decimal | None
    iso_currency_code: str | None


@dataclass(frozen=True)
class CurrentHolding:
    account_id: str
    account_name: str
    institution_name: str | None
    security_id: str
    security_name: str | None
    ticker_symbol: str | None
    security_type: str | None
    captured_at: datetime
    quantity: Decimal | None
    cost_basis: Decimal | None
    institution_price: Decimal | None
    institution_value: Decimal | None
    iso_currency_code: str | None


async def read_current_cash_balances(
    *, session_factory: async_sessionmaker[AsyncSession], account_ids: tuple[str, ...], iso_currency_code: str = "USD"
) -> tuple[CurrentCashBalance, ...]:
    if not account_ids:
        return ()
    ranked = (
        select(
            BalanceSnapshotRow.id.label("balance_snapshot_id"),
            func.row_number()
            .over(
                partition_by=BalanceSnapshotRow.account_id,
                order_by=(desc(BalanceSnapshotRow.captured_at), desc(BalanceSnapshotRow.id)),
            )
            .label("row_number"),
        )
        .join(AccountRow, AccountRow.account_id == BalanceSnapshotRow.account_id)
        .join(LinkRow, LinkRow.item_id == BalanceSnapshotRow.item_id)
        .where(
            BalanceSnapshotRow.account_id.in_(account_ids),
            LinkRow.status != "revoked",
            AccountRow.detached_at.is_(None),
        )
        .subquery()
    )
    stmt = (
        select(BalanceSnapshotRow, AccountRow, LinkRow)
        .join(ranked, ranked.c.balance_snapshot_id == BalanceSnapshotRow.id)
        .join(AccountRow, AccountRow.account_id == BalanceSnapshotRow.account_id)
        .join(LinkRow, LinkRow.item_id == BalanceSnapshotRow.item_id)
        .where(ranked.c.row_number == 1)
        .order_by(BalanceSnapshotRow.account_id)
    )
    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()
    return tuple(
        CurrentCashBalance(
            account_id=balance.account_id,
            account_name=account.name,
            institution_name=link.institution_name,
            captured_at=balance.captured_at,
            available=balance.available,
            current=balance.current,
            iso_currency_code=balance.iso_currency_code,
        )
        for balance, account, link in rows
        if balance.iso_currency_code in (None, iso_currency_code)
    )


async def read_current_holdings(
    *, session_factory: async_sessionmaker[AsyncSession], account_ids: tuple[str, ...], iso_currency_code: str = "USD"
) -> tuple[CurrentHolding, ...]:
    if not account_ids:
        return ()
    latest_captures = latest_holding_captures(account_ids)
    stmt = (
        select(
            HoldingSnapshotRow,
            AccountRow,
            LinkRow,
            SecurityRow,
            latest_captures.c.capture_id,
            latest_captures.c.captured_at.label("capture_captured_at"),
            HoldingCaptureAccountRow.account_id.label("covered_account_id"),
            latest_captures.c.has_unapplied_observations,
        )
        .select_from(AccountRow)
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .outerjoin(latest_captures, latest_captures.c.item_id == AccountRow.item_id)
        .outerjoin(
            HoldingCaptureAccountRow,
            (HoldingCaptureAccountRow.capture_id == latest_captures.c.capture_id)
            & (HoldingCaptureAccountRow.account_id == AccountRow.account_id),
        )
        .outerjoin(
            HoldingSnapshotRow,
            (HoldingSnapshotRow.capture_id == latest_captures.c.capture_id)
            & (HoldingSnapshotRow.account_id == AccountRow.account_id)
            & (HoldingSnapshotRow.item_id == AccountRow.item_id),
        )
        .outerjoin(SecurityRow, SecurityRow.security_id == HoldingSnapshotRow.security_id)
        .where(AccountRow.account_id.in_(account_ids), LinkRow.status != "revoked", AccountRow.detached_at.is_(None))
        .order_by(AccountRow.account_id, HoldingSnapshotRow.security_id)
    )
    async with session_factory() as session:
        rows = (await session.execute(stmt)).all()
    holdings: list[CurrentHolding] = []
    for holding, account, link, security, capture_id, capture_captured_at, covered_account_id, has_unapplied in rows:
        if capture_id is None or covered_account_id is None:
            raise RuntimeError(f"No latest holding capture covers active Plaid account {account.account_id!r}.")
        if has_unapplied:
            raise RuntimeError(f"Unapplied holding observations make Item {account.item_id!r} unsafe to read.")
        if holding is None:
            continue
        if security is None:
            raise RuntimeError(f"Holding security metadata is missing for {holding.security_id!r}.")
        if holding.iso_currency_code not in (None, iso_currency_code):
            continue
        holdings.append(
            CurrentHolding(
                account_id=holding.account_id,
                account_name=account.name,
                institution_name=link.institution_name,
                security_id=holding.security_id,
                security_name=security.name,
                ticker_symbol=security.ticker_symbol,
                security_type=security.type,
                captured_at=capture_captured_at,
                quantity=holding.quantity,
                cost_basis=holding.cost_basis,
                institution_price=holding.institution_price,
                institution_value=holding.institution_value,
                iso_currency_code=holding.iso_currency_code,
            )
        )
    return tuple(holdings)


async def read_transactions(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    start_date: date,
    end_date: date,
    account_ids: tuple[str, ...] = (),
) -> tuple[TransactionRow, ...]:
    """Posted (non-pending, non-removed) transactions in [start_date, end_date].

    Empty `account_ids` selects every account behind a non-revoked Link -- fine for a
    single-user deployment where the connection only sees that user's data. ORM rows
    are returned directly; AsyncSession uses expire_on_commit=False so callers can
    read attributes after the session closes."""
    stmt = (
        select(TransactionRow)
        .join(LinkRow, LinkRow.item_id == TransactionRow.item_id)
        .where(
            TransactionRow.removed.is_(False),
            TransactionRow.pending.is_(False),
            TransactionRow.date >= start_date,
            TransactionRow.date <= end_date,
            LinkRow.status != "revoked",
        )
        .order_by(TransactionRow.date, TransactionRow.transaction_id)
    )
    if account_ids:
        stmt = stmt.where(TransactionRow.account_id.in_(account_ids))
    async with session_factory() as session:
        return tuple((await session.execute(stmt)).scalars().all())
