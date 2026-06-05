# Rollout performance profiling (100-year horizon, large fan-out)

Investigation of where time and memory go when running augur sim rollouts at a
"production-ish" scale: a mortgage + nontrivial knob config, 100-year horizon
(1200 months), and 500–10000 parallel rollouts.

Tooling added for this: `//augur/sim:profile_rollout`
(<augur/sim/profile_rollout.py>) — builds the spike-1 bench scenario plus a
financed primary-residence purchase (mortgage origination + monthly
amortization, property tax, mortgage-interest deduction) and runs `simulate(...)`
under `cProfile`.

```bash
bazelisk run //augur/sim:profile_rollout --config=nolint -- \
  --rollouts 500 --horizon-months 1200 --sort tottime
```

(Built with `bazelisk` + system Java 21 and the BuildBuddy remote cache.)

## Headline finding

**The rollout math is already vectorized and parallel across the rollout (R)
axis. The serial month loop is cheap (~7% of wall time). The cost is at the
boundaries: decoding dense arrays into long-form Polars frames, and ingesting /
validating the external-series long frame via Python row iteration.** There is
also an O(horizon² × R) memory blow-up in tax-liability state history that
OOM-kills the process beyond ~500–1000 rollouts at a 100-year horizon on a
15 GB box.

So the user's hypothesis ("we're doing a lot serially instead of in parallel")
is only half right: the _temporal_ axis is serial (correct, and inherent), but
the _rollout_ axis is already SIMD-vectorized in NumPy — it is not the
bottleneck. The bottleneck is encode/decode and compile-time ingestion.

## Measured breakdown

500 rollouts × 1200 months, 26.9 s wall (cProfile, single core):

| Stage                                     | Time   | %   |
| ----------------------------------------- | ------ | --- |
| `decode_run` (dense arrays → Polars)      | 15.6 s | 58% |
| ↳ `decode_tax_liabilities`                | 11.4 s | 42% |
| ↳ other decoders (state history, events)  | ~4.2 s | 16% |
| `external_values_cube` (compile)          | 4.3 s  | 16% |
| `_validate_series_indexed_amounts`        | 4.1 s  | 15% |
| dense month loop (`_run_month_step`×1200) | 1.9 s  | 7%  |
| buffer alloc + snapshot + misc            | ~1.0 s | 4%  |

Top `tottime` offenders: `{built-in method new_str}` 6.66 s (string-column
materialization), `decode_tax_liabilities` 5.03 s, `DataFrame.iter_rows` 3.84 s
(3.6 M calls — from `external_values_cube` + `_validate_series_indexed_amounts`,
each walking the full 1.8 M-row external-series long frame once).

### Scaling

Wall clock at 500 rollouts, varying horizon: 240 mo → 2.7 s, 600 mo → 8.8 s,
1200 mo → 23.6 s. Super-linear, because tax-liability state history is
`(snapshots × liability_slots × R)` and `liability_slots` grows with the number
of tax years (`compile_tax_liability_slots`: one slot per link per year-end).
At 1200 mo that grid is `1201 × 200 × 500 = 120 M` elements, and
`decode_tax_liabilities` builds three int64 index arrays over the _full_ grid
(`state_axes`) before masking → ~4 GB transient for one decoder, ×R OOM beyond
~1000 rollouts.

## Top 10 interventions (highest impact first)

1. **Sparse-active decode for `decode_tax_liabilities` (and peers).** It calls
   `state_axes(snapshots, R, slots)` over the full grid, then masks. Instead get
   the active triples first (`np.argwhere(active)` / `np.nonzero`) and gather
   only those — the pattern `decode_tax_accruals`/`decode_capital_gains` already
   use. Kills ~11 s _and_ the OOM. Apply to every full-grid
   `state_history_frame_from_columns` decoder (tax_liabilities, property_state,
   liabilities). Biggest single win.

2. **Stop storing tax liabilities (and rarely-read state) as a dense
   per-snapshot grid.** Liability slots accumulate as `years × links` and
   persist in every future snapshot → O(horizon² × R). Model them as events
   (set at year-end, cleared at settlement) or snapshot only the columns a
   consumer reads. Removes the quadratic-in-horizon memory term.

