"""Historical USD net worth from complete sets of linked Plaid balances."""

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.plaid.db.models import AccountRow, BalanceSnapshotRow, LinkRow
from finance.plaid.spend.account_history import AccountHistory
from finance.plaid.spend.allowance import Transaction
from finance.plaid.spend.plaid_valuation import usd_minor_units
from finance.plaid.spend.policy_queries import select_policy_transactions

_DEBT_TYPES = {"credit", "loan"}
_ACCOUNT_TYPES = {"depository", "investment"} | _DEBT_TYPES
_MAX_CAPTURE_SPREAD = timedelta(hours=1)


class NetWorthTotals(BaseModel):
    assets_minor_units: int
    debt_minor_units: int
    net_minor_units: int
    observed_from: datetime
    observed_through: datetime
    inferred_zero_account_count: int = 0


class NetWorthDay(BaseModel):
    date: date
    totals: NetWorthTotals | None
    account_changes_minor_units: list[int] | None = None


class NetWorthHistory(BaseModel):
    days: list[NetWorthDay]
    account_labels: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _Balance:
    account_id: str
    is_debt: bool
    captured_at: datetime
    amount: Decimal

    @property
    def contribution(self) -> Decimal:
        return -self.amount if self.is_debt else self.amount


def _latest_aligned_balances(observations: list[_Balance], required_accounts: set[str]) -> dict[str, _Balance] | None:
    latest_by_account: dict[str, _Balance] = {}
    latest_window: dict[str, _Balance] | None = None
    for observation in observations:
        latest_by_account[observation.account_id] = observation
        in_window = {
            account: balance
            for account, balance in latest_by_account.items()
            if observation.captured_at - balance.captured_at <= _MAX_CAPTURE_SPREAD
        }
        if required_accounts <= in_window.keys():
            latest_window = in_window
    return latest_window


def _summarize_balances(balances: Mapping[str, _Balance], account_count: int) -> NetWorthTotals:
    latest_window = balances.values()
    assets = usd_minor_units(sum((b.amount for b in latest_window if not b.is_debt), Decimal(0)))
    debt = usd_minor_units(sum((b.amount for b in latest_window if b.is_debt), Decimal(0)))
    return NetWorthTotals(
        assets_minor_units=assets,
        debt_minor_units=debt,
        net_minor_units=assets - debt,
        observed_from=min(balance.captured_at for balance in latest_window),
        observed_through=max(balance.captured_at for balance in latest_window),
        inferred_zero_account_count=account_count - len(latest_window),
    )


def _account_labels(names: list[str]) -> list[str]:
    counts = Counter(names)
    occurrences: Counter[str] = Counter()
    labels = []
    for name in names:
        occurrences[name] += 1
        labels.append(f"{name} ({occurrences[name]})" if counts[name] > 1 else name)
    return labels


def _zero_before(evidence: AccountHistory, *, account_type: str, first_activity: date | None) -> date | None:
    """Find the first day that requires a balance, using applicable account evidence."""
    opening = evidence.opening
    if opening is None or (first_activity is not None and first_activity < opening.earliest):
        return None

    coverage = evidence.coverage
    if (
        account_type == "credit"
        and coverage is not None
        and coverage.complete_from <= opening.earliest <= coverage.as_of
        and first_activity is not None
        and first_activity > opening.earliest
    ):
        # A credit account with complete history from opening has no
        # balance before its first activity, through the evidence date.
        return min(first_activity - timedelta(days=1), coverage.as_of) + timedelta(days=1)
    return opening.earliest


