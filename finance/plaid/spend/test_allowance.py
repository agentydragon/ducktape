"""Synthetic allowance contract tests; no real account or transaction data."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.plaid.spend.allowance import (
    AllOf,
    AllowancePolicy,
    AllowanceView,
    AmountExact,
    AmountSign,
    AnalysisCategory,
    AnyOf,
    CategoryExact,
    DateRange,
    FieldExact,
    Kind,
    NameContains,
    NamePrefix,
    PaceAlert,
    PeriodId,
    Rule,
    RuleScope,
    Status,
    Transaction,
    TransactionDecision,
    calculate,
    matching_rule,
    month_anniversary,
)
from finance.plaid.spend.configuration import SpendConfiguration, load_configuration
from finance.plaid.spend.models import AllowanceConfigurationView, SpendConfigurationView
from finance.plaid.spend.reconciliation_views import allowance_view
from util.bazel.runfiles import get_required_path

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
    amount: int | Decimal,
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
    return allowance_view(calculate(policy(), list(rows), time_zone=ZoneInfo("UTC"), now=when, last_synced_at=when))


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


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"authorized_datetime": "2026-03-08T07:30:00Z"}, "2026-03-07"),
        ({"authorized_datetime": "2026-03-08T10:30:00Z"}, "2026-03-08"),
        ({"authorized_datetime": "2026-11-01T08:30:00Z"}, "2026-11-01"),
        ({"authorized_datetime": "2026-11-01T09:30:00Z"}, "2026-11-01"),
        ({"authorized_datetime": "2026-03-08T23:30:00-08:00"}, "2026-03-09"),
        ({"posted_datetime": "2026-03-08T07:30:00Z"}, "2026-03-07"),
        ({"authorized_date": "2026-03-06", "posted_datetime": "2026-03-08T07:30:00Z"}, "2026-03-06"),
        ({"authorized_datetime": "2026-03-08T07:30:00Z", "authorized_date": "2026-03-06"}, "2026-03-07"),
        ({"authorized_datetime": "2026-03-08T00:00:00Z", "authorized_date": "2026-03-06"}, "2026-03-06"),
        ({"authorized_datetime": "2026-03-08T00:00:00-08:00"}, "2026-03-08"),
        ({"authorized_datetime": "2026-03-08T07:30:00"}, "2026-03-08"),
        ({"authorized_datetime": "invalid", "posted_datetime": "2026-03-08T07:30:00Z"}, "2026-03-07"),
        ({"posted_datetime": "2026-03-08T00:00:00Z"}, "2026-03-08"),
        ({}, "2026-03-08"),
    ],
)
def test_transaction_purchase_day(fields: dict[str, str], expected: str) -> None:
    transaction = Transaction.model_validate({**row("2026-03-08", 10).model_dump(), **fields})
    assert transaction.report_day(ZoneInfo("America/Los_Angeles")) == date.fromisoformat(expected)
    assert transaction.date == date(2026, 3, 8)


def test_purchase_day_controls_activation_and_periods_but_not_classification_dates() -> None:
    chosen = policy(
        activation_at=date(2026, 3, 8),
        rules=[
            Rule(
                condition=AllOf(
                    conditions=[
                        NamePrefix(field="name", prefix="EXAMPLE"),
                        DateRange(start=date(2026, 3, 9), end=date(2026, 3, 9)),
                    ]
                ),
                kind=Kind.REVIEW,
            )
        ],
    )
    before = Transaction.model_validate(
        {**row("2026-03-08", 10).model_dump(), "authorized_datetime": "2026-03-08T07:30:00Z"}
    )
    after = Transaction.model_validate(
        {**row("2026-03-09", 20).model_dump(), "authorized_datetime": "2026-03-08T10:30:00Z"}
    )
    now = datetime(2026, 3, 8, 20, tzinfo=UTC)
    decisions: list[TransactionDecision] = []
    report = allowance_view(
        calculate(
            chosen,
            [before, after],
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=now,
            last_synced_at=now,
            decisions=decisions,
        )
    )
    assert report.posted_minor_units == 2_000
    assert report.review_minor_units == 2_000  # rule still matches the original posting date
    assert spend(report, PeriodId.CREDIT_CYCLE) == 2_000
    assert spend(report, PeriodId.ROLLING_7D) == 2_000
    assert pace(report, PeriodId.ROLLING_7D) == 3_000 // 7  # includes pre-activation history
    assert sum(decision.allowance_minor_units for decision in decisions) == 2_000


def test_pacific_calendar_day_and_monthly_credit_follow_local_midnight():
    chosen = policy(activation_at=date(2026, 10, 5))
    transactions = [row("2026-10-05", 25), row("2026-11-04", 40), row("2026-11-05", 70)]
    before_activation = datetime(2026, 10, 5, 6, 59, tzinfo=UTC)
    with pytest.raises(ValueError, match="future"):
        allowance_view(
            calculate(
                chosen,
                [],
                time_zone=ZoneInfo("America/Los_Angeles"),
                now=before_activation,
                last_synced_at=before_activation,
            )
        )
    activated = datetime(2026, 10, 5, 7, tzinfo=UTC)
    first = allowance_view(
        calculate(
            chosen, transactions, time_zone=ZoneInfo("America/Los_Angeles"), now=activated, last_synced_at=activated
        )
    )
    assert first.next_credit_at == datetime.fromisoformat("2026-11-05T00:00:00-08:00")
    assert first.time_zone == "America/Los_Angeles"
    before_credit = datetime(2026, 11, 5, 7, 59, tzinfo=UTC)
    previous_day = allowance_view(
        calculate(
            chosen,
            transactions,
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=before_credit,
            last_synced_at=before_credit,
        )
    )
    assert previous_day.available_minor_units == 10_000 - 2_500 - 4_000
    assert previous_day.forecast.basis_period.end == date(2026, 11, 4)
    assert spend(previous_day, PeriodId.CREDIT_CYCLE) == 6_500
    at_credit = datetime(2026, 11, 5, 8, tzinfo=UTC)
    current_day = allowance_view(
        calculate(
            chosen, transactions, time_zone=ZoneInfo("America/Los_Angeles"), now=at_credit, last_synced_at=at_credit
        )
    )
    assert current_day.available_minor_units == 20_000 - 2_500 - 4_000 - 7_000
    assert current_day.forecast.basis_period.end == date(2026, 11, 5)
    assert current_day.next_credit_at == datetime.fromisoformat("2026-12-05T00:00:00-08:00")
    assert spend(current_day, PeriodId.CREDIT_CYCLE) == 7_000


def test_configuration_rejects_invalid_time_zone():
    with pytest.raises(ValidationError, match="time_zone"):
        SpendConfiguration.model_validate_json('{"time_zone":"not/a-real-zone","cards":[]}')


def test_configuration_requires_root_time_zone_and_rejects_legacy_allowance_time_zone():
    with pytest.raises(ValidationError, match="time_zone"):
        SpendConfiguration.model_validate({"cards": []})

    configuration = SpendConfiguration.model_validate({"time_zone": "America/Los_Angeles", "cards": []})
    assert configuration.time_zone == ZoneInfo("America/Los_Angeles")

    allowance_configuration = policy().model_dump(mode="json")
    allowance_configuration["time_zone"] = "UTC"
    with pytest.raises(ValidationError, match="time_zone"):
        SpendConfiguration.model_validate({"time_zone": "UTC", "cards": [], "allowance": allowance_configuration})


def test_missing_root_time_zone_error_hides_account_selectors():
    allowance_configuration = policy().model_dump(mode="json")
    allowance_configuration["spending_account_ids"] = ["synthetic-nested-account-selector"]
    with pytest.raises(ValidationError) as error:
        SpendConfiguration.model_validate(
            {
                "cards": [{"account_id": "synthetic-root-card-selector", "label": "Example card", "enabled": True}],
                "allowance": allowance_configuration,
            }
        )
    assert "synthetic-root-card-selector" not in str(error.value)
    assert "synthetic-nested-account-selector" not in str(error.value)


def test_single_config_parses_cards_and_optional_allowance():
    without_allowance = SpendConfiguration.model_validate_json('{"time_zone":"UTC","cards":[]}')
    assert without_allowance.allowance is None
    assert without_allowance.time_zone == ZoneInfo("UTC")
    config = SpendConfiguration.model_validate_json(
        '{"time_zone":"UTC","cards":[],"allowance":{"monthly_minor_units":10000,"activation_at":"2026-01-31",'
        '"spending_account_ids":["example-card"],'
        '"analysis_categories":{"unclassified":{"label":"Unclassified","color":"#64748B"}},'
        '"rules":[{"condition":{"type":"name_prefix","field":"name","prefix":"EXAMPLE"},"kind":"flexible"}]}}'
    )
    assert config.allowance is not None
    assert config.allowance.monthly_minor_units == 10_000
    assert config.allowance.spending_account_ids == {"example-card"}
    assert config.allowance.rules[0] == name_rule("name", "EXAMPLE", Kind.FLEXIBLE)
    with pytest.raises(ValidationError):
        SpendConfiguration.model_validate_json(
            '{"time_zone":"UTC","cards":[],"allowance":{"monthly_minor_units":10000}}'
        )
    with pytest.raises(ValidationError):
        SpendConfiguration.model_validate_json(
            '{"time_zone":"UTC","cards":[],"allowance":{"monthly_minor_units":10000,'
            '"spending_account_ids":["example-card"],'
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
    result = allowance_view(
        calculate(
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
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
        )
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
    result = allowance_view(
        calculate(selected_policy, transactions, time_zone=ZoneInfo("UTC"), now=now, last_synced_at=now)
    )
    assert result.available_minor_units == 10_000
    assert pace(result, PeriodId.ROLLING_7D) is None
    assert pace(result, PeriodId.ROLLING_30D) == 20_000 // 30
    assert result.forecast.alert_state == PaceAlert.UNAVAILABLE
    assert result.spending_signal == PaceAlert.UNAVAILABLE
    monthly = allowance_view(
        calculate(
            selected_policy,
            transactions,
            time_zone=ZoneInfo("UTC"),
            now=now,
            last_synced_at=now,
            estimate_period_id=PeriodId.ROLLING_30D,
        )
    )
    assert monthly.forecast.basis_period.id == PeriodId.ROLLING_30D
    assert monthly.spending_signal == PaceAlert.WARNING


def test_daily_reference_is_rounded_for_wire_but_unrounded_rate_drives_signal():
    now = datetime(2026, 1, 31, tzinfo=UTC)
    result = allowance_view(
        calculate(
            policy(activation_at=date(2025, 12, 1)),
            [row("2026-01-30", Decimal("23.03"))],
            time_zone=ZoneInfo("UTC"),
            now=now,
            last_synced_at=now,
        )
    )

    # 10,000 * 12 / 365.2425 is about 328.55 minor units. The wire value rounds to 329,
    # while an observed pace of 329 still exceeds the unrounded business rate.
    assert result.daily_reference_minor_units == 329
    assert pace(result, PeriodId.ROLLING_7D) == 329
    assert result.forecast.alert_state == PaceAlert.NORMAL
    assert result.spending_signal == PaceAlert.WARNING


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
    result = allowance_view(
        calculate(
            policy(
                rules=[
                    category_rule(field="pfc_detailed", value="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", kind=Kind.EXCLUDED),
                    category_rule(field="pfc_primary", value="SHOPPING", kind=Kind.FLEXIBLE),
                ]
            ),
            rows,
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
        )
    )
    assert result.posted_minor_units == 2_000
    assert result.pending_minor_units == 0
    assert result.unmatched_refunds_minor_units == 800
    assert result.available_minor_units == 8_000
    inferred_refund = view([row("2026-01-31", -8)])
    assert inferred_refund.unmatched_refunds_minor_units == 800
    assert inferred_refund.available_minor_units == 10_000


def test_private_rule_and_uncertain_purchases():
    result = allowance_view(
        calculate(
            policy(rules=[name_rule(field="name", prefix="EXAMPLE", kind=Kind.FIXED)]),
            [row("2026-01-31", 42)],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
        )
    )
    assert result.available_minor_units == 10_000
    assert (
        allowance_view(
            calculate(
                policy(rules=[name_rule("name", "EXAMPLE", Kind.FLEXIBLE)]),
                [row("2026-01-31", -5)],
                time_zone=ZoneInfo("UTC"),
                now=START,
                last_synced_at=START,
            )
        ).available_minor_units
        == 10_000
    )
    uncertain = view([row("2026-01-31", 12, pfc_primary=None, pfc_detailed=None)])
    assert uncertain.available_minor_units == 8_800
    assert uncertain.review_minor_units == 1_200
    assert uncertain.review_transaction_count == 1
    assert unmatched(uncertain, PeriodId.ROLLING_7D) == (1, 1_200)
    prior = allowance_view(
        calculate(
            policy(activation_at=START_DATE, rules=[name_rule("name", "RENT ONLY", Kind.FIXED)]),
            [row("2026-01-30", 12)],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
        )
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
    result = allowance_view(
        calculate(policy(rules=[rule]), [wire], time_zone=ZoneInfo("UTC"), now=START, last_synced_at=START)
    )
    assert result.available_minor_units == 10_000
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
    result = allowance_view(
        calculate(policy(rules=[review]), [purchase], time_zone=ZoneInfo("UTC"), now=START, last_synced_at=START)
    )
    assert result.available_minor_units == 8_000
    assert result.review_minor_units == 2_000
    assert result.review_transaction_count == 1
    assert matching_rule(purchase.model_copy(update={"amount": Decimal(-20)}), [review]) is None


def test_reviewed_negative_credit_is_not_spending_or_income():
    review = Rule(condition=AmountSign(sign="negative"), kind=Kind.REVIEW)
    result = allowance_view(
        calculate(
            policy(rules=[review]), [row("2026-01-31", -5)], time_zone=ZoneInfo("UTC"), now=START, last_synced_at=START
        )
    )
    assert result.available_minor_units == 10_000
    assert result.unmatched_refunds_minor_units == 500


def one_off(
    rule_id: str,
    kind: Kind,
    *,
    prefix: str = "EXAMPLE",
    day: str = "2026-01-31",
    amount: str | None = None,
    analysis_category: str | None = None,
    note: str = "Corrected by the owner 2026-02-01.",
) -> Rule:
    """A decision about one observed transaction: a Rule whose scope narrows it to a bounded match."""
    conditions: list = [
        NamePrefix(field="name", prefix=prefix),
        DateRange(start=date.fromisoformat(day), end=date.fromisoformat(day)),
    ]
    if amount is not None:
        conditions.append(AmountExact(value=amount))
    return Rule(
        condition=AllOf(conditions=conditions),
        kind=kind,
        scope=RuleScope.ONE_OFF,
        id=rule_id,
        analysis_category=analysis_category,
        description=note,
    )


# A rule that matches nothing, so the un-overridden path stays the default-flexible behaviour.
NO_MATCH_RULE = name_rule("name", "NO OTHER MERCHANT", Kind.FIXED)


def test_one_off_addresses_a_transaction_by_match_not_by_plaid_id():
    correction = one_off("holiday-stay-is-travel", Kind.FIXED, amount="60.00", analysis_category="fixed_travel")
    purchase = row("2026-01-31", 60)
    assert matching_rule(purchase, [correction]) == correction
    assert matching_rule(purchase.model_copy(update={"name": "OTHER SHOP"}), [correction]) is None
    assert matching_rule(purchase.model_copy(update={"amount": Decimal(61)}), [correction]) is None
    assert matching_rule(purchase.model_copy(update={"date": date(2026, 2, 1)}), [correction]) is None
    # A relink that rewrites Plaid ids cannot dangle the correction: ids are not consulted.
    assert matching_rule(purchase.model_copy(update={"transaction_id": "relinked-id"}), [correction]) == correction


def test_one_off_precedes_the_patterns_it_corrects_by_position():
    purchase = row("2026-01-31", 60)
    pattern = name_rule("name", "EXAMPLE", Kind.FLEXIBLE)
    default = allowance_view(
        calculate(
            policy(rules=[name_rule("name", "NO OTHER MERCHANT", Kind.FIXED)]),
            [purchase],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
        )
    )
    assert default.available_minor_units == 4_000
    assert default.review_transaction_count == 1

    corrected = allowance_view(
        calculate(
            policy(rules=[one_off("rent", Kind.FIXED), pattern]),
            [purchase],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
        )
    )
    assert corrected.available_minor_units == 10_000
    assert corrected.review_transaction_count == 0

    excluded = allowance_view(
        calculate(
            policy(rules=[one_off("transfer", Kind.EXCLUDED), name_rule("name", "EXAMPLE", Kind.FIXED)]),
            [purchase],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
        )
    )
    assert excluded.available_minor_units == 10_000

    # Position is the whole precedence story: placed last, the one-off never governs.
    decisions: list = []
    allowance_view(
        calculate(
            policy(rules=[pattern, one_off("rent", Kind.FIXED)]),
            [purchase],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
            decisions=decisions,
        )
    )
    assert decisions[0].rule_number == 1
    governed = decisions[0].rule
    assert governed is not None
    assert governed.scope == RuleScope.PATTERN
    assert decisions[0].effective_kind == Kind.FLEXIBLE

    decisions = []
    allowance_view(
        calculate(
            policy(rules=[one_off("rent", Kind.FIXED), pattern]),
            [purchase],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
            decisions=decisions,
        )
    )
    applied = decisions[0].rule
    assert applied is not None
    assert applied.id == "rent"
    assert applied.scope == RuleScope.ONE_OFF
    assert applied.description == "Corrected by the owner 2026-02-01."
    # A one-off is a rule, so it is reported by its position in the same list and resolves to its own kind.
    assert decisions[0].rule_number == 1
    assert decisions[0].effective_kind == Kind.FIXED

    without: list = []
    allowance_view(
        calculate(
            policy(rules=[pattern]),
            [purchase],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
            decisions=without,
        )
    )
    assert without[0].rule_number == 1
    assert without[0].effective_kind == Kind.FLEXIBLE

    unclassified: list = []
    allowance_view(
        calculate(
            policy(rules=[name_rule("name", "NO OTHER MERCHANT", Kind.FIXED)]),
            [purchase],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
            decisions=unclassified,
        )
    )
    assert unclassified[0].effective_kind is None


def test_one_off_can_keep_a_purchase_counted_for_review():
    review = one_off("unclear-charge", Kind.REVIEW, analysis_category="merchant_review")
    result = allowance_view(
        calculate(
            policy(rules=[name_rule("name", "NO OTHER MERCHANT", Kind.FIXED), review]),
            [row("2026-01-31", 20)],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
        )
    )
    assert result.available_minor_units == 8_000
    assert result.review_minor_units == 2_000
    assert result.review_transaction_count == 1


def test_one_off_on_a_negative_credit_avoids_the_held_refund_bucket():
    correction = Rule(
        condition=AllOf(
            conditions=[
                NamePrefix(field="name", prefix="EXAMPLE"),
                DateRange(start=date(2026, 1, 31), end=date(2026, 1, 31)),
                AmountSign(sign="negative"),
            ]
        ),
        kind=Kind.EXCLUDED,
        scope=RuleScope.ONE_OFF,
        id="reversal-pair",
        description="Paired reversal leg; confirmed by the owner 2026-02-01.",
    )
    credit = row("2026-01-31", -250)
    standing = name_rule("name", "NO OTHER MERCHANT", Kind.FIXED)
    held = allowance_view(
        calculate(policy(rules=[standing]), [credit], time_zone=ZoneInfo("UTC"), now=START, last_synced_at=START)
    )
    assert held.unmatched_refunds_minor_units == 25_000
    paired = allowance_view(
        calculate(
            policy(rules=[correction, standing]), [credit], time_zone=ZoneInfo("UTC"), now=START, last_synced_at=START
        )
    )
    assert paired.unmatched_refunds_minor_units == 0
    assert paired.available_minor_units == 10_000
    decisions: list = []
    allowance_view(
        calculate(
            policy(rules=[correction, standing]),
            [credit],
            time_zone=ZoneInfo("UTC"),
            now=START,
            last_synced_at=START,
            decisions=decisions,
        )
    )
    applied = decisions[0].rule
    assert applied is not None
    assert applied.id == "reversal-pair"
    assert decisions[0].effective_kind == Kind.EXCLUDED


def test_one_off_scope_is_validated_from_its_condition():
    def build(conditions: list, rule_id: str = "example") -> dict:
        return {
            "condition": {"type": "all_of", "conditions": conditions},
            "kind": "excluded",
            "scope": "one_off",
            "id": rule_id,
            "description": "owner confirmed 2026-02-01.",
        }

    named = {"type": "name_prefix", "field": "name", "prefix": "EXAMPLE"}
    one_day = {"type": "date_range", "start": "2026-01-31", "end": "2026-01-31"}
    assert Rule.model_validate(build([named, one_day]))
    with pytest.raises(ValidationError, match="all_of"):
        Rule.model_validate(
            {"condition": named, "kind": "excluded", "scope": "one_off", "id": "example", "description": "confirmed."}
        )
    with pytest.raises(ValidationError, match="all_of"):
        Rule.model_validate(build([{"type": "any_of", "conditions": [named, one_day]}]))
    with pytest.raises(ValidationError, match="date_range"):
        Rule.model_validate(build([named, {"type": "date_range", "start": "2026-01-31"}]))
    with pytest.raises(ValidationError, match="at most"):
        Rule.model_validate(build([named, {"type": "date_range", "start": "2026-01-01", "end": "2026-06-30"}]))
    with pytest.raises(ValidationError, match="name, category, amount, field, or counterparty"):
        Rule.model_validate(build([one_day, {"type": "amount_sign", "sign": "positive"}]))
    with pytest.raises(ValidationError, match="requires an id"):
        Rule.model_validate(
            {
                "condition": {"type": "all_of", "conditions": [named, one_day]},
                "kind": "excluded",
                "scope": "one_off",
                "description": "confirmed.",
            }
        )
    with pytest.raises(ValidationError, match="requires a description"):
        Rule.model_validate(
            {
                "condition": {"type": "all_of", "conditions": [named, one_day]},
                "kind": "excluded",
                "scope": "one_off",
                "id": "unexplained",
            }
        )
    with pytest.raises(ValidationError, match="only a one_off rule takes an id"):
        Rule.model_validate(
            {
                "condition": {"type": "name_prefix", "field": "name", "prefix": "EXAMPLE"},
                "kind": "excluded",
                "id": "mislabelled-pattern",
            }
        )
    with pytest.raises(ValidationError, match="unique ids"):
        policy(
            rules=[
                name_rule("name", "NO OTHER MERCHANT", Kind.FIXED),
                one_off("same", Kind.EXCLUDED),
                one_off("same", Kind.FIXED),
            ]
        )
    unconfigured = policy(rules=[name_rule("name", "NO OTHER MERCHANT", Kind.FIXED)]).model_dump(mode="json")
    unconfigured["rules"].append(
        one_off("missing", Kind.FIXED, analysis_category="absent_category").model_dump(mode="json")
    )
    with pytest.raises(ValidationError, match="analysis_categories is missing"):
        AllowancePolicy.model_validate(unconfigured)


def test_configuration_preserves_financial_yaml_decimal_tokens(tmp_path: Path) -> None:
    example = get_required_path("_main/finance/plaid/spend/one-off-configuration.yaml")
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        example.read_text()
        + """
