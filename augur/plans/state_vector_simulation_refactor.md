# Augur state-vector simulation refactor

## The mess we have today

`augur/core/scenario_engine.py:run_scenario_vectorized` is 1400 lines.
At its top it allocates 20+ separate `(rollouts, months)` matrices, one
per facet of state:

```
cash, generic_sp500_value, generic_sp500_sale_gain, generic_sp500_sale_tax,
checking_floor_shortfall, crypto_value, crypto_sale_usd, crypto_sale_basis_usd,
remaining_crypto_quantity_by_month, remaining_crypto_basis_by_month,
private_equity_value, private_equity_sale_opportunity_value,
private_equity_sale_taxable_gain, private_equity_sale_tax,
remaining_sp500_units_by_month, remaining_sp500_basis_by_month,
remaining_private_equity_units_by_month, remaining_private_equity_basis_by_month,
private_equity_sale_usd_by_month, ...
```

Inside the per-month loop, 1D `(rollouts,)` locals are the actual
working state (`current_cash`, `remaining_sp500_units`, etc.); the
matrices are snapshot stores updated at end-of-month. State is split:
some lives in locals, some in matrices, some in property-cashflow
arrays, some in accumulators. **There is no single "state at month M"
object.**

Decisions are produced and applied in the same expression — no
separable "actions this month". A policy doesn't return "sell 50k
SP500"; it mutates `current_cash` directly. There's no decision log
that downstream code can re-derive state from.

There is a **second pass after the main loop** —
`_settle_required_cash_obligations` is called post-loop for annual_tax,
estimated_tax, partner contributions, special assessments, outside
rent. It does its own per-month sweep, reads the matrices the main
loop wrote, mutates them, forward-propagates deltas. The "linear march
through time" story is not what the code does.

The user's mental model:

```python
for month in months:
    state_t = state[month]          # (rollouts,)-per-asset frame
    actions = policy(state_t, market[month])   # (rollouts,)-per-decision frame
    state_t1 = transition(state_t, actions, market[month])
    state.append(state_t1)
```

is the right target. The current code is in the same shape at the
arithmetic level (vectorized across rollouts, sequential over months)
but the abstractions are missing.

## The target architecture

### Single working state object

```python
@dataclass(frozen=True)
class SimulationState:
    """Working state at one month boundary. All numeric fields are
    `(rollouts,)` numpy vectors; multi-asset / multi-account state lives
    in per-(account_id) / per-(asset_id) dicts of vectors. The
    simulation loop reads/writes `SimulationState` once per month;
    matrices are *derived* at end-of-simulation from the action log."}

    month_index: int  # absolute calendar month
    # Cash balances per account (account_id → (rollouts,))
    cash_by_account: pmap[str, np.ndarray]
    # Asset holdings per asset_id
    holdings: pmap[str, _AssetHolding]
    # Liability balances per liability_id
    liabilities: pmap[str, _LiabilityState]
    # Property state per property_id (depreciation taken, live mask, etc.)
    properties: pmap[str, _PropertyState]
    # Partner equity per agreement_id
    partner_equity: pmap[str, _PartnerEquityState]


@dataclass(frozen=True)
class _AssetHolding:
    asset_id: str
    asset_kind: AssetKind   # SP500 | CRYPTO_<symbol> | PRIVATE_EQUITY:<holding>
    units: np.ndarray       # (rollouts,)
    basis_usd: np.ndarray   # (rollouts,)
    # mark-to-market unit price computed on demand from market frame
```

`pmap` is `pyrsistent.pmap` (or plain `dict` if performance allows;
state is rebuilt at each step). State is **immutable** between steps —
each month produces a new `SimulationState` from the previous one + the
month's actions. No mutation in place.

### Append-only logs (the actual sources of truth)

Every state change is recorded as a row in one of these logs. State
matrices, the wire `ScenarioRunArrays`, and all downstream consumers
read from the logs (and from initial state).

#### `cashflow_log` — every change to cash, as a row

