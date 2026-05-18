# Augur event-stream → polars long-format refactor

Active plan. Remove from `plans/` once the migration lands and `bench_augur_run`
shows the expected ~5× speedup at 3 scenarios × 32 rollouts × 360 months.

## Context

`augur/core/scenario_engine.py:run_scenario_vectorized` is ~75% Pydantic
object construction + sorting + `model_copy`, only ~7% actual math.

py-spy of `//augur/core:bench_augur_run` (3 scenarios × 32 rollouts × 360
months on a fresh build, ~16 s per `simulate_set`, ~67 s across 4 bench
runs, 19,709 samples at 200 Hz):

| Bucket                                                                                      | Samples | Share |
| ------------------------------------------------------------------------------------------- | ------- | ----- |
| `_settle_required_cash_obligation_at_month_position` + the `_record_obligation_*` recorders | ~6500   | ~33%  |
| `_with_trajectory_identity` / `_copy_with_trajectory_identity` (Pydantic `model_copy`)      | ~4400   | ~22%  |
| `pydantic.model_copy` + `pydantic.__copy__` + `copy.copy` leaves under the above            | ~4000   | ~20%  |
| `_sorted_*` helpers over Pydantic lists                                                     | ~2000   | ~10%  |
| `metric_fan_columns` / `_fan_columns` (the only big numpy chunk)                            | ~1500   | ~7%   |
| Everything else                                                                             | ~1300   | ~7%   |

Scaled to the gaffer-private production workload (15 scenarios × 128
rollouts × 360 months) this puts a single `/api/scenario_sets/run` call
at ~5 minutes wall on the 1-CPU augur pod — the "unusably slow" symptom.

The Refactor D rollup (`augur/TODO.md`) already migrated the _numeric_
arrays (`ScenarioRunArrays.numerics`, `PropertyCashFlowArrays`,
`PartnerEquityArrays`, `PartnerEquityAgreementArrays`,
`PropertyDispositionArrays`, `Sp500SaleActionRecord`,
`CryptoSaleActionRecord`) from `dataclass`-of-`np.ndarray` to a polars
`pl.DataFrame` keyed by `(rollout_index, month_index)` with a
`column(name) -> np.ndarray` reshaper.

The per-record **event streams** are the remaining piece. They're still
`list[PydanticModel]`, accumulated row-by-row during the per-month loop,
then sorted, `model_copy(update=trajectory_id)`'d, frozen into
`tuple[Effect, ...]` etc. on `ScenarioRunArrays`. Every recorder already
iterates a `(rollout,)` 1D vector via `np.nonzero(mask)[0]` — the records
are just being unboxed one Pydantic instance at a time on the way out.
Switching to row-block emission eliminates the dominant cost.

## Approach

Replace each per-record `list[PydanticModel]` accumulator inside
`run_scenario_vectorized` with a polars long-format `DataFrame` keyed by
`(rollout_index, month_index, …)`. Recorders emit one row-block per
recorder call (one block per `(kind, month)` firing), not one Pydantic
instance per non-zero rollout.

Nine streams in scope (= 18 frames, one per discriminator variant):

1. `effects` — 4 variants (`Sell{Sp500,Crypto,PrivateEquity}Effect`, `SettlePropertySaleEffect`) → 4 frames
2. `policy_decisions` — 5 variants → 5 frames
3. `market_observations` — 2 variants → 2 frames
4. `accounting_details` — 2 variants → 2 frames
5. `lot_dispositions` — flat → 1 frame
6. `obligations` — flat, wire-gated default-off → 1 frame
7. `funding_decisions` — flat, wire-gated default-off → 1 frame
8. `settlement_results` — flat, wire-gated default-off → 1 frame
9. `failure_events` — flat, wire-gated default-off → 1 frame

Variant-split (not a single wide frame with nullable variant columns)
because each recorder already builds homogeneous batches of a single
variant — tight per-variant schemas avoid null bloat and match the Pydantic
field list 1:1.

