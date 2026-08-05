@README.md

Augur is pre-production. Do not add compatibility shims for older URL state
versions, request schemas, or serialized payloads unless the user explicitly
asks for backward compatibility.

The backend now executes through `augur/sim`; do not revive deleted
`augur/core` execution or market-bundle adapters. When extending API
responses, prefer native `ProjectionRun` read models
(`augur/sim/projections.py`) over deriving more tables from
`SimulationRun`'s long-form polars frames.

## numpy vs jnp

The simulator is JAX. numpy still belongs in it, but only in specific places, and the
line is not "whichever imports first":

- **numpy for compile-time STRUCTURE.** Plan arrays, static index sets used as gather
  keys, anything resolved once host-side before the scan. `lot_order_for_pool` is the
  archetype: lot identity and purchase month are plan columns, so FIFO order is static,
  computed once with `lexsort`, and the traced step just gathers by the result.
- **numpy for decode.** Buffers come back as numpy and polars frames are built from
  them. That is the far side of the boundary; it is not traced.
- **jnp for traced VALUES.** Anything the scan computes, carries, or branches on.

Two rules follow, and both have already been violated:

- **Never write a second numpy implementation of something the engine does in jnp.** It
  cannot be called from the scan, so it is either dead or a fork waiting to drift. A
  whole numpy FIFO (`fifo_sell_units` / `fifo_sell_dollars`) sat beside the engine's
  `_fifo_sell_*` until it was deleted — reachable only from its own tests, so a green
  suite implied coverage of a path that shipped from different code.
- **Write helpers destined for the scan in jnp from the start.** Drafting them in numpy
  and converting later is not free: `jnp` has no `argsort(kind=...)`, cannot scatter with
  `put_along_axis`, and rejects a plain dataclass as a jit output (use `NamedTuple`, which
  is a native pytree). Assert traceability in the test — `jax.jit` the helper and call it
  — so a numpy op cannot creep back in unnoticed.

**Validation splits along the same line.** A traced value cannot drive a Python `raise`,
so anything that must fail loudly has to be checked at config time on the static inputs.
A per-month check on an amount that may be series-indexed is not possible; check the
configured base amount instead, and say in the message why that is sufficient.
