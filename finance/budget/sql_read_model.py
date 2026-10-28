"""Postgres-backed budget classification and aggregation read model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Integer, String, and_, case, func, literal, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

from finance.budget.schema import (
    AccountCondition,
    AllOfCondition,
    AmountCondition,
    AnyOfCondition,
    BucketKind,
    BudgetConfig,
    Condition,
    DateCondition,
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
from finance.budget.wire import (
    BucketMonthly,
    BucketView,
    BudgetSnapshotResponse,
    BudgetTransactionsResponse,
    LumpyView,
    TransactionView,
)
from finance.plaid.db.models import AccountRow, LinkRow, TransactionRow

# Mean Gregorian month in days; converts a window's per-day average into `window_monthly_avg`.
DAYS_PER_MONTH = 365.2425 / 12.0
_DECIMAL_DAYS_PER_MONTH = Decimal("365.2425") / 12


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


# --- Rule -> SQLAlchemy expressions -------------------------------------------------


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


def _condition_expression(cond: Condition, base_tx: CTE) -> ColumnElement[bool]:
    """Build a condition over base_tx; SQLAlchemy owns bind creation and composition."""
    if isinstance(cond, MerchantSubstringCondition):
        return func.strpos(func.lower(func.coalesce(base_tx.c.merchant_name, "")), func.lower(cond.pattern)) > 0
    if isinstance(cond, NameSubstringCondition):
        return func.strpos(func.lower(base_tx.c.name), func.lower(cond.pattern)) > 0
    if isinstance(cond, MerchantRegexCondition):
        return func.coalesce(base_tx.c.merchant_name, "").op("~*")(cond.pattern)
    if isinstance(cond, NameRegexCondition):
        return base_tx.c.name.op("~*")(cond.pattern)
    if isinstance(cond, PfcCondition):
        conditions = [base_tx.c.pfc_primary == cond.primary]
        if cond.detailed is not None:
            conditions.append(base_tx.c.pfc_detailed == cond.detailed)
        return and_(*conditions)
    if isinstance(cond, AmountCondition):
        amount = func.abs(base_tx.c.amount) if cond.use_abs else base_tx.c.amount
        bounds: list[ColumnElement[bool]] = []
        if cond.min is not None:
            bounds.append(amount >= cond.min)
        if cond.max is not None:
            bounds.append(amount <= cond.max)
        return and_(*bounds)
    if isinstance(cond, DateCondition):
        if cond.on is not None:
            return base_tx.c.date == cond.on
        bounds = []
        if cond.min is not None:
            bounds.append(base_tx.c.date >= cond.min)
        if cond.max is not None:
            bounds.append(base_tx.c.date <= cond.max)
        return and_(*bounds)
    if isinstance(cond, AccountCondition):
        return base_tx.c.account_id.in_(cond.account_ids)
    if isinstance(cond, AllOfCondition):
        return and_(*(_condition_expression(condition, base_tx) for condition in cond.conditions))
    if isinstance(cond, AnyOfCondition):
        return or_(*(_condition_expression(condition, base_tx) for condition in cond.conditions))
    if isinstance(cond, NotCondition):
        return not_(_condition_expression(cond.condition, base_tx))
    raise TypeError(f"unknown condition type: {type(cond).__name__}")


def _direction_gate(base_tx: CTE, direction: TransferDirection) -> ColumnElement[bool]:
    # Plaid signs outflows positive, inflows negative; a rule fires only on the matching leg.
    return base_tx.c.amount >= 0 if direction == TransferDirection.OUTFLOW else base_tx.c.amount < 0


def _rules_case(config: BudgetConfig, base_tx: CTE) -> ColumnElement[str]:
    buckets_by_id = {bucket.id: bucket for bucket in config.buckets}
    arms = [
        (
            and_(_condition_expression(_rule_condition(rule), base_tx), _direction_gate(base_tx, bucket.direction)),
            rule.bucket_id,
        )
        for rule in config.rules
        if (bucket := buckets_by_id.get(rule.bucket_id)) is not None
    ]
    return case(*arms) if arms else literal(None, type_=String())


def _match_overrides_case(config: BudgetConfig, base_tx: CTE) -> ColumnElement[str]:
    bucket_ids = {bucket.id for bucket in config.buckets}
    arms = [
        (_condition_expression(match_override.condition, base_tx), match_override.bucket_id)
        for match_override in config.match_overrides
        if match_override.bucket_id in bucket_ids
    ]
    return case(*arms) if arms else literal(None, type_=String())


def _transaction_overrides_case(config: BudgetConfig, base_tx: CTE) -> ColumnElement[str]:
    bucket_ids = {bucket.id for bucket in config.buckets}
    arms = [
        (base_tx.c.transaction_id == override.transaction_id, override.bucket_id)
        for override in config.overrides
        if override.bucket_id in bucket_ids
    ]
    return case(*arms) if arms else literal(None, type_=String())


def _classified_tx(config: BudgetConfig, *, window_start: date, window_end: date, account_ids: tuple[str, ...]) -> CTE:
    transactions = TransactionRow.__table__.alias("t")
    links = LinkRow.__table__.alias("link")
    base_tx_query = (
        select(
            transactions.c.transaction_id,
            transactions.c.account_id,
            transactions.c.item_id,
            transactions.c.date,
            transactions.c.amount,
            transactions.c.name,
            transactions.c.merchant_name,
            transactions.c.pfc_primary,
            transactions.c.pfc_detailed,
        )
        .select_from(transactions.join(links, links.c.item_id == transactions.c.item_id))
        .where(
            transactions.c.removed.is_(False),
            transactions.c.pending.is_(False),
            transactions.c.date >= window_start,
            transactions.c.date <= window_end,
            links.c.status != "revoked",
        )
    )
    if account_ids:
        base_tx_query = base_tx_query.where(transactions.c.account_id.in_(account_ids))
    base_tx = base_tx_query.cte("base_tx")

    default_bucket = case(
        (base_tx.c.amount < 0, config.default_inflow_bucket_id), else_=config.default_outflow_bucket_id
    )
    return select(
        *base_tx.c,
        func.coalesce(
            _transaction_overrides_case(config, base_tx),
            _match_overrides_case(config, base_tx),
            _rules_case(config, base_tx),
            default_bucket,
        ).label("bucket_id"),
    ).cte("classified_tx")


async def _read_stale_overrides(session: AsyncSession, config: BudgetConfig) -> tuple[str, ...]:
    override_ids = [override.transaction_id for override in config.overrides]
    if not override_ids:
        return ()
    stmt = select(TransactionRow.transaction_id).where(
        TransactionRow.transaction_id.in_(override_ids), TransactionRow.removed.is_(False)
    )
    live = set((await session.scalars(stmt)).all())
    return tuple(sorted({txn_id for txn_id in override_ids if txn_id not in live}))


@dataclass(frozen=True)
class _MonthlyAgg:
    bucket_id: str
    month: date
    amount: Decimal
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
    classified_tx = _classified_tx(config, window_start=window_start, window_end=window_end, account_ids=account_ids)
    month = func.date_trunc("month", classified_tx.c.date).cast(Date).label("month")
    monthly_stmt = (
        select(
            classified_tx.c.bucket_id,
            month,
            func.sum(classified_tx.c.amount).label("amount"),
            func.count().cast(Integer).label("transaction_count"),
        )
        .group_by(classified_tx.c.bucket_id, month)
        .order_by(month, classified_tx.c.bucket_id)
    )
    expense_bucket_ids = tuple(bucket.id for bucket in config.buckets if bucket.kind == BucketKind.EXPENSE)
    lumpy_stmt = (
        select(
            classified_tx.c.transaction_id,
            classified_tx.c.date,
            classified_tx.c.amount,
            classified_tx.c.name,
            classified_tx.c.merchant_name,
            classified_tx.c.bucket_id,
        )
        .where(classified_tx.c.amount >= config.lumpy_threshold_usd, classified_tx.c.bucket_id.in_(expense_bucket_ids))
        .order_by(classified_tx.c.amount.desc(), classified_tx.c.date, classified_tx.c.transaction_id)
    )
    async with session_factory() as session:
        stale_overrides = await _read_stale_overrides(session, config)
        monthly_rows = (await session.execute(monthly_stmt)).mappings()
        lumpy_rows = (await session.execute(lumpy_stmt)).mappings()
        monthly = tuple(
            _MonthlyAgg(
                bucket_id=row["bucket_id"],
                month=row["month"],
                amount=row["amount"],
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
                monthly_amounts=tuple(
                    float(amounts_by_bucket_month.get((bucket.id, month), Decimal(0))) for month in months
                ),
                window_monthly_avg=(
                    float(
                        sum(
                            (amounts_by_bucket_month.get((bucket.id, month), Decimal(0)) for month in months),
                            Decimal(0),
                        )
                        / days_covered
                        * _DECIMAL_DAYS_PER_MONTH
                    )
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
    classified_tx = _classified_tx(config, window_start=window_start, window_end=window_end, account_ids=account_ids)
    accounts = AccountRow.__table__.alias("accounts")
    links = LinkRow.__table__.alias("link")
    stmt = (
        select(
            classified_tx.c.transaction_id,
            classified_tx.c.date,
            classified_tx.c.amount,
            classified_tx.c.name,
            classified_tx.c.merchant_name,
            classified_tx.c.pfc_primary,
            classified_tx.c.pfc_detailed,
            accounts.c.name.label("account_name"),
            links.c.institution_name,
            classified_tx.c.bucket_id,
        )
        .select_from(
            classified_tx.join(accounts, accounts.c.account_id == classified_tx.c.account_id).join(
                links, links.c.item_id == classified_tx.c.item_id
            )
        )
        .where(classified_tx.c.bucket_id == bucket_id)
        .order_by(classified_tx.c.date, classified_tx.c.transaction_id)
    )
    async with session_factory() as session:
        rows = (await session.execute(stmt)).mappings()
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
    amount: Decimal
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
    classified_tx = _classified_tx(
        config, window_start=window_start, window_end=window_end, account_ids=tuple(config.source.plaid_account_ids)
    )
    stmt = select(
        classified_tx.c.transaction_id,
        classified_tx.c.date,
        classified_tx.c.amount,
        classified_tx.c.name,
        classified_tx.c.merchant_name,
        classified_tx.c.pfc_primary,
        classified_tx.c.pfc_detailed,
        classified_tx.c.account_id,
        classified_tx.c.bucket_id,
    ).order_by(classified_tx.c.date, classified_tx.c.transaction_id)
    async with session_factory() as session:
        rows = (await session.execute(stmt)).mappings()
        return tuple(
            ClassifiedRow(
                transaction_id=row["transaction_id"],
                date=row["date"],
                amount=row["amount"],
                name=row["name"],
                merchant_name=row["merchant_name"],
                pfc_primary=row["pfc_primary"],
                pfc_detailed=row["pfc_detailed"],
                account_id=row["account_id"],
                bucket_id=row["bucket_id"],
            )
            for row in rows
        )
