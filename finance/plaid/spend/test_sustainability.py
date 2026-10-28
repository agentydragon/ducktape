"""Funding-rate accounting boundaries using synthetic purchases and portfolios."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.plaid.spend.allowance import (
    AllOf,
    AllowancePolicy,
    AnalysisCategory,
    DateRange,
    Kind,
    NamePrefix,
    Rule,
    RuleScope,
    Transaction,
)
from finance.plaid.spend.configuration import SpendConfiguration
from finance.plaid.spend.portfolio_models import PlaidSourcePolicy, PortfolioPolicy, PortfolioSources, PortfolioView
from finance.plaid.spend.reconciliation_views import sustainability_view
from finance.plaid.spend.sustainability import (
    AccountHistory,
    AccountOpening,
    DocumentedHistory,
    SustainabilityDisplay,
    SustainabilityPolicy,
    SustainabilityView,
    WithdrawalTax,
    calculate_sustainability,
)

_NOW = datetime(2026, 4, 1, 7, tzinfo=UTC)  # April 1 midnight in Pacific.


def policy(
    *, tax_rate: Decimal | None = None, account_history: dict[str, AccountHistory] | None = None
) -> SustainabilityPolicy:
    return SustainabilityPolicy(
        display=SustainabilityDisplay(windows_days=[30, 90, 365], default_window_days=90),
        portfolio=PortfolioPolicy(sources=PortfolioSources(plaid=PlaidSourcePolicy(asset_account_ids=["assets"]))),
        withdrawal_tax=WithdrawalTax()
        if tax_rate is None
        else WithdrawalTax(rate=tax_rate, assumption_note="Synthetic effective-tax scenario.", as_of=date(2026, 1, 1)),
        confirmed_refund_rule_ids=["confirmed-refund"],
        account_history=account_history or {},
    )


def allowance(*, spending_account_ids: set[str] | None = None) -> AllowancePolicy:
    return AllowancePolicy(
        monthly_minor_units=10_000,
        activation_at=date(2026, 3, 20),
        spending_account_ids={"card"} if spending_account_ids is None else spending_account_ids,
        rules=[
            Rule(
                condition=AllOf(
                    conditions=[
                        NamePrefix(field="name", prefix="CONFIRMED REFUND"),
                        DateRange(start=date(2026, 3, 6), end=date(2026, 3, 6)),
                    ]
                ),
                kind=Kind.FLEXIBLE,
                scope=RuleScope.ONE_OFF,
                id="confirmed-refund",
                description="Synthetic fixture confirms this refund on 2026-03-06.",
            ),
            Rule(condition=NamePrefix(field="name", prefix="RENT"), kind=Kind.FIXED),
            Rule(condition=NamePrefix(field="name", prefix="TRANSFER"), kind=Kind.EXCLUDED),
            Rule(condition=NamePrefix(field="name", prefix="SHOP"), kind=Kind.FLEXIBLE),
        ],
        analysis_categories={"unclassified": AnalysisCategory(label="Unclassified", color="#777777")},
    )


def transaction(
    day: date,
    amount: str,
    name: str = "SHOP",
    *,
    key: str = "purchase",
    pending: bool = False,
    replacement: str | None = None,
    instant: datetime | None = None,
    account_id: str = "card",
) -> Transaction:
    return Transaction(
        account_id=account_id,
        transaction_id=key,
        pending_transaction_id=replacement,
        date=day,
        authorized_datetime=instant,
        amount=Decimal(amount),
        pending=pending,
        name=name,
        merchant_name=None,
        pfc_primary=None,
        pfc_detailed=None,
        currency="USD",
    )


def evaluate_account_history(
    transactions: list[Transaction],
    *,
    account_history: dict[str, AccountHistory],
    spending_account_ids: set[str] | None = None,
    account_labels: dict[str, str] | None = None,
    window_days: int = 90,
) -> SustainabilityView:
    selected_ids = {"card"} if spending_account_ids is None else spending_account_ids
    return sustainability_view(
        calculate_sustainability(
            policy(tax_rate=Decimal("0.2"), account_history=account_history),
            allowance(spending_account_ids=selected_ids),
            transactions,
            portfolio(),
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=_NOW,
            window_days=window_days,
            source_issues=[],
            account_labels=account_labels,
        )
    )


def portfolio(*, ready: bool = True) -> PortfolioView:
    return PortfolioView(
        status="ready" if ready else "unavailable",
        net_minor_units=1_000_000 if ready else None,
        assets_minor_units=1_200_000 if ready else None,
        liabilities_minor_units=100_000 if ready else None,
        excluded_minor_units=100_000 if ready else None,
        sources=[],
    )


def test_total_consumption_includes_precommitment_mandatory_and_pending_once() -> None:
    result = sustainability_view(
        calculate_sustainability(
            policy(tax_rate=Decimal("0.2")),
            allowance(),
            [
                transaction(date(2026, 1, 1), "0", "TRANSFER", key="history"),
                transaction(date(2026, 3, 1), "100", "RENT", key="rent"),
                transaction(date(2026, 3, 2), "20", pending=True, key="pending"),
                transaction(date(2026, 3, 3), "25", replacement="pending", key="posted"),
                transaction(date(2026, 3, 4), "10", "UNKNOWN", key="unknown"),
                transaction(date(2026, 3, 5), "5000", "TRANSFER", key="transfer"),
                transaction(date(2026, 3, 6), "-5", "CONFIRMED REFUND", key="refund"),
                transaction(date(2026, 3, 7), "-50", key="held-credit"),
                transaction(date(2026, 4, 1), "900", key="today"),
            ],
            portfolio(),
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=_NOW,
            window_days=90,
            source_issues=[],
        )
    )
    assert result.status == "ready"
    assert result.covered_days == 90
    assert (result.mandatory_minor_units, result.flexible_minor_units, result.provisional_minor_units) == (
        10_000,
        2000,
        1000,
    )
    assert result.spending_minor_units == 13_000
    assert result.annualized_spending_minor_units == 52_757
    assert result.estimated_annual_draw_minor_units == 65_946
    assert result.tax_adjusted_draw_rate == Decimal("0.065946")
    result_json = result.model_dump(mode="json")
    assert result_json["tax_adjusted_draw_rate"] == "0.065946"
    assert result_json["effective_tax_rate"] == "0.2"
    assert result_json["reference_draw_rates"] == ["0.03", "0.04"]
    assert result.history[-1].annualized_spending_minor_units == result.annualized_spending_minor_units
    assert result.history[-1].tax_adjusted_draw_rate == result.tax_adjusted_draw_rate
    assert any("unconfirmed" in issue for issue in result.issues)


def test_partial_window_uses_actual_covered_days_and_pacific_instant() -> None:
    result = sustainability_view(
        calculate_sustainability(
            policy(),
            allowance(),
            [
                transaction(date(2026, 3, 22), "10", key="start"),
                transaction(date(2026, 4, 1), "10", key="cross-midnight", instant=datetime(2026, 4, 1, 6, tzinfo=UTC)),
            ],
            portfolio(),
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=_NOW,
            window_days=90,
            source_issues=[],
        )
    )
    assert result.covered_days == 10
    assert result.period_end == date(2026, 3, 31)
    assert result.spending_minor_units == 2000
    assert result.annualized_spending_minor_units == 73_049
    assert result.tax_adjusted_draw_rate is None
    assert any("Only 10 of 90" in issue for issue in result.issues)
    assert result.history == []


@pytest.mark.parametrize("covered_days", [29, 30, 31])
def test_history_starts_at_first_complete_window(covered_days: int) -> None:
    end = date(2026, 3, 31)
    first_day = end - timedelta(days=covered_days - 1)
    result = sustainability_view(
        calculate_sustainability(
            policy(),
            allowance(),
            [
                transaction(first_day, "0", "TRANSFER", key="history"),
                transaction(first_day + timedelta(days=1), "3000", "RENT", key="rent"),
            ],
            portfolio(),
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=_NOW,
            window_days=30,
            source_issues=[],
        )
    )
    assert len(result.history) == max(0, covered_days - 29)
    if result.history:
        assert result.history[0].date == first_day + timedelta(days=29)
        assert all(point.covered_days == 30 for point in result.history)
        assert result.history[0].annualized_spending_minor_units == 3_652_425
        assert result.history[-1].annualized_spending_minor_units == result.annualized_spending_minor_units


def test_history_window_includes_both_endpoints_and_excludes_adjacent_days() -> None:
    result = sustainability_view(
        calculate_sustainability(
            policy(),
            allowance(),
            [
                transaction(date(2026, 1, 1), "0", "TRANSFER", key="history"),
                transaction(date(2026, 3, 1), "9000", key="before-window"),
                transaction(date(2026, 3, 2), "10", key="first-in-window"),
                transaction(date(2026, 3, 31), "20", key="last-in-window"),
                transaction(date(2026, 4, 1), "9000", key="today"),
            ],
            portfolio(),
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=_NOW,
            window_days=30,
            source_issues=[],
        )
    )
    assert result.spending_minor_units == 3000
    assert result.annualized_spending_minor_units == 36_524
    assert result.history[-1].annualized_spending_minor_units == 36_524
    assert result.history[-1].covered_days == 30


def test_missing_portfolio_preserves_observed_spending_without_showing_rate() -> None:
    result = sustainability_view(
        calculate_sustainability(
            policy(),
            allowance(),
            [transaction(date(2026, 1, 1), "10")],
            portfolio(ready=False),
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=_NOW,
            window_days=90,
            source_issues=[],
        )
    )
    assert result.status == "unavailable"
    assert result.annualized_spending_minor_units == 4058
    assert result.spending_draw_rate is None
    assert all(point.spending_draw_rate is None for point in result.history)


def test_stale_spending_does_not_produce_a_funding_estimate() -> None:
    result = sustainability_view(
        calculate_sustainability(
            policy(),
            allowance(),
            [transaction(date(2026, 1, 1), "10")],
            portfolio(),
            time_zone=ZoneInfo("America/Los_Angeles"),
            now=_NOW,
            window_days=90,
            source_issues=["Spending source is stale."],
        )
    )
    assert result.status == "unavailable"
    assert result.annualized_spending_minor_units is None
    assert result.spending_draw_rate is None


def test_opening_and_complete_history_cover_zero_spending_without_transactions() -> None:
    account_id = "synthetic-account-key"
    evidence = AccountHistory(
        opening=AccountOpening(
            earliest=date(2026, 2, 1),
            latest=date(2026, 2, 1),
            source="Synthetic account-opening confirmation.",
            as_of=date(2026, 3, 31),
        ),
        coverage=DocumentedHistory(
            complete_from=date(2026, 2, 1), source="Synthetic complete-history confirmation.", as_of=date(2026, 3, 31)
        ),
    )

    result = evaluate_account_history(
        [],
        account_history={account_id: evidence},
        spending_account_ids={account_id},
        account_labels={account_id: "Everyday card"},
    )

    assert result.covered_days == 90
    assert result.period_start == date(2026, 1, 1)
    assert result.spending_minor_units == 0
    assert result.annualized_spending_minor_units == 0
    assert result.account_coverage[0].account_label == "Everyday card"
    assert result.account_coverage[0].first_recorded_on is None
    assert result.account_coverage[0].unknown_days_in_window == 0


def test_documented_history_alone_covers_an_empty_account() -> None:
    evidence = AccountHistory(
        coverage=DocumentedHistory(
            complete_from=date(2026, 1, 1), source="Synthetic complete-history confirmation.", as_of=date(2026, 3, 31)
        )
    )

    result = evaluate_account_history([], account_history={"card": evidence})

    assert result.covered_days == 90
    assert result.spending_minor_units == 0
    assert result.annualized_spending_minor_units == 0
    assert result.account_coverage[0].first_recorded_on is None
    assert result.account_coverage[0].unknown_days_in_window == 0


def test_multiple_accounts_combine_transaction_and_documented_coverage() -> None:
    old_account = "synthetic-older-account-key"
    new_account = "synthetic-newer-account-key"
    evidence = AccountHistory(
        opening=AccountOpening(
            earliest=date(2026, 2, 1),
            latest=date(2026, 2, 1),
            source="Synthetic account-opening confirmation.",
            as_of=date(2026, 3, 31),
        ),
        coverage=DocumentedHistory(
            complete_from=date(2026, 2, 1), source="Synthetic complete-history confirmation.", as_of=date(2026, 3, 31)
        ),
    )
    result = evaluate_account_history(
        [
            transaction(
                date(2026, 1, 1), "5000", "TRANSFER", key="synthetic-old-account-transfer", account_id=old_account
            ),
            transaction(date(2026, 1, 2), "10", key="synthetic-old-account-purchase", account_id=old_account),
            transaction(date(2026, 2, 5), "20", key="synthetic-new-account-purchase", account_id=new_account),
        ],
        account_history={new_account: evidence},
        spending_account_ids={old_account, new_account},
        account_labels={old_account: "Older account", new_account: "Newer account"},
    )

    assert result.covered_days == 90
    assert result.period_start == date(2026, 1, 1)
    assert result.spending_minor_units == 3000
    assert result.annualized_spending_minor_units == 12_175
    assert result.estimated_annual_draw_minor_units == 15_219
    assert {coverage.account_label for coverage in result.account_coverage} == {"Older account", "Newer account"}
    assert all(coverage.unknown_days_in_window == 0 for coverage in result.account_coverage)


def test_future_opening_proves_an_empty_account_was_absent_through_the_report_window() -> None:
    evidence = AccountHistory(
        opening=AccountOpening(
            earliest=date(2026, 7, 1),
            latest=date(2026, 7, 1),
            source="Synthetic scheduled-opening confirmation.",
            as_of=date(2026, 3, 31),
        )
    )

    result = evaluate_account_history([], account_history={"card": evidence})

    assert result.covered_days == 90
    assert result.spending_minor_units == 0
    assert result.annualized_spending_minor_units == 0
    assert result.account_coverage[0].unknown_days_in_window == 0


def test_opening_alone_leaves_a_gap_until_first_recorded_transaction() -> None:
    evidence = AccountHistory(
        opening=AccountOpening(
            earliest=date(2026, 2, 1),
            latest=date(2026, 2, 1),
            source="Synthetic account-opening confirmation.",
            as_of=date(2026, 3, 31),
        )
    )
    result = evaluate_account_history(
        [transaction(date(2026, 3, 1), "10", key="synthetic-first-transaction")], account_history={"card": evidence}
    )

    assert result.period_start == date(2026, 3, 1)
    assert result.covered_days == 31
    assert result.spending_minor_units == 1000
    assert result.account_coverage[0].first_recorded_on == date(2026, 3, 1)
    assert result.account_coverage[0].unknown_days_in_window == 28


def test_uncertain_opening_bounds_only_prove_absence_before_earliest_date() -> None:
    evidence = AccountHistory(
        opening=AccountOpening(
            earliest=date(2026, 2, 1),
            latest=date(2026, 2, 15),
            source="Synthetic account-opening record with uncertain date.",
            as_of=date(2026, 3, 31),
        )
    )
    result = evaluate_account_history([], account_history={"card": evidence})

    # The gap includes the full period from earliest through latest and the later unobserved days.
    assert result.account_coverage[0].unknown_days_in_window == 59
    assert result.account_coverage[0].first_recorded_on is None
    assert result.annualized_spending_minor_units is None
    assert result.spending_draw_rate is None


def test_history_keeps_covered_windows_nulls_gap_windows_and_resumes_after_full_window() -> None:
    evidence = AccountHistory(
        opening=AccountOpening(
            earliest=date(2026, 2, 1),
            latest=date(2026, 2, 1),
            source="Synthetic account-opening confirmation.",
            as_of=date(2026, 3, 31),
        ),
        coverage=DocumentedHistory(
            complete_from=date(2026, 3, 1), source="Synthetic complete-history confirmation.", as_of=date(2026, 3, 31)
        ),
    )
    result = evaluate_account_history(
        [transaction(date(2026, 3, 15), "10", key="synthetic-current-history")],
        account_history={"card": evidence},
        window_days=30,
    )
    points = {point.date: point for point in result.history}

    assert points[date(2026, 1, 30)].annualized_spending_minor_units == 0
    assert points[date(2026, 1, 31)].annualized_spending_minor_units == 0
    assert points[date(2026, 2, 15)].annualized_spending_minor_units is None
    assert points[date(2026, 3, 29)].annualized_spending_minor_units is None
    assert points[date(2026, 3, 30)].annualized_spending_minor_units == 12_175
    assert points[date(2026, 3, 31)].annualized_spending_minor_units == 12_175
    assert points[date(2026, 3, 30)].tax_adjusted_draw_rate == Decimal("0.015219")
    assert points[date(2026, 3, 31)].tax_adjusted_draw_rate == Decimal("0.015219")


def test_earlier_history_rates_survive_an_unavailable_current_suffix() -> None:
    older_account = "synthetic-earlier-history-account-key"
    newer_account = "synthetic-uncovered-current-account-key"
    evidence = {
        older_account: AccountHistory(
            coverage=DocumentedHistory(
                complete_from=date(2026, 1, 1),
                source="Synthetic complete-history confirmation.",
                as_of=date(2026, 3, 31),
            )
        ),
        newer_account: AccountHistory(
            opening=AccountOpening(
                earliest=date(2026, 2, 1),
                latest=date(2026, 2, 1),
                source="Synthetic account-opening confirmation.",
                as_of=date(2026, 3, 31),
            )
        ),
    }
    result = evaluate_account_history(
        [transaction(date(2026, 1, 15), "10", key="synthetic-earlier-purchase", account_id=older_account)],
        account_history=evidence,
        spending_account_ids={older_account, newer_account},
        window_days=30,
    )
    points = {point.date: point for point in result.history}

    assert result.status == "unavailable"
    assert result.covered_days == 0
    assert result.annualized_spending_minor_units is None
    assert result.spending_draw_rate is None
    assert points[date(2026, 1, 31)].annualized_spending_minor_units == 12_175
    assert points[date(2026, 1, 31)].spending_draw_rate == Decimal("0.012175")
    assert points[date(2026, 1, 31)].tax_adjusted_draw_rate == Decimal("0.015219")


def test_headline_annualizes_only_the_latest_contiguous_coverage_suffix() -> None:
    evidence = AccountHistory(
        opening=AccountOpening(
            earliest=date(2026, 2, 1),
            latest=date(2026, 2, 1),
            source="Synthetic account-opening confirmation.",
            as_of=date(2026, 3, 31),
        ),
        coverage=DocumentedHistory(
            complete_from=date(2026, 3, 1), source="Synthetic complete-history confirmation.", as_of=date(2026, 3, 31)
        ),
    )
    result = evaluate_account_history(
        [transaction(date(2026, 3, 15), "10", key="synthetic-latest-suffix-purchase")],
        account_history={"card": evidence},
    )

    assert result.period_start == date(2026, 3, 1)
    assert result.covered_days == 31
    assert result.spending_minor_units == 1000
    assert result.annualized_spending_minor_units == 11_782
    assert result.estimated_annual_draw_minor_units == 14_728


@pytest.mark.parametrize(
    ("contradiction_day", "expected_spending"), [(date(2026, 3, 15), 3000), (date(2026, 4, 1), 1000)]
)
def test_preopening_transaction_invalidates_rates_but_keeps_spending_and_hides_ids(
    contradiction_day: date, expected_spending: int
) -> None:
    contradictory_account = "synthetic-opening-conflict-key"
    observed_account = "synthetic-observed-account-key"
    evidence = AccountHistory(
        opening=AccountOpening(
            earliest=date(2026, 4, 2),
            latest=date(2026, 4, 2),
            source="Synthetic account-opening confirmation.",
            as_of=date(2026, 3, 31),
        )
    )
    result = evaluate_account_history(
        [
            transaction(contradiction_day, "20", key="synthetic-opening-conflict", account_id=contradictory_account),
            transaction(date(2026, 3, 15), "10", key="synthetic-retained-purchase", account_id=observed_account),
        ],
        account_history={contradictory_account: evidence},
        spending_account_ids={contradictory_account, observed_account},
        account_labels={contradictory_account: "New card", observed_account: "Daily account"},
    )

    assert result.status == "unavailable"
    assert result.spending_minor_units == expected_spending
    assert result.annualized_spending_minor_units is None
    assert result.spending_draw_rate is None
    assert result.tax_adjusted_draw_rate is None
    diagnostics = " ".join(result.issues + [coverage.account_label for coverage in result.account_coverage])
    assert "opening" in diagnostics.lower()
    assert contradictory_account not in diagnostics
    assert observed_account not in diagnostics
    serialized_result = result.model_dump_json()
    assert contradictory_account not in serialized_result
    assert observed_account not in serialized_result


def test_account_history_keys_must_be_selected_spending_accounts() -> None:
    unselected_account = "synthetic-unselected-account-key"
    evidence = AccountHistory(
        coverage=DocumentedHistory(
            complete_from=date(2026, 1, 1), source="Synthetic complete-history confirmation.", as_of=date(2026, 3, 31)
        )
    )

    with pytest.raises(ValidationError) as error:
        SpendConfiguration(
            time_zone=ZoneInfo("UTC"),
            cards=[],
            allowance=allowance(),
            sustainability=policy(account_history={unselected_account: evidence}),
        )

    assert unselected_account not in str(error.value)


def test_account_history_evidence_requires_ordered_bounds_and_provenance() -> None:
    with pytest.raises(ValidationError, match="opening bounds must be ordered"):
        AccountOpening(
            earliest=date(2026, 2, 2),
            latest=date(2026, 2, 1),
            source="Synthetic account-opening confirmation.",
            as_of=date(2026, 3, 31),
        )
    with pytest.raises(ValidationError, match="history coverage cannot start"):
        DocumentedHistory(
            complete_from=date(2026, 4, 1), source="Synthetic complete-history confirmation.", as_of=date(2026, 3, 31)
        )
    with pytest.raises(ValidationError):
        AccountHistory()
    with pytest.raises(ValidationError):
        AccountOpening(earliest=date(2026, 2, 1), latest=date(2026, 2, 1), source="   ", as_of=date(2026, 3, 31))
    with pytest.raises(ValidationError):
        AccountOpening.model_validate(
            {
                "earliest": date(2026, 2, 1),
                "latest": date(2026, 2, 1),
                "source": "Synthetic account-opening confirmation.",
            }
        )
    with pytest.raises(ValidationError):
        DocumentedHistory(complete_from=date(2026, 1, 1), source="   ", as_of=date(2026, 3, 31))


@pytest.mark.parametrize("rate", [Decimal("-0.1"), Decimal("1.0"), Decimal("NaN"), Decimal("Infinity")])
def test_tax_fraction_must_be_finite_and_below_one(rate: Decimal) -> None:
    with pytest.raises(ValidationError):
        WithdrawalTax(rate=rate, assumption_note="Synthetic", as_of=date(2026, 1, 1))


@pytest.mark.parametrize("rate", [Decimal(0), Decimal(1), Decimal("NaN"), Decimal("Infinity")])
def test_reference_draw_rates_must_be_finite_fractions(rate: Decimal) -> None:
    with pytest.raises(ValidationError):
        SustainabilityDisplay(reference_draw_rates=[rate])


def test_tax_fraction_requires_provenance_and_shared_classification() -> None:
    with pytest.raises(ValidationError, match="dated assumption"):
        WithdrawalTax(rate=Decimal("0.2"))
    with pytest.raises(ValidationError, match="requires shared"):
        SpendConfiguration(time_zone=ZoneInfo("UTC"), cards=[], sustainability=policy())
    with pytest.raises(ValidationError):
        WithdrawalTax.model_validate({"rate": 0.2, "assumption_note": "Synthetic", "as_of": date(2026, 1, 1)})
    with pytest.raises(ValidationError):
        SustainabilityDisplay.model_validate({"reference_draw_rates": [0.04]})


if __name__ == "__main__":
    pytest_bazel.main()
