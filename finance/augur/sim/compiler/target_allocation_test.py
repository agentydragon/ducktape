"""What the target-allocation compiler resolves, and what it refuses.

Compilation is where a policy's sleeves stop being `AssetKey`s and become dense codes the
traced engine gathers by. The interesting cases are the ones where something does not
resolve: those must fail HERE, because the engine cannot raise on a code it gathers.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.series import InflationKey, SecuritySymbol
from finance.augur.product.asset_key import SecurityKey
from finance.augur.sim.compiler.helpers import NO_CODE, AccountSlots, AssetTable, StringTable
from finance.augur.sim.compiler.target_allocation import compile_target_allocation_policies
from finance.augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    Scenario,
    SeriesIndexedAmount,
    SleeveTarget,
    TargetAllocationPolicy,
)

_VTI = SecurityKey(symbol=SecuritySymbol("vti"))
_BND = SecurityKey(symbol=SecuritySymbol("bnd"))
_SLOTS = AccountSlots(by_key={("alice", "checking"): 0, ("bob", "checking"): 1}, external=2)


def _policy(**overrides: object) -> TargetAllocationPolicy:
    return TargetAllocationPolicy(
        **{
            "agent_id": "alice",
            "account_id": "checking",
            "sleeves": [SleeveTarget(asset=_VTI, weight=3), SleeveTarget(asset=_BND, weight=1)],
            "cash_floor_usd": 10_000.0,
            "cash_ceiling_usd": 50_000.0,
            **overrides,
        }
    )


def _scenario(policies: list[TargetAllocationPolicy]) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="bob")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=100_000.0),
            InitialAccountBalance(agent_id="bob", account_id="checking", balance_usd=0.0),
        ],
        horizon_months=3,
        tax_profiles=[],
        target_allocation_policies=policies,
    )


def _compile(policies: list[TargetAllocationPolicy], *, slots: AccountSlots = _SLOTS, series: dict | None = None):
    strings, assets = StringTable(), AssetTable()
    for name in ("alice", "bob", "checking", "savings"):
        strings.require(name)
    return compile_target_allocation_policies(_scenario(policies), strings, assets, slots, series or {})


def test_weights_survive_compilation_in_sleeve_order() -> None:
    """Weights are what the water level divides by, so their pairing with sleeves is exactly
    what a reordering bug would corrupt while every shape stayed right."""

    out = _compile([_policy()])

    assert out.weights[0].tolist() == [3, 1]
    assert out.sleeve_assets[0, 0] != out.sleeve_assets[0, 1]


def test_padded_sleeve_columns_weigh_nothing() -> None:
    """Policies of different sleeve counts share one dense width, so the shorter one is
    padded. The padding must weigh NOTHING: a padded column carrying a weight would claim a
    share of the target, and the real sleeves would be sold down to fund a sleeve that does
    not exist."""

    out = _compile([_policy(), _policy(agent_id="bob", sleeves=[SleeveTarget(asset=_VTI, weight=5)])])

    assert out.sleeve_assets[1, 1] == NO_CODE
    assert np.all(out.weights[out.sleeve_assets == NO_CODE] == 0)


def test_an_indexed_band_keeps_both_bounds_on_the_same_series() -> None:
    """The config-time ordering check is only sound because indexing scales both bounds by
    the same series. That soundness is a property of the COMPILED rows, so assert it rather
    than trust it."""

    out = _compile(
        [
            _policy(
                cash_floor_usd=SeriesIndexedAmount(base_amount_usd=10_000.0, series=InflationKey()),
                cash_ceiling_usd=SeriesIndexedAmount(base_amount_usd=50_000.0, series=InflationKey()),
            )
        ],
        series={InflationKey(): 0},
    )

    assert out.floor_series[0] == out.ceiling_series[0] == 0


def test_an_unknown_funding_account_is_a_typo_not_a_counterparty() -> None:
    """`require`, not `resolve`. Settling a policy's proceeds against the rest of the world
    would sell the agent's lots and hand the cash to nobody — and silently, since net worth
    stays right when a lot leaves as its cash arrives."""

    with pytest.raises(ValueError, match="has no cash account"):
        _compile([_policy(account_id="savings")], slots=AccountSlots(by_key={("alice", "checking"): 0}, external=1))


if __name__ == "__main__":
    pytest_bazel.main()
