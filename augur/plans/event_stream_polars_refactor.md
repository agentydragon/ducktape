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

## Architecture: roots, not leaves

**Earlier draft factored this as "9 independent event streams, each gets
its own polars frame." That's wrong — many of the streams are projections,
filters, or joins over a smaller set of root data tables, and the recorder
that builds the Pydantic instances is doing 1:1 row-to-Pydantic work that
disappears entirely if the root table is already in polars.**

The right shape is to migrate the **roots** to polars and let the leaves
fall out as polars expressions on those roots, deleting the corresponding
recorders rather than rewriting them.

| Root                                                                                                                                                               | Status                                 | Streams derived from it                                                                                                                                                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Obligation lifecycle** (one row per obligation; `(rollout, month, obligation_id, kind, actor, creditor, due, paid, unpaid, status, required, source_policy_id)`) | Python lists today                     | `obligations` (= the frame itself), `settlement_results` (strict column subset), `failure_events` (= `filter(unpaid > 0 & required)` + an `id+":failure"` projection)                                      |
| **Funding-decision rollup**                                                                                                                                        | Python list today                      | `funding_decisions` (different cardinality from obligations — multiple decisions per obligation if the policy tries several funding sources, so this is its own root)                                      |
| **Sale action records** (Sp500/Crypto/PE; one polars frame per action emit, keyed by `rollout`)                                                                    | **Already polars** (Refactor D)        | `effects` (4 variants — `Sell{Sp500,Crypto,PrivateEquity}Effect` come from these; `SettlePropertySaleEffect` comes from `PropertyDispositionArrays`); parts of `lot_dispositions` and `accounting_details` |
| **Market-multiplier matrices** (in `MarketBundle`)                                                                                                                 | Already `(rollout, month)` ndarrays    | `market_observations` (`MarketPathObservation` is just a dense `(rollouts × months)` reshape; `PrivateEquitySaleOpportunityObservation` is a filter+project over the same matrices)                        |
| **Accounting trace**                                                                                                                                               | **Already polars** (Refactor D)        | `TaxPaymentAllocationDetail` half of `accounting_details`                                                                                                                                                  |
| **PropertyDispositionArrays**                                                                                                                                      | **Already polars** (Refactor D)        | `SettlePropertySaleEffect` + `PropertySaleBasisGainDetail` half of `accounting_details`                                                                                                                    |
| **Policy step execution**                                                                                                                                          | Python list today                      | `policy_decisions` (5 variants — genuinely accumulator-shaped, but can still emit one column-block per `(month, policy_type)` firing)                                                                      |
| **Tax-lot consumption** during sales                                                                                                                               | Mutated alongside sale recorders today | `lot_dispositions`                                                                                                                                                                                         |

Three of the eight roots are already polars (refactor D investment paying
forward); two more (`market multipliers`, `property dispositions`) are
already arrays/frames and just need projection. The actual new-polars work
is **obligation lifecycle**, **funding decisions**, **policy step
execution**, and **tax-lot consumption**.

## Decisions (locked in)

- **wire schema unchanged**: `ScenarioResult.effects: tuple[Effect, ...]` etc.
  stay Pydantic for backward compat. The materializers read from the root
  frame(s) and yield the right Pydantic variants on demand.
- **materialize-on-access shim**: tests keep `arrays.effects` / `result.effects`
  as Pydantic tuples via `@property` on `ScenarioRunArrays`.
- **single PR**: all remaining migration in one cut on this branch, multiple
  commits (one per root). The `failure_events` slice already landed as
  `a2c3009`; this gets reworked into a projection on the obligation root once
  that lands.
- **leave snapshots alone**: `tax_lots` and `liabilities` are end-of-run static,
  not hot — skip.

## Shared infrastructure (already landed in `a2c3009`)

`augur/core/event_streams.py`:

- `StreamFrameBuilder(schema)` — row-block accumulator that concats into a
  `pl.DataFrame` on `build()`. Used by the still-accumulator-shaped roots
  (obligations, funding decisions, policy decisions, tax-lot
  consumption). For pure-projection roots (market multipliers, sale
  actions, accounting trace), no builder is needed — the frame is built
  in one shot from arrays/existing polars at end-of-run.
- `build_identity_frame(identity_by_rollout)` + `join_trajectory_identity(df, identity_df)` —
  per-rollout trajectory identity is joined once at end-of-run. Replaces
  `_with_trajectory_identity` / `_copy_with_trajectory_identity` for every
  migrated stream.
- Per-stream schema declarations + sort + materializer functions.

### Critical recorder lesson from the `failure_events` slice

**Vectorize at the recorder level — never `.extend({...})` inside a
per-rollout loop.** First attempt put the `.extend` inside
`for rollout_index in np.nonzero(...)[0]:` and hit a +54% simulate-time
regression because each call allocated 7 single-element arrays + ran
schema validation. Moving to one column-block per recorder call (built
with vector ops over the mask) reversed it to −18%.

The recorders in `scenario_engine.py` are already `for rollout in
np.nonzero(mask)[0]` shaped; the migration is to compute the mask once,
project the per-rollout fields into 1D arrays, and emit one `extend(...)`
call covering all matching rollouts.

## Migration sequence (in order)

### Step 1 — Obligation lifecycle root (covers 4 streams; biggest profile slice, ~33%)

**This is where the bulk of the win lives** — the obligation-settlement
recorders are the largest hot block in the profile.

- Define `obligations_frame` with columns
  `(rollout_index, month_index, obligation_id, obligation_type, actor_id,
creditor_id, due_month_index, amount_due_usd, amount_paid_usd,
unpaid_amount_usd, status, source_policy_id, required)` plus the four
  trajectory-identity columns (joined at end-of-run).
- Rewrite `_record_obligation_settlement_rows` (`scenario_engine.py:4835`
  area) to compute one column-block per call from the input 1D arrays.
  The per-rollout for-loop disappears.
- `settlement_results` becomes a `@property` on `ScenarioRunArrays` that
  projects the obligations frame to the `SettlementResult` column subset
  via `iter_rows`.
- `failure_events` becomes a `@property` that does
  `obligations_frame.filter((pl.col("unpaid_amount_usd") > 0) & pl.col("required"))`
  - a derived `failure_event_id = obligation_id + ":failure"` column,
    then iter_rows into `FailureEvent` instances. **The current
    `failure_events_frame` field added in `a2c3009` and the accumulator/
    schema/sort/materializer for it in `event_streams.py` get deleted in
    this step** — it was the right answer for a single isolated stream,
    but `failure_events` is more cleanly a projection now that the root is
    polars.
- Funding decisions go in their own frame (Step 2) — different cardinality
  (multiple funding decisions per obligation when the policy tries cash,
  then sells SP500, etc.).
- Verification: `bbr test //augur/core:test_e2e`. `_sorted_obligations`,
  `_sorted_settlement_results`, `_sorted_failure_events` get deleted.

### Step 2 — Funding-decision root (~10% of profile)

- Define `funding_decisions_frame` with columns matching `FundingDecision`'s
  fields. Cardinality: multiple per obligation (one per funding source
  tried).
- Rewrite `_record_obligation_cash_funding_decisions` (`:4631`),
  `_record_obligation_sale_funding_decisions` (`:4665`),
  `_record_obligation_crypto_sale_funding_decisions` (`:4703`),
  `_record_obligation_pe_sale_funding_decisions` (`:4744`),
  `_record_unfunded_obligation_decisions` (`:4782`) to each emit one
  column-block per call (the per-rollout for-loop disappears in each).
- `funding_decisions` becomes a `@property` materializer over the frame.
- `_sorted_funding_decisions` gets deleted.

### Step 3 — Effects derive from sale action records (already polars)

- `Sp500SaleActionRecord`, `CryptoSaleActionRecord`,
  `PrivateEquitySaleActionRecord`, and `PropertyDispositionArrays` already
  carry every column an `Effect` Pydantic variant needs.
