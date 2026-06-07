"""Postgres-backed budget classification and aggregation read model."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from augur.budget.default_rules import DEFAULT_RULES
from augur.budget.schema import BudgetConfig, MerchantSubstringRule, NameSubstringRule, PfcRule, Rule
from augur.budget.wire import (
    BucketMonthly,
    BucketView,
    BudgetSnapshotResponse,
    BudgetTransactionsResponse,
    LumpyView,
    TransactionView,
)
from augur.dates import DAYS_PER_MONTH


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(month: date, delta: int) -> date:
    total = month.year * 12 + (month.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


def _enumerate_months(start: date, end: date) -> tuple[date, ...]:
    months: list[date] = []
    cursor = _month_start(start)
    last = _month_start(end)
    while cursor <= last:
        months.append(cursor)
        cursor = _add_months(cursor, 1)
    return tuple(months)


def _effective_rules(config: BudgetConfig) -> tuple[Rule, ...]:
    if config.include_default_rules:
        return (*config.rules, *DEFAULT_RULES)
    return config.rules


def _bucket_defs_json(config: BudgetConfig) -> str:
    return json.dumps(
        [
            {"bucket_id": bucket.id, "kind": bucket.kind.value, "direction": bucket.direction.value}
            for bucket in config.buckets
        ]
    )


def _overrides_json(config: BudgetConfig) -> str:
    bucket_ids = {bucket.id for bucket in config.buckets}
    return json.dumps(
        [
            {"transaction_id": override.transaction_id, "bucket_id": override.bucket_id}
            for override in config.overrides
            if override.bucket_id in bucket_ids
        ]
    )


def _rule_rows_json(config: BudgetConfig) -> str:
    bucket_ids = {bucket.id for bucket in config.buckets}
    rows: list[dict[str, Any]] = []
    for rule_order, rule in enumerate(_effective_rules(config)):
        if rule.bucket_id not in bucket_ids:
            continue
        if isinstance(rule, MerchantSubstringRule):
            rows.append(
                {
                    "rule_order": rule_order,
                    "rule_kind": "merchant_substring",
                    "bucket_id": rule.bucket_id,
                    "pattern": rule.pattern,
                    "primary_value": None,
                    "detailed_value": None,
                }
            )
        elif isinstance(rule, NameSubstringRule):
            rows.append(
                {
                    "rule_order": rule_order,
                    "rule_kind": "name_substring",
                    "bucket_id": rule.bucket_id,
                    "pattern": rule.pattern,
                    "primary_value": None,
                    "detailed_value": None,
                }
            )
        elif isinstance(rule, PfcRule):
            rows.append(
                {
                    "rule_order": rule_order,
                    "rule_kind": "pfc",
                    "bucket_id": rule.bucket_id,
                    "pattern": None,
                    "primary_value": rule.primary,
                    "detailed_value": rule.detailed,
                }
            )
        else:
            raise TypeError(f"unknown rule type: {type(rule).__name__}")
    return json.dumps(rows)


def _account_filter_sql(account_ids: tuple[str, ...]) -> str:
    if not account_ids:
        return ""
    return "AND t.account_id = ANY(CAST(:account_ids AS text[]))"


_CLASSIFIED_TX_CTE_TEMPLATE = """
WITH bucket_defs AS (
    SELECT *
    FROM jsonb_to_recordset(CAST(:bucket_defs_json AS jsonb))
        AS b(bucket_id text, kind text, direction text)
),
rules AS (
    SELECT *
    FROM jsonb_to_recordset(CAST(:rules_json AS jsonb))
        AS r(
            rule_order integer,
            rule_kind text,
            bucket_id text,
            pattern text,
            primary_value text,
            detailed_value text
        )
),
overrides AS (
    SELECT *
    FROM jsonb_to_recordset(CAST(:overrides_json AS jsonb))
        AS o(transaction_id text, bucket_id text)
),
base_tx AS (
    SELECT
        t.transaction_id,
        t.account_id,
        t.item_id,
        t.date,
        t.amount,
        t.name,
        t.merchant_name,
        t.pfc_primary,
        t.pfc_detailed
    FROM transactions AS t
    JOIN links AS link ON link.item_id = t.item_id
    WHERE
        t.removed IS FALSE
        AND t.pending IS FALSE
        AND t.date >= :window_start
        AND t.date <= :window_end
        AND link.status != 'revoked'
        {account_filter}
),
classified_tx AS (
    SELECT
        base_tx.*,
        COALESCE(
            -- A per-transaction override is the highest-priority assignment and is NOT
            -- direction-gated: an explicit human/agent call routes the txn to its bucket
            -- regardless of sign (e.g. a +amount "Returned Payment" -> transfers_out).
            ovr.bucket_id,
            matched.bucket_id,
            CASE
                WHEN base_tx.amount < 0 THEN :default_inflow_bucket_id
                ELSE :default_outflow_bucket_id
            END
        ) AS bucket_id
    FROM base_tx
    LEFT JOIN overrides AS ovr ON ovr.transaction_id = base_tx.transaction_id
    LEFT JOIN LATERAL (
        SELECT rules.bucket_id
        FROM rules
        JOIN bucket_defs ON bucket_defs.bucket_id = rules.bucket_id
        WHERE
            (
                (
                    rules.rule_kind = 'merchant_substring'
                    AND position(lower(rules.pattern) in lower(coalesce(base_tx.merchant_name, ''))) > 0
                )
                OR (
                    rules.rule_kind = 'name_substring'
                    AND position(lower(rules.pattern) in lower(base_tx.name)) > 0
                )
                OR (
                    rules.rule_kind = 'pfc'
                    AND base_tx.pfc_primary = rules.primary_value
                    AND (rules.detailed_value IS NULL OR base_tx.pfc_detailed = rules.detailed_value)
                )
            )
            AND (
                (bucket_defs.direction = 'outflow' AND base_tx.amount >= 0)
                OR (bucket_defs.direction = 'inflow' AND base_tx.amount < 0)
            )
        ORDER BY rules.rule_order
        LIMIT 1
    ) AS matched ON TRUE
)
"""

_MONTHLY_TOTALS_SQL_TEMPLATE = (
    _CLASSIFIED_TX_CTE_TEMPLATE
    + """
