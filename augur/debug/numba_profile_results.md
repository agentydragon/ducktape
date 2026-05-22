# Numba Simulator Profile Results

Captured on May 22, 2026 in the `codex/augur-numba-sim` worktree after rebasing
onto `2d6b9a265` (`augur: batch product metric fan projection`) while profiling
`//augur/api:profile_metric_fan`.

## Profile Target

Default request shape unless otherwise noted:

- `horizon_months`: 100
- `rollout_count`: 50
- `metric`: `liquid_net_worth_usd`
- `percentiles`: `1, 5, 25, 50, 75, 95, 99`
- profile guard: `--max-seconds=60`

## Commands

Polars baseline:

```bash
AUGUR_SIM_ENGINE=polars ./bazel-bin/augur/api/profile_metric_fan \
  --config augur/api/testdata/config.yaml \
  --profile-output=/tmp/augur_metric_fan_polars.prof --top=25
```

Numba steady-state requires a cache warmup. Without this, the target mostly
profiles LLVM/parfor compilation.

```bash
NUMBA_CACHE_DIR=/tmp/augur_numba_cache AUGUR_SIM_ENGINE=numba \
  ./bazel-bin/augur/api/profile_metric_fan \
  --config augur/api/testdata/config.yaml \
  --horizon-months=1 --rollout-count=1 \
  --profile-output=/tmp/augur_metric_fan_numba_cache_rewarm.prof \
  --max-seconds=240 --top=10

NUMBA_CACHE_DIR=/tmp/augur_numba_cache AUGUR_SIM_ENGINE=numba \
  ./bazel-bin/augur/api/profile_metric_fan \
  --config augur/api/testdata/config.yaml \
  --profile-output=/tmp/augur_metric_fan_numba_50r_cached_current.prof --top=15

NUMBA_CACHE_DIR=/tmp/augur_numba_cache AUGUR_SIM_ENGINE=numba \
  ./bazel-bin/augur/api/profile_metric_fan \
  --config augur/api/testdata/config.yaml \
  --rollout-count=500 \
  --profile-output=/tmp/augur_metric_fan_numba_500r_cached.prof --top=25
```

The binary was built with:

```bash
bazelisk build --config=rbe --config=nolint --build_runfile_links \
  --remote_download_outputs=all //augur/api:profile_metric_fan
```

## Timings

| Engine / cache state           | Rollouts | Months | Wall clock | Profile output                                              |
| ------------------------------ | -------: | -----: | ---------: | ----------------------------------------------------------- |
| Polars, after product batching |       50 |    100 |    15.999s | `/tmp/augur_metric_fan_rebased_polars_50.prof`              |
| Polars, after product batching |      500 |    100 |    14.555s | `/tmp/augur_metric_fan_rebased_polars_500.prof`             |
| Numba, cold local cache rewarm |        1 |      1 |   165.660s | `/tmp/augur_metric_fan_rebased_numba_cache_rewarm.prof`     |
| Numba, cached current kernel   |       50 |    100 |     1.142s | `/tmp/augur_metric_fan_rebased_numba_50_cached.prof`        |
| Numba, cached current kernel   |       50 |    100 |     0.849s | `/tmp/augur_metric_fan_rebased_numba_50_cached_second.prof` |
| Numba, cached current kernel   |      500 |    100 |     3.381s | `/tmp/augur_metric_fan_rebased_numba_500_cached.prof`       |

The cold local cache rewarm was still dominated by Numba compilation:
`engine.py:simulate_with_external_series_numba` took `164.355s`, with
`dispatcher.py:_compile_for_args` accounting for `164.323s`.

## 500-Rollout Hot Points

After product metric-fan batching, the 500-rollout cached Numba profile is
dominated by decoding dense Numba outputs back into Polars `SimulationRun`
frames, not by product-layer fan extraction:

| Function                                           | Cumulative time |
| -------------------------------------------------- | --------------: |
| `projection_service.py:_simulate_missing_rollouts` |          3.366s |
| `simulate.py:simulate_with_external_series`        |          3.082s |
| `engine.py:simulate_with_external_series_numba`    |          2.857s |
| `engine.py:_decode_run`                            |          2.449s |
| `engine.py:_decode_events`                         |          1.021s |
| `engine.py:_decode_tax_liabilities`                |          0.803s |

## Interpretation

The product metric-fan batching slice removed the previous per-rollout
`project_net_worth(run)` loop and event materialization from fan requests. With
that change applied, cached Numba plus product batching is much faster than the
current Polars engine:

- 50 rollouts x 100 months: `15.999s` Polars vs `0.849-1.142s` cached Numba.
- 500 rollouts x 100 months: `14.555s` Polars vs `3.381s` cached Numba.

Cold local Numba startup is still expensive enough to dominate one-off runs. In
a long-lived API server, the relevant steady-state cost is the cached path after
the initial compile/cache load.

## Planned Path Forward

The current product path still crosses through generic Polars/Pydantic
boundaries:

- Polars engine: `Polars SimulationRun -> batched product metrics -> MetricFanResponse`
- Numba engine: `Numba arrays -> Polars SimulationRun -> batched product metrics -> MetricFanResponse`

The product service no longer rebuilds a fan from per-rollout Pydantic
`RolloutOutput` objects. It projects monthly metrics once per missing batch,
computes fan percentiles over a dense `(rollout, month)` matrix, and materializes
events only for selected rollout detail.

The next implementation direction is:

1. Keep `//augur/sim:simulate_test` and `//augur/sim:simulate_numba_test` as
   inner simulator conformance tests. These should continue covering detailed
   event/state semantics such as FIFO lots, tax accruals, failure zeroing,
   mortgage timing, and replay invariants.
2. Add API/product-level golden tests that exercise deterministic
   `MetricFanRequest` and `RolloutRequest` inputs and assert expected
   `MetricFanResponse` / `RolloutResponse` payloads. Run the same tests against
   both `AUGUR_SIM_ENGINE=polars` and `AUGUR_SIM_ENGINE=numba`.
3. Add a native Numba product metric path for metric-fan requests that avoids
   decoding full `SimulationRun` event/state frames when the frontend only needs
   one fan metric and terminal summaries. Selected rollout detail can continue to
   decode events on demand.

Not every inner simulator e2e maps to API-level assertions because the public
API does not expose full event logs, individual lot-disposition rows, tax
breakdown internals, or replay invariants. The API golden tests should cover the
visible product contract: cash, public-security value, liquid/net worth,
shortfall, failure count, failed month, rollout ordering, selected rollout
events, and fan percentiles.