| column             | type | example                                                                                                                                                           |
| ------------------ | ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rollout_index`    | i64  | 5                                                                                                                                                                 |
| `month_index`      | i64  | 42                                                                                                                                                                |
| `account_id`       | str  | `"checking"`, `"savings"`                                                                                                                                         |
| `amount_delta_usd` | f64  | +2500 (rental income), −1247.88 (mortgage payment)                                                                                                                |
| `cause_kind`       | str  | `RENTAL_INCOME`, `MORTGAGE_PAYMENT`, `PROPERTY_TAX_SETTLEMENT`, `SP500_SALE_PROCEEDS`, `PARTNER_CONTRIBUTION`, `OBLIGATION_PAYMENT`, `ANNUAL_TAX_SETTLEMENT`, ... |
| `cause_id`         | str  | `"obligation:property_tax:rollout:5:month:42"`                                                                                                                    |
| `actor_id`         | str? | `"owner"`, `null`                                                                                                                                                 |
| `obligation_id`    | str? | linked obligation, null if not from an obligation                                                                                                                 |

Cash at month M for rollout R, account A:

```python
initial_cash[A] + cashflow_log
    .filter(pl.col("month_index") <= M)
    .filter(pl.col("rollout_index") == R)
    .filter(pl.col("account_id") == A)
    .select(pl.col("amount_delta_usd").sum())
