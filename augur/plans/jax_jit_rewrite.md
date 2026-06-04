# JAX engine JIT rewrite

## Why

The JAX backend is parity-correct but runs **eager**, ~38× slower than NumPy
(10.4s vs 0.27s at 1000 rollouts × 120 months, `--dense-only`). Profiling
(`profile_rollout --backend jax --sort tottime`) shows the cost is per-op
dispatch + per-primitive XLA compilation across the month loop
(`apply_primitive` 30.5k calls; `.at[]` scatter; `backend_compile_and_load`),
**not** host transfers. Vectorizing a single phase while still eager does not
help (measured: no change) — only `jax.jit` removes the dispatch overhead.

## Approach: incremental, per-phase JIT (suite stays green throughout)

JIT each phase as a pure function with `month` passed as a **traced scalar** (so
it compiles once and is reused across all months — a static `month` would
recompile per month). The existing Python month loop still threads state and
writes the NumPy buffers (eager, fine); converted phases drop from O(slots×ops)
primitive dispatches/month to one compiled call/month. Wall-time improves as the
hot phases (obligations, tax, FIFO sales) convert; the dual-backend
`simulate_test` verifies parity at every step.

Each phase must become **branch-free**:

- No `for slot in range(...)` Python loops with `if int(plan.x[month,slot]) >= 0`.
  Process all slots vectorized, masked by validity.
- Sentinel `-1` slot/profile indices → scatter to a dummy row via `_scatter_rows`
  (pad target with one extra row, redirect `-1` there, slice it off).
- No `bool(x.any())` short-circuits or `raise` on traced values — always compute;
  oversell guards move outside the jitted core (or drop).
- Per-slot amount schedules → `_amount_values_vec` (vectorized fixed/series select).

## Reusable infra (in `jax_engine.py`)

- `_scatter_rows(target, indices, values)` — sentinel-aware segment scatter-add.
- `_amount_values_vec(...)` — `_amount_values` vectorized over slots, branch-free.

## Phase order (by dispatch weight)

1. ✅ `_record_capital_gains` — branch-free (masked einsum, no per-lot scatter).
2. transfers — pattern proof (this slice).
3. obligation accruals / settlement / `_obligation_group_funded` — biggest slot loops.
4. scheduled asset sales + liquidity (FIFO; `ordered_lots` is static per slot — pad to max).
5. PE tenders.
6. year-end tax machinery (links/profiles/lots loops).
7. property purchases / lifecycle / depreciation / sale.

## Endgame

Once every phase is a jitted pure function, fold the Python month loop into a
single `lax.scan` over `jnp.arange(horizon)` and JIT the whole engine, filling
the NumPy buffers from the stacked scan outputs in one device→host transfer.

Benchmark target: CPU (current runner). The decisive JAX win is GPU at large
rollout counts (R≫1000); CPU may only roughly match NumPy given the small,
scatter-heavy arrays.
