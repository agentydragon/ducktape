"""The generator's own invariants, checked without running either engine.

Two matter enough to pin. One: every fixture of a shape has to reach the same compiled
program, or the value tier silently becomes the structural tier and the fuzzer runs orders
of magnitude fewer cases than it reports. Two: a fixture has to be one both engines accept,
and the lot-basis exactness rule is the constraint a value draw is most likely to break.
"""

from typing import Any

import pytest
import pytest_bazel

from finance.augur.rust.differential.generator import (
    QUANTITY_SCALE,
    VALUE_TIER_SHAPES,
    Shape,
    build_fixture,
    random_shape,
)

VALUE_SEEDS = (0, 1, 7, 41)


def _skeleton(fixture: dict[str, Any]) -> dict[str, Any]:
    """What the plan compiler counts: the entries per scenario key and the series axis."""

    scenario = fixture["scenario"]
    return {
        "horizon_months": scenario["horizon_months"],
        "rollout_count": fixture["rollout_count"],
        "counts": {key: len(value) for key, value in scenario.items() if isinstance(value, list)},
        "series": [series["series_id"] for series in fixture["series"]],
    }


# The scalars JAX folds into the compiled program's static key rather than passing as traced
# inputs — lifecycle event months and amounts, policy thresholds, and the lot purchase months
# that fix each FIFO pool's order. A value draw that moved any of them would recompile.
def _folded_scalars(fixture: dict[str, Any]) -> list[Any]:
    scenario = fixture["scenario"]
    return [
        [(lot["lot_id"], lot["purchase_month"]) for lot in scenario.get("initial_lots", [])],
        [
            (purchase["month"], purchase["purchase_price"], purchase.get("mortgage"))
            for purchase in scenario.get("scheduled_property_purchases", [])
        ],
        [(event["month"], event["amount"]) for event in scenario.get("capital_improvement_events", [])],
        [(sale["month"], sale["closing_cost_bps"]) for sale in scenario.get("property_sales", [])],
        [
            (policy["cash_floor"], policy["cash_ceiling"], policy["rebalance_tolerance_ppb"])
            for policy in scenario.get("target_allocation_policies", [])
        ],
        [policy["peak_annual_yield_ppb"] for policy in scenario.get("harvest_policies", [])],
        [policy["liquid_net_worth_floor"] for policy in scenario.get("private_equity_tender_policies", [])],
        [profile["section_121_exclusion"] for profile in scenario.get("tax_profiles", [])],
    ]


@pytest.mark.parametrize("shape", VALUE_TIER_SHAPES, ids=lambda shape: shape.name)
def test_one_shape_is_one_compiled_program(shape: Shape) -> None:
    first, *rest = [build_fixture(shape, seed) for seed in VALUE_SEEDS]
    for fixture in rest:
        assert _skeleton(fixture) == _skeleton(first)
        assert _folded_scalars(fixture) == _folded_scalars(first)


@pytest.mark.parametrize("shape_seed", range(12))
def test_a_random_shape_is_one_compiled_program_too(shape_seed: int) -> None:
    first, *rest = [build_fixture(random_shape(shape_seed), seed) for seed in VALUE_SEEDS]
    for fixture in rest:
        assert _skeleton(fixture) == _skeleton(first)


@pytest.mark.parametrize("shape_seed", range(12))
def test_lots_encode_an_exact_per_unit_basis(shape_seed: int) -> None:
    # Both engines refuse a lot whose total basis does not divide into whole quanta per unit,
    # so a draw that broke this would show up as a fixture neither engine runs rather than as
    # a fuzzing case.
    for value_seed in VALUE_SEEDS:
        for lot in build_fixture(random_shape(shape_seed), value_seed)["scenario"].get("initial_lots", []):
            assert lot["basis"] * lot["quantity_scale"] % lot["units"] == 0
            assert lot["units"] > 0


@pytest.mark.parametrize("shape_seed", range(12))
def test_sales_never_ask_for_more_units_than_the_pool_holds(shape_seed: int) -> None:
    for value_seed in VALUE_SEEDS:
        scenario = build_fixture(random_shape(shape_seed), value_seed)["scenario"]
        held: dict[tuple[str, str, str], int] = {}
        for lot in scenario.get("initial_lots", []):
            held[lot["agent_id"], lot["account_id"], lot["asset_id"]] = (
                held.get((lot["agent_id"], lot["account_id"], lot["asset_id"]), 0) + lot["units"]
            )
        for sale in scenario.get("scheduled_sales", []):
            pool = (sale["agent_id"], sale["account_id"], sale["asset_id"])
            assert 0 < sale["units"] <= held[pool]
            assert sale["units"] % (QUANTITY_SCALE // 2) == 0
            held[pool] -= sale["units"]


if __name__ == "__main__":
    pytest_bazel.main()
