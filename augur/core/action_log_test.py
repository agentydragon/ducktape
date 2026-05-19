"""Tests for `augur.core.action_log`."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest_bazel

from augur.core.action_log import (
    ASSET_CHANGE_LOG_SCHEMA,
    CASHFLOW_LOG_SCHEMA,
    PROPERTY_STATE_SCHEMA,
    AssetKindForLog,
    CashflowCause,
    TaxTreatment,
    build_cashflow_log_from_scheduled,
    build_property_state_frame,
    derive_cash_matrix,
    derive_per_month_taxable_gain_matrix,
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


def test_build_property_state_frame_shapes_match_inputs() -> None:
    rollout_count = 2
    month_index = np.array([0, 1, 2], dtype=np.int64)
    live = np.array([[1.0, 1.0, 0.0], [1.0, 1.0, 1.0]])
    value = np.array([[500_000.0, 510_000.0, 0.0], [500_000.0, 510_000.0, 520_000.0]])
    depr = np.array([[0.0, 1000.0, 1000.0], [0.0, 1000.0, 2000.0]])

    frame = build_property_state_frame(
        property_id="home", month_index=month_index, live=live, value_usd=value, cumulative_depreciation_usd=depr
    )
    assert frame.schema == PROPERTY_STATE_SCHEMA
    assert frame.height == rollout_count * month_index.size
    assert frame["property_id"].unique().to_list() == ["home"]
    sold_row = frame.filter((pl.col("rollout_index") == 0) & (pl.col("month_index") == 2)).row(0, named=True)
    assert sold_row["live"] == 0.0
    assert sold_row["value_usd"] == 0.0
    assert sold_row["cumulative_depreciation_usd"] == 1000.0


def _empty_asset_change_log() -> pl.DataFrame:
    return pl.DataFrame(schema=ASSET_CHANGE_LOG_SCHEMA)


def _asset_change_log_with_rows(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return _empty_asset_change_log()
    return pl.DataFrame(rows, schema=ASSET_CHANGE_LOG_SCHEMA)


def test_derive_per_month_taxable_gain_groups_by_asset_kind_and_treatment() -> None:
    rollout_count = 2
    month_index = np.array([0, 1, 2, 3], dtype=np.int64)
    log = _asset_change_log_with_rows(
        [
            # Rollout 0: SP500 long-term gain in month 1
            {
                "rollout_index": 0,
                "month_index": 1,
                "actor_id": "owner",
                "asset_id": "sp500",
                "asset_kind": AssetKindForLog.GENERIC_SP500.value,
                "delta_units": -10.0,
                "delta_basis_usd": -1000.0,
                "cash_proceeds_usd": 1500.0,
                "taxable_gain_usd": 500.0,
                "tax_treatment": TaxTreatment.LONG_TERM_CAPITAL.value,
                "cause_kind": "POLICY_SALE",
                "cause_id": "cause:0",
            },
            # Rollout 0: PE long-term gain in month 1 (different asset_kind)
            {
                "rollout_index": 0,
                "month_index": 1,
                "actor_id": "owner",
                "asset_id": "pe",
                "asset_kind": AssetKindForLog.PRIVATE_EQUITY.value,
                "delta_units": -5.0,
                "delta_basis_usd": -2000.0,
                "cash_proceeds_usd": 3000.0,
                "taxable_gain_usd": 1000.0,
                "tax_treatment": TaxTreatment.LONG_TERM_CAPITAL.value,
                "cause_kind": "POLICY_SALE",
                "cause_id": "cause:1",
            },
            # Rollout 1: SP500 short-term gain in month 1 (different treatment)
            {
                "rollout_index": 1,
                "month_index": 1,
                "actor_id": "owner",
                "asset_id": "sp500",
                "asset_kind": AssetKindForLog.GENERIC_SP500.value,
                "delta_units": -3.0,
                "delta_basis_usd": -100.0,
                "cash_proceeds_usd": 200.0,
                "taxable_gain_usd": 100.0,
                "tax_treatment": TaxTreatment.SHORT_TERM_CAPITAL.value,
                "cause_kind": "POLICY_SALE",
                "cause_id": "cause:2",
            },
            # Rollout 1: property recapture in month 2
            {
                "rollout_index": 1,
                "month_index": 2,
                "actor_id": "owner",
                "asset_id": "home",
                "asset_kind": AssetKindForLog.PROPERTY.value,
                "delta_units": -1.0,
                "delta_basis_usd": -100_000.0,
                "cash_proceeds_usd": 200_000.0,
                "taxable_gain_usd": 30_000.0,
                "tax_treatment": TaxTreatment.DEPRECIATION_RECAPTURE_1250.value,
                "cause_kind": "PROPERTY_SALE",
                "cause_id": "cause:3",
            },
        ]
    )
    # SP500 long-term only — rollout 0 gets 500 at month 1, rollout 1 zero everywhere.
    sp500_lt = derive_per_month_taxable_gain_matrix(
        log,
        rollout_count=rollout_count,
        month_index=month_index,
        asset_kind=AssetKindForLog.GENERIC_SP500,
        tax_treatment=TaxTreatment.LONG_TERM_CAPITAL,
    )
    np.testing.assert_array_equal(sp500_lt, [[0.0, 500.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]])

    # Property recapture only — rollout 1 month 2.
    recapture = derive_per_month_taxable_gain_matrix(
        log,
        rollout_count=rollout_count,
        month_index=month_index,
        tax_treatment=TaxTreatment.DEPRECIATION_RECAPTURE_1250,
    )
    np.testing.assert_array_equal(recapture, [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 30_000.0, 0.0]])


def test_derive_per_month_taxable_gain_sums_multiple_events_per_month() -> None:
    log = _asset_change_log_with_rows(
        [
            {
                "rollout_index": 0,
                "month_index": 5,
                "actor_id": "owner",
                "asset_id": "sp500",
                "asset_kind": AssetKindForLog.GENERIC_SP500.value,
                "delta_units": -1.0,
                "delta_basis_usd": -100.0,
                "cash_proceeds_usd": 200.0,
                "taxable_gain_usd": 100.0,
                "tax_treatment": TaxTreatment.LONG_TERM_CAPITAL.value,
                "cause_kind": "OBLIGATION_SALE",
                "cause_id": "obligation:property_tax",
            },
            # Same rollout, same month, same asset_kind+treatment — different cause.
            # The bug we're fixing: today these are tracked in different code paths and
            # the second one (e.g. policy-chain) overwrites the first (e.g. obligation).
            {
                "rollout_index": 0,
                "month_index": 5,
                "actor_id": "owner",
                "asset_id": "sp500",
                "asset_kind": AssetKindForLog.GENERIC_SP500.value,
                "delta_units": -2.0,
                "delta_basis_usd": -150.0,
                "cash_proceeds_usd": 300.0,
                "taxable_gain_usd": 150.0,
                "tax_treatment": TaxTreatment.LONG_TERM_CAPITAL.value,
                "cause_kind": "POLICY_SALE",
                "cause_id": "policy:checking_floor",
            },
        ]
    )
    sp500 = derive_per_month_taxable_gain_matrix(
        log,
        rollout_count=1,
        month_index=np.array([0, 5, 10], dtype=np.int64),
        asset_kind=AssetKindForLog.GENERIC_SP500,
        tax_treatment=TaxTreatment.LONG_TERM_CAPITAL,
    )
    # 100 + 150 = 250, both events accumulate; no overwrite.
    np.testing.assert_array_equal(sp500, [[0.0, 250.0, 0.0]])


def test_derive_per_month_taxable_gain_filter_by_actor_id() -> None:
    log = _asset_change_log_with_rows(
        [
            {
                "rollout_index": 0,
                "month_index": 1,
                "actor_id": "owner",
                "asset_id": "sp500",
                "asset_kind": AssetKindForLog.GENERIC_SP500.value,
                "delta_units": -1.0,
                "delta_basis_usd": -100.0,
                "cash_proceeds_usd": 200.0,
                "taxable_gain_usd": 100.0,
                "tax_treatment": TaxTreatment.LONG_TERM_CAPITAL.value,
                "cause_kind": "POLICY_SALE",
                "cause_id": "c0",
            },
            {
                "rollout_index": 0,
                "month_index": 1,
                "actor_id": "partner",
                "asset_id": "sp500",
                "asset_kind": AssetKindForLog.GENERIC_SP500.value,
                "delta_units": -1.0,
                "delta_basis_usd": -100.0,
                "cash_proceeds_usd": 999.0,
                "taxable_gain_usd": 999.0,
                "tax_treatment": TaxTreatment.LONG_TERM_CAPITAL.value,
                "cause_kind": "POLICY_SALE",
                "cause_id": "c1",
            },
        ]
    )
    owner_only = derive_per_month_taxable_gain_matrix(
        log, rollout_count=1, month_index=np.array([0, 1, 2], dtype=np.int64), actor_id="owner"
    )
    np.testing.assert_array_equal(owner_only, [[0.0, 100.0, 0.0]])


def test_derive_per_month_taxable_gain_empty_log_returns_zeros() -> None:
    matrix = derive_per_month_taxable_gain_matrix(
        _empty_asset_change_log(),
        rollout_count=2,
        month_index=np.array([0, 1, 2], dtype=np.int64),
        asset_kind=AssetKindForLog.GENERIC_SP500,
    )
    np.testing.assert_array_equal(matrix, np.zeros((2, 3)))


if __name__ == "__main__":
    pytest_bazel.main()
