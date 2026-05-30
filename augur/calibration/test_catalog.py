"""Validation tests for the typed prediction-market catalog (discriminated union)."""

from __future__ import annotations

from datetime import date

import pytest
import pytest_bazel
from pydantic import ValidationError

from augur.calibration.catalog import CorrelateMarket, ExactMarket, Mappability, MarketCatalog, UnmappableMarket
from util.bazel.runfiles import get_required_path


def _catalog() -> MarketCatalog:
    """A small in-memory catalog with one of each variant, built as typed objects."""
    return MarketCatalog(
        metadata={"source": "manifold", "as_of": "2026-05-29", "augur_model_as_of": "2026-05-27"},
        markets=[
            ExactMarket(
                slug="ipo-before-2027",
                manifold_id="AAA",
                question="Issuer IPO before 2027?",
                outcome_type="BINARY",
                resolution_deadline=date(2027, 1, 1),
                mapping_kind="ipo_by_date",
                mapping_params={"by_date": "2027-01-01"},
            ),
            CorrelateMarket(
                slug="revenue-2028",
                manifold_id="BBB",
                question="Issuer reaches $100B revenue in 2028?",
                outcome_type="BINARY",
                correlate_of="mark_per_unit_trajectory",
                correlate_strength="weak",
                reason="augur has no revenue channel.",
            ),
            UnmappableMarket(
                slug="ceo-2026",
                manifold_id="CCC",
                question="Will the CEO still be CEO at the end of 2026?",
                outcome_type="BINARY",
                reason="Governance; not modeled.",
            ),
        ],
    )


def test_partitions_dispatch_on_variant() -> None:
    catalog = _catalog()
    assert [m.slug for m in catalog.exact_markets()] == ["ipo-before-2027"]
    assert [m.slug for m in catalog.surfaced_markets()] == ["revenue-2028", "ceo-2026"]
    (exact,) = catalog.exact_markets()
    assert isinstance(exact, ExactMarket)
    assert exact.mapping_params == {"by_date": "2027-01-01"}


def test_exact_requires_mapping_fields() -> None:
    """The EXACT variant cannot be constructed without mapping_kind + mapping_params."""
    with pytest.raises(ValidationError):
        ExactMarket(slug="x", manifold_id="D", question="?", outcome_type="BINARY")  # type: ignore[call-arg]


def test_correlate_requires_correlate_of() -> None:
    """The CORRELATE variant cannot be constructed without correlate_of."""
    with pytest.raises(ValidationError):
        CorrelateMarket(slug="x", manifold_id="E", question="?", outcome_type="BINARY")  # type: ignore[call-arg]


def test_invalid_cross_field_state_is_unrepresentable() -> None:
    """A `mapping_kind` on an unmappable market is rejected by `extra="forbid"`: the field
    does not exist on that variant, so the nonsensical combination cannot be built."""
    with pytest.raises(ValidationError):
        MarketCatalog.model_validate(
            {
                "metadata": {"as_of": "2026-05-29"},
                "markets": [
                    {
                        "slug": "bad",
                        "manifold_id": "F",
                        "question": "?",
                        "outcome_type": "BINARY",
                        "mappability": "unmappable",
                        "mapping_kind": "ipo_by_date",
                    }
                ],
            }
        )


def test_unknown_mappability_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MarketCatalog.model_validate(
            {
                "metadata": {"as_of": "2026-05-29"},
                "markets": [
                    {"slug": "x", "manifold_id": "G", "question": "?", "outcome_type": "BINARY", "mappability": "weird"}
                ],
            }
        )


def test_shipped_example_catalog_parses() -> None:
    """The worked-example catalog YAML validates against the discriminated union."""
    catalog = MarketCatalog.from_yaml(get_required_path("_main/augur/calibration/example_openai_catalog.yaml"))
    assert {type(m) for m in catalog.markets} == {ExactMarket, CorrelateMarket, UnmappableMarket}
    assert catalog.exact_markets()  # at least one scored market
    assert catalog.surfaced_markets()  # and at least one surfaced market
    # Every exact market is resolver-ready (the variant guarantees the fields exist and are typed).
    assert all(isinstance(m, ExactMarket) and m.mapping_kind for m in catalog.exact_markets())
    assert all(m.mappability in set(Mappability) for m in catalog.markets)


if __name__ == "__main__":
    pytest_bazel.main()
