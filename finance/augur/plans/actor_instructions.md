# Actors, instructions, and the seam between them

Design for how agents act in augur. Written after a first attempt cut the boundary in the
wrong place; this records both the target shape and the reasoning, so the wrong cut does
not get re-derived.

## Why the first attempt was wrong

The first attempt gave a policy the output type
`(sell per sleeve, buy per sleeve, spend)` and called it `ActorActions`.

That is not an output at all. It is **intermediate state** inside one policy — what a
rebalancing actor computes on its way to deciding what it wants to do. Promoting it to the
boundary meant:

- an actor could not pay a **named** counterparty (spending went to an anonymous sink);
- an actor could not order in **units**, only dollars;
- an actor could not name an **instrument**, only a sleeve aggregate, and sleeves are a
  policy's abstraction that does not exist outside it;
- therefore any other kind of actor would need the **boundary** changed rather than a
  different policy written behind a stable one.

Every policy emits the same type. Aggregates are internal.

## The vocabulary: what you could click

The word is **instruction**, not "action". It is worth being strict about, because "action"
invites the vaguer reading the first attempt fell into — an abstract thing an agent wants —
whereas an instruction is a concrete, executable order with a named subject and an amount.
A policy emits instructions.

An instruction is a thing you could click in a broker web UI or a bank app. That test
decides most of the open questions, so prefer it over reasoning from the simulator's
internals.

| Instruction | Fields                                                     |
| ----------- | ---------------------------------------------------------- |
| Pay         | recipient agent + account, source account, amount          |
| Buy         | instrument, account, amount in **dollars** or in **units** |
| Sell        | instrument, account, amount in **dollars** or in **units** |

Consequences that fall out of the test rather than being chosen:

- **Per-instrument, never per-sleeve.** The broker has no idea what a sleeve is.
- **An over-order is rejected, not silently resized.** Clicking "sell 10 shares" while
  holding 5 is an error, not a 5-share fill. So computing an affordable amount is the
  **policy's** job, and what it emits must already be affordable. (This reverses the
  clamp-to-available behaviour in `ScheduledAssetPurchase`, which is really an _intent_
  — "invest my surplus" is not a thing you can click.)
- **Dollar- and unit-denominated orders are separate variants**, not one with a mode flag:
  the rounding rules differ (whole quanta vs. exact units) and so does what "insufficient"
  means.
- **Lot selection is an optional field, added later.** Brokers do let you pick tax lots, so
  specific-ID and HIFO are future variants of an existing instruction rather than a new
  boundary. The default stays the account's cost-basis method, which is FIFO here.
- **There is no "spend" instruction.** Spending is `Pay`, to whoever is being paid. Nothing
  in a bank app is labelled "spend" — you pay a landlord, a shop, a person. Keeping a
  separate spend variant would smuggle the anonymous-sink modelling back in, and with it
  the assumption that consumption has no counterparty. Discretionary spending is a policy
  choosing to emit `Pay`; a lifestyle tier changes how much and to whom, not which
  instruction.

## Everything that acts is a policy

A policy is a box in the RL sense: observations in, instructions out.

The `Scheduled*` and `Recurring*` config types are a **shortcut taken before agents had
policies**. The right model is that a scheduled buy is executed by a policy that watches
the clock and emits "buy this" when the month arrives. There are not two producers of
instructions — a compiler and a policy — there is one, and a schedule is the simplest
possible policy.

This is a conceptual unification, not necessarily an expensive one: the dense form of a
clock policy is precisely the per-month instruction table the compiler already builds. The
shortcut was not wrong as an implementation; it was wrong as a concept, because it made
schedules a separate _execution path_ rather than a degenerate policy. Unifying lets one
executor serve both.

**The one case needing care**: obligations. A bill arriving is not something you clicked,
and failing to pay one is ruin rather than simply not acting. Whatever they reduce to, the
must-pay property has to survive — it is what makes `failed_month` mean anything.

## Invariants

These were arrived at with reasons; the reasons matter more than the rules, because a rule
without its reason gets "simplified" away.

### The policy contract

- **Batched.** A struct _of arrays_: every field carries the rollout axis, and one call
  decides for all rollouts. No per-rollout Python. A learned policy drops into the same
  signature.
- **Observations carry only what is visible.** The agent's own accounts and lots, marked at
  this month's price, with per-lot basis and holding period. Not other agents. Not the
  `rest_of_world` contra row — an actor able to see it would read its own past spending as
  an asset.
- **Nothing from the future.** Two scenarios identical through month _m_ and diverging by a
  shock at _m+1_ must produce identical instructions at _m_.
- **Lot-level, not sleeve-level.** A statement shows lots, and lot identity is what
  tax-aware selection needs. Sleeve aggregation is a policy's own step.

### Execution

- **Double entry.** Money leaving the modeled world is credited to `rest_of_world`, so the
  cash tensor conserves across every transaction.
- **Integer cents throughout**, whole quanta only, sub-quantum remainder left in cash.
  Flooring the quanta and valuing them with the same helper the basis math uses keeps
  `spent <= budget` (`round(x) <= N` for `x <= ` integer `N`) and makes an immediate
  full-lot resale net exactly zero.
- **Cost basis is per-rollout.** A lot bought in month 3 carries the price _its_ rollout
  paid. Reading a compile-time column instead reports zero basis and books the entire
  proceeds as gain.
- **Buying settles after obligations**, so it can never starve an obligation into a false
  ruin, and is gated on the post-settlement failure mask so a failed rollout stops
  transacting immediately.
