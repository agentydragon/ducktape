"""Profile one product metric-fan request through the Augur API backend."""

from __future__ import annotations

import argparse
import cProfile
import pstats
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import cast, get_args

from finance.augur.api.catalog import build_catalog
from finance.augur.api.config import Config, load_augur_config
from finance.augur.api.portfolio_sources import resolve_portfolio_sources
from finance.augur.model.exogenous import Sampler
from finance.augur.product.scenarios import resolve_primary_agent_id, sim_locations_from_config
from finance.augur.product.service import ProductService
from finance.augur.product.wire import MetricName, ProjectionSamplingRequest, ScenarioKey
from util.bazel.runfiles import get_required_path

DEFAULT_CONFIG_RUNFILE = "_main/finance/augur/api/testdata/config.yaml"
DEFAULT_HORIZON_MONTHS = 100
DEFAULT_ROLLOUT_COUNT = 50
DEFAULT_MONTHLY_SPEND_USD = 7000.0
DEFAULT_PROFILE_OUTPUT = Path("/tmp/augur_metric_fan.prof")
DEFAULT_PERCENTILES = (1.0, 5.0, 25.0, 50.0, 75.0, 95.0, 99.0)


class ProfileTimeoutError(TimeoutError):
    pass


def main() -> int:
    args = _arg_parser().parse_args()
    config = load_augur_config(_config_path(args.config))
    resolved_portfolio = resolve_portfolio_sources(config)
    catalog = build_catalog(config)
    service = ProductService(
        portfolio=resolved_portfolio.portfolio,
        initial_cash=resolved_portfolio.snapshot.cash,
        primary_agent_id=resolve_primary_agent_id(config),
        security_distributions=config.security_distributions,
        known_location_ids=catalog.location_ids,
        locations=sim_locations_from_config(config.locations),
        properties_by_id=catalog.properties_by_id,
        models=_profile_models(config),
        max_rollout_samples=config.max_rollout_samples,
        max_horizon_months=config.max_horizon_months,
    )
    request = ProjectionSamplingRequest(
        scenario=ScenarioKey(
            model_id=config.default_model_id,
            horizon_months=args.horizon_months,
            monthly_spend=args.monthly_spend,
            spend_index=args.spend_index,
        ),
        first_seed=0,
        rollout_count=args.rollout_count,
        metric=args.metric,
        percentiles=tuple(args.percentiles),
    )

    profiler = cProfile.Profile()
    start = time.perf_counter()
    try:
        with _time_bound(args.max_seconds):
            response = profiler.runcall(service.metric_fan, request)
    except ProfileTimeoutError as exc:
        elapsed = time.perf_counter() - start
        args.profile_output.parent.mkdir(parents=True, exist_ok=True)
        profiler.dump_stats(args.profile_output)
        raise SystemExit(f"{exc}; partial profile written to {args.profile_output}; wall_clock_sec={elapsed:.3f}")
    elapsed = time.perf_counter() - start

    args.profile_output.parent.mkdir(parents=True, exist_ok=True)
    profiler.dump_stats(args.profile_output)

    print(f"wall_clock_sec: {elapsed:.3f}")
    print(f"profile_output: {args.profile_output}")
    print(f"horizon_months: {args.horizon_months}")
    print(f"rollout_count: {args.rollout_count}")
    print(f"metric: {args.metric}")
    print(f"percentiles: {','.join(str(percentile) for percentile in args.percentiles)}")
    print(f"monthly_metric_fan_rows: {len(response.monthly_metric_fan['month_index'])}")
    print(f"terminal_metric_percentile_rows: {len(response.terminal_metric_percentiles['percentile'])}")
    print(f"failed_count: {response.failed_count}")
    pstats.Stats(profiler).strip_dirs().sort_stats(pstats.SortKey.CUMULATIVE).print_stats(args.top)
    return 0


def _arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Profile one Augur product metric-fan request.")
    parser.add_argument(
        "--config", help=f"Path to Augur config YAML. Defaults to Bazel runfile {DEFAULT_CONFIG_RUNFILE}."
    )
    parser.add_argument("--horizon-months", type=int, default=DEFAULT_HORIZON_MONTHS)
    parser.add_argument("--rollout-count", type=int, default=DEFAULT_ROLLOUT_COUNT)
    parser.add_argument("--monthly-spend-usd", type=float, default=DEFAULT_MONTHLY_SPEND_USD)
    parser.add_argument("--spend-index", choices=["none", "inflation"], default="inflation")
    parser.add_argument("--metric", choices=get_args(MetricName), default="liquid_net_worth")
    parser.add_argument("--percentiles", type=float, nargs="+", default=list(DEFAULT_PERCENTILES))
    parser.add_argument("--profile-output", type=Path, default=DEFAULT_PROFILE_OUTPUT)
    parser.add_argument("--max-seconds", type=float, default=60.0)
    parser.add_argument("--top", type=int, default=40, help="Number of cumulative cProfile rows to print.")
    return parser


def _config_path(config: str | None) -> Path:
    return Path(config) if config is not None else get_required_path(DEFAULT_CONFIG_RUNFILE)


def _profile_models(config: Config) -> dict[str, Sampler]:
    return {preset_id: cast(Sampler, provider.realize_model()) for preset_id, provider in config.models.items()}


@contextmanager
def _time_bound(max_seconds: float) -> Iterator[None]:
    if max_seconds <= 0:
        raise ValueError("--max-seconds must be positive")
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, max_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


def _timeout(_signum: int, _frame: FrameType | None) -> None:
    raise ProfileTimeoutError("profile metric-fan request exceeded --max-seconds")


if __name__ == "__main__":
    raise SystemExit(main())