SELECT
    classified_tx.bucket_id,
    date_trunc('month', classified_tx.date)::date AS month,
    sum(classified_tx.amount)::float AS amount,
    count(*)::integer AS transaction_count
FROM classified_tx
GROUP BY classified_tx.bucket_id, month
ORDER BY month, classified_tx.bucket_id
"""
)

_LUMPY_SQL_TEMPLATE = (
    _CLASSIFIED_TX_CTE_TEMPLATE
    + """
SELECT
    classified_tx.transaction_id,
    classified_tx.date,
    classified_tx.amount,
    classified_tx.name,
    classified_tx.merchant_name,
    classified_tx.bucket_id
FROM classified_tx
JOIN bucket_defs ON bucket_defs.bucket_id = classified_tx.bucket_id
WHERE classified_tx.amount >= :lumpy_threshold_usd
  AND bucket_defs.kind = 'expense'
ORDER BY classified_tx.amount DESC, classified_tx.date, classified_tx.transaction_id
"""
)

_DRILLDOWN_SQL_TEMPLATE = (
    _CLASSIFIED_TX_CTE_TEMPLATE
    + """
SELECT
    classified_tx.transaction_id,
    classified_tx.date,
    classified_tx.amount,
    classified_tx.name,
    classified_tx.merchant_name,
    classified_tx.pfc_primary,
    classified_tx.pfc_detailed,
    accounts.name AS account_name,
    link.institution_name,
    classified_tx.bucket_id