Trajectory identity (`path_set_id`, `exogenous_path_id`,
`scenario_input_id`, `projection_trajectory_id`) is materialized as a
small per-rollout polars frame at the end of the run. Each event frame
gets one `df.join(identity_df, on="rollout_index", how="left")` instead
of N `record.model_copy(update=...)` calls — eliminates the ~22% block
entirely.

The `tuple[Effect, ...]` wire surface and the `arrays.effects` test
surface stay as `@property` shims that materialize the Pydantic instances
on demand from the underlying frame(s). Hot path never builds them.

## Decisions (locked in)

- **wire schema unchanged**: `ScenarioResult.effects: tuple[Effect, ...]` etc.
  stay Pydantic for backward compat
- **materialize-on-access shim**: tests keep `arrays.effects` / `result.effects`
  as Pydantic tuples
- **single PR**: all 9 hot streams in one cut, not slice-by-slice
- **leave snapshots alone**: `tax_lots` and `liabilities` are end-of-run static,
  not hot — skip

## New shared infrastructure

A small helper module — `augur/core/event_streams.py` — holds the row-block
builder, the per-variant schemas, and the identity-join helper. Lives next
to `scenario_engine.py` so the recorders import what they need without
inflating the engine file.

```python
class _StreamFrameBuilder:
    """Accumulates dict[str, np.ndarray] row-blocks per recorder call;
    `build()` concatenates into one pl.DataFrame."""

    def __init__(self, schema: dict[str, pl.DataType]) -> None: ...
    def extend(self, columns: dict[str, np.ndarray]) -> None: ...
    def build(self) -> pl.DataFrame: ...

def _join_trajectory_identity(df: pl.DataFrame, identity_df: pl.DataFrame) -> pl.DataFrame:
    return df.join(identity_df, on="rollout_index", how="left")
```

`identity_df` is built once per scenario run from
`_trace_identity_by_rollout` (`scenario_engine.py:2540-2556`).

Existing long-format pattern to mirror: `PropertyCashFlowArrays`
(`scenario_engine.py:704-730`), `PropertyDispositionArrays`
(`property_sale.py:62-76`), `Sp500SaleActionRecord` /
`CryptoSaleActionRecord` (`scenario_engine.py:851-882`).

## Per-stream migration

For each stream the diff has three pieces:

1. **Schema declaration** in `event_streams.py`, mirroring the Pydantic
   model's field list. Use `pl.Categorical` for short enum-valued columns
   (`decision_type`, `obligation_type`, `status`, etc.) and
   `pl.UInt32`/`pl.Int64`/`pl.Float64` to match.

2. **Recorder rewrite** in `scenario_engine.py`:
   - **`effects`**: `_record_property_sale_effects` (`:3373-3400`),
     `_record_sp500_sale_effects` (`:3429-3450`),
     `_record_crypto_sale_effects` (`:3513-3540`),
     `_record_private_equity_sale_effects` (`:3545-3620`) — each becomes
     `builder.extend({...})` with the 1D arrays already in scope.
   - **`policy_decisions`**: `_record_monthly_spend_decisions`
     (`:2724-2750`), `_record_sell_public_stock_decisions` (`:2752-2779`),
     `_record_private_equity_sale_decisions` (`:2781-2848`),
     `_record_partner_contribution_decisions` (`:2850-2869`).
   - **`market_observations`**: `_market_path_observations` (`:2569-2614`)
     produces a dense `(rollout, month)` rectangle — emit as one frame at
     end of run by direct construction from the multiplier matrices, no
     per-month accumulation needed. The opportunity observation recorders
     (`:2617-2640`, `:2643-2720`) stay row-block.
   - **`accounting_details`**: `_record_property_sale_accounting_details`
     (`:3048-3093`), `_record_tax_payment_allocation_details`
     (`:3095-3144`).
   - **`lot_dispositions`**: emitted from
     `_record_obligation_accrual_and_settlement_entries` (`:4323-4520`),
     row-block per obligation-settlement.
   - **Obligation triplet** (`obligations`, `funding_decisions`,
     `settlement_results`, `failure_events`):
     `_record_obligation_settlement_rows` (`:4810-4870`),
     `_record_obligation_cash_funding_decisions` (`:4631-4663`),
     `_record_obligation_sale_funding_decisions` (`:4665-4701`),
     `_record_obligation_crypto_sale_funding_decisions` (`:4703-4742`),
     `_record_obligation_pe_sale_funding_decisions` (`:4744-4780`),
     `_record_unfunded_obligation_decisions` (`:4782-4808`). All five
     already iterate `np.nonzero(...)[0]` — switching to row-block emission
     is local.

