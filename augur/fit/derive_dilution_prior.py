"""Binary: fit a per-rollout dilution prior from an evidence file and print a config block.

Mirrors `augur/calibration/ipo_prior.py`'s `derive_ipo_prior`: load an `EvidenceConfig` YAML
(per-issuer paired per-share `price_observations` + company post-money `valuation_observations`),
run the implied-shares log-linear fit for the chosen issuer, and print a paste-ready YAML block
plus a provenance comment listing the implied-share points it used.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from more_itertools import one

from augur.fit.dilution_prior import DilutionPrior, fit_dilution_prior
from augur.fit.evidence_config import EvidenceConfig, IssuerEvidence


def _select_issuer(config: EvidenceConfig, issuer_id: str | None) -> IssuerEvidence:
    """Pick the issuer to fit: the named one, or the sole issuer when there is exactly one."""

    if issuer_id is not None:
        return one(issuer for issuer in config.issuers if issuer.issuer_id == issuer_id)
    return one(
        config.issuers, too_long=ValueError("evidence file has multiple issuers; pass --issuer-id to choose one")
    )


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
    lines.append("#   implied_shares = valuation_usd / price_usd per paired date:")
    for point in prior.implied_share_points:
        lines.append(
            f"#     {point.date.isoformat()}  d_years={point.delta_years:.3f}  "
            f"price={point.price_usd:.4f}  valuation={point.valuation_usd:.0f}  "
            f"implied_shares={point.implied_shares:.2f}"
        )
    lines.append("#")
    lines.append("# NOTE (M2.2-A): point/log-linear fit. With ~5 points the log-sigma is weakly identified")
    lines.append("#   and intentionally wide. The discrete primary-round event kind (M2.2-C) and the full")
    lines.append("#   Bayesian posterior over (rate, sigma, V-drift, V-vol) via NUTS (M2.2-D) are deferred.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="Path to an EvidenceConfig YAML file.")
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

    config = EvidenceConfig.from_yaml_file(args.evidence)
    issuer = _select_issuer(config, args.issuer_id)
    prior = fit_dilution_prior(
        list(issuer.price_observations),
        list(issuer.valuation_observations),
        pairing_tolerance_days=args.pairing_tolerance_days,
    )
    print(_format_config_block(prior, issuer_id=issuer.issuer_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
