"""Reconstruct recorded flexible-purchase pace without inventing coverage."""

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from finance.plaid.spend.allowance import (
    AccountingPolicy,
    AllowancePolicy,
    CreditTreatment,
    Disposition,
    Kind,
    PeriodId,
    PreparedAccounting,
    Status,
    Transaction,
    accounting_disposition,
    daily_reference,
    prepare_accounting,
    round_minor_units,
)
from finance.plaid.spend.pace_history_models import PaceHistory, PaceHistoryDay, PaceHistoryPoint, PaceHistoryWindow
from finance.plaid.spend.sustainability import AccountCoverage, AccountHistory


def pace_history(
    policy: AllowancePolicy,
    transactions: list[Transaction],
    *,
    time_zone: ZoneInfo,
    now: datetime,
    last_synced_at: datetime | None,
    account_history: dict[str, AccountHistory],
    history_days: int,
    prepared: PreparedAccounting | None = None,
) -> PaceHistory:
    """Use today's ledger and rules to reconstruct each day's rolling purchase average.

    Values are not historical snapshots: later reconciliations and classification
    corrections can change older points. Coverage follows sustainability's
    account-evidence / first-recorded-activity inference.
    """
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if history_days not in (7, 30, 90, 365):
        raise ValueError("history_days must be 7, 30, 90, or 365")
    today = now.astimezone(time_zone).date()
    if prepared is None:
        prepared = prepare_accounting(transactions, allowance=policy, time_zone=time_zone, today=today)
    accounting = AccountingPolicy(
        currency=policy.currency, counted_kinds=frozenset({Kind.FLEXIBLE, Kind.REVIEW}), credits=CreditTreatment.HOLD
    )
    first: dict[str, date] = {}
    daily: dict[date, int] = defaultdict(int)
    daily_categories: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    conflicted = False
    for entry in prepared.entries:
        account = entry.transaction.account_id
        if account not in policy.spending_account_ids:
            continue
        first[account] = min(first.get(account, entry.report_day), entry.report_day)
        evidence = account_history.get(account)
        if evidence and evidence.opening and entry.report_day < evidence.opening.earliest:
            conflicted = True
        if accounting_disposition(entry, accounting) == Disposition.COUNTED:
            amount = max(0, entry.amount_minor_units)
            daily[entry.report_day] += amount
            if amount:
                category = (
                    entry.rule.analysis_category if entry.rule and entry.rule.analysis_category else "unclassified"
                )
                daily_categories[entry.report_day][category] += amount
    boundaries = [
        AccountCoverage(first.get(account), account_history.get(account)) for account in policy.spending_account_ids
    ]
    windows = []
    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D):
        days = period_id.rolling_days
        assert days is not None
        points = []
        for offset in range(history_days - 1, -1, -1):
            end = today - timedelta(days=offset)
            dates = [end - timedelta(days=day) for day in range(days)]
            covered = not conflicted and all(boundary.covers(day) for boundary in boundaries for day in dates)
            positive = sum(daily[day] for day in dates)
            observed = (
                positive // days
                if covered and positive
                else 0
                if covered and (end != today or (end - policy.activation_at).days + 1 >= days)
                else None
            )
            points.append(PaceHistoryPoint(date=end, observed_daily_minor_units=observed))
        windows.append(PaceHistoryWindow(period_id=period_id, points=points))
    daily_points = []
    for offset in range(history_days - 1, -1, -1):
        day = today - timedelta(days=offset)
        daily_points.append(
            PaceHistoryDay(
                date=day,
                categories=daily_categories.get(day, {}),
                coverage_complete=not conflicted and all(boundary.covers(day) for boundary in boundaries),
            )
        )
    used_categories = {category for point in daily_points for category in point.categories}
    issues = ["History uses current classifications and inferred account coverage; provider data can lag."]
    if conflicted:
        issues.append("A transaction predates account-opening evidence; history coverage is uncertain.")
    return PaceHistory(
        generated_at=now,
        time_zone=time_zone.key,
        currency=policy.currency,
        status=Status.ACTIVE,
        last_synced_at=last_synced_at,
        daily_reference_minor_units=round_minor_units(daily_reference(policy.monthly_minor_units), precision=0),
        windows=windows,
        issues=issues,
        daily=daily_points,
        categories={category: policy.analysis_categories[category] for category in used_categories},
    )
