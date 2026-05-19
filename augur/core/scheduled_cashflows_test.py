"""Tests for `augur.core.scheduled_cashflows`."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from augur.core.scheduled_cashflows import (
    SCHEDULED_CASHFLOW_SCHEMA,
    ScheduledCashflowKind,
    build_scheduled_cashflows,
    derive_per_kind_matrices,
)


def _zeros(rollouts: int, months: int) -> np.ndarray:
    return np.zeros((rollouts, months), dtype=np.float64)


def test_build_scheduled_cashflows_schema_and_round_trip() -> None:
    rollout_count = 4
    month_index = np.arange(6, dtype=np.int64)
    matrices = {
        "property_net_cash_flow_usd": _zeros(rollout_count, month_index.size),
        "property_sale_cash_flow_usd": _zeros(rollout_count, month_index.size),
        "partner_contribution_used_usd": _zeros(rollout_count, month_index.size),
        "property_tax_accrual_usd": _zeros(rollout_count, month_index.size),
        "hoa_accrual_usd": _zeros(rollout_count, month_index.size),
        "insurance_accrual_usd": _zeros(rollout_count, month_index.size),
        "maintenance_accrual_usd": _zeros(rollout_count, month_index.size),
    }
    matrices["property_net_cash_flow_usd"][:, 2] = np.array([100.0, 200.0, 300.0, 400.0])
    matrices["property_tax_accrual_usd"][:, 5] = np.array([50.0, 51.0, 52.0, 53.0])

    scheduled = build_scheduled_cashflows(rollout_count=rollout_count, month_index=month_index, **matrices)

    assert scheduled.frame.schema == SCHEDULED_CASHFLOW_SCHEMA
    assert scheduled.frame.height == rollout_count * month_index.size * 7

    np.testing.assert_array_equal(
        scheduled.amount_at(kind=ScheduledCashflowKind.PROPERTY_NET_CASH_FLOW, month_position=2),
        np.array([100.0, 200.0, 300.0, 400.0]),
    )
    np.testing.assert_array_equal(
        scheduled.amount_at(kind=ScheduledCashflowKind.PROPERTY_TAX_ACCRUAL, month_position=5),
        np.array([50.0, 51.0, 52.0, 53.0]),
    )

    derived = derive_per_kind_matrices(scheduled.frame, rollout_count=rollout_count, month_index=month_index)
    np.testing.assert_array_equal(
        derived[ScheduledCashflowKind.PROPERTY_NET_CASH_FLOW], matrices["property_net_cash_flow_usd"]
    )
    np.testing.assert_array_equal(
        derived[ScheduledCashflowKind.PROPERTY_TAX_ACCRUAL], matrices["property_tax_accrual_usd"]
    )


def test_build_scheduled_cashflows_rejects_mismatched_shape() -> None:
    rollout_count = 3
    month_index = np.arange(4, dtype=np.int64)
    matrices = {
        name: _zeros(rollout_count, month_index.size)
        for name in (
            "property_net_cash_flow_usd",
            "property_sale_cash_flow_usd",
            "partner_contribution_used_usd",
            "property_tax_accrual_usd",
            "hoa_accrual_usd",
            "insurance_accrual_usd",
        )
    }
    matrices["maintenance_accrual_usd"] = _zeros(rollout_count + 1, month_index.size)

    with pytest.raises(ValueError, match="maintenance_accrual"):
        build_scheduled_cashflows(rollout_count=rollout_count, month_index=month_index, **matrices)


def test_uses_absolute_month_index_values() -> None:
    rollout_count = 2
    month_index = np.array([10, 11, 12, 13], dtype=np.int64)
    matrices = {
        name: _zeros(rollout_count, month_index.size)
        for name in (
            "property_net_cash_flow_usd",
            "property_sale_cash_flow_usd",
            "partner_contribution_used_usd",
            "property_tax_accrual_usd",
            "hoa_accrual_usd",
            "insurance_accrual_usd",
            "maintenance_accrual_usd",
        )
    }
    matrices["property_net_cash_flow_usd"][:, 0] = 1.0

    scheduled = build_scheduled_cashflows(rollout_count=rollout_count, month_index=month_index, **matrices)
    months_in_frame = sorted(scheduled.frame.select(pl.col("month_index").unique()).to_series().to_list())
    assert months_in_frame == [10, 11, 12, 13]


if __name__ == "__main__":
    pytest_bazel.main()
