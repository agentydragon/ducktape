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
directly. The intended end-state — every read goes through the
working polars frames on `SimulationState` (filter to
`(actor_id, account_id)` to get a one-row-per-rollout column of cash
balances; same shape for holdings, liabilities, stakes) — is not
there.

**G1 also migrates the leaf storage** from the nested-dataclass-of-
numpy scaffold in `simulation_state.py` to the polars long-form
frames documented in [Single working state object](#single-working-state-object--polars-long-form-frames).
This is the canonical shape; doing it incrementally (numpy first,
polars later) means writing the engine's read sites twice. The
polars shape lines up directly with the persistent logs (working
state IS the cross-section of the log at month M), so the
log-emission step collapses to `log.extend(decision_frame)`.

**Effort:** 4 days (G1 — read-site migration to polars frames) plus 1
day to drop the 1D locals once all reads are routed (G1b).
**Blocks:** dropping the 1D locals (the engine collapse that takes it
under 400 LOC depends on this).

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
| 6   | G1+G1b  | 5 d    | high | Migrate working state to polars long-form frames + drop 1D locals.      |
| 7   | G3      | 4 d    | high | Policies-as-actions. Touches every policy.                              |
| 8   | G8      | ½ d    | low  | Derive remaining matrices; gated on G2.                                 |
| 9   | G9      | 1 d    | low  | Engine collapse + cleanup pass.                                         |

Total: ~16–19 days, 9 PRs.

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

### Single working state object — polars long-form frames

The rollout dimension is the bulk dimension. The working state at one
month boundary is held as a small number of **polars long-form
frames** — one per state kind — each keyed by `(rollout_index, …)`
with rollouts represented as rows. Every per-month operation — policy
decisions, transitions, accruals — is a polars expression that
vectorizes over the rollout column, the same way today's
`current_cash + sale_usd` vectorizes over a `(rollouts,)` numpy array.

Why polars, not nested-dataclass-of-numpy:

- The persistent append-only logs (`cashflow_log`, `asset_change_log`,
  `liability_log`, `obligations_log`, …) are **already** long-form
  polars. Having the working state in the same shape collapses the
  decision-emission step to "compute the new row(s), append to log" —
  no nested-dict ↔ row materialization in between.
- Multi-agent / multi-account / multi-asset / multi-property scales by
  row count, not by widening the schema. The cost of supporting a
  second agent or a second crypto symbol is zero schema-side; it's
  just more rows on the same frames.
- Joins, aggregations, group-bys are the natural shape of most
  per-month operations (cash impact of all this month's decisions:
  group decisions by `(rollout, account)`, sum `amount_delta_usd`,
  join into cash frame, add). The dict-of-arrays shape forces these
  into manual numpy reductions.
- The non-goal — no Python loop over rollouts — holds: polars'
  expression engine vectorizes over a column the same way numpy
  vectorizes over a `(rollouts,)` array.

Numpy stays viable inside individual cells (a column's underlying
storage is numpy; pulling a column to numpy, doing one ufunc, and
putting it back is fine for hot-path arithmetic kernels). But the
state-read / state-write **surface** is polars.

The working frames at one month boundary:

```
cash_balance_frame:    (rollout_index, actor_id, account_id, balance_usd)
asset_holding_frame:   (rollout_index, actor_id, asset_id, asset_kind,
                        units, basis_usd)
liability_frame:       (rollout_index, actor_id, liability_id,
                        liability_kind, property_id?, principal_usd,
                        interest_accrued_this_month_usd,
                        principal_paid_this_month_usd)
property_stake_frame:  (rollout_index, actor_id, property_id,
                        ownership_pct, contribution_used_usd,
                        equity_ledger_usd)
property_state_frame:  (rollout_index, property_id, live, value_usd,
                        cumulative_depreciation_usd)
rollout_status_frame:  (rollout_index, status, failure_month?)
```

Same schemas as the persistent long-form frames (see [Append-only logs
as source of truth](#append-only-logs-as-source-of-truth) below) —
**the working frame at month M IS the cross-section of the persistent
frame at month M, computed by running the cumulative balance up to
M**. There's only one schema per state kind.

`SimulationState` wraps these frames into a single typed bundle so
read sites can use stable accessors:

```python
@dataclass(frozen=True)
class SimulationState:
    month_position: int
    cash:        pl.DataFrame   # cash_balance_frame, filtered to current month
    assets:      pl.DataFrame   # asset_holding_frame ...
    liabilities: pl.DataFrame   # liability_frame ...
    stakes:      pl.DataFrame   # property_stake_frame ...
    properties:  pl.DataFrame   # property_state_frame ...
    rollouts:    pl.DataFrame   # rollout_status_frame ...
```

Reads are polars filters/joins; writes are polars `with_columns` + new
`SimulationState` (frames are cheap to clone — only the column
references move, the underlying chunks are shared).

A sale policy expressed against this shape:

```python
def sell_when_under_floor(state: SimulationState, market: MarketView, policy: SellPolicy) -> pl.DataFrame:
    """Returns one decision row per rollout that wants to sell, with
    schema (rollout_index, actor_id, asset_id, sale_usd)."""
    return (
        state.cash
        .filter(pl.col("actor_id") == policy.actor_id, pl.col("account_id") == "checking")
        .join(
            state.assets.filter(pl.col("actor_id") == policy.actor_id, pl.col("asset_id") == "sp500"),
            on="rollout_index",
        )
        .join(market.sp500_price_at(state.month_position), on="rollout_index")
        .with_columns(sp500_value=pl.col("units") * pl.col("unit_price"))
        .with_columns(
            requested=pl.when(pl.col("balance_usd") < float(policy.floor_usd))
            .then(pl.lit(float(policy.sale_amount_usd)))
            .otherwise(pl.lit(0.0)),
        )
        .with_columns(sale_usd=pl.min_horizontal("requested", "sp500_value"))
        .filter(pl.col("sale_usd") > 0)
        .select(["rollout_index"])
        .with_columns(
            actor_id=pl.lit(policy.actor_id),
            asset_id=pl.lit("sp500"),
            sale_usd=pl.col("sale_usd"),
        )
    )
```

Bulk-vectorized over rollouts via polars' expression engine; no
Python loop over rollouts; the output is already the
`asset_change_log` row shape (modulo a few derived columns), so it
appends directly to the log.

Failed rollouts stay in the frames. `rollout_status_frame` carries the
failure flag; every subsequent operation joins against it and masks
out failed rollouts at materialize time. No structural removal.

One entry per agent — owner-plus-partner adds rows with
`actor_id="partner"` to whichever frames it participates in (usually
only `property_stake_frame`). Adding a third agent, a second cash
account, a second crypto symbol, a second property: all just more
rows, same schemas.

The scaffolding in `simulation_state.py` today is the
nested-dataclass-of-numpy shape; G1 migrates it to polars long-form
frames as the canonical working state.

### Order of operations within a month

The intended pure-functional shape (G1 + G3 deliver this). `state` here
is the **one** `SimulationState` for this month, holding polars
long-form frames keyed by `rollout_index`. Every line in the function
body is a polars expression over those frames; there is no
`for rollout in ...` loop:

```python
def step(state: SimulationState, market: MarketView, scheduled: ScheduledCashflows, month: int) -> SimulationStep:
    """state in → (state', logs) out. Every op is a polars expression
    over rollout-keyed frames. The rollout dimension is silent in this
    function body, the same way it's silent in today's
    `current_cash + sale_usd`."""

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

## Non-goal: per-rollout objects

`SimulationState` is one object **per month**, not one **per rollout**.
The rollout dimension lives inside it as the `rollout_index` column of
the working polars frames. If at any point during this refactor
someone reaches for `for rollout in range(N): ...` or a
`list[SimulationState]` indexed by rollout, that's a bug in the
refactor — back out and find the polars-expression formulation.
Per-month operations are vectorized over `rollout_index` the same way
today's `current_cash + sale_usd` is a numpy add over all rollouts.
Polars' expression engine vectorizes over a column the same way numpy
vectorizes over a `(rollouts,)` array.

A failed rollout is not removed from the working frames. Failure is
a row in `rollout_status_frame`; subsequent operations join against it
and mask out failed rollouts at materialize time.

## Out of scope

- **Per-rollout policy parameter divergence** — policies are still the
  same across rollouts. The action-log shape supports per-rollout
  variation natively if it's ever wanted (each decision is already a
  `(rollouts,)` vector; the policy_id and other discriminators just
  happen to be scalar today).
- **Multi-step intra-month policy chain** — stays sequential; the chain
  emits multiple decisions per month, each rebinding state.
- **Replacing `MarketBundle` provider system** — orthogonal.
- **Replacing `AccountingTraceBuilder`** — orthogonal.
- **Performance** — bench tolerance ±15% per PR. Refactor is
  correctness/clarity-driven; if a PR loses more than that, address
  with column-major bulk inserts before landing.
