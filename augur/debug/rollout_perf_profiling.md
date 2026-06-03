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
is only half right: the *temporal* axis is serial (correct, and inherent), but
the *rollout* axis is already SIMD-vectorized in NumPy — it is not the
bottleneck. The bottleneck is encode/decode and compile-time ingestion.

## Measured breakdown

500 rollouts × 1200 months, 26.9 s wall (cProfile, single core):

| Stage                                     | Time    | %    |
| ----------------------------------------- | ------- | ---- |
| `decode_run` (dense arrays → Polars)      | 15.6 s  | 58%  |
| ↳ `decode_tax_liabilities`                | 11.4 s  | 42%  |
| ↳ other decoders (state history, events)  | ~4.2 s  | 16%  |
| `external_values_cube` (compile)          | 4.3 s   | 16%  |
| `_validate_series_indexed_amounts`        | 4.1 s   | 15%  |
| dense month loop (`_run_month_step`×1200) | 1.9 s   | 7%   |
| buffer alloc + snapshot + misc            | ~1.0 s  | 4%   |

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
`decode_tax_liabilities` builds three int64 index arrays over the *full* grid
(`state_axes`) before masking → ~4 GB transient for one decoder, ×R OOM beyond
~1000 rollouts.

## Top 10 interventions (highest impact first)

1. **Sparse-active decode for `decode_tax_liabilities` (and peers).** It calls
   `state_axes(snapshots, R, slots)` over the full grid, then masks. Instead get
   the active triples first (`np.argwhere(active)` / `np.nonzero`) and gather
   only those — the pattern `decode_tax_accruals`/`decode_capital_gains` already
   use. Kills ~11 s *and* the OOM. Apply to every full-grid
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
   need year-end / terminal values, snapshot at those indices instead of all
   1201. Cuts both snapshot cost and downstream decode volume.

7. **Drop `~current.failed` boolean-mask fancy-indexing in the hot phases.**
   `current.cash[slot, active_rollout] -= amount[active_rollout]` does a
   gather+scatter copy *every* slot *every* month even when no rollout has failed
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

## Note on parallelism

Rollouts are independent, so beyond the above the R axis can be **chunked**
(e.g. batches of 2000) and run per-chunk — trivially parallel across
processes/cores/nodes, and it bounds peak memory. But chunking is only worth it
*after* interventions 1–6: today the eager full-grid decode, not the rollout
math, is what caps you at ~500 rollouts × 1200 months in 15 GB.
