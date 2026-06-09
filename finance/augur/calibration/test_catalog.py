"""Validation tests for the typed prediction-market catalog (discriminated union)."""

from __future__ import annotations

from datetime import date

import pytest
import pytest_bazel
from pydantic import ValidationError

from finance.augur.calibration.catalog import (
    CorrelateMarket,
    ExactMarket,
    IpoByDateMapping,
    KalshiRef,
    ManifoldRef,
    Mappability,
    MarketCatalog,
    PolymarketRef,
    UnmappableMarket,
)
from finance.augur.calibration.platform import Platform
from util.bazel.runfiles import get_required_path


@pytest.fixture
def catalog() -> MarketCatalog:
    """A small in-memory catalog with one of each variant, built as typed objects."""
    return MarketCatalog(
        metadata={"source": "manifold", "as_of": "2026-05-29", "augur_model_as_of": "2026-05-27"},
        markets=[
            ExactMarket(
                platform_ref=ManifoldRef(manifold_id="AAA"),
                resolution_deadline=date(2027, 1, 1),
                mapping=IpoByDateMapping(issuer="openai", by_date=date(2027, 1, 1)),
            ),
            CorrelateMarket(
                platform_ref=ManifoldRef(manifold_id="BBB"),
                correlate_of="mark_per_unit_trajectory",
                correlate_strength="weak",
                reason="augur has no revenue channel.",
            ),
            UnmappableMarket(platform_ref=ManifoldRef(manifold_id="CCC"), reason="Governance; not modeled."),
        ],
    )


def test_partitions_dispatch_on_variant(catalog: MarketCatalog) -> None:
    assert [m.market_id for m in catalog.exact_markets()] == ["AAA"]
    assert [m.market_id for m in catalog.surfaced_markets()] == ["BBB", "CCC"]
    (exact,) = catalog.exact_markets()
    assert isinstance(exact, ExactMarket)
    assert exact.mapping == IpoByDateMapping(issuer="openai", by_date=date(2027, 1, 1))


def test_referenced_markets_unions_and_dedupes() -> None:
    """`referenced_markets` collects every (platform, market_id) across markets + all family rungs,
    deduping an id that backs more than one row (here "AAA" is both an exact and a correlate)."""
    catalog = MarketCatalog.model_validate(
        {
            "metadata": {"as_of": "2026-05-29"},
            "markets": [
                {
                    "manifold_id": "AAA",
                    "mappability": "exact",
                    "mapping": {"kind": "ipo_by_date", "issuer": "openai", "by_date": "2027-01-01"},
                },
                {"manifold_id": "AAA", "mappability": "correlate", "correlate_of": "ipo_by_date"},
                {"platform": "polymarket", "polymarket_id": "0xpoly", "mappability": "unmappable", "reason": "n/a"},
            ],
            "bucket_families": [
                {
                    "family_id": "spx",
                    "question": "S&P bucket",
                    "platform": "kalshi",
                    "series": "sp500",
                    "at_date": "2027-01-01",
                    "buckets": [
                        {"market_id": "K-LO", "label": "lo", "high": 6000},
                        {"market_id": "K-HI", "label": "hi", "low": 6000},
                    ],
                }
            ],
            "date_ladder_families": [
                {
                    "family_id": "ipo",
                    "question": "OpenAI IPO timing",
                    "platform": "manifold",
                    "issuer": "openai",
                    "dates": [
                        {"market_id": "D1", "by_date": "2027-01-01"},
                        {"market_id": "D2", "by_date": "2027-06-01"},
                    ],
                }
            ],
        }
    )
    assert catalog.referenced_markets() == {
        (Platform.MANIFOLD, "AAA"),
        (Platform.POLYMARKET, "0xpoly"),
        (Platform.KALSHI, "K-LO"),
        (Platform.KALSHI, "K-HI"),
        (Platform.MANIFOLD, "D1"),
        (Platform.MANIFOLD, "D2"),
    }


def test_exact_requires_mapping_fields() -> None:
    """The EXACT variant cannot be constructed without a typed `mapping`."""
    with pytest.raises(ValidationError):
        ExactMarket(platform_ref=ManifoldRef(manifold_id="D"))  # type: ignore[call-arg]


def test_correlate_requires_correlate_of() -> None:
    """The CORRELATE variant cannot be constructed without correlate_of."""
    with pytest.raises(ValidationError):
        CorrelateMarket(platform_ref=ManifoldRef(manifold_id="E"))  # type: ignore[call-arg]


def test_invalid_cross_field_state_is_unrepresentable() -> None:
    """A `mapping` on an unmappable market is rejected by `extra="forbid"`: the field
    does not exist on that variant, so the nonsensical combination cannot be built."""
    with pytest.raises(ValidationError):
        MarketCatalog.model_validate(
            {
                "metadata": {"as_of": "2026-05-29"},
                "markets": [
                    {
                        "manifold_id": "F",
                        "mappability": "unmappable",
                        "mapping": {"kind": "ipo_by_date", "issuer": "openai", "by_date": "2027-01-01"},
                    }
                ],
            }
        )


def test_unknown_mappability_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MarketCatalog.model_validate(
            {"metadata": {"as_of": "2026-05-29"}, "markets": [{"manifold_id": "G", "mappability": "weird"}]}
        )


def test_platform_ref_discriminated_union() -> None:
    """Each platform variant carries its own required ID field."""
    poly_market = ExactMarket(
        platform_ref=PolymarketRef(polymarket_id="0xabc"),
        mapping=IpoByDateMapping(issuer="openai", by_date=date(2027, 1, 1)),
    )
    assert poly_market.platform == Platform.POLYMARKET
    assert poly_market.market_id == "0xabc"

    kalshi_market = ExactMarket(
        platform_ref=KalshiRef(kalshi_id="OPENAI-IPO-2027"),
        mapping=IpoByDateMapping(issuer="openai", by_date=date(2027, 1, 1)),
    )
    assert kalshi_market.market_id == "OPENAI-IPO-2027"


def test_flat_yaml_backward_compat() -> None:
    """Existing catalogs with top-level `manifold_id` (no `platform`) are accepted."""
    catalog = MarketCatalog.model_validate(
        {
            "metadata": {"as_of": "2026-05-29"},
            "markets": [
                {
                    "manifold_id": "AAA",
                    "mappability": "exact",
                    "mapping": {"kind": "ipo_by_date", "issuer": "openai", "by_date": "2027-01-01"},
                }
            ],
        }
    )
    assert catalog.markets[0].market_id == "AAA"


def test_shipped_example_catalog_parses() -> None:
    """The worked-example catalog YAML validates against the discriminated union."""
    catalog = MarketCatalog.from_yaml(get_required_path("_main/finance/augur/calibration/example_openai_catalog.yaml"))
    assert {type(m) for m in catalog.markets} == {ExactMarket, CorrelateMarket, UnmappableMarket}
    assert catalog.exact_markets()  # at least one scored market
    assert catalog.surfaced_markets()  # and at least one surfaced market
    # Every exact market is resolver-ready (the variant guarantees a typed `mapping`).
    assert all(isinstance(m, ExactMarket) and m.mapping for m in catalog.exact_markets())
    assert all(m.mappability in set(Mappability) for m in catalog.markets)


if __name__ == "__main__":
    pytest_bazel.main()