3. **End-of-run assembly** at the `ScenarioRunArrays(...)` construction
   site (`scenario_engine.py:2510-2540`). Each
   `_with_trajectory_identity(_sorted_*(records))` call becomes:

   ```python
   df = builder.build()
   df = _join_trajectory_identity(df, identity_df)
   df = df.sort([...same key as the _sorted_X helper...])
   ```

   Then the frame is stored on `ScenarioRunArrays` (next section).
   `_sorted_*` helpers (`scenario_engine.py:3315-3370`),
   `_with_trajectory_identity` and `_copy_with_trajectory_identity`
   (`scenario_engine.py:2559-2566`) are deleted.

## `ScenarioRunArrays` shape + materialize-on-access shim

In `augur/core/scenario_engine.py:178-275`, replace the `tuple[Effect, …]`
fields with private frame fields and `@cached_property` shims:

```python
@dataclass(frozen=True)
class ScenarioRunArrays:
    ...
    _effects_frames: dict[EffectType, pl.DataFrame]              # one per variant
    _policy_decisions_frames: dict[PolicyDecisionType, pl.DataFrame]
    _market_observations_frames: dict[MarketObservationType, pl.DataFrame]
    _accounting_details_frames: dict[AccountingDetailType, pl.DataFrame]
    _lot_dispositions: pl.DataFrame
    _obligations: pl.DataFrame
    _funding_decisions: pl.DataFrame
    _settlement_results: pl.DataFrame
    _failure_events: pl.DataFrame

    @cached_property
    def effects(self) -> tuple[Effect, ...]:
        return tuple(_materialize_effects(self._effects_frames))
    # … same shape for the other 8 streams
```

- `_materialize_*(frames) -> Iterator[PydanticModel]` per stream: iterate
  via `df.iter_rows(named=True)`, construct one Pydantic instance per row,
  yield in already-sorted order. One materializer per variant.

- `@cached_property` so tests reading `arrays.effects` twice don't pay
  twice. `frozen=True` + `cached_property` requires the same
  `object.__setattr__` pattern already used in `accounting_tables.py`.

- `ScenarioRunArrays.rollout_statuses()` (`:225-274`) currently iterates
  `self.failure_events` to build `failures_by_rollout` — switch it to read
  the `self._failure_events` polars frame directly via
  `group_by("rollout_index")`. This is the only internal consumer that
  benefits from skipping Pydantic materialization on the hot dashboard
  path (e.g. when the deployment's `/distribution` view rolls up rollout
  health).

## Wire-response materialization

`ScenarioResult` (`augur/core/scenario_set.py:1137-1158`) is unchanged.
The four gated streams default off (`scenario_set.py:727-730`), so on
the wire they materialize to `()`. The six always-on streams (`effects`,
`policy_decisions`, `market_observations`, `lot_dispositions`,
`accounting_details`, `tax_lots`, `liabilities`) materialize on demand
from the shims when the response builder reads them.

