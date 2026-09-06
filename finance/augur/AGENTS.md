@README.md

Augur is pre-production. Do not add compatibility shims for older URL state
versions, request schemas, or serialized payloads unless the user explicitly
asks for backward compatibility.

Execution runs through `augur/rust`, against the plan `augur/sim` compiles; do
not revive deleted `augur/core` execution or market-bundle adapters. When
extending API responses, project the compiler plan and the canonical frames
directly, as `augur/product/projection.py` does, instead of adding parallel
read-model tables over `SimulationRun`'s long-form polars frames.

## numpy vs jnp

JAX is the sampler, not the simulator: `model/` and `fit/` trace and jit, and the engine
is Rust. Inside those packages numpy still belongs, but only in specific places, and the
line is not "whichever imports first":

- **numpy for compile-time STRUCTURE.** Static index sets used as gather keys, anything
  resolved once host-side before a traced call.
- **numpy for decode.** Sampled output comes back as numpy and polars frames are built
  from it. That is the far side of the boundary; it is not traced.
- **jnp for traced VALUES.** Anything a jitted function computes, carries, or branches on.

Two rules follow, and both have been violated before:

- **Never write a second numpy implementation of something a traced function does in
  jnp.** It cannot be called from the traced path, so it is either dead or a fork waiting
  to drift — and a green suite over the numpy copy implies coverage of a path that ships
  from different code.
- **Write helpers destined for a traced path in jnp from the start.** Converting later is
  not free: `jnp` has no `argsort(kind=...)`, cannot scatter with `put_along_axis`, and
  rejects a plain dataclass as a jit output (use `NamedTuple`, which is a native pytree).
  Assert traceability in the test — `jax.jit` the helper and call it — so a numpy op
  cannot creep back in unnoticed.

**Validation splits along the same line.** A traced value cannot drive a Python `raise`,
so anything that must fail loudly has to be checked before tracing, on the static
inputs.
