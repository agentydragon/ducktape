# Actor-facing policy interfaces

Target design for P1–P12 and gates GP/GL/GE in [the roadmap](roadmap.md),
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

Submission, acceptance and execution are distinct. Results identify the originating
request and report actual quantities/amounts or rejection. A collection of actions
does not imply an all-or-nothing transaction; each supported action defines its
execution/settlement conditions. Partial execution is supported only where modeled.

## `policy.py`

```python
"""Actor decisions and memory, independent of financial execution."""

@dataclass(frozen=True)
class Response:
    actions: tuple[Action, ...]
    review_at: datetime | None

def decide(
    observation: Observation, memory: MyPolicyMemory,
) -> tuple[Response, MyPolicyMemory]: ...
```

The experiment owns the policy's memory and function; no required superclass or
registry. `review_at` requests a reminder; `None` means wait for relevant events.
Neither postpones claims or suppresses world events. Record an intended sale at
request time and cash raised only after its execution result arrives. A budget
cut is a policy decision, not inferred from a payment failing.

## `simulation.py`

```python
"""Own financial state, execution, event ordering and decision delivery."""

class Session:
    def start(self) -> DecisionBatch | Finished: ...
    def advance(self, responses: Batch[Response]) -> DecisionBatch | Finished: ...
```

A decision batch contains pending observations plus routing identities for the
actor/path/decision. Responses correspond to those pending decisions; execution
produces consequences before the next observation.

The calling experiment can own the outer decision loop: start the session,
dispatch pending observations to policies, submit their responses, repeat until
finished. A library `run(...)` helper can own that loop when custom orchestration
is unnecessary. The executor's `advance` owns financial time evolution between
decisions: calendar/event ordering, accruals, settlement and taxes. The caller does not
reimplement those rules or advance past unanswered decision opportunities.

Decision opportunities follow information arrivals and opportunities to act, not
names of internal engine phases. Monthly resolution can be an explicit study
approximation; this does not require a general-purpose event scheduler. Ordering
between actors in one world is an environment rule, not an accident of batch
order. Books and paths stay in the existing executor; native and Python drivers
share its mechanics.

The initial stepping/bridge work uses Rust execution. RUNTIME/GE separately
reevaluate that language choice, including execution strategy, ragged output
layout and notebook usability. A Python executor would own the same financial
time evolution; the economic boundary is not a permanent Python/Rust boundary.

### Batching: requirement versus open API choice

High-N execution must support batched observation/action transfer. A candidate
policy signature is:

```python
def decide_batch(
    observations: Batch[Observation], memory: Batch[MyPolicyMemory],
) -> tuple[Batch[Response], Batch[MyPolicyMemory]]: ...
```

Whether batch functions are the primary authoring API or an optional fast path
alongside scalar functions remains open for P6/GL. The scalar sketch describes
one decision's meaning, not a requirement for per-path Python callbacks from Rust
workers or two permanent APIs. Compare a scalar-policy adapter with a batch-native
policy on the same workload before choosing layout and authoring surface.

`Batch` leaves row/column layout, ragged actions/lots and chunk size undecided.
Only active pending decisions are presented; paths may be at different times.
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
The executor must not run another funding or rebalancing strategy afterward.
Autopay or delegated liquidation requires an
explicit standing instruction with modeled terms. Non-mutating tax/trade previews
reuse canonical calculations with observable inputs and explicit assumptions,
never the future realized path.

## Acceptance and remaining choices

P7's first scoped example follows **bill arrives → actor requests sale → funds
become available → actor pays bill**. Verify request/result identity, lot/basis/tax
reconciliation, and that ignoring a sleeve helper causes no hidden engine trades.
On a rejected request the actor can choose another action at a modeled decision
opportunity; missed deadlines still have consequences. The environment does not
silently sell something else, cut spending or borrow.

GP resolves P7's information/review times, same-time action order,
execution versus settlement availability, and rejection/deadline/stop behavior.
Broader partial-payment, default and recovery models are separate scoped changes.
The existing opening-month/all-or-none cases remain controls for P1/P2/P6, not
the destination contract. P6/GL decides policy-call representation; RUNTIME/GE
decides executor placement. If GE retains hybrid execution, P9 promotes the
measured bridge with P7's actor loop and P8's helpers; otherwise replan the
language-specific steps. P11 migrates executable experiments; P12 cuts over
configured consumers and removes implicit
public-portfolio strategy. The roadmap owns these dependencies and their
acceptance criteria; this sketch does not introduce another prerequisite chain.
