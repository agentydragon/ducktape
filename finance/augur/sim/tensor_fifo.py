"""Tensorized FIFO lot consumption helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import numpy as np


@dataclass(frozen=True)
class FifoSaleResult:
    """Per-rollout FIFO sale result with full lot-axis outputs."""

    sold_units: np.ndarray
    cost_basis_consumed: np.ndarray
    proceeds: np.ndarray
    oversell: np.ndarray

    @property
    def total_proceeds(self) -> np.ndarray:
        return cast(np.ndarray, self.proceeds.sum(axis=1))


def lot_order_for_pool(
    *,
    lot_agent_codes: np.ndarray,
    lot_account_codes: np.ndarray,
    lot_asset_codes: np.ndarray,
    lot_purchase_month: np.ndarray,
    lot_id_codes: np.ndarray,
    agent_code: int,
    account_code: int,
    asset_code: int,
) -> np.ndarray:
    """Return FIFO-ordered lot indices for one `(agent, account, asset)` pool."""

    eligible = np.flatnonzero(
        (lot_agent_codes == agent_code) & (lot_account_codes == account_code) & (lot_asset_codes == asset_code)
    )
    order = np.lexsort((lot_id_codes[eligible], lot_purchase_month[eligible]))
    return eligible[order]


def fifo_sell_units(
    *,
    lot_remaining: np.ndarray,
    ordered_lots: np.ndarray,
    target_units: np.ndarray,
    unit_price: np.ndarray,
    cost_basis_per_unit: np.ndarray,
    epsilon: float = 1e-9,
) -> FifoSaleResult:
    """Sell target units FIFO across all rollouts.

    `lot_remaining` is `[R, L]`; `target_units` and `unit_price` are `[R]`.
    Oversell rows produce zero sale outputs and set `oversell=True`, so callers
    can fail the rollout or raise without accidentally partial-filling.
    """

    _validate_sale_inputs(lot_remaining, ordered_lots, target_units, unit_price, cost_basis_per_unit)
    ordered_quantity = lot_remaining[:, ordered_lots]
    available_units = ordered_quantity.sum(axis=1)
    oversell = target_units > available_units + epsilon
    effective_target = np.where(oversell, 0.0, target_units)

    before_units = np.cumsum(ordered_quantity, axis=1) - ordered_quantity
    sold_units_ordered = np.clip(effective_target[:, None] - before_units, 0.0, ordered_quantity)
    proceeds_ordered = sold_units_ordered * unit_price[:, None]
    basis_ordered = sold_units_ordered * cost_basis_per_unit[ordered_lots][None, :]

    return _scatter_ordered_result(
        lot_remaining=lot_remaining,
        ordered_lots=ordered_lots,
        sold_units_ordered=sold_units_ordered,
        basis_ordered=basis_ordered,
        proceeds_ordered=proceeds_ordered,
        oversell=oversell,
    )


def fifo_sell_dollars(
    *,
    lot_remaining: np.ndarray,
    ordered_lots: np.ndarray,
    target_dollars: np.ndarray,
    unit_price: np.ndarray,
    cost_basis_per_unit: np.ndarray,
    epsilon: float = 1e-9,
) -> FifoSaleResult:
    """Sell target dollars FIFO across all rollouts.

    `lot_remaining` is `[R, L]`; `target_dollars` and `unit_price` are `[R]`.
    Oversell rows produce zero sale outputs and set `oversell=True`.
    """

    _validate_sale_inputs(lot_remaining, ordered_lots, target_dollars, unit_price, cost_basis_per_unit)
    ordered_quantity = lot_remaining[:, ordered_lots]
    available_value = ordered_quantity * unit_price[:, None]
    available_total = available_value.sum(axis=1)
    oversell = target_dollars > available_total + epsilon
    effective_target = np.where(oversell, 0.0, target_dollars)

    before_value = np.cumsum(available_value, axis=1) - available_value
    sold_value_ordered = np.clip(effective_target[:, None] - before_value, 0.0, available_value)
    # Ceiling-round units: selling $750 from a lot priced at $100/unit means selling
    # 8 whole units (not 7.5). Snap targets that are within a cent of an exact whole-unit
    # value first; otherwise float32-ish upstream arithmetic can turn 100.0 units into
    # 100.00000x and cause a spurious extra whole-unit sale.
    # Proceeds reflect the actual whole-unit sale (may slightly exceed effective_target).
    sale_ratio = np.divide(
        sold_value_ordered, unit_price[:, None], out=np.zeros_like(sold_value_ordered), where=unit_price[:, None] > 0.0
    )
    nearest_units = np.rint(sale_ratio)
    nearest_value = nearest_units * unit_price[:, None]
    sold_units_before_clip = np.where(
        np.abs(nearest_value - sold_value_ordered) <= 0.01, nearest_units, np.ceil(sale_ratio)
    )
    sold_units_ordered = np.clip(sold_units_before_clip, 0.0, ordered_quantity)
    proceeds_ordered = sold_units_ordered * unit_price[:, None]
    basis_ordered = sold_units_ordered * cost_basis_per_unit[ordered_lots][None, :]

    return _scatter_ordered_result(
        lot_remaining=lot_remaining,
        ordered_lots=ordered_lots,
        sold_units_ordered=sold_units_ordered,
        basis_ordered=basis_ordered,
        proceeds_ordered=proceeds_ordered,
        oversell=oversell,
    )


def _validate_sale_inputs(
    lot_remaining: np.ndarray,
    ordered_lots: np.ndarray,
    target: np.ndarray,
    unit_price: np.ndarray,
    cost_basis_per_unit: np.ndarray,
) -> None:
    if lot_remaining.ndim != 2:
        raise ValueError(f"lot_remaining must have shape [R, L], got {lot_remaining.shape}")
    if ordered_lots.ndim != 1:
        raise ValueError(f"ordered_lots must have shape [O], got {ordered_lots.shape}")
    if target.shape != (lot_remaining.shape[0],):
        raise ValueError(f"target must have shape [R], got {target.shape}")
    if unit_price.shape != (lot_remaining.shape[0],):
        raise ValueError(f"unit_price must have shape [R], got {unit_price.shape}")
    if cost_basis_per_unit.shape != (lot_remaining.shape[1],):
        raise ValueError(f"cost_basis_per_unit must have shape [L], got {cost_basis_per_unit.shape}")
    if np.unique(ordered_lots).shape[0] != ordered_lots.shape[0]:
        raise ValueError("ordered_lots must not contain duplicates")
    if ordered_lots.size and (ordered_lots.min() < 0 or ordered_lots.max() >= lot_remaining.shape[1]):
        raise ValueError("ordered_lots contains an out-of-range lot index")


def _scatter_ordered_result(
    *,
    lot_remaining: np.ndarray,
    ordered_lots: np.ndarray,
    sold_units_ordered: np.ndarray,
    basis_ordered: np.ndarray,
    proceeds_ordered: np.ndarray,
    oversell: np.ndarray,
) -> FifoSaleResult:
    sold_units = np.zeros_like(lot_remaining)
    basis = np.zeros_like(lot_remaining)
    proceeds = np.zeros_like(lot_remaining)
    ordered_index = np.broadcast_to(ordered_lots[None, :], sold_units_ordered.shape)
    np.put_along_axis(sold_units, ordered_index, sold_units_ordered, axis=1)
    np.put_along_axis(basis, ordered_index, basis_ordered, axis=1)
    np.put_along_axis(proceeds, ordered_index, proceeds_ordered, axis=1)
    return FifoSaleResult(sold_units=sold_units, cost_basis_consumed=basis, proceeds=proceeds, oversell=oversell)