- Delete `_record_property_sale_effects` (`:3373`), `_record_sp500_sale_effects`
  (`:3429`), `_record_crypto_sale_effects` (`:3513`),
  `_record_private_equity_sale_effects` (`:3545`). The per-month list
  accumulator goes away.
- Build the effects frames once at end-of-run by `.with_columns(...)` /
  `.select(...)` on each action record's `numerics` frame, then concat for
  the materializer (which dispatches to the right Pydantic variant on
  `effect_type`).
- `_sorted_effects` gets deleted.

### Step 4 — Market observations derive from `MarketBundle` arrays

- `MarketPathObservation` is the dense `(rollouts × months)` cross-product
  of the multiplier matrices the bundle already exposes. Build the frame
  in one shot at end-of-run by `pl.DataFrame({"rollout_index": ..., "month_index": ..., "sp500_multiplier": flat, ...})`.
- `PrivateEquitySaleOpportunityObservation` rows derive from the
  `private_equity_sale_opportunity_event` mask + the opportunity-value
  matrices.
- Delete `_market_path_observations` (`:2569` — currently builds Pydantic
  list eagerly at scenario start),
  `_record_private_equity_sale_opportunity_observations` (`:2617`), and
  `_record_per_issuer_sale_opportunity_observations` (`:2643`). All three
  become end-of-run polars constructions.
- `_sorted_market_observations` gets deleted.

### Step 5 — Policy decisions (5 variants, column-blocks)

- This is the one root that's genuinely accumulator-shaped — emitted as
  policies fire during the per-month loop, with variable cardinality per
  month. Use one `StreamFrameBuilder` per variant.
- Rewrite `_record_monthly_spend_decisions` (`:2724`),
  `_record_sell_public_stock_decisions` (`:2752`),
  `_record_private_equity_sale_decisions` (`:2781`),
  `_record_partner_contribution_decisions` (`:2850`) — each emits one
  column-block per firing.
- `policy_decisions` becomes a `@property` that materializes the union of
  the 5 variant frames in sorted order.
- `_sorted_policy_decisions` gets deleted.

### Step 6 — Accounting details derive from accounting trace + property dispositions

- `TaxPaymentAllocationDetail` half comes from `accounting_trace` (already
  polars). Project + filter.
- `PropertySaleBasisGainDetail` half comes from `PropertyDispositionArrays`
  (already polars). Project + filter.
- Delete `_record_property_sale_accounting_details` (`:3048`) and
  `_record_tax_payment_allocation_details` (`:3095`).
- `_sorted_accounting_details` gets deleted.

### Step 7 — Lot dispositions derive from sale action records + tax-lot state

- One row per (rollout, month, sale event, lot consumed). Derivable as a
  join of the sale action records frame against the tax-lot inventory
  frame for each rollout-month.
- Delete the row-by-row append inside
  `_record_obligation_accrual_and_settlement_entries` (`:4323` area).
- `_sorted_lot_dispositions` gets deleted.

### Step 8 — Tear down `_with_trajectory_identity` + `_copy_with_trajectory_identity`

Once every stream that previously passed through `_with_trajectory_identity`
is built from a polars frame, both helpers (`:2559-2566`) are dead. Delete
them, drop the `trace_identity_by_rollout` dict in favor of the polars
`trace_identity_frame` already added in `a2c3009`. This single step
captures the ~22% profile slice that the trajectory-identity model_copy
pass owns today.

## `ScenarioRunArrays` end state

