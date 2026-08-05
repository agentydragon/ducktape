# Tensorized Simulator Plan

The dense backend's month-step phases run as a JAX `lax.scan` over month, with
JAX array operations over the rollout axis `R`, not as scalar per-rollout loops.
Event and state decoders are likewise vectorized over host NumPy buffers. This
doc records the design (goals, invariants, phase algorithms) and what's still
open.

## Goal

Time remains scan-ordered:

```python
for month in range(H):
    ...
```

Within a month, state transitions operate over the rollout axis `R` with
array operations:

```python
cash[:, checking_slot] -= monthly_due
failed |= cash[:, checking_slot] < 0.0
```

The simulator does not rely on Numba `parallel=True` / `prange` for rollout
parallelism. That path was measured as pathological for Augur's accounting
kernel: cold `1x1` product metric-fan compile was about `82.9s`, with about
`64.0s` in Numba parfor lowering. Plain cached Numba for the same month-step
shape cold-compiled in about `15.6s`.

The chosen design:

- NumPy-style tensor operations over `R` for monthly state transitions.
- Dense active-mask event buffers for human-readable event traces.
- Polars at table/API boundaries.
- JAX for the hot simulator program.
- Host NumPy arrays for the stable engine/codec boundary.

## Framework Stance

JAX owns the current backend because it has the important primitives for this
workload while compiling the whole month loop into one reusable program:

- `jnp.where` for masks;
- `jnp.minimum`, `jnp.maximum`, `jnp.clip` for pointwise transitions;
- `jnp.cumsum` and reductions for bounded ordered consumption;
- JAX gather/scatter updates for lot, event, and state arrays;
- `lax.scan` for the month loop.

The cost is cold compile behavior and the need to keep static plan structure
separate from traced numeric inputs. The codec boundary intentionally remains
host-side: `run_jax_scan` writes into preallocated NumPy buffers so decoders and
Polars/API surfaces keep a stable interface.

Do not reintroduce a second numeric backend as an experiment. Prototype narrow
JAX kernels or host-side codecs if a phase needs cleanup; the production
simulator has one execution path.

## FIFO Primitives

The needed operation is not an unbounded queue append/pop. It is bounded
ordered consumption from a fixed lot axis, which maps to:

- host-side sorting to precompute the FIFO lot order by
  `(agent, account, asset, purchase_month, lot_id)`;
- direct indexed selection when the order is static for a
  `(agent, account, asset)` group;
- `jnp.cumsum(..., axis=1)` to compute the prefix quantity/value consumed
  before each ordered lot;
- pointwise `jnp.clip`, `jnp.minimum`, and guarded division to
  compute the units actually sold from each lot;
- JAX scatter updates when multiple source positions may accumulate into the
  same output position, such as grouped tax/event aggregation.

## Shape Notation

Use the same notation as the engine code, plus:

- `R`: rollouts.
- `H`: event months.
- `S`: snapshots, `H + 1`.
- `C`: cash account slots.
- `L`: lot slots.
- `P`: property slots.
- `B`: liability slots.
- `G`: capital-gain profile slots.
- `J`: tax jurisdiction links.
- `K`: tax bracket slots.
- `T`: transfer slots per month.
- `O`: obligation slots per month.
- `Q`: liquidity policies.
- `A`: asset order slots per liquidity policy.

State arrays put rollout first unless there's a strong reason not to:

```python
cash[R, C]
lot_remaining[R, L]
ordinary_ytd[R, tax_profile]
capital_gain_ytd[R, G, 2]
tax_liability_amount[R, tax_liability]
property_basis[R, P]
liability_principal[R, B]
failed[R]
```

Event buffers keep their event-first layout for decode simplicity:

```python
transfer_active[H, T, R]
sched_disp_units[H, D, L, R]
liq_disp_units[H, Q, A, L, R]
```

## Invariants the engine maintains

These are the load-bearing properties of the tensorized design. Any change to
the month-step or decode path must preserve them.

### Month-step phases run over the rollout axis

- Amount evaluation (`evaluate_amount_for_month`) returns `amount[R]` for
  fixed and series-indexed amounts; rollout variation flows from
  `external_values`.
- Scheduled transfers apply per monthly slot via masked updates to
  `cash[:, from_slot]`, `cash[:, to_slot]`, and
  `ordinary_ytd[:, profile]`.
- Property purchases and mortgage originations apply month-active property
  slots across all rollouts in one pass.
- Scheduled asset sales use the bounded vector FIFO helper (`fifo_sell`)
  over `R × L` with target dollars or target units.
- Liquidity policies iterate policy and asset-order axes; each iteration
  sells target dollars across all rollouts via the same FIFO helper. The
  outer loop is over policies and asset-order slots (small fixed dimensions),
  not over rollouts.
- Tax accrual computes ordinary brackets, LTCG brackets, MID, SALT, tax
  liability writes, and the year-end YTD reset all per tax link over `R`.
