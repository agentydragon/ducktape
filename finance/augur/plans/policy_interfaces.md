# Actor-facing policy interfaces

Target design for the remaining migration and gates GP/GL/GE in [the roadmap](roadmap.md),
not implemented API declarations. Module names and types below are sketches;
reuse existing domain types and introduce fields only for a supported consumer.

The boundary is economic agency: a policy sees information available to its actor
and requests actions that actor could take. The environment owns contracts,
execution and consequences. Python versus Rust is an implementation choice below
that boundary. Rule-driven brokers, lenders and tax authorities suffice; this
does not require a strategic many-agent economy.

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

The calling experiment can own the outer monthly loop: start the session,
dispatch observations to policies, submit their responses, repeat until
finished. A library `run(...)` helper can own that loop when custom orchestration
is unnecessary. The executor's `advance` owns financial time evolution between
decisions: calendar/event ordering, accruals, settlement and taxes. The caller does not
reimplement those rules or advance past unanswered decision opportunities.

The first actor-action example has one decision-making household and rule-driven
counterparties: apply scheduled cashflows/events and assemble due claims, observe
and decide once, execute the ordered actions, then stop if any due claim remains
unpaid. Its supported trades retain their explicitly declared immediate-cash
control; this is not a promise about real products' settlement delays.
Ordering between additional decision-making actors and expanded product/housing
timing remain GP choices, not an accident of batch row order. Books and paths
stay in the existing executor; native and Python drivers share its mechanics.

The initial stepping/bridge work uses Rust execution. RUNTIME/GE separately
reevaluate that language choice, including execution strategy, ragged output
layout and notebook usability. A Python executor would own the same financial
time evolution; the economic boundary is not a permanent Python/Rust boundary.

### One batch-shaped policy API

The engine, caller-owned loop and convenience runner all use the same batch-shaped
callable. A singleton batch is the one-rollout case, not a second interface. An
optional scalar-to-batch helper may invoke an author's scalar function once per
row while routing its per-path memory; it returns the same keyed response batch.
Neither the engine nor runner dispatches through a separate scalar policy hook.

P6/GL compares that adapter with a directly batch-authored function on the same
workload. It chooses data representation and evaluates costs/usability, not
whether to maintain scalar and batch engine APIs.

`Batch` leaves row/column layout, ragged actions/lots and chunk size undecided.
Only active rollouts participate in a monthly decision batch.
Memory and any policy randomness are isolated per actor/path, never keyed by
temporary row position. Reordering, chunking or replaying selected independent
paths must preserve their decisions/results. One rollout's actor cannot use
observations from alternative rollouts; cross-path optimization belongs in the
experiment shell. Completed/stopped paths receive no further calls.

## `policies/sleeves.py`

```python
"""Portfolio decision algorithms; propose trades without executing them."""

def rebalance(
    portfolio: PortfolioView, targets: TargetWeights, cash_budget: Money,
) -> tuple[Buy | Sell, ...]: ...
```

Withdrawal allocation, cash bands, lot selection and guardrails are optional
ordinary helpers a policy can compose or ignore. Prefer Python for notebook-editable
strategy and calculators; retain native kernels where measured cost warrants them.
Move implementations with their consumers instead of maintaining Python/Rust copies.
A policy can ask a helper how to satisfy its needs under the products' settlement
rules, inspect or compose the returned operations, and submit them in its one
response. The helper proposes; the executor validates and settles. It must not
run another funding or rebalancing strategy afterward.
Autopay or delegated liquidation requires an
explicit standing instruction with modeled terms. Non-mutating tax/trade previews
reuse canonical calculations with observable inputs and explicit assumptions,
never the future realized path.

## Acceptance and remaining choices

P7's first scoped example observes a bill and emits `[Sell(...), PayClaim(...)]`
in one policy call. Under the explicitly chosen settlement assumptions, sale
proceeds must be available when the payment executes. Verify request/result
identity and lot/basis/tax reconciliation, plus a non-sells-first action chain.

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
The existing opening-month/all-or-none cases remain P6 parity controls, not
the destination contract. P6/GL decides batch data representation; RUNTIME/GE
decides executor placement. If GE retains hybrid execution, P9 promotes the
measured bridge with P7's actor loop and P8's helpers; otherwise replan the
language-specific steps. P11 migrates executable experiments; P12 cuts over
configured consumers and removes implicit
public-portfolio strategy. The roadmap owns these dependencies and their
acceptance criteria; this sketch does not introduce another prerequisite chain.
