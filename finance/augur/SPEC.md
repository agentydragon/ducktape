# Augur — Specification

Augur is a probabilistic simulator of a multi-agent economic system. Given a
**scenario** — a bundle of agents, assets, liabilities, external series, and policies —
it produces a distribution over trajectories of state (net worth, cash,
ownership shares, taxes, liquidity events, …) by sampling many rollouts.

This document specifies the entity model, the per-rollout evaluation loop, and
the user-visible guarantees. Implementation details live in code; SPEC.md only
records what an outside observer can rely on.

## Architecture Boundary

Augur separates exogenous path generation from path evaluation. `augur/model` owns
evidence ingestion, calibration, fitted-model identity, stochastic sampling,
and exogenous-model provenance. `augur/sim` evaluates scenario sets over already
materialized exogenous trajectories. `augur/api` adapts product requests into
model + simulation inputs and shapes responses for the frontend.

Compatibility adapters may exist during migration, but the durable contract is
the `model -> sim -> api -> frontend` boundary rather than the legacy wire
shapes.

The current product-language API surface is intentionally narrow. A
`ScenarioKey` describes one cash-spend scenario without randomness, and product
view requests add a compact rollout seed window (`first_seed`, `rollout_count`).
The product route also includes
deployment-configured portfolio sources that resolve to initial cash and
public-security lots as passive mark-to-market holdings; those holdings are
config/source facts, not frontend knobs. The product funding policy
can list supported sellable buckets in order, currently `public_securities`,
and can request the simulator's cash-buffer rule: when post-obligation cash is
below a dollar trigger, sell a fixed dollar amount from that order. The
product portfolio route returns the resolved initial cash and public-security
positions, including tax lots, as a read-only product surface. The metric-fan
route returns compact requested percentiles. Drill-down routes return one full
per-seed table plus product-readable event rows for a selected rollout, either
by explicit seed or by resolving a requested terminal percentile server-side.
Drill-down responses include details for only that selected rollout, such as
public-security sales, monthly expense settlements, and rollout failures.
Missing rollouts are transparently sampled and simulated into an in-memory
server cache. Product concepts that are neither in the request type nor
deployment config are not supported by the product endpoint yet.

## Model

### Entities

| Entity           | What it is                                                                                                                                                                                                    | Examples                                                                                                                               |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `Agent`          | An economic actor with state (cash, holdings, liabilities, ownership shares) and a set of policies.                                                                                                           | a primary owner, a lender, a tenant                                                                                                    |
| `Asset`          | Something an agent owns that has value. Discriminated subtype determines valuation and liquidity model.                                                                                                       | a `LiquidSecurity` tracking the `security:SPY` series, a `PrivateEquity` holding, a `RealEstate` property                              |
| `Liability`      | A debt an agent owes, with an amortization schedule.                                                                                                                                                          | a mortgage on a property                                                                                                               |
| `ExternalSeries` | An exogenous trajectory source generated outside the simulator and consumed as per-rollout paths.                                                                                                             | security price paths, local home-price paths, local rent paths, CPI, mortgage rate, per-`PrivateEquity` price + liquidity-event stream |
| `Policy`         | A typed rule attached to an agent: `(state, external_series, time) → list[Instruction]`. Composable; an agent can hold any number.                                                                            | liquidity-reserve maintenance, max-concentration rebalancing, mortgage payment, rental management                                      |
| `Instruction`    | A policy-emitted intent (e.g. "sell N units of asset X"). Validated and applied by the engine into an `Effect`.                                                                                               | `SellInstruction`, `BorrowInstruction`                                                                                                 |
| `Effect`         | A realized state mutation after validation. The trace records effects, not the raw instructions.                                                                                                              | `SellSp500Effect`, `SellCryptoEffect`, `SellPrivateEquityEffect`, `SettlePropertySaleEffect`                                           |
| `Obligation`     | A first-class cash demand on an actor (tax, mortgage, property tax, HOA, insurance, maintenance, outside rent, special assessment, estimated tax). Settled via the funding-policy chain or fails the rollout. | annual tax due at year-end, monthly property tax                                                                                       |
| `Scenario`       | A bundle: agents + assets + liabilities + initial state + policies + required external series + horizon.                                                                                                      | "primary buys property X and rents rooms while living there"                                                                           |

### Asset subtypes

