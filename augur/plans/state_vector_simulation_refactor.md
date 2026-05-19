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

### Single working state object — agent-centric

```python
@dataclass(frozen=True)
class SimulationState:
    """Working state at one month boundary. All numeric fields are
    `(rollouts,)` numpy vectors. `agents` is keyed by actor_id;
    `properties` is keyed by property_id and carries the shared
    real-world facts about each property (every agent sees the same
    property value, depreciation, live status). The simulation loop
    reads/writes `SimulationState` once per month; persistent frames
    are *derived* at end-of-simulation from the action logs."""

    month_position: int
    agents: dict[str, AgentState]            # by actor_id
    properties: dict[str, PropertyState]     # by property_id

@dataclass(frozen=True)
class AgentState:
    """Per-agent state: accounts they own, assets they hold, debts
    they owe, stakes they hold in shared properties."""
    actor_id: str
    cash_by_account: dict[str, np.ndarray]   # account_id → (rollouts,) balance
    holdings: dict[str, AssetHolding]        # asset_id → AssetHolding
    liabilities: dict[str, LiabilityState]   # liability_id → LiabilityState
    property_stakes: dict[str, PropertyStake] # property_id → PropertyStake

@dataclass(frozen=True)
class AssetHolding:
    asset_id: str
    asset_kind: AssetKind  # GENERIC_SP500 | CRYPTO | PRIVATE_EQUITY
    units: np.ndarray      # (rollouts,)
    basis_usd: np.ndarray  # (rollouts,)

@dataclass(frozen=True)
class PropertyState:
    """Per-property facts shared across all agents at this month."""
    property_id: str
    live: np.ndarray                         # (rollouts,) — 1.0 alive, 0.0 post-sale
    value_usd: np.ndarray                    # current mark-to-market
    cumulative_depreciation_usd: np.ndarray

@dataclass(frozen=True)
class PropertyStake:
    """One agent's relationship to one property at the current month."""
    property_id: str
    ownership_pct: np.ndarray
    contribution_used_usd: np.ndarray
    equity_ledger_usd: np.ndarray

@dataclass(frozen=True)
class LiabilityState:
    """A debt owed by an agent. `property_id` is non-null when the
    liability is secured against a property (mortgages); None for
    unsecured liabilities (tax_payable, ...)."""
    liability_id: str
    liability_kind: LiabilityKind  # MORTGAGE | TAX_PAYABLE
    property_id: str | None
    principal_usd: np.ndarray
    interest_accrued_this_month_usd: np.ndarray
    principal_paid_this_month_usd: np.ndarray
```

Plain `dict` for the per-step nesting (state is rebuilt at each step,
so immutability is structural via the frozen dataclass — not via
persistent maps). Each step produces a new `SimulationState` from the
previous one + the month's actions; no in-place mutation.

**Single-actor scenarios** still go through `state.agents` — the dict
has one entry keyed by `primary_owner_actor_id`. Owner-plus-partner
scenarios add a second entry for the partner; the partner typically
has empty `cash_by_account` / `holdings` / `liabilities` and only a
`property_stakes` entry.

### Derived persistent frames (long-form, one per kind)

The in-memory `SimulationState` is the _working representation_;
persistent state is held in **long-form polars frames** that
downstream consumers (materializers, fan charts, the wire
`ScenarioRunArrays` shape) read from. Each frame is keyed by
`(rollout_index, month_index, ...entity-id-columns...)` with one row
per leaf in the `SimulationState` tree:

```
cash_balance_frame:
  rollout_index  i64
  month_index    i64
  actor_id       str
  account_id     str
  balance_usd    f64

asset_holding_frame:
  rollout_index  i64
  month_index    i64
  actor_id       str
  asset_id       str
  asset_kind     str   -- SP500 | CRYPTO | PRIVATE_EQUITY (discriminator)
  units          f64
  basis_usd      f64

liability_frame:
  rollout_index                    i64
  month_index                      i64
  actor_id                         str
  liability_id                     str
  liability_kind                   str  -- MORTGAGE | TAX_PAYABLE
  property_id                      str? -- non-null when secured
  principal_usd                    f64
  interest_accrued_this_month_usd  f64
  principal_paid_this_month_usd    f64

property_stake_frame:
  rollout_index          i64
  month_index            i64
  actor_id               str
  property_id            str
  ownership_pct          f64
  contribution_used_usd  f64
  equity_ledger_usd      f64

property_state_frame:
  rollout_index                 i64
  month_index                   i64
  property_id                   str
  live                          f64
  value_usd                     f64
  cumulative_depreciation_usd   f64
```

