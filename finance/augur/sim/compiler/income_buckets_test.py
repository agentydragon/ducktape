"""Unit invariants for the (profile, source) → row mapping.

The e2e tests prove the mapping is wired correctly through the engine; these prove the
mapping itself, at shapes the e2e scenarios do not reach.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.compiler.helpers import NO_CODE
from finance.augur.sim.compiler.income_buckets import IncomeBuckets
from finance.augur.sim.scenario import InterestIncome, OrdinaryIncome

_TREASURY = InterestIncome(issuer_jurisdiction_id="federal_us")
_MUNI = InterestIncome(issuer_jurisdiction_id="california")
_CORPORATE = InterestIncome()


def _buckets(*sources: InterestIncome, profile_count: int) -> IncomeBuckets:
    return IncomeBuckets.for_sources(set(sources), profile_count=profile_count)


def test_ordinary_exists_without_being_requested() -> None:
    """A scenario that never tags income still needs somewhere to put wages."""

    assert _buckets(profile_count=2).source_ids == (OrdinaryIncome(),)


def test_rows_are_unique_across_every_profile_and_source() -> None:
    """The property the whole flattening rests on: no two (profile, source) share a row."""

    buckets = _buckets(_TREASURY, _MUNI, _CORPORATE, profile_count=4)
    rows = [
        buckets.bucket(profile, source) for profile in range(buckets.profile_count) for source in buckets.source_ids
    ]

    assert sorted(rows) == list(range(buckets.row_count))


@pytest.mark.parametrize("profile_count", [1, 3])
def test_split_rows_inverts_bucket(profile_count: int) -> None:
    buckets = _buckets(_TREASURY, _CORPORATE, profile_count=profile_count)
    rows = np.asarray(
        [buckets.bucket(profile, source) for profile in range(profile_count) for source in buckets.source_ids]
    )

    profiles, sources = buckets.split_rows(rows)

    assert [buckets.source_ids[index] for index in sources] == [
        source for _ in range(profile_count) for source in buckets.source_ids
    ]
    assert profiles.tolist() == [profile for profile in range(profile_count) for _ in buckets.source_ids]


def test_untaxed_recipient_stays_untaxed() -> None:
    """`NO_CODE` must survive the arithmetic — multiplying it by a source count would turn
    an untaxed agent into a valid row and silently start taxing them."""

    buckets = _buckets(_TREASURY, profile_count=2)

    assert buckets.bucket(NO_CODE, _TREASURY) == NO_CODE
    assert buckets.ordinary_bucket(NO_CODE) == NO_CODE
    assert buckets.ordinary_rows(np.asarray([NO_CODE, 1])).tolist() == [NO_CODE, buckets.ordinary_bucket(1)]


def test_ordinary_rows_matches_ordinary_bucket() -> None:
    """The vectorized path and the scalar path are the same map."""

    buckets = _buckets(_TREASURY, _MUNI, profile_count=3)
    profiles = np.arange(3)

    assert buckets.ordinary_rows(profiles).tolist() == [buckets.ordinary_bucket(p) for p in range(3)]


def test_source_order_is_stable_regardless_of_discovery_order() -> None:
    """Bucket indices are baked into the jitted program's static structure, so the same
    scenario must produce the same axis however the compiler happened to walk it."""

    forward = IncomeBuckets.for_sources({_TREASURY, _MUNI, _CORPORATE}, profile_count=1)
    reverse = IncomeBuckets.for_sources({_CORPORATE, _MUNI, _TREASURY}, profile_count=1)

    assert forward.source_ids == reverse.source_ids
    assert forward.source_wire_ids() == ("ordinary", "interest:california", "interest:federal_us", "interest:corporate")


if __name__ == "__main__":
    pytest_bazel.main()