Follow-up `augur/TODO.md:88-96` ("extend `ReportSpec.include_*` to the
smaller fields, default off") becomes cheaper after this refactor — the
cost of materializing what we _don't_ gate off goes away too.

## Critical files

- `augur/core/scenario_engine.py` — the recorders,
  `run_scenario_vectorized`, `ScenarioRunArrays`,
  `_with_trajectory_identity` (deleted), `_sorted_*` helpers (deleted),
  `rollout_statuses()` (switched to polars).
- `augur/core/event_streams.py` — **new module**, holds
  `_StreamFrameBuilder`, schemas, materializer functions, identity-join
  helper.
- `augur/core/scenario_set.py` — schema column types matching Pydantic
  variant fields. Wire schema unchanged.
- `augur/core/scenario_engine_test.py`, `augur/core/test_e2e.py` — should
  pass unchanged via the materialize-on-access shims; flag any
  order-of-construction edge cases (Pydantic constructor side effects?
  unlikely).
- `augur/core/accounting_tables.py` — reference for the `frozen=True +
cached_property` pattern.
- `augur/core/scenario_engine.py:704-730` (`PropertyCashFlowArrays`),
  `augur/core/property_sale.py:62-76` (`PropertyDispositionArrays`), and
  `augur/core/scenario_engine.py:851-890` (sale action records) —
  existing long-format patterns to mirror.

## Verification

1. **Determinism** — `bbr test //augur/core:test_e2e
//augur/core:scenario_engine_test //augur/core:scenario_set_test`.
   No changes from baseline expected. Sort keys are deterministic
   (`(month_index, rollout_index, ...)` with enum/id tiebreakers, all
   immutable post-construction), so `df.sort(...)` reproduces identical
   ordering. If any test fails on order, the fix is the sort key, not the
   data.

2. **Wire shape** — `bbr test //augur/core:backend_test
//augur/api:browser_shell_test` to confirm response payload is
   unchanged.

3. **Perf** — `bazelisk --bazelrc=$SESSION_BAZELRC run --remote_executor=""
--config=nolint //augur/core:bench_augur_run` before/after. Report the
   simulate / materialize / total triple in the PR description.

   Baseline (3 scenarios × 32 rollouts × 360 months, captured against
   commit `4e4cb79`):

   ```
   simulate_set: 15.8778s
   materialize:   0.8816s
   total:        16.7594s
   ```

   Target after refactor: `simulate_set ≤ 4 s` (≥4× speedup; profile
   says ~75% of simulate time is the migrated work). `materialize` may
   rise slightly (now joins identity + sorts on polars) but should stay
   < 1.5 s.

4. **Profile re-run** — `py-spy record --rate 200 --output /tmp/bench.svg
-- bazel-bin/augur/core/bench_augur_run`. Expected: new top frames are
   `polars.with_columns`, `numpy.nonzero`, and the actual simulation
   math; no `pydantic.model_copy` / `_copy_with_trajectory_identity` in
   the top 20.

5. **Live** — after the image bakes, smoke
   `https://augur.allegedly.works` distribution view against the
   gaffer-private 15×128×360 workload. 5 min → ~1 min would be a clear
   win, ~30 s would be on-target.

## Out of scope

- Migrating the wire schema to `ColumnarTable` (matches the frontend
  consumption pattern but is a separate breaking change; revisit after
  this perf win lands).
- Migrating `tax_lots` / `liabilities` (end-of-run snapshots, not hot —
  no perf reason).
- The remaining Refactor D rollup items (`augur/TODO.md:64-79`, engine
  arithmetic to polars expressions). Independent track; this refactor is
  prerequisite for _neither_ direction.
- Gating the remaining wire streams (`include_market_observations`, etc.,
  per `augur/TODO.md:88-96`). Cheaper to land after this PR but separate.
- Streaming partial fans / Redis caching. Both become much more viable
  once a `scenario_set` run finishes in seconds instead of minutes, but
  neither is blocked on this PR.
