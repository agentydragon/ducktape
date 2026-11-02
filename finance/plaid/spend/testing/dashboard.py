"""Synthetic dashboard responses for Spend visual tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from finance.plaid.spend.allowance import (
    AllOf,
    AllowancePolicy,
    AmountExact,
    AmountSign,
    AnalysisCategory,
    AnyOf,
    DateRange,
    EstimatePeriodId,
    FieldExact,
    Kind,
    NameContains,
    NamePrefix,
    PeriodId,
    ReversalPair,
    Rule,
    RuleScope,
    Transaction,
    TransactionDecision,
    calculate,
)
from finance.plaid.spend.configuration import CardConfig, SpendConfiguration
from finance.plaid.spend.models import (
    AlertState,
    AllowanceConfigurationView,
    CardConfigurationView,
    CardView,
    ProvisionalCardPeriod,
    SpendConfigurationView,
    SpendView,
    StatementCycle,
    StatementReason,
)
from finance.plaid.spend.reconciliation_views import allowance_view
from finance.plaid.spend.testing import ledger

RULES = [
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
                NameContains(field="name", substring="EXAMPLE OWN ACCOUNT TRANSFER"),
                DateRange(start=date(2026, 10, 11), end=date(2026, 10, 11)),
            ]
        ),
        kind=Kind.EXCLUDED,
        scope=RuleScope.ONE_OFF,
        id="example-own-account-leg",
        analysis_category="own_transfers",
        description="Second leg of one own-account transfer, confirmed by the owner 2026-10-12.",
    ),
    Rule(
        condition=NamePrefix(field="name", prefix="EXAMPLE OWN ACCOUNT"),
        kind=Kind.REVIEW,
        analysis_category="own_transfers",
        description="Synthetic rule classification retained as context for the reversal.",
    ),
    Rule(
        condition=NamePrefix(field="name", prefix="EXAMPLE OWN TRANSFER"),
        kind=Kind.EXCLUDED,
        analysis_category="own_transfers",
        description="Synthetic own-account transfer excluded from the allowance.",
    ),
    Rule(
        condition=NamePrefix(field="name", prefix="EXAMPLE TAX"),
        kind=Kind.EXCLUDED,
        analysis_category="taxes",
        description="Synthetic tax payment accounted for separately.",
    ),
    Rule(
        condition=NamePrefix(field="name", prefix="UPS"),
        kind=Kind.FIXED,
        analysis_category="shipping",
        description="Required document shipping for a synthetic example.",
    ),
    Rule(
        condition=NamePrefix(field="name", prefix="EXAMPLE TRAVEL"),
        kind=Kind.FLEXIBLE,
        analysis_category="travel",
        description="Synthetic discretionary trip purchase.",
    ),
    Rule(condition=NamePrefix(field="name", prefix="EXAMPLE GROCERIES"), kind=Kind.FLEXIBLE),
    Rule(condition=NamePrefix(field="name", prefix="EXAMPLE PURCHASE"), kind=Kind.FLEXIBLE),
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
]


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


def policy(*, warmup: bool = False, monthly_minor_units: int = 70000) -> AllowancePolicy:
    return AllowancePolicy(
        monthly_minor_units=monthly_minor_units,
        activation_at=ledger.NOW.date() if warmup else date(2026, 9, 25),
        spending_account_ids={"warmup-checking"} if warmup else {ledger.CARD, ledger.CHECKING},
        currency="USD",
        max_sync_age_hours=72,
        forecast_basis_period_id=PeriodId.ROLLING_7D,
        rules=RULES,
        reversal_pairs=[] if warmup else [full_reversal_pair(), unresolved_reversal_pair()],
        analysis_categories={
            "unclassified": AnalysisCategory(label="Unclassified", color="#D97706"),
            "refund_review": AnalysisCategory(label="Refund review", color="#B45309"),
            "shipping": AnalysisCategory(label="Document shipping", color="#0F766E"),
            "travel": AnalysisCategory(label="Holiday travel", color="#7C3AED"),
            "own_transfers": AnalysisCategory(label="Own-account transfers", color="#64748B"),
            "taxes": AnalysisCategory(label="Taxes", color="#B45309"),
        },
    )


@dataclass(frozen=True)
class Scenario:
    configuration: SpendConfiguration
    transactions: list[Transaction]
    decisions: list[TransactionDecision]
    statement_decisions: dict[tuple[str, str], tuple[StatementReason, int]]
    view: SpendView


def scenario(
    warmup: bool = False, estimate_period_id: EstimatePeriodId | None = None, *, monthly_minor_units: int = 70000
) -> Scenario:
    configuration = spend_configuration(warmup=warmup, monthly_minor_units=monthly_minor_units)
    transactions = ledger.transactions()
    decisions: list[TransactionDecision] = []
    assert configuration.allowance is not None
    allowance = allowance_view(
        calculate(
            configuration.allowance,
            transactions,
            time_zone=configuration.time_zone,
            now=ledger.NOW,
            last_synced_at=ledger.LAST_SYNCED,
            estimate_period_id=estimate_period_id,
            decisions=decisions,
        )
    )
    configured_cards = {card.account_id: card for card in configuration.cards}
    cards = [
        CardView(
            account_id=ledger.CARD,
            label=configured_cards[ledger.CARD].label,
            account_name="Example card",
            institution_name="Sample Bank",
            mask="0000",
            currency="USD",
            statement_period=StatementCycle(start=date(2026, 10, 1), through=ledger.NOW.date()),
            spend_minor_units=22000,
            posted_minor_units=20500,
            pending_minor_units=1500,
            limit_minor_units=configured_cards[ledger.CARD].limit_minor_units,
            alert_threshold_percent=configured_cards[ledger.CARD].alert_threshold_percent,
            spend_percent=Decimal(22),
            alert_state=AlertState.NORMAL,
            last_synced_at=ledger.LAST_SYNCED,
        ),
        CardView(
            account_id=ledger.NEW_CARD,
            label=configured_cards[ledger.NEW_CARD].label,
            account_name="New example card",
            institution_name="Sample Credit Union",
            mask="1111",
            currency="USD",
            statement_period=ProvisionalCardPeriod(start=date(2026, 10, 10), through=ledger.NOW.date()),
            spend_minor_units=3900,
            posted_minor_units=3900,
            pending_minor_units=0,
            limit_minor_units=configured_cards[ledger.NEW_CARD].limit_minor_units,
            alert_threshold_percent=configured_cards[ledger.NEW_CARD].alert_threshold_percent,
            spend_percent=None,
            alert_state=AlertState.UNAVAILABLE,
            last_synced_at=ledger.LAST_SYNCED,
        ),
    ]
    # These synthetic card transactions all count toward their statement totals.
    statement_decisions = {
        (transaction.account_id, transaction.transaction_id): (StatementReason.COUNTED, int(transaction.amount * 100))
        for transaction in transactions
        if transaction.account_id in {ledger.CARD, ledger.NEW_CARD}
    }
    return Scenario(
        configuration=configuration,
        transactions=transactions,
        decisions=decisions,
        statement_decisions=statement_decisions,
        view=SpendView(generated_at=ledger.NOW, allowance=allowance, cards=cards),
    )


def spend_view(
    warmup: bool = False, estimate_period_id: EstimatePeriodId | None = None, *, monthly_minor_units: int = 70000
) -> SpendView:
    return scenario(warmup, estimate_period_id, monthly_minor_units=monthly_minor_units).view


def spend_configuration(*, warmup: bool = False, monthly_minor_units: int = 70000) -> SpendConfiguration:
    return SpendConfiguration(
        time_zone=ZoneInfo("UTC"),
        cards=[
            CardConfig(
                account_id=ledger.CARD, label=ledger.ACCOUNT_LABELS[ledger.CARD], enabled=True, limit_minor_units=100000
            ),
            CardConfig(
                account_id=ledger.NEW_CARD,
                label=ledger.ACCOUNT_LABELS[ledger.NEW_CARD],
                enabled=True,
                limit_minor_units=100000,
            ),
        ],
        allowance=policy(warmup=warmup, monthly_minor_units=monthly_minor_units),
        account_labels={} if warmup else {ledger.CHECKING: ledger.ACCOUNT_LABELS[ledger.CHECKING]},
    )


def configuration() -> SpendConfigurationView:
    config = spend_configuration()
    allowance = config.allowance
    assert allowance is not None
    return SpendConfigurationView(
        time_zone=config.time_zone.key,
        cards=[
            CardConfigurationView(
                label=card.label,
                enabled=card.enabled,
                limit_minor_units=card.limit_minor_units,
                alert_threshold_percent=card.alert_threshold_percent,
            )
            for card in config.cards
        ],
        allowance=AllowanceConfigurationView(
            monthly_minor_units=allowance.monthly_minor_units,
            activation_at=allowance.activation_at,
            currency=allowance.currency,
            spending_account_count=len(allowance.spending_account_ids),
            max_sync_age_hours=allowance.max_sync_age_hours,
            forecast_basis_period_id=allowance.forecast_basis_period_id,
            rules=allowance.rules,
            analysis_categories=allowance.analysis_categories,
            reversal_pairs=allowance.reversal_pairs,
        ),
    )
