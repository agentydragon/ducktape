"""Offline market-model training entry point.

Reads a `MarketConfig` + source CSVs, fits a chosen `MarketModel`, and
writes two files: a `MarketProviderConfig` YAML (the discriminated
deployment config that the augur server reads at startup as part of
`AugurConfig.market_provider`) and a per-model trained-state blob (e.g. an
`.npz` archive). The manifest YAML's `trained_blob` is an absolute path so
the deployment authoring it knows exactly where the blob will live at
runtime.

Usage:

    bb run //augur/fit:train -- \\
        --market-config augur/model/train/config/market_config.example.json \\
        --model vecm \\
        --out-provider-config /path/to/market_provider.yaml \\
        --out-blob /path/to/trained_vecm.npz
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from augur.fit.data import load_evidence
from augur.fit.market_config import load_market_config
from augur.model.market_provider_config import VecmMarketProviderConfig
from augur.model.markets.models.vecm import VecmConfig, VecmModel

_SUPPORTED_MODEL_LABELS = ("vecm",)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train an Augur market model offline.")
    parser.add_argument(
        "--market-config",
        required=True,
        type=Path,
        help="Path to MarketConfig JSON/YAML (typically augur/model/train/config/market_config.example.json).",
    )
    parser.add_argument("--model", required=True, choices=_SUPPORTED_MODEL_LABELS, help="Which market model to train.")
    parser.add_argument(
        "--out-provider-config",
        required=True,
        type=Path,
        help="Absolute path the MarketProviderConfig YAML will be written to.",
    )
    parser.add_argument(
        "--out-blob",
        required=True,
        type=Path,
        help="Absolute path the per-model trained state blob will be written to. Echoed into the config verbatim.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    market_config_path = args.market_config.resolve()
    out_provider_config = args.out_provider_config.resolve()
    out_blob = args.out_blob.resolve()

    config = load_market_config(market_config_path)
    historical, evidence = load_evidence(config, market_config_path.parent)

    model = VecmModel(VecmConfig(k_ar_diff=1, coint_rank=1))
    model.fit(historical)

    provider_config = VecmMarketProviderConfig(
        trained_blob=out_blob,
        latest_observations=dict(evidence.latest_observations),
        current_mortgage30_rate_pct=float(evidence.current_mortgage30_rate_pct),
        location_market_sources=config.location_market_sources,
    )
    model.save(provider_config)

    out_provider_config.write_text(
        yaml.safe_dump(provider_config.model_dump(mode="json"), sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"wrote provider config: {out_provider_config}")
    print(f"wrote trained blob:    {out_blob}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
