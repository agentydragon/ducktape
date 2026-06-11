"""CSV serialization for budget exports."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from io import StringIO

from finance.augur.budget.wire import (
    BudgetAdjustment,
    BudgetSnapshotResponse,
    BudgetTransactionsResponse,
    HiddenBudgetAdjustment,
    OverrideBudgetAdjustment,
)

_MONTH_COLUMN_LENGTH = 7  # "YYYY-MM"
_FORMULA_TRIGGERS = frozenset(("=", "+", "-", "@", "\t", "\r"))


def _text_field(value: str) -> str:
    if value and value[0] in _FORMULA_TRIGGERS:
        value = f"'{value}"
    return value


def _amount(value: float) -> str:
    return f"{value:.2f}"


def _effective_signed_avg(kind: str, window_avg: float, adjustment: BudgetAdjustment | None) -> float:
    if not isinstance(adjustment, OverrideBudgetAdjustment):
        return window_avg
    if kind in {"inflow", "income"}:
        return -float(adjustment.monthly)
    if kind == "transfer" and window_avg < 0:
        return -float(adjustment.monthly)
    return float(adjustment.monthly)


def _write_csv(rows: Iterable[Iterable[str]]) -> str:
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerows(rows)
    return output.getvalue()


def build_summary_csv(snapshot: BudgetSnapshotResponse, adjustments: dict[str, BudgetAdjustment]) -> str:
    include_planning = bool(adjustments)
    planning_headers = ["Planned $/mo", "Hidden"] if include_planning else []
    header = [
        "Bucket",
        "Kind",
        "Family",
        *(month.isoformat()[:_MONTH_COLUMN_LENGTH] for month in snapshot.months),
        "Avg $/mo",
        "Tx count",
        *planning_headers,
    ]
    rows: list[list[str]] = [header]

    bucket_by_id = {bucket.id: bucket for bucket in snapshot.buckets}
    for row in snapshot.monthly_by_bucket:
        bucket = bucket_by_id[row.bucket_id]
        adjustment = adjustments.get(row.bucket_id)
        planning_fields: list[str] = []
        if include_planning:
            hidden = isinstance(adjustment, HiddenBudgetAdjustment)
            planned = 0.0 if hidden else _effective_signed_avg(bucket.kind.value, row.window_monthly_avg, adjustment)
            planning_fields = [_amount(planned), "yes" if hidden else ""]
        rows.append(
            [
                _text_field(bucket.label),
                _text_field(bucket.kind.value),
                _text_field(bucket.family or ""),
                *[_amount(value) for value in row.monthly_amounts],
                _amount(row.window_monthly_avg),
                str(row.transaction_count),
                *planning_fields,
            ]
        )
    return _write_csv(rows)


def build_transactions_csv(response: BudgetTransactionsResponse) -> str:
    header = ["Date", "Merchant", "Descriptor", "PFC primary", "PFC detailed", "Account", "Institution", "Amount"]
    rows = [header]
    rows.extend(
        [
            _text_field(row.date.isoformat()),
            _text_field(row.merchant_name or ""),
            _text_field(row.name),
            _text_field(row.pfc_primary or ""),
            _text_field(row.pfc_detailed or ""),
            _text_field(row.account_name),
            _text_field(row.institution_name or ""),
            _amount(row.amount),
        ]
        for row in response.transactions
    )
    return _write_csv(rows)
