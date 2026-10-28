"""Shared report decisions, with synthetic purchases only."""

from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import pytest_bazel

from finance.plaid.spend.allowance import (
    AccountingPolicy,
    AllOf,
    AllowancePolicy,
    AmountExact,
    AnalysisCategory,
    CreditTreatment,
    DateRange,
    Disposition,
    Kind,
    NamePrefix,
    ReversalPair,
    Rule,
    Transaction,
    accounting_disposition,
    prepare_accounting,
    round_minor_units,
)
from finance.plaid.spend.consumption import ConsumptionMode, prepare_consumption

_TODAY = date(2026, 4, 1)
_ZONE = ZoneInfo("America/Los_Angeles")


def transaction(name: str, amount: str = "10.00", **changes: object) -> Transaction:
    return Transaction.model_validate(
        {
            "account_id": "card",
            "transaction_id": name,
            "pending_transaction_id": None,
            "date": date(2026, 3, 10),
            "amount": Decimal(amount),
            "pending": False,
            "name": name,
            "merchant_name": None,
            "pfc_primary": None,
            "pfc_detailed": None,
            "currency": "USD",
            **changes,
        }
    )


def policy() -> AllowancePolicy:
    return AllowancePolicy(
        monthly_minor_units=10_000,
        activation_at=date(2026, 3, 1),
        spending_account_ids={"card"},
        rules=[
            Rule(condition=NamePrefix(field="name", prefix="RENT"), kind=Kind.FIXED),
            Rule(condition=NamePrefix(field="name", prefix="SHOP"), kind=Kind.FLEXIBLE),
            Rule(condition=NamePrefix(field="name", prefix="TRANSFER"), kind=Kind.EXCLUDED),
            Rule(condition=NamePrefix(field="name", prefix="TAX"), kind=Kind.EXCLUDED, analysis_category="tax"),
        ],
        analysis_categories={
            "unclassified": AnalysisCategory(label="Unclassified", color="#777777"),
            "tax": AnalysisCategory(label="Tax", color="#777777"),
        },
    )


@pytest.mark.parametrize("mode", list(ConsumptionMode))
def test_scope_purchase_day_supersession_and_coverage(mode: ConsumptionMode) -> None:
    result = prepare_consumption(
        policy(),
        [
            transaction("RENT", authorized_datetime=datetime(2026, 3, 11, 2, tzinfo=UTC)),
            transaction("SHOP pending", pending=True),
            transaction("SHOP posted", pending_transaction_id="SHOP pending"),
            transaction("SHOP future", authorized_date=date(2026, 4, 2)),
            transaction("SHOP unrelated", account_id="other"),
            transaction("TRANSFER"),
            transaction("TAX"),
            transaction("Unknown"),
            transaction("Foreign", currency="EUR"),
        ],
        time_zone=_ZONE,
        today=_TODAY,
        currency="USD",
        include_excluded_categories={"tax"},
        confirmed_refund_rule_ids=set(),
        mode=mode,
    )
    entries = {entry.transaction.name: entry for entry in result.entries}
    assert set(entries) == {"RENT", "SHOP posted", "TRANSFER", "TAX", "Unknown", "Foreign"}
    assert entries["RENT"].report_day == date(2026, 3, 10)
    assert entries["RENT"].kind == Kind.FIXED
    assert entries["SHOP posted"].amount_minor_units == 1_000
    assert entries["TRANSFER"].disposition == Disposition.EXCLUDED
    assert entries["TAX"].amount_minor_units == 1_000
    assert entries["Unknown"].kind == Kind.REVIEW
    assert entries["Unknown"].amount_minor_units == 1_000
    assert entries["Foreign"].disposition == Disposition.OTHER_CURRENCY
    assert entries["Foreign"].amount_minor_units == 0
    assert not result.issues


