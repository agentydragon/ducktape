# Augur JAX Simulation Review - 2026-06-05

Scope: deep review of the `augur/sim` JAX backend, especially validation
boundaries, numeric/static structure, and host/device handoff.

Update status: commits `bdac2d6b0` and `17a702618` fixed the concrete validation
and host-boundary issues called out below. The later JAX-only cutover removed the
NumPy backend and selector, so the former parity tests now live as single-backend
JAX validation/edge tests. The remaining open items are the numeric/static cache
boundary and the explicit precision contract.

## Findings

### TLH harvest validation

The removed NumPy backend validated tax-loss-harvesting index prices inside
`_apply_tlh_harvest` and raised when a harvest policy read a negative or
non-finite price. The JAX path previously validated private-equity sampled
channels before JIT, but did not validate TLH harvest series before entering the
compiled scan.

Status: fixed in `bdac2d6b0` by adding host-side TLH validation before
`_program_impl` runs. `tlh_harvest_engine_test.py` now covers negative and
non-finite harvest index prices.

Status: expanded in `17a702618`; after the JAX-only cutover this is
`validation_edge_test.py`, a focused target for sampled-input validation and
edge behavior:

- private-equity negative / non-finite mark validation;
- private-equity negative forced-recovery cashout validation;
- PE and TLH terminal snapshots are not treated as executable sim months;
- scheduled-sale oversell raises;
- invalid liquidity asset prices produce no sale and leave the obligation
  unfunded.

Rule going forward: any nontrivial validation timing, error-message, or
intentionally non-raising edge behavior should be pinned in focused JAX sim
tests.

### Numeric/static JAX cache boundary

`_TracedConfig` documents the intended boundary: shape/structure stays static,
while swept numeric values should remain traced so repeated product runs can
reuse the compiled program. Several numeric business knobs still appear to be
folded into static structures even though they do not obviously change shapes:

- property purchase stake contribution;
- liquidity trigger and sale amounts;
- private-equity floor policy scalars;
- TLH harvest policy scalars;
- some lifecycle event scalar fields.

This is likely correctness-safe, because static differences force a separate
compile, but it weakens the cache-reuse contract for product sweeps. Before
changing any of these, decide whether each knob is truly structural. If not,
move it into traced config/operands and add cache-reuse tests comparable to
`jax_engine_reuse_test.py`.

### Float precision contract

The JAX backend comments describe float64-sensitive settlement behavior, but the
source does not enable `jax_enable_x64`, and several monetary/scalar arrays are
explicitly created as `float32`. That may be acceptable for throughput, but the
contract should be explicit. Either enable x64 before JAX arrays are created and
test precision-sensitive paths, or update comments/tests to describe the
float32 tolerance policy.

### Host/device transfer

The scatter function claimed a single device-to-host transfer, but the previous
implementation unpacked device arrays and called `np.asarray` on many leaves.

Status: fixed in `bdac2d6b0` by batching `ys` and `sale_disp` through one
`jax.device_get((ys, sale_disp))` before unpacking/scattering.

### Module split / working surface

`jax_engine.py` had become a large mixed-responsibility module: host-side
sampled-input validation, compiled-program construction, the `lax.scan` body,
helper kernels, and buffer scatter all lived together.

Status: improved in `17a702618`:

- `jax_validation.py` owns host-side sampled-input validation;
- `jax_scatter.py` owns device-to-host transfer and NumPy buffer scatter;
- `jax_types.py` owns the shared static/folded dataclasses, including the
  precise `_ScanMeta` type used by scatter;
- `jax_engine.py` still owns `_build_program`, `_program_impl`, and the JAX
  kernels, preserving the existing compile/cache behavior and public test
  imports.

Remaining readability work should be incremental. The next plausible split is
moving pure JAX kernels out of `jax_engine.py` after their parity/cache behavior
is fully pinned.

### Current performance expectation

The JAX architecture is broadly sound: one module-level JIT, a static structural
plan, and `lax.scan` for the month loop. The current CPU profile should not be
assumed to beat the old NumPy path at small/medium entity counts; the documented
win condition is larger fan-out and/or accelerator execution.

## Test Expectations

Focused tests to run after changes in this area:

```bash
bazelisk test //augur/sim:validation_edge_test //augur/sim:tlh_harvest_engine_test //augur/sim:scan_test //augur/sim:jax_engine_reuse_test --config=rbe --test_output=errors
```

For broader handoff, use the repo-level Bazel targets documented in
`AGENTS.md`.
