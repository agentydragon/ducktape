"""Budget CSV export serialization tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.budget.csv_export import build_summary_csv, build_transactions_csv
from finance.budget.schema import BucketKind
from finance.budget.wire import (
    BucketMonthly,
    BucketView,
    BudgetSnapshotResponse,
    BudgetTransactionsResponse,
    HiddenBudgetAdjustment,
    OverrideBudgetAdjustment,
    TransactionView,
)


def _snapshot(*, bucket: BucketView, monthly: BucketMonthly) -> BudgetSnapshotResponse:
    return BudgetSnapshotResponse(
        months=(date(2025, 5, 1),),
        buckets=(bucket,),
        monthly_by_bucket=(monthly,),
        lumpy=(),
        lumpy_threshold_usd=Decimal(500),
        data_window_start=date(2025, 5, 1),
        data_window_end=date(2025, 5, 31),
    )


def test_build_summary_csv_emits_bucket_by_month_matrix() -> None:
    response = BudgetSnapshotResponse(
        months=(date(2025, 5, 1), date(2025, 6, 1)),
        buckets=(BucketView(id="rent", label="Rent", kind=BucketKind.EXPENSE, family="housing"),),
        monthly_by_bucket=(
            BucketMonthly(
                bucket_id="rent",
                monthly_amounts=(Decimal(3200), Decimal(3200)),
                window_monthly_avg=Decimal(3200),
                transaction_count=2,
            ),
        ),
        lumpy=(),
        lumpy_threshold_usd=Decimal(500),
        data_window_start=date(2025, 5, 1),
        data_window_end=date(2025, 6, 30),
    )

    lines = build_summary_csv(response, {}).strip().split("\n")

    assert lines[0] == "Bucket,Kind,Family,2025-05,2025-06,Avg $/mo,Tx count"
    assert lines[1] == "Rent,expense,housing,3200.00,3200.00,3200.00,2"


def test_build_summary_csv_blanks_missing_family_and_preserves_inflow_sign() -> None:
    response = _snapshot(
        bucket=BucketView(id="reimbursements", label="Insurance reimbursements", kind=BucketKind.INFLOW, family=None),
        monthly=BucketMonthly(
            bucket_id="reimbursements",
            monthly_amounts=(Decimal("-450.50"),),
            window_monthly_avg=Decimal("-450.50"),
            transaction_count=1,
        ),
    )

    row = build_summary_csv(response, {}).strip().split("\n")[1]

    assert row == "Insurance reimbursements,inflow,,-450.50,-450.50,1"


def test_build_transactions_csv_renders_nulls_and_quotes_embedded_commas() -> None:
    response = BudgetTransactionsResponse(
        bucket_id="rent",
        transactions=(
            TransactionView(
                transaction_id="tx",
                date=date(2025, 5, 12),
                merchant_name=None,
                name="ACH DEBIT, LANDLORD LLC",
                pfc_primary="RENT_AND_UTILITIES",
                pfc_detailed=None,
                account_name="Checking",
                institution_name=None,
                amount=Decimal(3200),
                bucket_id="rent",
            ),
        ),
    )

    header, row = build_transactions_csv(response).strip().split("\n")

    assert header == "Date,Merchant,Descriptor,PFC primary,PFC detailed,Account,Institution,Amount"
    assert row == '2025-05-12,,"ACH DEBIT, LANDLORD LLC",RENT_AND_UTILITIES,,Checking,,3200.00'


def test_text_fields_starting_with_formula_trigger_are_neutralized() -> None:
    response = BudgetTransactionsResponse(
        bucket_id="misc",
        transactions=(
            TransactionView(
                transaction_id="tx",
                date=date(2025, 5, 12),
                merchant_name="=HYPERLINK(evil)",
                name="@cmd",
                pfc_primary=None,
                pfc_detailed=None,
                account_name="Checking",
                institution_name=None,
                amount=Decimal(-42),
                bucket_id="misc",
            ),
        ),
    )

    row = build_transactions_csv(response).strip().split("\n")[1]

    assert row == "2025-05-12,'=HYPERLINK(evil),'@cmd,,,Checking,,-42.00"


def test_build_summary_csv_adds_planning_columns_when_adjustments_are_present() -> None:
    response = BudgetSnapshotResponse(
        months=(date(2025, 5, 1),),
        buckets=(
            BucketView(id="rent", label="Rent", kind=BucketKind.EXPENSE, family=None),
            BucketView(id="insurance", label="Insurance", kind=BucketKind.EXPENSE, family=None),
            BucketView(id="groceries", label="Groceries", kind=BucketKind.EXPENSE, family=None),
        ),
        monthly_by_bucket=(
            BucketMonthly(
                bucket_id="rent",
                monthly_amounts=(Decimal(3200),),
                window_monthly_avg=Decimal(3200),
                transaction_count=1,
            ),
            BucketMonthly(
                bucket_id="insurance",
                monthly_amounts=(Decimal(312),),
                window_monthly_avg=Decimal(312),
                transaction_count=1,
            ),
            BucketMonthly(
                bucket_id="groceries",
                monthly_amounts=(Decimal(600),),
                window_monthly_avg=Decimal(600),
                transaction_count=1,
            ),
        ),
        lumpy=(),
        lumpy_threshold_usd=Decimal(500),
        data_window_start=date(2025, 5, 1),
        data_window_end=date(2025, 5, 31),
    )

    lines = (
        build_summary_csv(
            response, {"rent": HiddenBudgetAdjustment(), "insurance": OverrideBudgetAdjustment(monthly=Decimal(450))}
        )
        .strip()
        .split("\n")
    )

    assert lines[0] == "Bucket,Kind,Family,2025-05,Avg $/mo,Tx count,Planned $/mo,Hidden"
    assert lines[1] == "Rent,expense,,3200.00,3200.00,1,0.00,yes"
    assert lines[2] == "Insurance,expense,,312.00,312.00,1,450.00,"
    assert lines[3] == "Groceries,expense,,600.00,600.00,1,600.00,"


def test_budget_money_stays_decimal_through_wire_and_csv() -> None:
    amount = Decimal("9007199254740993.01")
    monthly = BucketMonthly(
        bucket_id="groceries",
        monthly_amounts=(amount,),
        window_monthly_avg=Decimal("5832999953545646.177845611702"),
        transaction_count=1,
    )
    response = _snapshot(
        bucket=BucketView(id="groceries", label="Groceries", kind=BucketKind.EXPENSE, family=None), monthly=monthly
    )

    wire = response.model_dump(mode="json")
    assert wire["monthly_by_bucket"][0]["monthly_amounts"] == ["9007199254740993.01"]
    assert wire["monthly_by_bucket"][0]["window_monthly_avg"] == "5832999953545646.177845611702"
    assert wire["lumpy_threshold_usd"] == "500"

    row = build_summary_csv(response, {"groceries": OverrideBudgetAdjustment(monthly=Decimal("0.01"))})
    assert row.splitlines()[1] == "Groceries,expense,,9007199254740993.01,5832999953545646.18,1,0.01,"


def test_budget_wire_rejects_nonfinite_money() -> None:
    with pytest.raises(ValidationError):
        BucketMonthly(
            bucket_id="groceries", monthly_amounts=(Decimal("NaN"),), window_monthly_avg=Decimal(0), transaction_count=0
        )
    with pytest.raises(ValidationError):
        OverrideBudgetAdjustment(monthly=Decimal("Infinity"))


if __name__ == "__main__":
    pytest_bazel.main()
