"""What a spending ladder refuses to be configured as.

Every check here is one the engine cannot make for itself. A tier's cost may be
CPI-indexed, so whether one rung sits below another would otherwise depend on the path;
configuration time is the only place it can be settled for every path at once.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.model.series import InflationKey
from finance.augur.sim.scenario import SeriesIndexedAmount, SpendingLadder, SpendingTier


def _tier(name: str, monthly: int) -> SpendingTier:
    return SpendingTier(name=name, monthly_spend=Decimal(monthly))


def _indexed(name: str, monthly: int) -> SpendingTier:
    """A tier held in real terms, which is what makes the ordering check non-trivial."""

    return SpendingTier(
        name=name, monthly_spend=SeriesIndexedAmount(base_amount=Decimal(monthly), series=InflationKey())
    )


def test_a_well_formed_ladder_is_accepted() -> None:
    """The happy path, so the rejections below are known to reject something specific
    rather than everything."""

    ladder = SpendingLadder(tiers=(_tier("intended", 9_000), _tier("trimmed", 6_000), _tier("lean", 4_000)))

    assert [tier.name for tier in ladder.tiers] == ["intended", "trimmed", "lean"]


def test_a_single_tier_ladder_is_accepted() -> None:
    """A plan with no fallback is a real plan, and it is the shape every scenario that
    names one spend level already has. Rejecting it would make the ladder unable to
    express today's behaviour, so a caller could not adopt it incrementally."""

    assert len(SpendingLadder(tiers=(_tier("intended", 9_000),)).tiers) == 1


def test_an_empty_ladder_is_rejected() -> None:
    """A ladder with no rungs describes no standard of living at all — not even the one
    the plan intends — so there is nothing for a transition to step down from."""

    with pytest.raises(ValidationError, match="names no tiers"):
        SpendingLadder(tiers=())


@pytest.mark.parametrize(
    ("costs", "why"), [((6_000, 9_000), "inverted"), ((9_000, 9_000), "equal")], ids=["inverted", "equal"]
)
def test_rungs_must_strictly_descend(costs: tuple[int, int], why: str) -> None:
    """Index order IS cost order — that is what makes "step down" mean anything, and what
    lets monotone traversal be an index that never decreases. Equal rungs are rejected too:
    stepping between them changes the reported tier without changing the life."""

    with pytest.raises(ValidationError, match="tiers are ordered by cost"):
        SpendingLadder(tiers=(_tier("above", costs[0]), _tier("below", costs[1])))


def test_ordering_is_checked_on_indexed_tiers_too() -> None:
    """An indexed tier's ordering is settled on its base amount.

    It cannot be checked per month: the amount is traced, and a traced value cannot drive a
    raise. With one price level every tier scales by the same series, so the base ordering
    holds on every path — the assumption a second price level (#5489) would break."""

    with pytest.raises(ValidationError, match="tiers are ordered by cost"):
        SpendingLadder(tiers=(_indexed("above", 4_000), _indexed("below", 6_000)))

    SpendingLadder(tiers=(_indexed("above", 9_000), _indexed("below", 4_000)))


def test_duplicate_tier_names_are_rejected() -> None:
    """Tiers are reported by name, so two rungs sharing one makes time-in-tier
    unattributable — the quantity the whole ladder exists to report."""

    with pytest.raises(ValidationError, match="more than once"):
        SpendingLadder(tiers=(_tier("lean", 9_000), _tier("lean", 4_000)))


def test_a_zero_spend_bottom_rung_is_rejected() -> None:
    """The bottom rung is a funded life. A ladder whose floor is "spend nothing" describes
    running out of money, which the failure vector already reports — and it would let a
    study claim a plan "survived" in a state where it funded nothing at all."""

    with pytest.raises(ValidationError, match="bottom rung is a funded life"):
        SpendingLadder(tiers=(_tier("intended", 9_000), _tier("nothing", 0)))


if __name__ == "__main__":
    pytest_bazel.main()
