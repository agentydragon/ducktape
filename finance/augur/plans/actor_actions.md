# Actors, actions, and the seam between them

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

The word is **action**, and the variants are `Pay`, `Buy`, `Sell`.

An intermediate draft of this document called them "instructions", reasoning that "action"
had invited the vagueness of the first attempt. That was a misdiagnosis. The first attempt
was wrong about SHAPE — it carried sleeve aggregates — and no noun would have prevented
that. Meanwhile "instruction" actively imports the wrong connotation: an instruction is
something submitted, which may be declined. These are not. Affordability is the policy's
job, so non-execution is either ruin or a bug, never a routine decline.

"Action" carries the right meaning natively — in the RL sense an action is APPLIED to the
environment, not requested of it — and the box emitting them is already called a policy.

Concreteness lives in the variants, which is where it belongs and where the first attempt
lost it:

| Action | Fields                                                     |
| ------ | ---------------------------------------------------------- |
| Pay    | recipient agent + account, source account, amount          |
| Buy    | instrument, account, amount in **dollars** or in **units** |
| Sell   | instrument, account, amount in **dollars** or in **units** |

There is no "best effort" flag and no optional variant — see "what a shortfall means"
below. An action means **do this**, and that is the only thing it can mean.

**Executing an action produces ledger entries** — the statement lines. That is where the
word "happened" belongs, and keeping the two named separately is what stops the emitted
type from having to mean both.

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
  specific-ID and HIFO are future variants of an existing action rather than a new
  boundary. The default stays the account's cost-basis method, which is FIFO here.
- **There is no "spend" action.** Spending is `Pay`, to whoever is being paid. Nothing
  in a bank app is labelled "spend" — you pay a landlord, a shop, a person. Keeping a
  separate spend variant would smuggle the anonymous-sink modelling back in, and with it
  the assumption that consumption has no counterparty. Discretionary spending is a policy
  choosing to emit `Pay`; a lifestyle tier changes how much and to whom, not which action.

## Where the metaphor stops: what a shortfall means

The broker test guides what fields an action **has**. It is no guide to what happens
when one **cannot execute**, because in a broker UI a rejection is free — the order bounces
and you shrug. Read the metaphor that far and the emitted type quietly acquires
silently-skip-on-failure semantics, which is wrong: trying to spend $1,000 without $1,000
must not be a no-op.

There is exactly one rule: **an action that cannot execute fails the rollout.** No optional
actions, no best-effort mode, no partial fills.

An earlier draft of this document had a `required` flag, so that a discretionary action
which could not be funded would abort the run as a policy bug while a required one failed
the rollout as ruin. That is wrong, and instructively so. A flag whose false value means
"and if not, never mind" **does the policy's job for it** — the whole point of putting
affordability on the policy is that deciding what to do with less money is a decision, and
decisions belong in the box that makes decisions. Offer the fallback and policies will lean
on it; a boolean named `required` all but advertises that `required=False` should quietly
skip.

So the choice a policy faces is not "ask for $1,000 and see". It is: emit `Pay($500)`, or
emit `Pay($1,000)` and be ruined. Both are real answers, they are genuinely different, and
choosing between them is exactly the behaviour being modelled. An engine that resolved it
would be making that choice on the policy's behalf, badly and invisibly.

This also removes any need to distinguish ruin from a policy bug at execution time. Both
surface identically — a failed rollout with a recorded reason — and which one it was is a
question about the policy, answerable from that record. It is not something the type system
should encode, and trying to is what produced the flag.

**Joint affordability is the policy's problem too.** A month's actions must be affordable
_together_, not one at a time: a policy emitting rent and then a discretionary purchase it
can only afford separately has emitted an unaffordable set. Execution still needs a
deterministic order so that _which_ action fails is reproducible, but that is a tie-break
for diagnosis, not a priority scheme a policy may plan around.

## Everything that acts is a policy

A policy is a box in the RL sense: observations in, actions out.

The `Scheduled*` and `Recurring*` config types are a **shortcut taken before agents had
policies**. The right model is that a scheduled buy is executed by a policy that watches
the clock and emits "buy this" when the month arrives. There are not two producers of
actions — a compiler and a policy — there is one, and a schedule is the simplest possible
policy.

