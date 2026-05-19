"""Tests for `augur.core.action_log`."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest_bazel

from augur.core.action_log import (
    CASHFLOW_LOG_SCHEMA,
    CashflowCause,
    build_cashflow_log_from_scheduled,
    derive_cash_matrix,
)
from augur.core.scheduled_cashflows import build_scheduled_cashflows


def _zero_matrix(rollouts: int, months: int) -> np.ndarray:
    return np.zeros((rollouts, months), dtype=np.float64)


def test_build_cashflow_log_attributes_scheduled_cash_flows() -> None:
    rollout_count = 3
    month_index = np.array([0, 1, 2, 3, 4], dtype=np.int64)
    net_cf = _zero_matrix(rollout_count, month_index.size)
    sale_cf = _zero_matrix(rollout_count, month_index.size)
    partner = _zero_matrix(rollout_count, month_index.size)
    net_cf[:, 2] = np.array([100.0, 200.0, 300.0])
    sale_cf[:, 3] = np.array([1000.0, 1000.0, 1000.0])
    partner[:, 1] = np.array([50.0, 50.0, 50.0])

    scheduled = build_scheduled_cashflows(
        rollout_count=rollout_count,
        month_index=month_index,
        property_net_cash_flow_usd=net_cf,
        property_sale_cash_flow_usd=sale_cf,
        partner_contribution_used_usd=partner,
        property_tax_accrual_usd=_zero_matrix(rollout_count, month_index.size),
        hoa_accrual_usd=_zero_matrix(rollout_count, month_index.size),
        insurance_accrual_usd=_zero_matrix(rollout_count, month_index.size),
        maintenance_accrual_usd=_zero_matrix(rollout_count, month_index.size),
    )

    log = build_cashflow_log_from_scheduled(scheduled, actor_id="owner", account_id="checking")
    assert log.schema == CASHFLOW_LOG_SCHEMA

    assert log.height == 9
    assert sorted(log["cause"].unique().to_list()) == sorted(
        {
            CashflowCause.PROPERTY_NET_CASH_FLOW.value,
            CashflowCause.PROPERTY_SALE_CASH_FLOW.value,
            CashflowCause.PARTNER_CONTRIBUTION_USED.value,
        }
    )
    # All rows carry the same (actor_id, account_id) pair attribution.
    assert log["actor_id"].unique().to_list() == ["owner"]
    assert log["account_id"].unique().to_list() == ["checking"]


def test_derive_cash_matrix_matches_running_balance() -> None:
    rollout_count = 2
    month_index = np.array([0, 1, 2, 3], dtype=np.int64)
    sale_cf = _zero_matrix(rollout_count, month_index.size)
    sale_cf[:, 2] = np.array([500.0, 700.0])
    scheduled = build_scheduled_cashflows(
        rollout_count=rollout_count,
        month_index=month_index,
        property_net_cash_flow_usd=_zero_matrix(rollout_count, month_index.size),
        property_sale_cash_flow_usd=sale_cf,
        partner_contribution_used_usd=_zero_matrix(rollout_count, month_index.size),
        property_tax_accrual_usd=_zero_matrix(rollout_count, month_index.size),
        hoa_accrual_usd=_zero_matrix(rollout_count, month_index.size),
        insurance_accrual_usd=_zero_matrix(rollout_count, month_index.size),
        maintenance_accrual_usd=_zero_matrix(rollout_count, month_index.size),
    )
    log = build_cashflow_log_from_scheduled(scheduled, actor_id="owner", account_id="checking")
    initial = np.array([10_000.0, 20_000.0])

    matrix = derive_cash_matrix(
        log,
        actor_id="owner",
        account_id="checking",
        initial_balance_per_rollout=initial,
        rollout_count=rollout_count,
        month_index=month_index,
    )
    expected = np.array([[10_000.0, 10_000.0, 10_500.0, 10_500.0], [20_000.0, 20_000.0, 20_700.0, 20_700.0]])
    np.testing.assert_array_equal(matrix, expected)


def test_derive_cash_matrix_filters_by_actor_id() -> None:
    rollout_count = 1
    month_index = np.array([0, 1], dtype=np.int64)
    log = pl.DataFrame(
        {
            "rollout_index": [0, 0],
            "month_index": [1, 1],
            "actor_id": ["owner", "partner"],
            "account_id": ["checking", "checking"],
            "amount_delta_usd": [100.0, 999.0],
            "cause": ["x", "x"],
        },
        schema=CASHFLOW_LOG_SCHEMA,
    )
    matrix = derive_cash_matrix(
        log,
        actor_id="owner",
        account_id="checking",
        initial_balance_per_rollout=np.array([0.0]),
        rollout_count=rollout_count,
        month_index=month_index,
    )
    np.testing.assert_array_equal(matrix, np.array([[0.0, 100.0]]))


def test_derive_cash_matrix_empty_log_returns_initial_repeated() -> None:
    rollout_count = 3
    month_index = np.array([0, 1, 2], dtype=np.int64)
    log = build_cashflow_log_from_scheduled(
        build_scheduled_cashflows(
            rollout_count=rollout_count,
            month_index=month_index,
            property_net_cash_flow_usd=_zero_matrix(rollout_count, month_index.size),
            property_sale_cash_flow_usd=_zero_matrix(rollout_count, month_index.size),
            partner_contribution_used_usd=_zero_matrix(rollout_count, month_index.size),
            property_tax_accrual_usd=_zero_matrix(rollout_count, month_index.size),
            hoa_accrual_usd=_zero_matrix(rollout_count, month_index.size),
            insurance_accrual_usd=_zero_matrix(rollout_count, month_index.size),
            maintenance_accrual_usd=_zero_matrix(rollout_count, month_index.size),
        ),
        actor_id="owner",
        account_id="checking",
    )
    initial = np.array([1.0, 2.0, 3.0])
    matrix = derive_cash_matrix(
        log,
        actor_id="owner",
        account_id="checking",
        initial_balance_per_rollout=initial,
        rollout_count=rollout_count,
        month_index=month_index,
    )
    np.testing.assert_array_equal(matrix, np.array([[1.0, 1.0, 1.0], [2.0, 2.0, 2.0], [3.0, 3.0, 3.0]]))


if __name__ == "__main__":
    pytest_bazel.main()
