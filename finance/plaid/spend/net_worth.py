"""Historical USD net worth from complete sets of linked Plaid balances."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finance.plaid.db.models import AccountRow, BalanceSnapshotRow, LinkRow
from finance.plaid.spend.plaid_valuation import usd_minor_units

_DEBT_TYPES = {"credit", "loan"}
_ACCOUNT_TYPES = {"depository", "investment"} | _DEBT_TYPES
_MAX_CAPTURE_SPREAD = timedelta(hours=1)


class NetWorthTotals(BaseModel):
    assets_minor_units: int
    debt_minor_units: int
    net_minor_units: int
    observed_from: datetime
    observed_through: datetime


class NetWorthDay(BaseModel):
    date: date
    totals: NetWorthTotals | None


class NetWorthHistory(BaseModel):
    days: list[NetWorthDay]


@dataclass(frozen=True)
class _Balance:
    account_id: str
    is_debt: bool
    captured_at: datetime
    amount: Decimal


def _latest_aligned_balances(observations: list[_Balance], account_count: int) -> NetWorthTotals | None:
    latest_by_account: dict[str, _Balance] = {}
    latest_window: tuple[_Balance, ...] = ()
    for observation in observations:
        latest_by_account[observation.account_id] = observation
        if len(latest_by_account) != account_count:
            continue
        observed_from = min(balance.captured_at for balance in latest_by_account.values())
        if observation.captured_at - observed_from <= _MAX_CAPTURE_SPREAD:
            latest_window = tuple(latest_by_account.values())
    if not latest_window:
        return None

    assets = usd_minor_units(sum((b.amount for b in latest_window if not b.is_debt), Decimal(0)))
    debt = usd_minor_units(sum((b.amount for b in latest_window if b.is_debt), Decimal(0)))
    return NetWorthTotals(
        assets_minor_units=assets,
        debt_minor_units=debt,
        net_minor_units=assets - debt,
        observed_from=min(balance.captured_at for balance in latest_window),
        observed_through=max(balance.captured_at for balance in latest_window),
    )


async def read_net_worth(session: AsyncSession, *, time_zone: ZoneInfo, now: datetime) -> NetWorthHistory:
    """Use a fixed cohort of currently attached USD accounts and one total per report day.

    A day's values come from its latest complete observation window. Missing accounts
    and account link problems leave gaps without changing the cohort or filling with zero.
    """
    rows = await session.execute(
        select(AccountRow.account_id, AccountRow.type, BalanceSnapshotRow.captured_at, BalanceSnapshotRow.current)
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
    account_ids: set[str] = set()
    by_day: dict[date, list[_Balance]] = defaultdict(list)
    for account_id, account_type, captured_at, amount in rows:
        account_ids.add(account_id)
        if captured_at is not None and amount is not None:
            day = captured_at.astimezone(time_zone).date()
            by_day[day].append(_Balance(account_id, account_type in _DEBT_TYPES, captured_at, amount))

    totals_by_day = {}
    for day, observations in by_day.items():
        totals = _latest_aligned_balances(observations, len(account_ids))
        if totals is not None:
            totals_by_day[day] = totals
    if not totals_by_day:
        return NetWorthHistory(days=[])

    day = min(totals_by_day)
    last_day = max(by_day)
    days: list[NetWorthDay] = []
    while day <= last_day:
        days.append(NetWorthDay(date=day, totals=totals_by_day.get(day)))
        day += timedelta(days=1)
    return NetWorthHistory(days=days)
