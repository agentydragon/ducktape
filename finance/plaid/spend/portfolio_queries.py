"""Selected, active Plaid account balances and complete holding capture batches."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, and_, func, select

from finance.plaid.db.models import AccountRow, BalanceSnapshotRow, HoldingSnapshotRow, LinkRow, SecurityRow


def select_account_balances(account_ids: Sequence[str]) -> Select[Any]:
    """Select each active account's newest balance, breaking capture ties by row ID."""
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
    return select(
        accounts.c.account_id,
        accounts.c.type.label("account_type"),
        accounts.c.account_currency,
        balances.c.current,
        balances.c.balance_currency,
        balances.c.captured_at,
    ).select_from(
        accounts.outerjoin(balances, and_(balances.c.account_id == accounts.c.account_id, balances.c.balance_rank == 1))
    )


def select_holding_batches(account_ids: Sequence[str]) -> Select[Any]:
    """Select each account's latest complete holding batch, with security metadata.

    Selecting the newest row independently per security would retain sold positions.
    A batch is the mirror's shared captured_at for the account's holdings response.
    """
    batches = (
        select(HoldingSnapshotRow.account_id, func.max(HoldingSnapshotRow.captured_at).label("captured_at"))
        .join(
            AccountRow,
            and_(
                AccountRow.account_id == HoldingSnapshotRow.account_id, AccountRow.item_id == HoldingSnapshotRow.item_id
            ),
        )
        .join(LinkRow, LinkRow.item_id == HoldingSnapshotRow.item_id)
        .where(
            HoldingSnapshotRow.account_id.in_(account_ids), AccountRow.detached_at.is_(None), LinkRow.status == "active"
        )
        .group_by(HoldingSnapshotRow.account_id)
        .cte("latest_holding_batches")
    )
    return (
        select(
            HoldingSnapshotRow.account_id,
            HoldingSnapshotRow.security_id,
            HoldingSnapshotRow.captured_at,
            HoldingSnapshotRow.institution_value,
            HoldingSnapshotRow.iso_currency_code.label("holding_currency"),
            HoldingSnapshotRow.quantity,
            SecurityRow.name.label("security_name"),
            SecurityRow.ticker_symbol.label("ticker"),
            SecurityRow.type.label("security_type"),
        )
        .join(
            batches,
            and_(
                batches.c.account_id == HoldingSnapshotRow.account_id,
                batches.c.captured_at == HoldingSnapshotRow.captured_at,
            ),
        )
        .outerjoin(SecurityRow, SecurityRow.security_id == HoldingSnapshotRow.security_id)
        .order_by(HoldingSnapshotRow.account_id, HoldingSnapshotRow.security_id, HoldingSnapshotRow.id.desc())
    )
