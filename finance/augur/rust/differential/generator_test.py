"""The generator's own invariants, checked without running either engine.

Two matter enough to pin. One: every case of a shape has to reach the same compiled program,
or the value tier silently becomes the structural tier and the fuzzer runs orders of magnitude
fewer cases than it reports. Two: a case has to be one both engines accept, and the lot-basis
exactness rule is the constraint a value draw is most likely to break.

The compiled program's actual key is `_Static` in `sim/engine/jax_types.py` plus the shapes of
the arrays around it; what follows is the authored-side projection of it, so the checks stay
cheap enough to run over every shape. Since tax law moved up to the jurisdiction records, a
profile's brackets, deduction and rates are constants of its jurisdiction ids and no longer a
tier at all — the only tax knob a value draw still moves is how much prior-year tax is owed,
and whether any is owed stays structural because it decides whether the compiler emits the
quarterly obligation slots.
"""

from typing import Any

import pytest
import pytest_bazel

from finance.augur.rust.differential.generator import VALUE_TIER_SHAPES, Shape, build_case, random_shape
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_scale_for_asset, quantity_to_quanta
from finance.augur.sim.scenario import CapitalImprovementEvent, PropertySaleEvent, Scenario, SetRentedFractionEvent
from finance.augur.sim.testing.case import Case

VALUE_SEEDS = (0, 1, 7, 41)


def _skeleton(case: Case) -> dict[str, Any]:
    """What the plan compiler counts: the entries per scenario field and the series axis."""

    scenario = case.scenario
    return {
        "horizon_months": int(scenario.horizon_months),
        "rollout_count": case.rollout_count,
        "counts": {
            name: len(value)
            for name in type(scenario).model_fields
            if isinstance(value := getattr(scenario, name), list)
        },
        "series": sorted(key.wire_id for key in case.series),
        "private_equity_issuers": sorted(case.private_equity.issuer_ids()),
        "locations": sorted(case.locations),
    }


def _folded_lifecycle(scenario: Scenario) -> list[Any]:
    """Each property lifecycle event as the compiler folds it: its kind, month and scalars."""

    folded: list[Any] = []
    for event in scenario.property_lifecycle_events:
        match event:
            case SetRentedFractionEvent():
                folded.append(("rented_fraction", event.month, event.property_id, event.rented_fraction))
            case CapitalImprovementEvent():
                folded.append(("capital_improvement", event.month, event.property_id, event.amount))
            case PropertySaleEvent():
                folded.append(("sale", event.month, event.property_id, event.closing_cost_pct))
    return folded


def _folded_scalars(case: Case) -> list[Any]:
    """The Python scalars JAX folds into the program rather than passing as traced inputs.

    Lifecycle months and amounts, policy thresholds, and the lot purchase months that fix each
    FIFO pool's order. A value draw that moved any of them would recompile.
    """

    scenario = case.scenario
    return [
        [(lot.lot_id, lot.purchase_month_index) for lot in scenario.initial_lots],
        [
            (purchase.month, purchase.purchase_price, purchase.land_value_fraction, purchase.mortgage)
            for purchase in scenario.scheduled_property_purchases
        ],
        _folded_lifecycle(scenario),
        [
            (policy.cash_floor, policy.cash_ceiling, policy.rebalance_tolerance, policy.purchase_slots_per_sleeve)
            for policy in scenario.target_allocation_policies
        ],
        [policy.yield_params for policy in scenario.harvest_policies],
        [policy.liquid_net_worth_floor for policy in scenario.private_equity_tender_policies],
        # The jurisdictions decide the whole tax table, and whether any estimated tax is owed
        # decides whether the quarterly obligation slots exist. How much is owed does not.
        [(profile.agent_id, profile.jurisdiction_ids, profile.prior_year_tax > 0) for profile in scenario.tax_profiles],
        [bond.maturity_month_index for bond in scenario.initial_bonds],
    ]


@pytest.mark.parametrize("shape", VALUE_TIER_SHAPES, ids=lambda shape: shape.name)
def test_one_shape_is_one_compiled_program(shape: Shape) -> None:
    first, *rest = [build_case(shape, seed) for seed in VALUE_SEEDS]
    for case in rest:
        assert _skeleton(case) == _skeleton(first)
        assert _folded_scalars(case) == _folded_scalars(first)


@pytest.mark.parametrize("shape_seed", range(12))
def test_a_random_shape_is_one_compiled_program_too(shape_seed: int) -> None:
    first, *rest = [build_case(random_shape(shape_seed), seed) for seed in VALUE_SEEDS]
    for case in rest:
        assert _skeleton(case) == _skeleton(first)
        assert _folded_scalars(case) == _folded_scalars(first)


@pytest.mark.parametrize("shape_seed", range(12))
def test_lots_encode_an_exact_per_unit_basis(shape_seed: int) -> None:
    # The Rust fixture stores a lot's TOTAL basis, so a per-unit basis that does not multiply
    # out to whole quanta is refused outright — a draw that broke this would show up as a case
    # the comparison never made rather than as a fuzzing case.
    for value_seed in VALUE_SEEDS:
        scenario = build_case(random_shape(shape_seed), value_seed).scenario
        for lot in scenario.initial_lots:
            scale = quantity_scale_for_asset(lot.asset)
            units = int(quantity_to_quanta(lot.quantity, scale=scale))
            assert units > 0
            assert (
                int(currency_amount_to_quanta(lot.cost_basis_per_unit, quantum=scenario.currency.quantum))
                * units
                % scale
                == 0
            )


@pytest.mark.parametrize("shape_seed", range(12))
def test_sales_never_ask_for_more_units_than_the_pool_holds(shape_seed: int) -> None:
    for value_seed in VALUE_SEEDS:
        scenario = build_case(random_shape(shape_seed), value_seed).scenario
        held: dict[tuple[str, str, str], int] = {}
        for lot in scenario.initial_lots:
            scale = quantity_scale_for_asset(lot.asset)
            pool = (lot.agent_id, lot.account_id, lot.asset.wire_id)
            held[pool] = held.get(pool, 0) + int(quantity_to_quanta(lot.quantity, scale=scale))
        for sale in scenario.scheduled_asset_sales:
            scale = quantity_scale_for_asset(sale.asset)
            pool = (sale.agent_id, sale.source_account_id, sale.asset.wire_id)
            units = int(quantity_to_quanta(sale.quantity, scale=scale))
            assert 0 < units <= held[pool]
            # A half-unit multiple, which is what puts the per-unit divides on the tie.
            assert units % (scale // 2) == 0
            held[pool] -= units


if __name__ == "__main__":
    pytest_bazel.main()