async def read_net_worth(
    session: AsyncSession, *, time_zone: ZoneInfo, now: datetime, account_history: Mapping[str, AccountHistory]
) -> NetWorthHistory:
    """Value currently attached USD accounts from aligned balances and dated opening evidence."""
    rows = await session.execute(
        select(
            AccountRow.account_id,
            AccountRow.type,
            AccountRow.name,
            LinkRow.institution_name,
            BalanceSnapshotRow.captured_at,
            BalanceSnapshotRow.current,
        )
        .join(LinkRow, LinkRow.item_id == AccountRow.item_id)
        .outerjoin(
            BalanceSnapshotRow,
            and_(
                BalanceSnapshotRow.account_id == AccountRow.account_id,
                BalanceSnapshotRow.item_id == AccountRow.item_id,
                BalanceSnapshotRow.iso_currency_code == "USD",
                BalanceSnapshotRow.current.is_not(None),
                BalanceSnapshotRow.captured_at <= now,
            ),
        )
        .where(
            LinkRow.status != "revoked",
            AccountRow.detached_at.is_(None),
            AccountRow.iso_currency_code == "USD",
            AccountRow.type.in_(_ACCOUNT_TYPES),
        )
        .order_by(BalanceSnapshotRow.captured_at, BalanceSnapshotRow.id)
    )
    account_types: dict[str, str] = {}
    account_names: dict[str, str] = {}
    first_activity: dict[str, date] = {}
    by_day: dict[date, list[_Balance]] = defaultdict(list)
    for account_id, account_type, name, institution, captured_at, amount in rows:
        account_types[account_id] = account_type
        account_names[account_id] = f"{institution} · {name}" if institution and institution != name else name
        if captured_at is not None and amount is not None:
            day = captured_at.astimezone(time_zone).date()
            by_day[day].append(_Balance(account_id, account_type in _DEBT_TYPES, captured_at, amount))
            if account_id in account_history and amount:
                first_activity.setdefault(account_id, day)

    dated_accounts = sorted(
        account
        for account in account_types
        if account in account_history and account_history[account].opening is not None
    )
    if dated_accounts:
        ledger = await session.execute(
            select_policy_transactions(dated_accounts, include_raw_json=False, active_only=False)
        )
        for row in ledger.mappings():
            transaction = Transaction.model_validate(dict(row))
            # Either an earlier authorization or an earlier posting conflicts
            # with a zero inferred from account opening or first activity.
            day = min(transaction.date, transaction.report_day(time_zone))
            account = transaction.account_id
            first_activity[account] = min(day, first_activity.get(account, day))

    zero_before = {
        account: cutoff
        for account in dated_accounts
        if (
            cutoff := _zero_before(
                account_history[account],
                account_type=account_types[account],
                first_activity=first_activity.get(account),
            )
        )
        is not None
    }
    aligned_by_day: dict[date, dict[str, _Balance]] = {}
    for day, observations in by_day.items():
        required_accounts = account_types.keys() - {account for account, cutoff in zero_before.items() if day < cutoff}
        aligned = _latest_aligned_balances(observations, required_accounts)
        if aligned is not None:
            aligned_by_day[day] = aligned
    if not aligned_by_day:
        return NetWorthHistory(days=[])

    account_ids = sorted(account_names, key=lambda account_id: (account_names[account_id].casefold(), account_id))
    labels = _account_labels([account_names[account_id] for account_id in account_ids])
    day = min(aligned_by_day)
    last_day = max(by_day)
    days: list[NetWorthDay] = []
    previous_amounts: list[Decimal] | None = None
    while day <= last_day:
        balances = aligned_by_day.get(day)
        if balances is None:
            days.append(NetWorthDay(date=day, totals=None))
            previous_amounts = None
        else:
            amounts = [
                balance.contribution if (balance := balances.get(account_id)) is not None else Decimal(0)
                for account_id in account_ids
            ]
            changes = (
                [
                    usd_minor_units(current - previous)
                    for current, previous in zip(amounts, previous_amounts, strict=True)
                ]
                if previous_amounts is not None
                else None
            )
            days.append(
                NetWorthDay(
                    date=day,
                    totals=_summarize_balances(balances, len(account_ids)),
                    account_changes_minor_units=changes,
                )
            )
            previous_amounts = amounts
        day += timedelta(days=1)
    return NetWorthHistory(days=days, account_labels=labels)
