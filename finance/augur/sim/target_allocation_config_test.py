"""What a target-allocation policy refuses to be configured as.

Every check here is one the engine cannot make for itself: the band's bounds may be
CPI-indexed, so whether a band is inverted would otherwise depend on the path. Config time is
the only place these can be caught for every path at once, which is why they are worth their
own tests.
"""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import InflationKey, SecurityKey, SecuritySymbol
from finance.augur.sim.scenario import (
    CashflowOnly,
    DriftBand,
    SeriesIndexedAmount,
    SleeveTarget,
    TargetAllocationPolicy,
)

_VTI = SecurityKey(symbol=SecuritySymbol("vti"))
_BND = SecurityKey(symbol=SecuritySymbol("bnd"))


def _policy(**overrides: object) -> TargetAllocationPolicy:
    return TargetAllocationPolicy(
        **{
            "agent_id": "alice",
            "account_id": "checking",
            "sleeves": [SleeveTarget(asset=_VTI, weight=3), SleeveTarget(asset=_BND, weight=1)],
            "cash_floor": 10_000,
            "cash_ceiling": 50_000,
            "rebalancing": CashflowOnly(),
            "allow_purchases": False,
            **overrides,
        }
    )


def test_a_well_formed_policy_is_accepted() -> None:
    """The happy path, so the rejections below are known to be rejecting something specific
    rather than everything."""

    policy = _policy()

    assert [sleeve.weight for sleeve in policy.sleeves] == [3, 1]


def test_an_inverted_band_is_rejected() -> None:
    """A floor above its ceiling has no interior, so every balance crosses both bounds at
    once and the policy would sell and buy in the same month, forever. The exclusivity the
    policy relies on is exactly this check."""

    with pytest.raises(ValidationError, match="floor must not exceed its ceiling"):
        _policy(cash_floor=50_000, cash_ceiling=10_000)


def test_a_negative_floor_is_rejected() -> None:
    """A negative floor would mean the agent aims to be overdrawn."""

    with pytest.raises(ValidationError, match="floor must not be negative"):
        _policy(cash_floor=-1)


def test_the_band_is_checked_on_indexed_bounds_too() -> None:
    """A CPI-indexed band is checked on its BASE amounts. That is sufficient rather than a
    compromise: indexing scales both bounds by the same series, so an ordering that holds at
    configuration holds on every path — which is what makes the traced check unnecessary."""

    with pytest.raises(ValidationError, match="floor must not exceed its ceiling"):
        _policy(
            cash_floor=SeriesIndexedAmount(base_amount=50000, series=InflationKey()),
            cash_ceiling=SeriesIndexedAmount(base_amount=10000, series=InflationKey()),
        )


def test_an_asset_weighted_twice_is_rejected() -> None:
    """Two sleeves naming one asset count its value twice, inflating the portfolio total and
    skewing every target — including the targets of the sleeves that are correct."""

    with pytest.raises(ValidationError, match="more than once"):
        _policy(sleeves=[SleeveTarget(asset=_VTI, weight=3), SleeveTarget(asset=_VTI, weight=1)])


def test_a_policy_with_no_sleeves_is_rejected() -> None:
    """An empty target can never raise cash, so the policy would silently fail every
    obligation the account could not already cover — a ruin that looks like the model's
    answer rather than like a misconfiguration."""

    with pytest.raises(ValidationError, match="names no sleeves"):
        _policy(sleeves=[])


def test_zero_target_remains_in_scope_but_all_zero_targets_are_rejected() -> None:
    policy = _policy(sleeves=[SleeveTarget(asset=_VTI, weight=0), SleeveTarget(asset=_BND, weight=1)])
    assert [sleeve.asset for sleeve in policy.sleeves] == [_VTI, _BND]
    with pytest.raises(ValidationError, match="at least one positive"):
        _policy(sleeves=[SleeveTarget(asset=_VTI, weight=0), SleeveTarget(asset=_BND, weight=0)])
    with pytest.raises(ValidationError):
        SleeveTarget(asset=_VTI, weight=-1)


def test_a_policy_must_say_how_it_rebalances() -> None:
    """`rebalancing` has no default, and that is the point.

    `CashflowOnly` and `DriftBand` are both real strategies with different turnover and tax
    drag — the very difference the allocation study exists to measure. A default would pick one
    for every caller that did not think about it, and the pick would show up neither at the call
    site nor in any report of the run. So the model refuses to be constructed without one.
    """

    with pytest.raises(ValidationError, match="rebalancing"):
        TargetAllocationPolicy(
            allow_purchases=False,
            agent_id="alice",
            account_id="checking",
            sleeves=[SleeveTarget(asset=_VTI, weight=3), SleeveTarget(asset=_BND, weight=1)],
            cash_floor=10_000,
            cash_ceiling=50_000,
        )  # type: ignore[call-arg]


def test_a_drift_band_with_purchases_disabled_is_rejected() -> None:
    """A rebalance sells the overweight sleeves and buys the underweight ones. If buying
    is disabled the policy would only ever sell — draining a little
    more of the portfolio into cash on every trigger, which is a slow ruin rather than a
    rebalance. `CashflowOnly` is unaffected, since it never trades on drift at all."""

    with pytest.raises(ValidationError, match=r"drift band .* but purchases are disabled"):
        _policy(rebalancing=DriftBand(tolerance=0.25), allow_purchases=False)

    _policy(rebalancing=DriftBand(tolerance=0.25), allow_purchases=True)
    _policy(rebalancing=CashflowOnly(), allow_purchases=False)


if __name__ == "__main__":
    pytest_bazel.main()