sustainability:
  portfolio:
    sources:
      plaid:
        asset_account_ids: [example-brokerage]
        holding_value_corrections:
          - account_id: example-brokerage
            security_ids: [example-fund]
            value_multiplier: 1.00000000000000000000000000001
  withdrawal_tax:
    rate: 0.10000000000000000000000000001
    assumption_note: Synthetic precision test
    as_of: 2026-10-27
""",
        encoding="utf-8",
    )
    loaded = load_configuration(policy)
    assert loaded.sustainability is not None
    assert loaded.sustainability.withdrawal_tax.rate == Decimal("0.10000000000000000000000000001")
    [correction] = loaded.sustainability.portfolio.sources.plaid.holding_value_corrections
    assert correction.value_multiplier == Decimal("1.00000000000000000000000000001")


def test_one_off_yaml_round_trip_and_configuration_view():
    path = get_required_path("_main/finance/plaid/spend/one-off-configuration.yaml")
    loaded = load_configuration(path)
    parsed = loaded.allowance
    assert parsed is not None
    assert [rule.id for rule in parsed.rules] == ["example-hotel-2026-06-04", None]
    assert [rule.scope for rule in parsed.rules] == [RuleScope.ONE_OFF, RuleScope.PATTERN]
    assert SpendConfiguration.model_validate(loaded.model_dump(mode="json")) == loaded
    assert load_configuration(path) == loaded
    view = SpendConfigurationView(
        time_zone=loaded.time_zone.key,
        cards=[],
        allowance=AllowanceConfigurationView(
            monthly_minor_units=parsed.monthly_minor_units,
            activation_at=parsed.activation_at,
            currency=parsed.currency,
            spending_account_count=len(parsed.spending_account_ids),
            max_sync_age_hours=parsed.max_sync_age_hours,
            forecast_basis_period_id=parsed.forecast_basis_period_id,
            rules=parsed.rules,
            analysis_categories=parsed.analysis_categories,
        ),
    )
    assert view.allowance is not None
    assert view.allowance.rules[0].description is not None
    assert view.allowance.rules[0].description.startswith("Confirmed example")


if __name__ == "__main__":
    pytest_bazel.main()
