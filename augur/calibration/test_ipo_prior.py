"""Hermetic tests for deriving going-public CDF anchors from market prices.

A `mock_manifold_client` answers fixed YES probabilities keyed by Manifold id, so the
derivation runs with no network. The catalog mixes monotone `ipo_by_date` markets with a
later-deadline / lower-probability point (market noise) and a before-sim-start deadline, to
exercise the drop logic, and the result is round-tripped through the M1 model validator.
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest
import pytest_bazel

from augur.calibration.catalog import ExactMarket, IpoByDateMapping, ManifoldRef, MarketCatalog
from augur.calibration.ipo_prior import derive_public_market_anchors
from augur.calibration.manifold import ManifoldClient
from augur.calibration.testing import mock_manifold_client
from augur.model.private_equity_risk import PrivateEquityRiskIssuerConfig


def _ipo_market(manifold_id: str, by_date: str) -> ExactMarket:
    return ExactMarket(
        platform_ref=ManifoldRef(manifold_id=manifold_id),
        question=f"IPO by {by_date}?",
        outcome_type="BINARY",
        mapping=IpoByDateMapping(issuer="openai", by_date=date.fromisoformat(by_date)),
    )


@pytest.fixture
def catalog() -> MarketCatalog:
    # Anchor 2026-05-27. Deadlines map to months 7 / 19 / 31 / 43, plus a -1 month before
    # sim start. The 2030 market (0.80) sits below the 2029 market (0.93): market noise.
    return MarketCatalog(
        metadata={"as_of": "2026-05-29", "augur_model_as_of": "2026-05-27"},
        markets=[
            _ipo_market("B28", "2028-01-01"),  # month 19
            _ipo_market("B27", "2027-01-01"),  # month 7 (out of order on purpose)
            _ipo_market("B30", "2030-01-01"),  # month 43, NON-MONOTONE
            _ipo_market("B29", "2029-01-01"),  # month 31
            _ipo_market("BPRE", "2026-05-01"),  # month -1, dropped
        ],
    )


@pytest.fixture
def prices() -> ManifoldClient:
    return mock_manifold_client({"B27": 0.30, "B28": 0.55, "B29": 0.93, "B30": 0.80, "BPRE": 0.99})


def test_derives_monotone_anchors_dropping_market_noise(catalog: MarketCatalog, prices: ManifoldClient) -> None:
    anchors = derive_public_market_anchors(catalog, price_client=prices)

    # The before-sim-start market (month -1) and the non-monotone 2030 point are dropped.
    assert [anchor.month for anchor in anchors] == [7, 19, 31]
    assert [anchor.cumulative_probability for anchor in anchors] == [0.30, 0.55, 0.93]

    # Month strictly increasing; cumulative probability non-decreasing.
    months = [anchor.month for anchor in anchors]
    assert all(later > earlier for earlier, later in itertools.pairwise(months))
    cumulatives = [anchor.cumulative_probability for anchor in anchors]
    assert all(later >= earlier for earlier, later in itertools.pairwise(cumulatives))


def test_derived_anchors_validate_against_m1_issuer_config(catalog: MarketCatalog, prices: ManifoldClient) -> None:
    # The whole point of the derivation: the output must satisfy the M1 model validator
    # (strictly-increasing month, non-decreasing CDF), so the markets can feed the model.
    anchors = derive_public_market_anchors(catalog, price_client=prices)
    config = PrivateEquityRiskIssuerConfig(current_mark_usd=100.0, public_market_cdf_anchors=anchors)
    assert config.public_market_cdf_anchors == anchors


def test_probabilities_clamped_below_one() -> None:
    # A near-certain (>= 1.0) market must clamp into [0, 1) so the `lt=1.0` field accepts it.
    catalog = MarketCatalog(
        metadata={"as_of": "2026-05-29", "augur_model_as_of": "2026-05-27"}, markets=[_ipo_market("CERT", "2029-01-01")]
    )
    anchors = derive_public_market_anchors(catalog, price_client=mock_manifold_client({"CERT": 1.0}))
    assert anchors[0].cumulative_probability < 1.0


if __name__ == "__main__":
    pytest_bazel.main()