```

In vector form (matrix derivation):

```python
cash_matrix = (cashflow_log
    .group_by(["rollout_index", "account_id", "month_index"])
    .agg(pl.col("amount_delta_usd").sum())
    .sort(["rollout_index", "account_id", "month_index"])
    .with_columns(
        balance_usd=pl.col("amount_delta_usd").cum_sum().over(["rollout_index", "account_id"])
        + pl.col("initial_balance_usd")
    )
)
```

#### `asset_change_log` — every change to an asset position

| column              | type | example                                                                              |
| ------------------- | ---- | ------------------------------------------------------------------------------------ |
| `rollout_index`     | i64  | 5                                                                                    |
| `month_index`       | i64  | 42                                                                                   |
| `asset_id`          | str  | `"sp500_brokerage"`, `"pe_holding_1"`                                                |
| `asset_kind`        | str  | `SP500`, `CRYPTO`, `PRIVATE_EQUITY`                                                  |
| `delta_units`       | f64  | −12.5 (sold) or +3.4 (bought)                                                        |
| `delta_basis_usd`   | f64  | −5000 (sold) or +1700 (bought)                                                       |
| `cash_proceeds_usd` | f64  | +5247 (sale) or −1700 (purchase)                                                     |
| `taxable_gain_usd`  | f64  | +247                                                                                 |
| `cause_kind`        | str  | `OBLIGATION_SALE`, `LIQUIDITY_SALE`, `SCHEDULED_ACQUISITION`, `INITIAL_DEPOSIT`, ... |
| `cause_id`          | str  | linked event ID                                                                      |

#### `obligations_log` — every obligation accrual + settlement

Already exists (`event_streams.OBLIGATION_LIFECYCLE_SCHEMA`). One row
per (rollout, month, obligation_type). `amount_due_usd`,
`amount_paid_usd`, derived `unpaid_amount_usd` + `status` at
materialize time.

#### `funding_decisions_log`

Already exists (`event_streams.FUNDING_DECISION_SCHEMA`). One row per
(rollout, month, obligation_type, decision_type, [policy_step,
asset_type]).

#### `liability_log` — principal/interest changes per liability

| column                 | type | example                                      |
| ---------------------- | ---- | -------------------------------------------- |
| `rollout_index`        | i64  |                                              |
| `month_index`          | i64  |                                              |
| `liability_id`         | str  | `"mortgage:property_1"`                      |
| `liability_kind`       | str  | `MORTGAGE`, `TAX_PAYABLE`                    |
| `delta_principal_usd`  | f64  | −1247.88 (amortization), +1.5M (origination) |
| `interest_accrued_usd` | f64  | per-month interest                           |
| `interest_paid_usd`    | f64  | settled interest portion of payment          |
| `cause_kind`           | str  |                                              |
| `cause_id`             | str  |                                              |

Mortgage balance at month M = initial + cum_sum(delta_principal).

#### `property_state_log` — depreciation taken, owner-occupied mask

| column                    | type | notes                             |
| ------------------------- | ---- | --------------------------------- |
| `rollout_index`           | i64  |                                   |
| `month_index`             | i64  |                                   |
| `property_id`             | str  |                                   |
| `live`                    | bool | true while held                   |
| `depreciation_basis_usd`  | f64  | current month's depreciable basis |
| `depreciation_taken_usd`  | f64  | this-month depreciation           |
| `rental_income_gross_usd` | f64  |                                   |
| `rental_active`           | bool |                                   |
| `unit_value_usd`          | f64  | current mark                      |

Property-cashflow effects (rental income, expenses, mortgage payment)
land in `cashflow_log` as separate rows; `property_state_log` is just
the depreciation/value/occupancy state.

#### `accounting_trace` — kept

The existing `AccountingTraceBuilder` and posting-schemas stay
untouched. The cashflow*log is a \_condensed* view; accounting_trace is
the full double-entry bookkeeping. They share `cause_id` so each
cashflow row can join to its accounting entries when needed.

### Market frame (exogenous environment)

Already exists in `MarketBundle` as 2D matrices per market variable.
Refactor to a single long-form polars frame:

| column          | type | notes                                                                                    |
| --------------- | ---- | ---------------------------------------------------------------------------------------- |
| `rollout_index` | i64  |                                                                                          |
| `month_index`   | i64  |                                                                                          |
| `path_kind`     | str  | `GENERIC_SP500`, `CRYPTO`, `PROPERTY_VALUE`, `RENTAL_INCOME_INDEX`, `MORTGAGE_RATE`, ... |
| `path_key`      | str  | location_id, symbol, issuer_key, etc.                                                    |
| `value`         | f64  | the multiplier / index / rate                                                            |

Engine reads `market_frame.filter(month_index == M)` as a 1D
`(rollouts,)` slice per `(path_kind, path_key)`.

### Scheduled cashflows frame (pre-computed inputs)

Many cashflows are fully determined by `(scenario, market, month)` and
don't depend on policy decisions: rental income, property
tax/HOA/insurance/maintenance accruals (NOT settlements), mortgage
payment schedule, scheduled partner contributions, scheduled property
acquisition. Pre-compute these into a frame the engine reads each
month:

| column          | type | example                                                                                            |
| --------------- | ---- | -------------------------------------------------------------------------------------------------- |
| `rollout_index` | i64  |                                                                                                    |
| `month_index`   | i64  |                                                                                                    |
| `kind`          | str  | `RENTAL_INCOME`, `PROPERTY_TAX_ACCRUAL`, `MORTGAGE_PAYMENT`, `PARTNER_CONTRIBUTION_SCHEDULED`, ... |
| `account_id`    | str? | for direct-to-cash flows                                                                           |
| `liability_id`  | str? | for liability-touching flows                                                                       |
| `amount_usd`    | f64  | positive (inflow) or specific to kind                                                              |
| `cause_id`      | str  |                                                                                                    |

Per month, the engine pulls this month's slice and applies it to state.

### Policy decisions frame (already partly there)

`event_streams.PolicyDecision` and the funding-decisions frame already
capture this. The new schema is: per `(rollout, month, decision_id)`,
what the agent chose to do this month, with a `decision_kind`
discriminator and per-kind payload columns. This is _separate_ from
`cashflow_log` because one decision can produce multiple cashflow rows
(an SP500 sale produces a SELL_PROCEEDS cashflow on the asset side AND
a CASH_PROCEEDS cashflow on the cash side AND a TAX_ACCRUAL
liability_log row).

### Initial state frame (replaces ad-hoc scenario reads)

Per (rollout, scenario), the starting balance sheet flattened:

```
initial_state:
  cash_balances:        (rollout, account_id) → balance_usd
  asset_holdings:       (rollout, asset_id, asset_kind) → (units, basis_usd)
  liabilities:          (rollout, liability_id) → principal_usd
  properties:           (rollout, property_id) → (basis_usd, value_usd, live, ...)
  partner_equity:       (rollout, agreement_id) → fields
