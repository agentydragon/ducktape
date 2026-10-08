"""Synthetic allowance contract tests; no real account or transaction data."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.plaid.spend.allowance import (
    AllOf,
    AllowancePolicy,
    AllowanceView,
    AmountSign,
    AnalysisCategory,
    AnyOf,
    CategoryExact,
    FieldExact,
    Kind,
    NameContains,
    NamePrefix,
    PaceAlert,
    PeriodId,
    Rule,
    Status,
    Transaction,
    calculate,
    matching_rule,
    month_anniversary,
)
from finance.plaid.spend.models import SpendConfiguration

START = datetime(2026, 1, 31, tzinfo=UTC)
START_DATE = date(2026, 1, 31)


def category_rule(field: Literal["pfc_primary", "pfc_detailed"], value: str, kind: Kind) -> Rule:
    return Rule(condition=CategoryExact(field=field, value=value), kind=kind)


def name_rule(field: Literal["name", "merchant_name"], prefix: str, kind: Kind) -> Rule:
    return Rule(condition=NamePrefix(field=field, prefix=prefix), kind=kind)


def policy(*, activation_at: date = START_DATE, rules: list[Rule] | None = None) -> AllowancePolicy:
    configured_rules = (
        rules if rules is not None else [category_rule(field="pfc_primary", value="SHOPPING", kind=Kind.FLEXIBLE)]
    )
    category_ids = {
        "unclassified",
        *(rule.analysis_category for rule in configured_rules if rule.analysis_category is not None),
    }
    return AllowancePolicy(
        monthly_minor_units=10_000,
        spending_account_ids={"card-1"},
        activation_at=activation_at,
        rules=configured_rules,
        analysis_categories={
            category_id: AnalysisCategory(label=category_id.replace("_", " ").title(), color="#336699")
            for category_id in category_ids
        },
    )


def row(
    day: str,
    amount: int,
    *,
    pending: bool = False,
    transaction_id: str | None = None,
    pending_transaction_id: str | None = None,
    pfc_primary: str | None = "SHOPPING",
    pfc_detailed: str | None = "SHOPPING_GENERAL_MERCHANDISE",
) -> Transaction:
    return Transaction(
        account_id="card-1",
        transaction_id=transaction_id or f"tx-{day}-{amount}",
        date=date.fromisoformat(day),
        amount=Decimal(amount),
        pending=pending,
        pending_transaction_id=pending_transaction_id,
        currency="USD",
        name="EXAMPLE SHOP",
        merchant_name=None,
        pfc_primary=pfc_primary,
        pfc_detailed=pfc_detailed,
    )


def view(rows=(), when=START):
    return calculate(policy(), list(rows), now=when, last_synced_at=when)


def spend(report: AllowanceView, period_id: PeriodId) -> int:
    return next(period.spend_minor_units for period in report.spend_periods if period.period.id == period_id)


def pace(report: AllowanceView, period_id: PeriodId) -> int | None:
    return next(
        period.observed_daily_minor_units for period in report.recorded_pace_periods if period.period.id == period_id
    )


def unmatched(report: AllowanceView, period_id: PeriodId) -> tuple[int, int]:
    period = next(period for period in report.recorded_pace_periods if period.period.id == period_id)
    assert period.unmatched_charges is not None
    return period.unmatched_charges.count, period.unmatched_charges.amount_minor_units


def test_single_config_parses_cards_and_optional_allowance():
    assert SpendConfiguration.model_validate_json('{"cards":[]}').allowance is None
    config = SpendConfiguration.model_validate_json(
        '{"cards":[],"allowance":{"monthly_minor_units":10000,"activation_at":"2026-01-31",'
        '"spending_account_ids":["example-card"],'
        '"analysis_categories":{"unclassified":{"label":"Unclassified","color":"#64748B"}},'
        '"rules":[{"condition":{"type":"name_prefix","field":"name","prefix":"EXAMPLE"},"kind":"flexible"}]}}'
    )
    assert config.allowance is not None
    assert config.allowance.monthly_minor_units == 10_000
    assert config.allowance.spending_account_ids == {"example-card"}
    assert config.allowance.rules[0] == name_rule("name", "EXAMPLE", Kind.FLEXIBLE)
    with pytest.raises(ValidationError):
        SpendConfiguration.model_validate_json('{"cards":[],"allowance":{"monthly_minor_units":10000}}')
    with pytest.raises(ValidationError):
        SpendConfiguration.model_validate_json(
            '{"cards":[],"allowance":{"monthly_minor_units":10000,"spending_account_ids":["example-card"],'
            '"rules":[{"condition":{"type":"name_prefix","field":"name","prefix":"EXAMPLE"},"kind":"flexible"}]}}'
        )
    with pytest.raises(ValidationError):
        AllowancePolicy.model_validate(
            {
                "monthly_minor_units": 10_000,
                "activation_at": None,
                "spending_account_ids": ["example-card"],
                "rules": [
                    {"condition": {"type": "name_prefix", "field": "name", "prefix": "EXAMPLE"}, "kind": "flexible"}
                ],
            }
        )


def test_configured_allowance_is_active_and_no_double_credit():
    assert view().status == Status.ACTIVE
    with pytest.raises(ValueError, match="future"):
        view(when=START.replace(year=2025))
    assert view([row("2026-01-30", 90)]).available_minor_units == 10_000
    assert view(when=datetime(2026, 2, 1, tzinfo=UTC)).available_minor_units == 10_000
    assert view(when=datetime(2026, 2, 28, tzinfo=UTC)).available_minor_units == 20_000
    assert month_anniversary(START, 2) == datetime(2026, 3, 31, tzinfo=UTC)
    with pytest.raises(ValidationError):
        policy(activation_at=datetime(2026, 1, 31, 12, tzinfo=UTC))
    assert policy().activation_at == date(2026, 1, 31)


def test_carry_windows_and_early_pace():
    now = datetime(2026, 2, 28, tzinfo=UTC)
    result = view([row("2026-01-31", 20), row("2026-02-28", 30)], when=now)
    assert result.available_minor_units == 15_000
    assert result.prior_carry_minor_units == 8_000
    assert spend(result, PeriodId.CREDIT_CYCLE) == 3_000
    assert spend(result, PeriodId.CALENDAR_MONTH) == 3_000
    assert result.next_credit_at == datetime(2026, 3, 31, tzinfo=UTC)
    fast = view([row("2026-01-31", 70)], when=START)
    assert fast.forecast.alert_state == PaceAlert.WARNING
    assert fast.forecast.estimated_exhaustion_at == START + (datetime(2026, 2, 1, tzinfo=UTC) - START) * (3 / 7)


def test_trailing_windows_include_exactly_seven_and_thirty_calendar_days():
    now = datetime(2026, 3, 2, tzinfo=UTC)
    result = view(
        [row("2026-02-23", 10), row("2026-02-24", 20), row("2026-01-31", 30), row("2026-02-01", 40)], when=now
    )
    assert spend(result, PeriodId.ROLLING_7D) == 2_000
    assert spend(result, PeriodId.ROLLING_30D) == 7_000
    assert pace(result, PeriodId.ROLLING_7D) == 2_000 // 7
    assert pace(result, PeriodId.ROLLING_30D) == 7_000 // 30


def test_prior_purchases_inform_pace_without_importing_debt():
    # Fixed purchases and purchases outside the lookback must not affect the pace.
    result = calculate(
        policy(
            rules=[
                category_rule(field="pfc_primary", value="RENT", kind=Kind.FIXED),
                category_rule(field="pfc_primary", value="SHOPPING", kind=Kind.FLEXIBLE),
            ]
        ),
        [
            row("2026-01-24", 50),
            row("2026-01-26", 14),
            row("2026-01-30", 56),
            row("2026-01-30", 300, pfc_primary="RENT", pfc_detailed=None),
        ],
        now=START,
        last_synced_at=START,
    )
    assert result.available_minor_units == 10_000
    assert result.posted_minor_units == 0
    assert spend(result, PeriodId.ROLLING_7D) == 0
    assert result.forecast.daily_pace_minor_units == 1_000
    assert pace(result, PeriodId.ROLLING_7D) == 1_000
    assert pace(result, PeriodId.ROLLING_30D) == 12_000 // 30
    assert result.forecast.alert_state == PaceAlert.WARNING
    assert result.forecast.projected_cycle_end_minor_units == -18_000


def test_monthly_observed_pace_warns_when_selected_without_weekly_forecast_or_opening_debt():
    now = datetime(2026, 3, 2, tzinfo=UTC)
    selected_policy = policy(activation_at=now.date())
    transactions = [row("2026-02-10", 200)]
    result = calculate(selected_policy, transactions, now=now, last_synced_at=now)
    assert result.available_minor_units == 10_000
    assert pace(result, PeriodId.ROLLING_7D) is None
    assert pace(result, PeriodId.ROLLING_30D) == 20_000 // 30
    assert result.forecast.alert_state == PaceAlert.UNAVAILABLE
    assert result.spending_signal == PaceAlert.UNAVAILABLE
    monthly = calculate(
        selected_policy, transactions, now=now, last_synced_at=now, estimate_period_id=PeriodId.ROLLING_30D
    )
    assert monthly.forecast.basis_period.id == PeriodId.ROLLING_30D
    assert monthly.spending_signal == PaceAlert.WARNING


def test_no_pace_until_history_or_a_full_week_of_zero_spend():
    opening = view()
    assert opening.available_minor_units == 10_000
    assert opening.forecast.daily_pace_minor_units is None
    assert pace(opening, PeriodId.ROLLING_7D) is None
    assert pace(opening, PeriodId.ROLLING_30D) is None
    assert opening.forecast.projected_cycle_end_minor_units is None
    assert opening.forecast.alert_state == PaceAlert.UNAVAILABLE
    assert view(when=datetime(2026, 2, 5, tzinfo=UTC)).forecast.alert_state == PaceAlert.UNAVAILABLE
    mature = view(when=datetime(2026, 2, 6, tzinfo=UTC))
    assert mature.forecast.daily_pace_minor_units == 0
    assert pace(mature, PeriodId.ROLLING_7D) == 0
    assert mature.forecast.alert_state == PaceAlert.NORMAL


def test_pending_posted_transfer_and_unmatched_refund():
    rows = [
        row("2026-01-31", 20, pending=True, transaction_id="pending"),
        row("2026-01-31", 20, transaction_id="posted", pending_transaction_id="pending"),
        row(
            "2026-01-31",
            45,
            transaction_id="payment",
            pfc_primary="TRANSFER_OUT",
            pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
        ),
        row("2026-01-31", -8, transaction_id="mystery-refund", pfc_primary=None, pfc_detailed=None),
    ]
    result = calculate(
        policy(
            rules=[
                category_rule(field="pfc_detailed", value="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", kind=Kind.EXCLUDED),
                category_rule(field="pfc_primary", value="SHOPPING", kind=Kind.FLEXIBLE),
            ]
        ),
        rows,
        now=START,
        last_synced_at=START,
    )
    assert result.posted_minor_units == 2_000
    assert result.pending_minor_units == 0
    assert result.unmatched_refunds_minor_units == 800
    assert result.available_minor_units == 8_000
    inferred_refund = view([row("2026-01-31", -8)])
    assert inferred_refund.unmatched_refunds_minor_units == 800
    assert inferred_refund.available_minor_units == 10_000


def test_private_rule_and_uncertain_purchases():
    result = calculate(
        policy(rules=[name_rule(field="name", prefix="EXAMPLE", kind=Kind.FIXED)]),
        [row("2026-01-31", 42)],
        now=START,
        last_synced_at=START,
    )
    assert result.available_minor_units == 10_000
    assert (
        calculate(
            policy(rules=[name_rule("name", "EXAMPLE", Kind.FLEXIBLE)]),
            [row("2026-01-31", -5)],
            now=START,
            last_synced_at=START,
        ).available_minor_units
        == 10_000
    )
    uncertain = view([row("2026-01-31", 12, pfc_primary=None, pfc_detailed=None)])
    assert uncertain.available_minor_units == 8_800
    assert uncertain.review_minor_units == 1_200
    assert uncertain.review_transaction_count == 1
    assert unmatched(uncertain, PeriodId.ROLLING_7D) == (1, 1_200)
    prior = calculate(
        policy(activation_at=START_DATE, rules=[name_rule("name", "RENT ONLY", Kind.FIXED)]),
        [row("2026-01-30", 12)],
        now=START,
        last_synced_at=START,
    )
    assert prior.available_minor_units == 10_000
    assert prior.review_transaction_count == 0
    assert unmatched(prior, PeriodId.ROLLING_7D) == (1, 1_200)
    two_uncertain = view(
        [
            row("2026-01-31", 12, pfc_primary=None, pfc_detailed=None),
            row("2026-01-31", 3, pfc_primary=None, pfc_detailed=None),
        ]
    )
    assert two_uncertain.review_minor_units == 1_500
    assert two_uncertain.review_transaction_count == 2
    assert unmatched(two_uncertain, PeriodId.ROLLING_7D)[0] == 2
    with pytest.raises(ValidationError):
        Rule.model_validate(
            {"condition": {"type": "name_prefix", "field": "pfc_primary", "prefix": "SHOPPING"}, "kind": "excluded"}
        )
    assert policy().spending_account_ids == {"card-1"}


def test_compound_wire_rule_matches_only_named_beneficiary_and_wire_category():
    rule = Rule(
        condition=AllOf(
            conditions=[
                NameContains(field="name", substring="EXAMPLE BROKER"),
                CategoryExact(field="pfc_detailed", value="TRANSFER_OUT_WIRE"),
            ]
        ),
        kind=Kind.EXCLUDED,
    )
    assert Rule.model_validate(rule.model_dump()) == rule
    wire = row("2026-01-31", 500, pfc_detailed="TRANSFER_OUT_WIRE").model_copy(
        update={"name": "WIRE BENEFICIARY: Example Broker LLC"}
    )
    assert matching_rule(wire, [rule]) == rule
    assert matching_rule(wire.model_copy(update={"pfc_detailed": "GENERAL_SERVICES_LEGAL"}), [rule]) is None
    assert matching_rule(wire.model_copy(update={"name": "WIRE BENEFICIARY: OTHER BROKER"}), [rule]) is None
    assert calculate(policy(rules=[rule]), [wire], now=START, last_synced_at=START).available_minor_units == 10_000
    with pytest.raises(ValidationError):
        AllOf(conditions=[NameContains(field="name", substring="XX")])


def test_review_rule_uses_shared_conditions_and_preserves_uncertainty():
    review = Rule(
        condition=AllOf(
            conditions=[
                AmountSign(sign="positive"),
                AnyOf(
                    conditions=[
                        FieldExact(field="merchant_category_code", value="5812"),
                        NamePrefix(field="name", prefix="CAFE"),
                    ]
                ),
            ]
        ),
        kind=Kind.REVIEW,
        analysis_category="food_review",
    )
    assert Rule.model_validate(review.model_dump()) == review
    with pytest.raises(ValidationError, match="Unable to extract tag"):
        Rule.model_validate({"condition": {"sign": "positive"}, "kind": "review"})
    purchase = row("2026-01-31", 20).model_copy(update={"merchant_category_code": "5812"})
    assert matching_rule(purchase, [review]) == review
    result = calculate(policy(rules=[review]), [purchase], now=START, last_synced_at=START)
    assert result.available_minor_units == 8_000
    assert result.review_minor_units == 2_000
    assert result.review_transaction_count == 1
    assert matching_rule(purchase.model_copy(update={"amount": Decimal(-20)}), [review]) is None


def test_reviewed_negative_credit_is_not_spending_or_income():
    review = Rule(condition=AmountSign(sign="negative"), kind=Kind.REVIEW)
    result = calculate(policy(rules=[review]), [row("2026-01-31", -5)], now=START, last_synced_at=START)
    assert result.available_minor_units == 10_000
    assert result.unmatched_refunds_minor_units == 500


if __name__ == "__main__":
    pytest_bazel.main()
