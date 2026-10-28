"""Selected, active Plaid account balances and complete holding captures."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, and_, case, func, literal, select

from finance.plaid.db.models import (
    AccountRow,
    BalanceSnapshotRow,
    HoldingCaptureAccountRow,
    HoldingSnapshotRow,
    LinkRow,
    SecurityRow,
)
from finance.plaid.db.read_model import latest_holding_captures


def select_account_balances(account_ids: Sequence[str], *, holding_account_ids: Sequence[str] = ()) -> Select[Any]:
    """Select active balances, using a holding capture's balance where holdings are required."""
    accounts = (
        select(
            AccountRow.account_id,
            AccountRow.item_id,
            AccountRow.type,
            AccountRow.iso_currency_code.label("account_currency"),
        )
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .where(AccountRow.account_id.in_(account_ids), AccountRow.detached_at.is_(None), LinkRow.status == "active")
        .cte("selected_accounts")
    )
    balances = (
        select(
            BalanceSnapshotRow.account_id,
            BalanceSnapshotRow.current,
            BalanceSnapshotRow.iso_currency_code.label("balance_currency"),
            BalanceSnapshotRow.captured_at,
            func.row_number()
            .over(
                partition_by=BalanceSnapshotRow.account_id,
                order_by=(BalanceSnapshotRow.captured_at.desc(), BalanceSnapshotRow.id.desc()),
            )
            .label("balance_rank"),
        )
        .join(
            accounts,
            and_(
                accounts.c.account_id == BalanceSnapshotRow.account_id, accounts.c.item_id == BalanceSnapshotRow.item_id
            ),
        )
        .cte("ranked_balances")
    )

    balance_source = accounts.outerjoin(
        balances, and_(balances.c.account_id == accounts.c.account_id, balances.c.balance_rank == 1)
    )
    if not holding_account_ids:
        return select(
            accounts.c.account_id,
            accounts.c.type.label("account_type"),
            accounts.c.account_currency.label("account_currency"),
            balances.c.current.label("current"),
            balances.c.balance_currency.label("balance_currency"),
            balances.c.captured_at.label("captured_at"),
            literal(None).label("holding_capture_id"),
            literal(None).label("holding_capture_covered"),
            literal(False).label("holding_capture_has_unapplied_observations"),
        ).select_from(balance_source)

    latest_captures = latest_holding_captures(holding_account_ids)
    holding_accounts = (
        select(
            accounts.c.account_id,
            latest_captures.c.capture_id,
            latest_captures.c.captured_at,
            HoldingCaptureAccountRow.account_id.label("covered_account_id"),
            HoldingCaptureAccountRow.current,
            HoldingCaptureAccountRow.iso_currency_code,
            latest_captures.c.has_unapplied_observations,
        )
        .select_from(
            accounts.outerjoin(latest_captures, latest_captures.c.item_id == accounts.c.item_id).outerjoin(
                HoldingCaptureAccountRow,
                and_(
                    HoldingCaptureAccountRow.capture_id == latest_captures.c.capture_id,
                    HoldingCaptureAccountRow.account_id == accounts.c.account_id,
                ),
            )
        )
        .where(accounts.c.account_id.in_(holding_account_ids))
        .cte("holding_account_observations")
    )
    requires_holdings = accounts.c.account_id.in_(holding_account_ids)
    return select(
        accounts.c.account_id,
        accounts.c.type.label("account_type"),
        case((requires_holdings, holding_accounts.c.iso_currency_code), else_=accounts.c.account_currency).label(
            "account_currency"
        ),
        case((requires_holdings, holding_accounts.c.current), else_=balances.c.current).label("current"),
        case((requires_holdings, holding_accounts.c.iso_currency_code), else_=balances.c.balance_currency).label(
            "balance_currency"
        ),
        case((requires_holdings, holding_accounts.c.captured_at), else_=balances.c.captured_at).label("captured_at"),
        holding_accounts.c.capture_id.label("holding_capture_id"),
        case((requires_holdings, holding_accounts.c.covered_account_id.is_not(None)), else_=literal(None)).label(
            "holding_capture_covered"
        ),
        case(
            (requires_holdings, func.coalesce(holding_accounts.c.has_unapplied_observations, literal(False))),
            else_=literal(False),
        ).label("holding_capture_has_unapplied_observations"),
    ).select_from(balance_source.outerjoin(holding_accounts, holding_accounts.c.account_id == accounts.c.account_id))


def select_holding_batches(account_ids: Sequence[str]) -> Select[Any]:
    """Select only holdings linked to each selected account's latest Item capture."""
    accounts = (
        select(AccountRow.account_id, AccountRow.item_id)
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .where(AccountRow.account_id.in_(account_ids), AccountRow.detached_at.is_(None), LinkRow.status == "active")
        .cte("selected_holding_accounts")
    )
    latest_captures = latest_holding_captures(account_ids)
    return (
        select(
            HoldingSnapshotRow.account_id,
            HoldingSnapshotRow.security_id,
            HoldingSnapshotRow.capture_id,
            latest_captures.c.captured_at.label("captured_at"),
            HoldingSnapshotRow.institution_value,
            HoldingSnapshotRow.iso_currency_code.label("holding_currency"),
            HoldingSnapshotRow.quantity,
            SecurityRow.name.label("security_name"),
            SecurityRow.ticker_symbol.label("ticker"),
            SecurityRow.type.label("security_type"),
        )
        .select_from(accounts)
        .join(latest_captures, latest_captures.c.item_id == accounts.c.item_id)
        .join(
            HoldingCaptureAccountRow,
            and_(
                HoldingCaptureAccountRow.capture_id == latest_captures.c.capture_id,
                HoldingCaptureAccountRow.account_id == accounts.c.account_id,
            ),
        )
        .join(
            HoldingSnapshotRow,
            and_(
                HoldingSnapshotRow.capture_id == latest_captures.c.capture_id,
                HoldingSnapshotRow.account_id == accounts.c.account_id,
                HoldingSnapshotRow.item_id == accounts.c.item_id,
            ),
        )
        .outerjoin(SecurityRow, SecurityRow.security_id == HoldingSnapshotRow.security_id)
        .order_by(HoldingSnapshotRow.account_id, HoldingSnapshotRow.security_id, HoldingSnapshotRow.id.desc())
    )