**No column duplication per entity.** Adding a partner doesn't widen
the schema (no `partner_cash_usd` column) — it adds rows where
`actor_id="partner"`. Adding a second property doesn't widen the
schema — it adds rows where `property_id="property_2"`. Asset kinds
(SP500/crypto/PE) don't get their own columns; they're a
discriminator value in `asset_kind`, and `units` / `basis_usd` carry
whatever units the kind expresses. Cardinality goes into row count,
not column count. The schema cost of supporting any number of
agents/accounts/assets/properties is **one extra key column per
dimension** — fixed.

### Today's dense matrices are projections

The current `cash` `(rollouts, months)` matrix corresponds to:

```python
cash_balance_frame
    .filter(pl.col("actor_id") == primary_owner_actor_id,
            pl.col("account_id") == "checking")
    .pivot(values="balance_usd", index="rollout_index", on="month_index")
    .to_numpy()
```

i.e. a 2D slice for one specific `(actor_id, account_id)` pair.
`remaining_sp500_units_by_month` projects out the
`(actor_id=primary_owner, asset_id="sp500")` slice of
`asset_holding_frame.units`, and so on. Wire compatibility with
`ScenarioRunArrays` is preserved by projecting these views at
materialize time; the frame underneath generalizes to multi-account /
multi-asset / multi-agent without re-shaping the wire schema.

### From in-memory state to persistent frames

The state-frame schemas above don't need to be built by walking
`SimulationState` snapshots month-by-month. They get derived once
at end-of-simulation from the **append-only logs** below, with
running balances computed by `cum_sum().over(...)`:

```python
cash_balance_frame = (
    cashflow_log
    .group_by(["rollout_index", "actor_id", "account_id", "month_index"])
    .agg(pl.col("amount_delta_usd").sum())
    .with_columns(
        balance_usd=initial_balance_usd
        + pl.col("amount_delta_usd").cum_sum().over(
            ["rollout_index", "actor_id", "account_id"]
        )
    )
)
```

Same shape derivation applies to `asset_holding_frame` (cumulative
deltas over `asset_change_log`), `liability_frame` (over
`liability_log`), and `property_stake_frame` (over
`property_stake_log`). `property_state_frame` is built from scenario
inputs + market paths, not from a log — its rows are facts, not
events.

### Append-only logs (the actual sources of truth)

Every state change is recorded as a row in one of these logs. State
matrices, the wire `ScenarioRunArrays`, and all downstream consumers
read from the logs (and from initial state).

#### `cashflow_log` — every change to cash, as a row

| column             | type | example                                                                                                                                                           |
| ------------------ | ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rollout_index`    | i64  | 5                                                                                                                                                                 |
| `month_index`      | i64  | 42                                                                                                                                                                |
| `actor_id`         | str  | `"owner"`, `"partner"` — owning agent of the account                                                                                                              |
| `account_id`       | str  | `"checking"`, `"savings"`                                                                                                                                         |
| `amount_delta_usd` | f64  | +2500 (rental income), −1247.88 (mortgage payment)                                                                                                                |
| `cause_kind`       | str  | `RENTAL_INCOME`, `MORTGAGE_PAYMENT`, `PROPERTY_TAX_SETTLEMENT`, `SP500_SALE_PROCEEDS`, `PARTNER_CONTRIBUTION`, `OBLIGATION_PAYMENT`, `ANNUAL_TAX_SETTLEMENT`, ... |
| `cause_id`         | str  | `"obligation:property_tax:rollout:5:month:42"`                                                                                                                    |
| `obligation_id`    | str? | linked obligation, null if not from an obligation                                                                                                                 |

Cash at month M for rollout R, agent A, account C:

```python
initial_cash[A, C] + cashflow_log
    .filter(pl.col("month_index") <= M)
    .filter(pl.col("rollout_index") == R)
    .filter(pl.col("actor_id") == A)
    .filter(pl.col("account_id") == C)
    .select(pl.col("amount_delta_usd").sum())
