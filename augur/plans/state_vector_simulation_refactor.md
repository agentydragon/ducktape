# Augur state-vector simulation refactor

## Status (2026-05-19)

The user-facing simulation correctness goals are met. The engine is now:

- A single forward-only month loop. No post-hoc passes. Every obligation
  (tax, mortgage, special-assessment, outside-rent, partner-contribution)
  settles inline in the month it's due.
- Vectorized across rollouts (every per-month op is `(rollouts,)` numpy).
- A single past→future DAG. Sales create gain, gain feeds tax accrual,
  tax becomes an obligation, obligation settles → cash drops. All in the
  iteration that observes the sale.
- One source of truth for the year-tax math (`TaxActor`), one source for
  the per-sale-kind allocation (`annual_sale_tax_allocation` reads
  `TaxActor.year_federal_tax_usd` / `year_california_tax_usd` directly —
  no duplicate bracket walk per year).
- One unified asset-change log (`augur/core/action_log.py:
  ASSET_CHANGE_LOG_SCHEMA`) feeds every per-asset-class gain matrix via
  `derive_per_month_taxable_gain_matrix(...)`. SP500, crypto, PE, and
  property all flow through it. Crypto is taxed (lot disposition
  `tax_expense_usd` is populated, `ReportMetric.CRYPTO_SALE_TAX_USD`
  surfaces it, `TaxActor` includes crypto in long-term capital gain).

What's _not_ yet there is the structural / data-modeling end-state — the
"plain `state → policy(state) → action → state'` " shape, with the
per-month state held in one `SimulationState` object and the per-month
matrices derived from append-only logs at end of simulation. The
remaining waves below carry that work.

`scenario_engine.py` is at 6308 LOC. The target is ≤400 once the logs
fully replace the maintained matrices and policies return decisions
instead of mutating locals.

## What landed

Each entry below is one commit (or one tight series) on the working
branch. Headings group by the conceptual area the work touches.

**State object scaffolding.** `SimulationState`, `AgentState`,
`AssetHolding`, `PropertyState`, `PropertyStake`, `LiabilityBalance` in
`simulation_state.py`. Constructed per-iteration from the engine's 1D
locals via `_build_state(...)`; consumed by the end-of-month snapshot
block to write matrix `[:, M]` columns. Not yet the source of truth —
the locals are still upstream.

**Long-form log infrastructure.** `CASHFLOW_LOG_SCHEMA` +
`derive_cash_matrix(...)`, `PROPERTY_STATE_SCHEMA` +
`build_property_state_frame(...)`, `ASSET_CHANGE_LOG_SCHEMA` +
`derive_per_month_taxable_gain_matrix(...)` in `action_log.py`.
`asset_change_log` is actively used (every gain matrix derives from it);
`cashflow_log` is built only for the scheduled-cashflow portion and not
yet read by the engine.

**Tax actor.** `TaxActor` in `tax_actor.py` accumulates taxable events
per year, emits quarterly + year-end obligations inline at IRS markers.
Filing-status-aware safe-harbor. Crypto gain folded into the
long-term-capital-gain accumulator. Per-year `year_federal_tax_usd` /
`year_california_tax_usd` cached so `annual_sale_tax_allocation` doesn't
re-call `federal_income_tax_due_usd` / `california_income_tax_due_usd`.

**Inline obligation settlement.** Mortgage, special-assessment,
outside-rent, estimated-tax, annual-tax, and partner-contribution
obligations all settle inside the main month loop via
`_settle_required_cash_obligation_at_month_position` (1D state in,
`_ObligationSettlementResult` out). Property-cost obligations settle
BEFORE the tax block so their forced asset sales feed the year-tax
accrual. Partner contributions use a per-agreement
`_PartnerSettlementContext` carrying the contributing actor's 1D state.