@pytest.mark.parametrize("mode", list(ConsumptionMode))
def test_full_history_reversals_and_isolated_credits(mode: ConsumptionMode) -> None:
    allowance = policy().model_copy(
        update={
            "reversal_pairs": [
                ReversalPair(
                    id="synthetic-return",
                    description="Synthetic confirmed full reversal.",
                    original=AllOf(
                        conditions=[
                            DateRange(start=date(2026, 1, 1), end=date(2026, 1, 1)),
                            NamePrefix(field="name", prefix="SHOP original"),
                            AmountExact(value="10.00"),
                        ]
                    ),
                    reversal=AllOf(
                        conditions=[
                            DateRange(start=date(2026, 3, 10), end=date(2026, 3, 10)),
                            NamePrefix(field="name", prefix="SHOP return"),
                            AmountExact(value="-10.00"),
                        ]
                    ),
                )
            ]
        }
    )
    result = prepare_consumption(
        allowance,
        [
            transaction("SHOP original", date=date(2026, 1, 1)),
            transaction("SHOP return", "-10.00"),
            transaction("SHOP credit", "-5.00"),
        ],
        time_zone=_ZONE,
        today=_TODAY,
        currency="USD",
        include_excluded_categories=set(),
        confirmed_refund_rule_ids=set(),
        mode=mode,
    )
    assert [entry.disposition for entry in result.entries[:2]] == [Disposition.REVERSED] * 2
    assert result.entries[2].disposition == (
        Disposition.HELD_REFUND if mode == ConsumptionMode.NET else Disposition.GROSS_CREDIT
    )
    assert sum(entry.amount_minor_units for entry in result.entries) == 0
    assert not result.issues


def test_ambiguous_reversal_reports_issue_and_keeps_purchase() -> None:
    allowance = policy().model_copy(
        update={
            "reversal_pairs": [
                ReversalPair(
                    id="synthetic-return",
                    description="Synthetic incomplete full reversal.",
                    original=AllOf(
                        conditions=[
                            DateRange(start=date(2026, 1, 1), end=date(2026, 1, 1)),
                            NamePrefix(field="name", prefix="SHOP original"),
                            AmountExact(value="10.00"),
                        ]
                    ),
                    reversal=AllOf(
                        conditions=[
                            DateRange(start=date(2026, 3, 10), end=date(2026, 3, 10)),
                            NamePrefix(field="name", prefix="SHOP return"),
                            AmountExact(value="-10.00"),
                        ]
                    ),
                )
            ]
        }
    )
    result = prepare_consumption(
        allowance,
        [transaction("SHOP original", date=date(2026, 1, 1))],
        time_zone=_ZONE,
        today=_TODAY,
        currency="USD",
        include_excluded_categories=set(),
        confirmed_refund_rule_ids=set(),
        mode=ConsumptionMode.GROSS_PURCHASES,
    )
    assert result.entries[0].amount_minor_units == 1_000
    assert result.issues


