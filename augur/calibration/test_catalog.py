"""Loader + validation tests for the typed prediction-market catalog."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.calibration.catalog import Mappability, MarketCatalog
from util.bazel.runfiles import get_required_path

_VALID = dedent(
    """
    metadata:
      source: manifold
      as_of: "2026-05-29"
      augur_model_as_of: "2026-05-27"
    markets:
      - slug: ipo-before-2027
        manifold_id: AAA
        question: "Issuer IPO before 2027?"
        outcome_type: BINARY
        close_date: "2027-01-01"
        resolution_deadline: "2027-01-01"
        mappability: exact
        mapping_kind: ipo_by_date
        mapping_params: {by_date: "2027-01-01"}
        curation_snapshot: {yes_prob: 0.75, total_liquidity: 1000, unique_bettors: 82, volume: 26892}
      - slug: revenue-2028
        manifold_id: BBB
        question: "Issuer reaches $100B revenue in 2028?"
        outcome_type: BINARY
        resolution_deadline: "2028-12-31"
        mappability: correlate
        correlate_of: mark_per_unit_trajectory
        correlate_strength: weak
        reason: "augur has no revenue channel."
        curation_snapshot: {yes_prob: 0.59}
      - slug: ceo-2026
        manifold_id: CCC
        question: "Will the CEO still be CEO at the end of 2026?"
        outcome_type: BINARY
        mappability: unmappable
        reason: "Governance; not modeled."
        curation_snapshot: {yes_prob: 0.94}
    """
)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_and_partitions(tmp_path: Path) -> None:
    catalog = MarketCatalog.from_yaml(_write(tmp_path, _VALID))
    assert [m.slug for m in catalog.exact_markets()] == ["ipo-before-2027"]
    assert [m.slug for m in catalog.surfaced_markets()] == ["revenue-2028", "ceo-2026"]
    exact = catalog.exact_markets()[0]
    assert exact.mappability is Mappability.EXACT
    assert exact.mapping_params == {"by_date": "2027-01-01"}
    assert exact.resolution_deadline == date(2027, 1, 1)
    assert exact.curation_snapshot.yes_prob == 0.75


def test_exact_without_mapping_params_is_rejected(tmp_path: Path) -> None:
    """A valuation/exact-without-params entry must not load -- it cannot be scored."""
    body = dedent(
        """
        metadata: {as_of: "2026-05-29"}
        markets:
          - slug: valuation-1t
            manifold_id: DDD
            question: "Issuer valuation of $1T by end of 2027?"
            outcome_type: BINARY
            mappability: exact
            mapping_kind: valuation_threshold
            curation_snapshot: {yes_prob: 0.77}
        """
    )
    with pytest.raises(ValidationError, match="requires mapping_kind and mapping_params"):
        MarketCatalog.from_yaml(_write(tmp_path, body))


def test_correlate_without_correlate_of_is_rejected(tmp_path: Path) -> None:
    body = dedent(
        """
        metadata: {as_of: "2026-05-29"}
        markets:
          - slug: dangling-correlate
            manifold_id: EEE
            question: "?"
            outcome_type: BINARY
            mappability: correlate
            curation_snapshot: {yes_prob: 0.5}
        """
    )
    with pytest.raises(ValidationError, match="requires correlate_of"):
        MarketCatalog.from_yaml(_write(tmp_path, body))


def test_unknown_field_is_rejected(tmp_path: Path) -> None:
    body = dedent(
        """
        metadata: {as_of: "2026-05-29"}
        markets:
          - slug: typo
            manifold_id: FFF
            question: "?"
            outcome_type: BINARY
            mappability: unmappable
            curation_snapshot: {yes_prob: 0.5}
            mappabilty: exact
        """
    )
    with pytest.raises(ValidationError):
        MarketCatalog.from_yaml(_write(tmp_path, body))


def test_shipped_example_catalog_parses() -> None:
    """The worked-example catalog validates against the typed model."""
    catalog = MarketCatalog.from_yaml(get_required_path("_main/augur/calibration/example_openai_catalog.yaml"))
    assert catalog.exact_markets()  # has at least one scored market
    assert catalog.surfaced_markets()  # and at least one surfaced market
    # Every exact market is resolver-ready; every correlate names its augur signal.
    assert all(m.mapping_kind and m.mapping_params is not None for m in catalog.exact_markets())


if __name__ == "__main__":
    pytest_bazel.main()
