# Augur — Specification

Augur is a probabilistic simulator of a multi-agent economic system. Given a
**scenario** — a bundle of agents, assets, liabilities, markets, and policies —
it produces a distribution over trajectories of state (net worth, cash,
ownership shares, taxes, liquidity events, …) by sampling many rollouts.

This document specifies the entity model, the per-rollout evaluation loop, and
the user-visible guarantees. Implementation details live in code; SPEC.md only
records what an outside observer can rely on.

## Model

### Entities

| Entity        | What it is                                                                                                                                                                                                                          | Examples                                                                                                                             |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `Agent`       | An economic actor with state (cash, holdings, liabilities, ownership shares) and a set of policies.                                                                                                                                 | a primary owner, an equity-building occupant                                                                                         |
| `Asset`       | Something an agent owns that has value. Discriminated subtype determines valuation and liquidity model.                                                                                                                             | a `LiquidSecurity` tracking SP500, a `PrivateEquity` holding, a `RealEstate` property                                                |
| `Liability`   | A debt an agent owes, with an amortization schedule.                                                                                                                                                                                | a mortgage on a property                                                                                                             |
| `Market`      | A stochastic input source producing per-rollout paths.                                                                                                                                                                              | SP500 total return, local home-price paths, local rent paths, CPI, mortgage rate, per-`PrivateEquity` price + liquidity-event stream |
| `Policy`      | A typed rule attached to an agent: `(state, market, time) → list[Instruction]`. Composable; an agent can hold any number.                                                                                                           | liquidity-reserve maintenance, max-concentration rebalancing, partner-equity agreement, mortgage payment, rental management          |
| `Instruction` | A policy-emitted intent (e.g. "sell N units of asset X"). Validated and applied by the engine into an `Effect`.                                                                                                                     | `SellInstruction`, `BorrowInstruction`                                                                                               |
| `Effect`      | A realized state mutation after validation. The trace records effects, not the raw instructions.                                                                                                                                    | `SellSp500Effect`, `SellCryptoEffect`, `SellPrivateEquityEffect`, `SettlePropertySaleEffect`                                         |
| `Obligation`  | A first-class cash demand on an actor (tax, mortgage, property tax, HOA, insurance, maintenance, outside rent, partner contribution, special assessment, estimated tax). Settled via the funding-policy chain or fails the rollout. | annual tax due at year-end, monthly property tax                                                                                     |
| `Scenario`    | A bundle: agents + assets + liabilities + initial state + policies + which markets to sample from + horizon.                                                                                                                        | "primary buys property X with partner contributing"                                                                                  |

### Asset subtypes

| Subtype          | Valuation                                                                                                         | Liquidity                                                                                 |
| ---------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `Cash`           | Face value.                                                                                                       | Always liquid.                                                                            |
| `LiquidSecurity` | Tracks a market-provided multiplier (e.g. SP500 total-return proxy).                                              | Always sellable.                                                                          |
| `RealEstate`     | Tracks location-bound home-value and rent paths; has property tax / insurance / HOA / maintenance / depreciation. | Sellable on demand; sale incurs closing costs, capital-gains tax, depreciation recapture. |
| `PrivateEquity`  | Tracks an idiosyncratic price process (per-asset sampled path).                                                   | Determined by a `LiquidityRegime` variant attached to the asset. See below.               |

### LiquidityRegime (variant on `PrivateEquity`)

A discriminated union. Current variants:

| Variant              | Meaning                                                                                                                                                                                                                                                                        | Status       |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ |
| `LiquidityEventOnly` | Sale only at discrete sampled liquidity opportunities. The event stream is a stochastic process with binary arrivals and per-event price.                                                                                                                                      | Implemented. |
| `PublicMarket`       | Free sale at the spot price each month, subject to optional `lockup_end_month`. Participates in the obligation funding-policy chain when a `CheckingFloorSellPublicStockPolicy` lists `PRIVATE_EQUITY` in `sale_asset_preference`; the default preference does not include PE. | Implemented. |
| `Acquisition`        | One-shot forced conversion of the entire remaining position at a fixed `cash_per_unit_usd` on `event_month`. Realized gain feeds the existing annual sale-tax allocation.                                                                                                      | Implemented. |

A `PrivateEquity` asset can transition between regimes via a sampled
**regime-change event** (e.g. an IPO converts `LiquidityEventOnly` → `PublicMarket`).
The discriminated-union shape supports this, but there is no runtime hook
yet: regime changes mid-rollout are Future. Today, a position's regime is
fixed for the whole horizon by `PrivateEquityPosition.liquidity_regime`.

