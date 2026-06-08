"""Postgres-backed budget classification and aggregation read model."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance.augur.budget.schema import (
    AccountCondition,
    AllOfCondition,
    AmountCondition,
    AnyOfCondition,
    BudgetConfig,
    Condition,
    MatchRule,
    MerchantRegexCondition,
    MerchantSubstringCondition,
    MerchantSubstringRule,
    NameRegexCondition,
    NameSubstringCondition,
    NameSubstringRule,
    NotCondition,
    PfcCondition,
    PfcRule,
    Rule,
    TransferDirection,
)
from finance.augur.budget.wire import (
    BucketMonthly,
    BucketView,
    BudgetSnapshotResponse,
    BudgetTransactionsResponse,
    LumpyView,
    TransactionView,
)
from finance.augur.dates import DAYS_PER_MONTH


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


# --- Rule -> SQL compiler (Stage A) ------------------------------------------------
# Each rule compiles to one `WHEN (<condition>) AND (<direction gate>) THEN :bucket` arm
# of a CASE; first matching arm wins (rule order). Condition values become bound params
# (never inlined), so patterns / amounts / account ids stay injection-safe.


def _rule_condition(rule: Rule) -> Condition:
    """The flat rule kinds are shorthands for single-leaf conditions; MatchRule carries its own."""
    if isinstance(rule, MerchantSubstringRule):
        return MerchantSubstringCondition(pattern=rule.pattern)
    if isinstance(rule, NameSubstringRule):
        return NameSubstringCondition(pattern=rule.pattern)
    if isinstance(rule, PfcRule):
        return PfcCondition(primary=rule.primary, detailed=rule.detailed)
    if isinstance(rule, MatchRule):
        return rule.condition
    raise TypeError(f"unknown rule type: {type(rule).__name__}")


def _compile_condition(cond: Condition, bind: Callable[[object], str]) -> str:
    """Compile a condition to a boolean SQL expression over `base_tx`, binding values via `bind`."""
    if isinstance(cond, MerchantSubstringCondition):
        return f"position(lower({bind(cond.pattern)}) in lower(coalesce(base_tx.merchant_name, ''))) > 0"
    if isinstance(cond, NameSubstringCondition):
        return f"position(lower({bind(cond.pattern)}) in lower(base_tx.name)) > 0"
    if isinstance(cond, MerchantRegexCondition):
        return f"coalesce(base_tx.merchant_name, '') ~* {bind(cond.pattern)}"
    if isinstance(cond, NameRegexCondition):
        return f"base_tx.name ~* {bind(cond.pattern)}"
    if isinstance(cond, PfcCondition):
        sql = f"base_tx.pfc_primary = {bind(cond.primary)}"
        if cond.detailed is not None:
            sql = f"({sql} AND base_tx.pfc_detailed = {bind(cond.detailed)})"
        return sql
    if isinstance(cond, AmountCondition):
        col = "abs(base_tx.amount)" if cond.use_abs else "base_tx.amount"
        bounds: list[str] = []
        if cond.min is not None:
            bounds.append(f"{col} >= {bind(cond.min)}")
        if cond.max is not None:
            bounds.append(f"{col} <= {bind(cond.max)}")
        return "(" + " AND ".join(bounds) + ")"
    if isinstance(cond, AccountCondition):
        return f"base_tx.account_id = ANY(CAST({bind(list(cond.account_ids))} AS text[]))"
    if isinstance(cond, AllOfCondition):
        return "(" + " AND ".join(_compile_condition(c, bind) for c in cond.conditions) + ")"
    if isinstance(cond, AnyOfCondition):
        return "(" + " OR ".join(_compile_condition(c, bind) for c in cond.conditions) + ")"
    if isinstance(cond, NotCondition):
        return f"(NOT ({_compile_condition(cond.condition, bind)}))"
    raise TypeError(f"unknown condition type: {type(cond).__name__}")


def _direction_gate(direction: TransferDirection) -> str:
    # Plaid signs outflows positive, inflows negative; a rule fires only on the leg whose sign
    # matches its target bucket's direction (same gating as the flat rule kinds).
    return "base_tx.amount >= 0" if direction == TransferDirection.OUTFLOW else "base_tx.amount < 0"


def _compile_rules_case(config: BudgetConfig) -> tuple[str, dict[str, object]]:
    """Build the first-match-wins CASE assigning a bucket from the rules, plus its bind params."""
    buckets_by_id = {bucket.id: bucket for bucket in config.buckets}
    params: dict[str, object] = {}
    counter = 0

    def bind(value: object) -> str:
        nonlocal counter
        key = f"rc{counter}"
        counter += 1
        params[key] = value
        return f":{key}"

    arms: list[str] = []
    for rule in config.rules:
        bucket = buckets_by_id.get(rule.bucket_id)
        if bucket is None:
            continue
        cond_sql = _compile_condition(_rule_condition(rule), bind)
        arms.append(f"WHEN ({cond_sql}) AND ({_direction_gate(bucket.direction)}) THEN {bind(rule.bucket_id)}")
    if not arms:
        return "NULL", params
    return "CASE " + " ".join(arms) + " END", params


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
            -- Generated first-match-wins CASE over the configured rules (_compile_rules_case);
            -- evaluates to NULL when no rule matches, falling through to the per-direction default.
            {matched_case},
            CASE
                WHEN base_tx.amount < 0 THEN :default_inflow_bucket_id
                ELSE :default_outflow_bucket_id
            END
        ) AS bucket_id
    FROM base_tx
    LEFT JOIN overrides AS ovr ON ovr.transaction_id = base_tx.transaction_id
)
"""

