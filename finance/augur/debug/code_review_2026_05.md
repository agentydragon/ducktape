# Augur Code Review - Current Open Findings

Last merged: 2026-05-27.

Consolidates `code_review_2026_05_26.md` and
`code_review_2026_05_27.md`. This file keeps findings that still matched the
current tree at merge time and drops landed progress logs, externally blocked
release notes, and modeling wish-list items already tracked in `augur/sim/TODO.md`
or `augur/plans/roadmap.md`.

Severity guide:

- P1: can produce materially wrong simulation output or hide an invalid scenario.
- P2: correctness risk, scaling bottleneck, or boundary problem to fix before the
  area grows.
- P3: cleanup, stale docs, or structural debt.

## Findings

### P2: External-series cubes use serial row loops and uneven coverage checks

`simulate._validate_series_indexed_amounts` builds a Python dict from long Polars
rows, then checks required rollout/month cells with nested Python loops.
`external_values_cube` and `external_event_values_cube` also fill dense arrays by
iterating rows. Existing `SampledExogenousBundle` helpers have a stricter
long-frame-to-matrix pattern for exact rollout/month coverage, but the sim
boundary does not use that uniformly.

Impact:

The dense simulator pays avoidable Python overhead before NumPy gets control.
Coverage validation also differs by consumer: `SeriesIndexedAmount` references
get prechecked, while asset prices, home values, and event cubes can still enter
the dense plan as `NaN` or default `False` cells.

Current evidence:

- `augur/sim/simulate.py`: `_validate_series_indexed_amounts`.
- `augur/sim/compiler/series.py`: `external_values_cube`,
  `external_event_values_cube`.
- `augur/model/exogenous.py`: matrix helpers on `SampledExogenousBundle`.

Recommendation:

Share one coverage-checked matrix materialization path for level and event
series. If `ExternalSeriesContext` remains the sim boundary, give it the same
strict matrix helpers.

### P2: Product fan path splits batched simulations back into one-rollout cache entries

`ProductService._simulate_missing` samples and simulates all missing seeds in one
dense batch, then immediately stores one sliced `DenseSimulationResult` per seed.
`_decoded_rollouts` computes monthly arrays one rollout at a time, and
`_metric_matrix` stacks those arrays back into a percentile matrix.

Impact:

The product gets the cost of batch simulation but loses much of the benefit at
the cache/API boundary. The fan path becomes "simulate in batch, split, decode N
times, restack".

Current evidence:

- `augur/product/service.py`: `_simulate_missing`, `_decoded_rollouts`,
  `_metric_matrix`.
- `augur/sim/TODO.md`: ProjectionRun/product cutover item.

Recommendation:

Separate distribution/fan caching from selected-rollout detail caching. Keep a
batch-shaped dense result or batch-shaped metric matrix for fan requests, and
slice only for selected-rollout detail.

### P2: Direct cash mutations bypass obligation/failure semantics

Several phases debit cash directly: scheduled transfers, property purchases, and
capital improvements. Obligation settlement has explicit funded/failed handling,
but these direct debits can push cash negative without going through the
hard-demand failure path.

Impact:

An unaffordable purchase or capital improvement can continue as a negative-cash
rollout. That may be intentional for some transfers, but today the behavior is
defined by phase implementation rather than by scenario-visible contract.

Current evidence:

- `augur/sim/engine/phases.py`: `_apply_scheduled_transfers`,
  `_apply_property_purchases`, `_apply_lifecycle_events`,
  `_apply_obligation_settlement`.

Recommendation:

Make the cash-demand taxonomy explicit. If purchases/capex/transfers can
overdraft, expose diagnostics and document it. If they cannot, lower them into
obligation-like demands or a common funding/failure policy.

### P2: Obligation funding recomputes each account group once per slot

`_obligation_group_funded` loops every obligation slot, rebuilds the
`(agent, from_slot)` group mask, recomputes grouped due, and rereads available
cash for each slot.

Impact:

The grouped semantics look right, but the monthly hot loop repeats the same
aggregation for every member of a group.

Current evidence:

- `augur/sim/engine/phases.py`: `_obligation_group_funded`.

Recommendation:

Precompute per-month group ids for `(agent, from_slot)`, or derive unique groups
once per month. Compute group due with a vectorized scatter/add or a loop over
unique groups, then broadcast the funded result back to slots.

### P3: Rollout detail still materializes selected dense results through the old frame path

The fan endpoint reads metric arrays directly from dense buffers, but rollout
detail still calls `dense.decode()` and then maps Polars rows into product
Pydantic event markers. This is already tracked as the `ProjectionRun` product
cutover.

Impact:

Selected-rollout detail remains on a different dataflow than distribution fans,
and product decode still knows too much about decoded sim frames.

Current evidence:

- `augur/product/service.py`: `rollout`.
- `augur/product/decode.py`: event projection helpers.
- `augur/sim/projections.py`: `ProjectionRun`.
- `augur/sim/TODO.md`: rollout-detail cutover item.

Recommendation:

Finish the `ProjectionRun` cutover for rollout detail and keep product-specific
conversion at the API/product boundary. Use dense-buffer/projection helpers for
lot/property valuation rather than reimplementing per-lot loops in product
decode.

### P3: Lifecycle and obligation discriminators are still raw SoA fields

The old arena-splitting work mostly landed, but `LifecycleEventCompileOutput`
still reuses one `amount` field for different meanings by `kind`, and
`ObligationCompileOutput` still carries `source_kind` / `source_index` with
kind-dependent payload meaning. This is efficient for dense arrays, but callers
must remember per-kind field semantics manually.

Impact:

The hot path can keep the SoA layout, but slow-path engine dispatch and decode
logic remain easy to misuse when new lifecycle or obligation kinds are added.

Current evidence:

- `augur/sim/compiler/lifecycle.py`: `LifecycleEventCompileOutput`.
- `augur/sim/compiler/obligations.py`: `ObligationCompileOutput`.
- `augur/sim/engine/phases.py`: lifecycle and obligation source dispatch.

Recommendation:

Add typed views or per-kind slices over the dense rows: a discriminated union
over the existing SoA layout. Keep the NumPy arrays for the engine, but make
non-hot-path callers consume named per-kind payloads instead of raw kind-indexed
columns.

### P3: Some compile-plan fields still sit outside their natural arenas

Most `CompiledSimulation` arena work landed, but a few domain fields remain as
top-level arrays: capital-gain agent mapping, property runtime initial state,
property owner profile index, and liability owner profile index.

Impact:

This is not urgent, but it keeps `CompiledSimulation` from being fully
domain-shaped and makes cross-domain ownership details harder to find.

Current evidence:

- `augur/sim/compiler/plan.py`: `CompiledSimulation` fields
  `capital_gain_agent_codes`, `tax_profile_capital_gain_index`,
  `property_rented_fraction`, `property_building_basis`,
  `property_owner_profile_index`, and `liability_owner_profile_index`.

Recommendation:

Move these only when touching nearby compiler code. Likely homes are
`TaxCompileOutput`, `PropertyCompileOutput`, and `LiabilityCompileOutput`, or
small companion compile outputs if widening those domains would be awkward.

## Dropped During Merge

- Historical progress log for R-last buffers, codec/compiler/engine package
  splits, and landed `*CompileOutput` arenas. Git history is the better record.
- Cross-repo GitHub Actions suspension note from 2026-05-26. It was release
  operations state, not an Augur code-review finding.
- Deferred modeling realism wish-list already tracked in `augur/sim/TODO.md` or
  `augur/plans/roadmap.md`.