| Subtype          | Valuation                                                                                                         | Liquidity                                                                                 |
| ---------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `Cash`           | Face value.                                                                                                       | Always liquid.                                                                            |
| `LiquidSecurity` | Tracks an external-series multiplier (e.g. the `security:SPY` proxy).                                             | Always sellable.                                                                          |
| `RealEstate`     | Tracks location-bound home-value and rent paths; has property tax / insurance / HOA / maintenance / depreciation. | Sellable on demand; sale incurs closing costs, capital-gains tax, depreciation recapture. |
| `PrivateEquity`  | Tracks an idiosyncratic per-asset price path and protocol control channels supplied by the typed PE bundle.       | Saleability is driven by the typed PE protocol bundle plus the owner's PE tender policy.  |

### Private-Equity Protocol

PE state crosses the model↔sim boundary as a single typed
`PrivateEquityBundle` — one wide polars frame keyed by
`(rollout_index, month_index, issuer_id)` with ten dtype-typed channels.
The model layer must emit a complete per-issuer entry whenever a
scenario holds the issuer; producing an issuer with any channel missing
is a schema violation, not a runtime sim-compile failure. Channels
group by dtype:

| Group                       | Channel                       | Meaning                                                                                                                                                              |
| --------------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PrivateEquityFloatChannel` | `mark_usd_per_unit`           | Per-unit mark / sale price path.                                                                                                                                     |
|                             | `sale_capacity_fraction`      | Fraction of currently held units sellable through a voluntary tender/public-market opportunity.                                                                      |
|                             | `eligible_fraction`           | Fraction of currently held units eligible for voluntary sale.                                                                                                        |
|                             | `forced_sale_fraction`        | Fraction of currently held units forcibly sold in that month.                                                                                                        |
|                             | `forced_recovery_cashout_usd` | Dollar recovery paid for the remaining position in that month.                                                                                                       |
|                             | `company_valuation_usd`       | Opt-in M2 company market cap `V(t)`. All-zeros when the valuation channel is off (no `current_valuation_usd` anchor); a positive market cap is never all-zeros.      |
| `PrivateEquityIntChannel`   | `regime_code`                 | `PrivateEquityRegimeCode`: `PRIVATE_OPERATING`, `PUBLIC_MARKET`, `ACQUIRED`, `COLLAPSED`.                                                                            |
|                             | `event_kind_code`             | `PrivateEquityEventKindCode`: `NONE`, `TENDER`, `ADMIN_MARK_UPDATE`, `PUBLIC_MARKET_OPEN`, `ACQUISITION_CASHOUT`, `LEGAL_IMPAIRMENT`, `FORCED_RECOVERY`, `COLLAPSE`. |
| `PrivateEquityBoolChannel`  | `sale_opportunity_active`     | Discrete voluntary tender opportunity flag. Public-market saleability is represented by the `PUBLIC_MARKET` regime, not by this flag.                                |
|                             | `liquidity_blocked`           | True blocks voluntary tender/public-market sales for the month.                                                                                                      |

Producers construct one issuer's bundle via the keyword-only
`PrivateEquityBundle.from_issuer_arrays(...)` — every channel argument
is required, so a producer can't half-fill an issuer. Multiple issuers
combine via `PrivateEquityBundle.combine(...)`.

Consumers (sim engine) index the bundle by issuer position into the
typed `PEChannels` dataclass produced at compile time — there is no
string-keyed PE series lookup on the request, in the sim plan, or in
event compilation.

The simulator validates finite marks and fraction bounds before
applying sales. Voluntary sales are policy-mediated: the owner's PE
tender policy sets a liquid-net-worth floor, while the typed bundle
determines whether a tender or public-market sale is possible and how
much is sellable. Forced sale and forced-recovery cashout channels
bypass the voluntary floor and apply directly to the remaining
position.

### Policy types

Policies are first-class typed objects. The current policy vocabulary:

| Policy                    | Inputs                                                       | Action(s) emitted                                                                                          |
| ------------------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| `PrivateEquitySalePolicy` | Sale-rule configuration for voluntary PE sale opportunities. | Sell `PrivateEquity` when an automatic rule intersects with an exogenous tender/public-market opportunity. |
| `MortgagePaymentPolicy`   | Mortgage liability, payer agent, cash source.                | `PayLiability` from owner cash flow.                                                                       |
| `RentalUsePolicy`         | Property, mode (occupied / rented / partial), tenant pool.   | `OccupyProperty` / `RentProperty`.                                                                         |
| `OccupancyDecisionPolicy` | Property, move-out month, alternative housing config.        | Transitions occupation phase; potentially triggers `RentProperty`.                                         |

Policies do not encode actor identities in their type names — actor IDs are
data in scenario configuration, not type-system distinctions.

Partner/co-owner contribution agreements are intentionally not part of the
current product contract. The previous frontend/backend actor-policy path was
removed until the simulator has a tested, explicit agreement model.

### Obligation Lifecycle

Current required obligations are due immediately in the month they fire. The
engine debits the configured cash account, uses the agent's configured
liquidation policy to sell assets if the cash account goes negative, and marks
the rollout failed if the account cannot be brought back to non-negative cash.
After failure, state-backed value metrics for that rollout are frozen at zero
for the rest of the simulation; the failed status and first failure month remain
machine-readable. It does not model partial payments, grace periods,
delinquency balances, recovery/cure, or underpayment penalties.

### Effect types

`Effect` rows are the user-visible trace surface for realized sales. System-emitted accounting moves (mortgage settlement, monthly spend, property-cost obligations) are derivable from ledger postings, balance snapshots, and accounting details — the canonical detail surface — and are not separate effect rows.

| Effect                     | What it records                                                                                                   |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `SellSecurityEffect`       | Sale of a tradable security (index proxy, individual stock, crypto): units, basis, realized gain, tax allocation. |
| `SellPrivateEquityEffect`  | Sale of private-equity holding (tender, public-market post-lockup, or forced acquisition).                        |
| `SettlePropertySaleEffect` | Property disposition: gross proceeds, debt payoff, closing costs, capital-gains allocation.                       |

Discrete one-time events the engine also records (not produced by policies but
by exogenous trajectory inputs / scenario configuration):

| Event                           | Source                                    | Effect                                                                                                                               |
| ------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `PrivateEquityMarkerEvent`      | Exogenous PE protocol frame.              | Explains tender/admin/public-market/acquisition/legal/recovery/collapse markers and the sim-facing PE regime/controls for a month.   |
| `PrivateEquityOpportunityEvent` | Exogenous PE opportunity + holder policy. | Explains sparse tender decisions: sold, floor-satisfied, capacity-zero, liquidity-blocked, no-policy, no-units, or nonpositive-mark. |
| Lifecycle/property markers      | Scenario configuration.                   | Explains deterministic property/rental/primary-residence lifecycle events that affect state or obligations.                          |

## Per-rollout evaluation loop

For one rollout, given a `Scenario` and an exogenous trajectory bundle:

1. The selected exogenous trajectory provides shared macro paths plus
   per-asset price paths, sellability masks, and opportunity streams.
2. State is initialized from the scenario: each agent's cash, holdings,
   liabilities, ownership shares.
3. For each month `t` in `[0, horizon_months]`:
   a. Apply scheduled events for the month (regime changes, lockup expiry,
   liquidity events open).
   b. Mark-to-market: update asset values using external series.
   c. Accrue: rent income, expenses, depreciation, mortgage interest,
   property tax, insurance.
   d. Each agent's policies produce actions in deterministic order.
   e. Engine validates + applies actions; ledger records them.
   f. Record the per-month row (one row per agent + portfolio-wide aggregates).
4. At horizon, record the terminal row. A property is sold only if the
   scenario includes a `PropertySaleEvent`; otherwise it remains owned and
   contributes home equity rather than sale cash.

## Outputs

The product API exposes two response shapes against a `ScenarioKey`:

- `MetricFanResponse` — one user-selected metric over the horizon as a
  percentile fan across the requested rollout seed window, plus terminal percentiles
  for that same selected metric. It does not return per-rollout records; response
  size is bounded by horizon × requested percentile count, not rollout count.
  The request identifies the rollouts with a seed window, not a per-rollout seed
  list.
- `RolloutResponse` — full per-month metric frame and typed event log for
  one selected rollout. It may be requested by explicit seed or by asking the
  server to select the rollout at a terminal metric percentile from a bounded
  seed set.

Both carry a `model_id` so the caller can identify which
trajectory bundle the response was sampled against. Failed rollouts zero
their downstream metrics from the failure month onward.

## What augur does not do (non-goals)

- It is not a tax compliance engine. Tax computations are approximations
  parameterized at the scenario level (marginal rates, cap-gains rates,
  depreciation rules). They are not authoritative.
- It is not a real-time pricing engine. Exogenous paths are trajectories
  generated outside the simulator; intra-month dynamics are not modeled.
- It is not a portfolio optimizer. Policies are user-specified rules; augur
  reports their consequences, not what optimal policies would be.
- It does not model agent learning or strategic interaction (game-theoretic
  best response). Each agent's policy is fixed by scenario configuration.
- It currently assumes FIFO lot selection for sale-basis accounting where a
  simulator slice needs concrete cost-basis math. HIFO, specific-identification,
  and average-cost lot selection are future extensions.
