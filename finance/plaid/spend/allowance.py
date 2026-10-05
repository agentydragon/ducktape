"""Account for a configurable flexible allowance using dated Plaid purchases."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Kind(StrEnum):
    FLEXIBLE = "flexible"
    FIXED = "fixed"
    EXCLUDED = "excluded"


class Status(StrEnum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"


class PaceAlert(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    EXCEEDED = "exceeded"
    UNAVAILABLE = "unavailable"


class NamePrefix(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["name_prefix"]
    field: Literal["name", "merchant_name"]
    prefix: str = Field(min_length=2)


class NameContains(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["name_contains"]
    field: Literal["name", "merchant_name"]
    substring: str = Field(min_length=2)


class CategoryExact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["category_exact"]
    field: Literal["pfc_primary", "pfc_detailed"]
    value: str = Field(min_length=2)


class AllOf(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["all_of"]
    conditions: list[Annotated[NamePrefix | NameContains | CategoryExact, Field(discriminator="type")]] = Field(
        min_length=2
    )


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    condition: Annotated[NamePrefix | NameContains | CategoryExact | AllOf, Field(discriminator="type")]
    kind: Kind


class AllowancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    monthly_minor_units: int = Field(gt=0)
    activation_at: date
    spending_account_ids: set[str] = Field(min_length=1)
    currency: Literal["USD"] = "USD"
    rules: list[Rule] = Field(min_length=1)
    max_sync_age_hours: int = Field(default=72, ge=1, le=720)


class Transaction(BaseModel):
    """Plaid mirror transaction fields needed by the allowance calculator."""

    account_id: str
    transaction_id: str
    pending_transaction_id: str | None
    date: date
    amount: Decimal
    pending: bool
    name: str
    merchant_name: str | None
    pfc_primary: str | None
    pfc_detailed: str | None
    currency: str | None


@dataclass(frozen=True)
class Purchase:
    transaction: Transaction
    minor_units: int
    needs_review: bool


class Windows(BaseModel):
    current_credit_cycle_minor_units: int
    calendar_month_minor_units: int
    year_to_date_minor_units: int
    trailing_7_days_minor_units: int
    trailing_30_days_minor_units: int


class AllowanceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Status
    currency: str
    monthly_minor_units: int
    activation_at: date
    available_minor_units: int | None
    next_credit_at: datetime | None
    posted_minor_units: int
    pending_minor_units: int
    review_minor_units: int
    review_transaction_count: int
    unmatched_refunds_minor_units: int
    windows_minor_units: Windows | None = Field(
        description="Spend after the configured start date in each reporting window; null when unavailable."
    )
    trailing_7_daily_minor_units: int | None
    estimated_exhaustion_at: datetime | None = Field(
        description="Projected at trailing seven-day positive purchase pace, ignoring future credits; null if no recent spend."
    )
    alert_state: PaceAlert
    last_synced_at: datetime | None
    note: str | None = None
    prior_carry_minor_units: int = 0
    projected_cycle_end_minor_units: int | None = None


def month_anniversary(start: datetime, months: int) -> datetime:
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    month += 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def matches(transaction: Transaction, condition: NamePrefix | NameContains | CategoryExact | AllOf) -> bool:
    if isinstance(condition, AllOf):
        return all(matches(transaction, part) for part in condition.conditions)
    if isinstance(condition, CategoryExact):
        category = transaction.pfc_primary if condition.field == "pfc_primary" else transaction.pfc_detailed
        return category == condition.value
    name = transaction.name if condition.field == "name" else transaction.merchant_name
    if name is None:
        return False
    if isinstance(condition, NamePrefix):
        return name.casefold().startswith(condition.prefix.casefold())
    return condition.substring.casefold() in name.casefold()


def matching_rule(transaction: Transaction, rules: list[Rule]) -> Rule | None:
    return next((rule for rule in rules if matches(transaction, rule.condition)), None)


def calculate(
    policy: AllowancePolicy, transactions: list[Transaction], *, now: datetime, last_synced_at: datetime | None
) -> AllowanceView:
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    start = datetime.combine(policy.activation_at, datetime.min.time(), tzinfo=UTC)
    if start > now:
        raise ValueError("allowance start date cannot be in the future")

    credits = 0
    while month_anniversary(start, credits) <= now:
        credits += 1
    next_credit = month_anniversary(start, credits)
    superseded = {
        (transaction.account_id, transaction.pending_transaction_id)
        for transaction in transactions
        if not transaction.pending and transaction.pending_transaction_id is not None
    }
    # Keep each included purchase once, with its category and posting state.
    included: list[Purchase] = []
    unmatched = 0
    for transaction in transactions:
        if not start.date() <= transaction.date <= now.date():
            continue
        if transaction.pending and (transaction.account_id, transaction.transaction_id) in superseded:
            continue
        if transaction.currency not in (None, policy.currency):
            continue
        rule = matching_rule(transaction, policy.rules)
        if rule is not None and rule.kind != Kind.FLEXIBLE:
            continue
        amount = int((transaction.amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        # An inferred category alone cannot associate a refund with an actual discretionary purchase.
        if amount < 0 and not (rule is not None and isinstance(rule.condition, NamePrefix)):
            unmatched += -amount
            continue
        included.append(Purchase(transaction=transaction, minor_units=amount, needs_review=rule is None))

    posted = sum(p.minor_units for p in included if not p.transaction.pending)
    pending = sum(p.minor_units for p in included if p.transaction.pending)
    review = sum(p.minor_units for p in included if p.needs_review and p.minor_units > 0)
    cycle_start = month_anniversary(start, credits - 1).date()
    windows = Windows(
        current_credit_cycle_minor_units=sum(p.minor_units for p in included if p.transaction.date >= cycle_start),
        calendar_month_minor_units=sum(
            p.minor_units for p in included if p.transaction.date >= now.date().replace(day=1)
        ),
        year_to_date_minor_units=sum(p.minor_units for p in included if p.transaction.date >= date(now.year, 1, 1)),
        trailing_7_days_minor_units=sum(
            p.minor_units for p in included if p.transaction.date >= (now - timedelta(days=6)).date()
        ),
        trailing_30_days_minor_units=sum(
            p.minor_units for p in included if p.transaction.date >= (now - timedelta(days=29)).date()
        ),
    )
    trailing_positive = sum(
        max(0, p.minor_units) for p in included if p.transaction.date >= (now - timedelta(days=6)).date()
    )
    daily = trailing_positive // max(1, min(7, (now.date() - start.date()).days + 1))
    available = credits * policy.monthly_minor_units - posted - pending
    projected_end = available - daily * max(1, (next_credit.date() - now.date()).days)
    alert = PaceAlert.EXCEEDED if available <= 0 else PaceAlert.WARNING if projected_end < 0 else PaceAlert.NORMAL
    return AllowanceView(
        status=Status.ACTIVE,
        currency=policy.currency,
        monthly_minor_units=policy.monthly_minor_units,
        activation_at=policy.activation_at,
        available_minor_units=available,
        next_credit_at=next_credit,
        posted_minor_units=posted,
        pending_minor_units=pending,
        review_minor_units=review,
        review_transaction_count=sum(1 for p in included if p.needs_review and p.minor_units > 0),
        unmatched_refunds_minor_units=unmatched,
        windows_minor_units=windows,
        trailing_7_daily_minor_units=daily,
        estimated_exhaustion_at=now + timedelta(days=max(0, available) / daily) if daily else None,
        alert_state=alert,
        last_synced_at=last_synced_at,
        prior_carry_minor_units=(credits - 1) * policy.monthly_minor_units
        - (posted + pending - windows.current_credit_cycle_minor_units),
        projected_cycle_end_minor_units=projected_end,
    )
