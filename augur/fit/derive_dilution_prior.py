"""Binary: fit a per-rollout dilution prior from an evidence file and print a config block.

Mirrors `augur/calibration/ipo_prior.py`'s `derive_ipo_prior`: load a private-markets evidence
file (the same JSONL observation file `train_private_equity` ingests, one observation per line
carrying its own `issuer_id`, parsed with `augur.fit.private_equity.load_price_observations_jsonl`),
run the implied-shares log-linear fit for the chosen issuer, and print a paste-ready YAML block
plus a provenance comment listing the implied-share points it used.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import yaml

from augur.fit.dilution_prior import DilutionPrior, fit_dilution_prior
from augur.fit.private_equity import (
    PriceObservation,
    PrivateEquityObservation,
    ValuationObservation,
    load_price_observations_jsonl,
)


def _select_issuer_observations(
    observations: list[PrivateEquityObservation], issuer_id: str | None
) -> tuple[str, list[PriceObservation], list[ValuationObservation]]:
    """Group the flat observation list by `issuer_id`, pick one issuer (named, or the sole one),
    and split its observations into prices and valuations."""

    by_issuer: dict[str, list[PrivateEquityObservation]] = defaultdict(list)
    for observation in observations:
        by_issuer[observation.issuer_id].append(observation)
    if issuer_id is None:
        if len(by_issuer) != 1:
            raise ValueError(f"evidence file has {len(by_issuer)} issuers; pass --issuer-id to choose one")
        issuer_id = next(iter(by_issuer))
    if issuer_id not in by_issuer:
        raise ValueError(f"issuer {issuer_id!r} not found; available: {sorted(by_issuer)}")
    issuer_observations = by_issuer[issuer_id]
    prices = [obs for obs in issuer_observations if isinstance(obs, PriceObservation)]
    valuations = [obs for obs in issuer_observations if isinstance(obs, ValuationObservation)]
    return issuer_id, prices, valuations


def _format_config_block(prior: DilutionPrior, *, issuer_id: str) -> str:
    """Render the fitted dilution prior as a paste-ready YAML block + provenance comment.

    Following the M1 precedent in :func:`augur.calibration.ipo_prior._render_anchors_yaml`,
    the ``key: value`` config lines are serialized with :func:`yaml.safe_dump` (over a
    ``dict[str, float]``, never hand-spliced) so the values come out as genuine YAML floats.
    The provenance/NOTE block is a plain-text comment header wrapped around that body.
    """

    n = len(prior.implied_share_points)

    # Config values go through yaml.safe_dump so they serialize as genuine YAML floats
    # (no f-string splicing). Build the dict in emission order and disable sort_keys to
    # preserve it; round for 6-dp tidiness but keep them as floats.
    block: dict[str, float] = {
        "annual_dilution_rate": round(prior.annual_dilution_rate, 6),
        "annual_dilution_rate_log_sigma": round(prior.annual_dilution_rate_log_sigma, 6),
    }
    if prior.valuation_monthly_log_return_mu is not None:
        block["valuation_monthly_log_return_mu"] = round(prior.valuation_monthly_log_return_mu, 6)
    if prior.valuation_monthly_log_return_sigma is not None:
        block["valuation_monthly_log_return_sigma"] = round(prior.valuation_monthly_log_return_sigma, 6)
    config_yaml = yaml.safe_dump(block, sort_keys=False, default_flow_style=False)

    # Provenance is documentation, not config data, so it stays a plain-text comment.
    header = [f"# Derived dilution prior for issuer {issuer_id!r} (paste into its issuer config):"]
    footer = ["#"]
    footer.append(f"# Provenance: implied-shares log-linear fit on n={n} paired (price, valuation) points.")
    footer.append(f"#   shares0 (intercept) = {prior.shares0:.2f}; residual log-std = {prior.residual_log_std:.6f}")
    footer.append("#   implied_shares = valuation_usd / price_usd_per_share per paired date:")
    footer.extend(
        f"#     {point.date.isoformat()}  d_years={point.delta_years:.3f}  "
        f"price={point.price_usd_per_share:.4f}  valuation={point.valuation_usd:.0f}  "
        f"implied_shares={point.implied_shares:.2f}"
        for point in prior.implied_share_points
    )
    footer.append("#")
    footer.append("# NOTE (M2.2-A): point/log-linear fit. With ~5 points the log-sigma is weakly identified")
    footer.append("#   and intentionally wide. The discrete primary-round event kind (M2.2-C) and the full")
    footer.append("#   Bayesian posterior over (rate, sigma, V-drift, V-vol) via NUTS (M2.2-D) are deferred.")

    return "\n".join(header) + "\n" + config_yaml + "\n".join(footer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Path to a private-markets observations JSONL file.")
    parser.add_argument(
        "--issuer-id", default=None, help="Issuer to fit. Optional when the evidence file has exactly one issuer."
    )
    parser.add_argument(
        "--pairing-tolerance-days",
        type=int,
        default=31,
        help="Max day gap to pair a price with a near-dated valuation (default: 31).",
    )
    args = parser.parse_args(argv)

    observations = load_price_observations_jsonl(args.evidence)
    issuer_id, prices, valuations = _select_issuer_observations(observations, args.issuer_id)
    prior = fit_dilution_prior(prices, valuations, pairing_tolerance_days=args.pairing_tolerance_days)
    print(_format_config_block(prior, issuer_id=issuer_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
