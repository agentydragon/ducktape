"""Account for a configurable flexible allowance using dated Plaid purchases."""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, Json, TypeAdapter, ValidationError, field_validator, model_validator


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


class PeriodId(StrEnum):
    CREDIT_CYCLE = "credit_cycle"
    CALENDAR_MONTH = "calendar_month"
    YEAR_TO_DATE = "year_to_date"
    ROLLING_7D = "rolling_7d"
    ROLLING_30D = "rolling_30d"

    @property
    def rolling_days(self) -> int | None:
        return {self.ROLLING_7D: 7, self.ROLLING_30D: 30}.get(self)

    def start(self, today: date, cycle_start: date | None = None) -> date:
        match self:
            case PeriodId.CREDIT_CYCLE:
                if cycle_start is None:
                    raise ValueError("credit-cycle period requires a cycle start")
                return cycle_start
            case PeriodId.CALENDAR_MONTH:
                return today.replace(day=1)
            case PeriodId.YEAR_TO_DATE:
                return date(today.year, 1, 1)
            case PeriodId.ROLLING_7D | PeriodId.ROLLING_30D:
                days = self.rolling_days
                assert days is not None
                return today - timedelta(days=days - 1)
        raise ValueError(f"unsupported period: {self}")


type TransactionPeriodId = Literal[PeriodId.CREDIT_CYCLE, PeriodId.ROLLING_7D, PeriodId.ROLLING_30D]
type EstimatePeriodId = Literal[PeriodId.ROLLING_7D, PeriodId.ROLLING_30D]


class Period(BaseModel):
    """Inclusive calendar dates for a spend, pace, or forecast report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: PeriodId
    start: date
    end: date

    @classmethod
    def for_id(cls, period_id: PeriodId, today: date, cycle_start: date | None = None) -> Period:
        return cls(id=period_id, start=period_id.start(today, cycle_start), end=today)


class UnmatchedCharges(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    amount_minor_units: int


class AllowanceSpendPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period: Period
    counted_from: date = Field(description="Allowance spend starts no earlier than activation.")
    spend_minor_units: int


class RecordedPacePeriod(BaseModel):
    """Positive recorded purchases can include history before allowance activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    period: Period
    observed_daily_minor_units: int | None
    unmatched_charges: UnmatchedCharges | None = None


class ForecastView(BaseModel):
    """Burst-adjusted estimate with the period that supplied its purchase history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    basis_period: Period
    daily_pace_minor_units: int | None
    projected_cycle_end_minor_units: int | None
    estimated_exhaustion_at: datetime | None
    alert_state: PaceAlert


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


class DateRange(BaseModel):
    """Inclusive Plaid transaction-date bounds for a historical or one-off match."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["date_range"] = "date_range"
    start: date | None = Field(default=None, description="Inclusive first Plaid transaction date to match.")
    end: date | None = Field(default=None, description="Inclusive last Plaid transaction date to match.")

    @model_validator(mode="after")
    def _valid_bounds(self) -> DateRange:
        if self.start is None and self.end is None:
            raise ValueError("date range requires a start or end")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("date range start must not be after end")
        return self


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


