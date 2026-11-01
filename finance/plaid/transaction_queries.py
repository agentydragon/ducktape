"""Selected transaction projections for policy classification and spend analysis."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, Text, and_, case, cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from finance.plaid.db.models import AccountRow, LinkRow, TransactionRow


def select_transactions(account_ids: Sequence[str]) -> Select[Any]:
    """Retained pending and posted rows on selected active, attached accounts.

    JSON counterparts are serialized for the spend classifier's Pydantic Json
    boundary. Classification and purchase-day selection remain caller policy.
    """
    raw = cast(TransactionRow.raw_json, JSONB)
    return (
        select(
            LinkRow.institution_name,
            AccountRow.type,
            AccountRow.subtype,
            TransactionRow.account_id,
            TransactionRow.transaction_id,
            TransactionRow.pending_transaction_id,
            cast(TransactionRow.date, Text).label("date"),
            raw["authorized_date"].astext.label("authorized_date"),
            raw["authorized_datetime"].astext.label("authorized_datetime"),
            raw["datetime"].astext.label("posted_datetime"),
            cast(TransactionRow.amount, Text).label("amount"),
            TransactionRow.pfc_primary,
            TransactionRow.pfc_detailed,
            TransactionRow.merchant_name,
            TransactionRow.name,
            TransactionRow.pending,
            TransactionRow.iso_currency_code,
            func.coalesce(TransactionRow.iso_currency_code, raw["unofficial_currency_code"].astext).label("currency"),
            raw["merchant_category_code"].astext.label("merchant_category_code"),
            raw["personal_finance_category"]["confidence_level"].astext.label("pfc_confidence"),
            case(
                (func.jsonb_typeof(raw["counterparties"]) == "array", cast(raw["counterparties"], Text)), else_="[]"
            ).label("counterparties"),
        )
        .select_from(TransactionRow)
        .join(
            AccountRow,
            and_(AccountRow.account_id == TransactionRow.account_id, AccountRow.item_id == TransactionRow.item_id),
        )
        .join(LinkRow, LinkRow.item_id == TransactionRow.item_id)
        .where(
            LinkRow.status == "active",
            AccountRow.detached_at.is_(None),
            TransactionRow.removed.is_(False),
            TransactionRow.account_id.in_(account_ids),
        )
    )