```

In vector form (running-balance derivation):

```python
cash_balance_frame = (cashflow_log
    .group_by(["rollout_index", "actor_id", "account_id", "month_index"])
    .agg(pl.col("amount_delta_usd").sum())
    .sort(["rollout_index", "actor_id", "account_id", "month_index"])
    .with_columns(
        balance_usd=pl.col("amount_delta_usd").cum_sum().over(
            ["rollout_index", "actor_id", "account_id"]
        )
        + pl.col("initial_balance_usd")
    )
)
```

> **Phase 2 caveat.** The Phase 2 cashflow log scaffold in
> `augur/core/action_log.py` does _not_ yet include the `actor_id`
> column — scheduled cashflows fold to a single-account log keyed by
> `account_id` only. Add `actor_id` as a non-null column (and rebuild
> the fold + `derive_cash_matrix` to group by it) before Phase 5
> wires emission from the policy chain, since by then multi-agent
> scenarios start producing rows for different actors.

#### `asset_change_log` — every change to an asset position

Capital gains across asset classes (SP500, crypto, PE, property) are
**all the same shape** with different `asset_kind` discriminators and
different tax treatments. Today the engine has three parallel lists of
sale-action records (`sp500_sale_action_records`,
`crypto_sale_action_records`, `private_equity_sale_action_records`)
plus the property sale's gain columns on `disposition`, plus separate
matrices per asset class for the gain aggregates. They're all the same
shape: one event per (rollout, month, asset) realizing a
`proceeds − basis = gain` quantity that flows into the year's tax
computation. Unify them into one append-only event log:

| column              | type | example                                                                              |
| ------------------- | ---- | ------------------------------------------------------------------------------------ |
| `rollout_index`     | i64  | 5                                                                                    |
| `month_index`       | i64  | 42                                                                                   |
| `actor_id`          | str  | `"owner"` — owning agent of the asset                                                |
| `asset_id`          | str  | `"sp500_brokerage"`, `"pe_holding_1"`                                                |
| `asset_kind`        | str  | `GENERIC_SP500`, `CRYPTO`, `PRIVATE_EQUITY`, `PROPERTY`                              |
| `delta_units`       | f64  | −12.5 (sold) or +3.4 (bought)                                                        |
| `delta_basis_usd`   | f64  | −5000 (sold) or +1700 (bought)                                                       |
| `cash_proceeds_usd` | f64  | +5247 (sale) or −1700 (purchase)                                                     |
| `taxable_gain_usd`  | f64  | +247 (proceeds − basis; negative for losses; 0 for purchases)                        |
| `tax_treatment`     | str? | `LONG_TERM_CAPITAL`, `SHORT_TERM_CAPITAL`, `DEPRECIATION_RECAPTURE_1250`, null       |
| `cause_kind`        | str  | `OBLIGATION_SALE`, `LIQUIDITY_SALE`, `SCHEDULED_ACQUISITION`, `INITIAL_DEPOSIT`, ... |
| `cause_id`          | str  | linked event ID                                                                      |

**Property sales emit two rows**, one per tax treatment. The federal
§1250 depreciation recapture caps at the 25% recapture rate while the
appreciation portion is `LONG_TERM_CAPITAL` — they belong to different
treatment buckets even though both come from the same disposition
event. Splitting at the event-log level means the tax computation
filters by `tax_treatment` without needing to know about §1250's
quirks per asset class.

**Year-end tax is a group-by on the event log**, not a sum of separate
per-asset-class matrices:

```python
def year_taxable_amounts(events: pl.DataFrame, year: int) -> dict[str, np.ndarray]:
    year_events = events.filter(pl.col("month_index").floordiv(12) == year)
    return {
        treatment: (
            year_events.filter(pl.col("tax_treatment") == treatment)
                .group_by("rollout_index")
                .agg(pl.col("taxable_gain_usd").sum())
                .sort("rollout_index")["taxable_gain_usd"].to_numpy()
        )
        for treatment in ("LONG_TERM_CAPITAL", "DEPRECIATION_RECAPTURE_1250", "SHORT_TERM_CAPITAL")
    }