**Dead-code purge.** The post-loop sweep wrapper
`_settle_required_cash_obligations` (with its `[:, M+1:]` forward-write
delta pattern) is gone. The PE settlement-funding branch (which was the
last `[:, M:]` scratchpad — and was dead code: the matrix it read was
zero-initialized and the forward-write computed `0 − 0 = 0` at every
month) is gone, along with `_PrivateEquityFundingState`,
`_apply_pe_checking_floor_obligation_funding_policy`, and
`PrivateEquityObligationFundingPolicyApplication`. Dead pre-loop
allocations of matrices reassigned post-loop (`generic_sp500_sale_tax`,
`private_equity_sale_taxable_gain`, etc.) are gone.

**Per-month value matrices derived.** `generic_sp500_value` and
`crypto_value` matrices are computed once post-loop as `units × multipliers`
instead of being maintained imperatively each iteration.

**Regression test.** `test_property_cost_driven_sp500_sales_show_up_in_year_tax`
pins the property-cost → SP500 sale → year-tax flow. (The migration
exposed a real bug — mortgage settlement initially ran AFTER
`TaxActor.observe_month`, dropping the gain. Fixed by reordering
property-cost obligations before the tax block.)

## Remaining gaps

Numbered for cross-referencing in commits and follow-up plans. Effort
estimates are calendar days of focused engineering, not wall-clock.

### G1. State object is not the source of truth

`SimulationState` is rebuilt from the engine's 1D locals every iteration
and consumed only by the end-of-month snapshot block. All other engine
code reads the 1D locals (`current_cash`, `remaining_sp500_units`, …)
directly. The intended end-state — every read goes through
`state.agent(...).cash(...)` / `state.agent(...).holding(...)` — is not
there. **Effort:** 3 days (G1) plus 1 day to drop the locals once all
reads are routed (G1b). **Blocks:** dropping the 1D locals (the engine
collapse that takes it under 400 LOC depends on this).

### G2. Cash matrix not log-derived

The `cash[:, M]` matrix is maintained by `cash[:, month] = current_cash`
in the snapshot block. The cashflow log infrastructure exists but the
engine doesn't emit to it. Target: wire every `current_cash += X` site
(scheduled cashflows, policy steps, settlement returns) to emit a
`cashflow_log` row; derive `cash` from log + initial cash at end of
simulation; keep a parity assertion against the maintained matrix for
one release; then drop the maintained matrix. **Effort:** 2 days.
**Blocks:** dropping `current_cash` (G1b).

### G3. Policies mutate state directly

