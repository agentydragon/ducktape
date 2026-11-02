"""Synthetic dashboard responses for Spend visual tests."""

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
    EstimatePeriodId,
    FieldExact,
    ForecastView,
    Kind,
    NameContains,
    NamePrefix,
    PaceAlert,
    Period,
    PeriodId,
    RecordedPacePeriod,
    ReversalPair,
    Rule,
    RuleScope,
    Status,
    UnmatchedCharges,
)
from finance.plaid.spend.models import (
    AlertState,
    AllowanceConfigurationView,
    CardConfigurationView,
    CardView,
    ProvisionalCardPeriod,
    SpendConfigurationView,
    SpendView,
    StatementCycle,
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
    activation = today if warmup else date(2026, 9, 25)
    daily_pace = 1250 if estimate_period_id == PeriodId.ROLLING_7D else 2380
    alert = PaceAlert.NORMAL if estimate_period_id == PeriodId.ROLLING_7D else PaceAlert.WARNING
    spend_amounts = {
        PeriodId.CREDIT_CYCLE: 50000,
        PeriodId.CALENDAR_MONTH: 50000,
        PeriodId.YEAR_TO_DATE: 50000,
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
        daily_reference_minor_units=2300,
        activation_at=activation,
        available_minor_units=70000 if warmup else 20000,
        next_credit_at=datetime(2026, 11, 15, tzinfo=UTC) if warmup else datetime(2026, 10, 25, tzinfo=UTC),
        posted_minor_units=0 if warmup else 48500,
        pending_minor_units=0 if warmup else 1500,
        review_minor_units=0 if warmup else 1500,
        review_transaction_count=0 if warmup else 1,
        unmatched_refunds_minor_units=0 if warmup else 4200,
        spend_periods=spend_periods,
        recorded_pace_periods=[
            RecordedPacePeriod(
                period=Period.for_id(period_id, today),
                observed_daily_minor_units=None if warmup else observed,
                unmatched_charges=UnmatchedCharges(count=0 if warmup else 1, amount_minor_units=0 if warmup else 1500),
            )
            for period_id, observed in ((PeriodId.ROLLING_7D, 1250), (PeriodId.ROLLING_30D, 1833))
        ],
        forecast=ForecastView(
            basis_period=Period.for_id(estimate_period_id, today),
            daily_pace_minor_units=None if warmup else daily_pace,
            projected_cycle_end_minor_units=None
            if warmup
            else (7500 if estimate_period_id == PeriodId.ROLLING_7D else -3800),
            estimated_exhaustion_at=None
            if warmup
            else datetime(2026, 10, 15, 12, tzinfo=UTC) + timedelta(days=20000 / daily_pace),
            alert_state=PaceAlert.UNAVAILABLE if warmup else alert,
        ),
        spending_signal=PaceAlert.UNAVAILABLE if warmup else alert,
        last_synced_at=datetime(2026, 10, 15, 11, tzinfo=UTC),
        prior_carry_minor_units=0,
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
            activation_at=date(2026, 9, 25),
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
