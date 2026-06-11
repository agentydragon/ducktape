"""End-to-end product-path scale benchmark/smoke.

Drives the real `ProductService.metric_fan` (exogenous sampling → reduced JAX
product metrics → percentiles) at a configurable rollout count, so we can see
where time/memory go through the *actual* product entry point rather than the
synthetic sim profiler.

CI runs it tiny (defaults) as a smoke test. Scale it locally:

  bazelisk test //finance/augur/product:service_scale_test --config=nolint \\
    --test_env=AUGUR_BENCH_ROLLOUTS=10000 --test_env=AUGUR_BENCH_HORIZON=240 \\
    --test_env=AUGUR_BENCH_PROFILE=1 --test_output=all --nocache_test_results \\
    --test_timeout=900
"""

from __future__ import annotations

import cProfile
import io
import os
import pstats
import time

import pytest_bazel

from finance.augur.api.config import Config
from finance.augur.product.conftest import MakeProductService
from finance.augur.product.service import ProductService
from finance.augur.product.wire import MetricFanRequest, RolloutRequest, ScenarioKey


def test_metric_fan_scale(make_product_service: MakeProductService, augur_config: Config) -> None:
    rollouts = int(os.environ.get("AUGUR_BENCH_ROLLOUTS", "64"))
    horizon = int(os.environ.get("AUGUR_BENCH_HORIZON", str(min(60, augur_config.max_horizon_months))))
    profile = os.environ.get("AUGUR_BENCH_PROFILE") == "1"

    # `make_product_service` registers the realized model under "current_model" (see conftest),
    # so the scenario must reference that id regardless of the config's default preset name.
    model = augur_config.models[augur_config.default_model_id].realize_model()
    service: ProductService = make_product_service(model)
    scenario = ScenarioKey(
        model_id="current_model", horizon_months=horizon, monthly_spend_usd=5_000.0, spend_index="none"
    )
    request = MetricFanRequest(
        scenario=scenario, first_seed=0, rollout_count=rollouts, metric="cash_usd", percentiles=(5.0, 50.0, 95.0)
    )

    profiler = cProfile.Profile() if profile else None
    start = time.perf_counter()
    if profiler is not None:
        profiler.enable()
    response = service.metric_fan(request)
    if profiler is not None:
        profiler.disable()
    fan_elapsed = time.perf_counter() - start

    # A single-rollout detail view exercises the lazy decode path (events_log + asset_lots only).
    rollout_start = time.perf_counter()
    service.rollout(RolloutRequest(scenario=scenario, seed=0))
    rollout_elapsed = time.perf_counter() - rollout_start

    print(
        f"\n[bench] metric_fan rollouts={rollouts} horizon={horizon} "
        f"fan_wall={fan_elapsed:.3f}s rollout_detail_wall={rollout_elapsed:.3f}s "
        f"failed={response.failed_count}"
    )
    if profiler is not None:
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("tottime").print_stats(20)
        print(stream.getvalue())

    assert response.failed_count <= rollouts
    assert len(response.monthly_metric_fan["month_index"]) == (horizon + 1) * len(request.percentiles)


if __name__ == "__main__":
    pytest_bazel.main()
