# Actor-facing policy interfaces

Target design for the remaining migration and gates GP/GL/GE in [the roadmap](roadmap.md),
not implemented API declarations. Module names and types below are sketches;
reuse existing domain types and introduce fields only for a supported consumer.

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

The initial action session uses Rust financial-step execution. RUNTIME/GE separately
reevaluate those internals, including execution strategy, ragged output layout and
notebook usability. They do not gate interface consolidation or decide who owns
the loop. A Python implementation of a step would own the same financial time
evolution; it replaces the internals, not the session's economic contract.

### One batch-shaped policy API

The engine, caller-owned loop and convenience runner all use the same batch-shaped
callable. A singleton batch is the one-rollout case, not a second interface. An
optional scalar-to-batch helper may invoke an author's scalar function once per
row while routing its per-path memory; it returns the same keyed response batch.
Neither the engine nor runner dispatches through a separate scalar policy hook.

GL compares that adapter with a directly batch-authored function on the same
workload. Start with one concrete typed representation and use measured costs/usability
to revise it atomically, not to postpone convergence or maintain scalar and batch
engine APIs. A failed cost check blocks the affected workload's cutover, not all
consumer migration.

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

The landed native actor loop in `rust/engine/actors.rs` observes a bill and accepts
`[Sell(...), PayClaim(...)]` in one batch response. Its native tests cover immediate
sale cash, request/result identity, lot/basis/tax reconciliation and a
non-sells-first action chain. The landed `x/monthly_actions` consumer has CI
coverage of its real CLI and generated financial inputs; P9 migrates it to the
Python session and removes its native callback driver.

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
The existing opening-month/all-or-none cases are spending-probe controls, not
the destination contract. Migrated consumers must explicitly test and explain
timing/funding differences rather than hide them in a compatibility runner.
P9 consumes P8's declaration slice and replaces the native actor driver with the
general Python session. P11 uses helpers and compact capture to migrate experiments
and retire the scalar amount/weight callbacks plus spending-only Python prototype.
P12 migrates configured consumers and removes old full-run loops and implicit
public-portfolio strategy, preserving required existing housing/PE capabilities.
RUNTIME/GE can later change step internals without delaying this sequence.
The roadmap owns dependencies, per-consumer deletion checkpoints and acceptance;
this sketch does not introduce another prerequisite chain.
