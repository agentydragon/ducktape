"""What a target-allocation policy refuses to be configured as.

Every check here is one a traced engine cannot make: the band's bounds may be CPI-indexed,
so their monthly values are arrays and a traced value cannot drive a `raise`. Config time is
the only place these can be caught, which is why they are worth their own tests.
"""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import InflationKey, SecuritySymbol
from finance.augur.product.asset_key import SecurityKey
from finance.augur.sim.scenario import SeriesIndexedAmount, SleeveTarget, TargetAllocationPolicy

_VTI = SecurityKey(symbol=SecuritySymbol("vti"))
_BND = SecurityKey(symbol=SecuritySymbol("bnd"))


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
        _policy(cash_floor_usd=50_000.0, cash_ceiling_usd=10_000.0)


def test_a_negative_floor_is_rejected() -> None:
    """A negative floor would mean the agent aims to be overdrawn."""

    with pytest.raises(ValidationError, match="floor must not be negative"):
        _policy(cash_floor_usd=-1.0)


def test_the_band_is_checked_on_indexed_bounds_too() -> None:
    """A CPI-indexed band is checked on its BASE amounts. That is sufficient rather than a
    compromise: indexing scales both bounds by the same series, so an ordering that holds at
    configuration holds on every path — which is what makes the traced check unnecessary."""

    with pytest.raises(ValidationError, match="floor must not exceed its ceiling"):
        _policy(
            cash_floor_usd=SeriesIndexedAmount(base_amount_usd=50_000.0, series=InflationKey()),
            cash_ceiling_usd=SeriesIndexedAmount(base_amount_usd=10_000.0, series=InflationKey()),
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


def test_a_zero_weight_is_rejected() -> None:
    """Weight zero means "in the universe but targeted at nothing", which is not a target —
    it is the sleeve being absent, and absence is expressed by not naming it. Allowing zero
    would also put a zero in the water-filling divisor."""

    with pytest.raises(ValidationError):
        SleeveTarget(asset=_VTI, weight=0)


if __name__ == "__main__":
    pytest_bazel.main()
