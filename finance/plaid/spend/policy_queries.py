"""Current Plaid account and transaction reads used by allowance policy."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, Text, and_, case, cast, func, select

from finance.plaid.db.models import AccountRow, LinkRow, TransactionRow


def select_policy_accounts(account_ids: Sequence[str]) -> Select[Any]:
    """Select configured accounts that remain in the latest complete inventory."""
    return (
        select(
            AccountRow.account_id, AccountRow.name, AccountRow.type, LinkRow.institution_name, LinkRow.last_synced_at
        )
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .where(AccountRow.account_id.in_(account_ids), AccountRow.detached_at.is_(None), LinkRow.status == "active")
    )


def select_policy_transactions(account_ids: Sequence[str], *, include_raw_json: bool) -> Select[Any]:
    """Select unbounded current transaction history for the configured policy accounts."""
    return (
        select(
            TransactionRow.account_id,
            TransactionRow.transaction_id,
            TransactionRow.pending_transaction_id,
            TransactionRow.date,
            TransactionRow.amount,
            TransactionRow.pending,
            TransactionRow.name,
            TransactionRow.merchant_name,
            TransactionRow.pfc_primary,
            TransactionRow.pfc_detailed,
            TransactionRow.raw_json["authorized_date"].as_string().label("authorized_date"),
            TransactionRow.raw_json["authorized_datetime"].as_string().label("authorized_datetime"),
            TransactionRow.raw_json["datetime"].as_string().label("posted_datetime"),
            AccountRow.type.label("account_type"),
            TransactionRow.raw_json["merchant_category_code"].as_string().label("merchant_category_code"),
            case(
                (
                    func.jsonb_typeof(TransactionRow.raw_json["counterparties"]) == "array",
                    cast(TransactionRow.raw_json["counterparties"], Text),
                ),
                else_="[]",
            ).label("counterparties"),
            func.coalesce(
                TransactionRow.iso_currency_code, TransactionRow.raw_json["unofficial_currency_code"].as_string()
            ).label("currency"),
            *((TransactionRow.raw_json.label("raw_json"),) if include_raw_json else ()),
        )
        .join(
            AccountRow,
            and_(AccountRow.account_id == TransactionRow.account_id, AccountRow.item_id == TransactionRow.item_id),
        )
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .where(
            TransactionRow.account_id.in_(account_ids),
            TransactionRow.removed.is_(False),
            AccountRow.detached_at.is_(None),
            LinkRow.status == "active",
        )
    )
