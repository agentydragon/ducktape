"""Annual funding draw implied by observed consumption and current eligible assets."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from finance.plaid.spend.allowance import AllowancePolicy, Disposition, Kind, Transaction
from finance.plaid.spend.consumption import ConsumptionMode, prepare_consumption
from finance.plaid.spend.portfolio_models import PortfolioPolicy, PortfolioView

_YEAR_DAYS = Decimal("365.2425")


class SustainabilityDisplay(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    windows_days: list[int] = Field(default_factory=lambda: [30, 90, 365], min_length=1)
    default_window_days: int = 90
    reference_draw_rates: list[FiniteFloat] = Field(default_factory=lambda: [0.03, 0.04])

    @model_validator(mode="after")
    def _valid_windows(self) -> SustainabilityDisplay:
        if any(not 1 <= days <= 730 for days in self.windows_days) or len(set(self.windows_days)) != len(
            self.windows_days
        ):
            raise ValueError("windows must be distinct day counts between 1 and 730")
        if self.default_window_days not in self.windows_days:
            raise ValueError("default window must be configured")
        if any(not 0 < rate < 1 for rate in self.reference_draw_rates):
            raise ValueError("reference draw rates must be finite fractions between zero and one")
        return self


class WithdrawalTax(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    model: Literal["effective_rate"] = "effective_rate"
    rate: FiniteFloat | None = Field(default=None, ge=0, lt=1)
    assumption_note: str | None = None
    as_of: date_type | None = None

    @model_validator(mode="after")
    def _documented_assumption(self) -> WithdrawalTax:
        if self.rate is not None and (
            not self.assumption_note or not self.assumption_note.strip() or self.as_of is None
        ):
            raise ValueError("a tax rate requires a dated assumption note")
        if self.rate is None and (self.assumption_note is not None or self.as_of is not None):
            raise ValueError("tax provenance requires a tax rate")
        return self


class AccountOpening(BaseModel):
    """Dated evidence of account existence, independently of imported history."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    earliest: date_type
    latest: date_type
    source: str = Field(min_length=1, pattern=r"\S")
    as_of: date_type

    @model_validator(mode="after")
    def _ordered_bounds(self) -> AccountOpening:
        if self.earliest > self.latest:
            raise ValueError("account opening bounds must be ordered")
        return self