- **FIFO ordering stays compile-time derivable** while slots fill monotonically: a slot
  holds zero units until its purchase, and a zero-quantity lot contributes nothing to a
  walk that reaches it early.
- **Slot exhaustion aborts the whole run.** Failing only the affected rollouts would drop
  exactly the paths that traded most; trading tracks volatility; so the terminal
  distribution would be biased toward calm — a systematically optimistic answer. The error
  must name slots configured, slots needed, and which sleeve overflowed.

### Allocation arithmetic (one policy's internals, not a boundary)

- **Integer relative weights, not fractions.** A fraction is derivable from weights, so
  storing fractions stores a computed quantity and needs a float sum-to-one validator to
  defend it. `(3,1)` and `(30,10)` are the same policy.
- **The universe is what it names.** Anything unnamed is out of the target denominator.
  That is what makes an untradeable holding expressible — a target over private equity
  before liquidity leaves the policy permanently overweight something it cannot sell.
- **Water-filling, both directions.** A level `L` with `sum_i max(0, value_i - L*weight_i) = S`
  lands the portfolio _on_ target rather than nearer it. The deposit side is the mirror,
  and the two must be exact inverses.
- **Rebalancing rides cashflow only.** No periodic rebalance, no drift tolerance: turnover
  and its tax drag would swamp the effect the study measures. Zero drift plus zero cashflow
  must emit zero instructions.
- **Cash band is (s,S), refilling to the far edge.** Not mainly for turnover — refilling
  only to the floor puts the agent back at its trigger next month, making it a **forced
  seller into every dip**, which is the risk the whole exercise exists to price. Recorded
  counterargument: refilling to the ceiling realizes gains earlier, and deferral is worth
  money; far-edge vs near-edge is empirical and is the first rule to vary.
- **Sized once, at month start, on the projected end-of-month balance** (cash minus
  scheduled obligations — a calculation, not a forecast). One decision a month like a
  person, and it is what makes a later unpayable obligation mean _nothing was left to sell_
  rather than _the sale had not been attempted_.
- **Band bounds must share indexation.** A CPI-indexed floor against a fixed ceiling starts
  valid and inverts partway through the horizon; an inverted band has no interior, so the
  policy trades forever. Checked at config time, because per-month the bounds are traced.

### Money representation

Integer cents inside, float at both boundaries (`Scenario`'s `*_usd` fields, every
`pl.Float64()` decoded column). Tracked in #3741. The failure mode is not magnitude but
decimal representation: values that cannot round-trip, and decoded frames that cannot be
reconciled exactly.

## Deliberately deferred, with the direction of the error

Stated with direction because a deferral whose bias is unknown is a trap.

- **Tiers are bundles, not spending dials.** Moving changes the rent commitment, the
  `Location`, and **tax residency**. Currently handwaved to spending level only, keeping the
  starting jurisdictions — so a cheaper-location tier spends Czech amounts while paying
  California tax, and every result about it reads **optimistic**. One-off transition costs
  (moving, breaking a lease) are unmodelled too, and they are what make hysteresis an
  economic constraint rather than an anti-chatter device.
- **Bonds are not tradeable by a policy** until a discount curve exists. Selling one before
  maturity needs a price the simulator can _calculate_; book value would be an
  approximation, and sim calculates rather than approximates. Until then, an instruction to
  sell one must be rejected loudly.
- **Private equity is not purchasable.** It is marked, not priced, so an order has no
  defined quantity.
- **Basis and purchase month are carried as history but never change.** Slots are never
  reused, so `(lot, rollout)` final state would cost a factor of `snapshot_months` less —
  361x at a 30-year horizon. Separable from the behavioural work; changes the decoded
  per-month frame, so it should be a deliberate change rather than a side effect.

## Build order

Each step is independently landable and independently verifiable.

1. **This document.**
2. **The instruction vocabulary and its executors.** One type with a config form and a dense
   runtime form; the engine phases become executors of it. Verified by every existing
   scenario producing identical output — the strongest available signal, and only available
   while behaviour is unchanged.
3. **Schedules become clock policies.** `Scheduled*` / `Recurring*` config lowers to a
   clock policy emitting instructions, deleting the parallel execution paths. Still
   behaviour-preserving.
4. **Purchase slots with a runtime cursor**, per-rollout purchase month, and the
   exhaustion abort.
5. **The target-allocation policy**, with `allocation.py` and `cash_band.py` as its
   internals and the observation type from the closed PR. Deletes `LiquidityPolicy`
   outright, including the full-stack wire/product/frontend change: ordered sell-list to
   per-holding integer weights, trigger/amount to floor/ceiling.
6. **Discretionary spending as policy-emitted `Pay`.** Needs an obligation's amount to
   become policy-emitted; `AmountSpec` is structurally closed to simulated state today, which
   is what currently makes spending config rather than a decision.
7. **Tier state**: policy-internal mode, hysteresis, an explicit one-month lag, and a
   declared precedence against rebalancing so the substitution between cutting spending and
   selling assets is configured rather than decided by evaluation order.

Independent of the above, and needed before a bond can be a sleeve at all: the jointly
sampled par-yield path, and bond mark-to-market. The float boundary (#3741) is independent
and arguably belongs before more money-handling code.

## Superseded

PRs #3745 (policy arithmetic and observation) and #3746 (decide/execute split) were closed
against this replan. Their content returns at steps 2 and 5; the arithmetic and the
observation type survive unchanged, the aggregate boundary does not.
