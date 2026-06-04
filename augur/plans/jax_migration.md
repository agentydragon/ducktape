# JAX migration: full rewrite of the simulation numeric core

Status: **proposed**. This plans a full migration of augur's forward-simulation numeric core
(exogenous sampling + the dense month loop) from imperative NumPy to JAX (`jit` + `lax.scan` +
`vmap`), keeping the compile and decode boundaries on the host.

## Why

After the 2026-06 product-path optimization arc (see <augur/debug/rollout_perf_profiling.md>), a
10k-rollout × 100-month `metric_fan` request is ~11.6 s wall and **entirely simulation + sampling
bound** — every layer of per-rollout Python marshaling is gone. The remaining cost is real
Monte-Carlo compute, and the two things that would actually move it are exactly what JAX is for:

1. **Vectorized, per-seed-independent RNG.** The GBM sampler (<augur/model/gbm.py>) still draws each
   trajectory with its own `np.random.default_rng(seed)` in a Python loop, because NumPy has no way
   to draw from `R` independently-seeded generators in one vectorized call. JAX does:
   `vmap(lambda key: random.normal(key, (M,)))(keys)` with `keys = vmap(random.fold_in, (None, 0))(base, seeds)`.
   This is the literal "vector of `R` independent seeded states" — correct per-seed semantics, vectorized.