```

Built once at start of `run_scenario_vectorized` from `Scenario`. The
working `SimulationState` at `month=−1` (start) equals the initial
state with month_index set to -1 (or 0 depending on convention).

## Order of operations within a month

The current per-month loop has these _implicit_ phases (scattered
across 250+ lines of intermixed code at `scenario_engine.py:1545+`).
The refactored loop makes them explicit:

```python
def step(state: SimulationState, market: MarketView, scheduled: ScheduledCashflows, month: int) -> SimulationStep:
    """One month's simulation step. Pure: state in, (state', logs) out.
    All ops are `(rollouts,)`-vectorized."""

    cashflow_rows: list[dict] = []
    asset_change_rows: list[dict] = []
    obligation_rows: list[dict] = []
    funding_decision_rows: list[dict] = []
    liability_rows: list[dict] = []
    property_state_rows: list[dict] = []
    decision_rows: list[dict] = []

    # 1. Mark-to-market: refresh per-holding unit prices and per-property values
    state = mark_to_market(state, market)

    # 2. Apply scheduled cashflows for this month:
    #    rental income → cashflow_log
    #    property tax / HOA / insurance / maintenance accruals → obligation_log
    #    mortgage payment → cashflow_log (cash side) + liability_log (principal/interest split)
    #    scheduled property acquisition → state.properties + cashflow_log (down payment) + liability_log (mortgage origination)
    state = apply_scheduled_cashflows(state, scheduled.at(month), logs)

    # 3. Compute this-month tax obligations from YTD state:
    #    estimated tax at quarterly markers, annual tax true-up in month 11/12 of each year
    #    → obligation_log (the obligations to settle this month)
    new_tax_obligations = compute_tax_obligations(state, scenario.tax_profile, month)
    obligation_rows.extend(new_tax_obligations)

    # 4. Settle obligations via policy chain (the meat of today's
    #    `_settle_required_cash_obligation_at_month_position`):
    for obligation in iter_open_obligations(state, this_month=True):
        decisions = run_funding_policy_chain(state, obligation, market)
        for d in decisions:
            # decisions are "pay X from cash account A" or "sell Y units of asset Z"
            cashflow_rows.extend(d.cashflow_effects())
            asset_change_rows.extend(d.asset_effects())
            funding_decision_rows.append(d.as_funding_decision_row())
            state = state.apply(d)   # rebind state with d's effects

    # 5. Within-month discretionary policies (sp500/crypto/PE sale rules,
    #    monthly spending, partner contribution policies):
    discretionary_decisions = run_discretionary_policies(state, market)
    for d in discretionary_decisions:
        cashflow_rows.extend(d.cashflow_effects())
        asset_change_rows.extend(d.asset_effects())
        decision_rows.append(d.as_policy_decision_row())
        state = state.apply(d)

    # 6. End-of-month accruals: interest accruals on liabilities, depreciation
    state, liability_accrual_rows, property_state_rows = run_eom_accruals(state, market, month)

    return SimulationStep(
        state=state,
        cashflow_rows=cashflow_rows,
        asset_change_rows=asset_change_rows,
        obligation_rows=obligation_rows,
        funding_decision_rows=funding_decision_rows,
        liability_rows=liability_rows + liability_accrual_rows,
        property_state_rows=property_state_rows,
        decision_rows=decision_rows,
    )


def simulate(scenario: Scenario, market_bundle: MarketBundle) -> ScenarioRunArrays:
    initial = build_initial_state(scenario)
    market = MarketView(market_bundle)
    scheduled = build_scheduled_cashflows(scenario, market_bundle)

    state = initial.with_month_index(-1)
    all_cashflows = []
    # ... same for each log

    for month in range(month_count):
        step = step_month(state, market, scheduled, month)
        state = step.state
        all_cashflows.extend(step.cashflow_rows)
        # ... etc

    # Derive state matrices from logs + initial state:
    cashflow_log = pl.DataFrame(all_cashflows, schema=CASHFLOW_LOG_SCHEMA)
    asset_change_log = pl.DataFrame(all_asset_changes, ...)
    cash_matrix = derive_cash_matrix(initial, cashflow_log)         # cum_sum().over(...)
    holdings_matrices = derive_holdings_matrices(initial, asset_change_log)
    liability_matrices = derive_liability_matrices(initial, liability_log)

    return ScenarioRunArrays(
        # Pydantic-tuple-shaped public surface, same as today
        obligations=...,
        funding_decisions=...,
        metric_arrays=metric_arrays_from_matrices(cash_matrix, holdings_matrices, ...),
        ...
    )
