"""Score the active trained market model on the same metric battery.

Loads historical market-factor data, fits each model from
the active model list, runs held-out + rolling-origin + multi-step predictive
log-density, writes a `summary.json`.

Use as a yardstick for "did the change to model X regress its score?"
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from augur.fit.data import load_historical
from augur.fit.market_model import MarketModel
from augur.fit.metrics import (
    held_out_predictive_log_density,
    multi_step_predictive_log_density,
    rolling_origin_predictive_log_density,
)
from augur.model.markets.models.vecm import VecmConfig, VecmModel


@dataclass(frozen=True)
class ModelMetricSpec:
    build: Callable[[], MarketModel]
    rolling_origin_refit_every: int = 1


_ACTIVE_MODEL_METRIC_SPECS: tuple[ModelMetricSpec, ...] = (
    ModelMetricSpec(build=lambda: VecmModel(VecmConfig(k_ar_diff=1, coint_rank=1))),
)


def evaluate_all(
    *,
    train_fraction: float = 0.8,
    rolling_min_train: int = 60,
    multi_step_horizons: tuple[int, ...] = (1, 6, 12),
    config_path: Path | None = None,
) -> dict[str, Any]:
    historical = load_historical(config_path)
    held_out_rows: list[dict[str, Any]] = []
    rolling_rows: list[dict[str, Any]] = []
    multi_step_rows: list[dict[str, Any]] = []
    for spec in _ACTIVE_MODEL_METRIC_SPECS:
        held_out = held_out_predictive_log_density(spec.build(), historical, train_fraction=train_fraction)
        held_out_rows.append(asdict(held_out))
        rolling = rolling_origin_predictive_log_density(
            spec.build, historical, min_train=rolling_min_train, refit_every=spec.rolling_origin_refit_every
        )
        rolling_rows.append(asdict(rolling))
        multi_step = multi_step_predictive_log_density(
            spec.build(), historical, horizons=multi_step_horizons, train_fraction=train_fraction
        )
        multi_step_rows.append(asdict(multi_step))
    return {
        "factor_names": list(historical.factor_names),
        "n_steps": historical.levels.shape[0] - 1,
        "first_month": historical.months[0],
        "last_month": historical.months[-1],
        "train_fraction": train_fraction,
        "rolling_min_train": rolling_min_train,
        "multi_step_horizons": list(multi_step_horizons),
        "held_out_split": held_out_rows,
        "rolling_origin": rolling_rows,
        "multi_step": multi_step_rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Augur: score the active trained market model on the metric battery.")
    parser.add_argument(
        "--config", type=Path, default=None, help="path to market_config.example.json (default: bundled)"
    )
    parser.add_argument("--train-fraction", type=float, default=0.8)
    parser.add_argument("--rolling-min-train", type=int, default=60)
    parser.add_argument("--out", type=Path, default=None, help="optional path to write summary.json")
    args = parser.parse_args(argv)

    summary = evaluate_all(
        train_fraction=args.train_fraction, rolling_min_train=args.rolling_min_train, config_path=args.config
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.out is not None:
        args.out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
