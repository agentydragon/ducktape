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
    Status,
    Transaction,
    accounting_disposition,
    daily_reference,
    prepare_accounting,
    round_minor_units,
)
from finance.plaid.spend.pace_history_models import PaceHistory, PaceHistoryPoint, PaceHistoryWindow
from finance.plaid.spend.sustainability import AccountCoverage, AccountHistory

HISTORY_DAYS = 30


def pace_history(
    policy: AllowancePolicy,
    transactions: list[Transaction],
    *,
    time_zone: ZoneInfo,
    now: datetime,
    last_synced_at: datetime | None,
    account_history: dict[str, AccountHistory],
) -> PaceHistory:
    """Use today's ledger and rules to reconstruct each day's rolling purchase average.

    Values are not historical snapshots: later reconciliations and classification
    corrections can change older points. Coverage follows sustainability's
    account-evidence / first-recorded-activity inference.
    """
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(time_zone).date()
    prepared = prepare_accounting(transactions, allowance=policy, time_zone=time_zone, today=today)
    accounting = AccountingPolicy(
        currency=policy.currency, counted_kinds=frozenset({Kind.FLEXIBLE, Kind.REVIEW}), credits=CreditTreatment.HOLD
    )
    first: dict[str, date] = {}
    daily: dict[date, int] = defaultdict(int)
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
            daily[entry.report_day] += max(0, entry.amount_minor_units)
    boundaries = [
        AccountCoverage(first.get(account), account_history.get(account)) for account in policy.spending_account_ids
    ]
    windows = []
    for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D):
        days = period_id.rolling_days
        assert days is not None
        points = []
        for offset in range(HISTORY_DAYS - 1, -1, -1):
            end = today - timedelta(days=offset)
            dates = [end - timedelta(days=day) for day in range(days)]
            covered = (
                not conflicted
                and end >= policy.activation_at
                and all(boundary.covers(day) for boundary in boundaries for day in dates)
            )
            positive = sum(daily[day] for day in dates)
            observed = (
                positive // days
                if covered and positive
                else 0
                if covered and (end - policy.activation_at).days + 1 >= days
                else None
            )
            points.append(PaceHistoryPoint(date=end, observed_daily_minor_units=observed))
        windows.append(PaceHistoryWindow(period_id=period_id, points=points))
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
    )
