# Experiment programs for a proposed Augur library

Design sketches, updated 2026-09-09. These Python programs and `proposed_augur/*.pyi`
describe a destination, **not runnable Augur APIs or financially validated studies**.
They produce no results in this PR. The caller supplies historical records, model
artifacts, product/tax configurations and private inputs.

| Program                                           | Question                                                                                 | Distinct demand on the library                                           |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| [Market-model comparison](model_comparison.md)    | Which models predict better, and do their differences change the policy we would select? | Rolling held-out forecasts; cross-model policy evaluation                |
| [Trinity](trinity.md)                             | How do withdrawal rate and allocation affect historical success?                         | Overlapping historical windows, explicit annual conventions              |
| [Guyton–Klinger](guyton_klinger.md)               | How do adaptive rules change sustainable withdrawals and purchasing power?               | Path-dependent spending and portfolio management; conditional statistics |
| [Vanguard-style dynamic spending](vanguard.md)    | How does bounded spending flexibility change outcomes across allocations and horizons?   | Our own forecast paths; an executable spending function                  |
| [Allocation glide paths](glide_paths.md)          | Does changing allocation over time improve outcomes under different return assumptions?  | An executable allocation function; shortfall severity, not just failure  |
| [Forecast-driven spending](forecast_feedback.md)  | What if the spending policy periodically reassesses the plan?                            | Conditional inner forecasts, explicit policy beliefs, no future leakage  |
| [Spending and allocation](spending_allocation.md) | Which combinations of lifestyle flexibility and investments produce acceptable outcomes? | Actual lots, taxes, reinvestment, transitions, model sensitivity         |
| [Housing and investments](housing.md)             | How does buying and financing a home change the distribution?                            | Several agents, contracts, asset acquisition and disposition             |

These are distributional experiments with parameter sweeps, not a commitment to
match every paper's numbers. Proprietary forecast replication is not a goal.
[Exogenous models](exogenous.md) keeps loading, fitting and sampling in experiment
shells; [further studies](studies.md) records other relevant research families.
[Study helpers](study_helpers.md) shows ordinary reusable Python policy functions.

## Proposed modules

Each stub has a brief responsibility docstring. Names are negotiable; economic
boundaries are the point. The current [architecture plan](../roadmap.md) owns
sequencing, [policy interfaces](../policy_interfaces.md) owns the common contract,
and [timing](../policy_timing.md) owns unresolved financial timing choices.

| Module                                          | Responsibility                                                        |
| ----------------------------------------------- | --------------------------------------------------------------------- |
| [money](proposed_augur/money.pyi)               | Exact amounts and explicit purchasing-power bases.                    |
| [instruments](proposed_augur/instruments.pyi)   | Product identities/terms shared by models, books and policies.        |
| [accounting](proposed_augur/accounting.pyi)     | Read-only books, accounts, exact lots and financial receipts.         |
| [taxes](proposed_augur/taxes.pyi)               | Canonical tax consequences in accumulated filing-unit context.        |
| [contracts](proposed_augur/contracts.pyi)       | Known obligations and counterparty-supplied offers.                   |
| [data](proposed_augur/data.pyi)                 | Author-selected datasets, vintages and alignment.                     |
| [markets](proposed_augur/markets.pyi)           | Load/fit/sample exogenous paths and construct supported products.     |
| [state](proposed_augur/state.pyi)               | Assemble opening facts, not a web-app scenario.                       |
| [observations](proposed_augur/observations.pyi) | Information available to one actor at a monthly decision.             |
| [actions](proposed_augur/actions.pyi)           | Explicit sales, purchases, transfers, claim payments and consumption. |
| [policies](proposed_augur/policies.pyi)         | Ordinary functions and actor/path-local memory.                       |
| [proposals](proposed_augur/proposals.pyi)       | Optional Python-friendly action calculators and canonical previews.   |
| [simulation](proposed_augur/simulation.pyi)     | Canonical stepping/execution; optional monthly-loop helper.           |
| [results](proposed_augur/results.pyi)           | Requested reductions, replay and independent forecast scoring.        |

No registry, required policy superclass, action dependency graph or experiment
framework is implied. Pure model scoring needs no investor or financial execution.

## The experiment owns the monthly loop

One ordinary function receives one actor's monthly observation and memory and
returns `(Response(ordered_actions), memory)`. Initialization is another ordinary
function, not a required class. Scalar authoring can be adapted over observations
in Python while transferring all active paths' actions in one batch:

```python
from collections.abc import Mapping

from proposed_augur.accounting import Actor
from proposed_augur.markets import Worlds
from proposed_augur.policies import Initialize
from proposed_augur.results import Observer, PathResults
from proposed_augur.simulation import Finished, Session
from proposed_augur.state import Situation


def experiment_loop(
    situation: Situation, worlds: Worlds, policies: Mapping[Actor, Initialize],
    actor: Actor, observers: Mapping[str, Observer],
) -> PathResults:
    session = Session(
        situation, worlds=worlds, decision_actors=tuple(policies),
        reporting_actor=actor, observers=observers,
    )
    local = {}
    pending = session.start()
    while not isinstance(pending, Finished):
        responses = {}
        for key, observation in pending.observations.items():
            if key.policy not in local:
                local[key.policy] = policies[key.policy.actor](key.policy)
            decide, memory = local[key.policy]
            responses[key], memory = decide(observation, memory)
            local[key.policy] = decide, memory
        # One monthly transfer for the active population, not a native call per row.
        pending = session.advance(responses)
    return pending.result
```

