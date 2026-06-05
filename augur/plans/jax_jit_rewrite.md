# JAX engine JIT rewrite — COMPLETED (2026-06-05)

This plan is done. The dense JAX engine is a single always-`lax.scan` device program: the whole
month loop compiles into one `jax.jit` program with the per-rollout `_ScanState` as the scan carry,
every phase is branch-free, and the NumPy buffers are filled from the stacked scan outputs in one
device→host transfer. Every phase from the original plan landed — transfers, purchases, scheduled and
liquidity sales, obligations/settlement for every source kind, PE tenders, TLH harvest, property
sale, §168 depreciation, lifecycle / primary-residence, and the year-end tax machinery — with the
dual-backend `simulate_test` and the engine parity suite green on both NumPy and JAX.

Compilation caching is JAX-native (not hand-rolled): `_program_impl` is a stable module-level
`@partial(jax.jit, static_argnames=("p", "structure"))` keyed on the natively-hashable `SlotPlan` and
`_Structure` plus the avals of the traced `_Baked` pytree, so identical-structure runs and traced
config/seed sweeps reuse the compiled executable; an opt-in on-disk cache
(`AUGUR_JAX_COMPILATION_CACHE_DIR`) carries reuse across processes.

See <augur/sim/engine/jax_engine.py> for the implementation. This tombstone can be deleted once no
one needs the historical context.
