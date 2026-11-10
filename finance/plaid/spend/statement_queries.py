"""SQLAlchemy Core reads for the shared card statement view."""

from datetime import date
from typing import Any

from sqlalchemy import Text, and_, case, cast, func, select
from sqlalchemy.sql import Select

from finance.plaid.db.models import AccountRow, LiabilityCreditSnapshotRow, LinkRow, TransactionRow


def select_card_accounts(account_ids: list[str]) -> Select[Any]:
    """Select configured, active credit accounts as the statement view currently does."""
    return (
        select(
            AccountRow.account_id,
            AccountRow.name.label("account_name"),
            AccountRow.mask,
            func.coalesce(
                AccountRow.iso_currency_code,
                AccountRow.raw_json["balances"]["iso_currency_code"].as_string(),
                AccountRow.raw_json["balances"]["unofficial_currency_code"].as_string(),
                "USD",
            ).label("currency"),
            LinkRow.institution_name,
            LinkRow.item_id,
            LinkRow.last_synced_at,
        )
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .where(AccountRow.account_id.in_(account_ids), AccountRow.type == "credit", LinkRow.status == "active")
        .order_by(AccountRow.account_id)
    )


def select_statement_snapshots(account_ids: list[str]) -> Select[Any]:
    """Select only each account's newest captured credit-liability snapshot."""
    return (
        select(
            LiabilityCreditSnapshotRow.account_id,
            LiabilityCreditSnapshotRow.raw_json["last_statement_issue_date"]
            .as_string()
            .label("last_statement_issue_date"),
        )
        .join(
            AccountRow,
            and_(
                AccountRow.account_id == LiabilityCreditSnapshotRow.account_id,
                AccountRow.item_id == LiabilityCreditSnapshotRow.item_id,
            ),
        )
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .where(LiabilityCreditSnapshotRow.account_id.in_(account_ids), LinkRow.status == "active")
        .distinct(LiabilityCreditSnapshotRow.account_id)
        .order_by(
            LiabilityCreditSnapshotRow.account_id,
            LiabilityCreditSnapshotRow.captured_at.desc(),
            LiabilityCreditSnapshotRow.id.desc(),
        )
    )


def select_first_card_transactions(account_ids: list[str], through: date) -> Select[Any]:
    """Find the first retained transaction through today for provisional periods."""
    return (
        select(TransactionRow.account_id, func.min(TransactionRow.date).label("first_date"))
        .join(
            AccountRow,
            and_(AccountRow.account_id == TransactionRow.account_id, AccountRow.item_id == TransactionRow.item_id),
        )
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .where(
            TransactionRow.account_id.in_(account_ids),
            TransactionRow.date <= through,
            TransactionRow.removed.is_(False),
            LinkRow.status == "active",
        )
        .group_by(TransactionRow.account_id)
    )


def select_card_transactions(
    account_ids: list[str], *, start_date: date, through: date, include_history: bool, include_raw_json: bool
) -> Select[Any]:
    """Select card transactions, including retained posted replacements when bounded."""
    posted_replacement = and_(TransactionRow.pending.is_(False), TransactionRow.pending_transaction_id.is_not(None))
    within_period_or_replacement = and_(
        TransactionRow.date <= through, (TransactionRow.date >= start_date) | posted_replacement
    )
    counterparties = case(
        (
            func.jsonb_typeof(TransactionRow.raw_json["counterparties"]) == "array",
            cast(TransactionRow.raw_json["counterparties"], Text),
        ),
        else_="[]",
    ).label("counterparties")
    columns = [
        TransactionRow.account_id,
        TransactionRow.transaction_id,
        TransactionRow.date,
        TransactionRow.amount,
        TransactionRow.pending,
        TransactionRow.pending_transaction_id,
        TransactionRow.raw_json["authorized_date"].as_string().label("authorized_date"),
        TransactionRow.raw_json["authorized_datetime"].as_string().label("authorized_datetime"),
        TransactionRow.raw_json["datetime"].as_string().label("posted_datetime"),
        TransactionRow.name,
        TransactionRow.merchant_name,
        TransactionRow.pfc_primary,
    ]
    if include_raw_json:
        columns.append(TransactionRow.raw_json.label("raw_json"))
        columns.append(counterparties)
    columns.extend(
        [
            TransactionRow.raw_json["merchant_category_code"].as_string().label("merchant_category_code"),
            func.coalesce(
                TransactionRow.iso_currency_code, TransactionRow.raw_json["unofficial_currency_code"].as_string()
            ).label("currency"),
            func.coalesce(
                TransactionRow.pfc_detailed,
                TransactionRow.raw_json["personal_finance_category"]["detailed"].as_string(),
            ).label("pfc_detailed"),
        ]
    )
    query = (
        select(*columns)
        .join(
            AccountRow,
            and_(AccountRow.account_id == TransactionRow.account_id, AccountRow.item_id == TransactionRow.item_id),
        )
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .where(
            TransactionRow.account_id.in_(account_ids), LinkRow.status == "active", TransactionRow.removed.is_(False)
        )
    )
    if not include_history:
        query = query.where(within_period_or_replacement)
    return query