@pytest.mark.parametrize(
    ("counted_kinds", "credits", "honor_reversals", "exclude_card_payments", "expected"),
    [
        (
            frozenset({Kind.FLEXIBLE, Kind.REVIEW}),
            CreditTreatment.HOLD,
            True,
            False,
            [
                Disposition.COUNTED,
                Disposition.FIXED,
                Disposition.EXCLUDED,
                Disposition.HELD_REFUND,
                Disposition.OTHER_CURRENCY,
                Disposition.EXCLUDED,
                Disposition.SUPERSEDED_PENDING,
            ],
        ),
        (
            frozenset({Kind.FIXED, Kind.FLEXIBLE, Kind.REVIEW}),
            CreditTreatment.CONFIRMED_REFUNDS,
            True,
            False,
            [
                Disposition.COUNTED,
                Disposition.COUNTED,
                Disposition.EXCLUDED,
                Disposition.HELD_REFUND,
                Disposition.OTHER_CURRENCY,
                Disposition.EXCLUDED,
                Disposition.SUPERSEDED_PENDING,
            ],
        ),
        (
            frozenset(Kind),
            CreditTreatment.ALL,
            False,
            True,
            [
                Disposition.COUNTED,
                Disposition.COUNTED,
                Disposition.COUNTED,
                Disposition.COUNTED,
                Disposition.OTHER_CURRENCY,
                Disposition.CARD_PAYMENT,
                Disposition.SUPERSEDED_PENDING,
            ],
        ),
    ],
)
def test_shared_decisions_preserve_allowance_consumption_and_statement_meaning(
    counted_kinds: frozenset[Kind],
    credits: CreditTreatment,
    honor_reversals: bool,
    exclude_card_payments: bool,
    expected: list[Disposition],
) -> None:
    prepared = prepare_accounting(
        [
            transaction("SHOP"),
            transaction("RENT"),
            transaction("TRANSFER"),
            transaction("SHOP credit", "-5"),
            transaction("SHOP foreign", currency="EUR"),
            transaction("TRANSFER payment", pfc_detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
            transaction("SHOP pending", pending=True),
            transaction("SHOP posted", pending_transaction_id="SHOP pending"),
            transaction("SHOP unrelated", account_id="other"),
        ],
        allowance=policy(),
        time_zone=_ZONE,
        today=_TODAY,
    )
    view_policy = AccountingPolicy(
        currency="USD",
        counted_kinds=counted_kinds,
        credits=credits,
        honor_reversals=honor_reversals,
        exclude_card_payments=exclude_card_payments,
    )
    assert [accounting_disposition(entry, view_policy) for entry in prepared.entries[:7]] == expected
    assert len(prepared.entries) == 8
    assert prepared.entries[0].rule_number == 2


def test_statement_keeps_signed_reversal_legs_and_posting_date() -> None:
    allowance = policy().model_copy(
        update={
            "reversal_pairs": [
                ReversalPair(
                    id="synthetic-return",
                    description="Synthetic returned purchase",
                    original=AllOf(
                        conditions=[
                            DateRange(start=date(2026, 1, 1), end=date(2026, 1, 1)),
                            NamePrefix(field="name", prefix="SHOP original"),
                        ]
                    ),
                    reversal=AllOf(
                        conditions=[
                            DateRange(start=date(2026, 3, 10), end=date(2026, 3, 10)),
                            NamePrefix(field="name", prefix="SHOP return"),
                        ]
                    ),
                )
            ]
        }
    )
    original = transaction("SHOP original", date=date(2026, 1, 1))
    reversal = transaction("SHOP return", "-10", authorized_date=date(2026, 3, 9))
    prepared = prepare_accounting([original, reversal], allowance=allowance, time_zone=_ZONE, today=_TODAY)
    statement = AccountingPolicy(
        currency="USD",
        counted_kinds=frozenset(Kind),
        credits=CreditTreatment.ALL,
        honor_reversals=False,
        exclude_card_payments=True,
    )
    consumption = AccountingPolicy(
        currency="USD", counted_kinds=frozenset(Kind), credits=CreditTreatment.CONFIRMED_REFUNDS
    )
    assert not prepared.issues
    assert [accounting_disposition(entry, statement) for entry in prepared.entries] == [Disposition.COUNTED] * 2
    assert [entry.amount_minor_units for entry in prepared.entries] == [1000, -1000]
    assert [accounting_disposition(entry, consumption) for entry in prepared.entries] == [Disposition.REVERSED] * 2
    assert prepared.entries[1].report_day == date(2026, 3, 9)
    assert prepared.entries[1].transaction.date == date(2026, 3, 10)


def test_credit_modes_and_currency_rounding() -> None:
    credit = transaction("SHOP refund", "-3.125")
    prepared = prepare_accounting([credit], allowance=policy(), time_zone=_ZONE, today=_TODAY)
    assert prepared.entries[0].amount_minor_units == -313
    expected = {
        CreditTreatment.HOLD: Disposition.HELD_REFUND,
        CreditTreatment.CONFIRMED_REFUNDS: Disposition.HELD_REFUND,
        CreditTreatment.GROSS_PURCHASES: Disposition.GROSS_CREDIT,
        CreditTreatment.ALL: Disposition.COUNTED,
    }
    for credits, disposition in expected.items():
        view_policy = AccountingPolicy(currency="USD", counted_kinds=frozenset(Kind), credits=credits)
        assert accounting_disposition(prepared.entries[0], view_policy) == disposition
    assert round_minor_units(Decimal("3.5"), 0) == 4
    assert round_minor_units(Decimal("3.1255"), 3) == 3126


if __name__ == "__main__":
    pytest_bazel.main()
