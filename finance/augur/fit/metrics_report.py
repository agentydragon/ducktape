"""Score the active exogenous models on the same metric battery.

Loads historical exogenous-factor data, builds each model from the active
list (`_ACTIVE_MODEL_METRIC_SPECS`), runs held-out + rolling-origin +
multi-step predictive log-density + CRPS, writes a `summary.json`.

Use as a yardstick for "did the change to model X regress its score?".
Currently included:

  - **VECM** (NumPyro, joint cointegrated): fits per-origin in
    rolling-origin scoring.
  - **Independent** (YAML-configured per-series GBM): no fit step;
    same testdata config the dev fixture uses.

A model that doesn't satisfy `Fittable` is skipped from rolling-origin
scoring but still appears in held-out and multi-step rows (those don't
refit). A model that returns `None` from `predictive(...)` shows up as
an `Unscored*` row instead of crashing.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from finance.augur.api.config import load_augur_config
from finance.augur.fit.data import load_historical
from finance.augur.fit.metrics import (
    held_out_predictive_score,
    multi_step_predictive_score,
    rolling_origin_predictive_score,
)
from finance.augur.fit.model import FittableScorable, Scorable
from finance.augur.model.independent import IndependentProviderConfig
from finance.augur.model.path_models.scenarios import HistoricalSeries
from finance.augur.model.provider_config import CompositeProviderConfig
from finance.augur.model.vecm import VecmConfig, VecmModel
from util.bazel.runfiles import get_required_path


@dataclass(frozen=True)
class ModelMetricSpec:
    label: str
    build_scorable: Callable[[], Scorable]
    build_fittable_scorable: Callable[[], FittableScorable] | None = None
    rolling_origin_refit_every: int = 1


def _build_independent_from_testdata() -> Scorable:
    augur_config = load_augur_config(get_required_path("_main/finance/augur/api/testdata/config.yaml"))
    provider = augur_config.models[augur_config.default_model_id]
    if isinstance(provider, CompositeProviderConfig):
        config = provider.macro
        if not isinstance(config, IndependentProviderConfig):
            raise TypeError("public fixture composite macro provider must be independent for metric scoring")
    elif isinstance(provider, IndependentProviderConfig):
        config = provider
    else:
        raise TypeError("public fixture default preset must be a composite or independent provider for metric scoring")
    return cast(Scorable, config.realize_model())


_ACTIVE_MODEL_METRIC_SPECS: tuple[ModelMetricSpec, ...] = (
    ModelMetricSpec(
        label="vecm",
        build_scorable=lambda: VecmModel(config=VecmConfig()),
        build_fittable_scorable=lambda: VecmModel(config=VecmConfig()),
        # NumPyro SVI default takes ~5min per fit @ 20k iters. refit_every=1
        # would multiply by ~190 origins → unusable. Annual refit gives a
        # tractable ~16 fits while still tracking how fit quality evolves
        # with more data.
        rolling_origin_refit_every=12,
    ),
    ModelMetricSpec(label="independent", build_scorable=_build_independent_from_testdata, build_fittable_scorable=None),
)


def evaluate_all(
    *, train_fraction: float = 0.8, rolling_min_train: int = 60, multi_step_horizons: tuple[int, ...] = (1, 6, 12)
) -> dict[str, Any]:
    historical = load_historical()
    held_out_rows: list[dict[str, Any]] = []
    rolling_rows: list[dict[str, Any]] = []
    multi_step_rows: list[dict[str, Any]] = []
    for spec in _ACTIVE_MODEL_METRIC_SPECS:
        held_out_model = spec.build_scorable()
        if spec.build_fittable_scorable is not None:
            # Fittable: train on the prefix before scoring.
            fit_train_end = max(1, round((historical.levels.shape[0] - 1) * train_fraction))
            # Mutating fit on a fresh instance avoids leaking state across calls.
            train_series = HistoricalSeries(
                factor_names=historical.factor_names,
                levels=historical.levels[: fit_train_end + 1],
                months=historical.months[: fit_train_end + 1],
            )
            fittable = spec.build_fittable_scorable()
            fittable.fit(train_series)
            held_out_model = fittable
        held_out = held_out_predictive_score(held_out_model, historical, train_fraction=train_fraction)
        held_out_rows.append({"model": spec.label, **asdict(held_out)})

        if spec.build_fittable_scorable is not None:
            rolling = rolling_origin_predictive_score(
                spec.build_fittable_scorable,
                historical,
                min_train=rolling_min_train,
                refit_every=spec.rolling_origin_refit_every,
            )
            rolling_rows.append({"model": spec.label, **asdict(rolling)})

        # Multi-step uses the (possibly fitted) held_out_model.
        multi_step = multi_step_predictive_score(
            held_out_model, historical, horizons=multi_step_horizons, train_fraction=train_fraction
        )
        multi_step_rows.append({"model": spec.label, **asdict(multi_step)})
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
    parser = argparse.ArgumentParser(description="Augur: score the active exogenous models on the metric battery.")
    parser.add_argument("--train-fraction", type=float, default=0.8)
    parser.add_argument("--rolling-min-train", type=int, default=60)
    parser.add_argument("--out", type=Path, default=None, help="optional path to write summary.json")
    args = parser.parse_args(argv)

    summary = evaluate_all(train_fraction=args.train_fraction, rolling_min_train=args.rolling_min_train)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.out is not None:
        args.out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
