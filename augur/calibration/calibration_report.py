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
from augur.calibration.calibration import build_anchored_level_paths, mark_fan, run_calibration
from augur.calibration.catalog import MarketCatalog
from augur.calibration.default_clients import build_default_price_clients
from augur.calibration.macro_anchors import resolve_anchors
from augur.model.exogenous import ExogenousSamplingRequest, level_series_request_channels
from augur.model.private_equity_bundle import PrivateEquityFloatChannel
from augur.model.series import IssuerId, parse_level_series_key


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
    catalog = MarketCatalog.from_yaml(catalog_config.catalog_path)

    provider = augur_config.models[preset_id]
    model = provider.realize_model()
    emit_issuers = sorted(
        IssuerId(issuer)
        for issuer in catalog.referenced_issuers()
        if IssuerId(issuer) in model.emittable_private_equity_issuers()
    )
    catalog_level = {parse_level_series_key(wire) for wire in catalog.referenced_level_series()}
    wanted_level = catalog_level & model.emittable_level_keys()
    print(f"catalog: issuers={[str(i) for i in emit_issuers]}, n_markets={len(catalog.markets)}")
    sampling = ExogenousSamplingRequest(
        horizon_months=args.horizon,
        rollout_seeds=tuple(range(1, args.rollouts + 1)),
        required_private_equity_issuers=frozenset(emit_issuers),
        **level_series_request_channels(wanted_level),
    )
    sampled = model.sample(sampling)
    bundle = sampled.private_equity
    anchors = resolve_anchors(catalog)
    level_paths = build_anchored_level_paths(
        sampled,
        anchors=anchors.anchors,
        requested_wire_ids=catalog.referenced_level_series(),
        rollout_count=args.rollouts,
        horizon_months=args.horizon,
    )

    price_clients = build_default_price_clients()
    try:
        result = run_calibration(
            catalog,
            horizon_months=args.horizon,
            rollout_seeds=sampling.rollout_seeds,
            price_clients=price_clients,
            bundle=bundle,
            level_paths=level_paths,
            inflation_history=anchors.inflation_history,
        )
    finally:
        for client in price_clients.values():
            client.close()

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

    surfaced_table = [
        [f"{r.p_market:.3f}", r.platform, r.mappability, r.market_id, r.question[:60]] for r in result.surfaced
    ]
    print("\nSURFACED MARKETS (not scored, context only)")
    print(tabulate(surfaced_table, headers=["p_market", "platform", "mappability", "market_id", "question"]))

    for fam in result.categorical:
        kl = f"{fam.kl_bits:+.4f} bits" if fam.kl_bits is not None else "n/a"
        print(f"\nCATEGORICAL {fam.family_id} [{fam.platform}] {fam.channel} @ {fam.at_date}  multinomial KL={kl}")
        bucket_rows = [
            [b.label, f"{b.p_market:.3f}", f"{b.p_model:.3f}" if b.p_model is not None else "n/a"] for b in fam.buckets
        ]
        print(tabulate(bucket_rows, headers=["bucket", "p_market", "p_model"]))

    fan_months = [0, 6, 12, 24, 60, 120]
    for issuer in emit_issuers:
        mark_pct = mark_fan(
            bundle,
            issuer=issuer,
            rollout_count=args.rollouts,
            horizon_months=args.horizon,
            percentiles=(5.0, 50.0, 95.0),
        )
        mark_rows = [
            [m, f"${b.values['5.0']:.0f}", f"${b.values['50.0']:.0f}", f"${b.values['95.0']:.0f}"]
            for m in fan_months
            if (b := next((b for b in mark_pct.months if b.month_index == m), None)) is not None
        ]
        print(f"\nPER-UNIT MARK FAN [{issuer}] (p5 / p50 / p95)")
        print(tabulate(mark_rows, headers=["month", "p5", "p50", "p95"]))

        val_pct = mark_fan(
            bundle,
            issuer=issuer,
            rollout_count=args.rollouts,
            horizon_months=args.horizon,
            percentiles=(5.0, 50.0, 95.0),
            channel=PrivateEquityFloatChannel.COMPANY_VALUATION_USD,
        )
        val_rows = [
            [m, f"${b.values['5.0'] / 1e9:.0f}B", f"${b.values['50.0'] / 1e9:.0f}B", f"${b.values['95.0'] / 1e9:.0f}B"]
            for m in fan_months
            if (b := next((b for b in val_pct.months if b.month_index == m), None)) is not None
        ]
        print(f"\nCOMPANY VALUATION FAN [{issuer}] (p5 / p50 / p95)")
        print(tabulate(val_rows, headers=["month", "p5", "p50", "p95"]))

    return 0


if __name__ == "__main__":
    sys.exit(main())
