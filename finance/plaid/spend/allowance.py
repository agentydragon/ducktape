"""Account for a configurable flexible allowance using dated Plaid purchases."""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Json, TypeAdapter, ValidationError


class Kind(StrEnum):
    FLEXIBLE = "flexible"
    FIXED = "fixed"
    EXCLUDED = "excluded"
    REVIEW = "review"


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
    type: Literal["name_prefix"] = "name_prefix"
    field: Literal["name", "merchant_name"]
    prefix: str = Field(min_length=2)


class NameContains(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["name_contains"] = "name_contains"
    field: Literal["name", "merchant_name"]
    substring: str = Field(min_length=2)


class CategoryExact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["category_exact"] = "category_exact"
    field: Literal["pfc_primary", "pfc_detailed"]
    value: str = Field(min_length=2)


class AmountExact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["amount_exact"] = "amount_exact"
    value: str = Field(pattern=r"^\d+(\.\d{1,4})?$")


class AmountSign(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["amount_sign"] = "amount_sign"
    sign: Literal["negative", "zero", "positive"]


class FieldExact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["field_exact"] = "field_exact"
    field: Literal["name", "merchant_name", "account_type", "merchant_category_code"]
    value: str | bool


class CounterpartyExact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["counterparty_exact"] = "counterparty_exact"
    counterparty_type: str = Field(min_length=1)
    name: str = Field(min_length=1)


class PlaidCounterparty(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    type: str | None = None
    name: str | None = None


_COUNTERPARTIES_JSON = TypeAdapter(Json[list[PlaidCounterparty]])


type SimpleCondition = (
    NamePrefix | NameContains | CategoryExact | AmountExact | AmountSign | FieldExact | CounterpartyExact
)


class AnyOf(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["any_of"] = "any_of"
    conditions: list[Annotated[SimpleCondition, Field(discriminator="type")]] = Field(min_length=2)


class AllOf(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["all_of"] = "all_of"
    conditions: list[Annotated[SimpleCondition | AnyOf, Field(discriminator="type")]] = Field(min_length=2)


type Condition = SimpleCondition | AnyOf | AllOf


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    condition: Annotated[Condition, Field(discriminator="type")]
    kind: Kind
    analysis_category: str | None = Field(default=None, min_length=1)
    description: str | None = Field(
        default=None, min_length=1, max_length=240, description="Human-readable rationale for this classification rule."
    )


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
    account_type: str | None = None
    merchant_category_code: str | None = None
    counterparties: Json[list[PlaidCounterparty]] | None = None


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
    # Recorded positive purchases, including preactivation history, divided by the full window.
    # Separate from the short-window burst-sensitive pace used for forecasting.
    trailing_7_observed_daily_minor_units: int | None
    trailing_30_observed_daily_minor_units: int | None
    trailing_7_unmatched_count: int | None
    trailing_7_unmatched_minor_units: int | None
    estimated_exhaustion_at: datetime | None = Field(
        description="Projected at trailing seven-day positive purchase pace, ignoring future credits; null if no recent spend."
    )
    alert_state: PaceAlert
    spending_signal: PaceAlert
    last_synced_at: datetime | None
    note: str | None = None
    prior_carry_minor_units: int = 0
    projected_cycle_end_minor_units: int | None = None


def month_anniversary(start: datetime, months: int) -> datetime:
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    month += 1
    return start.replace(year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1]))


def matches_fields(fields: Transaction | Mapping[str, object], condition: Condition) -> bool:
    """Match policy conditions against a mapping or a runtime model object."""

    def value(field: str) -> object | None:
        if isinstance(fields, Mapping):
            return fields.get(field)
        return getattr(fields, field, None)

    if isinstance(condition, AllOf):
        return all(matches_fields(fields, part) for part in condition.conditions)
    if isinstance(condition, AnyOf):
        return any(matches_fields(fields, part) for part in condition.conditions)
    if isinstance(condition, CategoryExact):
        category = value(condition.field)
        return category == condition.value
    if isinstance(condition, AmountExact):
        amount = value("amount")
        try:
            return amount is not None and Decimal(str(amount)) == Decimal(condition.value)
        except ArithmeticError:
            return False
    if isinstance(condition, AmountSign):
        amount = value("amount")
        try:
            number = Decimal(str(amount))
        except ArithmeticError:
            return False
        return (
            number < 0 if condition.sign == "negative" else number > 0 if condition.sign == "positive" else number == 0
        )
    if isinstance(condition, FieldExact):
        actual = value(condition.field)
        if isinstance(actual, str) and isinstance(condition.value, str):
            return actual.casefold() == condition.value.casefold()
        return actual == condition.value
    if isinstance(condition, CounterpartyExact):
        counterparties = value("counterparties")
        if isinstance(counterparties, str):
            try:
                counterparties = _COUNTERPARTIES_JSON.validate_python(counterparties)
            except ValidationError:
                return False
        return isinstance(counterparties, list) and any(
            isinstance(counterparty, PlaidCounterparty)
            and isinstance(counterparty.type, str)
            and counterparty.type.casefold() == condition.counterparty_type.casefold()
            and isinstance(counterparty.name, str)
            and counterparty.name.casefold() == condition.name.casefold()
            for counterparty in counterparties
        )
    name = value(condition.field)
    if name is None:
        return False
    if isinstance(condition, NamePrefix):
        return isinstance(name, str) and name.casefold().startswith(condition.prefix.casefold())
    return isinstance(name, str) and condition.substring.casefold() in name.casefold()


def matching_rule(transaction: Transaction | Mapping[str, object], rules: list[Rule]) -> Rule | None:
    return next((rule for rule in rules if matches_fields(transaction, rule.condition)), None)


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
    recent_positive = 0
    monthly_positive = 0
    weekly_unmatched_count = 0
    weekly_unmatched_minor_units = 0
    unmatched = 0
    pace_start = (now - timedelta(days=6)).date()
    monthly_pace_start = (now - timedelta(days=29)).date()
    for transaction in transactions:
        if not min(start.date(), monthly_pace_start) <= transaction.date <= now.date():
            continue
        if transaction.pending and (transaction.account_id, transaction.transaction_id) in superseded:
            continue
        if transaction.currency not in (None, policy.currency):
            continue
        rule = matching_rule(transaction, policy.rules)
        if rule is not None and rule.kind in (Kind.FIXED, Kind.EXCLUDED):
            continue
        amount = int((transaction.amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        # An inferred category alone cannot associate a refund with an actual discretionary purchase.
        if amount < 0 and (rule is None or rule.kind == Kind.REVIEW or not isinstance(rule.condition, NamePrefix)):
            if transaction.date >= start.date():
                unmatched += -amount
            continue
        if transaction.date >= pace_start:
            recent_positive += max(0, amount)
            if (rule is None or rule.kind == Kind.REVIEW) and amount > 0:
                weekly_unmatched_count += 1
                weekly_unmatched_minor_units += amount
        if transaction.date >= monthly_pace_start:
            monthly_positive += max(0, amount)
        if transaction.date >= start.date():
            included.append(
                Purchase(
                    transaction=transaction, minor_units=amount, needs_review=rule is None or rule.kind == Kind.REVIEW
                )
            )

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
    elapsed_days = min(7, (now.date() - start.date()).days + 1)
    since_start_positive = sum(max(0, p.minor_units) for p in included if p.transaction.date >= pace_start)
    observed_weekly = recent_positive // 7 if recent_positive else (0 if elapsed_days >= 7 else None)
    observed_monthly = (
        monthly_positive // 30 if monthly_positive else (0 if (now.date() - start.date()).days >= 29 else None)
    )
    # History can inform the pace without becoming an opening allowance debt.
    # Early post-start bursts should not disappear into the seven-day average.
    daily = max(recent_positive // 7, since_start_positive // elapsed_days) if recent_positive else None
    if daily is None and elapsed_days == 7:
        daily = 0
    available = credits * policy.monthly_minor_units - posted - pending
    projected_end = available - daily * max(1, (next_credit.date() - now.date()).days) if daily is not None else None
    alert = (
        PaceAlert.EXCEEDED
        if available <= 0
        else PaceAlert.UNAVAILABLE
        if projected_end is None
        else PaceAlert.WARNING
        if projected_end < 0
        else PaceAlert.NORMAL
    )
    reference_rate = Decimal(policy.monthly_minor_units) * 12 / Decimal("365.2425")
    signal = (
        PaceAlert.EXCEEDED
        if available <= 0
        else PaceAlert.WARNING
        if alert == PaceAlert.WARNING
        or (observed_weekly is not None and observed_weekly > reference_rate)
        or (observed_monthly is not None and observed_monthly > reference_rate)
        else PaceAlert.UNAVAILABLE
        if observed_weekly is None and observed_monthly is None
        else PaceAlert.NORMAL
    )
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
        trailing_7_observed_daily_minor_units=observed_weekly,
        trailing_30_observed_daily_minor_units=observed_monthly,
        trailing_7_unmatched_count=weekly_unmatched_count,
        trailing_7_unmatched_minor_units=weekly_unmatched_minor_units,
        estimated_exhaustion_at=now + timedelta(days=max(0, available) / daily) if daily else None,
        alert_state=alert,
        spending_signal=signal,
        last_synced_at=last_synced_at,
        prior_carry_minor_units=(credits - 1) * policy.monthly_minor_units
        - (posted + pending - windows.current_credit_cycle_minor_units),
        projected_cycle_end_minor_units=projected_end,
    )