```

`federal_income_tax_due_usd` and `california_income_tax_due_usd` then
take those bucket sums — they don't know or care that the gains came
from four different asset classes. Today's
`annual_sale_tax_allocation` constructs the same buckets implicitly by
summing per-asset-class matrices; the unified log makes the bucketing
explicit and removes the parallel structures.

**The bug this fixes.** Today
`generic_sp500_sale_gain[:, month] = sp500_sale - sp500_basis` at
`scenario_engine.py:1955` **overwrites** the matrix from
within-month-policy-chain locals only. Property-cost obligation
settlement happens earlier in the same iteration, sells SP500 to fund
the obligation, and appends to `sp500_sale_action_records` — but
never updates `sp500_sale` / `sp500_basis` (those reset to zero at
`:1820`). Result: a property-cost-driven SP500 sale with a real gain
leaves the gain matrix at zero, and `annual_sale_tax_allocation`
reports zero SP500 tax for the month. The unified-log shape derives
`generic_sp500_sale_gain` (and any other per-asset-class gain matrix
downstream consumers still want) by filter+group_by on the events,
which automatically picks up every sale regardless of which code path
triggered it.

**What consolidates / goes away:**

- `sp500_sale_action_records`, `crypto_sale_action_records`,
  `private_equity_sale_action_records` (parallel lists) →
  one `asset_change_log` frame.
- `generic_sp500_sale_gain[:, month] = sp500_sale - sp500_basis` and
  any other imperative per-asset-class gain matrices →
  derived views: `.filter(asset_kind == X).group_by("rollout_index",
"month_index").agg(sum(taxable_gain_usd))`.
- `disposition.column("taxable_property_capital_gain_usd")` and
  `disposition.column("depreciation_recapture_usd")` (precomputed
  property-side columns) → derived views over `asset_change_log`
  filtered to property rows by `tax_treatment` bucket.
- The `_tax_share_for_sale_action` helper's `source_taxable_income`
  denominator is replaced by the events-frame group-by sum so it
  always matches the action's numerator regardless of which code
  path triggered the sale.

**Implementation ordering note.** The `taxable_gain_usd` for a sale
event is `proceeds − basis_sold` for the units that left the position.
Computing that requires knowing the basis attribution at sale time —
average cost, lot-by-lot FIFO, or whatever the asset class uses today.
The current per-asset-class action-record builders already do this
work; the migration is to emit a row in the unified log rather than
appending to a per-asset-class list. The `LotDisposition` lot-level
detail can either stay as a parallel structure (it's a fine-grained
event already) or be derived from the same source.

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
| `actor_id`             | str  | `"owner"` — agent who owes this liability    |
| `liability_id`         | str  | `"mortgage:property_1"`                      |
| `liability_kind`       | str  | `MORTGAGE`, `TAX_PAYABLE`                    |
| `property_id`          | str? | non-null when secured against a property     |
| `delta_principal_usd`  | f64  | −1247.88 (amortization), +1.5M (origination) |
| `interest_accrued_usd` | f64  | per-month interest                           |
| `interest_paid_usd`    | f64  | settled interest portion of payment          |
| `cause_kind`           | str  |                                              |
| `cause_id`             | str  |                                              |

Mortgage balance at month M = initial + `cum_sum(delta_principal).over(
"rollout_index", "actor_id", "liability_id")`.

#### `property_stake_log` — partner-equity-style stake changes per (agent, property)

| column                        | type | example                                     |
| ----------------------------- | ---- | ------------------------------------------- |
| `rollout_index`               | i64  |                                             |
| `month_index`                 | i64  |                                             |
| `actor_id`                    | str  | `"owner"`, `"partner"`                      |
| `property_id`                 | str  |                                             |
| `delta_contribution_used_usd` | f64  | this-month change in contribution           |
| `delta_equity_ledger_usd`     | f64  | this-month change in equity ledger          |
| `ownership_pct_after`         | f64  | snapshot after the event (rebased on sale)  |
| `cause_kind`                  | str  | `PARTNER_CONTRIBUTION`, `STAKE_SALE_REBASE` |
| `cause_id`                    | str  |                                             |

`ownership_pct` is logged as a snapshot rather than a delta because the
post-sale `_settle_partner_equity_on_property_sale(...)` mutation
rebases the ledger rather than additively adjusting it — additive
deltas don't compose cleanly across the sale boundary.

#### `property_state_log` — depreciation taken, occupancy, value

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

No `actor_id` — properties are shared real-world objects, not
agent-owned. Multi-agent ownership is captured by per-agent
`property_stake_log` rows, not by replicating property facts.
Property-cashflow effects (rental income, expenses, mortgage payment)
land in `cashflow_log` as separate rows with the receiving agent's
`actor_id`; `property_state_log` is just the
depreciation/value/occupancy state.

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

> **Followup before Phase 5 emission lands.** The Phase 2 scaffold
> shipped (`augur/core/action_log.py`) without an `actor_id` column —
> scheduled cashflows fold to a single-account log keyed only by
> `account_id`. Add `actor_id` to `CASHFLOW_LOG_SCHEMA` (non-null),
> thread it through `build_cashflow_log_from_scheduled(...)` and
> `derive_cash_matrix(...)` (group by it in the `cum_sum`), and do
> the same up-front for `asset_change_log` / `liability_log` /
> `property_stake_log` when those land. Without it, Phase 5 hits
> multi-agent scenarios producing rows for different actors and the
> derivation collapses them.

### Phase 3: PE + properties + liabilities

Same shape as Phase 1+2 but for PE holdings, property state, and
mortgage liabilities. Removes `_PrivateEquityFundingState` (folded
into `SimulationState.holdings`). Removes the property-state matrix
soup.

#### Phase 3b: agent-centric SimulationState restructure

**The structural insight.** Properties exist as shared real-world
objects; each agent (actor) has its own cash accounts, asset
holdings, debts, and a _stake_ in each property. Today the engine
flattens this into a single-actor view with a top-level
`PartnerEquityArrays` frame bolted on. The cleaner shape:

```python
@dataclass(frozen=True)
class SimulationState:
    """Working state at one month boundary. `agents` is keyed by
    actor_id; `properties` is keyed by property_id and holds the
    shared facts about each property (value, depreciation taken,
    live mask). All per-agent / per-property numerics are
    `(rollouts,)` vectors sliced at this month_position."""

    month_position: int
    agents: dict[str, AgentState]            # by actor_id
    properties: dict[str, PropertyState]     # by property_id

