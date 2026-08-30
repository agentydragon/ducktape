# Actor actions and environment settlement

Augur models a partially observable actor in a sampled exogenous world. This document fixes the
boundary between the actor's policy, which decides what that actor tries to do, and the simulator,
which supplies facts and settles the decisions. It is deliberately narrower than a general
workflow or market-microstructure design.

## The boundary

A policy is a pure, JAX-traceable function from one actor's batched observation to that actor's
batched actions:

```text
ActorView[rollout] -> ActorActionBatch[slot, rollout]
```

The policy may propose only actions the actor can perform through capabilities compiled into its
slots. In particular, it cannot mint money, read another actor's private state, select an
exogenous price, move another actor's money, or decide whether an invoice exists.

The environment owns facts and outcomes:

- sampled prices, yields, wages, dividends, taxes, invoices, and counterparty behaviour;
- the current ledger/state and the public market data exposed in an `ActorView`;
- validation, atomic settlement, derived tax/liability effects, and emitted ledger events;
- the rollout-failure record when an action batch cannot be executed.

An **action** is what an actor controls. An **event** is a settled fact recorded by the
environment. A policy never emits an event, and an invoice is not an actor action. The invoice is
a fact the environment presents to the actor; the payment is the actor's `Pay` action.

## The action vocabulary

The initial vocabulary is intentionally small:

| Action | Runtime amount            | Meaning                                                            |
| ------ | ------------------------- | ------------------------------------------------------------------ |
| `Pay`  | integer money quanta      | move money from an authorized account to a named recipient account |
| `Buy`  | integer instrument quanta | buy a named instrument into an authorized account                  |
| `Sell` | integer instrument quanta | sell a named instrument from an authorized account                 |

`Pay` is the one spending primitive. Rent, a tax payment, a gift, and a restaurant payment differ
by recipient/cause, not by an extra `Spend` action kind. Discretionary categories are distinct
recipient/sink accounts such as `restaurants_sink`, not an alternate bucket abstraction.

A trade amount is units, never a money budget. A policy that wants to invest a money amount uses
its observed price and a documented rounding rule to choose the integer quantity. This keeps the
amount decision in the policy rather than letting settlement silently choose a quantity.

The contract uses integer counts of the configured currency's money quantum. The current engine
calls its implementation unit `cents`; a currency-quantum migration may rename or generalize that
storage unit without changing the action boundary.

## Dense action slots

The compiler fixes the action surface before tracing. Each actor receives a finite sequence of
capability-scoped slot specifications:

```text
ActionSlotSpec (compile time)
  kind: Pay | Buy | Sell
  actor_id
  from_account_id / custody account_id
  Pay: recipient agent_id + account_id, optional invoice/cause id
  Buy/Sell: instrument_id

ActorActionBatch (runtime)
  active[slot, rollout]: bool
  amount_quanta[slot, rollout]: signed? no -- non-negative integer
```

A slot's static metadata is the authority check. Runtime values only choose whether to activate
that capability and how much of it to use. This gives JAX a fixed-shape struct-of-arrays program
across all rollouts, avoids per-rollout Python and dynamic action lists, and permits a learned
policy to replace a clock or rule policy without changing the executor.

The first version should use non-negative amounts and one kind per slot. There is no generic
runtime recipient or instrument identifier, and no action that changes the compiled capability
set. Slots that are inactive carry zero amount; active slots must carry a positive amount.

## Observation contract

An actor's view contains only information available at the decision time:

- its authorized accounts and lots, with acquisition/basis data needed for its policy;
- current marks and the current price of every instrument it is authorized to trade, including
  instruments it does not hold;
- its current, environment-generated invoices/obligations and any policy state it carries;
- no other actor's private balances or lots, no `rest_of_world` contra balance, and no future
  values from sampled paths.

The existing `ActorView` and `ActorSlots` establish this direction. The current
`scheduled_outflow_cents` field is a transitional aggregate view of environment-created
obligations. The action boundary replaces it with policy-visible, fixed payment capabilities and
amounts, preserving fixed shape rather than making invoices dynamic Python objects.

## Execution and failure semantics

An action means _do this_, not _try this if convenient_. For every active action in a rollout:

- the slot capability and amount representation must be valid;
- `Pay` must be jointly affordable with every other outgoing action from the same account;
- `Sell` must have the requested quantity available under the configured disposal rule;
- `Buy` must be jointly fundable at the environment's current price; and
- required invoice coverage must be complete when the scenario/policy requires an invoice paid.

The environment validates the **complete batch** before it changes value-bearing state. It then
settles all actions or fails the rollout loudly and records a deterministic reason. It does not
partially fill, clamp, resize, reorder for affordability, silently omit an action, or provide a
best-effort flag.

A deterministic static slot order may identify the first diagnostic failure, but it is not a
priority scheme. Policies must make their complete batch jointly executable; they may not rely on
an engine ordering to rescue rent before a discretionary action.

This rule applies to both an accidentally unaffordable policy and a scenario that reaches ruin.
The failure record and the policy's trace explain which occurred. A `required=False` escape hatch
would move the decision back into the executor and is intentionally excluded.

The environment emits ordinary transfer, disposition, purchase, tax, liability, and failure
events after validation/settlement. These retain their accounting meaning; `ActorActionBatch` is
the decision trace that explains why those events occurred.

## What is current, and what changes

Today `TargetAllocationPolicy` is a funding policy:

- it observes aggregate upcoming obligation demand;
- it emits sleeve-level buy/sell quantities;
- the engine executes its sales, settles obligations itself, and executes its buys after
  obligation settlement.

That is a useful policy implementation, but it is not the general actor-action boundary. In the
target state, all outgoing `Pay`, `Buy`, and `Sell` decisions are emitted by actor policies through
compiled slots. The engine continues to own factual accruals, prices, validation, settlement, and
accounting effects.

A schedule is a degenerate clock policy, not a second execution authority. Existing
`Scheduled*`/`Recurring*` scenario definitions may continue to compile efficiently into static
per-month tables, but their eventual execution path is the same slot/action executor used by all
policies. Likewise, an obligation accrual remains an environment fact; a policy chooses the
corresponding payment.

## Incremental migration

Keep each change behavior-preserving and separately testable.

1. **Introduce the boundary.** Add compile-time slot specs, a JAX-pytree action batch, batch
   validation, failure diagnostics, and decision-trace read models. Existing policies can use an
   adapter initially.
2. **Move one payment path.** Compile one narrow clock-driven obligation class into `Pay` slots.
   Its policy reproduces today's payment amounts; compare the old and new ledger/failure outputs.
3. **Unify schedules.** Move scheduled and recurring transfers/purchases through clock policies
   and the shared executor, retaining their static tables as compiled policy data.
4. **Adapt funding.** Convert target-allocation sleeve decisions into per-instrument `Buy` and
   `Sell` slots. Policy-side sizing remains units-only; settlement owns neither sizing nor a
   fallback clamp.
5. **Add tier state and discretionary payment policy.** Carry tier state through the scan with
   explicit trigger/recovery hysteresis and transition timing. Tiers choose `Pay` amounts to
   configured sink accounts; they do not introduce a separate spending abstraction.

Every stage must preserve the invariants above, remain vectorized over rollouts, and add a test
that proves a deliberately unaffordable active batch fails rather than partially executing.

## Deliberately out of scope

This boundary does not add exchange microstructure, bid/ask spreads, fractional settlement,
partial fills, failed-order recovery, dynamic action creation, generic scripts, or a second NumPy
execution model. Add any of those only when a concrete Augur scenario requires it and its
fixed-shape, actor/environment ownership is specified first.
