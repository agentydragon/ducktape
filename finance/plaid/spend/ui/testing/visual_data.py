"""Synthetic response data for Spend visual scenarios."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from finance.plaid.spend.allowance import (
    AllOf,
    AllowanceSpendPeriod,
    AllowanceView,
    AmountExact,
    AmountSign,
    AnalysisCategory,
    AnyOf,
    DateRange,
    Disposition,
    EstimatePeriodId,
    FieldExact,
    ForecastView,
    Kind,
    NameContains,
    NamePrefix,
    PaceAlert,
    Period,
    PeriodId,
    PlaidCounterparty,
    RecordedPacePeriod,
    ReversalPair,
    Rule,
    RuleScope,
    Status,
    TransactionPeriodId,
    UnmatchedCharges,
)
from finance.plaid.spend.models import (
    AlertState,
    AllowanceConfigurationView,
    AnalysisCategoryView,
    CardConfigurationView,
    CardView,
    ExcludedFlowSummary,
    PaceEffect,
    PlaidPaymentMeta,
    PlaidPersonalFinanceCategory,
    PlaidTransactionDetails,
    PlaidTransactionLocation,
    ProvisionalCardPeriod,
    SpendConfigurationView,
    SpendTransactionRow,
    SpendTransactionsView,
    SpendView,
    StatementCycle,
    StatementReason,
    TransactionPeriodSummary,
)
from finance.plaid.spend.portfolio_models import PortfolioView, SourceValuation
from finance.plaid.spend.reconciliation import ReportStatus
from finance.plaid.spend.sustainability import (
    AccountOpening,
    DocumentedHistory,
    SpendingAccountCoverage,
    SustainabilityDashboard,
    SustainabilityHistoryPoint,
    SustainabilityView,
)


def sustainability(variant: str = "ready", *, window_days: int = 90) -> SustainabilityView:
    stale = variant == "stale"
    unavailable = variant == "unavailable"
    is_provisional = variant == "provisional"
    tax_unmodeled = variant == "tax_unmodeled"
    low_draw = variant == "low_draw"
    portfolio_unavailable = stale or unavailable
    portfolio_net = None if portfolio_unavailable else 125_000_000
    annualized_spending = 600_621 if low_draw else 4_951_065
    effective_tax_rate = None if tax_unmodeled else Decimal("0.12")

    def rates_for(annualized: int) -> tuple[Decimal | None, Decimal | None]:
        if portfolio_net is None:
            return None, None
        if effective_tax_rate is None:
            return Decimal(annualized) / Decimal(portfolio_net), None
        tax_adjusted = int((Decimal(annualized) / (Decimal(1) - effective_tax_rate)).quantize(Decimal(1)))
        return Decimal(annualized) / Decimal(portfolio_net), Decimal(tax_adjusted) / Decimal(portfolio_net)

    history: list[SustainabilityHistoryPoint] = []
    history_values = (
        (570_000, 585_000, annualized_spending) if low_draw else (4_700_000, 4_850_000, annualized_spending)
    )
    for day, annualized in zip(("2026-10-12", "2026-10-13", "2026-10-14"), history_values, strict=True):
        spending_rate, tax_adjusted_rate = rates_for(annualized)
        if is_provisional and day == "2026-10-14":
            point_status = ReportStatus.PROVISIONAL
        elif portfolio_unavailable:
            point_status = ReportStatus.UNAVAILABLE
        else:
            point_status = ReportStatus.READY
        history.append(
            SustainabilityHistoryPoint(
                date=date.fromisoformat(day),
                status=point_status,
                covered_days=90,
                annualized_spending_minor_units=annualized,
                spending_draw_rate=spending_rate,
                tax_adjusted_draw_rate=tax_adjusted_rate,
            )
        )

    spending_draw_rate, tax_adjusted_draw_rate = rates_for(annualized_spending)
    estimated_annual_draw = (
        None
        if portfolio_unavailable or effective_tax_rate is None
        else int((Decimal(annualized_spending) / (Decimal(1) - effective_tax_rate)).quantize(Decimal(1)))
    )
    mandatory, flexible, provisional, total = (
        (92_000, 50_000, 6_000, 148_000) if low_draw else (760_000, 410_000, 50_000, 1_220_000)
    )
    if is_provisional:
        view_status = ReportStatus.PROVISIONAL
    elif portfolio_unavailable:
        view_status = ReportStatus.UNAVAILABLE
    else:
        view_status = ReportStatus.READY
    if is_provisional:
        issues = ["Review the unverified reversal and confirm its classification before relying on this estimate."]
    elif stale or unavailable:
        issues = ["Synthetic source has delayed updates."]
    else:
        issues = []
    portfolio = PortfolioView(
        status="unavailable" if portfolio_unavailable else "ready",
        net_minor_units=portfolio_net,
        assets_minor_units=None if portfolio_unavailable else 145_000_000,
        liabilities_minor_units=None if portfolio_unavailable else 15_000_000,
        excluded_minor_units=None if portfolio_unavailable else 5_000_000,
        sources=[
            SourceValuation(
                name="Synthetic investment source",
                status="unavailable" if portfolio_unavailable else "ready",
                amount_minor_units=None if portfolio_unavailable else 125_000_000,
                as_of=datetime(2026, 8, 30, 12, tzinfo=UTC) if stale else datetime(2026, 10, 14, 12, tzinfo=UTC),
                issues=["Synthetic valuation has not refreshed recently."] if stale else [],
            ),
            SourceValuation(
                name="Synthetic cash source",
                status="unavailable" if unavailable else "ready",
                amount_minor_units=None if unavailable else 20_000_000,
                as_of=datetime(2026, 10, 14, 12, tzinfo=UTC),
                issues=[],
            ),
        ],
    )
    tax_assumption_note = (
        None
        if tax_unmodeled
        else "Synthetic fixture tax assumption; actual tax treatment depends on account and income details."
    )
    return SustainabilityView(
        generated_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
        status=view_status,
        currency="USD",
        time_zone="UTC",
        window_days=window_days,
        available_windows_days=sorted({30, 90, window_days}),
        period_start=date(2026, 7, 17),
        period_end=date(2026, 10, 14),
        covered_days=90,
        mandatory_minor_units=mandatory,
        flexible_minor_units=flexible,
        provisional_minor_units=provisional,
        spending_minor_units=total,
        annualized_spending_minor_units=annualized_spending,
        estimated_annual_draw_minor_units=estimated_annual_draw,
        spending_draw_rate=spending_draw_rate,
        tax_adjusted_draw_rate=tax_adjusted_draw_rate,
        effective_tax_rate=effective_tax_rate,
        tax_assumption_note=tax_assumption_note,
        tax_assumption_as_of=None if tax_unmodeled else date(2026, 1, 1),
        reference_draw_rates=[Decimal("0.03"), Decimal("0.035"), Decimal("0.04"), Decimal("0.045")],
        portfolio=portfolio,
        history=history,
        account_coverage=[],
        issues=issues,
        known_issues=[
            "Synthetic fixture: investment values are illustrative and can lag market changes.",
            "No income outside observed spending and portfolio sources is represented.",
        ],
    )


def with_sustainability_history(
    view: SustainabilityView, history: list[SustainabilityHistoryPoint], *, issues: list[str] | None = None
) -> SustainabilityView:
    return SustainabilityView(
        generated_at=view.generated_at,
        status=view.status,
        currency=view.currency,
        time_zone=view.time_zone,
        window_days=view.window_days,
        available_windows_days=view.available_windows_days,
        period_start=view.period_start,
        period_end=view.period_end,
        covered_days=view.covered_days,
        mandatory_minor_units=view.mandatory_minor_units,
        flexible_minor_units=view.flexible_minor_units,
        provisional_minor_units=view.provisional_minor_units,
        spending_minor_units=view.spending_minor_units,
        annualized_spending_minor_units=view.annualized_spending_minor_units,
        estimated_annual_draw_minor_units=view.estimated_annual_draw_minor_units,
        spending_draw_rate=view.spending_draw_rate,
        tax_adjusted_draw_rate=view.tax_adjusted_draw_rate,
        effective_tax_rate=view.effective_tax_rate,
        tax_assumption_note=view.tax_assumption_note,
        tax_assumption_as_of=view.tax_assumption_as_of,
        reference_draw_rates=view.reference_draw_rates,
        portfolio=view.portfolio,
        history=history,
        issues=view.issues if issues is None else issues,
        known_issues=view.known_issues,
        account_coverage=view.account_coverage,
    )


def sustainability_dashboard(*windows: SustainabilityView) -> SustainabilityDashboard:
    return SustainabilityDashboard(default_window_days=90, windows=list(windows))


def sustainability_window(days: int, *, history: list[SustainabilityHistoryPoint] | None = None) -> SustainabilityView:
    result = sustainability()
    if days == 90:
        return result if history is None else with_sustainability_history(result, history)
    mandatory, flexible, provisional = (200_000, 100_000, 10_000) if days == 30 else (3_000_000, 2_000_000, 300_000)
    total = mandatory + flexible + provisional
    annual = int((Decimal(total) * Decimal("365.2425") / days).quantize(Decimal(1)))
    funding = int((Decimal(annual) / Decimal("0.88")).quantize(Decimal(1)))
    dates = (
        ("2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14")
        if days == 30
        else ("2026-10-13", "2026-10-14")
    )
    history_points = (
        history
        if history is not None
        else [
            SustainabilityHistoryPoint(
                date=date.fromisoformat(day),
                status=ReportStatus.READY,
                covered_days=days,
                annualized_spending_minor_units=annual,
                spending_draw_rate=Decimal(annual) / Decimal(125_000_000),
                tax_adjusted_draw_rate=Decimal(funding) / Decimal(125_000_000),
            )
            for day in dates
        ]
    )
    return SustainabilityView(
        generated_at=result.generated_at,
        status=result.status,
        currency=result.currency,
        time_zone=result.time_zone,
        window_days=days,
        available_windows_days=[30, 90, 365],
        period_start=date(2026, 10, 15) - timedelta(days=days),
        period_end=result.period_end,
        covered_days=days,
        mandatory_minor_units=mandatory,
        flexible_minor_units=flexible,
        provisional_minor_units=provisional,
        spending_minor_units=total,
        annualized_spending_minor_units=annual,
        estimated_annual_draw_minor_units=funding,
        spending_draw_rate=Decimal(annual) / Decimal(125_000_000),
        tax_adjusted_draw_rate=Decimal(funding) / Decimal(125_000_000),
        effective_tax_rate=result.effective_tax_rate,
        tax_assumption_note=result.tax_assumption_note,
        tax_assumption_as_of=result.tax_assumption_as_of,
        reference_draw_rates=result.reference_draw_rates,
        portfolio=result.portfolio,
        history=history_points,
        issues=result.issues,
        known_issues=result.known_issues,
        account_coverage=result.account_coverage,
    )


def sustainability_partial_window() -> SustainabilityView:
    result = sustainability()
    return SustainabilityView(
        generated_at=result.generated_at,
        status=result.status,
        currency=result.currency,
        time_zone=result.time_zone,
        window_days=result.window_days,
        available_windows_days=result.available_windows_days,
        period_start=date(2026, 8, 31),
        period_end=result.period_end,
        covered_days=45,
        mandatory_minor_units=380_000,
        flexible_minor_units=205_000,
        provisional_minor_units=25_000,
        spending_minor_units=610_000,
        annualized_spending_minor_units=result.annualized_spending_minor_units,
        estimated_annual_draw_minor_units=result.estimated_annual_draw_minor_units,
        spending_draw_rate=result.spending_draw_rate,
        tax_adjusted_draw_rate=result.tax_adjusted_draw_rate,
        effective_tax_rate=result.effective_tax_rate,
        tax_assumption_note=result.tax_assumption_note,
        tax_assumption_as_of=result.tax_assumption_as_of,
        reference_draw_rates=result.reference_draw_rates,
        portfolio=result.portfolio,
        history=[],
        issues=result.issues,
        known_issues=result.known_issues,
        account_coverage=result.account_coverage,
    )


def sustainability_with_partial_account_history() -> SustainabilityView:
    result = sustainability()
    annual = int((Decimal(610_000) * Decimal("365.2425") / 44).quantize(Decimal(1)))
    draw = int((Decimal(annual) / Decimal("0.88")).quantize(Decimal(1)))
    account_coverage = [
        SpendingAccountCoverage(
            account_label="Example checking",
            first_recorded_on=date(2026, 9, 1),
            opening=AccountOpening(
                earliest=date(2026, 7, 20),
                latest=date(2026, 7, 27),
                source="Synthetic account archive",
                as_of=date(2026, 10, 14),
            ),
            coverage=None,
            unknown_days_in_window=43,
        ),
        SpendingAccountCoverage(
            account_label="Example card",
            first_recorded_on=date(2026, 8, 15),
            opening=None,
            coverage=DocumentedHistory(
                complete_from=date(2026, 8, 15), source="Synthetic statement archive", as_of=date(2026, 10, 14)
            ),
            unknown_days_in_window=29,
        ),
    ]
    return SustainabilityView(
        generated_at=result.generated_at,
        status=result.status,
        currency=result.currency,
        time_zone=result.time_zone,
        window_days=result.window_days,
        available_windows_days=result.available_windows_days,
        period_start=date(2026, 9, 1),
        period_end=result.period_end,
        covered_days=44,
        mandatory_minor_units=380_000,
        flexible_minor_units=205_000,
        provisional_minor_units=25_000,
        spending_minor_units=610_000,
        annualized_spending_minor_units=annual,
        estimated_annual_draw_minor_units=draw,
        spending_draw_rate=Decimal(annual) / Decimal(125_000_000),
        tax_adjusted_draw_rate=Decimal(draw) / Decimal(125_000_000),
        effective_tax_rate=result.effective_tax_rate,
        tax_assumption_note=result.tax_assumption_note,
        tax_assumption_as_of=result.tax_assumption_as_of,
        reference_draw_rates=result.reference_draw_rates,
        portfolio=result.portfolio,
        history=[],
        issues=result.issues,
        known_issues=result.known_issues,
        account_coverage=account_coverage,
    )


def sustainability_with_historical_coverage_gap() -> SustainabilityView:
    result = sustainability()
    history = [
        SustainabilityHistoryPoint(
            date=date(2026, 10, 8),
            status=ReportStatus.READY,
            covered_days=90,
            annualized_spending_minor_units=4_700_000,
            spending_draw_rate=Decimal("0.0376"),
            tax_adjusted_draw_rate=Decimal("0.0427272727"),
        ),
        SustainabilityHistoryPoint(
            date=date(2026, 10, 9),
            status=ReportStatus.READY,
            covered_days=90,
            annualized_spending_minor_units=4_850_000,
            spending_draw_rate=Decimal("0.0388"),
            tax_adjusted_draw_rate=Decimal("0.0440909091"),
        ),
        *[
            SustainabilityHistoryPoint(
                date=date(2026, 10, day),
                status=ReportStatus.UNAVAILABLE,
                covered_days=89 - (day - 10),
                annualized_spending_minor_units=None,
                spending_draw_rate=None,
                tax_adjusted_draw_rate=None,
            )
            for day in range(10, 15)
        ],
    ]
    return SustainabilityView(
        generated_at=result.generated_at,
        status=ReportStatus.UNAVAILABLE,
        currency=result.currency,
        time_zone=result.time_zone,
        window_days=result.window_days,
        available_windows_days=result.available_windows_days,
        period_start=date(2026, 7, 17),
        period_end=result.period_end,
        covered_days=0,
        mandatory_minor_units=0,
        flexible_minor_units=0,
        provisional_minor_units=0,
        spending_minor_units=0,
        annualized_spending_minor_units=None,
        estimated_annual_draw_minor_units=None,
        spending_draw_rate=None,
        tax_adjusted_draw_rate=None,
        effective_tax_rate=result.effective_tax_rate,
        tax_assumption_note=result.tax_assumption_note,
        tax_assumption_as_of=result.tax_assumption_as_of,
        reference_draw_rates=result.reference_draw_rates,
        portfolio=result.portfolio,
        history=history,
        issues=result.issues,
        known_issues=result.known_issues,
        account_coverage=[
            SpendingAccountCoverage(
                account_label="Example new account",
                first_recorded_on=None,
                opening=AccountOpening(
                    earliest=date(2026, 10, 10),
                    latest=date(2026, 10, 14),
                    source="Synthetic opening archive",
                    as_of=date(2026, 10, 14),
                ),
                coverage=None,
                unknown_days_in_window=5,
            )
        ],
    )


def full_reversal_pair() -> ReversalPair:
    return ReversalPair(
        id="example-full-reversal",
        description="Synthetic example of a fully reversed purchase.",
        original=AllOf(
            conditions=[
                NameContains(field="name", substring="EXAMPLE OWN ACCOUNT TRANSFER"),
                AmountExact(value="120.00"),
                DateRange(start=date(2026, 10, 11), end=date(2026, 10, 11)),
            ]
        ),
        reversal=AllOf(
            conditions=[
                NameContains(field="name", substring="EXAMPLE OWN ACCOUNT REVERSAL"),
                AmountExact(value="-120.00"),
                DateRange(start=date(2026, 10, 12), end=date(2026, 10, 12)),
            ]
        ),
    )


def unresolved_reversal_pair() -> ReversalPair:
    return ReversalPair(
        id="example-reversal-needs-review",
        description="Synthetic selector with no matching original transaction.",
        original=AllOf(
            conditions=[
                NameContains(field="name", substring="EXAMPLE MISSING ORIGINAL"),
                AmountExact(value="15.50"),
                DateRange(start=date(2026, 10, 7), end=date(2026, 10, 7)),
            ]
        ),
        reversal=AllOf(
            conditions=[
                NameContains(field="name", substring="EXAMPLE MISSING REVERSAL"),
                AmountExact(value="-15.50"),
                DateRange(start=date(2026, 10, 8), end=date(2026, 10, 8)),
            ]
        ),
    )


def allowance(warmup: bool = False, estimate_period_id: EstimatePeriodId | None = None) -> AllowanceView:
    today = date(2026, 10, 15)
    estimate_period_id = estimate_period_id or PeriodId.ROLLING_7D
    activation = today if warmup else date(2026, 10, 1)
    spend_amounts = {
        PeriodId.CREDIT_CYCLE: 55000,
        PeriodId.CALENDAR_MONTH: 55000,
        PeriodId.YEAR_TO_DATE: 55000,
        PeriodId.ROLLING_7D: 8750,
        PeriodId.ROLLING_30D: 50000,
    }
    spend_periods = []
    for period_id, amount in spend_amounts.items():
        period = Period.for_id(period_id, today, activation)
        spend_periods.append(
            AllowanceSpendPeriod(
                period=period, counted_from=max(period.start, activation), spend_minor_units=0 if warmup else amount
            )
        )
    return AllowanceView(
        status=Status.ACTIVE,
        time_zone="UTC",
        currency="USD",
        monthly_minor_units=70000,
        # Deliberately differs from the monthly-derived rate to verify both UI displays use the API field.
        daily_reference_minor_units=2800,
        activation_at=activation,
        available_minor_units=70000 if warmup else 20000,
        next_credit_at=datetime(2026, 10, 25, tzinfo=UTC),
        posted_minor_units=0 if warmup else 52500,
        pending_minor_units=0 if warmup else 2500,
        review_minor_units=0 if warmup else 1500,
        review_transaction_count=0 if warmup else 2,
        unmatched_refunds_minor_units=0,
        spend_periods=spend_periods,
        recorded_pace_periods=[
            RecordedPacePeriod(
                period=Period.for_id(period_id, today),
                observed_daily_minor_units=None if warmup else observed,
                unmatched_charges=UnmatchedCharges(count=0 if warmup else 2, amount_minor_units=0 if warmup else 300),
            )
            for period_id, observed in ((PeriodId.ROLLING_7D, 1250), (PeriodId.ROLLING_30D, 1000))
        ],
        forecast=ForecastView(
            basis_period=Period.for_id(estimate_period_id, today),
            daily_pace_minor_units=None if warmup else (1250 if estimate_period_id == PeriodId.ROLLING_7D else 1000),
            projected_cycle_end_minor_units=None
            if warmup
            else (7500 if estimate_period_id == PeriodId.ROLLING_7D else 10000),
            estimated_exhaustion_at=None if warmup else datetime(2026, 10, 31, tzinfo=UTC),
            alert_state=PaceAlert.UNAVAILABLE if warmup else PaceAlert.NORMAL,
        ),
        spending_signal=PaceAlert.UNAVAILABLE if warmup else PaceAlert.NORMAL,
        last_synced_at=datetime(2026, 10, 15, 11, tzinfo=UTC),
        prior_carry_minor_units=0 if warmup else 5000,
        note="Provisional: Reversal pair example-reversal-needs-review: "
        "Original selector matched 0 retained transactions; exactly one is required.",
    )


def spend_view(warmup: bool = False, estimate_period_id: EstimatePeriodId | None = None) -> SpendView:
    synced = datetime(2026, 10, 15, 11, tzinfo=UTC)
    return SpendView(
        generated_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
        allowance=allowance(warmup, estimate_period_id),
        cards=[
            CardView(
                account_id="synthetic-card",
                label="Example card",
                account_name="Example card",
                mask="0000",
                institution_name="Sample Bank",
                currency="USD",
                statement_period=StatementCycle(start=date(2026, 10, 1), through=date(2026, 10, 15)),
                alert_state=AlertState.NORMAL,
                alert_threshold_percent=None,
                spend_minor_units=22000,
                limit_minor_units=100000,
                spend_percent=Decimal(22),
                pending_minor_units=1000,
                posted_minor_units=21000,
                last_synced_at=synced,
            ),
            CardView(
                account_id="synthetic-new-card",
                label="New example card",
                account_name="New example card",
                mask="1111",
                institution_name="Sample Credit Union",
                currency="USD",
                statement_period=ProvisionalCardPeriod(start=date(2026, 10, 10), through=date(2026, 10, 15)),
                alert_state=AlertState.UNAVAILABLE,
                alert_threshold_percent=None,
                spend_minor_units=3900,
                limit_minor_units=100000,
                spend_percent=None,
                pending_minor_units=0,
                posted_minor_units=3900,
                last_synced_at=synced,
            ),
        ],
    )


def configuration() -> SpendConfigurationView:
    return SpendConfigurationView(
        time_zone="UTC",
        cards=[
            CardConfigurationView(
                label="Example card", enabled=True, limit_minor_units=None, alert_threshold_percent=None
            )
        ],
        allowance=AllowanceConfigurationView(
            monthly_minor_units=70000,
            activation_at=date(2026, 10, 1),
            currency="USD",
            spending_account_count=1,
            max_sync_age_hours=72,
            forecast_basis_period_id=PeriodId.ROLLING_7D,
            rules=[
                Rule(
                    condition=AllOf(
                        conditions=[
                            NameContains(field="name", substring="EXAMPLE FUNDING"),
                            DateRange(start=date(2026, 10, 3), end=date(2026, 10, 3)),
                        ]
                    ),
                    kind=Kind.EXCLUDED,
                    scope=RuleScope.ONE_OFF,
                    id="example-funding-transfer",
                    description="Own-account funding leg, confirmed by the owner 2026-10-04.",
                ),
                Rule(
                    condition=AllOf(
                        conditions=[
                            AmountSign(sign="negative"),
                            AnyOf(
                                conditions=[
                                    NamePrefix(field="name", prefix="EXAMPLE"),
                                    FieldExact(field="merchant_category_code", value="5812"),
                                ]
                            ),
                        ]
                    ),
                    kind=Kind.REVIEW,
                    analysis_category="refund_review",
                    description="Unverified credit; inspect the earlier purchase before netting it.",
                ),
            ],
            analysis_categories={
                "unclassified": AnalysisCategory(label="Unclassified", color="#D97706"),
                "refund_review": AnalysisCategory(label="Refund review", color="#B45309"),
                "shipping": AnalysisCategory(label="Document shipping", color="#0F766E"),
                "travel": AnalysisCategory(label="Holiday travel", color="#7C3AED"),
            },
            reversal_pairs=[full_reversal_pair(), unresolved_reversal_pair()],
        ),
    )


def transactions(period: TransactionPeriodId = PeriodId.ROLLING_30D) -> SpendTransactionsView:
    today = date(2026, 10, 15)
    rows = [
        SpendTransactionRow(
            date=date(2026, 10, 15),
            account_label="Example card",
            name="EXAMPLE CAFE PURCHASE",
            merchant_name="Example Cafe",
            amount_minor_units=1500,
            currency="USD",
            pending=True,
            allowance_in_scope=True,
            disposition=Disposition.COUNTED,
            rule_number=None,
            rule=None,
            allowance_minor_units=1500,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=1500)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=1500,
            statement_reason=StatementReason.COUNTED,
            pfc_primary="FOOD_AND_DRINK",
            pfc_detailed="FOOD_AND_DRINK_COFFEE",
            merchant_category_code="5812",
            category=AnalysisCategoryView(id="unclassified", label="Unclassified", color="#D97706"),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 14),
            account_label="Example card",
            name="UPS SHIPPING",
            merchant_name="UPS",
            amount_minor_units=1850,
            currency="USD",
            pending=False,
            allowance_in_scope=True,
            disposition=Disposition.FIXED,
            rule_number=2,
            effective_kind=Kind.FIXED,
            rule=Rule(
                condition=NamePrefix(field="name", prefix="UPS"),
                kind=Kind.FIXED,
                analysis_category="shipping",
                description="Required document shipping for a synthetic example.",
            ),
            allowance_minor_units=0,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=1850,
            statement_reason=StatementReason.COUNTED,
            pfc_primary="TRANSPORTATION",
            pfc_detailed="TRANSPORTATION_SHIPPING",
            merchant_category_code="4215",
            category=AnalysisCategoryView(id="shipping", label="Document shipping", color="#0F766E"),
            counterparties=[
                PlaidCounterparty(
                    name="Example Shipping",
                    type="merchant",
                    confidence_level="HIGH",
                    entity_id="synthetic-merchant",
                    website="https://example.invalid/shipping",
                ),
                PlaidCounterparty(name="Example Marketplace", type="marketplace"),
            ],
            details=PlaidTransactionDetails(
                account_id="synthetic-account",
                transaction_id="synthetic-transaction",
                amount=Decimal("18.50"),
                iso_currency_code="USD",
                original_description="EXAMPLE SHIPPING PAYMENT",
                authorized_date=date(2026, 10, 13),
                payment_channel="in store",
                location=PlaidTransactionLocation(city="Example City", region="CA", country="US"),
                payment_meta=PlaidPaymentMeta(reference_number="synthetic-reference"),
                personal_finance_category=PlaidPersonalFinanceCategory(
                    primary="TRANSPORTATION", detailed="TRANSPORTATION_SHIPPING", confidence_level="HIGH"
                ),
            ),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 13),
            account_label="Example card",
            name="EXAMPLE REFUND",
            merchant_name=None,
            amount_minor_units=-4200,
            currency="USD",
            pending=False,
            allowance_in_scope=True,
            disposition=Disposition.HELD_REFUND,
            rule_number=1,
            effective_kind=Kind.REVIEW,
            rule=Rule(
                condition=NamePrefix(field="name", prefix="EXAMPLE"),
                kind=Kind.REVIEW,
                description="Confirm the purchase before netting this refund.",
            ),
            allowance_minor_units=0,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=-4200,
            statement_reason=StatementReason.COUNTED,
            pfc_primary="GENERAL_MERCHANDISE",
            pfc_detailed="GENERAL_MERCHANDISE_OTHER",
            merchant_category_code=None,
            category=AnalysisCategoryView(id="unclassified", label="Unclassified", color="#D97706"),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 11),
            account_label="Example checking",
            name="EXAMPLE OWN ACCOUNT TRANSFER",
            merchant_name=None,
            amount_minor_units=12_000,
            currency="USD",
            pending=False,
            allowance_in_scope=True,
            disposition=Disposition.REVERSED,
            rule_number=3,
            effective_kind=Kind.EXCLUDED,
            reversal_pair=full_reversal_pair(),
            rule=Rule(
                condition=AllOf(
                    conditions=[
                        NameContains(field="name", substring="EXAMPLE OWN ACCOUNT TRANSFER"),
                        DateRange(start=date(2026, 10, 11), end=date(2026, 10, 11)),
                    ]
                ),
                kind=Kind.EXCLUDED,
                scope=RuleScope.ONE_OFF,
                id="example-own-account-leg",
                description="Second leg of one own-account transfer, confirmed by the owner 2026-10-12.",
            ),
            allowance_minor_units=0,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=None,
            statement_reason=None,
            pfc_primary="TRANSFER_OUT",
            pfc_detailed="TRANSFER_OUT_OTHER",
            merchant_category_code=None,
            category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 12),
            account_label="Example checking",
            name="EXAMPLE OWN ACCOUNT REVERSAL",
            merchant_name=None,
            amount_minor_units=-12_000,
            currency="USD",
            pending=False,
            allowance_in_scope=True,
            disposition=Disposition.REVERSED,
            rule_number=2,
            effective_kind=Kind.EXCLUDED,
            reversal_pair=full_reversal_pair(),
            rule=Rule(
                condition=NamePrefix(field="name", prefix="EXAMPLE OWN ACCOUNT"),
                kind=Kind.REVIEW,
                description="Synthetic rule classification retained as context for the reversal.",
            ),
            allowance_minor_units=0,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=None,
            statement_reason=None,
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_OTHER",
            merchant_category_code=None,
            category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 2),
            account_label="Example checking",
            name="EXAMPLE TRAVEL PURCHASE",
            merchant_name="Example Travel",
            amount_minor_units=53500,
            currency="USD",
            pending=False,
            allowance_in_scope=True,
            disposition=Disposition.COUNTED,
            rule_number=3,
            effective_kind=Kind.FLEXIBLE,
            rule=Rule(
                condition=NamePrefix(field="name", prefix="EXAMPLE TRAVEL"),
                kind=Kind.FLEXIBLE,
                analysis_category="travel",
                description="Synthetic discretionary trip purchase.",
            ),
            allowance_minor_units=53500,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=53500 if period_id == PeriodId.ROLLING_30D else 0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=None,
            statement_reason=None,
            pfc_primary="TRAVEL",
            pfc_detailed="TRAVEL_OTHER",
            merchant_category_code=None,
            category=AnalysisCategoryView(id="travel", label="Holiday travel", color="#7C3AED"),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 10),
            account_label="Example checking",
            name="EXAMPLE OWN TRANSFER DEBIT",
            merchant_name=None,
            amount_minor_units=12_500,
            currency="USD",
            pending=False,
            allowance_in_scope=True,
            disposition=Disposition.EXCLUDED,
            effective_kind=Kind.EXCLUDED,
            rule=Rule(
                condition=NamePrefix(field="name", prefix="EXAMPLE OWN TRANSFER"),
                kind=Kind.EXCLUDED,
                analysis_category="own_transfers",
                description="Synthetic own-account transfer excluded from the allowance.",
            ),
            allowance_minor_units=0,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=None,
            statement_reason=None,
            pfc_primary="TRANSFER_OUT",
            pfc_detailed="TRANSFER_OUT_OTHER",
            merchant_category_code=None,
            category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 11),
            account_label="Example checking",
            name="EXAMPLE OWN TRANSFER CREDIT",
            merchant_name=None,
            amount_minor_units=-12_500,
            currency="USD",
            pending=False,
            allowance_in_scope=True,
            disposition=Disposition.EXCLUDED,
            effective_kind=Kind.EXCLUDED,
            rule=Rule(
                condition=NamePrefix(field="name", prefix="EXAMPLE OWN TRANSFER"),
                kind=Kind.EXCLUDED,
                analysis_category="own_transfers",
                description="Synthetic own-account transfer excluded from the allowance.",
            ),
            allowance_minor_units=0,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=None,
            statement_reason=None,
            pfc_primary="TRANSFER_IN",
            pfc_detailed="TRANSFER_IN_OTHER",
            merchant_category_code=None,
            category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 12),
            account_label="Example checking",
            name="EXAMPLE TAX PAYMENT",
            merchant_name=None,
            amount_minor_units=41_500,
            currency="USD",
            pending=True,
            allowance_in_scope=True,
            disposition=Disposition.EXCLUDED,
            effective_kind=Kind.EXCLUDED,
            rule=Rule(
                condition=NamePrefix(field="name", prefix="EXAMPLE TAX"),
                kind=Kind.EXCLUDED,
                analysis_category="taxes",
                description="Synthetic tax payment accounted for separately.",
            ),
            allowance_minor_units=0,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=None,
            statement_reason=None,
            pfc_primary="TAXES",
            pfc_detailed="TAXES_OTHER_TAXES",
            merchant_category_code=None,
            category=AnalysisCategoryView(id="taxes", label="Taxes", color="#B45309"),
        ),
        SpendTransactionRow(
            date=date(2026, 10, 13),
            account_label="Example checking",
            name="EXAMPLE TAX PAYMENT CAD",
            merchant_name=None,
            amount_minor_units=7_200,
            currency="CAD",
            pending=False,
            allowance_in_scope=True,
            disposition=Disposition.EXCLUDED,
            effective_kind=Kind.EXCLUDED,
            rule=Rule(
                condition=NamePrefix(field="name", prefix="EXAMPLE TAX"),
                kind=Kind.EXCLUDED,
                analysis_category="taxes",
                description="Synthetic foreign-currency tax payment accounted for separately.",
            ),
            allowance_minor_units=0,
            pace_effects=[
                PaceEffect(period_id=period_id, amount_minor_units=0)
                for period_id in (PeriodId.ROLLING_7D, PeriodId.ROLLING_30D)
            ],
            statement_minor_units=None,
            statement_reason=None,
            pfc_primary="TAXES",
            pfc_detailed="TAXES_OTHER_TAXES",
            merchant_category_code=None,
            category=AnalysisCategoryView(id="taxes", label="Taxes", color="#B45309"),
        ),
    ]
    shown = [row for row in rows if row.date >= PeriodId(period).start(today, date(2026, 10, 1))]
    return SpendTransactionsView(
        generated_at=datetime(2026, 10, 15, 12, tzinfo=UTC),
        requested_period_id=period,
        period=Period.for_id(PeriodId(period), today, date(2026, 10, 1)),
        summary=TransactionPeriodSummary(
            transaction_count=len(shown),
            net_allowance_spend_minor_units=sum(row.allowance_minor_units for row in shown),
            unmatched_charge_count=1,
            unmatched_charge_minor_units=1500,
            excluded_flows=[
                ExcludedFlowSummary(
                    category=AnalysisCategoryView(id="own_transfers", label="Own-account transfers", color="#64748B"),
                    currency="USD",
                    debit_minor_units=12_500,
                    credit_minor_units=12_500,
                    transaction_count=2,
                    pending_count=0,
                ),
                ExcludedFlowSummary(
                    category=AnalysisCategoryView(id="taxes", label="Taxes", color="#B45309"),
                    currency="USD",
                    debit_minor_units=41_500,
                    credit_minor_units=0,
                    transaction_count=1,
                    pending_count=1,
                ),
                ExcludedFlowSummary(
                    category=AnalysisCategoryView(id="taxes", label="Taxes", color="#B45309"),
                    currency="CAD",
                    debit_minor_units=7_200,
                    credit_minor_units=0,
                    transaction_count=1,
                    pending_count=0,
                ),
            ],
        ),
        allowance=allowance(),
        rows=shown,
    )