3. **Vectorize `external_values_cube` (4.3 s).** Replace
   `for row in series_values.iter_rows()` with columnar extraction:
   pull `series_id`/`rollout_index`/`month_index`/`value` as NumPy arrays, map
   `series_id → index` once (Polars `replace`/categorical), then a single
   fancy-index scatter `values[idx, rollout, month] = value`. ~50–100× faster.

4. **Short-circuit `_validate_series_indexed_amounts` (4.1 s, ~100% wasted
   here).** It unconditionally builds a 1.8 M-entry Python dict from
   `iter_rows()` before checking whether any `SeriesIndexedAmount` exists. Return
   early when `_series_indexed_amount_uses` yields none; when they do exist, do
   the membership/zero checks with a Polars filter/join, not a per-row dict.

5. **Make decode lazy / opt-in.** `simulate()` eagerly decodes ~15 Polars
   frames; most callers (benches, parameter sweeps, summary stats) need a
   handful. Return `DenseSimulationResult` and decode per-frame on demand
   (`simulate_dense_with_external_series` already exposes this). For Monte-Carlo
   summaries, reduce on the dense arrays and never build long frames at all.

6. **Decimate state snapshots.** `_snapshot_current_state` copies ~25 arrays
   every month (0.83 s + all the memory decode then walks). If consumers only
   need year-end / terminal values, snapshot at those indices instead of all 1201. Cuts both snapshot cost and downstream decode volume.

7. **Drop `~current.failed` boolean-mask fancy-indexing in the hot phases.**
   `current.cash[slot, active_rollout] -= amount[active_rollout]` does a
   gather+scatter copy _every_ slot _every_ month even when no rollout has failed
   (the common case). Fast-path `if not current.failed.any():` to operate on the
   full contiguous slice; only fall back to masked writes once failures exist.

8. **Hoist month-invariant per-slot Python loops.** Transfers/obligations do
   `for slot in range(...)` per month re-reading `plan.transfers.cause[month,
slot]`. For recurring transfers the active-slot set is largely static —
   precompute per-month active slots at compile time, or fold the whole transfer
   step into a segment-sum/matmul into `cash` keyed by from/to slot. Turns
   O(months × slots) Python into O(months) NumPy.

9. **Keep ID columns integer/categorical, not Python `object` strings.** The
   6.7 s `new_str` is per-row Python string objects for `agent_id` /
   `jurisdiction_id` across tens of millions of rows. Emit dictionary-encoded
   `pl.Categorical`/`Enum` (or int codes joined to a tiny code→string table at
   the very end) so Polars never allocates per-row `str`.

10. **float32 state + scratch reuse for large fan-out.** State buffers and the
    external cube are float64; for 100-year × 10k-rollout Monte Carlo the engine
    is memory-bandwidth-bound on the R axis, so float32 ~halves memory and
    bandwidth where precision allows. Also reuse scratch in `_amount_values`/FIFO
    (each `np.full(rollout_count, …)` allocates per call) to cut allocator churn.

## Results after the first optimization pass

Three boundary fixes landed (rollout math untouched — it was never the problem):

1. **Sparse-active `decode_tax_liabilities`** — gather active `(month, rollout, slot)`
   triples via `np.nonzero` instead of building three full-grid int64 index arrays
   (`state_axes`) and masking. Removes the ~2.9 GB transient index allocation.
2. **Vectorized `external_values_cube`** — columnar `series_id → index` map + a single
   fancy-index scatter, replacing a Python `iter_rows()` loop over every
   `(rollout, month, series)` row.
3. **Short-circuited `_validate_series_indexed_amounts`** — returns immediately when no
   `SeriesIndexedAmount` is in use (the common case), and filters to referenced series
   otherwise, instead of unconditionally building a millions-of-entries dict from
   `iter_rows()`.

500 rollouts × 1200 months: **26.9 s → 18.1 s** (function calls 5.9 M → 484 K). The two
compile-time `iter_rows` hotspots are gone; `decode_tax_liabilities` is now dominated by
Polars `new_str` building the `agent_id`/`jurisdiction_id` string columns over the
(still large) active-row set.

### Where the memory actually goes (the 1000–10000 rollout wall)