Every policy in `policy_runtime.py` mutates engine 1D locals in-place
(`current_cash = current_cash + sale_usd`). There's no "actions this
month" object. The plan's `policy(state) → Decision` / `state' =
state.apply(d)` shape is not there. **Effort:** 3-5 days (one of the
biggest items; touches every policy class). **Blocks:** the user's
original "for month in months: state → policy → action → state'"
control-flow shape.

### G4. Sale-record lists duplicate the asset_change_log

`sp500_sale_action_records`, `crypto_sale_action_records`,
`private_equity_sale_action_records` are Python lists of dataclasses
maintained alongside the polars `asset_change_log` frame. The journal-
entry / lot-disposition / effect recording loops iterate the lists, not
the frame. Migrate consumers to read from the frame; delete the lists.
**Effort:** 2 days.

### G5. `LiabilityKind.TAX_PAYABLE` declared but unused

The enum variant exists in `simulation_state.py` but `TaxActor` doesn't
surface accrued-but-unpaid tax as a `LiabilityBalance`. Real accounting
posts a `TAX_ACCRUAL` JE (debit `TAX_EXPENSE`, credit `TAX_PAYABLE`) at
year-end; settlement debits `TAX_PAYABLE` and credits `CHECKING_CASH`.
The chart-account balances already track this — what's missing is the
`AgentState.liabilities["tax_payable"]` view on `SimulationState`.
**Effort:** ½ day. **Caveat:** event-based-accrual model means mid-year
balance is `0` until the year-end accrual fires, then jumps to
year-tax-minus-paid-so-far. Document this rather than fudge it.

### G6. Per-month tax allocation still in `annual_sale_tax_allocation`

Year-tax math runs once per (scenario, year) via `TaxActor`, but the
per-month allocation matrices (`federal_income_tax_usd`,
`generic_sp500_sale_tax_usd`, `generic_crypto_sale_tax_usd`,
`private_equity_sale_tax_usd`, `property_sale_tax_usd`,
`rental_income_tax_usd`) are still produced by a separate
`annual_sale_tax_allocation` function — which accepts the precomputed
totals from `TaxActor` but otherwise does its own year-loop. Move the
allocation into `TaxActor`; delete `annual_sale_tax_allocation`. **Effort:**
1 day.

### G7. Per-month accumulator matrices (sale_usd, sale_basis_usd) still imperative

`crypto_sale_usd`, `crypto_sale_basis_usd`,
`private_equity_sale_usd_by_month` are written via `[:, M] +=`
accumulator-style. These aren't the scratchpad pattern (they only touch
`[:, M]`, not `[:, M:]`), but they're per-month sums of events the
`asset_change_log` already records. Derive post-loop instead. **Effort:**
½ day.

### G8. Remaining state matrices maintained imperatively

`remaining_sp500_units_by_month`, `remaining_sp500_basis_by_month`,
`remaining_crypto_quantity_by_month`, `remaining_crypto_basis_by_month`,
`remaining_private_equity_units_by_month`,
`remaining_private_equity_basis_by_month` are written by the
end-of-month snapshot block from the 1D locals. Derive post-loop from
`asset_change_log` (initial + cumulative deltas) once G1 + G2 land and
the snapshot block is gone. **Effort:** ½ day, gated on G1/G2.

### G9. Engine ≤400 LOC target

`scenario_engine.py` is 6308 LOC; `run_scenario_vectorized` is ~600 LOC
inside it. The target was ≤400 for the whole module. Falls out of G1 +
G2 + G3 + G8: once state lives in one object, policies return actions,
and matrices derive from logs, the engine collapses to roughly the shape
shown in [Order of operations within a month](#order-of-operations-within-a-month)
below. **Effort:** 1 day of cleanup after the above land.

## Roadmap

Ordered by `(impact × tractability)` descending. Each row is one PR.

| #   | Gap     | Effort | Risk | Notes                                                                   |
| --- | ------- | ------ | ---- | ----------------------------------------------------------------------- |
| 1   | G5      | ½ d    | low  | Small, concrete, makes `state.liabilities` honest.                      |
| 2   | G6      | 1 d    | low  | Eliminates the `annual_sale_tax_allocation` redundancy.                 |
| 3   | G7      | ½ d    | low  | Append-only cleanup; no behavior change.                                |
| 4   | G4      | 2 d    | med  | Touches every journal-entry / effect recorder. Snapshot test gates it.  |
| 5   | G2      | 2 d    | med  | Cashflow-log wire-up + parity check + drop maintained matrix.           |
| 6   | G1+G1b  | 4 d    | high | State-as-truth + drop 1D locals. Biggest control-flow shift.            |
| 7   | G3      | 4 d    | high | Policies-as-actions. Touches every policy.                              |
| 8   | G8      | ½ d    | low  | Derive remaining matrices; gated on G2.                                 |
| 9   | G9      | 1 d    | low  | Engine collapse + cleanup pass.                                         |

Total: ~15–18 days, 9 PRs.

The dependency structure is:

```
  G5  ─────┐
  G6  ─────┤
  G7  ─────┼─►  G4  ─►  G2  ─►  G1  ─►  G1b  ─►  G8  ─►  G9
  (small)  │   (lists)  (cash)  (state) (drop)  (matrices) (collapse)
           │
           └──► (G3 policies-as-actions can land any time after G1)
