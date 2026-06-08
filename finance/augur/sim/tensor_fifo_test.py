from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.tensor_fifo import fifo_sell_dollars, fifo_sell_units, lot_order_for_pool


def test_lot_order_for_pool_is_account_scoped_fifo() -> None:
    order = lot_order_for_pool(
        lot_agent_codes=np.array([1, 1, 1, 1], dtype=np.int64),
        lot_account_codes=np.array([10, 10, 11, 10], dtype=np.int64),
        lot_asset_codes=np.array([20, 20, 20, 21], dtype=np.int64),
        lot_purchase_month=np.array([5, 2, 1, 0], dtype=np.int64),
        lot_id_codes=np.array([101, 100, 99, 98], dtype=np.int64),
        agent_code=1,
        account_code=10,
        asset_code=20,
    )

    np.testing.assert_array_equal(order, np.array([1, 0], dtype=np.int64))


def test_fifo_sell_units_vectorizes_partial_and_multilot_sales() -> None:
    lot_remaining = np.array([[10.0, 5.0, 20.0], [10.0, 5.0, 20.0], [10.0, 5.0, 20.0]], dtype=np.float64)
    ordered_lots = np.array([1, 0], dtype=np.int64)

    result = fifo_sell_units(
        lot_remaining=lot_remaining,
        ordered_lots=ordered_lots,
        target_units=np.array([3.0, 12.0, 15.0], dtype=np.float64),
        unit_price=np.array([100.0, 110.0, 120.0], dtype=np.float64),
        cost_basis_per_unit=np.array([60.0, 40.0, 1_000.0], dtype=np.float64),
    )

    np.testing.assert_allclose(
        result.sold_units, np.array([[0.0, 3.0, 0.0], [7.0, 5.0, 0.0], [10.0, 5.0, 0.0]], dtype=np.float64)
    )
    np.testing.assert_allclose(result.total_proceeds, np.array([300.0, 1320.0, 1800.0]))
    np.testing.assert_allclose(result.cost_basis_consumed.sum(axis=1), np.array([120.0, 620.0, 800.0]))
    np.testing.assert_array_equal(result.oversell, np.array([False, False, False]))


def test_fifo_sell_units_oversell_marks_row_without_partial_sale() -> None:
    result = fifo_sell_units(
        lot_remaining=np.array([[10.0, 5.0], [10.0, 5.0]], dtype=np.float64),
        ordered_lots=np.array([0, 1], dtype=np.int64),
        target_units=np.array([16.0, 8.0], dtype=np.float64),
        unit_price=np.array([100.0, 100.0], dtype=np.float64),
        cost_basis_per_unit=np.array([50.0, 60.0], dtype=np.float64),
    )

    np.testing.assert_array_equal(result.oversell, np.array([True, False]))
    np.testing.assert_allclose(result.sold_units[0], np.array([0.0, 0.0]))
    np.testing.assert_allclose(result.sold_units[1], np.array([8.0, 0.0]))
    np.testing.assert_allclose(result.proceeds.sum(axis=1), np.array([0.0, 800.0]))


def test_fifo_sell_units_with_empty_pool_marks_oversell_without_sale() -> None:
    result = fifo_sell_units(
        lot_remaining=np.zeros((2, 0), dtype=np.float64),
        ordered_lots=np.array([], dtype=np.int64),
        target_units=np.array([1.0, 0.0], dtype=np.float64),
        unit_price=np.array([100.0, 100.0], dtype=np.float64),
        cost_basis_per_unit=np.array([], dtype=np.float64),
    )

    np.testing.assert_array_equal(result.oversell, np.array([True, False]))
    assert result.sold_units.shape == (2, 0)
    np.testing.assert_allclose(result.total_proceeds, np.array([0.0, 0.0]))


def test_fifo_sell_dollars_uses_rollout_specific_prices_and_targets() -> None:
    # Rollout 0: target $250, price $100. Lot0 covers $200 (2 units); lot1 needs
    # $50 more → ceil($50/$100)=1 whole unit sold ($100). Total proceeds $300, units [2,1].
    # Rollout 1: target $450, price $150. Lot0 covers $300 (2 units); lot1 needs
    # $150 → ceil($150/$150)=1 unit ($150). Total proceeds $450, units [2,1].
    result = fifo_sell_dollars(
        lot_remaining=np.array([[2.0, 3.0], [2.0, 3.0]], dtype=np.float64),
        ordered_lots=np.array([0, 1], dtype=np.int64),
        target_dollars=np.array([250.0, 450.0], dtype=np.float64),
        unit_price=np.array([100.0, 150.0], dtype=np.float64),
        cost_basis_per_unit=np.array([40.0, 50.0], dtype=np.float64),
    )

    np.testing.assert_allclose(result.sold_units, np.array([[2.0, 1.0], [2.0, 1.0]], dtype=np.float64))
    np.testing.assert_allclose(result.proceeds.sum(axis=1), np.array([300.0, 450.0]))
    np.testing.assert_allclose(result.cost_basis_consumed.sum(axis=1), np.array([130.0, 130.0]))


def test_fifo_sell_dollars_snaps_near_exact_whole_unit_targets() -> None:
    result = fifo_sell_dollars(
        lot_remaining=np.array([[200.0]], dtype=np.float64),
        ordered_lots=np.array([0], dtype=np.int64),
        target_dollars=np.array([50_000.004], dtype=np.float64),
        unit_price=np.array([500.0], dtype=np.float64),
        cost_basis_per_unit=np.array([400.0], dtype=np.float64),
    )

    np.testing.assert_allclose(result.sold_units, np.array([[100.0]], dtype=np.float64))
    np.testing.assert_allclose(result.total_proceeds, np.array([50_000.0]))
    np.testing.assert_allclose(result.cost_basis_consumed.sum(axis=1), np.array([40_000.0]))


def test_fifo_sell_dollars_oversell_and_zero_price_do_not_partial_fill() -> None:
    result = fifo_sell_dollars(
        lot_remaining=np.array([[2.0, 3.0], [2.0, 3.0]], dtype=np.float64),
        ordered_lots=np.array([0, 1], dtype=np.int64),
        target_dollars=np.array([600.0, 1.0], dtype=np.float64),
        unit_price=np.array([100.0, 0.0], dtype=np.float64),
        cost_basis_per_unit=np.array([40.0, 50.0], dtype=np.float64),
    )

    np.testing.assert_array_equal(result.oversell, np.array([True, True]))
    np.testing.assert_allclose(result.sold_units, np.zeros((2, 2), dtype=np.float64))
    np.testing.assert_allclose(result.proceeds, np.zeros((2, 2), dtype=np.float64))


def test_fifo_rejects_duplicate_ordered_lots() -> None:
    with pytest.raises(ValueError, match="must not contain duplicates"):
        fifo_sell_units(
            lot_remaining=np.array([[2.0]], dtype=np.float64),
            ordered_lots=np.array([0, 0], dtype=np.int64),
            target_units=np.array([1.0], dtype=np.float64),
            unit_price=np.array([100.0], dtype=np.float64),
            cost_basis_per_unit=np.array([40.0], dtype=np.float64),
        )


if __name__ == "__main__":
    pytest_bazel.main()
