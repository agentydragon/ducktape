@README.md

Pre-production: no compatibility shims for older URL state, request schemas or serialized
payloads unless the user asks.

Python owns financial execution (<sim/session.py>, <sim/world.py>); no native evaluator or
runtime fallback, and no reviving deleted `augur/core` execution or market-bundle adapters.
TLH portfolio state and transitions live in the Python component, not a second holding book.
API responses project the prepared input and canonical frames directly (as
<product/projection.py> does), never parallel read-model tables over the polars frames.

## Conventions

- **One way to declare a world:** `declare_*`, `hold_lot`, `hold_bond`, `track` on a `World`,
  keyword arguments (no argument-bundle records), driven by `start()`/`step()` or
  `ActionSession` (README § Using Augur). No second declaration path.
- **Exogenous paths flow one way:** nothing in `sim/` feeds back into a sampler.
- **No scenario objects** (a config value/enum covering every use case); callers compose
  worlds in code. No layer without a caller that needs it now.
- **Strategies stay with their study** under `study/` or `x/`; core grows no options for them.
- **Models identify by a `model_id` string**, and results display it.
- **Core never depends on `study/` or `x/`**; add no visibility exception. A model moves from
  `x/models/` into core only on evidence it is good enough.
- **Money conversions are exact or raise**: never via floats or `round(x * 100)`; use the
  helpers named in README § Using Augur.

## numpy vs jnp

JAX is the sampler, not the simulator: `model/`, `fit/`, `x/models/` trace and jit; `sim/` is
plain Python. In traced packages: numpy for compile-time structure (static gather index sets,
anything resolved host-side before the call) and for decoding sampled output into polars; jnp
for every traced value.

- **No numpy twin of a traced jnp function**: it is dead or a drifting fork whose green tests
  cover code that does not ship.
- **Write helpers bound for a traced path in jnp from the start** (`jnp` lacks
  `argsort(kind=...)` and `put_along_axis`; jit outputs must be pytrees, so `NamedTuple` not
  dataclass). Test traceability by `jax.jit`-ing the helper.
- **Validate on static inputs before tracing**: a traced value cannot drive a `raise`.

## Provenance of a reported number

A probability is always according to a model. Every reported figure (PR, issue, `debug/`
note, docstring, commit, chat) carries enough to reproduce it, labeled "probability of X
under {sampler, window, policy}" or pointing at the config pinning them:

- **Sampler**: historical replay (overlapping windows: `N` windows ≪ `N` independent draws) or
  a fitted model.
- **Fit window and evidence series**, for a fitted sampler.
- **Full policy config**: allocation, cash band, rebalancing, withdrawal rate, horizon.
- **Instrument construction** per sleeve (bond maturity, credit quality, fee — 120bp on the
  bond sleeve outweighs the sampler choice).
- **Standard error** beside each estimate; do not rank cells whose intervals overlap (most
  allocation-grid cells are inseparable at achievable sample sizes).

`CashBandHousehold.reinvest` (<policy/cash_band_household.py>) is an explicit choice: `None`
accumulates surplus cash (sales-only — reports must say so); `rebalance_tolerance_ppb` says
whether drift alone trades. Results from before that choice was required ran cash-flow-only.

A quoted figure keeps its provenance or says it could not be established; an inherited number
of unknown model is a lead, not evidence.