2. **A fused month loop.** The engine (<augur/sim/engine/**init**.py>) runs `for month in
range(horizon): _run_month_step(...)` — Python dispatch over `H` months × ~12 phases, each
   allocating intermediates. `lax.scan` over months with the whole step as the scan body fuses this
   into one XLA kernel (no per-step Python, no intermediate allocs), and runs on GPU/TPU at scale.

### Expected payoff and the Amdahl ceiling

Current cold-path breakdown (10k × 100 mo, cProfile ≈ wall):

| Layer                                | Time   | Under JAX         |
| ------------------------------------ | ------ | ----------------- |
| exogenous sampling (GBM + PE)        | ~5.2 s | traced → fast     |
| dense month loop (`_run_month_step`) | ~2.8 s | `lax.scan` → fast |
| `compile_simulation`                 | ~2.1 s | **stays host**    |
| polars decode / reductions           | ~2.4 s | **stays host**    |

The traceable core is ~8 s of the ~11.6 s. Even if JAX drives it to near-zero on CPU, `compile`
(~2.1 s) and polars decode (~2.4 s) remain on the host → a **~4.5 s floor** unless those are also
moved on-device (the cube build in <augur/sim/compiler/series.py> and the reductions in
<augur/product/decode.py>). So the single-process CPU win is bounded at ~2.5×; the larger wins come
from **GPU at large `R`** and from later pulling the cube + reductions on-device. This plan should
not be sold as "make it all fast" — it is "make the numeric core fast and GPU-ready, and fix the RNG."

## Architecture: the host/device boundary

The seam already exists and is clean:

```
scenario ──(host)──> compile_simulation ──> CompiledSimulation        (static-shaped numeric arrays + host tables)
                                                  │
                                          (device) jitted sim:  sample exogenous → scan months → state arrays
                                                  │
state arrays ──(host)──> monthly_metric_arrays_batch + polars decode ──> MetricFanResponse
```

- **Host (unchanged, Python/NumPy/polars):** `compile_simulation` (<augur/sim/compiler/plan.py>) —
  string tables, typed slots, jurisdictions, the `external_values_cube` scatter — and all of
  `decode.py` / the long-frame builders. These construct object tables and polars frames that are not
  traceable; they bound the trace inputs and consume its outputs.
- **Device (the rewrite):** everything between compile and decode — the exogenous samplers and the
  month loop over the state buffers.

The `CompiledSimulation` plan is the contract: it must hand the jitted sim **only static-shaped
numeric arrays** (it already does — see below) plus python-side constants closed over at trace time.

## Feasibility from the current code

- ✅ **Static shapes everywhere.** Every state buffer in <augur/sim/buffers.py> is a fixed
  `(snapshots, count, R)` array (`cash_state (S, cash_count, R)`, `lot_state`, `property_*_state`,
  `liability_*_state`, the capital-gain split, the failure flags). All counts come from compile. This
  is the prerequisite for `jit`/`scan` and it is already satisfied — there is **no dynamic-shape
  FIFO/`searchsorted`** in the phases; lot disposition and settlement are mask-based. This is the
  single biggest "is it even possible" risk and it is clear.
- ✅ **`scan`-shaped loop.** The month loop carries the mutable `current` state (a fixed pytree of
  fixed-shape arrays) and writes one snapshot per month — exactly a `lax.scan` `(carry, ys)`.
- ⚠️ **The cost is the functional rewrite.** <augur/sim/engine/phases.py> has ~132 `if`/`for`/`while`
  lines and ~59 masked/data-dependent ops. The masked vectorized ops (`where`/`maximum`/`cumsum`/…)
  port nearly 1:1 to `jnp`. The work is in the constructs JAX cannot trace directly:
  - in-place mutation (`current.cash[slot, active] -= amt`) → functional `x.at[...].add(...)`;
  - Python `if`-on-data and `if not failed.any()` fast-paths → `jnp.where` / `lax.cond` (compute-both-select);
  - per-slot `for` loops → vectorize, or `lax.scan`/`fori_loop` where genuinely sequential.
- 🔴 **Hardest piece: the tax machinery.** Year-end accruals, the capital-gain split, and the
  tax-liability event log carry the most data-dependent branching. Functionalize these last.

## Functional-translation patterns (reference for the rewrite)

| Imperative NumPy                          | JAX                                              |
| ----------------------------------------- | ------------------------------------------------ |
| `buf[i, mask] -= x`                       | `buf = buf.at[i].add(jnp.where(mask, -x, 0.0))`  |
| `if cond_scalar: A else: B` (data-dep)    | `lax.cond(cond, f_A, f_B, operands)`             |
| `if not failed.any(): fast` (perf guard)  | drop the guard; always compute the masked form   |
| `for month in range(H): step(...)`        | `lax.scan(step, init_carry, xs=month_indices)`   |
| `for slot in range(n): ...` (per-slot)    | vectorize over the slot axis, or `lax.scan`      |
| `np.random.default_rng(seed).normal(...)` | `random.normal(random.fold_in(base, seed), ...)` |

Carry = the `current` state pytree; the scan `ys` accumulates the per-month snapshots that become the
state-history buffers.

## Phased plan

Each phase is independently landable and gated by a **parity harness**: run the JAX path and the
retained NumPy reference on a fixture scenario and assert `allclose` (within an x64 tolerance) on
every output buffer. The NumPy engine stays as the reference until the JAX path is proven, then is
retired.

### Phase 0 — infra + parity harness

- Add `jax` + `jaxlib` (CPU) to `pyproject.toml`, regenerate `requirements_bazel.txt` via RBE, add
  to the RBE worker image (<devinfra/rbe_image/Dockerfile>). **This is the real cost of Phase 0** —
  getting `jaxlib` hermetic under Bazel + NixOS + the worker image.
- `jax.config.update("jax_enable_x64", True)` globally for float64 parity (float32 would widen value
  drift and lose precision on dollar values).
- A `util` module for shared config + a `parity` test helper (`assert_engine_parity(scenario, …)`).

### Phase 1 — exogenous sampling in JAX (the proving ground)

Port GBM (<augur/model/gbm.py>) and PE risk (<augur/model/private_equity_risk.py>) to `jax.random` +
`vmap` (GBM) / `lax.scan` (PE's per-month evolution). Self-contained: produces the level/PE matrices,
converted to NumPy at the boundary for the existing cube/bundle. Delivers the **correct per-seed
vectorized RNG**, banks part of the ~5.2 s, and validates the entire JAX build/serving story on a
small surface before betting on the engine. **Values change → regenerate sampling-dependent goldens.**

### Phase 2 — the month loop via `lax.scan` (the bulk)

Port `_run_month_step` phase-by-phase **behind the parity test**: convert one phase in
<augur/sim/engine/phases.py> to `jnp`, assert identical against the NumPy reference, repeat until the
whole step is functional, then wrap in `scan` + `jit`. Order: the cashflow/transfer/sale/obligation
phases first (mostly masked arithmetic), the **tax phases last** (the branchy part). 80% of the
effort and risk lives here.

### Phase 3 (optional) — decode + cube on-device

Only if Phase 2's measured payoff justifies chasing the host floor: move `external_values_cube` and
the `monthly_metric_arrays_batch` reductions on-device so the whole compute path is one trace, and
materialize polars frames only at the very end.

### Phase 4 (optional) — GPU

Once on JAX, run on GPU for large `R` (needs GPU in RBE/serving). This is where the order-of-magnitude
wins are, especially combined with `vmap` over scenarios.

## Risks & decisions

- **Total golden/snapshot churn.** RNG + float reduction order (+x64) change every numeric output,
  including the visual goldens (RBE regen). One-time but large. (Already accepted: reproducibility may
  break between commits.)
- **Build integration.** `jaxlib` is a heavy wheel; hermetic under Bazel + NixOS + the RBE worker
  image is the Phase-0 risk. CPU-first; GPU later.
- **`jit` compile latency.** First call traces+compiles (seconds). Fine amortized in the long-running
  server; bad for one-off CLI runs → persistent compilation cache (`jax.experimental.compilation_cache`)
  or an explicit warmup at startup.
- **Debuggability.** Traced code, `jax.debug.print`, NaN-hunting in the tax logic is harder than NumPy.
  The parity harness is the mitigation — every phase is diffed against the NumPy reference.
- **Dual implementation during migration.** The NumPy engine stays as the reference + parity oracle
  until JAX is proven, then is retired. Carrying both has a maintenance cost for the migration window.

## Decisions needed before Phase 0

1. **x64 vs float32.** Recommend x64 (float64 parity, dollar precision). GPU x64 is slower but CPU is
   the current target.
2. **CPU-only vs GPU roadmap.** Determines whether Phase 4 is in scope and whether RBE/serving needs
   GPU. CPU-first is the safe default.
3. **Cut-over policy.** Keep NumPy as a runtime-selectable reference for a deprecation window, or hard
   cut once parity holds? Recommend a window with parity tests in CI.

## Success criteria

- Phase 1: GBM/PE produce statistically-valid draws with correct per-seed independence; the JAX build
  works on RBE; sampling-dependent goldens regenerated; measured sampling time recorded.
- Phase 2: full-engine parity test green (JAX vs retired NumPy reference within x64 tolerance on every
  buffer); `metric_fan` wall time recorded; the month loop is a single jitted `scan`.
- Overall: a recorded end-to-end number to decide whether Phase 3/4 (on-device decode, GPU) are worth
  pursuing past the host floor.