A `--dense-only` profiler mode (compile + month loop, **no** Polars decode) isolates pure
rollout compute from the encode boundary:

| rollouts | dense-only (compute) | full `simulate()` (with decode) |
| -------- | -------------------- | ------------------------------- |
| 1000     | 2.8 GB / 3.2 s ✅    | OOM (decode frames)             |
| 4000     | 11 GB / 11 s ✅      | OOM                             |
| 10000    | OOM ❌               | OOM                             |

So the rollout compute itself is cheap and parallel — 1000 rollouts in 3.2 s / 2.8 GB.
The remaining walls are two O(horizon² × R) eager allocations, **not** the per-cell state:

- **Dense `tax_liability_state` buffer** `(snapshots, years×links, R)`. At 1000 rollouts
  that's `1201 × 200 × 1000 × 8 B = 1.9 GB`; at 10000 the dense-only run dies allocating
  exactly `17.9 GiB for an array with shape (1201, 200, 10000)`. Every snapshot stores
  all 200 per-year liability slots even though each liability only exists ~12 months —
  quadratic in horizon. This is intervention #2 and is the next lever: store tax
  liabilities as events (set at year-end, cleared at settlement), not a dense
  per-snapshot grid. (`tax_liability_active_state` + the decoded long frame have the same
  shape problem.)