```

The small items (G5, G6, G7) are independent and can land first as
quick wins. G4 → G2 → G1 → G1b → G8 → G9 is the structural spine. G3
can land in parallel with G2 once G1 is in.

## Target architecture

The end-state the gaps above push toward.

### Single working state object

`SimulationState` is the working representation at one month boundary:

```python
@dataclass(frozen=True)
class SimulationState:
    month_position: int
    agents: dict[str, AgentState]            # by actor_id
    properties: dict[str, PropertyState]     # by property_id

@dataclass(frozen=True)
class AgentState:
    actor_id: str
    cash_by_account: dict[str, np.ndarray]   # account_id → (rollouts,)
    holdings: dict[str, AssetHolding]        # asset_id → holding
    liabilities: dict[str, LiabilityBalance] # liability_id → balance
    property_stakes: dict[str, PropertyStake]
```

All numeric fields are `(rollouts,)` numpy vectors. Frozen dataclass +
plain dicts; immutability is structural (rebuilt each step), not via
persistent-map data structures. One entry per agent — owner-plus-partner
adds a second agent, partner usually only carries
`property_stakes`. (This is mostly in place via `simulation_state.py`;
G1 makes the engine actually _read_ from it instead of treating it as a
write target.)

### Order of operations within a month

The intended pure-functional shape (G1 + G3 deliver this):

```python
def step(state: SimulationState, market: MarketView, scheduled: ScheduledCashflows, month: int) -> SimulationStep:
    """state in → (state', logs) out. All ops (rollouts,)-vectorized."""

    # 1. Mark-to-market — refresh holding values and property values.
    state = mark_to_market(state, market)

    # 2. Scheduled cashflows for this month (rental income, mortgage payment,
    #    property cost accruals, scheduled property acquisitions).
    state, rows = apply_scheduled_cashflows(state, scheduled.at(month))

    # 3. Tax obligations from YTD state (quarterly + year-end markers).
    state, rows = accrue_tax_obligations(state, scenario.tax_profile, month)

    # 4. Settle required obligations via policy chain (property cost, tax,
    #    partner contribution). Each policy returns Decisions; the engine
    #    applies them.
    state, rows = settle_required_obligations(state, market)

    # 5. Discretionary policies (SP500 / crypto / PE sale rules, monthly
    #    spend, partner-equity accruals).
    state, rows = run_discretionary_policies(state, market)

    # 6. End-of-month accruals (mortgage interest, depreciation).
    state, rows = run_eom_accruals(state, market, month)

    return SimulationStep(state=state, logs=rows)


def simulate(scenario, market_bundle) -> ScenarioRunArrays:
    state = build_initial_state(scenario)
    market = MarketView(market_bundle)
    scheduled = build_scheduled_cashflows(scenario, market_bundle)
    all_logs: list[Row] = []

    for month in range(month_count):
        step = step_month(state, market, scheduled, month)
        state = step.state
        all_logs.extend(step.logs)

    # Derive the wire-shape matrices from the logs.
    cashflow_log = pl.DataFrame(all_logs.cashflow_rows, schema=CASHFLOW_LOG_SCHEMA)
    asset_change_log = pl.DataFrame(all_logs.asset_change_rows, ...)
    cash_matrix = derive_cash_matrix(initial, cashflow_log)
    holdings = derive_holdings_matrices(initial, asset_change_log)
    return materialize_result(cash_matrix, holdings, asset_change_log, ...)
```

**The post-loop pass is gone today.** Quarterly + year-end tax are
emitted inline as obligations at their markers (step 3) and settled by
the same chain that handles property-cost obligations (step 4).

### Append-only logs as source of truth

```
cashflow_log:        (rollout, month, actor_id, account_id, amount_delta_usd, cause_kind, cause_id)
asset_change_log:    (rollout, month, actor_id, asset_id, asset_kind, delta_units,
                      delta_basis_usd, cash_proceeds_usd, taxable_gain_usd, tax_treatment,
                      cause_kind, cause_id)
obligations_log:     (rollout, month, obligation_type, actor_id, creditor_id,
                      amount_due_usd, amount_paid_usd, unpaid_amount_usd, source_policy_id, required)
