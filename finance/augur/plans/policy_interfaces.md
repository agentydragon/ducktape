# Actor-facing policy interfaces

Target design for the remaining migration and gates in [the roadmap](roadmap.md).
The [GWORLD and GMETRICS decisions](library_design_gates.md) are still open:
these sketches do not finalize a public World, component-registration/step
protocol, or metrics collector. Preserve the settled economic action contract
while comparing those alternatives.
Reuse the common `ActionSession` and its single batch contract. Public requests
already belong to `sim/actions.py`, current facts to `sim/observations.py`, and
lifecycle to `sim/session.py`; there is no public native wrapper counterpart.
The richer types below are sketches, not additional API declarations. Extend
existing domain types only for a supported consumer.

The boundary is economic agency: a policy sees information available to its actor
and requests actions that actor could take. The environment owns contracts,
execution and consequences. **Python owns the outer loop for every consumer**, including
the app as it migrates. Financial execution is Python-owned. A World may still
coordinate all participating economic objects and enforce cross-object invariants;
experiment ownership of the loop does not rule that out. GWORLD chooses the
public ownership and lifecycle mechanism, not whether accounting duties matter.
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

`tax_records` above is still a target, not a field on the Python observation.
CAP must expose actor-scoped recorded income, jurisdiction gain/carryforward
facts and assessed outstanding liabilities from the existing Python accounting records
views when a tax-aware policy needs them. Current TLH value/basis and payment
claims are not a substitute. Test visibility after the month's modeled losses,
after a prior sale and across year-end/reset; exclude future assessments and
other actors' facts. Reuse the canonical records, not a policy-side tax ledger.

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

type Action = Sell | Buy | Transfer | PayClaim | Consume | Contribute | Withdraw | Liquidate
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
orchestration is unnecessary; the app must migrate its remaining configured
consumers to the agreed interface. The sketch does not require retaining this
exact Session/World class split. Under the current contract, `advance` owns financial time evolution between
decisions: calendar/event ordering, accruals, settlement and taxes. The caller does not
reimplement those rules or advance past unanswered decision opportunities.

The first actor-action example has one decision-making household and rule-driven
counterparties: apply scheduled cashflows/events and assemble due claims, observe
and decide once, execute the ordered actions, then stop if any due claim remains
unpaid. Its supported trades retain their explicitly declared immediate-cash
control; this is not a promise about real products' settlement delays.
Ordering between additional decision-making actors and expanded product/housing
timing remain GP choices, not an accident of batch row order. The executor keeps
ordinary books and prepared paths; the opaque Python TLH component owns its
private holdings and supplies settled effects/statements. Python unit tests may drive the step primitives;
production consumers converge on the Python loop, not two supported drivers.

Future PE support distinguishes mandatory issuer events from holder decisions.
Forced recovery executes without a policy opt-in. Each presented sale opportunity
requires an explicit sell or decline response; missing one is invalid, not an
implicit decision to hold. Response coverage belongs to the same monthly batch,
not a second callback or retry loop. This requirement is recorded for deferred PE
work; the current action session does not implement it.

The current session uses Python financial steps. Keep one canonical implementation
and migrate callers atomically when improving domain composition. GL and RUNTIME/GE are parked optimization work,
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
quantity calculations are also Python-callable. P12 retires remaining implicit
configured-policy readers; it does not need another language port. Add another helper
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

## Opaque TLH portfolio composition

The approved concrete `TlhPortfolio` is Python-owned and described in the
[TLH contract](../docs/tlh.md). Its investor view contains value and reported tax
basis, not private cohorts/harvesting memory. Contributions, gross withdrawals
and liquidation use the common ordered-action contract. The policy does not
manufacture losses or trigger monthly harvesting.

The Python session advances each component once before investor operations,
including scheduled/configured redemptions, then settles its financial effects
through direct Python-world financial calls. Candidate state is adopted only with accepted
cash/tax settlement. Accounting/capture may retain immutable reporting statements,
never a mirrored mutable position/basis book. Whether the experiment supplies these
components to World or another coordinator is a GWORLD decision. No custom exception taxonomy, model
callback handoff or generic managed-account API is needed.

The [paired TLH study](managed_portfolio.md) remains future work. Tax-aware rules
additionally need the CAP tax-observation slice above; fixed investor-flow
controls do not.

## Measurement ownership remains a design gate

Experiment-authored per-step extraction, such as appending selected account
balances to a metrics list, is a candidate rather than a finalized API. Optional
recorders/observers and hybrids are also candidates under GMETRICS. A coordinating
World may provide consistent observation points without owning every metric.
Financial correctness, taxes and visible failed/unpaid outcomes cannot depend on
whether a metrics collector was installed. RECORD implements the selected design;
CAP can still add a concrete missing factual view without waiting for a collector
framework. See the gate note for timing, scope, copy-safety and comparison evidence.

## Acceptance and remaining choices

`x/monthly_actions` authors a Python batch policy and advances the common action
session, including its population/profile and selected-replay entrypoints. Its CI
controls cover immediate sale cash, lot/basis/tax reconciliation, fatal action
prefixes and summary/trace agreement with generated financial inputs. Python tests
exercise world operations directly; session tests own lifecycle, batch
routing and claim-authority controls. Retained historical receipts must not retain
executable claim authority or permit request mutation to rewrite history.

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

P12 migrates the remaining configured Python consumers to common actions/results
and removes implicit public-portfolio strategy, preserving required existing
housing/PE capabilities. The app and legacy acceptance
readers remain. Their configured allocation proposer is Python-owned and reuses
shared sleeve helpers; retiring its implicit schema/orchestration is distinct from
moving the strategy's implementation. The remaining `engine.rs::simulate*`
configured helpers are test-only, not a second public driver.
ACCEPT moves supported consumers to the common session. Existing Python funding, common
reporting and held-bond capture are reused, not reimplemented. New `product/`
features are deferred; its remaining adapter work must simplify existing behavior
or retire legacy execution. The roadmap names the narrow
committed-purchase and private-equity timing gates for complete app cutover.
Configured source-account claim grouping is all-or-none; each migration must test
and explain timing/funding differences rather than hide them in a compatibility
runner. GP gates only the additional product/multi-actor semantics a slice needs.
RUNTIME/GE can later change step internals without delaying this sequence.
The roadmap owns dependencies, per-consumer deletion checkpoints and acceptance;
this sketch does not introduce another prerequisite chain.
