@README.md

Augur is pre-production. Do not add compatibility shims for older URL state
versions, request schemas, or serialized payloads unless the user explicitly
asks for backward compatibility.

Python orchestrates execution through `augur/sim`; native financial steps live in
`augur/rust`. TLH portfolio state and its transitions belong to the Python
component, not a second native holding book. Do not revive deleted `augur/core`
execution or market-bundle adapters. When
extending API responses, project the prepared input and the canonical frames
directly, as `augur/product/projection.py` does, instead of adding parallel
read-model tables over `SimulationRun`'s long-form polars frames.

## numpy vs jnp

JAX is the sampler, not the simulator: `model/` and `fit/` trace and jit;
`sim/` runs ordinary Python alongside native financial steps. Inside the traced
packages numpy still belongs, but only in specific places, and the
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

## Provenance of a reported number

Augur reports probabilities, and a probability is always _according to a model_. A number
quoted without the model that produced it is not a weaker claim than one quoted with it —
it is a different, unfalsifiable claim, and it survives into later work as if it were
measured.

So every reported figure — in a PR body, an issue, a `debug/` note, a docstring, a commit
message, a chat reply — carries enough to reproduce it:

- **Which sampler.** Historical replay over overlapping windows, or draws from a fitted
  model. These are not interchangeable and do not have the same error structure: replay
  windows overlap, so `N` windows are worth far fewer than `N` independent observations.
- **Which fit window**, for a fitted sampler, and which evidence series it was fit against.
- **The policy config that was live**, in full — allocation, the cash band, rebalancing,
  withdrawal rate, horizon.
- **The instrument construction** for each sleeve. A bond sleeve's assumed maturity, credit
  quality and fee are not detail; a 120bp difference on the bond sleeve moves the answer
  more than the choice of sampler does.
- **The sampling noise.** Report the standard error next to the estimate, and do not rank
  cells whose intervals overlap. Most cells in a plausible allocation grid are not
  separable at achievable sample sizes, so a bare ordering of cells is usually reading
  noise.

`TargetAllocationPolicy.allow_purchases` is an explicit policy choice. With `False`, surplus
cash accumulates instead of being invested; reports must state this sales-only behavior.

`rebalancing` on the same model used to be the worse instance of this, defaulting to "never
rebalance on drift"; it is now a required `CashflowOnly | DriftBand`, so every run says which
it was. Results published before that change did not, and ran as `CashflowOnly`.

**"Probability of X" is never the whole label.** Write "probability of X under
{sampler, window, policy}", or point at the config that pins all three. Where a figure is
quoted from elsewhere, carry its provenance with it or say that you could not establish
it — an inherited number whose model is unknown is a starting point for an investigation,
not evidence.