funding_decisions_log: per-decision detail (which source funded, sale or cash, shortfall)
liability_log:       (rollout, month, actor_id, liability_id, liability_kind, property_id,
                      principal_delta_usd, interest_accrued_this_month_usd, principal_paid_this_month_usd)
property_stake_log:  per-(agent, property) stake changes (partner-equity accruals)
```

Per-month state matrices are derivable views over the logs:

```python
cash_balance = (
    cashflow_log.group_by(["rollout_index", "actor_id", "account_id", "month_index"])
        .agg(pl.col("amount_delta_usd").sum())
        .with_columns(
            balance_usd=initial_balance_usd
            + pl.col("amount_delta_usd").cum_sum().over(["rollout_index", "actor_id", "account_id"])
        )
)
```

The wire-shape `(rollouts, months)` matrices in `ScenarioRunArrays` are
projections of these long-form frames at materialize time — wire
compatibility preserved without baking owner-only or single-asset
shapes into the schemas.

### Today's matrices are projections

The `cash` `(rollouts, months)` matrix on `ScenarioRunArrays` corresponds
to:

```python
cash_balance_frame
    .filter(pl.col("actor_id") == primary_owner_actor_id,
            pl.col("account_id") == "checking")
    .pivot(values="balance_usd", index="rollout_index", on="month_index")
    .to_numpy()
```

`remaining_sp500_units_by_month` projects out the
`(actor_id=primary_owner, asset_id="sp500")` slice of
`asset_holding_frame.units`. Cardinality (multiple agents / multiple
properties / multiple crypto symbols) goes into row count, not column
count. The schema cost of adding agents/properties/assets is one extra
key column per dimension — fixed.

## Test strategy

Each PR preserves `ScenarioRunArrays` bit-for-bit on the existing
`test_e2e` suite. Targeted tests for each gap:

- **G2 cashflow log** — assertion `derived_cash_matrix == maintained_cash_matrix`
  to the maintained matrix (gated by a `--check-derive` flag for one
  release before deleting the maintained side).
- **G4 sale-record lists drop** — snapshot test on lot-dispositions for
  a multi-asset multi-sale scenario before/after the migration.
- **G5 TAX_PAYABLE** — assert `state.agent(owner).liability("tax_payable").principal_usd`
  matches `cum(accrued) − cum(paid)` for a multi-year scenario with
  staggered quarterly + annual payments.
- **G1 state-as-truth** — assert engine reads through `state.agent(...)`
  match the 1D-locals values throughout a representative scenario.
  Deletion test: no remaining `[:, month]` matrix-write inside the
  per-month loop body once G8 lands.

`test_property_cost_driven_sp500_sales_show_up_in_year_tax` already
pins the cross-cutting property-cost → year-tax flow; keep it in the
green set on every PR.

## What stays untouched

- **Wire schemas (`augur/core/schemas.py`)** — Pydantic `Obligation`,
  `FundingDecision`, `Effect`, `PolicyDecision`, `LotDisposition`, etc.
  are the public API. Unchanged.
- **`event_streams.py` materializers** — already converts polars frames
  to Pydantic tuples on demand. New logs feed the same materializers.
- **`market_bundle.py`** — keep its current API for generating multiplier
  paths; add a `.as_long_frame()` convenience for long-frame views.
- **`AccountingTraceBuilder`** — the double-entry layer is orthogonal
  to the state-vector refactor. Stays.

## Out of scope

- **Per-rollout policy parameter divergence** — policies are still the
  same across rollouts. The action-log shape supports per-rollout
  variation natively if it's ever wanted.
- **Multi-step intra-month policy chain** — stays sequential; the chain
  emits multiple decisions per month, each rebinding state.
- **Replacing `MarketBundle` provider system** — orthogonal.
- **Replacing `AccountingTraceBuilder`** — orthogonal.
- **Performance** — bench tolerance ±15% per PR. Refactor is
  correctness/clarity-driven; if a PR loses more than that, address
  with column-major bulk inserts before landing.