```python
@dataclass(frozen=True)
class ScenarioRunArrays:
    scenario_id: str
    scenario_label: str
    month_index: np.ndarray
    numerics: pl.DataFrame  # already polars
    accounting_trace: AccountingTrace  # already polars

    # Per-stream roots (new):
    obligations_frame: pl.DataFrame
    funding_decisions_frame: pl.DataFrame
    sp500_effects_frame: pl.DataFrame
    crypto_effects_frame: pl.DataFrame
    pe_effects_frame: pl.DataFrame
    property_sale_effects_frame: pl.DataFrame
    market_path_observations_frame: pl.DataFrame
    pe_sale_opportunity_observations_frame: pl.DataFrame
    policy_decisions_frames: dict[PolicyDecisionType, pl.DataFrame]
    accounting_details_frames: dict[AccountingDetailType, pl.DataFrame]
    lot_dispositions_frame: pl.DataFrame

    # Tuple-shaped snapshots (left alone):
    tax_lots: tuple[TaxLot, ...]
    liabilities: tuple[LiabilityState, ...]

    # @property shims that materialize the Pydantic surface lazily for the
    # wire schema + test access (effects, policy_decisions,
    # market_observations, lot_dispositions, accounting_details, obligations,
    # funding_decisions, settlement_results, failure_events)
```

`rollout_statuses()` reads `self.obligations_frame.filter(...)` directly
to compute failure-by-rollout aggregates (already done for
`failure_events_frame` in `a2c3009`; the rewire happens in Step 1).

## Wire-response materialization

Unchanged from the original plan — `ScenarioResult` schema is stable; the
four gated streams (`include_obligations`, `include_funding_decisions`,
`include_settlement_results`, `include_failure_events`) materialize to
`()` when their gates are false, and the always-on streams materialize
on demand from the `@property` shims.

## Critical files

- `augur/core/scenario_engine.py` — recorders (most get deleted), the
  per-month loop, `ScenarioRunArrays`, `_with_trajectory_identity`
  (deleted in Step 8), `_sorted_*` helpers (each deleted in its step),
  `rollout_statuses()` (rewired in Step 1).
- `augur/core/event_streams.py` — schemas, materializers, `StreamFrameBuilder`,
  identity-join helpers. Already in place from `a2c3009`; each step adds
  the schemas + materializers for that step's streams and removes the
  `failure_events` standalone artifacts in Step 1.
- `augur/core/scenario_set.py` — wire schema unchanged.
- `augur/core/scenario_engine_test.py`, `augur/core/test_e2e.py` —
  determinism gates. Sort keys are bit-identical to the legacy
  `_sorted_*` tuple keys; if any test breaks on order, the fix is the
  sort key, not the data.
- `augur/core/property_sale.py` — already polars; Step 3 / Step 6 read
  from it.
- `augur/core/accounting_tables.py` — already polars; Step 6 reads from
  it.

## Verification

1. **Determinism** — `bbr test //augur/core:test_e2e
//augur/core:scenario_engine_test //augur/core:scenario_set_test`.
   Sort keys deterministic, `df.sort(...)` reproduces identical ordering.

2. **Wire shape** — `bbr test //augur/core:backend_test
//augur/api:browser_shell_test`.

3. **Perf** — `bazelisk --bazelrc=$SESSION_BAZELRC run --remote_executor=""
--config=nolint //augur/core:bench_augur_run` after each step. Report
   the `simulate / materialize / total` triple in each commit message.

   Baseline (3 scenarios × 32 rollouts × 360 months):

   ```
   simulate_set: 15.7150s
   materialize:  0.8982s
   total:        16.6547s
   ```

   After Step 0 (`a2c3009`, failure_events isolated slice):

   ```
   simulate_set: 12.7956s   (-18%)
   materialize:  0.9129s
   total:        13.7085s
   ```

   Target after Step 8 (full migration): `simulate_set ≤ 4 s` (~4×
   speedup; the profile says ~75% of simulate time is the migrated work).

4. **Profile re-run** — `py-spy record --rate 200 --output /tmp/bench.svg
-- bazel-bin/augur/core/bench_augur_run` after Step 8. Expected: new
   top frames are `polars.with_columns`, `numpy.nonzero`, and the actual
   simulation math; no `pydantic.model_copy` /
   `_copy_with_trajectory_identity` in the top 20.

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