### Policy types

Policies are first-class typed objects. The current policy vocabulary:

| Policy                    | Inputs                                                                        | Action(s) emitted                                                                      |
| ------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `PrivateEquitySalePolicy` | Sale-rule configuration for sampled PE sale opportunities.                    | Sell `PrivateEquity` when an automatic rule intersects with a market sale opportunity. |
| `PartnerEquityAgreement`  | Contributor agent, owner agent, property, monthly amount, share-accrual rule. | `Transfer` + `AccrueOwnership`.                                                        |
| `MortgagePaymentPolicy`   | Mortgage liability, payer agent, cash source.                                 | `PayLiability` from owner cash flow.                                                   |
| `RentalUsePolicy`         | Property, mode (occupied / rented / partial), tenant pool.                    | `OccupyProperty` / `RentProperty`.                                                     |
| `OccupancyDecisionPolicy` | Property, move-out month, alternative housing config.                         | Transitions occupation phase; potentially triggers `RentProperty`.                     |

Policies do not encode actor identities in their type names — actor IDs are
data in scenario configuration, not type-system distinctions.

### Effect types

`Effect` rows are the user-visible trace surface for realized sales. System-emitted accounting moves (mortgage settlement, partner contributions, partner-equity accruals, monthly spend, property-cost obligations) are derivable from ledger postings, balance snapshots, and accounting details — the canonical detail surface — and are not separate effect rows.

| Effect                     | What it records                                                                             |
| -------------------------- | ------------------------------------------------------------------------------------------- |
| `SellSp500Effect`          | Sale of generic SP500 stock: units, basis, realized gain, tax allocation.                   |
| `SellCryptoEffect`         | Sale of crypto holding: units, basis, realized gain, tax allocation.                        |
| `SellPrivateEquityEffect`  | Sale of private-equity holding (tender, public-market post-lockup, or forced acquisition).  |
| `SettlePropertySaleEffect` | Property disposition: gross proceeds, debt payoff, closing costs, capital-gains allocation. |

Discrete one-time events the engine also records (not produced by policies but
by markets / scenario configuration):

| Event            | Source                                 | Effect                                                                                           |
| ---------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `LiquidityEvent` | Sampled by market for `PrivateEquity`. | Window during which `SellAsset` on that equity is permitted at the event's `price_usd_per_unit`. |
| `RegimeChange`   | Sampled by market (future).            | Mutates the asset's `LiquidityRegime` variant.                                                   |

## Per-rollout evaluation loop

For one rollout, given a `Scenario`:

1. Markets sample shared macro paths plus per-`PrivateEquity` price paths and
   liquidity event streams.
2. State is initialized from the scenario: each agent's cash, holdings,
   liabilities, ownership shares.
3. For each month `t` in `[0, horizon_months]`:
   a. Apply scheduled events for the month (regime changes, lockup expiry,
   liquidity events open).
   b. Mark-to-market: update asset values using market paths.
   c. Accrue: rent income, expenses, depreciation, mortgage interest,
   property tax, insurance.
   d. Each agent's policies produce actions in deterministic order.
   e. Engine validates + applies actions; ledger records them.
   f. Record the per-month row (one row per agent + portfolio-wide aggregates).
4. At horizon, record the terminal row. A property is sold only if the
   scenario includes a `PropertySaleEvent`; otherwise it remains owned and
   contributes home equity rather than sale cash.

## Outputs

A scenario-set run produces a typed `ScenarioSetRunResponse`:

- `MarketBundleMetadata`: the sampled market model, seed, rollout count,
  horizon, event streams, and source metadata.
- `ScenarioResult`: one result per scenario, each with accepted input summary,
  report tables, metric summaries, effects (sales), policy decisions, market
  observations, obligations + settlement results + funding decisions, accounting
  details, ledger entries, and balance snapshots.
- `ReportTable`: columnar per-month arrays for fan charts, sample paths, and
  terminal distributions.
- `warnings`: validation or modeling notes that did not prevent the run.

## What augur does not do (non-goals)

- It is not a tax compliance engine. Tax computations are approximations
  parameterized at the scenario level (marginal rates, cap-gains rates,
  depreciation rules). They are not authoritative.
- It is not a real-time pricing engine. Market paths are stochastic processes
  fit offline; intra-month dynamics are not modeled.
- It is not a portfolio optimizer. Policies are user-specified rules; augur
  reports their consequences, not what optimal policies would be.
- It does not model agent learning or strategic interaction (game-theoretic
  best response). Each agent's policy is fixed by scenario configuration.