@dataclass(frozen=True)
class AgentState:
    actor_id: str
    cash_by_account: dict[str, np.ndarray]   # accounts owned by this agent
    holdings: dict[str, AssetHolding]        # SP500 / crypto / PE this agent owns
    liabilities: dict[str, LiabilityState]   # debts this agent owes
    property_stakes: dict[str, PropertyStake] # this agent's stake in each property

@dataclass(frozen=True)
class PropertyState:
    """Per-property facts shared across all agents."""
    property_id: str
    live: np.ndarray                         # (rollouts,) — 1.0 alive, 0.0 post-sale
    value_usd: np.ndarray                    # current mark-to-market
    cumulative_depreciation_usd: np.ndarray  # cumulative through this month

@dataclass(frozen=True)
class PropertyStake:
    """One agent's relationship to one property at the current month."""
    property_id: str
    ownership_pct: np.ndarray                # (rollouts,) — this agent's %
    contribution_used_usd: np.ndarray        # cumulative contribution
    equity_ledger_usd: np.ndarray            # current ledger value
    # (additional fields read off PartnerEquityAgreementArrays
    # as we land them — see "Partner-equity fold-in" below)

@dataclass(frozen=True)
class LiabilityState:
    """A debt owed by an agent. `property_id` is set when the
    liability is secured against a property (mortgages); None for
    unsecured liabilities (tax_payable, ...)."""
    liability_id: str                        # e.g. "mortgage:property_1"
    liability_kind: LiabilityKind            # MORTGAGE | TAX_PAYABLE
    property_id: str | None                  # secured-against link, None if unsecured
    principal_usd: np.ndarray                # current outstanding balance
    interest_accrued_this_month_usd: np.ndarray
    principal_paid_this_month_usd: np.ndarray


