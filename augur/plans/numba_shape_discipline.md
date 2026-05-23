# Numba Simulator Shape Discipline Plan

Status: planned.

This plan records the discipline we want before adding much more economic
surface area to the Numba simulator. The current dense-array strategy is a good
fit for Augur: the simulation state is fixed-size for a compiled scenario, and
the human-readable event streams have computable upper bounds. The refactor goal
is not to switch frameworks or introduce ragged runtime data structures. The
goal is to make the fixed-shape contract explicit enough that adding new state
or event kinds is mechanical and hard to mis-wire.

## Current Model

The Numba backend has three conceptual layers:

1. `augur/sim/numba/compiler.py` converts object-heavy scenario data into
   dense numeric arrays: interned string codes, account slots, lot slots,
   monthly transfer/obligation slots, external series cubes, tax bracket
   arrays, and policy arrays.
2. `augur/sim/numba/kernels.py` runs one mutable current state per rollout.
   The current state is local to the rollout loop: cash balances, remaining
   lots, YTD income, tax liabilities, property state, liability state, and
   failure state. It writes snapshots and bounded event records into output
   arrays.
3. `augur/sim/numba/engine.py` allocates output buffers, invokes the kernel,
   and decodes active output slots back into Polars `SimulationRun` frames.

Variable-length event streams are currently represented as dense buffers plus
active masks. That is the desired broad shape. The rough edges are that shape
logic is scattered, some naturally multidimensional event slots are flattened,
and `_Buffers` mixes current-state history with every event family.

## Shape Notation

Use this notation consistently in comments, dataclass fields, and validation:

- `H`: event months, equal to `horizon_months`.
- `S`: snapshot months, equal to `H + 1`.
- `R`: rollout count.
- `C`: cash account slots.
- `L`: initial lot slots.
- `G`: tax profile or capital-gain agent slots, as named by the buffer.
- `J`: tax jurisdiction links.
- `P`: property purchase slots.
- `B`: liability slots.
- `T`: max transfer slots per event month.
- `O`: max obligation slots per event month.
- `D`: scheduled asset sale slots.
- `Q`: liquidity policy slots.
- `A`: max sell-order asset slots per liquidity policy.

Every dense buffer declaration should state its shape in this notation. For
example:

```python
# cash_state[S, R, C]
# transfer_active[H, T, R]
# scheduled_lot_disposition_units[H, D, L, R]
# liquidity_lot_disposition_units[H, Q, A, L, R]
```

## Principles

- State arrays model what determines the next month. They are fixed for a
  compiled scenario and are mutated in the kernel.
- Event arrays model what happened for human inspection and replay tests. They
  are bounded projections of transitions and should be active-mask encoded.
- Prefer semantic axes over flattened indices when an event is naturally
  multidimensional.
- Keep shape ownership close to allocation and decode. Decoder loops should
  consume named shapes, not rediscover flattening math.
- Validate compiled shapes once before calling Numba. Shape mismatch should be a
  Python-side error, not silent corrupted output.
- Do not introduce static shape-typing libraries for this slice. NumPy typing
  is useful for dtypes, but runtime validation and named buffer groups are more
  reliable here.

## Refactor Sequence

### 1. Add SlotPlan

Add a small compiler-owned `SlotPlan` dataclass describing all scenario-derived
dimensions and slot maxima. It should be built in `compile_simulation()` and
stored on `CompiledSimulation`.

Initial fields:

- `horizon_months`
- `rollout_count`
- `cash_count`
- `lot_count`
- `tax_profile_count`
- `capital_gain_agent_count`
- `tax_link_count`
- `tax_liability_count`
- `property_count`
- `liability_count`
- `max_transfer_slots`
- `max_obligation_slots`
- `scheduled_sale_count`
- `liquidity_policy_count`
- `max_liquidity_policy_assets`

The first pass can keep existing flattened event buffers. The value is that all
dimensions have one named source of truth.

### 2. Split Buffer Groups

Replace the single `_Buffers` bag with grouped dataclasses in
`augur/sim/numba/engine.py`.

Suggested groups:

- `StateHistoryBuffers`: cash, lots, income, gains, taxes, properties,
  liabilities, rollout status.
