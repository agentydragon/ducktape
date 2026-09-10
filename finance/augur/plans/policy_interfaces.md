# Actor-facing policy interfaces

Target design for the remaining migration and gates GP/GL/GE in [the roadmap](roadmap.md).
The common action session is implemented in `rust/simulator.pyi`; module names and
richer types below are sketches, not additional API declarations. Reuse existing
domain types and introduce fields only for a supported consumer.

The boundary is economic agency: a policy sees information available to its actor
and requests actions that actor could take. The environment owns contracts,
execution and consequences. **Python owns the outer loop for every consumer**, including
the app; only the implementation inside each financial step is a Python/Rust choice.
Rule-driven brokers, lenders and tax authorities suffice; this does not require a
strategic many-agent economy.

## `observations.py`

```python
"""Read-only information available to an actor at a particular time."""

@dataclass(frozen=True)
class Observation:
    at: datetime
    accounts: tuple[AccountView, ...]
    positions: tuple[PositionView, ...]
    contracts: tuple[ContractView, ...]
    claims: tuple[ClaimView, ...]
    market: MarketView
    tax_records: TaxRecords
    events: tuple[ActorEvent, ...]
```

Accounts distinguish available cash from unsettled proceeds; positions expose
accessible lots/basis. Contracts and claims expose known terms, amounts and due
dates, not a funding strategy. Market observations carry publication/observation
times; a property estimate is not an observable true value. Tax records are known
filing/payment facts, not hidden future assessments. Events convey executions,
payments, rejections and new information. No future realized paths or another
actor's private books. Path routing and capture configuration belong to the runner.
The common observation exposes exact current/origin CPI when modeled; a dependent
policy rejects its absence. No policy receives the future sampled CPI path.

## `actions.py`

```python
"""Economic requests, never direct edits to balances or tax state."""

@dataclass(frozen=True)
class Sell:
    account: AccountRef
    lots: tuple[LotQuantity, ...]

@dataclass(frozen=True)
class PayClaim:
    source: AccountRef
    claim: ClaimId
    amount: Money

type Action = Sell | Buy | Transfer | PayClaim | Consume
```

`Buy` names an account, instrument and quantity; `Transfer` names source,
destination and amount; `Consume` names a funding account, consumption component
and amount. Lot selection is an actor choice, with optional FIFO/tax-aware helpers.
Target weights or a desired spending budget are inputs to policy algorithms, not
instructions for the engine to invent trades. Housing later adds concrete actions
such as accepting a mortgage offer or purchasing property on specified terms;
the actor cannot declare that a lender granted a loan.

Submit one ordered action list per actor per month. The executor processes it in
the supplied order, so a later action can use balances changed by an earlier one:
`Sell → Buy → Transfer → Buy` needs no global sells-before-buys rule. The list can
cross the language boundary in one batch; batching does not change its semantics.

An action that cannot execute is fatal for that rollout. The failed action changes
no books; earlier successful actions and their receipts remain. Skip the rest of
the list and all later policy calls for that rollout. Other rollouts continue.
There is no within-month retry or return to policy, and the list is not an atomic
transaction. Results identify the failed action and successful prefix. Execution
uses the product's stated cash-availability rules; submission alone creates no cash.

## `policy.py`

```python
"""Actor decisions and memory, independent of financial execution."""

@dataclass(frozen=True)
class Response:
    actions: tuple[Action, ...]

def decide(
    observations: Batch[Observation], memory: MyPolicyMemory,
) -> tuple[Batch[Response], MyPolicyMemory]: ...
```

The experiment owns the policy's memory and function; no required superclass or
registry. There is one policy function shape: batches in, batches out. Each active
actor/path appears once in its monthly batch; an empty action list is a valid
decision and does not defer claims. Successful execution results can inform the
next month's decision, not another call within this month. Record intentions as
intentions, and actual cash raised from receipts. A budget cut is a policy decision,
not inferred from a payment failing.

## `simulation.py`

```python
"""Own financial state and execute one monthly action list per active actor."""

class Session:
    def start(self) -> DecisionBatch | Finished: ...
    def advance(self, responses: Batch[Response]) -> DecisionBatch | Finished: ...
```

A decision batch contains monthly observations plus actor/path/month routing
identities. Responses correspond to those pending decisions. Advance executes
their ordered lists and financial month, returning next-month observations for
continuing paths or final results. It never requests another decision for the same
actor/month. Terminal results retain the action and reason that stopped a path.

The calling experiment owns the outer monthly loop in Python: start the session,
dispatch observations to policies, submit their responses, repeat until
finished. An optional Python `run(...)` helper uses the same session when custom
orchestration is unnecessary; the app also uses this interface, not a Rust full-run
entrypoint. The executor's `advance` owns financial time evolution between
decisions: calendar/event ordering, accruals, settlement and taxes. The caller does not
reimplement those rules or advance past unanswered decision opportunities.

The first actor-action example has one decision-making household and rule-driven
counterparties: apply scheduled cashflows/events and assemble due claims, observe
and decide once, execute the ordered actions, then stop if any due claim remains
unpaid. Its supported trades retain their explicitly declared immediate-cash
control; this is not a promise about real products' settlement delays.
Ordering between additional decision-making actors and expanded product/housing
timing remain GP choices, not an accident of batch row order. Books and paths
stay in the existing executor. Native unit tests may drive the step primitives;
production consumers converge on the Python loop, not two supported drivers.

Future PE support distinguishes mandatory issuer events from holder decisions.
Forced recovery executes without a policy opt-in. Each presented sale opportunity
requires an explicit sell or decline response; missing one is invalid, not an
implicit decision to hold. Response coverage belongs to the same monthly batch,
not a second callback or retry loop. This requirement is recorded for deferred PE
work; the current action session does not implement it.

