"""Run calibration against live prediction markets for a deployment config + report.

Usage:

    bb run //augur/calibration:calibration_report -- \\
        /path/to/augur/config.yaml \\
        [--preset bayesian_mint_streams] [--rollouts 5000] [--horizon 120]

Loads the catalog and presets from the deployment config, samples the chosen preset model,
runs `run_calibration` against live market prices, and prints scored markets sorted by
KL divergence (loudest disagreement first).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tabulate import tabulate

from augur.api.config import load_augur_config
from augur.calibration.calibration import mark_fan, run_calibration
from augur.calibration.catalog import MarketCatalog
from augur.calibration.kalshi import KalshiClient
from augur.calibration.manifold import ManifoldClient
from augur.calibration.platform import Platform, PriceClient
from augur.calibration.polymarket import PolymarketClient
from augur.model.exogenous import ExogenousSamplingRequest
from augur.model.private_equity_bundle import PrivateEquityFloatChannel
from augur.model.series import IssuerId


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run calibration against live prediction markets for a config.")
    parser.add_argument("config", type=Path, help="Path to augur config.yaml")
    parser.add_argument("--preset", default=None, help="Preset id (default = config default)")
    parser.add_argument("--rollouts", type=int, default=5000)
    parser.add_argument("--horizon", type=int, default=120)
    args = parser.parse_args(argv)

    augur_config = load_augur_config(args.config)
    preset_id = args.preset or augur_config.default_model_id
    print(f"loaded config: presets={list(augur_config.models)}, default={augur_config.default_model_id}")
    print(f"running preset: {preset_id}")

    catalog_config = augur_config.calibration_catalog
    if catalog_config is None:
        print("error: no calibration_catalog configured", file=sys.stderr)
        return 2
    catalog = MarketCatalog.from_yaml(catalog_config.catalog_path)
    issuer = catalog_config.issuer
    print(f"catalog: issuer={issuer}, n_markets={len(catalog.markets)}")

    provider = augur_config.models[preset_id]
    model = provider.realize_model()
    sampling = ExogenousSamplingRequest(
        horizon_months=args.horizon,
        rollout_seeds=tuple(range(1, args.rollouts + 1)),
        required_private_equity_issuers=frozenset({IssuerId(issuer)}),
    )
    sampled = model.sample(sampling)
    bundle = sampled.private_equity

    price_clients: dict[Platform, PriceClient] = {
        Platform.MANIFOLD: ManifoldClient(),
        Platform.POLYMARKET: PolymarketClient(),
        Platform.KALSHI: KalshiClient(),
    }
    try:
        result = run_calibration(
            model,
            catalog,
            issuer=issuer,
            horizon_months=args.horizon,
            rollout_seeds=sampling.rollout_seeds,
            price_clients=price_clients,
            bundle=bundle,
        )
    finally:
        for client in price_clients.values():
            client.close()

    mark_pct = mark_fan(
        bundle, issuer=issuer, rollout_count=args.rollouts, horizon_months=args.horizon, percentiles=(5.0, 50.0, 95.0)
    )
    val_pct = mark_fan(
        bundle,
        issuer=issuer,
        rollout_count=args.rollouts,
        horizon_months=args.horizon,
        percentiles=(5.0, 50.0, 95.0),
        channel=PrivateEquityFloatChannel.COMPANY_VALUATION_USD,
    )

    clean_rows = sorted(result.clean, key=lambda r: -abs(r.kl_bits) if r.kl_bits is not None else 0)
    clean_table = [
        [
            f"{r.p_model:.3f}" if r.p_model is not None else "n/a",
            f"{r.p_market:.3f}",
            f"{r.kl_bits:+.3f}" if r.kl_bits is not None else "n/a",
            r.platform,
            r.market_id,
            r.question[:60],
        ]
        for r in clean_rows
    ]
    print("\nSCORED MARKETS (sorted by |KL|, loudest first)")
    print(tabulate(clean_table, headers=["p_model", "p_market", "KL_bits", "platform", "market_id", "question"]))

    surfaced_table = [[f"{r.p_market:.3f}", r.platform, r.market_id, r.question[:60]] for r in result.surfaced]
    print("\nSURFACED MARKETS (not scored, context only)")
    print(tabulate(surfaced_table, headers=["p_market", "platform", "market_id", "question"]))

    fan_months = [0, 6, 12, 24, 60, 120]
    mark_rows = []
    for m in fan_months:
        month = next((b for b in mark_pct.months if b.month_index == m), None)
        if month is None:
            continue
        mark_rows.append(
            [
                m,
                f"${month.values.get('5.0', 0):.0f}",
                f"${month.values.get('50.0', 0):.0f}",
                f"${month.values.get('95.0', 0):.0f}",
            ]
        )
    print("\nPER-UNIT MARK FAN (p5 / p50 / p95)")
    print(tabulate(mark_rows, headers=["month", "p5", "p50", "p95"]))

    val_rows = []
    for m in fan_months:
        month = next((b for b in val_pct.months if b.month_index == m), None)
        if month is None:
            continue
        val_rows.append(
            [
                m,
                f"${month.values.get('5.0', 0) / 1e9:.0f}B",
                f"${month.values.get('50.0', 0) / 1e9:.0f}B",
                f"${month.values.get('95.0', 0) / 1e9:.0f}B",
            ]
        )
    print("\nCOMPANY VALUATION FAN (p5 / p50 / p95)")
    print(tabulate(val_rows, headers=["month", "p5", "p50", "p95"]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