- Obligation due computation and settlement apply per monthly slot with
  group funding by `(agent, cash_account)`; `paid` / `shortfall` / failure
  masks are full `R` boolean arrays.

### Failure semantics are vectorized

- `active = ~failed` is computed at the top of every pre-settlement phase,
  so failed rollouts don't emit future events that have to be zeroed later.
- New failures during a month update `failed[R]` and `failed_month[R]`
  with masked assignment.
- After settlement, value-bearing state for failed rollouts is zeroed by
  one masked write across the rollout axis.

### Snapshots are direct array assignments

Snapshot writes copy current state into `state_history[month + 1]` without
intermediate dicts or row builders.

### Decode is dense → sparse, not nested loops

State-history decoders (`_decode_cash`, `_decode_asset_lots`,
`_decode_ordinary_income`, `_decode_capital_gains`,
`_decode_tax_liabilities`, `_decode_property_state`,
`_decode_property_stakes`, `_decode_liabilities`,
`_decode_rollout_status_history`, `_decode_final_rollout_status`) gather
all `(month, rollout, slot)` triples via numpy broadcasts, optionally
filter by an active mask, and materialize the polars frame in one
`pl.from_numpy(...)` call. Per-slot string columns are resolved by
`_codes_to_strings` (O(slot) Python work) and indexed.

Event decoders (`_decode_transfers`, `_decode_property_purchases`,
`_decode_sched_dispositions`, `_decode_liquidity_dispositions`,
`_decode_tax_accruals`, `_decode_obligations`,
`_decode_mortgage_originations`, `_decode_mortgage_payments`,
`_decode_tax_settlements`) gather sparse events via `np.argwhere(active)`,
then bool-gather value buffers and index per-axis ID lookups. Dynamic
cause-ID f-strings are O(N gathered events) comp, not the dense iteration
space. `_frame_from_columns` passes `schema=spec.schema` to
`pl.DataFrame()` so object-dtype string columns cast to `pl.Utf8`,
keeping `pl.concat` across families dtype-clean.

### Product fan bypasses polars entirely

`monthly_metric_arrays` reads `dense.buffers.*` directly and returns
`{name: (H+1,) ndarray}`. `_DecodedRollout` caches that dict, not a
polars frame. The fan endpoint never calls `dense.decode()`; the
rollout-detail endpoint calls `dense.decode()` only for the event log
and emits `monthly_metrics` via `{name: arr.tolist()}`, not
`pl.DataFrame(...).to_dict()`.

## Phase Algorithms

### Amount Evaluation

Target signature:

```python
amount = evaluate_amount_for_month(compiled_amount, month, external_values)
# amount[R]
```

For fixed amounts:

```python
amount = np.full(R, fixed)
```

For indexed amounts, `base_month` and `reset_month` are scalar for a monthly
slot, while external values vary by rollout:

```python
base_level = external_values[series_index, :, base_month]
reset_level = external_values[series_index, :, reset_month]
amount = base * reset_level / base_level
```

### Transfers

Loop over transfer slots for the month, not rollouts:

```python
amount = evaluate_amount_for_month(slot, month, external_values)  # [R]
active = ~failed
cash[active, from_slot] -= amount[active]
cash[active, to_slot] += amount[active]
ordinary_ytd[active, profile] += amount[active]
transfer_active[month, slot, active] = True
transfer_amount[month, slot, active] = amount[active]
```

Invalid `from_slot` / `to_slot` values are guarded before touching cash
arrays.

### Tax Brackets

For one tax link, all rollouts at once:

```python
upper = ordinary_upper[link, :K]
rate = ordinary_rate[link, :K]
prev = np.concatenate(([0.0], upper[:-1]))
slice_top = np.minimum(taxable[:, None], upper[None, :])
in_bracket = np.clip(slice_top - prev[None, :], 0.0, None)
tax = (in_bracket * rate[None, :]).sum(axis=1)
```

Long-term capital gain brackets use the same idea, with
`ordinary_taxable[:, None]` as the bracket floor.

### FIFO Lot Sales

FIFO selling is the main irregular operation, but it's bounded and
tensorial. Precompute the static lot order per `(agent, account, asset)`
after filtering to eligible lots. Lots in different accounts are not
fungible unless a higher-level policy explicitly iterates over both
accounts:

```python
lot_order[agent_account_asset] = np.lexsort((lot_id, purchase_month))
```

For a target dollar sale across all rollouts:

```python
ordered_lots = lot_order[agent_account_asset]  # [L]
qty = lot_remaining[:, ordered_lots]            # [R, L]
price = unit_price[:, None]                     # [R, 1]
available_value = qty * price                   # [R, L]
available_total = available_value.sum(axis=1)   # [R]
oversell = target_dollars > available_total + epsilon
before_value = np.cumsum(available_value, axis=1) - available_value
sold_value = np.clip(
    target_dollars[:, None] - before_value,
    0.0,
    available_value,
)
sold_value[oversell, :] = 0.0
sold_units = np.divide(
    sold_value,
    price,
    out=np.zeros_like(sold_value),
    where=price > 0,
)
```