The current session uses Rust financial steps. Prefer Python moves that improve
the domain model and experiment composition; keep one canonical implementation
and migrate callers atomically. GL and RUNTIME/GE are parked optimization work,
not gates on those moves. A rollout is stateful across time; parallel independent
rollouts do not require a dense whole-future kernel or equally sized event lists.

### One batch-shaped policy API

The engine, caller-owned loop and convenience runner all use the same batch-shaped
callable. A singleton batch is the one-rollout case, not a second interface. An
optional scalar-to-batch helper may invoke an author's scalar function once per
row while routing its per-path memory; it returns the same keyed response batch.
Neither the engine nor runner dispatches through a separate scalar policy hook.

Start with one clear typed representation, chosen for the domain and authoring
experience. GL may compare layouts/adapters when actual large-N workloads need
optimization later. No performance budget gates current consumer migration, and
no separate scalar/batch engine APIs are permitted.

`Batch` leaves row/column layout, ragged actions/lots and chunk size undecided.
Only active rollouts participate in a monthly decision batch.
Memory and any policy randomness are isolated per actor/path, never keyed by
temporary row position. Reordering, chunking or replaying selected independent
paths must preserve their decisions/results. One rollout's actor cannot use
observations from alternative rollouts; cross-path optimization belongs in the
experiment shell. Completed/stopped paths receive no further calls.

## `policy/sleeves.py`

```python
"""Portfolio decision algorithms; propose trades without executing them."""

def rebalance(
    portfolio: PortfolioView, targets: TargetWeights, cash_budget: Money,
) -> tuple[Buy | Sell, ...]: ...
```

Withdrawal/deposit allocation, cash bands, rebalancing, lot selection and guardrails
are ordinary **Python-callable** helpers a policy can compose or ignore. Implement
them in Python by default so notebook authors can inspect, edit and replace them
without rebuilding Rust. A measured hot calculation may use a native kernel behind
that same Python surface; its current Rust location alone is not justification.
`policy/sleeves.py` already provides withdrawal/deposit/rebalance proposals with
scoped FIFO selection, including zero targets and full exits. Cash-band and exact
quantity calculations are also Python-callable. P12 deletes the old Rust
calculations as their remaining configured callers migrate. Add another helper
only for a concrete consumer, with exact rounding/scale tests.
Do not grow a native-only helper API first or maintain Python/Rust twins as supported
alternatives. Helpers use scoped observed lots, cash, prices and product terms; they
do not duplicate the executor's tax, accounting or settlement machinery.
A policy can ask a helper how to satisfy its needs under the products' settlement
rules, inspect or compose the returned operations, and submit them in its one
response. The helper proposes; the executor validates and settles. It must not
run another funding or rebalancing strategy afterward.
Autopay or delegated liquidation requires an
explicit standing instruction with modeled terms. Non-mutating tax/trade previews
reuse canonical calculations with observable inputs and explicit assumptions,
never the future realized path.

## Managed-account composition

A household-owned managed portfolio attaches a modeled investment service to an
account. The policy requests contributions/withdrawals and reads current account
facts; it does not manufacture tax losses or trigger the service's internal
harvesting each month. Typed model consequences enter canonical execution through
a separate, narrowly scoped financial-step boundary, not the investor action API.
The [managed-portfolio plan](managed_portfolio.md) gives the Python composition
sketch, phase/interface decisions and passive-account → investor-actions → runnable
comparison slices. These are proposed extensions to the same batch session, not
implemented APIs or a general entity/plugin framework.

## Acceptance and remaining choices

`x/monthly_actions` authors a Python batch policy and advances the common action
session, including its population/profile and selected-replay entrypoints. Its CI
controls cover immediate sale cash, lot/basis/tax reconciliation, fatal action
prefixes and summary/trace agreement with generated financial inputs. Native tests
exercise the same steps, not an alternative production callback driver.

A failing middle action must leave the successful prefix intact, apply none of
the failed action, and execute neither later actions nor later policy calls for
that rollout. Independent rollouts continue. Ignoring sleeve helpers must cause
no hidden engine trades, cuts or borrowing. Results distinguish fatal invalid
actions from unmet claims; unsupported input schemas and simulator bugs are not
silently converted into modeled financial failure.

The single batch-shaped API, monthly call limit, caller-specified action order,
fatal-action contract and first example's cashflows/claims-before-policy ordering
are settled. An unpaid due
claim stops that example after the action list, distinctly from an invalid action.
GP gates expanded product/housing and multi-policy-actor timing, not this first
integration. No retry/default/recovery mechanism is part of this interface.
`x/{bounded_spending,allocation_glide,joint_spending_allocation}` compose Python
policies on the common session. Their controls cover annual cadence, explicit
funding/reserves, selected replay and joint decisions. Intentions remain separate
from attempted requests: when an earlier action prevents consumption, its request
is absent but actual paid consumption is zero for that observed month, not for
unobserved post-stop months. The bounded-rule scalar/batch comparison and profiler
use this same session, not a second policy interface.

P12 migrates configured consumers and removes old full-run loops and implicit
public-portfolio strategy, preserving required existing housing/PE capabilities.
Benchmarks and the app remain. The roadmap separates Python product
funding, common session reporting and held-bond capture from the narrow harvesting,
committed-purchase and private-equity timing gates needed for full app cutover.
Configured source-account claim grouping is all-or-none; each migration must test
and explain timing/funding differences rather than hide them in a compatibility
runner. GP gates only the additional product/multi-actor semantics a slice needs.
RUNTIME/GE can later change step internals without delaying this sequence.
The roadmap owns dependencies, per-consumer deletion checkpoints and acceptance;
this sketch does not introduce another prerequisite chain.