This is a conceptual unification, not necessarily an expensive one: the dense form of a
clock policy is precisely the per-month action table the compiler already builds. The
shortcut was not wrong as an implementation; it was wrong as a concept, because it made
schedules a separate _execution path_ rather than a degenerate policy. Unifying lets one
executor serve both.

**Obligations reduce too**, and more cleanly without the flag. A recurring obligation is a
clock policy emitting a `Pay`. The must-pay property that makes `failed_month` mean anything
needs no special marking, because every action is must-pay: an unfundable `Pay` fails the
rollout whether it came from a lease or from a whim.

That beats keeping obligations separate for a concrete reason, not tidiness. A separate
channel fixes the amount at config time, so it cannot express a payment whose amount a
policy chooses — and rent under a lifestyle tier is exactly that. The parallel channel would
have had to grow a policy hook anyway, at which point it is the action path with extra steps.

It also retires the question of whether required payments must settle before discretionary
ones. There is no such distinction to order: a month's actions must be jointly affordable,
and if they are not, the policy emitted a set it could not fund. What the engine owes is a
deterministic order so the failure is reproducible, not a priority scheme that would let a
policy under-plan and be rescued.

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
  shock at _m+1_ must produce identical actions at _m_.
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
  must emit zero actions.
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
  approximation, and sim calculates rather than approximates. Until then, an action selling
  one must be rejected loudly.
- **Private equity is not purchasable.** It is marked, not priced, so an order has no
  defined quantity.
- **Basis and purchase month are carried as history but never change.** Slots are never
  reused, so `(lot, rollout)` final state would cost a factor of `snapshot_months` less —
  361x at a 30-year horizon. Separable from the behavioural work; changes the decoded
  per-month frame, so it should be a deliberate change rather than a side effect.

## Build order

Fixes and cleanups first, features last. Everything in phases A and B makes the engine more
correct, more symmetrical, or less duplicated without adding capability, so each is cheap to
review and none of them can be blamed for a later behaviour change.

### Phase A — things that are wrong (DONE)

**A1. Every cash write needs a counterparty.** Landed in #3753.

The plan said "sales must credit `rest_of_world`" and listed the sale paths. That framing
was too narrow, and the way it was too narrow is the lesson: an audit of EVERY `cash.at[...]`
write found **five** one-sided phases, and the fifth runs the other direction.

| Phase                    | Was                               | Direction    |
| ------------------------ | --------------------------------- | ------------ |
| Scheduled asset sales    | credited proceeds, debited nobody | mints        |
| Liquidity-policy sales   | same, per pool                    | mints        |
| Private-equity tenders   | same                              | mints        |
| Property sales           | same                              | mints        |
| **Capital improvements** | debits the owner, credits nobody  | **destroys** |

Searching for "places cash is credited from a disposal" walks straight past the last one.
Pairing off every write finds it. `external_cash_slot` went from 3 occurrences to 10 in a
3321-line engine.

Verified negatively, which is the only verification worth anything here: with the fix
reverted and the new tests in place, 6 of 8 fail with the expected mints. The conservation
scenario now holds lots, properties, policies and PE, and each sub-scenario asserts its
disposal actually fired so conservation cannot pass vacuously.

Left deliberately, and pinned by a test so a later edit must choose: on a property sale the
contra entry is the NET, because the mortgage payoff extinguishes a liability with no cash
leg at all — the lender is never credited, even when modelled. Booking the gross would break
conservation by exactly the payoff. Paying the lender is a modelling change, not ledger
symmetry.

**A2. Delete the dead numpy FIFO.** Landed in #3750, −233 lines. `lot_order_for_pool` was the
one live symbol and moved to `compiler/plan.py`; its coverage went from 1 case to 3.

**A3. `sim/tax.py` is a second dead shadow, and worse.** Found by the B5 sweep, not by this
plan. 190 LOC whose only consumer is its own test, carrying 17 hand-computed federal/CA
bracket cases — while the SHIPPED bracket math (`_apply_brackets`, `_apply_ltcg_brackets`,
`_net_capital_gains_jnp`) has none. Its docstrings cite `phases.` and `step.py`/`apply.py`,
none of which still exist.

Decided: JAX is the only implementation. The cases port onto the shipped functions and the
shadow goes. Not a pure move — the two differ in arithmetic (integer cents vs float64), so
each ported case is a decision about which value is right, with JAX winning by virtue of
being what runs.

### Phase B — things that are inconsistent