class LiabilityKind(StrEnum):
    MORTGAGE = "mortgage"
    TAX_PAYABLE = "tax_payable"   # seam for Phase 4 — not populated by 3b
```

**Why agent-centric.** Owner-managed scenarios have one actor; the
agent dict has one entry and call sites read
`state.agents[primary_owner_actor_id].cash("checking")`. Owner-plus-
partner scenarios have two agents; the owner holds cash + holdings

- liabilities + a stake in the property, the partner holds just a
  stake. Cash, holdings, and liabilities never split across agents —
  they belong to one. Properties never split across agents — they
  exist as a shared world-fact. Partner equity is exactly the stake
  relationship, which fits naturally on `AgentState.property_stakes`
  rather than as a top-level frame.

**Mortgages live on the borrowing agent.** A mortgage is a personal
debt secured by a property; conceptually it's owed by the entity
holding the title (today's engine: the primary owner). So
`agents[primary_owner].liabilities["mortgage:property_1"]` carries
the principal/interest, and the `property_id` field on
`LiabilityState` records which property it's secured against. The
property-stake's `equity_ledger_usd` already nets the secured debt
to give equity; the secured-debt link is recorded but not double-
counted.

**Identifiers.**

- `actor_id` — already a stable scenario-level string for each
  actor (today: `primary_owner_actor_id`; partner scenarios add the
  partner's actor_id).
- `property_id` — scalar `scenario.property_selection.property_id`
  (engine is single-property today). Dict keying is forward-
  compatible.
- `liability_id` — `f"mortgage:{property_id}"` for mortgages; same
  scheme today.
- Cash account_id — today `"checking"` for the primary owner; same
  scheme as Phase 1.

**Where the data lives today** (file:line on `scenario_engine.py`):

- `mortgage_balance` / `mortgage_interest` / `mortgage_principal` —
  `(rollouts, months+1)` `float64`, all built once at init in
  `_amortization_arrays()` (`:5674-5703`). Masked by
  `property_live_mask` post-sale. Read at `:1356-1357`,
  `:1375-1376`, `:1939-1950`, `:2184-2185`. Never written inside
  the loop.
- `property_value` — `(rollouts, months+1)` `float64`, computed
  from the property value path + sale event (`:5660`,
  `_property_and_mortgage_arrays`). Read at `:1618`, `:1938`.
- `property_live_mask` — `(rollouts, months+1)` `float64` (1.0
  alive, 0.0 post-sale). Built once at `:1352-1355` from
  `disposition.sale_month`.
- `PropertyDispositionArrays.numerics` — polars frame with 16
  columns (purchase costs, depreciation, sale proceeds, debt
  payoff, net sale cash flow). Built once before the loop. Read
  via `disposition.column(...)`. Includes a
  `depreciation_taken_usd` column we slice for
  `cumulative_depreciation_usd`.
- `PartnerEquityArrays.numerics` — polars frame with 15 columns
  (contributions, principal credit, equity ledger, ownership %).
  Built once at `:1369-1381`; one in-loop `replace()` at `:1965`
  inside `_settle_partner_equity_on_property_sale(...)` produces a
  new instance reflecting post-sale partner state. Read at
  `:1391`, `:2060`, `:2173-2174`.

**Partner-equity fold-in.** `PartnerEquityArrays` already carries
the per-(rollout, month) data for every stake field; Phase 3b
slices its columns at `month_position` to populate each
`PropertyStake`. The one in-loop `replace()` at `:1965` (post-sale
ledger reset) means at the iteration boundary right after the sale
month, the stake reflects the post-sale state — Phase 3b just
reads from `PartnerEquityArrays` at slice time, so the rebind
automatically flows through.

For a single-actor (owner-only) scenario, the owner still has a
`PropertyStake` with `ownership_pct = 1.0` everywhere. This keeps
ownership math uniform across single- and multi-actor scenarios.

**Construction in the engine.** Phase 3b replaces both `state =
SimulationState(...)` sites with the agent-centric form. The Phase
1/3a `cash_by_account` and `holdings` move _inside_
`AgentState(actor_id=primary_owner_actor_id, ...)`:

```python
state = SimulationState(
    month_position=month,
    agents={
        primary_owner_actor_id: AgentState(
            actor_id=primary_owner_actor_id,
            cash_by_account={cash_account_id: current_cash},
            holdings={
                "sp500": AssetHolding(...),
                "crypto": AssetHolding(...),
                "private_equity": AssetHolding(...),
            },
            liabilities=(
                {f"mortgage:{property_id}": LiabilityState(
                    liability_id=f"mortgage:{property_id}",
                    liability_kind=LiabilityKind.MORTGAGE,
                    property_id=property_id,
                    principal_usd=mortgage_balance[:, month],
                    interest_accrued_this_month_usd=mortgage_interest[:, month],
                    principal_paid_this_month_usd=mortgage_principal[:, month],
                )}
                if property_id is not None
                else {}
            ),
            property_stakes=(
                {property_id: PropertyStake(
                    property_id=property_id,
                    ownership_pct=partner_equity.column("owner_ownership_pct")[:, month],
                    contribution_used_usd=partner_equity.column(
                        "owner_contribution_used_usd"
                    )[:, month],
                    equity_ledger_usd=partner_equity.column(
                        "owner_equity_ledger_usd"
                    )[:, month],
                )}
                if property_id is not None
                else {}
            ),
        ),
        # Plus a partner AgentState if scenario has one — empty
        # cash/holdings/liabilities, just property_stakes.
        **partner_agents,
    },
    properties=(
        {property_id: PropertyState(
            property_id=property_id,
            live=property_live_mask[:, month],
            value_usd=property_value[:, month],
            cumulative_depreciation_usd=disposition.column(
                "depreciation_taken_usd"
            )[:, month],
        )}
        if property_id is not None
        else {}
    ),
)
```

Same construction at the initial-state site (column 0) and the
end-of-month site (column `month`).

**No matrices removed.** Mortgage matrices, `property_value`,
disposition / partner-equity frames stay as they are;
`SimulationState` reads from them. Phase 5 / 6 are where reads
migrate through state and matrices become derived.

**Tax payable seam.** `LiabilityKind.TAX_PAYABLE` is declared but
not populated by Phase 3b. Phase 4 adds tax-payable entries to
`agents[actor_id].liabilities["tax_payable"]` when the post-loop
pass collapses into the main loop and quarterly / year-end tax
obligations emit at their natural months.

**Files touched in Phase 3b:**

- `augur/core/simulation_state.py` — restructure:
  add `AgentState`, `PropertyState`, `PropertyStake`,
  `LiabilityState`, `LiabilityKind`. Move `cash_by_account` and
  `holdings` from `SimulationState` into `AgentState`. Convenience
  accessors: `state.agent(actor_id)`, `state.property(property_id)`,
  `agent.cash(account_id)`, `agent.holding(asset_id)`,
  `agent.liability(liability_id)`, `agent.stake(property_id)`.
- `augur/core/simulation_state_test.py` — extend tests to cover
  the agent-centric shape: single-actor scenario, owner+partner
  scenario, no-property scenario.
- `augur/core/scenario_engine.py` — rewrite both `state =
SimulationState(...)` call sites to construct the
  `AgentState` / `PropertyState` / `PropertyStake` /
  `LiabilityState` tree. No read-side changes.
- `augur/core/BUILD.bazel` — no dep changes.

**Verification.**

1. `bazelisk test //augur/core:simulation_state_test
//augur/core:scenario_engine_test //augur/core:test_e2e
//augur/core:backend_test //augur/core:property_sale_test
//augur/core:annual_tax_test` — all green.
2. Tests: round-trip `(rollouts,)` arrays through the new
   accessors; assert single-actor scenarios produce one agent
   entry, owner+partner produce two.
