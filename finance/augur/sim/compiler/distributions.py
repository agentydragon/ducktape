"""Security-distribution compile output — the per-unit payout a fund makes each month.

One compiled row per (pool, tax slice), because a slice is the smallest thing with a single
destination and a single income row. A fund with a `{federal_us: 0.4, corporate: 0.6}` split
is two rows over the same lots; the engine pays and taxes each independently, and their sum
is the payout, so there is no rounded total for the parts to disagree with.

Each row carries a `(lot,)` mask rather than a lot list: the units a pool holds are dynamic
(policies buy and sell into it), so the engine reads them out of `lot_remaining` every month
as `mask @ lot_remaining`. That is the same shape as the sale-policy masks and needs no
ragged structure and no scan carry.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.model.series import LevelSeriesKey, SecurityDistributionKey
from finance.augur.product.asset_key import asset_price_key
from finance.augur.sim.compiler.helpers import NO_CODE, AccountSlots, AssetTable, StringTable
from finance.augur.sim.compiler.income_buckets import IncomeBuckets
from finance.augur.sim.scenario import InterestIncome, Scenario, SecurityDistribution


@dataclass(frozen=True)
class DistributionCompileOutput:
    """Per-(pool, tax slice) payout plumbing. All arrays are `(slice,)` except `lot_mask`."""

    # (slice, lot) 0/1 — which lots' units this slice pays on.
    lot_mask: NDArray[np.int64]
    # Row into `external_values` carrying dollars per unit for the pool's asset.
    series: NDArray[np.int64]
    # Quanta per unit for the pool's asset, so `mask @ lot_remaining` converts back to units.
    quantity_scale: NDArray[np.int64]
    fraction: NDArray[np.float64]
    to_slot: NDArray[np.int64]
    # Income-tensor row this slice accrues to, or NO_CODE when the holder is untaxed.
    income_row: NDArray[np.int64]


def distribution_income_categories(scenario: Scenario) -> set[InterestIncome]:
    """The interest sources distributions contribute to the income-bucket axis.

    Same reason bonds have their own: the axis must carry a row for an issuer before the
    compiled table can name one, and a fund's slices name issuers no transfer mentions.
    """

    return {
        InterestIncome(issuer_jurisdiction_id=tax_slice.issuer_jurisdiction_id)
        for distribution in scenario.security_distributions
        for tax_slice in distribution.tax_character
    }


def compile_distributions(
    scenario: Scenario,
    strings: StringTable,
    assets: AssetTable,
    account_slot_by_key: AccountSlots,
    profile_index_by_agent: dict[str, int],
    buckets: IncomeBuckets,
    series_index_by_id: dict[LevelSeriesKey, int],
    *,
    lot_agent_codes: NDArray[np.int64],
    lot_account_codes: NDArray[np.int64],
    lot_asset_codes: NDArray[np.int64],
    lot_quantity_scale: NDArray[np.int64],
) -> DistributionCompileOutput:
    lot_count = len(lot_agent_codes)
    masks: list[NDArray[np.int64]] = []
    series: list[int] = []
    quantity_scale: list[int] = []
    fraction: list[float] = []
    to_slot: list[int] = []
    income_row: list[int] = []

    for distribution in scenario.security_distributions:
        mask = _pool_lot_mask(
            distribution,
            strings=strings,
            assets=assets,
            lot_agent_codes=lot_agent_codes,
            lot_account_codes=lot_account_codes,
            lot_asset_codes=lot_asset_codes,
        )
        row = _distribution_series_row(distribution, series_index_by_id)
        # Every lot in a pool holds the same asset, so they share one quantum size; taking it
        # from the pool's first lot beats threading the asset's scale through a second path.
        scale = int(lot_quantity_scale[np.flatnonzero(mask)[0]])
        slot = account_slot_by_key.require(
            distribution.agent_id, distribution.to_account_id, owner=f"distribution on {distribution.asset.wire_id!r}"
        )
        profile_index = profile_index_by_agent.get(distribution.agent_id, NO_CODE)
        for tax_slice in distribution.tax_character:
            masks.append(mask)
            series.append(row)
            quantity_scale.append(scale)
            fraction.append(float(tax_slice.fraction))
            to_slot.append(slot)
            income_row.append(
                buckets.bucket(profile_index, InterestIncome(issuer_jurisdiction_id=tax_slice.issuer_jurisdiction_id))
            )

    return DistributionCompileOutput(
        lot_mask=np.asarray(masks, dtype=np.int64).reshape((len(masks), lot_count)),
        series=np.asarray(series, dtype=np.int64),
        quantity_scale=np.asarray(quantity_scale, dtype=np.int64),
        fraction=np.asarray(fraction, dtype=np.float64),
        to_slot=np.asarray(to_slot, dtype=np.int64),
        income_row=np.asarray(income_row, dtype=np.int64),
    )


def _pool_lot_mask(
    distribution: SecurityDistribution,
    *,
    strings: StringTable,
    assets: AssetTable,
    lot_agent_codes: NDArray[np.int64],
    lot_account_codes: NDArray[np.int64],
    lot_asset_codes: NDArray[np.int64],
) -> NDArray[np.int64]:
    """Lots this pool pays on — including slots a policy has not bought into yet.

    Empty purchase slots carry their eventual agent/account/asset from compile time and hold
    zero units until filled, so including them is what makes a fund bought mid-horizon start
    distributing the month it is held rather than never.

    A pool with no lot at all is rejected: nothing can ever fill it, so the spec would pay
    zero forever, which reads as a fund that does not distribute rather than as the typo it
    almost certainly is.
    """

    mask: NDArray[np.int64] = (
        (lot_agent_codes == strings.require(distribution.agent_id))
        & (lot_account_codes == strings.require(distribution.holding_account_id))
        & (lot_asset_codes == assets.require(distribution.asset))
    ).astype(np.int64)
    if not mask.any():
        raise ValueError(
            f"security distribution on {distribution.asset.wire_id!r} names the pool "
            f"{distribution.agent_id}/{distribution.holding_account_id}, which holds no lots and has "
            "no purchase slot that could ever fill it, so the payout would be zero for the whole horizon"
        )
    return mask


def _distribution_series_row(distribution: SecurityDistribution, series_index_by_id: dict[LevelSeriesKey, int]) -> int:
    """The dollars-per-unit row this pool reads.

    A scenario that declares a distribution but sampled no such series cannot pay it at all,
    so that is rejected here by name rather than resolving to a missing row and surfacing
    later as a non-finite payout.
    """

    key = SecurityDistributionKey(symbol=asset_price_key(distribution.asset).symbol)
    row = series_index_by_id.get(key)
    if row is None:
        raise ValueError(
            f"security distribution on {distribution.asset.wire_id!r} has no modeled "
            f"{key.wire_id!r} series, so its per-unit payout is unknown. Add it to the sampled bundle."
        )
    return row
