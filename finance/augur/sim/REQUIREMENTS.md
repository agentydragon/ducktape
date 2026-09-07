# Augur core simulator requirements

## Purpose

Augur core is a deterministic, auditable financial trajectory evaluator. Given
an authored financial scenario and many externally supplied possible futures,
it produces one internally consistent financial history per future, an outcome
distribution, and enough causal detail to explain any selected history.

This document defines the target observable contract. Some requirements may
precede complete implementation; conformance comes from tests and validated
examples. Architecture and implementation belong in `DESIGN.md`.

## Inputs and validation

A run consists of a scenario, monthly horizon, exogenous trajectory bundle, and
identified financial and tax ruleset.

The scenario defines agents, accounts, assets, liabilities, properties,
contracts, scheduled events, obligations, and decision policies. Exogenous
trajectories describe observations outside the agents' control, such as prices,
rates, property values, spending, rent, and liquidity opportunities. These
observations must remain distinct from authored decisions: an agent's actions
change its own financial state, not the supplied market path.

Before evaluation, Augur must reject unresolved or duplicate identifiers,
invalid ownership or account references, impossible contract relationships,
malformed financial values, invalid policy topology, missing trajectory
coverage, inconsistent dimensions, and unsupported tax configuration. It must
not silently replace financially meaningful missing inputs with zeroes or
defaults.

## Financial state and behavior

Each rollout must maintain every modeled agent's evolving:

- accounts and cash;
- asset positions, acquisition dates, distributions, and tax lots;
- liabilities, principal, interest, and counterparties;
- property ownership, adjusted basis, occupancy, equity, improvements, and
  depreciation;
- income, gains, losses, deductions, tax liabilities, and tax payments;
- required obligations and settlement status.

Augur must evaluate scheduled and recurring cashflows; purchases and lot-aware
sales; tax-loss harvesting and loss carryforward; liquidity and allocation
policies; supported mortgages and amortizing contracts; property purchase,
rental activity, carrying costs, and sale; and constrained private-asset
liquidity. Adding another entity that follows supported financial behavior
should normally be a scenario change, not a new core capability.

Actions within a month must follow a documented, stable financial order.
Independent actions must not depend on incidental processing order. When a
shared constraint such as cash availability makes order material, the result
must be deterministic and explainable.

## Double-entry accounting

All economic events must use correct double-entry accounting.

- A journal entry may contain any number of postings, but total debits must
  equal total credits; equivalently, signed postings sum to zero in each
  currency.
- Every money movement must identify source and sink accounts, counterparties,
  income, expenses, liabilities, or equity.
- Internal transfers move value without creating or destroying it.
- External inflows and outflows use explicit boundary counterparties or
  clearing accounts. Those rows participate in journal balancing even when the
  external party's complete balance sheet is outside the scenario.
- Initial cash, holdings, and liabilities are established through opening
  entries against opening equity.
- Purchases, sales, loan originations, principal and interest payments, fees,
  taxes, and property closings reconcile across every affected party.
- Accruals use named asset, liability, income, expense, or equity accounts.
  Market revaluation is identified separately from cash movement and
  reconciles to the supplied price change.

At each reporting boundary, balances must reconcile to the journal and each
modeled agent must satisfy assets = liabilities + equity, including opening
equity and cumulative income, expense, and recognized gains or losses.
Aggregate results must reconcile to the same entries and balances. No repair
pass may hide an unbalanced event.

Financial quantities and rounding must follow explicit, stable rules suitable
for ledgers and tax calculations.

## Tax responsibility

Tax is part of the financial history, not a percentage applied to terminal
wealth. Within its declared scope, Augur must model tax accrual, liability,
payment schedules, funding, and settlement.

The target scope is US federal and California taxation for supported
single-filer scenarios, including applicable ordinary income, capital gains and
losses, deductions, mortgage interest, net investment income tax, rental
activity and depreciation, depreciation recapture, primary-residence gain
exclusions, property and transfer taxes, estimated-tax safe harbor, and annual
true-up.

Results must identify the tax year, jurisdiction, filing status, and ruleset,
and expose enough detail to audit classifications, deductions, bracket
application, accruals, payments, and remaining liabilities. Augur applies the
explicitly modeled rules; it is not an authoritative tax-return or tax-advice
system. Unsupported tax situations must be rejected or identified as out of
scope rather than silently approximated.

## Scale and reproducibility

Augur must preserve the identity and internal consistency of every rollout
while evaluating large populations. A representative production target is
100,000 rollouts over a long monthly planning horizon. The project must
maintain a representative benchmark with explicit latency and memory budgets.
Meeting those budgets must not sacrifice accounting reconciliation, per-rollout
status, or selected-rollout inspection.

Identical scenarios, trajectory bundles, horizons, and rulesets must produce
identical results. Results must identify the scenario and policies, trajectory
bundle and path identity or seed, simulator version, ruleset versions, horizon,
and rollout population. Plan comparisons should support paired evaluation
against the same futures, and a selected rollout must be reproducible without
sampling a replacement path.

## Outputs and explainability

For every rollout, Augur must expose monthly state sufficient to answer what
each agent owned, owed, earned, paid, and could spend.

Every significant state change must have an attributable cause. The causal
history must cover authored and recurring events, policy decisions, journal
entries, lot acquisitions and dispositions, realized gain classification,
obligations, funding attempts, payments, shortfalls, tax accrual and settlement,
and the event that caused failure.

The core must support two complementary output levels:

1. **Population state:** status and financial outcomes for every rollout,
   sufficient for downstream systems to derive distributions, failure rates,
   net worth, liabilities, taxes, and product metrics.
2. **Selected-rollout history:** a complete readable financial and causal
   history for forensic inspection of concrete rollouts.

Derived summaries must remain linked to actual rollout identities. Metrics
across all rollouts must be distinguishable from metrics conditioned on
survival. Population state, summaries, and selected histories must reconcile
to the same underlying entries and balances.

## Failure handling

Invalid inputs fail before evaluation. Financial failure during a valid run is
a modeled outcome and is local to that rollout; it must not abort or corrupt
other rollouts.

When an obligation cannot be funded, Augur must record the rollout, month,
obligation, amount due, amount paid, shortfall, attempted funding sources, and
state at the failure boundary. A successful funding sale is not failure, and a
low balance is not equivalent to an unpaid obligation.

## Acceptance invariants

Acceptance tests must demonstrate that:

- every journal entry balances to zero;
- all cash movements have explicit sources and sinks;
- opening balances reconcile through opening equity;
- external flows reconcile through boundary counterparties;
- balances reconcile to journal entries and assets equal liabilities plus
  equity;
- asset units, lots, basis, proceeds, and realized gains reconcile;
- loans and counterparty principal movements reconcile;
- property purchases, sales, costs, and mortgage payoff reconcile;
- tax accruals, payments, and remaining liabilities reconcile;
- aggregate metrics reconcile with individual rollout histories;
- identical inputs produce identical outputs;
- failed rollouts remain identifiable without affecting other rollouts;
- missing or inconsistent inputs are rejected explicitly.

Worked scenarios should serve as executable acceptance examples, especially
for taxes, properties, loans, constrained liquidity, and failure behavior.

## Boundaries

Augur core does not own raw evidence ingestion; stochastic-model fitting,
calibration, or production trajectory generation; plan optimization; product
request parsing or presentation; persistent customer storage; unsupported tax
approximation; market impact; or unmodeled human behavior. Those belong to
modeling, optimization, product, storage, and presentation components.

Augur core owns the correct, reproducible, and explainable evaluation of the
financial scenario it is given.