3. Bench within ±5% of current baseline.

**Out of scope for Phase 3b (followups):**

- Multi-property support. Engine is single-property today; the
  `dict`-keyed shape is forward-compatible but no engine code
  iterates more than one property yet.
- Read-site migration. Replacing inline matrix reads with
  `state.agent(...).liability(...).principal_usd` etc. lands in
  Phase 5 alongside derive-from-logs work.
- `TAX_PAYABLE` liability population. Lands in Phase 4 with the
  post-loop pass collapse.
- Decision-log entries for partner-equity changes (the post-sale
  `replace()` becomes a stake-update row on the action log).
  Lands later alongside the cashflow log emission work.

### Phase 4: post-loop pass elimination

Move annual*tax and estimated_tax obligation emission \_inside* the
main loop at their natural months. Settlement is the same policy
chain used for property-cost obligations. Delete
`_settle_required_cash_obligations` (post-loop wrapper).

This is the biggest behavioral change. Needs careful test
preservation: snapshot tests on `Obligation` tuples for tax kinds,
order of operations between estimated and annual tax true-up.

**Sequencing constraint.** Phase 4 wiring (TaxActor observing
per-month taxable events) depends on the unified `asset_change_log`
landing first. Today's
`generic_sp500_sale_gain[:, month] = sp500_sale - sp500_basis`
overwrite at `scenario_engine.py:1955` misses property-cost-driven
SP500 sales' gains (they only flow through `sp500_sale_action_records`,
which the within-month-policy-chain locals don't touch); the same
shape risk exists for crypto and PE on the post-loop sale paths. The
TaxActor needs a single source of truth for per-month gains
regardless of which code path triggered the sale, which is exactly
what the unified `asset_change_log` provides. Land that first:

