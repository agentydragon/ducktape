"""What a scenario demands of the exogenous model, before any engine runs it."""

from __future__ import annotations

import pytest_bazel

from finance.augur.model.series import InflationKey
from finance.augur.sim.compiler.series import scenario_level_series_keys
from finance.augur.sim.testing.bonds import bond_scenario


def test_an_indexed_bond_demands_an_inflation_path() -> None:
    """The demand a TIPS makes that nothing else in a scenario need make.

    Every other level-series demand comes from something PRICED — a lot, a sleeve, a sale. A
    bond has no price series at all, so an indexed one is the only instrument whose exogenous
    demand is invisible from the thing that carries it. Without it, `compile_bonds` raises
    "carry no inflation path" for any caller that derives its sampling request from the
    scenario, which is what the product surface does, unless it happens to want CPI anyway.

    Asserted on the demand function rather than through a run: a run supplies its own bundle
    and would pass either way, which is how the gap stayed invisible from `sim/`.
    """

    assert InflationKey() in scenario_level_series_keys(bond_scenario(indexed=True))
    # And not otherwise: a nominal bond's cashflows are fixed by its terms, so demanding a
    # series it never reads would fail an unmodeled-inflation deployment for no reason.
    assert InflationKey() not in scenario_level_series_keys(bond_scenario(indexed=False))


if __name__ == "__main__":
    pytest_bazel.main()