`run(...)` is the optional convenience for this same loop. The experiment chooses
policies explicitly; its `Run` adds replay using those supplied initializers.
The raw loop returns `PathResults`, without pretending the session captured
arbitrary caller functions. The experiment chooses
paths, models, actors, policies, sweeps and analysis; the executor still owns
accrual, settlement, contracts, taxes and financial time evolution. Row layout,
batch-native authoring and chunk size remain open to P6/GL. Response keys include the pending month, so a stale
response map is rejected; policy memory uses the stable actor/path identity.
Runtime language is separately open to RUNTIME/GE; ordinary Python policies do not require all execution
to remain Rust or imply a per-path native call. No arbitrary closure serialization
or universal checkpoint API is required.

Every active actor gets exactly one call per month. Annual studies return empty
actions in intervening months; their spending/rebalance cadence does not change.
Annual-only records need the declared synthetic month-grid adapter in
[study helpers](study_helpers.md), not invented observed monthly returns.
The first monthly example has settled ordering: scheduled cashflows and claim
assembly, then the single policy call, then its ordered actions, then a stop if
any due claim remains unpaid. Its scope is one household with scripted
counterparties and the existing explicit immediate-cash control. GP remains for
expanded product, housing and cross-actor timing, not this first monthly placement.
The product-terms interfaces below are design targets, not implemented delayed
settlement support.

## Actions, observations and helper boundaries

The engine executes each submitted list in caller order. `Sell → Buy → Transfer →
Buy` is valid when each step meets its financial conditions; there is no global
sells-first pass. Submission does not make unsettled proceeds available.

An action that cannot execute changes no book and is fatal for **that rollout**.
Earlier successful actions and receipts remain; later actions and all future
policy calls for that path are skipped. Other paths continue. The batch is not
all-or-nothing, and there is no retry, reminder, event-driven callback or second
decision within the month. Invalid input schemas and simulator bugs are errors,
not fabricated financial failures.

An empty list is valid, but cannot defer a due claim. A still-unpaid due claim
also stops the rollout after its actions; it is distinct from an invalid action.

Request identity is the actor/path/month decision key plus the action's position
in its ordered list; receipts retain that identity, not a guessed category match.

Policies see their own accessible accounts/lots, known claims/contracts, observed
market information, filing/payment records and prior execution receipts. They do
not see future sampled prices, other actors' private state or a mutable ledger.
Intentions and successful execution are distinct. Later memory updates can use
receipts; a rejected action cannot trigger an immediate revised plan.

Budgets and weights are inputs to an author's algorithm, not engine commands.
Optional funding, FIFO, reserve and rebalance helpers calculate explicit actions.
A policy may replace or ignore them, and execution must not invent trades afterward.
Non-mutating previews share canonical accounting with execution under actor-known
facts and named assumptions; they do not supply hidden future taxes or prices.
The personal example composes funding, payments, consumption and investment in one
function. Housing acceptance cannot manufacture loan approval.

## Financial fidelity and outputs

Product construction must agree with valuation, payouts, trading and taxes for
every held or purchasable instrument. A gross total-return index is a declared
NoTax study proxy, not a taxable price series with dividends silently reinvested.
A distributing fund requires explicit payouts; a bond fund and an individual
tradable bond require different mechanics. Unsupported combinations fail at
assembly, before reporting apparently credible financial outcomes.

All cells reuse immutable worlds and opening facts. Actual lots retain basis;
changing allocation executes trades, not a replacement opening book.
Money and tax rounding belong to canonical execution. Approximate policy numbers
are not ledger balances. Real values use the explicit currency/index/base date,
including inner planning situations; no silent rebasing at a forecast origin.

`Run.paths` has one row per original path identity with requested reductions,
`reached_horizon`, `failed_action` and `stop_reason`; scoped spending/default
indicators remain distinct. Terminal metrics are null for stopped paths. Paid
consumption includes only actual receipts through stopping, not hypothetical
post-failure spending. Terminal wealth includes liabilities and accrued taxes,
but is not automatically liquidation value. Observer names and reducers belong to
the experiment; no central registry of all lifestyle or policy outcomes is needed.

`run.trace(path_id)` replays the identified path with fresh isolated policy memory.
It does not require web-app rollout caching. The receipt pins path identities,
dataset vintages, fit/model/product choices, opening facts, tax assumptions,
calendar, policy code revision/parameters and engine version. The experiment must
also pin any author-owned decision log used in its analysis; a note is not a receipt.

Within a model, path selection/reordering/chunking preserves identity and results.
Across models, equal seeds alone do not create economically paired paths. Historical
overlapping-window frequencies are not IID probabilities. Report conditional
statistics with their denominators, paired Monte Carlo uncertainty where applicable,
and model uncertainty separately. Neither wealth above zero nor a model class name
establishes an acceptable lifestyle or institutional forecasting quality.