```

Note: **the post-loop pass is gone.** Quarterly estimated tax and
year-end annual tax are emitted as obligations at their respective
months _inside the main loop_ (step 3 above), and settled by the same
policy chain that handles property-cost obligations (step 4). No
second sweep needed.

## What stays untouched

- **Wire schemas (`augur/core/schemas.py`)** — the Pydantic
  `Obligation`, `FundingDecision`, `Effect`, `PolicyDecision`,
  `LotDisposition`, etc. are unchanged. They're the public API.
- **`event_streams.py` materializers** — already converts polars frames
  to Pydantic tuples on demand. New logs feed the same materializers
  (or new ones for cashflow/asset_change/liability if we ever expose
  them — likely we don't, they stay internal).
- **`market_bundle.py`** — keep its current API for generating
  multiplier paths; just add a `.as_long_frame()` convenience for the
  market frame view.
- **`AccountingTraceBuilder`** — kept as the double-entry layer.
  Cashflow rows reference accounting entries via `cause_id`.
- **Policy library (`policy_runtime.py`)** — the `Policy` subclasses
  and `_apply_*_funding_policy` helpers continue to take per-rollout
  vectors and return per-rollout vectors. The wrapper that _invokes_
  them changes; the policies themselves don't.
- **All the `_record_*` accounting recorder functions** — unchanged.
  They still get called from the corresponding decision-effect emitter.

## What goes away

- The 20+ top-of-function matrix allocations at
  `scenario_engine.py:1276+`. All derived from logs at end.
- The 1D-locals + matrix-snapshot dance inside the month loop. State
  lives in `SimulationState`, not in scattered locals + matrix columns.
- `_settle_required_cash_obligations` post-loop wrapper. Quarterly /
  annual tax obligations emit at their natural month inside the main
  loop.
- The forward-propagation delta logic we just landed in
  `_settle_required_cash_obligations`. Not needed — matrices are
  derived once at end, not maintained as working state.
- The `_PrivateEquityFundingState` helper and its in-place matrix
  mutations. PE state lives in `SimulationState.holdings` like every
  other asset.
- All the `_*_sale_action_records` lists that exist purely as
  intermediates between policy execution and accounting recording —
  replaced by reading from the action logs.

## Migration plan (in PR-sized phases)

Cannot land as one big bang — the engine is too central. Phases below
are each individually testable, each preserves the existing
`ScenarioRunArrays` Pydantic-shaped output via materializer adapters.

### Phase 0: scheduled cashflows frame

**Pre-compute the inputs.** Build `ScheduledCashflows` frame from
`scenario + market_bundle` before the main loop. Engine reads from it
each month instead of computing inline. No behavior change.

Files touched: `augur/core/scenario_engine.py` (extract a
`_build_scheduled_cashflows` function), new `augur/core/scheduled.py`
for the frame schema.

Verifies the schema works end-to-end before bigger phases land.

### Phase 1: state object — cash + sp500 + crypto

Introduce `SimulationState` with `cash_by_account`, `holdings`
(SP500 + crypto only). Engine maintains it in parallel with the
existing matrices (the matrices stay; the new state object is the
emerging source of truth).

Per-month loop:

- Construct `state` from current 1D locals at month start.
- Pass `state` to settlement (still also passes the existing 1D args
  for now).
- After settlement, sync 1D locals from `state`.

This is a "scaffolding" phase. No matrices removed yet. The new state
object proves it can carry the same info.

### Phase 2: cashflow log

Add the `cashflow_log` and `asset_change_log` builders. Every
`current_cash += X` becomes "log a row, update state". The existing
`cash` matrix is still maintained from the state object (just for
parity).

At end of simulation, build the `cash` matrix from the log via
`cum_sum().over(...)` and assert it matches the maintained version
(under `--check-derive`). Once stable, remove the maintained version
and use the derived one.

### Phase 3: PE + properties + liabilities

Same shape as Phase 1+2 but for PE holdings, property state, and
mortgage liabilities. Removes `_PrivateEquityFundingState` (folded
into `SimulationState.holdings`). Removes the property-state matrix
soup.

#### Phase 3b: detailed design (properties + mortgage liabilities)

After mapping property and mortgage state in
`scenario_engine.py`, the key finding is that **almost all
property-related and mortgage-related per-`(rollout, month)` state is
already precomputed and static during the main month loop**. There is
very little _mutable working state_ for the engine to thread through
the loop — the matrices fall out of scenario-level inputs +
amortization math + the disposition (sale-month) calculation, and
then get read but not written inside the loop. The Phase 3 plan's
"property-state matrix soup" framing is right that the matrices are
scattered; less right that they're "scratchpad". They're a
precomputed schedule.

What this means for `SimulationState`: properties and liabilities
join the state object as **views into the precomputed per-month data
at `month_position`**, not as accumulators the engine mutates inside
the loop. Same `(rollouts,)`-vector shape as cash and holdings; the
only difference is the data source (precomputed matrix slice rather
than a 1D local accumulating across iterations).

**Where the data lives today** (file:line on `scenario_engine.py`):

- `mortgage_balance` / `mortgage_interest` / `mortgage_principal` —
  `(rollouts, months+1)` `float64`, all built once at init in
  `_amortization_arrays()` (`:5674-5703`). Masked by
  `property_live_mask` post-sale. Read at `:1356-1357`,
  `:1375-1376`, `:1939-1950`, `:2184-2185`. Never written inside the
  month loop.
- `property_value` — `(rollouts, months+1)` `float64`, computed from
  the property value path + sale event (`:5660`,
  `_property_and_mortgage_arrays`). Read at `:1618`, `:1938`.
- `property_live_mask` — `(rollouts, months+1)` `float64` mask
  (1.0 alive, 0.0 post-sale). Built once at `:1352-1355` from
  `disposition.sale_month`. Multiplied into property-cost / mortgage
  values to zero them post-sale.
- `PropertyDispositionArrays.numerics` — polars frame with 16 columns
  for purchase costs, depreciation, sale proceeds, debt payoff, net
  sale cash flow. Built once before the month loop in
  `property_disposition_arrays(...)`. Read via
  `disposition.column(...)`.
- `PropertyCashFlowArrays.numerics` — polars frame with 10 columns
  (rental income, taxes, hoa, insurance, maintenance, net operating
  cash flow). Built once at `:1332-1339` via
  `_property_cash_flow_arrays()`. Read inside the loop but only as
  inputs to `ScheduledCashflows` and the property-cost obligation
  pipeline (both already framed). Never written.
- `PartnerEquityArrays.numerics` — polars frame with 15 columns
  (contributions, principal credit, equity ledger, ownership %).
  Built once at `:1369-1381`; the only in-loop mutation is one
  `replace()` call at `:1965` inside
  `_settle_partner_equity_on_property_sale(...)` that produces a new
  instance reflecting post-sale partner state. Read at `:1391`,
  `:2060`, `:2173-2174`.

**Identifiers — engine is single-property today.** The scenario carries
a scalar `property_selection.property_id: str | None`, and the
mortgage liability ID is `f"mortgage:{property_id}"`. There is no
per-rollout property variation and no list-of-properties construct.
`SimulationState.properties` is therefore a `dict[property_id, …]`
with `0` entries (no property) or `1` entry (the one property);
keying it by `property_id` keeps the shape future-proof without
forcing multi-property changes now.

**Shape of `SimulationState.properties[id]` and
`.liabilities[id]`:**

```python
@dataclass(frozen=True)
class PropertyState:
    """View of one property's per-rollout state at the current
    month boundary. All numeric fields are `(rollouts,)` vectors
    sliced from the precomputed property matrices at
    `month_position`."""

    property_id: str
    live: np.ndarray              # (rollouts,) float — 1.0 alive, 0.0 post-sale
    value_usd: np.ndarray         # current mark-to-market value
    cumulative_depreciation_usd: np.ndarray
    # The disposition columns we care to expose at the month boundary
    # (the rest stay accessible via the existing PropertyDispositionArrays
    # frame, which downstream phases will fold in).

