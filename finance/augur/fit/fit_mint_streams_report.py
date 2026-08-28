"""Run the mint-streams NUTS fit on a JSONL of observations and print a report.

Usage (via Bazel):

    bb run //finance/augur/fit:fit_mint_streams_report -- \\
        path/to/observations.jsonl --issuer openai \\
        [--num-warmup 1500] [--num-samples 3000] [--num-chains 2] [--seed 0]

Reads observations from the JSONL (one record per line), filters to the given issuer,
runs the fit, prints a paste-ready preset block for `bayesian_mint_streams`.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from finance.augur.fit.bayes_mint_streams import BayesianMintStreamsPosterior, fit_bayesian_mint_streams_prior
from finance.augur.fit.private_equity import (
    PriceObservation,
    PrivateEquityObservation,
    ValuationObservation,
    load_price_observations_jsonl,
)


def _filter_by_issuer(
    observations: list[PrivateEquityObservation], issuer_id: str
) -> tuple[list[PriceObservation], list[ValuationObservation]]:
    prices = [o for o in observations if isinstance(o, PriceObservation) and o.issuer_id == issuer_id]
    valuations = [o for o in observations if isinstance(o, ValuationObservation) and o.issuer_id == issuer_id]
    return prices, valuations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the mint-streams NUTS fit and print the posterior.")
    parser.add_argument("observations", type=Path, help="Path to observations.jsonl")
    parser.add_argument("--issuer", required=True, help="Issuer ID to fit (e.g. 'openai')")
    parser.add_argument("--num-warmup", type=int, default=1500)
    parser.add_argument("--num-samples", type=int, default=3000)
    parser.add_argument("--num-chains", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    observations = load_price_observations_jsonl(args.observations)
    prices, valuations = _filter_by_issuer(observations, args.issuer)
    print(f"loaded {len(prices)} prices and {len(valuations)} valuations for issuer={args.issuer}")

    primaries = [v for v in valuations if v.valuation_kind == "primary"]
    secondaries = [v for v in valuations if v.valuation_kind != "primary"]
    print(f"  primary events:    {len(primaries)}")
    print(f"  non-primary vals:  {len(secondaries)}")

    posterior = fit_bayesian_mint_streams_prior(
        prices,
        valuations,
        num_warmup=args.num_warmup,
        num_samples=args.num_samples,
        num_chains=args.num_chains,
        seed=args.seed,
    )

    print("\n=== Posterior summary ===")
    print(f"  observation_window_months:           {posterior.observation_window_months:.1f}")
    print(
        f"  n_primary / n_secondary / n_price:   {posterior.n_primary_events} / "
        f"{posterior.n_secondary_observations} / {posterior.n_price_observations}"
    )
    print(f"  num_divergences:                     {posterior.num_divergences}")
    print()
    print(
        f"  monthly_hazard (closed-form Gamma):  {posterior.monthly_hazard:.4f} = 1 / "
        f"{1 / posterior.monthly_hazard:.1f} months"
    )
    print(
        f"  monthly_hazard posterior Gamma:      alpha={posterior.monthly_hazard_posterior_alpha:.2f}, "
        f"beta={posterior.monthly_hazard_posterior_beta:.2f}"
    )
    print(f"  cash_over_v_pre_median:              {posterior.cash_over_v_pre_median:.4f}")
    print(f"  cash_over_v_pre_log_sigma:           {posterior.cash_over_v_pre_log_sigma:.4f}")
    print(
        f"  annual_mint_rate_mature:             {posterior.annual_mint_rate_mature:.4f} = "
        f"{posterior.annual_mint_rate_mature * 100:.1f}%/yr"
    )
    print(f"  annual_mint_rate_log_sigma:          {posterior.annual_mint_rate_log_sigma:.4f}")
    print()
    print("  V-drift (FIXED-shape, prior centers):")
    print(f"    valuation_monthly_log_return_mu (mu_mature):   {posterior.valuation_monthly_log_return_mu:.6f}")
    print(f"    valuation_drift_mu_young:                       {posterior.valuation_drift_mu_young:.6f}")
    print(
        f"    valuation_drift_log_value_onset_usd:            {posterior.valuation_drift_log_value_onset_usd:.6f} "
        f"(${math.exp(posterior.valuation_drift_log_value_onset_usd) / 1e9:.0f}B)"
    )
    print(f"    valuation_drift_log_value_scale:                {posterior.valuation_drift_log_value_scale:.6f}")
    print(f"    valuation_monthly_log_return_sigma:             {posterior.valuation_monthly_log_return_sigma:.6f}")
    print()
    print(f"  shares0:                             {posterior.shares0:.3e}")
    print(f"  V0:                                  ${posterior.v0_usd:.3e}")
    print()
    print("=== Paste-ready preset block ===")
    print(_paste_ready_yaml(posterior, issuer_id=args.issuer))
    return 0


def _paste_ready_yaml(posterior: BayesianMintStreamsPosterior, *, issuer_id: str) -> str:
    """Emit the fitted parameters as a YAML snippet for `bayesian_mint_streams.private_equity.issuers.<id>`.

    Note that `current_mark_usd`, `current_valuation_usd`, and `shares_outstanding_initial`
    come from the deployment config (cap-table anchors), not from the fit. We DO emit the
    fitted shares0/V0 below the snippet as a sanity check.
    """

    p = posterior
    return f"""        {issuer_id}:
          # ... unchanged static fields (current_mark_usd, current_valuation_usd,
          # shares_outstanding_initial, tender / collapse / legal / IPO params) ...
          # === FROM mint-streams NUTS posterior ===
          valuation_monthly_log_return_mu: {p.valuation_monthly_log_return_mu:.6f}  # mu_mature
          valuation_monthly_log_return_sigma: {p.valuation_monthly_log_return_sigma:.6f}
          valuation_drift_scale_reversion:
            monthly_log_return_mu_young: {p.valuation_drift_mu_young:.6f}
            log_value_onset_usd: {p.valuation_drift_log_value_onset_usd:.6f}  # ${math.exp(p.valuation_drift_log_value_onset_usd) / 1e9:.0f}B
            log_value_scale: {p.valuation_drift_log_value_scale:.6f}
          primary_round_config:
            monthly_hazard: {p.monthly_hazard:.4f}  # 1 / {1 / p.monthly_hazard:.1f} months (Gamma-Poisson posterior mean)
            cash_over_v_pre_median: {p.cash_over_v_pre_median:.4f}
            cash_over_v_pre_log_sigma: {p.cash_over_v_pre_log_sigma:.4f}
            step_up_median: 1.0
            step_up_log_sigma: 0.0
            ipo_anticipation_decay: true
            # monthly_hazard_scale_reversion omitted — the fit doesn't currently identify it.
          employee_mint_config:
            annual_mint_rate_mature: {p.annual_mint_rate_mature:.4f}
            annual_mint_rate_log_sigma: {p.annual_mint_rate_log_sigma:.4f}
          # Fit diagnostics:
          #   n_primary_events:    {p.n_primary_events}
          #   n_secondary_obs:     {p.n_secondary_observations}
          #   n_price_obs:         {p.n_price_observations}
          #   observation_window:  {p.observation_window_months:.1f} months
          #   num_divergences:     {p.num_divergences}
          #   shares0 (fitted):    {p.shares0:.3e}  [sanity check vs shares_outstanding_initial]
          #   V0 (fitted):         ${p.v0_usd:.3e}  [sanity check vs current_valuation_usd]"""


if __name__ == "__main__":
    sys.exit(main())