Then scatter `sold_units` back to `lot_remaining[:, ordered_lots]` and write
event buffers. Oversell rows must not be silently partial-filled: either
mark the rollout failed with a declared reason or raise a Python exception
before returning a response. Prefer rollout failure for runtime economic
outcomes so metric fans can show the distribution; reserve exceptions for
statically invalid scenario configuration. Scheduled sale oversell is a
rollout failure with a reason such as `scheduled_sale_insufficient_lots`,
not a partial sale. Because each ordered lot appears once for an
`(agent, account, asset)` group, the main FIFO scatter doesn't need
reduction. If a future shape permits duplicate target lots, use `np.add.at`
or switch the helper to JAX/PyTorch scatter APIs.

For target unit sales:

```python
available_units = qty.sum(axis=1)  # [R]
oversell = target_units > available_units + epsilon
before_units = np.cumsum(qty, axis=1) - qty
sold_units = np.clip(target_units[:, None] - before_units, 0.0, qty)
sold_units[oversell, :] = 0.0
sold_value = sold_units * price
```

For basis and gain:

```python
lot_basis_per_unit = basis_per_unit[ordered_lots]  # [L]
sold_basis = sold_units * lot_basis_per_unit[None, :]
sold_gain = sold_value - sold_basis
```

Capital gain profile updates start as explicit sums per compiled gain
profile / holding-period class. If a vectorized form has repeated group
targets, use `np.add.at` into `capital_gain_ytd[R, G, 2]`.

### Liquidity Policy Sell Order

Ordered control flow over policy and asset-order axes; vectorize inside
each step over `R`:

```python
for policy in policies:
    deficit = compute_deficit_for_policy(policy)  # [R]
    for asset_order_slot in range(A):
        available = available_value_for_asset_slot(policy, asset_order_slot)  # [R]
        sale_target = np.minimum(np.clip(deficit, 0.0, None), available)
        # the dollar-target FIFO block above, applied to this slot's pool
        sold = fifo_sell(policy, asset_order_slot, sale_target)  # [R]
        deficit -= sold
    liquidity_shortfall = deficit > epsilon
    failed |= liquidity_shortfall
    failure_reason = set_failure_reason(
        failure_reason,
        liquidity_shortfall,
        FAILURE_LIQUIDITY_INSUFFICIENT_ASSETS,
    )
```

This matches "sell assets in configured order" while avoiding a rollout
loop. The liquidity policy doesn't ask the FIFO helper for more than the
current asset slot can provide. If the whole configured sell order cannot
restore the buffer, the rollout is marked failed with a declared liquidity
failure reason.

### Obligation Settlement

Each obligation slot is compiled to a group id keyed by
`(agent_code, from_cash_slot)`. For month `m`:

```python
due[R, O] = vectorized_due_amounts(...)
group_due[R, group] = sum(due[R, O in group])
funded[R, group] = cash[R, group_cash_slot] >= group_due[R, group] - epsilon
paid[R, O] = np.where(funded[R, obligation_group[O]], due[R, O], 0.0)
shortfall[R, O] = due[R, O] - paid[R, O]
```

This preserves all-or-nothing group settlement semantics while making the
grouping explicit.

### Event Materialization

Events remain dense buffers plus active masks. Tensorized phases write the
same event facts the semantic transition produces: active mask,
amount/due/paid/shortfall, lot units/basis/proceeds, tax breakdowns,
source slot/policy attempt metadata where applicable.

Metric-fan requests never force full event decode. The core writes dense
event buffers unconditionally (cheap), but API/product layers decode events
only for selected rollout detail.

## Open work

- **Liquidity-policy oversell failure reason.** Add a test asserting that
  when the configured sell order cannot restore the cash buffer, the
  rollout is failed with a declared liquidity failure reason (not a silent
  partial fill). The engine path exists; the test gap is the open item.

## Validation Gates

Focused gate:

```bash
bazelisk test --config=nolint \
  //augur/sim:simulate_test \
  //augur/sim:projections_test \
  //augur/product:projection_fan_test \
  //augur/api:server_test
```

Before landing broad phase work:

```bash
bazelisk test --config=nolint //augur/...
```

Profile after each major slice:

```bash
bazelisk run --config=nolint \
  //augur/api:profile_metric_fan -- \
  --horizon-months=1 --rollout-count=1 --profile-output=/tmp/augur_cold.prof

bazelisk run --config=nolint \
  //augur/api:profile_metric_fan -- \
  --horizon-months=100 --rollout-count=500 --profile-output=/tmp/augur_hot.prof
```

## Open Questions

- For repeated group aggregation, is `np.add.at` fast/readable enough, or
  should we precompile dense group matrices and use masked reductions?
