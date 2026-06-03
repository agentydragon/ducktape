"""Derive empirical going-public CDF anchors from live prediction-market prices.

The PE realization-risk model (`augur.model.private_equity_risk`) accepts a
``public_market_cdf_anchors`` vector: (month-from-sim-start, P(public by month)) pairs
that pin down a front-loaded, saturating IPO-prior CDF. This module turns a curated
catalog's ``ipo_by_date`` markets into exactly that vector, so the live market term
structure can feed the model end-to-end.

Markets are noisy and not internally consistent (a "by 2030" market can sit BELOW a
"by 2029" one), and the model's validator demands strictly-increasing months and a
non-decreasing CDF. `derive_public_market_anchors` therefore sorts, de-dups, and drops
non-monotone points (logging each drop) before constructing the typed anchors.

The ``main()`` entry point fetches live Manifold prices for a catalog YAML and prints a
ready-to-paste ``public_market_cdf_anchors:`` block.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

from augur.calibration.catalog import IpoByDateMapping, MarketCatalog
from augur.calibration.manifold import ManifoldClient
from augur.calibration.resolvers import months_after
from augur.model.private_equity_risk import PublicMarketCdfAnchor

logger = logging.getLogger(__name__)

# The model's `cumulative_probability` field is `lt=1.0`; a market at 1.0 (or above, from
# rounding) would be rejected, so clamp into [0.0, 1.0) leaving residual survival mass.
_MAX_CUMULATIVE_PROBABILITY = 0.999


def derive_public_market_anchors(
    catalog: MarketCatalog, *, price_client: ManifoldClient
) -> tuple[PublicMarketCdfAnchor, ...]:
    """Build a monotone going-public CDF from the catalog's live ``ipo_by_date`` markets.

    Each `ipo_by_date` market's deadline becomes a month offset from the catalog's model
    anchor date and its live YES probability becomes the cumulative probability. Markets at
    month < 1 are dropped (the model requires `month >= 1`), duplicate months collapse to the
    higher probability, and points that would make the CDF decrease are dropped as market
    noise (each drop is logged).
    """
    anchor_date = catalog.metadata.model_anchor_date

    # month -> highest live prob seen at that month (de-dups duplicate deadlines).
    prob_by_month: dict[int, float] = {}
    for market in catalog.exact_markets():
        if not isinstance(market.mapping, IpoByDateMapping):
            continue
        by_date = market.mapping.by_date
        month = months_after(anchor_date, by_date)
        if month < 1:
            logger.info(
                "dropping %s: deadline %s is at month %d < 1 (before sim start)", market.market_id, by_date, month
            )
            continue
        prob = min(max(price_client.fetch_yes_probability(market.market_id), 0.0), _MAX_CUMULATIVE_PROBABILITY)
        if month in prob_by_month and prob <= prob_by_month[month]:
            logger.info(
                "collapsing duplicate month %d for %s: keeping higher prob %.4f",
                month,
                market.market_id,
                prob_by_month[month],
            )
            continue
        prob_by_month[month] = prob

    anchors: list[PublicMarketCdfAnchor] = []
    last_kept_prob = 0.0
    for month in sorted(prob_by_month):
        prob = prob_by_month[month]
        if prob < last_kept_prob:
            logger.info(
                "dropping non-monotone anchor at month %d: prob %.4f < last kept %.4f", month, prob, last_kept_prob
            )
            continue
        anchors.append(PublicMarketCdfAnchor(month=month, cumulative_probability=prob))
        last_kept_prob = prob
    return tuple(anchors)


def _render_anchors_yaml(anchors: tuple[PublicMarketCdfAnchor, ...]) -> str:
    body = yaml.safe_dump(
        {"public_market_cdf_anchors": [anchor.model_dump() for anchor in anchors]},
        sort_keys=False,
        default_flow_style=False,
    )
    return (
        "# Empirical going-public CDF anchors derived from live Manifold prices.\n"
        "# Remember to set `annual_public_market_probability` to the desired flat TAIL\n"
        "# hazard applied past the last anchor month.\n"
        f"{body}"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path, help="Path to a MarketCatalog YAML.")
    args = parser.parse_args(argv)

    catalog = MarketCatalog.from_yaml(args.catalog)
    client = ManifoldClient()
    try:
        anchors = derive_public_market_anchors(catalog, price_client=client)
    finally:
        client.close()
    print(_render_anchors_yaml(anchors))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