class DocumentedHistory(BaseModel):
    """Explicit assertion of complete history from a date, with provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    complete_from: date_type
    source: str = Field(min_length=1, pattern=r"\S")
    as_of: date_type

    @model_validator(mode="after")
    def _dated_coverage(self) -> DocumentedHistory:
        if self.complete_from > self.as_of:
            raise ValueError("history coverage cannot start after its evidence date")
        return self


class AccountHistory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    opening: AccountOpening | None = None
    coverage: DocumentedHistory | None = None

    @model_validator(mode="after")
    def _has_evidence(self) -> AccountHistory:
        if self.opening is None and self.coverage is None:
            raise ValueError("account history requires opening or coverage evidence")
        return self


class SustainabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    currency: Literal["USD"] = "USD"
    display: SustainabilityDisplay = Field(default_factory=SustainabilityDisplay)
    portfolio: PortfolioPolicy
    withdrawal_tax: WithdrawalTax = Field(default_factory=WithdrawalTax)
    include_excluded_categories: list[str] = Field(default_factory=list)
    confirmed_refund_rule_ids: list[str] = Field(default_factory=list)
    known_issues: list[str] = Field(default_factory=list)
    account_history: dict[str, AccountHistory] = Field(default_factory=dict)


class SpendingAccountCoverage(BaseModel):
    account_label: str
    first_recorded_on: date_type | None
    opening: AccountOpening | None
    coverage: DocumentedHistory | None
    unknown_days_in_window: int


class SustainabilityHistoryPoint(BaseModel):
    date: date_type
    covered_days: int
    annualized_spending_minor_units: int | None
    spending_draw_rate: float | None
    tax_adjusted_draw_rate: float | None


class SustainabilityView(BaseModel):
    generated_at: datetime
    status: Literal["disabled", "ready", "unavailable"]
    currency: Literal["USD"] = "USD"
    time_zone: str
    window_days: int
    available_windows_days: list[int]
    period_start: date_type
    period_end: date_type
    covered_days: int
    mandatory_minor_units: int
    flexible_minor_units: int
    provisional_minor_units: int
    spending_minor_units: int
    annualized_spending_minor_units: int | None
    estimated_annual_draw_minor_units: int | None
    spending_draw_rate: float | None
    tax_adjusted_draw_rate: float | None
    effective_tax_rate: float | None
    tax_assumption_note: str | None = None
    tax_assumption_as_of: date_type | None = None
    reference_draw_rates: list[float]
    portfolio: PortfolioView
    history: list[SustainabilityHistoryPoint]
    issues: list[str]
    known_issues: list[str]
    account_coverage: list[SpendingAccountCoverage] = Field(default_factory=list)


class SustainabilityDashboard(BaseModel):
    default_window_days: int
    windows: list[SustainabilityView]


@dataclass(frozen=True)
class Consumption:
    mandatory: int = 0
    flexible: int = 0
    provisional: int = 0

    @property
    def total(self) -> int:
        return self.mandatory + self.flexible + self.provisional


@dataclass(frozen=True)
class _AccountCoverage:
    first_recorded: date_type | None
    evidence: AccountHistory | None

    def covers(self, day: date_type) -> bool:
        if self.evidence is not None:
            if self.evidence.opening is not None and day < self.evidence.opening.earliest:
                return True
            if self.evidence.coverage is not None:
                return day >= self.evidence.coverage.complete_from
        return self.first_recorded is not None and day >= self.first_recorded


def empty_portfolio() -> PortfolioView:
    return PortfolioView(
        status="unavailable",
        net_minor_units=None,
        assets_minor_units=None,
        liabilities_minor_units=None,
        excluded_minor_units=None,
        sources=[],
    )


def disabled_view(now: datetime, time_zone: str, today: date_type) -> SustainabilityView:
    return SustainabilityView(
        generated_at=now,
        status="disabled",
        time_zone=time_zone,
        window_days=90,
        available_windows_days=[30, 90, 365],
        period_start=today - timedelta(days=90),
        period_end=today - timedelta(days=1),
        covered_days=0,
        mandatory_minor_units=0,
        flexible_minor_units=0,
        provisional_minor_units=0,
        spending_minor_units=0,
        annualized_spending_minor_units=None,
        estimated_annual_draw_minor_units=None,
        spending_draw_rate=None,
        tax_adjusted_draw_rate=None,
        effective_tax_rate=None,
        reference_draw_rates=[0.03, 0.04],
        portfolio=empty_portfolio(),
        history=[],
        issues=["Sustainability is not configured."],
        known_issues=[],
    )


def calculate_sustainability(
    policy: SustainabilityPolicy,
    allowance: AllowancePolicy,
    transactions: list[Transaction],
    portfolio: PortfolioView,
    *,
    time_zone: ZoneInfo,
    now: datetime,
    window_days: int,
    source_issues: list[str],
    account_labels: dict[str, str] | None = None,
) -> SustainabilityView:
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if window_days not in policy.display.windows_days:
        raise ValueError("window must be configured")
    today = now.astimezone(time_zone).date()
    end = today - timedelta(days=1)
    requested_start = today - timedelta(days=window_days)
    issues = list(source_issues)
    prepared = prepare_consumption(
        allowance,
        transactions,
        time_zone=time_zone,
        today=today,
        currency=policy.currency,
        include_excluded_categories=policy.include_excluded_categories,
        confirmed_refund_rule_ids=policy.confirmed_refund_rule_ids,
        mode=ConsumptionMode.NET,
    )
    issues.extend(prepared.issues)
    days_by_account: dict[str, list[date_type]] = defaultdict(list)
    daily: dict[date_type, dict[Kind, int]] = defaultdict(lambda: defaultdict(int))
    contradictory_accounts: set[str] = set()
    held_refunds = other_currency = 0
    for entry in prepared.entries:
        transaction = entry.transaction
        day = entry.report_day
        # Even a transfer establishes that a source has some recorded history.
        days_by_account[transaction.account_id].append(day)
        evidence = policy.account_history.get(transaction.account_id)
        if evidence is not None and evidence.opening is not None and day < evidence.opening.earliest:
            contradictory_accounts.add(transaction.account_id)
        if day > end:
            continue
        if entry.disposition == Disposition.OTHER_CURRENCY:
            other_currency += int(requested_start <= day <= end)
        elif entry.disposition == Disposition.HELD_REFUND:
            held_refunds += int(requested_start <= day <= end)
        elif entry.disposition == Disposition.COUNTED:
            daily[day][entry.kind] += entry.amount_minor_units

    account_coverage = []
    boundaries = []
    for index, account in enumerate(sorted(allowance.spending_account_ids), start=1):
        first_recorded = min(days_by_account[account]) if days_by_account[account] else None
        evidence = policy.account_history.get(account)
        boundary = _AccountCoverage(first_recorded, evidence)
        boundaries.append(boundary)
        label = (account_labels or {}).get(account, f"Spending account {index}")
        if account in contradictory_accounts:
            issues.append(
                f"{label}: a recorded transaction predates the earliest possible opening; "
                "resolve the evidence conflict before using funding estimates."
            )
        account_coverage.append(
            SpendingAccountCoverage(
                account_label=label,
                first_recorded_on=first_recorded,
                opening=evidence.opening if evidence else None,
                coverage=evidence.coverage if evidence else None,
                unknown_days_in_window=sum(
                    not boundary.covers(requested_start + timedelta(days=offset)) for offset in range(window_days)
                ),
            )
        )

    # Evaluate account evidence once per calendar day across the chart and its
    # lookback, then reuse the mask for the headline and every rolling window.
    first_coverage_day = today - timedelta(days=365 + window_days - 1)
    covered_dates = {
        first_coverage_day + timedelta(days=offset)
        for offset in range((today - first_coverage_day).days)
        if all(boundary.covers(first_coverage_day + timedelta(days=offset)) for boundary in boundaries)
    }
    # Only the covered suffix ending yesterday can support the current pace.
    # Summing disjoint known days would hide a gap in which spending is unknown.
    start = today
    while start > requested_start and start - timedelta(days=1) in covered_dates:
        start -= timedelta(days=1)
    covered_days = (today - start).days
    if covered_days < window_days:
        issues.append(
            f"Only {covered_days} of {window_days} days have contiguous covered history ending yesterday; "
            "annualization uses those days."
        )
    if held_refunds:
        issues.append(
            f"{held_refunds} unconfirmed incoming credits are held outside spending; refunds may overstate net spending."
        )
    if other_currency:
        issues.append(
            f"{other_currency} transactions in other currencies are not converted; spending coverage is incomplete."
        )
    if policy.withdrawal_tax.rate is None:
        issues.append("Withdrawal taxes are not modeled; tax-inclusive funding and draw rates are unavailable.")
    source_ready = not source_issues and not contradictory_accounts and other_currency == 0
    net = portfolio.net_minor_units if portfolio.status == "ready" else None
    if portfolio.status != "ready":
        issues.append("Portfolio coverage or freshness is unavailable; see source details.")
    ready = source_ready and covered_days > 0 and net is not None and net > 0
    if net is not None and net <= 0:
        issues.append("Eligible funding assets are not positive; a withdrawal percentage is unavailable.")
    totals = {kind: sum(values.get(kind, 0) for day, values in daily.items() if start <= day <= end) for kind in Kind}
    consumption = Consumption(
        mandatory=totals[Kind.FIXED],
        # Policy-included excluded categories retain their flexible treatment.
        flexible=totals[Kind.FLEXIBLE] + totals[Kind.EXCLUDED],
        provisional=totals[Kind.REVIEW],
    )
    annual = _annualize(consumption.total, covered_days) if source_ready and covered_days else None
    draw = _tax_adjust(annual, policy.withdrawal_tax.rate)
    if annual is not None and annual < 0:
        issues.append(
            "Net refunds exceed purchases in this window; do not interpret a negative pace as sustainable income."
        )
    history: list[SustainabilityHistoryPoint] = []
    # Reuse the same complete-day bounds and rounding as the headline. Every
    # historical point uses today's eligible assets, never reconstructed holdings.
    # A rolling-window series starts only once that entire window is covered.
    # Annualizing its first one or two days amplifies monthly bills into spikes.
    chart_day = today - timedelta(days=365)
    while chart_day <= end:
        point_start = chart_day - timedelta(days=window_days - 1)
        point_days = sum(point_start + timedelta(days=offset) in covered_dates for offset in range(window_days))
        point_total = sum(sum(values.values()) for day, values in daily.items() if point_start <= day <= chart_day)
        point_annual = _annualize(point_total, window_days) if source_ready and point_days == window_days else None
        point_draw = _tax_adjust(point_annual, policy.withdrawal_tax.rate)
        # Omit incomplete startup windows; retain later nulls to break the line
        # across unknown history, including a gap that reaches the present.
        if history or point_days == window_days:
            history.append(
                SustainabilityHistoryPoint(
                    date=chart_day,
                    covered_days=point_days,
                    annualized_spending_minor_units=point_annual,
                    spending_draw_rate=point_annual / net if point_annual is not None and net and net > 0 else None,
                    tax_adjusted_draw_rate=point_draw / net if point_draw is not None and net and net > 0 else None,
                )
            )
        chart_day += timedelta(days=1)
    return SustainabilityView(
        generated_at=now,
        status="ready" if ready else "unavailable",
        time_zone=time_zone.key,
        window_days=window_days,
        available_windows_days=policy.display.windows_days,
        period_start=start if covered_days else requested_start,
        period_end=end,
        covered_days=covered_days,
        mandatory_minor_units=consumption.mandatory,
        flexible_minor_units=consumption.flexible,
        provisional_minor_units=consumption.provisional,
        spending_minor_units=consumption.total,
        annualized_spending_minor_units=annual,
        estimated_annual_draw_minor_units=draw,
        spending_draw_rate=annual / net if ready and annual is not None and net else None,
        tax_adjusted_draw_rate=draw / net if ready and draw is not None and net else None,
        effective_tax_rate=policy.withdrawal_tax.rate,
        tax_assumption_note=policy.withdrawal_tax.assumption_note,
        tax_assumption_as_of=policy.withdrawal_tax.as_of,
        reference_draw_rates=policy.display.reference_draw_rates,
        portfolio=portfolio,
        history=history,
        issues=issues,
        known_issues=policy.known_issues,
        account_coverage=account_coverage,
    )


def _annualize(amount: int, days: int) -> int:
    return int((Decimal(amount) * _YEAR_DAYS / days).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _tax_adjust(annual: int | None, rate: float | None) -> int | None:
    if annual is None or rate is None:
        return None
    return int((Decimal(annual) / (1 - Decimal(str(rate)))).quantize(Decimal(1), rounding=ROUND_HALF_UP))