@dataclass(frozen=True)
class LiabilityState:
    """View of one liability's per-rollout balance at the current
    month boundary."""

    liability_id: str             # e.g. "mortgage:property_1"
    liability_kind: LiabilityKind # MORTGAGE | TAX_PAYABLE
    principal_usd: np.ndarray     # current outstanding balance
    interest_accrued_this_month_usd: np.ndarray
    principal_paid_this_month_usd: np.ndarray


class LiabilityKind(StrEnum):
    MORTGAGE = "mortgage"
    TAX_PAYABLE = "tax_payable"  # future — once Phase 4 lands
```

`SimulationState` grows two fields:

```python
@dataclass(frozen=True)
class SimulationState:
    month_position: int
    cash_by_account: dict[str, np.ndarray]
    holdings: dict[str, AssetHolding]
    properties: dict[str, PropertyState]   # NEW in Phase 3b
    liabilities: dict[str, LiabilityState] # NEW in Phase 3b
```

**Construction in the engine.** Phase 3b is mechanically similar to
Phases 1 and 3a: at every `state = SimulationState(...)` site
(initial + end-of-month), populate the two new dicts.

- Initial site (before the loop, `scenario_engine.py:~1573`):
  populate `properties[property_id]` if a property is configured,
  using `property_live_mask[:, 0]`, `property_value[:, 0]`, and a
  cumulative-depreciation slice (initially zeros). Populate
  `liabilities["mortgage:{property_id}"]` from `mortgage_balance[:,
0]`, `mortgage_interest[:, 0]`, `mortgage_principal[:, 0]`.
- End-of-month site (`~:1872`): same construction but at the current
  `month` column position. The dict carries a fresh
  `PropertyState` / `LiabilityState` per iteration, each with
  `(rollouts,)` slices of the precomputed matrices.

For the no-property case (`property_id is None`), both dicts are
empty — `state.properties` is `{}`, no mortgage entry. Downstream
consumers loop over `.values()` and naturally do nothing.

**Cumulative depreciation slice.** This is the one piece of data
that needs a small derivation: today the engine reads
`disposition.column("depreciation_taken_usd")[:, month]` (which is
already cumulative per the property-sale module's output). Verify
this is monotone-nondecreasing in `month` and use it directly. If
it's not cumulative there, derive via `cumsum` over
`property_depreciation_usd` in the disposition frame at Phase 3b
init time.

**No matrix is removed in Phase 3b.** Mortgage matrices,
`property_value`, and the disposition / partner*equity / property-
cash-flow frames stay as they are; `SimulationState` reads from
them. Phase 6 (matrix-derivation) and the eventual decision-log
refactor are where these get either replaced (mortgage payments
become entries on the liability log, not a precomputed schedule) or
kept as canonical inputs (the property value path is genuinely an
input, not state). That's intentional — Phase 3b is the
\_architectural* placement; physical replacement comes later.

**Tax payable is deferred to Phase 4.** Today there is no
tax-payable liability tracked in the engine — tax accrual happens
inside `AccountingTraceBuilder` as posting entries against
`TAX_PAYABLE` chart accounts, but nothing surfaces it as a per-month
balance matrix. Phase 4 (collapsing the post-loop pass into the main
month loop) is the natural place to introduce a `LiabilityKind.
TAX_PAYABLE` entry: at quarter-marker and year-end months, the new
single-pass loop will compute tax obligations from YTD income +
estimated-payment ledger and emit obligations _and_ update the tax-
payable liability balance. Phase 3b's
`LiabilityKind` enum has a `TAX_PAYABLE` member as the seam for
this; no engine code reads it yet.

**Read sites — none change in Phase 3b.** The engine continues to
read mortgage / property arrays directly. Phase 3b is purely the
state-object scaffold for these fields; later phases (Phase 5+) will
swap individual read sites to go through `state.liabilities[…]` /
`state.properties[…]`.

**`_settle_partner_equity_on_property_sale` and partner equity.**
Partner equity is _not_ a liability — it's an ownership-stake
ledger. Folding it into `SimulationState` is a separate question
(see "Out of scope" below). The one in-loop `replace()` mutation at
`:1965` produces a new `PartnerEquityArrays`; in a future phase the
PartnerEquityArrays + the single mutation site become a per-rollout
ownership-stake update with a clean log row. For Phase 3b, leave
partner equity alone.

**Files touched in Phase 3b:**

- `augur/core/simulation_state.py` — add `LiabilityKind`,
  `PropertyState`, `LiabilityState` dataclasses; add `properties`
  and `liabilities` fields to `SimulationState` plus
  `state.property(id)` / `state.liability(id)` accessor helpers.
- `augur/core/simulation_state_test.py` — unit-test the new
  dataclasses and accessors with simple fixtures.
- `augur/core/scenario_engine.py` — populate the two new dicts at
  the two `state = SimulationState(...)` construction sites. No
  read-side changes; matrices unchanged.
- `augur/core/BUILD.bazel` — no dep changes (numpy + dataclass
  only).

**Verification.**

1. `bazelisk test //augur/core:simulation_state_test
//augur/core:scenario_engine_test //augur/core:test_e2e
//augur/core:backend_test //augur/core:property_sale_test
//augur/core:annual_tax_test` — all green.
2. Add a small test in `simulation_state_test` that constructs
   `SimulationState` with one property and one mortgage liability
   and verifies `state.property(id).value_usd` / `state.liability(
id).principal_usd` round-trip the input `(rollouts,)` arrays.
3. Bench unchanged within ±5% (the only new work is two `dict`
   constructions per month with one-element dicts — negligible).