**B3. State the numpy/jnp rule and enforce it.** Landed in #3752. numpy for compile-time
STRUCTURE and for DECODE; jnp for traced VALUES. It also records the two consequences that
bit: a numpy twin of something the engine does in jnp cannot be called from the scan, so it
is dead or drifting; and a traced value cannot drive a Python `raise`, so anything that must
fail loudly belongs at config time.

**B4. Lot attributes that never change stop being history.** Landed in #3751. Cost basis is
`(lot, rollout)` rather than `(snapshot, lot, rollout)` — 361x less at a 30-year horizon for
identical information, and the buffer the purchase-slot budget is priced against.

Purchase month is still a static plan column, which is the remaining half of the same
asymmetry. It becomes per-rollout when policies decide WHEN to buy (phase D).

**B5. Missing tests, and test theater.** The sweep ran; these are its results.

Two failure modes producing the same illusion — green meaning safe — needing different
detection.

_Missing_: a real path with no test. A1 existed because no conservation scenario ever held a
lot.

_Theater_: tests that exist, pass, and guard nothing. Three kinds, each with a way to find it:

- **Tests of dead code** — check whether the module under test has a non-test importer. Found
  the numpy FIFO (A2) and `sim/tax.py` (A3).
- **Tests that survive mutation of what they claim to test.** Break the function deliberately
  and see what still passes. `test_agents_are_independent_taxpayers` passed under a mutated
  income-bucket function because its scenario was too symmetric to notice.
- **Docstrings claiming more coverage than the scenario delivers.** The conservation test
  said it catches "every leak, anywhere" while its scenario had no lots. Not fake, but
  overstated — and the overstatement is what stopped anyone looking.

The last is the most corrosive, because the claim is what future readers trust instead of
re-deriving the coverage. It is also the direct cause of A1.

**B7. Rental-lifecycle tests recompute the answer they assert.** Also from the sweep. The
helper computes `rent x fraction x (1 - vacancy)`, feeds it in as the transfer amount, then
asserts the engine paid it — and `vacancy` / `management_fee` do not exist under `sim/` at
all. The real lowering is covered at `product/service_test.py`, so this is theater rather
than a coverage hole, but four test names and a "headline accounting test" claim coverage
that cannot fail.

**B6. Money in cents at the boundaries** (#3741). **Sized and deferred out of this batch**:
25 `*_usd` config fields, 77 `pl.Float64()` decoded columns, 141 wire/frontend references,
and ~1168 `_usd=` construction sites, overwhelmingly in tests. That is a program of work, not
a PR, and bundling it would stall everything behind it. The natural split is that the DECODE
side (cents as source of truth, float as derived rendering) is separable from the config
side, where nearly all the call sites are — and the decode half alone buys exact
reconciliation of decoded frames, which conservation currently has to reach into raw buffers
to get.

### Phase C — unify execution, without changing behaviour

**C7. One action type and its executors.** Engine phases become executors of a single
vocabulary. Verified by every existing scenario producing identical output — the strongest
signal available, and available only while behaviour is unchanged.

**C8. Schedules become clock policies.** `Scheduled*` / `Recurring*` lower to a clock policy
emitting actions; the parallel execution paths are deleted. Still behaviour-preserving.

### Phase D — new capability

**D9. Purchase slots** with a runtime cursor, per-rollout purchase month, and the exhaustion
abort.

**D10. The target-allocation policy**, with `allocation.py` and `cash_band.py` as internals
and the observation type from the closed PR. Deletes `LiquidityPolicy` outright, including
the full-stack wire/product/frontend change: ordered sell-list to per-holding integer
weights, trigger/amount to floor/ceiling.

**D11. Policy-chosen payment amounts.** `AmountSpec` is structurally closed to simulated
state, which is what makes spending config rather than a decision today.

**D12. Tier state**: policy-internal mode, hysteresis, an explicit one-month lag, and a
declared precedence against rebalancing so the substitution between cutting spending and
selling assets is configured rather than decided by evaluation order.

### Independent

The jointly sampled par-yield path and bond mark-to-market, both needed before a bond can be
a sleeve at all — until a discount curve exists, a pre-maturity bond sale has no price the
simulator can calculate.

## Superseded

PRs #3745 (policy arithmetic and observation) and #3746 (decide/execute split) were closed
against this replan. Their content returns at steps 2 and 5; the arithmetic and the
observation type survive unchanged, the aggregate boundary does not.