class PlaidCounterpartyBacs(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    account: str | None = None
    sort_code: str | None = None


class PlaidCounterpartyInternational(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    iban: str | None = None
    bic: str | None = None


class PlaidCounterpartyNumbers(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    bacs: PlaidCounterpartyBacs | None = None
    international: PlaidCounterpartyInternational | None = None


class PlaidCounterparty(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    type: str | None = None
    name: str | None = None
    entity_id: str | None = None
    website: str | None = None
    logo_url: str | None = None
    confidence_level: str | None = None
    account_numbers: PlaidCounterpartyNumbers | None = None


_COUNTERPARTIES_JSON = TypeAdapter(Json[list[PlaidCounterparty]])


type SimpleCondition = (
    NamePrefix | NameContains | CategoryExact | AmountExact | AmountSign | DateRange | FieldExact | CounterpartyExact
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


class AnalysisCategory(BaseModel):
    """Configured display metadata for a stable analysis-category key."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    label: str = Field(min_length=1, max_length=80)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("category labels must not be blank")
        return value


class AllowancePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    monthly_minor_units: int = Field(gt=0)
    activation_at: date
    spending_account_ids: set[str] = Field(min_length=1)
    currency: Literal["USD"] = "USD"
    rules: list[Rule] = Field(min_length=1)
    max_sync_age_hours: int = Field(default=72, ge=1, le=720)
    forecast_basis_period_id: PeriodId = PeriodId.ROLLING_7D
    analysis_categories: dict[str, AnalysisCategory] = Field(
        min_length=1, description="Display labels and colors keyed by rule analysis_category; includes unclassified."
    )

    @field_validator("forecast_basis_period_id")
    @classmethod
    def _rolling_forecast_basis(cls, period_id: PeriodId) -> PeriodId:
        if period_id.rolling_days is None:
            raise ValueError("forecast basis must be a rolling period")
        return period_id

    @model_validator(mode="after")
    def _configured_analysis_categories(self) -> AllowancePolicy:
        if any(not category or category.strip() != category for category in self.analysis_categories):
            raise ValueError("analysis category keys must be nonblank and trimmed")
        if "unclassified" not in self.analysis_categories:
            raise ValueError("analysis_categories must define the reserved unclassified category")
        missing = {
            rule.analysis_category
            for rule in self.rules
            if rule.analysis_category is not None and rule.analysis_category not in self.analysis_categories
        }
        if missing:
            raise ValueError(f"analysis_categories is missing rule categories: {', '.join(sorted(missing))}")
        return self


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


class Disposition(StrEnum):
    COUNTED = "counted"
    PACE_ONLY = "pace_only"
    FIXED = "fixed"
    EXCLUDED = "excluded"
    HELD_REFUND = "held_refund"
    SUPERSEDED_PENDING = "superseded_pending"
    OTHER_CURRENCY = "other_currency"


@dataclass(frozen=True)
class TransactionDecision:
    transaction: Transaction
    rule_number: int | None
    rule: Rule | None
    disposition: Disposition
    allowance_minor_units: int
    pace_effects_minor_units: dict[PeriodId, int]


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
    spend_periods: list[AllowanceSpendPeriod]
    recorded_pace_periods: list[RecordedPacePeriod]
    forecast: ForecastView
    spending_signal: PaceAlert
    last_synced_at: datetime | None
    note: str | None = None
    prior_carry_minor_units: int = 0


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
    if isinstance(condition, DateRange):
        actual = value("date")
        if isinstance(actual, datetime):
            actual = actual.date()
        elif isinstance(actual, str):
            try:
                actual = date.fromisoformat(actual)
            except ValueError:
                return False
        return (
            isinstance(actual, date)
            and (condition.start is None or actual >= condition.start)
            and (condition.end is None or actual <= condition.end)
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
    policy: AllowancePolicy,
    transactions: list[Transaction],
    *,
    now: datetime,
    last_synced_at: datetime | None,
    decisions: list[TransactionDecision] | None = None,
    estimate_period_id: EstimatePeriodId | None = None,
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
    rolling_periods = tuple(period_id for period_id in PeriodId if period_id.rolling_days is not None)
    today = now.date()
    rolling_starts = {period_id: period_id.start(today) for period_id in rolling_periods}
    positive_by_period = dict.fromkeys(rolling_periods, 0)
    unmatched_count_by_period = dict.fromkeys(rolling_periods, 0)
    unmatched_minor_units_by_period = dict.fromkeys(rolling_periods, 0)
    unmatched = 0

    def record(
        transaction: Transaction,
        rule: Rule | None,
        disposition: Disposition,
        *,
        allowance_minor_units: int = 0,
        pace_effects_minor_units: dict[PeriodId, int] | None = None,
    ) -> None:
        if decisions is not None:
            decisions.append(
                TransactionDecision(
                    transaction=transaction,
                    rule_number=next((i for i, candidate in enumerate(policy.rules, 1) if candidate is rule), None),
                    rule=rule,
                    disposition=disposition,
                    allowance_minor_units=allowance_minor_units,
                    pace_effects_minor_units=pace_effects_minor_units or dict.fromkeys(rolling_periods, 0),
                )
            )

    for transaction in transactions:
        if not min(start.date(), *rolling_starts.values()) <= transaction.date <= today:
            continue
        if transaction.pending and (transaction.account_id, transaction.transaction_id) in superseded:
            record(transaction, None, Disposition.SUPERSEDED_PENDING)
            continue
        if transaction.currency not in (None, policy.currency):
            record(transaction, None, Disposition.OTHER_CURRENCY)
            continue
        rule = matching_rule(transaction, policy.rules)
        if rule is not None and rule.kind in (Kind.FIXED, Kind.EXCLUDED):
            record(transaction, rule, Disposition.FIXED if rule.kind == Kind.FIXED else Disposition.EXCLUDED)
            continue
        amount = int((transaction.amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        # A merchant rule does not prove which earlier purchase a credit reverses.
        if amount < 0:
            if transaction.date >= start.date():
                unmatched += -amount
            record(transaction, rule, Disposition.HELD_REFUND)
            continue
        pace_effects = {
            period_id: max(0, amount) if transaction.date >= period_start else 0
            for period_id, period_start in rolling_starts.items()
        }
        for period_id, effect in pace_effects.items():
            positive_by_period[period_id] += effect
            if effect and (rule is None or rule.kind == Kind.REVIEW):
                unmatched_count_by_period[period_id] += 1
                unmatched_minor_units_by_period[period_id] += effect
        if transaction.date >= start.date():
            included.append(
                Purchase(
                    transaction=transaction, minor_units=amount, needs_review=rule is None or rule.kind == Kind.REVIEW
                )
            )
        record(
            transaction,
            rule,
            Disposition.COUNTED if transaction.date >= start.date() else Disposition.PACE_ONLY,
            allowance_minor_units=amount if transaction.date >= start.date() else 0,
            pace_effects_minor_units=pace_effects,
        )

    posted = sum(p.minor_units for p in included if not p.transaction.pending)
    pending = sum(p.minor_units for p in included if p.transaction.pending)
    review = sum(p.minor_units for p in included if p.needs_review and p.minor_units > 0)
    cycle_start = month_anniversary(start, credits - 1).date()
    spend_periods = [
        AllowanceSpendPeriod(
            period=period,
            counted_from=max(period.start, policy.activation_at),
            spend_minor_units=sum(p.minor_units for p in included if p.transaction.date >= period.start),
        )
        for period in (Period.for_id(period_id, today, cycle_start) for period_id in PeriodId)
    ]
    days_since_start = (today - start.date()).days + 1
    recorded_pace_periods: list[RecordedPacePeriod] = []
    for period_id in rolling_periods:
        days = period_id.rolling_days
        assert days is not None
        positive = positive_by_period[period_id]
        observed = positive // days if positive else 0 if days_since_start >= days else None
        recorded_pace_periods.append(
            RecordedPacePeriod(
                period=Period.for_id(period_id, today),
                observed_daily_minor_units=observed,
                unmatched_charges=UnmatchedCharges(
                    count=unmatched_count_by_period[period_id],
                    amount_minor_units=unmatched_minor_units_by_period[period_id],
                ),
            )
        )
    forecast_basis = estimate_period_id or policy.forecast_basis_period_id
    basis_days = forecast_basis.rolling_days
    assert basis_days is not None
    recent_positive = positive_by_period[forecast_basis]
    elapsed_days = min(basis_days, days_since_start)
    since_start_positive = sum(
        max(0, p.minor_units) for p in included if p.transaction.date >= rolling_starts[forecast_basis]
    )
    # History can inform the pace without becoming an opening allowance debt.
    # Early post-start bursts should not disappear into the full-window average.
    daily = max(recent_positive // basis_days, since_start_positive // elapsed_days) if recent_positive else None
    if daily is None and elapsed_days == basis_days:
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
    basis_observed = next(
        period.observed_daily_minor_units for period in recorded_pace_periods if period.period.id == forecast_basis
    )
    signal = (
        PaceAlert.EXCEEDED
        if available <= 0
        else PaceAlert.WARNING
        if alert == PaceAlert.WARNING or (basis_observed is not None and basis_observed > reference_rate)
        else PaceAlert.UNAVAILABLE
        if basis_observed is None
        else PaceAlert.NORMAL
    )
    current_cycle_spend = next(
        report.spend_minor_units for report in spend_periods if report.period.id == PeriodId.CREDIT_CYCLE
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
        spend_periods=spend_periods,
        recorded_pace_periods=recorded_pace_periods,
        forecast=ForecastView(
            basis_period=Period.for_id(forecast_basis, today),
            daily_pace_minor_units=daily,
            projected_cycle_end_minor_units=projected_end,
            estimated_exhaustion_at=now + timedelta(days=max(0, available) / daily) if daily else None,
            alert_state=alert,
        ),
        spending_signal=signal,
        last_synced_at=last_synced_at,
        prior_carry_minor_units=(credits - 1) * policy.monthly_minor_units - (posted + pending - current_cycle_spend),
    )