**Out of scope for Phase 3b (followups):**

- Partner equity as a state-object field. The single in-loop
  mutation site at `:1965` and the `PartnerEquityArrays.replace()`
  pattern want a small design pass before fitting them into the
  state object. Tracked as a Phase 3c if it lands separately.
- Multi-property support. Engine is single-property today; the
  `dict`-keyed shape is forward-compatible but no engine code today
  iterates more than one property. Tracked as a future scenario-
  schema change.
- Read-site migration. Replacing inline matrix reads
  (`mortgage_principal[:, month]`, `property_value[:, month]`, …)
  with `state.liability(...).principal_usd` / `state.property(...).
value_usd` is mechanical and lands in Phase 5 alongside the
  derive-from-logs work; no value adding it here.

### Phase 4: post-loop pass elimination

Move annual*tax and estimated_tax obligation emission \_inside* the
main loop at their natural months. Settlement is the same policy
chain used for property-cost obligations. Delete
`_settle_required_cash_obligations` (post-loop wrapper).

This is the biggest behavioral change. Needs careful test
preservation: snapshot tests on `Obligation` tuples for tax kinds,
order of operations between estimated and annual tax true-up.

### Phase 5: derive everything

Switch all metric matrices to be derived from logs at end-of-sim. The
main loop only updates `SimulationState`; matrices fall out of
`derive_*` calls. Delete the per-month snapshot block at
`:1818-1826`.