1. **Phase 4a — unify the sale-event logs.** Replace the three
   per-asset-class action-record lists with a single
   `asset_change_log` frame keyed by `asset_kind` + `tax_treatment`.
   Property sale emits two rows (LONG_TERM_CAPITAL +
   DEPRECIATION_RECAPTURE_1250). Delete the per-month gain matrix
   overwrites; derive them as filter+group_by views on the events
   frame. Fixes the property-cost-driven SP500-gain bug as a side
   effect. Behavior change: previously untaxed property-cost-driven
   SP500 sales now correctly land in the year's tax. Tests likely
   to flag this; the bug-fix-on-purpose framing is the right
   defense.
2. **Phase 4b — TaxActor wiring.** Insert TaxActor observation +
   obligation emission inside the main loop, reading the per-month
   gain buckets from the unified events frame. Delete the post-loop
   `_settle_required_cash_obligations` calls for estimated_tax and
   annual_tax. Keep the other post-loop kinds (partner_contribution,
   special_assessment, outside_rent) for now — each can become its
   own actor later but isn't blocking.

   **Open architectural question — year-0 quarterly safe-harbor with no
   prior-year tax.** Today's `_quarterly_estimated_tax_obligation_due_usd`
   uses 90% of year-0's actual total tax (the IRS first-year exception)
   for Q1-Q4 of year 0 when `tax_profile.prior_year_tax_usd` is None.
   This relies on **forward knowledge** — the simulation precomputes the
   full year-0 tax via `annual_sale_tax_allocation` post-loop, then
   places 0.9/4 of that amount at each year-0 quarterly marker
   retroactively. A true inline observer cannot do this without either:
   - a **two-pass simulation** (pass 1 just observes to fill TaxActor's
     year accumulators; pass 2 emits obligations using known totals
     — ~2x runtime),
   - **changing test expectations** so year-0 quarterlies are zero
     when no prior_year_tax is supplied and the full residual settles
     at year-end (semantically more honest — you don't actually know
     this year's tax in April; the IRS just won't penalize you under
     the first-year exception),
   - or some pre-loop estimate (e.g., a scenario-attribute
     `expected_year_zero_tax_usd` distinct from `prior_year_tax_usd`).

   The wired Phase 4b prototype showed the inline TaxActor + accumulator
   - per-iteration observe pattern works for the common case (years
     N ≥ 1, prior-year tax known) and matches today's bit-for-bit. The
     3 e2e tests that fail are all year-0-no-prior-year-tax scenarios.
     The decision on which solution to take is the gating item for
     Phase 4b landing.

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