# Query tails appended to the dynamically-built classified-tx CTE (see _budget_query).
_MONTHLY_TOTALS_TAIL = """
SELECT
    classified_tx.bucket_id,
    date_trunc('month', classified_tx.date)::date AS month,
    sum(classified_tx.amount)::float AS amount,
    count(*)::integer AS transaction_count
FROM classified_tx
GROUP BY classified_tx.bucket_id, month
ORDER BY month, classified_tx.bucket_id
"""

_LUMPY_TAIL = """
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

_DRILLDOWN_TAIL = """
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

# Every live classified txn (all buckets at once), for the Beancount exporter. Reuses the
# same classified-tx CTE as the budget UI so the exported ledger and the in-app budget can
# never disagree about which bucket a transaction landed in.
_EXPORT_TAIL = """
SELECT
    classified_tx.transaction_id,
    classified_tx.date,
    classified_tx.amount,
    classified_tx.name,
    classified_tx.merchant_name,
    classified_tx.pfc_primary,
    classified_tx.pfc_detailed,
    classified_tx.account_id,
    classified_tx.bucket_id
FROM classified_tx
ORDER BY classified_tx.date, classified_tx.transaction_id
"""

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


def _budget_query(
    tail: str,
    *,
    config: BudgetConfig,
    window_start: date,
    window_end: date,
    account_ids: tuple[str, ...],
    extra_params: dict[str, object] | None = None,
) -> tuple[Any, dict[str, object]]:
    """Build an executable statement (classified-tx CTE + `tail`) and its bind params."""
    matched_case, rule_params = _compile_rules_case(config)
    cte = _CLASSIFIED_TX_CTE_TEMPLATE.format(account_filter=_account_filter_sql(account_ids), matched_case=matched_case)
    params: dict[str, object] = {
        "bucket_defs_json": _bucket_defs_json(config),
        "overrides_json": _overrides_json(config),
        "window_start": window_start,
        "window_end": window_end,
        "default_inflow_bucket_id": config.default_inflow_bucket_id,
        "default_outflow_bucket_id": config.default_outflow_bucket_id,
        "account_ids": list(account_ids),
        **rule_params,
        **(extra_params or {}),
    }
    return text(cte + tail), params


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
    monthly_stmt, monthly_params = _budget_query(
        _MONTHLY_TOTALS_TAIL, config=config, window_start=window_start, window_end=window_end, account_ids=account_ids
    )
    lumpy_stmt, lumpy_params = _budget_query(
        _LUMPY_TAIL,
        config=config,
        window_start=window_start,
        window_end=window_end,
        account_ids=account_ids,
        extra_params={"lumpy_threshold_usd": config.lumpy_threshold_usd},
    )
    async with session_factory() as session:
        stale_overrides = await _read_stale_overrides(session, config)
        monthly_rows = (await session.execute(monthly_stmt, monthly_params)).mappings()
        lumpy_rows = (await session.execute(lumpy_stmt, lumpy_params)).mappings()
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
    stmt, params = _budget_query(
        _DRILLDOWN_TAIL,
        config=config,
        window_start=window_start,
        window_end=window_end,
        account_ids=account_ids,
        extra_params={"bucket_id": bucket_id},
    )
    async with session_factory() as session:
        rows = (await session.execute(stmt, params)).mappings()
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


@dataclass(frozen=True)
class ClassifiedRow:
    """One live classified transaction, as the Beancount exporter consumes it."""

    transaction_id: str
    date: date
    amount: float
    name: str
    merchant_name: str | None
    pfc_primary: str | None
    pfc_detailed: str | None
    account_id: str
    bucket_id: str


async def read_all_classified(
    *, session_factory: async_sessionmaker[AsyncSession], config: BudgetConfig, window_start: date, window_end: date
) -> tuple[ClassifiedRow, ...]:
    """Return every live transaction in [window_start, window_end] with its bucket.

    Unlike the snapshot/drilldown readers this returns all buckets at once and applies
    no per-bucket filter -- it's the full projection the Beancount exporter renders.
    Account scope comes from the configured source (``plaid_account_ids``).
    """
    stmt, params = _budget_query(
        _EXPORT_TAIL,
        config=config,
        window_start=window_start,
        window_end=window_end,
        account_ids=tuple(config.source.plaid_account_ids),
    )
    async with session_factory() as session:
        rows = (await session.execute(stmt, params)).mappings()
        return tuple(
            ClassifiedRow(
                transaction_id=row["transaction_id"],
                date=row["date"],
                amount=float(row["amount"]),
                name=row["name"],
                merchant_name=row["merchant_name"],
                pfc_primary=row["pfc_primary"],
                pfc_detailed=row["pfc_detailed"],
                account_id=row["account_id"],
                bucket_id=row["bucket_id"],
            )
            for row in rows
        )