FROM classified_tx
JOIN accounts ON accounts.account_id = classified_tx.account_id
JOIN links AS link ON link.item_id = classified_tx.item_id
WHERE classified_tx.bucket_id = :bucket_id
ORDER BY classified_tx.date, classified_tx.transaction_id
"""
)

# Stale-override probe (decision 7): a GLOBAL existence check by transaction_id, NOT
# scoped to the request window/accounts, so an override for an out-of-window txn is not
# falsely flagged. An override whose txn is gone (e.g. after a relink mints new ids, or
# the txn was removed) surfaces here instead of silently no-op'ing.
_STALE_OVERRIDES_SQL = text(
    "SELECT transaction_id FROM transactions "
    "WHERE transaction_id = ANY(CAST(:override_ids AS text[])) AND removed IS FALSE"
)


async def _read_stale_overrides(session: AsyncSession, config: BudgetConfig) -> tuple[str, ...]:
    override_ids = [override.transaction_id for override in config.overrides]
    if not override_ids:
        return ()
    live = set((await session.execute(_STALE_OVERRIDES_SQL, {"override_ids": override_ids})).scalars())
    return tuple(sorted({txn_id for txn_id in override_ids if txn_id not in live}))


def _statement_params(
    *, config: BudgetConfig, window_start: date, window_end: date, account_ids: tuple[str, ...]
) -> dict[str, object]:
    return {
        "bucket_defs_json": _bucket_defs_json(config),
        "rules_json": _rule_rows_json(config),
        "overrides_json": _overrides_json(config),
        "window_start": window_start,
        "window_end": window_end,
        "default_inflow_bucket_id": config.default_inflow_bucket_id,
        "default_outflow_bucket_id": config.default_outflow_bucket_id,
        "account_ids": list(account_ids),
    }


def _budget_statement(sql_template: str, account_ids: tuple[str, ...]) -> Any:
    return text(sql_template.format(account_filter=_account_filter_sql(account_ids)))


@dataclass(frozen=True)
class _MonthlyAgg:
    bucket_id: str
    month: date
    amount: float
    transaction_count: int


async def read_budget_snapshot(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    config: BudgetConfig,
    window_start: date,
    window_end: date,
    account_ids: tuple[str, ...] = (),
) -> BudgetSnapshotResponse:
    """Return the budget snapshot with filtering, classification, and grouping done in SQL."""
    params = _statement_params(config=config, window_start=window_start, window_end=window_end, account_ids=account_ids)
    async with session_factory() as session:
        stale_overrides = await _read_stale_overrides(session, config)
        monthly_rows = (
            await session.execute(_budget_statement(_MONTHLY_TOTALS_SQL_TEMPLATE, account_ids), params)
        ).mappings()
        lumpy_rows = (
            await session.execute(
                _budget_statement(_LUMPY_SQL_TEMPLATE, account_ids),
                params | {"lumpy_threshold_usd": config.lumpy_threshold_usd},
            )
        ).mappings()
        monthly = tuple(
            _MonthlyAgg(
                bucket_id=row["bucket_id"],
                month=row["month"],
                amount=float(row["amount"]),
                transaction_count=int(row["transaction_count"]),
            )
            for row in monthly_rows
        )
        lumpy = tuple(
            LumpyView(
                transaction_id=row["transaction_id"],
                date=row["date"],
                amount=float(row["amount"]),
                name=row["name"],
                merchant_name=row["merchant_name"],
                bucket_id=row["bucket_id"],
            )
            for row in lumpy_rows
        )

    months = _enumerate_months(window_start, window_end)
    amounts_by_bucket_month = {(row.bucket_id, row.month): row.amount for row in monthly}
    counts_by_bucket: dict[str, int] = {}
    for row in monthly:
        counts_by_bucket[row.bucket_id] = counts_by_bucket.get(row.bucket_id, 0) + row.transaction_count
    days_covered = max((window_end - window_start).days + 1, 1)

    return BudgetSnapshotResponse(
        months=months,
        buckets=tuple(
            BucketView(id=bucket.id, label=bucket.label, kind=bucket.kind, family=bucket.family)
            for bucket in config.buckets
        ),
        monthly_by_bucket=tuple(
            BucketMonthly(
                bucket_id=bucket.id,
                monthly_amounts=tuple(amounts_by_bucket_month.get((bucket.id, month), 0.0) for month in months),
                window_monthly_avg=(
                    sum(amounts_by_bucket_month.get((bucket.id, month), 0.0) for month in months)
                    / days_covered
                    * DAYS_PER_MONTH
                ),
                transaction_count=counts_by_bucket.get(bucket.id, 0),
            )
            for bucket in config.buckets
        ),
        lumpy=lumpy,
        lumpy_threshold_usd=config.lumpy_threshold_usd,
        stale_overrides=stale_overrides,
        data_window_start=window_start,
        data_window_end=window_end,
        coverage_starts=config.source.coverage_starts,
    )


async def read_budget_bucket_transactions(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    config: BudgetConfig,
    bucket_id: str,
    window_start: date,
    window_end: date,
    account_ids: tuple[str, ...] = (),
) -> BudgetTransactionsResponse:
    """Return one bucket's transactions with classification/filtering done in SQL."""
    bucket_ids = {bucket.id for bucket in config.buckets}
    if bucket_id not in bucket_ids:
        raise ValueError(f"unknown bucket_id {bucket_id!r}; have {sorted(bucket_ids)}")
    params = _statement_params(
        config=config, window_start=window_start, window_end=window_end, account_ids=account_ids
    ) | {"bucket_id": bucket_id}
    async with session_factory() as session:
        rows = (await session.execute(_budget_statement(_DRILLDOWN_SQL_TEMPLATE, account_ids), params)).mappings()
        transactions = tuple(
            TransactionView(
                transaction_id=row["transaction_id"],
                date=row["date"],
                amount=float(row["amount"]),
                name=row["name"],
                merchant_name=row["merchant_name"],
                pfc_primary=row["pfc_primary"],
                pfc_detailed=row["pfc_detailed"],
                account_name=row["account_name"],
                institution_name=row["institution_name"],
                bucket_id=row["bucket_id"],
            )
            for row in rows
        )
    return BudgetTransactionsResponse(bucket_id=bucket_id, transactions=transactions)