### Phase 6: cleanup

Delete dead code, simplify signatures, retire intermediate dataclasses
that were only there to thread state through nested functions. Engine
shrinks substantially (target: `run_scenario_vectorized` ≤ 400 lines).

## Test strategy

Each phase preserves `ScenarioRunArrays` bit-for-bit on the existing
`test_e2e` suite. Each phase adds focused tests:

- Phase 0: snapshot test on `ScheduledCashflows` frame for a
  representative scenario.
- Phase 1: assert `SimulationState.cash_by_account["checking"]`
  matches `cash[:, month]` matrix at each month for `test_e2e`
  scenarios.
- Phase 2: assert derived `cash` matrix from log equals maintained
  `cash` matrix.
- Phase 3: same for holdings, liabilities, properties.
- Phase 4: snapshot test on obligations for a multi-year scenario
  comparing pre- and post-refactor tuple sequences (must be
  identical).
- Phase 5: deletion test — assert no remaining `[:, month]`
  matrix-write inside the per-month loop body.

## Out of scope

- **Per-rollout policy parameter divergence** — currently policies are
  same across rollouts. The refactor keeps that. If we want
  per-rollout policy variation later, the action-log shape supports
  it natively.
- **Multi-step intra-month dependencies** that current policy chain
  needs (cash → SP500 → crypto → PE fallback). Stays sequential
  within the month; the chain emits multiple decisions, each
  rebinding state.
- **Replacing `MarketBundle` provider system.** Stays the same; just
  add a long-frame view convenience.
- **Replacing `AccountingTraceBuilder`.** The double-entry layer is
  orthogonal to the state-vector refactor.
- **Performance.** Bench tolerance ±15% from current 1.06s baseline
  per phase. The refactor is correctness/clarity-driven; if we lose
  more than that, address with column-major bulk inserts before
  landing the phase.

## Effort estimate

- Phase 0: 1 PR, ~2 days
- Phase 1: 2 PRs (cash, then holdings), ~3 days each
- Phase 2: 1 PR, ~3 days (log infra + parity check)
- Phase 3: 2 PRs (PE, properties+liabilities), ~3 days each
- Phase 4: 1 PR, ~4 days (most behaviorally risky)
- Phase 5: 1 PR, ~2 days (deletion + matrix derivation)
- Phase 6: 1 PR, ~2 days (cleanup)

Total: ~9 PRs over ~3 weeks.
