"""Binary: fit a per-rollout dilution prior from an evidence file and print a config block.

Mirrors `augur/calibration/ipo_prior.py`'s `derive_ipo_prior`: load a private-markets evidence
file (the same `{"issuers": {issuer_id: [observations]}}` shape `train_private_equity` ingests,
parsed with `augur.fit.private_equity`'s loader), run the implied-shares log-linear fit for the
chosen issuer, and print a paste-ready YAML block plus a provenance comment listing the
implied-share points it used.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from augur.fit.dilution_prior import DilutionPrior, fit_dilution_prior
from augur.fit.private_equity import PriceObservation, ValuationObservation, _load_evidence_payload, _parse_observations


def _select_issuer_observations(
    by_issuer: dict[str, list], issuer_id: str | None
) -> tuple[str, list[PriceObservation], list[ValuationObservation]]:
    """Pick the issuer to fit (named, or the sole issuer) and split its observations by kind."""

    if not by_issuer:
        raise ValueError("evidence file contains no issuers")
    if issuer_id is None:
        if len(by_issuer) != 1:
            raise ValueError(f"evidence file has {len(by_issuer)} issuers; pass --issuer-id to choose one")
        issuer_id = next(iter(by_issuer))
    if issuer_id not in by_issuer:
        raise ValueError(f"issuer {issuer_id!r} not found; available: {sorted(by_issuer)}")
    observations = by_issuer[issuer_id]
    prices = [obs for obs in observations if isinstance(obs, PriceObservation)]
    valuations = [obs for obs in observations if isinstance(obs, ValuationObservation)]
    return issuer_id, prices, valuations


def _format_config_block(prior: DilutionPrior, *, issuer_id: str) -> str:
    """Render the fitted dilution prior as a paste-ready YAML block + provenance comment."""

    n = len(prior.implied_share_points)
    lines = [
        f"# Derived dilution prior for issuer {issuer_id!r} (paste into its issuer config):",
        f"annual_dilution_rate: {prior.annual_dilution_rate:.6f}",
        f"annual_dilution_rate_log_sigma: {prior.annual_dilution_rate_log_sigma:.6f}",
    ]
    if prior.valuation_monthly_log_return_mu is not None:
        lines.append(f"valuation_monthly_log_return_mu: {prior.valuation_monthly_log_return_mu:.6f}")
    if prior.valuation_monthly_log_return_sigma is not None:
        lines.append(f"valuation_monthly_log_return_sigma: {prior.valuation_monthly_log_return_sigma:.6f}")
    lines.append("#")
    lines.append(f"# Provenance: implied-shares log-linear fit on n={n} paired (price, valuation) points.")
    lines.append(f"#   shares0 (intercept) = {prior.shares0:.2f}; residual log-std = {prior.residual_log_std:.6f}")
    lines.append("#   implied_shares = valuation_usd / price_usd_per_share per paired date:")
    lines.extend(
        f"#     {point.date.isoformat()}  d_years={point.delta_years:.3f}  "
        f"price={point.price_usd_per_share:.4f}  valuation={point.valuation_usd:.0f}  "
        f"implied_shares={point.implied_shares:.2f}"
        for point in prior.implied_share_points
    )
    lines.append("#")
    lines.append("# NOTE (M2.2-A): point/log-linear fit. With ~5 points the log-sigma is weakly identified")
    lines.append("#   and intentionally wide. The discrete primary-round event kind (M2.2-C) and the full")
    lines.append("#   Bayesian posterior over (rate, sigma, V-drift, V-vol) via NUTS (M2.2-D) are deferred.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Path to a private-markets evidence file (JSON/YAML).")
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

    by_issuer = _parse_observations(_load_evidence_payload(args.evidence))
    issuer_id, prices, valuations = _select_issuer_observations(by_issuer, args.issuer_id)
    prior = fit_dilution_prior(prices, valuations, pairing_tolerance_days=args.pairing_tolerance_days)
    print(_format_config_block(prior, issuer_id=issuer_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