- `TransferEventBuffers`: transfer active mask and amounts.
- `LotDispositionEventBuffers`: scheduled and liquidity disposition buffers.
- `TaxEventBuffers`: tax accrual, breakdown, and settlement buffers.
- `ObligationEventBuffers`: obligations, settlements, failures, mortgage
  payments.
- `PropertyEventBuffers`: property purchase transfers and mortgage originations.
- `SimulationBuffers`: top-level grouping passed through allocation/decode.

Keep kernel signatures pragmatic. It is acceptable for `simulate_with_external_series_numba()`
to pass raw arrays into Numba while Python-side code owns named groups.

### 3. Document Shapes At Allocation

In `_allocate_buffers()`, add comments beside every allocation using the shape
notation above. This is the near-term guardrail even before deeper refactors.

Example:

```python
# State snapshots after month application, including opening month 0.
cash_state=np.zeros((S, R, C), dtype=np.float64)

# Human-readable transfers emitted during event months.
transfer_active=np.zeros((H, T, R), dtype=np.bool_)
```

### 4. Add Runtime Shape Validation

Each buffer group should expose a `validate(plan: SlotPlan) -> None` method.
Call validation immediately after allocation and before the kernel invocation.

Validation should assert:

- expected shape for every array;
- dtype for masks, codes, and numeric arrays;
- no zero-sized dimensions where the kernel expects `max(1, ...)`;
- matching active/value shapes inside each event group.

Use normal exceptions, not bare `assert`, so validation is not affected by
Python optimization flags.

### 5. Unflatten Lot-Disposition Events

The highest-value event shape cleanup is lot dispositions.

Current shape flattens scheduled-sale or liquidity-policy dimensions together
with lot slots. Target shape:

```python
# scheduled sale dispositions
scheduled_lot_active[H, D, L, R]
scheduled_lot_units[H, D, L, R]
scheduled_lot_basis[H, D, L, R]
scheduled_lot_proceeds[H, D, L, R]

# liquidity policy dispositions
liquidity_lot_active[H, Q, A, L, R]
liquidity_lot_units[H, Q, A, L, R]
liquidity_lot_basis[H, Q, A, L, R]
liquidity_lot_proceeds[H, Q, A, L, R]
```

The kernel can then write using semantic indices instead of calculating
`offset + lot`. The decoder can iterate the same axes and no longer reverse
flattened slot math.

### 6. Keep Event Decode Optional For Product Metric Fans

Product metric-fan requests should not decode human event tables. The current
product path already avoids selected-rollout event detail for metric fans, but
the Numba engine still decodes a full `SimulationRun` before product metrics are
projected. Keep a follow-up path open for native Numba product metrics that
reads dense state buffers directly and decodes events only for selected rollout
detail.

That optimization should come after shape discipline, so it can consume named
state and event groups instead of depending on a monolithic `_Buffers` layout.

## Validation Gates

Every refactor step should keep these green:

```bash
bazelisk test //augur/sim:simulate_test //augur/sim:simulate_numba_test
bazelisk test //augur/product:projection_service_test //augur/product:projection_service_numba_test
```

Before landing a shape-discipline slice, run:

```bash
bazelisk test //augur/...
```

For any change that alters decoded visual surfaces or fixture defaults, update
browser/visual goldens in the same commit and rerun the full Augur suite.

## Non-Goals

- Do not switch the simulator to TensorFlow, PyTorch, JAX, or a ragged-array
  framework for this work.
- Do not add static shape typing libraries unless they prove useful in a small
  isolated experiment. Runtime validation plus named dimensions is the default.
- Do not change simulator semantics while refactoring shapes. The paired Polars
  and Numba simulator/product tests are the guardrail.
- Do not make event tables mandatory for metric-fan requests. Human event decode
  remains an on-demand selected-rollout concern.

## Done Criteria

This plan is complete when:

- every Numba buffer allocation has documented dimensions;
- a `SlotPlan` or equivalent owns all max slot counts and semantic dimensions;
- buffer groups separate state history from typed event families;
- shape validation runs before the kernel;
- scheduled and liquidity lot-disposition buffers use semantic axes instead of
  flattened `slot = outer * lot_count + lot` math;
- product metric fans still avoid selected-rollout event materialization; and
- `bazelisk test //augur/...` passes.
