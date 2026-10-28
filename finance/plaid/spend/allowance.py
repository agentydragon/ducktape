"""Account for a configurable flexible allowance using dated Plaid purchases."""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date as date_type, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, Json, TypeAdapter, ValidationError, field_validator, model_validator

from finance.plaid.spend.reconciliation import (
    ReconciliationReport,
    ReversalIssue,
    ReversalIssueCode,
    ReversalLeg,
    ReversalMatch,
    ReversalPairResolution,
    ReversalRole,
)


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

    def start(self, today: date_type, cycle_start: date_type | None = None) -> date_type:
        match self:
            case PeriodId.CREDIT_CYCLE:
                if cycle_start is None:
                    raise ValueError("credit-cycle period requires a cycle start")
                return cycle_start
            case PeriodId.CALENDAR_MONTH:
                return today.replace(day=1)
            case PeriodId.YEAR_TO_DATE:
                return date_type(today.year, 1, 1)
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
    start: date_type
    end: date_type

    @classmethod
    def for_id(cls, period_id: PeriodId, today: date_type, cycle_start: date_type | None = None) -> Period:
        return cls(id=period_id, start=period_id.start(today, cycle_start), end=today)


class UnmatchedCharges(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    amount_minor_units: int


class AllowanceSpendPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period: Period
    counted_from: date_type = Field(description="Allowance spend starts no earlier than activation.")
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
    value: str = Field(pattern=r"^-?\d+(\.\d{1,4})?$")


class AmountSign(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["amount_sign"] = "amount_sign"
    sign: Literal["negative", "zero", "positive"]


class DateRange(BaseModel):
    """Inclusive Plaid transaction-date bounds for a historical or one-off match."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["date_range"] = "date_range"
    start: date_type | None = Field(default=None, description="Inclusive first Plaid transaction date to match.")
    end: date_type | None = Field(default=None, description="Inclusive last Plaid transaction date to match.")

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
_DATETIME = TypeAdapter(datetime)


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


_MAX_ONE_OFF_WINDOW_DAYS = 31


def _is_identity(condition: Condition) -> bool:
    """Whether a condition identifies a merchant or movement, rather than only bounding a date or sign."""
    if isinstance(condition, AllOf | AnyOf):
        return any(_is_identity(part) for part in condition.conditions)
    return not isinstance(condition, DateRange | AmountSign)


def _validate_observed_selector(condition: Condition, subject: str) -> None:
    if not isinstance(condition, AllOf):
        raise ValueError(f"{subject} must be an all_of of conditions")
    window = next((part for part in condition.conditions if isinstance(part, DateRange)), None)
    if window is None or window.start is None or window.end is None:
        raise ValueError(f"{subject} requires a bounded date_range with both start and end")
    if (window.end - window.start).days > _MAX_ONE_OFF_WINDOW_DAYS:
        raise ValueError(f"{subject} window must span at most {_MAX_ONE_OFF_WINDOW_DAYS} days")
    if not any(_is_identity(part) for part in condition.conditions):
        raise ValueError(f"{subject} requires a name, category, amount, field, or counterparty condition")


class RuleScope(StrEnum):
    """What a rule claims to be about: a recurring pattern, or one observed transaction."""

    PATTERN = "pattern"
    ONE_OFF = "one_off"


class Rule(BaseModel):
    """One classification decision: a condition and the kind it assigns.

    Every decision is the same object, and precedence is its position in `AllowancePolicy.rules`: the first
    condition that matches governs the transaction. A `one_off` states a fact about one observed transaction
    and is placed above the patterns it corrects; a `pattern` states a standing rule that may be wrong for a
    given purchase. Nothing else differs between them -- the same matcher, the same kinds, the same
    accounting -- and a one-off addresses a transaction by a bounded match rather than by Plaid transaction
    id, because relinking an account rewrites those ids and silently dangles any id-addressed correction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    condition: Annotated[Condition, Field(discriminator="type")]
    kind: Kind
    scope: RuleScope = Field(
        default=RuleScope.PATTERN,
        description="One_off narrows this decision to observed transactions and requires a key and a note.",
    )
    id: str | None = Field(
        default=None,
        min_length=3,
        max_length=60,
        pattern=r"^[a-z0-9][a-z0-9._/-]*$",
        description="Stable, human-written key, required for a one_off and reported with each matched transaction.",
    )
    analysis_category: str | None = Field(default=None, min_length=1)
    description: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        description="Human-readable rationale: why this classification is right, and who confirmed it when.",
    )

    @model_validator(mode="after")
    def _scope_matches_condition(self) -> Rule:
        if self.scope == RuleScope.PATTERN:
            if self.id is not None:
                raise ValueError("only a one_off rule takes an id; a standing rule is addressed by its number")
            return self
        if self.id is None:
            raise ValueError("a one_off rule requires an id: a key to cite the correction by")
        if self.description is None:
            raise ValueError("a one_off rule requires a description: why this is the right classification")
        _validate_observed_selector(self.condition, "a one_off")
        return self


class ReversalPair(BaseModel):
    """An asserted full reversal, addressed by two bounded, uniquely matching selectors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=3, max_length=60, pattern=r"^[a-z0-9][a-z0-9._/-]*$")
    description: str = Field(min_length=1, max_length=240)
    original: AllOf
    reversal: AllOf

    @model_validator(mode="after")
    def _bounded_selectors(self) -> ReversalPair:
        _validate_observed_selector(self.original, "original selector")
        _validate_observed_selector(self.reversal, "reversal selector")
        return self


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
    activation_at: date_type
    spending_account_ids: set[str] = Field(min_length=1)
    currency: Literal["USD"] = "USD"
    rules: list[Rule] = Field(
        min_length=1, description="Ordered decisions, most specific first: the first match governs the transaction."
    )
    reversal_pairs: list[ReversalPair] = Field(
        default_factory=list, description="Confirmed full reversals, reconciled against all retained selected history."
    )
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
        keyed = [rule.id for rule in self.rules if rule.id is not None]
        if len(keyed) != len(set(keyed)):
            raise ValueError("rules must have unique ids")
        pair_ids = [pair.id for pair in self.reversal_pairs]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError("reversal pairs must have unique ids")
        missing = {
            category
            for rule in self.rules
            if (category := rule.analysis_category) is not None and category not in self.analysis_categories
        }
        if missing:
            raise ValueError(f"analysis_categories is missing rule categories: {', '.join(sorted(missing))}")
        return self


class Transaction(BaseModel):
    """Plaid mirror transaction fields needed by the allowance calculator."""

    account_id: str
    transaction_id: str
    pending_transaction_id: str | None
    date: date_type
    authorized_date: date_type | None = None
    authorized_datetime: datetime | None = None
    posted_datetime: datetime | None = None
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

    @field_validator("authorized_datetime", "posted_datetime", mode="before")
    @classmethod
    def _usable_instant(cls, value: object) -> datetime | None:
        # Plaid may omit times or supply default midnight values. A naive value
        # cannot be converted to a policy zone without inventing a source zone.
        try:
            instant = _DATETIME.validate_python(value)
        except ValidationError:
            return None
        if instant.utcoffset() is None or instant.timetz().replace(tzinfo=None) == time.min:
            return None
        return instant

    def report_day(self, zone: ZoneInfo) -> date_type:
        """Purchase day in the policy zone; date-only values are already calendar days."""
        if self.authorized_datetime is not None:
            return self.authorized_datetime.astimezone(zone).date()
        if self.authorized_date is not None:
            return self.authorized_date
        if self.posted_datetime is not None:
            return self.posted_datetime.astimezone(zone).date()
        return self.date


@dataclass(frozen=True)
class Purchase:
    transaction: Transaction
    minor_units: int
    needs_review: bool


class Disposition(StrEnum):
    COUNTED = "counted"
    PACE_ONLY = "pace_only"
    GROSS_CREDIT = "gross_credit"
    CARD_PAYMENT = "card_payment"
    FIXED = "fixed"
    EXCLUDED = "excluded"
    REVERSED = "reversed"
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
    reversal_pair: ReversalPair | None = None

    @property
    def effective_kind(self) -> Kind | None:
        """The accounting kind: confirmed reversals are excluded; otherwise the first rule's or default-flexible.

        The review tally, the transaction rows, and every client read this instead of re-deriving which
        decision won, so there is one answer to "what kind is this money" and it comes from the matcher.
        """
        if self.reversal_pair is not None:
            return Kind.EXCLUDED
        return self.rule.kind if self.rule is not None else None


class AllowanceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Status
    currency: str
    monthly_minor_units: int
    daily_reference_minor_units: int
    activation_at: date_type
    time_zone: str
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


@dataclass(frozen=True)
class AllowanceCalculation:
    view: AllowanceView
    reconciliation: ReconciliationReport


def daily_reference(monthly_minor_units: int) -> Decimal:
    """Return the unrounded monthly-credit-equivalent daily rate in minor units."""
    return Decimal(monthly_minor_units) * 12 / Decimal("365.2425")


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
                actual = date_type.fromisoformat(actual)
            except ValueError:
                return False
        return (
            isinstance(actual, date_type)
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
    """The single decision that governs a transaction: the first matching rule, with no second tier above it.

    Ordering is the whole precedence story, so a one_off correction is written where it reads -- above the
    patterns it narrows -- rather than in a list whose priority is implied by its type.
    """
    return next((rule for rule in rules if matches_fields(transaction, rule.condition)), None)


def superseded_pending_keys(transactions: list[Transaction]) -> set[tuple[str, str]]:
    return {
        (transaction.account_id, transaction.pending_transaction_id)
        for transaction in transactions
        if not transaction.pending and transaction.pending_transaction_id is not None
    }


@dataclass(frozen=True)
class ResolvedReversals:
    pairs_by_transaction: dict[tuple[str, str], ReversalPair]
    resolutions: list[ReversalPairResolution]


def resolve_reversal_pairs(
    policy: AllowancePolicy, transactions: list[Transaction], *, time_zone: ZoneInfo, today: date_type
) -> ResolvedReversals:
    """Validate assertions before windowing: a later return removes the original from every consumption window.

    Missing/ambiguous/inconsistent assertions leave ordinary conservative accounting in force. Never use
    a credit in isolation, an unposted leg, or a future-dated row as proof of a reversed purchase.
    """
    superseded = superseded_pending_keys(transactions)
    retained = {
        (transaction.account_id, transaction.transaction_id): transaction
        for transaction in transactions
        if transaction.account_id in policy.spending_account_ids
        and transaction.date <= today
        and transaction.report_day(time_zone) <= today
        and not (transaction.pending and (transaction.account_id, transaction.transaction_id) in superseded)
    }
    matches = [
        (
            {key for key, transaction in retained.items() if matches_fields(transaction, pair.original)},
            {key for key, transaction in retained.items() if matches_fields(transaction, pair.reversal)},
        )
        for pair in policy.reversal_pairs
    ]
    claims: dict[tuple[str, str], set[int]] = {}
    for index, (originals, reversals) in enumerate(matches):
        for key in originals | reversals:
            claims.setdefault(key, set()).add(index)
    pairs_by_transaction = {}
    resolutions = []
    for pair, (originals, reversals) in zip(policy.reversal_pairs, matches, strict=True):
        issues = []
        for role, keys in ((ReversalRole.ORIGINAL, originals), (ReversalRole.REVERSAL, reversals)):
            if not keys:
                issues.append(ReversalIssue(code=ReversalIssueCode.MISSING_LEG, role=role))
            elif len(keys) != 1:
                issues.append(ReversalIssue(code=ReversalIssueCode.AMBIGUOUS_MATCH, role=role))
            elif retained[next(iter(keys))].pending:
                issues.append(ReversalIssue(code=ReversalIssueCode.PENDING_LEG, role=role))
        if any(len(claims[key]) > 1 for key in originals | reversals):
            issues.append(ReversalIssue(code=ReversalIssueCode.OVERLAPPING_PAIR))
        if len(originals) == len(reversals) == 1:
            original = retained[next(iter(originals))]
            reversal = retained[next(iter(reversals))]
            if originals == reversals:
                issues.append(ReversalIssue(code=ReversalIssueCode.SAME_TRANSACTION))
            if original.amount <= 0 or reversal.amount >= 0 or original.amount != -reversal.amount:
                issues.append(ReversalIssue(code=ReversalIssueCode.AMOUNT_MISMATCH))
            if original.account_id != reversal.account_id:
                issues.append(ReversalIssue(code=ReversalIssueCode.ACCOUNT_MISMATCH))
            if (original.currency or policy.currency) != policy.currency or (
                reversal.currency or policy.currency
            ) != policy.currency:
                issues.append(ReversalIssue(code=ReversalIssueCode.CURRENCY_MISMATCH))
        if not issues:
            for key in originals | reversals:
                pairs_by_transaction[key] = pair
        resolutions.append(
            ReversalPairResolution(
                pair_id=pair.id,
                original=_reversal_leg(pair.original, originals, retained, time_zone),
                reversal=_reversal_leg(pair.reversal, reversals, retained, time_zone),
                issues=tuple(issues),
            )
        )
    return ResolvedReversals(pairs_by_transaction=pairs_by_transaction, resolutions=resolutions)


def _reversal_leg(
    selector: AllOf, keys: set[tuple[str, str]], retained: dict[tuple[str, str], Transaction], time_zone: ZoneInfo
) -> ReversalLeg:
    window = next(part for part in selector.conditions if isinstance(part, DateRange))
    # Validated by ReversalPair.
    assert window.start is not None
    assert window.end is not None
    return ReversalLeg(
        posting_start=window.start,
        posting_end=window.end,
        matches=tuple(
            ReversalMatch(account_id=key[0], transaction_id=key[1], report_day=retained[key].report_day(time_zone))
            for key in sorted(keys)
        ),
    )


class CreditTreatment(StrEnum):
    HOLD = "hold"
    CONFIRMED_REFUNDS = "confirmed_refunds"
    GROSS_PURCHASES = "gross_purchases"
    ALL = "all"


@dataclass(frozen=True)
class AccountingPolicy:
    currency: str
    counted_kinds: frozenset[Kind]
    credits: CreditTreatment
    honor_reversals: bool = True
    exclude_card_payments: bool = False
    include_excluded_categories: frozenset[str] = frozenset()
    confirmed_refund_rule_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PreparedTransaction:
    transaction: Transaction
    report_day: date_type
    rule: Rule | None
    rule_number: int | None
    amount_minor_units: int
    superseded_pending: bool
    reversal_pair: ReversalPair | None

    @property
    def kind(self) -> Kind:
        return self.rule.kind if self.rule is not None else Kind.REVIEW

    @property
    def card_payment(self) -> bool:
        return self.transaction.pfc_detailed == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"


@dataclass(frozen=True)
class PreparedAccounting:
    entries: list[PreparedTransaction]
    reconciliation: ReconciliationReport


def round_minor_units(amount: Decimal, precision: int = 2) -> int:
    quantum = Decimal(1).scaleb(-precision)
    return int(amount.quantize(quantum, rounding=ROUND_HALF_UP) * 10**precision)


def prepare_accounting(
    transactions: list[Transaction],
    *,
    time_zone: ZoneInfo,
    today: date_type,
    allowance: AllowancePolicy | None = None,
    minor_unit_precision: int = 2,
) -> PreparedAccounting:
    """Resolve transaction facts against retained history before any view windows.

    With an allowance, scope to its selected accounts and match its rules and
    reversal assertions. Statement-only callers need replacement and amount facts
    without a spending policy. Posting dates stay on the source transaction;
    report_day is the independently computed local purchase day.
    """
    selected = [t for t in transactions if allowance is None or t.account_id in allowance.spending_account_ids]
    superseded = superseded_pending_keys(selected)
    reversals = (
        resolve_reversal_pairs(allowance, selected, time_zone=time_zone, today=today)
        if allowance is not None
        else ResolvedReversals(pairs_by_transaction={}, resolutions=[])
    )
    entries = []
    for transaction in selected:
        rule = matching_rule(transaction, allowance.rules) if allowance is not None else None
        key = (transaction.account_id, transaction.transaction_id)
        entries.append(
            PreparedTransaction(
                transaction=transaction,
                report_day=transaction.report_day(time_zone),
                rule=rule,
                rule_number=(
                    next((i for i, candidate in enumerate(allowance.rules, 1) if candidate is rule), None)
                    if allowance is not None
                    else None
                ),
                amount_minor_units=round_minor_units(transaction.amount, minor_unit_precision),
                superseded_pending=transaction.pending and key in superseded,
                reversal_pair=reversals.pairs_by_transaction.get(key),
            )
        )
    return PreparedAccounting(
        entries=entries, reconciliation=ReconciliationReport(resolutions=tuple(reversals.resolutions))
    )


def accounting_disposition(entry: PreparedTransaction, policy: AccountingPolicy) -> Disposition:
    """One inclusion decision; views specify which economic effects they measure."""
    if entry.superseded_pending:
        return Disposition.SUPERSEDED_PENDING
    if policy.exclude_card_payments and entry.card_payment:
        return Disposition.CARD_PAYMENT
    if entry.transaction.currency not in (None, policy.currency):
        return Disposition.OTHER_CURRENCY
    if policy.honor_reversals and entry.reversal_pair is not None:
        return Disposition.REVERSED
    if entry.kind not in policy.counted_kinds:
        if entry.kind == Kind.FIXED:
            return Disposition.FIXED
        if entry.kind != Kind.EXCLUDED or (
            entry.rule is None or entry.rule.analysis_category not in policy.include_excluded_categories
        ):
            return Disposition.EXCLUDED
    if entry.amount_minor_units < 0:
        if policy.credits == CreditTreatment.GROSS_PURCHASES:
            return Disposition.GROSS_CREDIT
        if policy.credits == CreditTreatment.HOLD or (
            policy.credits == CreditTreatment.CONFIRMED_REFUNDS
            and (entry.rule is None or entry.rule.id not in policy.confirmed_refund_rule_ids)
        ):
            return Disposition.HELD_REFUND
    return Disposition.COUNTED


def calculate(
    policy: AllowancePolicy,
    transactions: list[Transaction],
    *,
    time_zone: ZoneInfo,
    now: datetime,
    last_synced_at: datetime | None,
    decisions: list[TransactionDecision] | None = None,
    estimate_period_id: EstimatePeriodId | None = None,
) -> AllowanceCalculation:
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(UTC)
    zone = time_zone
    local_now = now.astimezone(zone)
    start = datetime.combine(policy.activation_at, datetime.min.time(), tzinfo=zone)
    if start > now:
        raise ValueError("allowance start date cannot be in the future")

    credits = 0
    while month_anniversary(start, credits) <= now:
        credits += 1
    next_credit = month_anniversary(start, credits)
    prepared = prepare_accounting(transactions, allowance=policy, time_zone=zone, today=local_now.date())
    accounting_policy = AccountingPolicy(
        currency=policy.currency, counted_kinds=frozenset({Kind.FLEXIBLE, Kind.REVIEW}), credits=CreditTreatment.HOLD
    )
    # Keep each included purchase once, with its category and posting state.
    included: list[Purchase] = []
    rolling_periods = tuple(period_id for period_id in PeriodId if period_id.rolling_days is not None)
    today = local_now.date()
    rolling_starts = {period_id: period_id.start(today) for period_id in rolling_periods}
    positive_by_period = dict.fromkeys(rolling_periods, 0)
    unmatched_count_by_period = dict.fromkeys(rolling_periods, 0)
    unmatched_minor_units_by_period = dict.fromkeys(rolling_periods, 0)
    unmatched = 0

    def record(
        entry: PreparedTransaction,
        rule: Rule | None,
        disposition: Disposition,
        *,
        allowance_minor_units: int = 0,
        pace_effects_minor_units: dict[PeriodId, int] | None = None,
        reversal_pair: ReversalPair | None = None,
    ) -> None:
        if decisions is not None:
            decisions.append(
                TransactionDecision(
                    transaction=entry.transaction,
                    rule_number=entry.rule_number if rule is not None else None,
                    rule=rule,
                    disposition=disposition,
                    allowance_minor_units=allowance_minor_units,
                    pace_effects_minor_units=pace_effects_minor_units or dict.fromkeys(rolling_periods, 0),
                    reversal_pair=reversal_pair,
                )
            )

    for entry in prepared.entries:
        transaction = entry.transaction
        day = entry.report_day
        if not min(start.date(), *rolling_starts.values()) <= day <= today:
            continue
        disposition = accounting_disposition(entry, accounting_policy)
        amount = entry.amount_minor_units
        rule = entry.rule
        if disposition != Disposition.COUNTED:
            if disposition == Disposition.HELD_REFUND and day >= start.date():
                unmatched += -amount
            # Preserve the API's absence of a rule on transport-ineligible rows.
            reported_rule = (
                None if disposition in (Disposition.SUPERSEDED_PENDING, Disposition.OTHER_CURRENCY) else rule
            )
            record(
                entry,
                reported_rule,
                disposition,
                reversal_pair=entry.reversal_pair if disposition == Disposition.REVERSED else None,
            )
            continue
        pace_effects = {
            period_id: max(0, amount) if day >= period_start else 0
            for period_id, period_start in rolling_starts.items()
        }
        for period_id, effect in pace_effects.items():
            positive_by_period[period_id] += effect
            if effect and (rule is None or rule.kind == Kind.REVIEW):
                unmatched_count_by_period[period_id] += 1
                unmatched_minor_units_by_period[period_id] += effect
        if day >= start.date():
            included.append(
                Purchase(
                    transaction=transaction, minor_units=amount, needs_review=rule is None or rule.kind == Kind.REVIEW
                )
            )
        record(
            entry,
            rule,
            Disposition.COUNTED if day >= start.date() else Disposition.PACE_ONLY,
            allowance_minor_units=amount if day >= start.date() else 0,
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
            spend_minor_units=sum(p.minor_units for p in included if p.transaction.report_day(zone) >= period.start),
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
        max(0, p.minor_units) for p in included if p.transaction.report_day(zone) >= rolling_starts[forecast_basis]
    )
    # History can inform the pace without becoming an opening allowance debt.
    # Early post-start bursts should not disappear into the full-window average.
    daily = max(recent_positive // basis_days, since_start_positive // elapsed_days) if recent_positive else None
    if daily is None and elapsed_days == basis_days:
        daily = 0
    available = credits * policy.monthly_minor_units - posted - pending
    projected_end = available - daily * max(1, (next_credit.date() - today).days) if daily is not None else None
    alert = (
        PaceAlert.EXCEEDED
        if available <= 0
        else PaceAlert.UNAVAILABLE
        if projected_end is None
        else PaceAlert.WARNING
        if projected_end < 0
        else PaceAlert.NORMAL
    )
    reference_rate = daily_reference(policy.monthly_minor_units)
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
    reconciliation = prepared.reconciliation.for_period(start=min(start.date(), *rolling_starts.values()), end=today)
    view = AllowanceView(
        status=Status.ACTIVE,
        currency=policy.currency,
        monthly_minor_units=policy.monthly_minor_units,
        daily_reference_minor_units=round_minor_units(reference_rate, precision=0),
        activation_at=policy.activation_at,
        time_zone=zone.key,
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
    return AllowanceCalculation(view=view, reconciliation=reconciliation)