- **Eager full decode** of every state-history frame (intervention #5). `simulate()`
  materializes ~15 Polars long frames up front; for large fan-out this is tens of GB.
  Making decode lazy/opt-in (the `DenseSimulationResult` already exists) lets summary
  callers reduce on the dense arrays and never build the long frames.

Both are deeper changes (they touch the `SimulationRun` contract / state-history storage)
and are deliberately left as follow-ups to the boundary fixes above.

## Results after the second pass (event-based tax liabilities)

Intervention #2 landed: the dense `(snapshots, year-slots, R)` tax-liability state history
is replaced by a sparse balance-change log (one event at year-end accrual + one per
settlement), and the `tax_liabilities` output frame is now those change events. The
piecewise-constant per-month balance is fully described by the changes, so every existing
consumer (all in tests, querying the change-months) is unchanged.

This removed both the 17.9 GiB dense buffer _and_ the largest decode frame (tax-liability
rows accumulated to tens of millions over 100 years), so the full pipeline now fits:

| rollouts | dense-only (compute)       | full `simulate()` (with decode)            |
| -------- | -------------------------- | ------------------------------------------ |
| 1000     | 2.8 GB                     | 3.6 GB / 7.4 s ✅ (was OOM)                |
| 2000     | —                          | 7.1 GB / 14.4 s ✅ (was OOM)               |
| 10000    | 7.7 GB / 57 s ✅ (was OOM) | (other per-month frames still O(months×R)) |

Remaining lever for full `simulate()` at 10000 is lazy/opt-in decode (#5): the other
state-history frames (cash, lots, …) are still materialized eagerly at O(months × R).

## Results after the third pass (dict-encoded id columns)

`new_str` — Polars building `pl.Utf8` columns from NumPy `object` arrays of Python `str` —
was the dominant remaining decode cost (~60% of decode wall, spread across `decode_cash`,
`decode_asset_lots`, `decode_liabilities`, `decode_obligations`, `decode_transfers`). The
codec now carries id columns as a `CodeColumn` (integer codes + small category table) and
materializes them with a single Arrow dict-gather instead of per-element `str`; the two
`rollout_status` decoders are likewise vectorized (no Python `failed_month` loop, gather for
the status string).

500/1000-rollout, 1200-month decode (cProfile, relative): `new_str` 13.3 s → 0.7 s;
`decode_obligations` 7.2 s → 0.9 s. Full `simulate()` at 1000 × 1200: **7.4 s → 5.1 s**
(≈30% faster), output frames byte-identical (all sim + product + api tests pass).

`#5` for the genuinely per-month frames (`cash`, `liabilities`, `ordinary_income`) is _not_
safe to event-ize: unlike tax liabilities, they change every month and consumers query them
at arbitrary months expecting forward-filled state. Their remaining decode cost is the row
count itself (O(months × R)); the lever there is lazy/opt-in decode (#1), not event-izing.

## Results after the fourth pass (lazy decode)

`SimulationRun` is now a lazy facade over the dense result: each long-form frame (and the
event log) is a `cached_property` decoded on first access, so `decode()` is free and a caller
only pays to materialize the frames it reads. Public attribute surface unchanged — every
consumer and test is untouched.

This makes the existing "stats on dense arrays" path (`metric_fan`, `monthly_metric_arrays`)
pay **zero** decode, and the single-rollout detail view (`rollout_events_from`, which reads
only `events_log` + `asset_lots`) skip the other ~10 frames it never used. 1000 × 1200:

| caller pattern                                    | before | lazy   |
| ------------------------------------------------- | ------ | ------ |
| summary / fan (reduces on dense, decodes nothing) | 6.5 s  | 1.9 s  |
| rollout detail (`events_log` + `asset_lots`)      | 6.5 s  | 3.85 s |
| full-frame caller (`materialize all`)             | 6.5 s  | 6.5 s  |

The backend already caches the dense result per `(scenario, seed)` (`ProductService` LRU of
R=1 `DenseSimulationResult`), so re-deriving any frame or stat for a cached rollout is
re-simulation-free; lazy decode means that re-derivation only builds what's asked for.

## Results after the fifth pass (per-rollout slicing)

Passes 1–4 profiled the raw `simulate()` boundary (`//augur/sim:profile_rollout`). With those
landed, the **product `metric_fan` service path** became the thing to profile — a new tool,
`//augur/api:profile_metric_fan` (<augur/api/profile_metric_fan.py>), runs one product request
end to end:

```bash
bazelisk run //augur/api:profile_metric_fan --config=nolint -- \
  --rollout-count 10000 --horizon-months 100
```

This path does something raw `simulate()` does not: after simulating the batch it **slices the
batched dense result into R independent R=1 `DenseSimulationResult`s** to populate the per-`(scenario,
seed)` LRU (`_simulate_missing` → `slice_dense_results`). At 10k rollouts that slicing dominated.

Two fixes landed:

1. **O(R²) → O(R) exogenous-frame partition (#1850).** Per-seed slicing filtered the full batch
   Polars frames (`series_values`, the PE bundle) once per rollout — R passes over an R-row frame.
   `_partition_by_rollout` now partitions each frame once up front, so all R slices share a single
   `partition_by`. Measured wall at 10k × 100mo: **139 s → 33.6 s**.
2. **Drop fancy `np.take` from buffer slicing (#1857).** Each batched buffer field was sliced per
   rollout with `np.take(val, [i], axis).copy()` — the fancy-indexing slow path, copied twice
   (`take` already returns a fresh array). Replaced with basic slicing `val[..., i : i + 1, ...].copy()`
   and the buffer dataclass is now walked once across all rollouts (`_split_dc`/`_split_array`).
   10k × 100mo (cProfile): `slice_dense_results` **78.7 s → 46.6 s** (−41%), whole request
   **133.4 s → 92.2 s** (−31%); the 890k `np.take` calls (26.4 s) are gone.

### Current breakdown (10k × 100mo, `profile_metric_fan`, cProfile)

| Stage                                     | Time   | %   | Notes                                       |
| ----------------------------------------- | ------ | --- | ------------------------------------------- |
| `slice_dense_results`                     | 46.6 s | 50% | split batch → R cacheable R=1 results       |
| ↳ `np.ndarray.copy` (2.0 M calls)         | 33.9 s | 37% | one strided copy per (rollout × leaf-field) |
| `simulate_dense_with_external_series`     | 24.8 s | 27% | the actual rollout compute + sampling       |
| ↳ exogenous sampling (`composite.sample`) | 13.9 s | 15% | PE-risk + level-series draws                |
| ↳ `_validate_series_indexed_amounts`      | 8.2 s  | 9%  | inflation-indexed spend validation          |
| ↳ polars `collect` (20 099 calls)         | 7.3 s  | 8%  | per-month-step lazy-frame materialization   |
| `compile_simulation`                      | 7.7 s  | 8%  | once per batch, seed-independent            |

(cProfile inflates wall ~3–4× and over-weights high-call-count paths; figures are relative.)

## Results after the sixth pass (eliminate slicing; vectorize validation)

Two of the four "next levers" proposed above did **not** survive the call graph and were dropped
after investigation — a caution against attributing cost by where lines sit in a cumulative profile:

- **"Cache the compiled plan" was wrong.** `compile_simulation` calls `external_values_cube(external_series, …)`,
  baking the per-rollout sampled cube into the plan — it is **seed-dependent**, so a per-scenario plan
  cache would serve stale data. Not done.
- **"Batch the month-loop collects" was misattributed.** The dense month loop is pure NumPy (zero
  `.collect()`); the 20 k `LazyFrame.collect` calls were `slice_dense_results` doing a per-rollout
  `with_columns(rollout_index=0)` relabel. So they were **part of slicing**, not a separate lever —
  and disappear with it.

The two valid levers landed:

1. **Reduce `metric_fan` on the batch; stop per-rollout slicing (PR #1859).** Cache the simulated
   batch once, shared by every seed it was sampled with; each cache entry records only its column
   index. The metric reductions take a `rollout_index` and read that column straight out of the shared
   batch (only the arrays a metric needs), so the fan path slices nothing; `rollout()` slices its one
   seed on demand for the event log. Per-`(scenario, seed)` keys and overlapping-range reuse unchanged.
   `slice_dense_results` — the #1 line at ~50% — **leaves the profile entirely** (no copy floor, no
   relabel collects). 10k × 100mo: whole request **~30 s → 19.6 s wall**.
2. **Vectorize `_validate_series_indexed_amounts` (PR #1858).** It built a per-`(series, month, rollout)`
   dict from `iter_rows()` over the whole referenced frame before any check; now restricted to each
   amount's reset anchors with a columnar `group_by`/`n_unique`. Was ~8 s (its share at this scale);
   no longer a hotspot.

Both landed (#1858, #1859 merged). With slicing gone, `metric_fan` is dominated by
`simulate_dense_with_external_series` itself — the actual rollout compute plus exogenous sampling
— i.e. real work rather than marshaling.

### Current hotspots (all six passes landed)

10k rollouts × 100-month horizon, `profile_metric_fan`, cProfile (**15.2 s**, down from the
slicing-dominated ~30 s wall):

| Stage                                         | Time   | %   | What                                              |
| --------------------------------------------- | ------ | --- | ------------------------------------------------- |
| `composite.sample` (exogenous sampling)       | 5.4 s  | 36% | GBM level draws + PE-risk sampling                |
| ↳ `gbm.sample_levels` (×6 blocks)             | 2.3 s  |     | geometric-Brownian-motion level paths             |
| ↳ `private_equity_risk.sample`                | 1.9 s  |     | PE trajectory + tender draws                      |
| `simulate_dense` (compile + month loop)       | 5.6 s  | 37% | the rollout engine                                |
| ↳ `compile_simulation`                        | 2.1 s  |     | plan + `external_values_cube` (seed-dependent)    |
| ↳ `_run_month_step` ×240                      | 3.4 s  |     | dense NumPy month loop                            |
| &nbsp;&nbsp;↳ `_apply_liquidity_policy_sales` | 1.75 s |     | per-month liquidity sales                         |
| `monthly_metric_arrays` ×10 000 (reductions)  | 2.6 s  | 17% | per-rollout column reads (`_lot_value_by_month`)  |
| polars `collect` ×103                         | 2.5 s  |     | inside sampling/compile (was ×20 k under slicing) |

The polars `collect` count fell from ~20 000 (slicing's per-rollout relabel) to **103** — those
remaining collects live in GBM/PE sampling and compile, not the hot path.

### Seventh pass: vectorized reduction (PR #1864)

The reduction loop above landed: `monthly_metric_arrays_batch` reduces every metric over the whole
`(…, R)` batch in one pass (`(H+1, R)` per metric), and `_decoded_rollouts` reduces each distinct
batch **once** then column-slices per seed, instead of calling the reduction per rollout.
`monthly_metric_arrays` **leaves the profile** (function calls 4.0 M → 2.3 M); whole request
**15.2 s → 11.6 s wall**. `metric_fan` is now entirely sampling + simulation:

| Stage                                 | Time  | What                                |
| ------------------------------------- | ----- | ----------------------------------- |
| `composite.sample` (exogenous draws)  | 5.2 s | GBM + PE sampling                   |
| `simulate_dense` (compile+month loop) | 4.8 s | the engine; `_run_month_step` 2.8 s |
| polars `collect` ×103                 | 2.4 s | inside sampling/compile             |

No per-rollout Python marshaling remains.

### Remaining levers (highest impact first)

1. **Exogenous sampling (~5.2 s).** GBM + PE draws are now the largest block and inherent stochastic
   work; the deterministic setup may be cacheable across requests, the draws are not. The 103 polars
   `collect`s live here.
2. **`_apply_liquidity_policy_sales` (~1.75 s) in the month loop.** Per-month Python work in the
   otherwise-NumPy loop — candidate for the same fast-path / vectorization treatment as the other
   phases (interventions 7–8 above).
3. **Process-level chunking** of the R axis for memory bounding and multi-core, now that the
   per-request boundaries are this thin.

## JAX engine: per-call recompilation (2026-06-05)

Profiling the JAX engine against the **real gaffer-private deployed config**
(`//gaffer_augur:profile_rollout`, `model=bayesian`, 1 lot, 15 properties, 1 PE
issuer; 2000 rollouts × 120 months, CPU) showed the JAX backend ~9× slower
end-to-end than NumPy warm, and `JAX_LOG_COMPILES=1` showed why: **`_program_impl`
(the whole scan program) re-compiled on essentially every call** — ~1.7 s of XLA
compile baked into each "warm" run, even though the traced-arg avals were
byte-identical across calls.

Root cause: the native `jax.jit` cache keys on the static args `(p, structure)`.
`structure` (then `_Structure`) carried **external-series row indices** — e.g.
`_FoldedPE.floor_series`, harvest/liquidity-pool `series_index`, the lifecycle
home-value series, the liquidity amount-spec tuples' series slot, and
`sale_price_series`. Those indices were assigned by `collect_level_series_keys`
in **`polars.unique()` (hash) order**, which is non-deterministic, so two compiles
of the identical scenario produced a structure that differed only in a series
index (observed `floor_series` flipping `1↔3`). Different static arg → cache miss
→ full recompile, on ~every call.

Two fixes (both landed):

1. **Deterministic series order** — `collect_level_series_keys` now `.sort()`s the
   unique series ids, so index assignment is reproducible (`augur/sim/compiler/series.py`).
2. **Series indices are traced operands, not static structure** — a series index is
   just a row into `external_values`, so it's now threaded as a traced device array
   (`_Operands.*`, dynamic gather) instead of a Python `int` baked into the jit static
   key. The compiled program is independent of _which_ row, so no series-index value
   can trigger a recompile by construction. The two jit-arg pytrees were renamed to say
   what they are: `_Static` (the compile cache key — counts, slot indices, folded event
   tuples, masks) and `_Operands` (every traced device array the scan closes over).

Result on the gaffer config (2000 × 120, CPU): `_program_impl` compiles **once**
(cold `run[0]` ≈ 3.1 s incl. the ~1.7 s XLA compile), then warm engine is a steady
**~0.16 s** (was alternating 0.16 s hit / 2.6 s recompile). NumPy↔JAX parity
(`scan_test`, `simulate_test`, `jax_engine_reuse_test`) stays green; a new
`test_independent_compiles_of_same_scenario_are_structurally_identical` guards the
invariant (two independent compiles ⇒ equal `_Static` ⇒ no recompile).

JAX is still ~1.7× NumPy's 0.095 s engine on CPU at this entity scale (the always-on
branch-free body is op-heavy — see #249's HLO breakdown); the win is on GPU / large
fan-out, and the pathological recompile is gone.

## Note on parallelism

Rollouts are independent, so beyond the above the R axis can be **chunked**
(e.g. batches of 2000) and run per-chunk — trivially parallel across
processes/cores/nodes, and it bounds peak memory. But chunking is only worth it
_after_ interventions 1–6: today the eager full-grid decode, not the rollout
math, is what caps you at ~500 rollouts × 1200 months in 15 GB.
