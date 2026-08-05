from __future__ import annotations

import numpy as np
import pytest_bazel

from finance.augur.sim.compiler.plan import lot_order_for_pool


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


def test_lot_order_for_pool_breaks_purchase_month_ties_by_lot_id() -> None:
    order = lot_order_for_pool(
        lot_agent_codes=np.array([1, 1, 1], dtype=np.int64),
        lot_account_codes=np.array([10, 10, 10], dtype=np.int64),
        lot_asset_codes=np.array([20, 20, 20], dtype=np.int64),
        lot_purchase_month=np.array([3, 3, 3], dtype=np.int64),
        lot_id_codes=np.array([102, 100, 101], dtype=np.int64),
        agent_code=1,
        account_code=10,
        asset_code=20,
    )

    np.testing.assert_array_equal(order, np.array([1, 2, 0], dtype=np.int64))


def test_lot_order_for_pool_with_no_eligible_lots_is_empty() -> None:
    order = lot_order_for_pool(
        lot_agent_codes=np.array([1, 1], dtype=np.int64),
        lot_account_codes=np.array([10, 11], dtype=np.int64),
        lot_asset_codes=np.array([20, 20], dtype=np.int64),
        lot_purchase_month=np.array([0, 1], dtype=np.int64),
        lot_id_codes=np.array([100, 101], dtype=np.int64),
        agent_code=1,
        account_code=12,
        asset_code=20,
    )

    assert order.size == 0


if __name__ == "__main__":
    pytest_bazel.main()
