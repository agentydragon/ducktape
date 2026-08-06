# Actors, actions, and the seam between them

Design for how agents act in augur, and the live plan for what is left. Written after a
first attempt cut the boundary in the wrong place; it records the target shape and the
reasoning, so the wrong cut does not get re-derived.

Phases A, B, C and D are done but for C8, D11 and D12 — the build order below carries the
status. **"What next" is the section to read** if you want the current plan rather than how
it was arrived at.

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

| Action | Fields                                               |
| ------ | ---------------------------------------------------- |
| Pay    | recipient agent + account, source account, an amount |
| Buy    | instrument, account, a **quantity**                  |
| Sell   | instrument, account, a **quantity**                  |

`Pay` moves money, so its amount is money. `Buy` and `Sell` move units, so their amount is
units — **never dollars**. See below.

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
- **Orders are denominated in units only.** A policy wanting to spend a dollar amount
  computes the quantity itself. This is the one place the broker test is OVERRULED rather
  than followed: real brokers do accept "buy $500 of VTI", so the metaphor would license a
  dollar-denominated variant — but the engine would then have to divide by a price and floor
  the result, which is the engine deciding how much to trade. That is precisely the
  clamp-to-available behaviour the rule above forbids, wearing a different hat. Supporting
  both would also mean two rounding rules, two meanings of "insufficient", and two ways for
  a policy to express the same order, so a test asserting on one says nothing about the
  other.
- **Lot selection is an optional field, added later.** Brokers do let you pick tax lots, so
  specific-ID and HIFO are future variants of an existing action rather than a new
  boundary. The default stays the account's cost-basis method, which is FIFO here.
- **There is no "spend" action.** Spending is `Pay`, to whoever is being paid. Nothing
  in a bank app is labelled "spend" — you pay a landlord, a shop, a person. Keeping a
  separate spend variant would smuggle the anonymous-sink modelling back in, and with it
  the assumption that consumption has no counterparty. Discretionary spending is a policy
  choosing to emit `Pay`; a lifestyle tier changes how much and to whom, not which action.

Units-only has two consequences worth naming before they surprise someone:

- **The observation must carry prices for instruments the agent does not yet hold.** A
  policy cannot turn "$500 of VTI" into a quantity without this month's VTI price. Today the
  observation is specified as the agent's own lots marked at this month's price, which is
  enough to size a sale and not enough to size a purchase. Prices are public, so this widens
  what an actor sees without breaching visibility.
- **Turning `ScheduledAssetPurchase` into a clock policy is where the division moves.** Its
  `amount_usd` does not disappear; it becomes config read by a clock policy that divides by
  the month's price and emits units. The engine stops dividing, which is the point — and
  that this works for the one dollar-denominated case that exists today is the evidence the
  rule is not merely tidier.

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
  this month's price, with per-lot basis and holding period, plus this month's price for
  every instrument it may trade — held or not, because orders are in units and sizing a
  purchase needs a price. Not other agents. Not the `rest_of_world` contra row — an actor
  able to see it would read its own past spending as an asset.
- **Nothing from the future.** Two scenarios identical through month _m_ and diverging by a
  shock at _m+1_ must produce identical actions at _m_.
- **Lot-level, not sleeve-level.** A statement shows lots, and lot identity is what
  tax-aware selection needs. Sleeve aggregation is a policy's own step.

### Execution

- **Double entry.** Money leaving the modeled world is credited to `rest_of_world`, so the
  cash tensor conserves across every transaction.
- **Integer cents throughout**, and a quantity is whole quanta. Valuing an order's quanta
  with the same helper the basis math uses is what makes an immediate full-lot resale net
  exactly zero. The companion property — that a budgeted purchase satisfies
  `spent <= budget` — moves to the POLICY along with the division. Two steps, and they round
  differently: the quanta are FLOORED, so the value they represent is at most the budget; that
  value is then ROUNDED to cents, and rounding cannot cross an integer bound it is already
  under (`round(x) <= N` whenever `x <= N` for integer `N`).
- **Cost basis AND purchase month are per-rollout.** A lot bought in month 3 carries the
  price and the month _its_ rollout paid. Reading a compile-time column instead reports zero
  basis (booking the entire proceeds as gain) and month 0 (making every later sale
  long-term).
- **An affordability clamp survives on the buy side, and the reason is not the policy.**
  The rule above puts affordability on the policy, and it holds: the policy sizes against
  the cash it observed. But settlement, a scheduled purchase, or a second policy on the same
  account can all spend between the observation and the execution, and an unclamped buy
  would then overdraw. Flooring the order keeps `spent <= cash`, so the clamp binds only
  when something else took the cash first — it is not the engine choosing a size.
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
- **Rebalancing rides cashflow by default.** A withdrawal or deposit moves the portfolio
  toward target for free, because the trade was going to happen anyway. Zero drift plus zero
  cashflow emits zero actions.
- **Drift-triggered trimming is opt-in and off by default** (`rebalance_tolerance`, #3813).
  Whether the tax drag of turnover is worth the drift it removes is what the study
  MEASURES, so a default that rebalanced would assume the answer. When configured it fires
  only in a month the band is quiet — the band's own water-filling is already the best
  rebalance that cashflow can buy — and it goes all the way back to target rather than to
  the tolerance edge, for the same forced-trading reason the cash band refills to its far
  edge.
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
- **Bonds are held, not traded.** `BondHolding` is par-purchase, held-to-maturity: coupons,
  CPI accretion for TIPS, redemption at maturity. Selling one early or marking it monthly
  needs a price, which nothing produces yet, so an action selling one must be rejected
  loudly. Note the direction of THIS error is favourable and worth stating: a held ladder
  genuinely is rate-immune, so not marking it is not a simplification — it is the accounting
  that matches the strategy. What is understated is flexibility, not risk.
- **Private equity is not purchasable.** It is marked, not priced. A unit-denominated order
  is perfectly well formed against it — the missing piece is the cash leg, since there is no
  price at which those units convert. So the rejection is at execution, not a malformed
  action.
- **Bond marking and pre-maturity sale.** See "What the exogenous layer owes" below: the
  blocker is a price the simulator can calculate, and the shape of that is now settled even
  though none of it is built.

## Build order

Fixes and cleanups first, features last. Everything in phases A and B makes the engine more
correct, more symmetrical, or less duplicated without adding capability, so each is cheap to
review and none of them can be blamed for a later behaviour change.

### Phases A and B — done

Engine fixes and cleanups, landed. Detail lives in the PRs; what follows is only what still
bears on the work ahead.

|     |                                                                 |                     |
| --- | --------------------------------------------------------------- | ------------------- |
| A1  | Every cash write has a counterparty                             | #3753               |
| A2  | Delete the dead numpy FIFO                                      | #3750               |
| A3  | `sim/tax.py` shadow deleted, JAX is the only tax implementation | #3754, #3756        |
| B3  | The numpy/jnp rule, written down                                | #3752               |
| B4  | Lot basis as final state, not history                           | #3751               |
| B5  | Missing-test and test-theater sweep                             | produced A1, A3, B7 |
| B7  | Rental tests that can actually fail                             | #3755               |

**What carries forward:**

- **Three ways a test lies, and how to catch each.** The sweep's method, reusable on every
  phase below. Tests of DEAD CODE — check for a non-test importer. Tests that SURVIVE
  MUTATION of what they claim to test — break the function and see what still passes. And
  docstrings CLAIMING MORE than the scenario delivers — the most corrosive, because the
  claim is what readers trust instead of re-deriving coverage, and the only one that needs a
  human to check the arithmetic. Every one of A1, A3 and B7 was a member of the third kind.
- **A rewritten test is worth nothing until it has been seen to fail.** Break the behaviour
  deliberately and confirm. This is how B7 was validated and how A1 was proven — 6 of 8 new
  conservation tests fail with the fix reverted.
- **The purchase-month asymmetry is closed.** B4 made basis per-rollout and left the
  purchase month a static plan column; #3797 and #3810 finished it, once policies could
  decide WHEN to buy. Both are now `(lot, rollout)` final state and both reach the decoded
  frame.
- **The mortgage payoff has no cash leg.** It extinguishes a liability without crediting
  anyone, even a modelled lender, which is why a property sale's contra entry is the NET.
  Pinned by a test so a later edit must choose rather than drift.

**B6. Money in cents at the boundaries** (#3741) — the one phase-B item NOT done. Sized and
deferred: 25 `*_usd` config fields, 77 `pl.Float64()` decoded columns, 141 wire/frontend
references, ~1168 `_usd=` construction sites, overwhelmingly tests. A program, not a PR. The
DECODE side is separable from the config side, where nearly all the call sites are — and the
decode half alone buys exact reconciliation of decoded frames, which the conservation
invariant currently has to reach into raw buffers to get.

### Phase C — unify execution — done but for C8

**C7a. One primitive moves money** (#3760). `_move_cash` takes both sides or moves nothing;
eleven phases call it and `cash.at[` appears once in the file. The one-sided write #3753 fixed
in five places is no longer expressible.

**C7b. One disposal executor** (#3789). This was blocked, and the blocker was real rather
than stylistic: three sale paths existed and the liquidity policy's target was denominated in
CENTS while the other two were in units. `_fifo_sell_cents` allocated a cent target across
lots and ceilinged EACH lot's slice to quanta; converting once and walking units ceilings
ONCE. Measured over 400k random trades: whole-unit assets never disagreed, but fractional
quanta did — 0.3% at scale 100, 47% at scale 100,000 (crypto), by a quantum each time.

D10 deleting `LiquidityPolicy` removed the last cents-denominated target and unblocked it.
One `_fifo_sell` now carries an `in_cents` parameter for the one caller that still needs it.

**C8. Schedules become clock policies — NOT done.** `Scheduled*` / `Recurring*` still lower
through parallel execution paths rather than emitting actions. It is the last piece of the
conceptual unification and it is behaviour-preserving, which also means it buys no answer.
See the ordering note below.

### Phase D — capability — done but for spending

**D9. Purchase slots** (#3804, #3810). Configured slot count per sleeve, a `(policy, sleeve,
rollout)` cursor in the scan carry, per-rollout basis and purchase month written at the fill,
and the exhaustion abort. Buys execute after obligation settlement, gated on the
post-settlement failure mask.

**D10. The target-allocation policy** (#3785, #3788, #3793). Deleted `LiquidityPolicy`
outright including the wire/product/frontend change; ordered sell-list became per-holding
integer weights, trigger/amount became floor/ceiling. #3793 put instrument prices in the
observation and made orders units-only, which is what let the buy side exist at all.

**D10b. Drift-triggered trimming** (#3813), which the plan had not anticipated. Selling an
overweight sleeve when there is no cash need is the one thing neither side of the band could
express, and a sleeve that quietly doubles was otherwise never sold down.

**D11. Policy-chosen payment amounts — NOT done.** `AmountSpec` is structurally closed to
simulated state, which is what makes spending config rather than a decision.

**D12. Tier state — NOT done.** Policy-internal mode, hysteresis, an explicit one-month lag,
and a declared precedence against rebalancing.

## What the exogenous layer owes

Marking a bond, or selling one before maturity, needs a price. The shape of where that price
comes from is settled even though none of it is built, and settling it removed most of what
an earlier draft of this plan had scoped as weeks of work.

**Consistency is the model's internal property.** Rai's framing:

> "prices should be rationally consistent" should just be a property that lives inside the
> structure of the exogenous model. i.e. that model knows about things like normal shapes of
> yield curves. and nothing downstream does.

So the interface is uniform — **every tradeable instrument has a price series**, and a bond
is marked exactly the way an equity is. Curve shape, no-arbitrage between rungs, and
pull-to-par at maturity are invariants the model maintains internally. Neither the sim nor
the engine learns what a discount curve is. This is the same principle that moved instrument
prices into the observation in #3793: what something trades at is a fact about the market,
so the exogenous layer hands it over rather than having the engine infer it.

Two intermediate positions were considered and are wrong by that principle. Emitting a
**yield** makes the sim carry a quantity it must convert. Emitting **discount factors** is
less leaky and still makes the engine learn to discount a cashflow, which is the model's job.

**Stated generally, because both of those were proposed and rejected one at a time before the
pattern was visible: emit observable PRIMITIVES, never derived RATIOS.** A ratio forces the
consumer to reconstruct one of its operands, and that reconstruction is the model's job leaking
downstream. A yield is `distribution / price`; a discount factor is a price of a unit cashflow.
Emit the price and the cashflow and every ratio is derivable by whoever wants one — including
the model itself, which is where the derivation belongs. The test is mechanical: if the sim has
to MULTIPLY the emitted quantity by something to recover dollars, it is a ratio and the wrong
thing crossed the boundary.

Three consequences, recorded because each was expensive to work out:

- **The positivity wall stops mattering.** The level stack is multiplicative and log-based,
  and positivity is enforced — not assumed — in ten places (`HistoricalSeries` rejects
  non-positive levels; the VECM fits `np.log(levels)`; anchoring is a multiplicative rescale
  by month-0 so a zero month-0 is unanchorable; `sample_sanity` raises rather than reporting
  a band miss). All of that constrains emitted LEVEL SERIES. A yield that is internal model
  state, projected into positive prices, never becomes a `LevelSeriesKey`, so none of those
  sites are on the path.
- **`PrivateEquityRiskModel` is the precedent.** It carries hazards, regimes, lockups and
  event priors — none of them level series — and emits marks. Rich internals, narrow emitted
  surface. A bond-pricing model has the same shape.
- **The instruments to price are scenario-defined, not fit-defined.** A ladder's rungs come
  from config, so the model must price an instrument it was never fitted on — "the path for
  a 4.2% coupon, 2041 CA muni". That is a pricing FUNCTION over internal curve state,
  evaluated per requested instrument, rather than a fixed factor set.
  `ExogenousSamplingRequest` already names what must be priced; this extends what can be
  named.

Two traps for whoever builds it, both found by survey and both silent:
`model/state_space_factor.py:55` hand-spreads the four level-key classes instead of using
`LevelSeriesKey`, so a new level kind is dropped from the state-space basis **with no type
error**; and `model/state_space.py`'s `_coupling_allowed` falls through to `return True` for
an unrecognized factor, silently coupling it to every macro factor at 0.5 shrinkage. Also:
`fit/evidence_data.py` aligns factors with an INNER join under a 36-month minimum, so adding
a series with a short history truncates the fit window for **every existing factor** —
DFII10 starts in 2003 and DGS30 has a 2002–2006 gap.

## How to model fixed income

The allocation question — "how should I allocate my current assets?" — cannot be answered
without a stock/bond mix, and the comparison anyone reaches for first is "60/40, rebalanced".
**That is inexpressible today.** Bonds are held-to-maturity, never marked, structurally excluded
from liquid net worth, and outside the target allocation, so fixed income cannot be a
rebalanceable SLEEVE. #3825 made a held ladder configurable end to end; it did not make it an
allocation.

Three paths, written down together because their costs are not comparable by intuition.

### A — bond funds as sleeves

A fund (BND aggregate, MUB national munis, CMF California munis) is a `SecurityKey`: already in
every union, already a legal `SleeveTarget.asset`, already anchorable.
Weights, rebalancing and trimming work untouched. It is also what FIRE portfolios actually hold
— nobody running 60/40 holds a ladder — and it avoids modelling one instrument per Treasury
maturity.

It needs three things, and the second silently doubles the return if missed.

**1. Distributions, which are the actual return mechanic.** A bond fund's return is
distributions plus price change; the price change mean-reverts toward zero over the fund's
duration while the payout is the durable part. Not a bolt-on.

The tax character is a VECTOR of fractions, not a tag — real funds are mixed and publish the
split (BND is part Treasury and part corporate; MUB is fed-exempt throughout but only its CA
slice is CA-exempt for a Californian). **This needs no new tax machinery**: split the
distribution into streams, each tagged with an existing
`InterestIncome(issuer_jurisdiction_id=…)`, and the income-bucket axis plus the per-link
inclusion mask already compute exemption per category. It is the muni-bond path called several
times with weights.

Those fractions are NOT exogenous: a fund's state breakdown follows its mandate, is published
annually, and is stable. They belong with the instrument definition, next to the price-series
declaration — not in portfolio config, which would let two positions in the same fund disagree
about what CMF is.

**2. The price series must become price-only.** Every security is fitted on Yahoo ADJUSTED close
(`loading.yahoo_adjusted_close_frame` reads `indicators.adjclose`), which already contains
reinvested distributions. A distribution stream on top of a total-return path counts the return
twice. Yahoo returns `close` in the same payload, so this is a loader change rather than a new
evidence source — and nothing would flag it.

**3. The distribution is exogenous, and crosses the boundary as DOLLARS PER UNIT.** It moves
with the fed rate and credit spreads, so it is a market fact belonging in the black-box half
rather than a config constant asserting a white-box answer about it.

But per the rule above, the quantity that crosses must be the primitive. A _yield_ would make
the sim multiply by market value to recover cash; a _distribution per unit_ is multiplied by
units held, which is the operation the sim already performs for price. So a fund emits two
dollar-denominated per-unit series — price, and distribution-per-unit — with the same shape,
units and handling. **No rate appears in the engine anywhere**: the mechanic is the coupon path
with nothing new, a bond paying `face x rate` and a fund paying `units x distribution_per_unit`.

Required for correctness, not only architecture. A payout that does not rise with rates gives
the crash without the recovery, **biasing against fixed income precisely in the regime a
stock/bond mix is chosen to survive.**

Cost: a distribution-per-unit is not a price, so it needs its own `LevelSeriesKind` — which
means both traps below apply, including the `state_space_factor.py:55` union that drops a new
kind from the state-space basis with no type error. Cheaper than pricing every rung; not free.

Gap worth naming: this generalizes to equity funds but `IncomeCategory` does not. It has
ORDINARY and INTEREST only, no qualified dividends, so distributions on an equity fund would be
overtaxed as ordinary income until a third category exists.

What A gives up: a fund is marked and traded, so it has no held-to-maturity rate immunity, and
the floor argument rests on exactly that. Over horizons longer than the fund's duration the two
converge; for near-dated spending they do not.

### How A gets FITTED, which the first draft got wrong

A's engine half landed in #3834 and #3837 and is right. Its evidence half — one fitted
`security_distribution:<symbol>` factor per fund, derived from `close` versus `adjclose` — is
wrong, and the reason is a rule this document already states one section up.

**The coupling between bond-derived quantities belongs INSIDE the exogenous layer.** BND, MUB,
CMF, a Treasury ladder rung and a fund's payout are all functions of one thing: the curve, plus
a credit spread and each instrument's duration. Fitting them as independent per-symbol factors
puts that relation outside the model, where BND and AGG can drift apart for no economic reason
and where a rate shock reaches one instrument and not another. It is exactly the mistake the
"consistency is the model's internal property" rule exists to prevent, arrived at from a
different direction: not a leaked RATIO this time, but a leaked STRUCTURE.

So the exogenous layer carries a latent rates/credit state and DERIVES every bond-derived
emission from it — each fund's price, each fund's distribution per unit, each bond's price if
that is ever wanted. Downstream still sees only per-instrument dollar primitives and never
learns they share a driver, which is why **the boundary type from #3834 survives this
redesign unchanged.** That is the payoff of having emitted primitives rather than yields: the
fitting strategy was replaceable without touching the interface.

**The structural consequence: emitted level series are no longer the same set as fitted
factors.** Today they are conflated — `FactorKey` is the level-key union plus PE marks, and
`evidence_data.factor_names` is both "what we fit" and "what we emit". A structural model emits
N series from K < N latent factors, so those two have to come apart before this can be built.

#### Measured, because the first draft asserted these from memory

Reproduced from the public upstreams the evidence repo caches (FRED/Yahoo/Zillow), so the
numbers are checkable without a checkout:

- **The aligned fit window is 96 months, 2017-12 .. 2026-06.** `_align_inner` inner-joins every
  factor, so ETH's 2017-12 start truncates all eight. Adding BND, MUB, CMF, VTIP or AGG costs
  **+0 months each, and +0 for all four together** — the recalled claim, now measured. SGOV
  costs **−29 months** (window starts 2020-10), so it stays out.
- **The sparsity is self-inflicted, and rates data is the opposite of sparse.** GS10 has 880
  monthly observations back to 1953 (73 years), FEDFUNDS 865 back to 1954, GS2 602 back to 1976,
  AAA/BAA 1291 back to 1919. A joint fit on the intersection spends 96 of GS10's 880 months. A
  structural model fits its rates block on the long history and couples blocks on the overlap;
  **that, not the fund list, is what the fixed-income work is really blocked on.**
- **`close` versus `adjclose` does recover the payout history, and the rate-sensitivity claim
  holds.** BND averages 2.56%/yr (2018-21) → 3.67%/yr (2024-26) against a −15.2% price return in
  2022; MUB 2.21 → 3.00 (−9.3%); CMF 1.90 → 2.80 (−10.0%). So a constant payout really would
  misstate the regime that matters.
- **The positivity wall DOES bite, and the earlier "it does not" was wrong.** Two distinct
  findings. (a) BND/MUB/CMF/AGG derive to exactly zero in ~4% of months, always January: those
  funds go ex-distribution near the end of December, so December carries 2–4x the median and the
  next month carries nothing — the Dec+Jan pair sums to 1.8–4.2x median, so the money is all
  there, mis-bucketed by a one-day boundary. An artifact, fixable by bucketing on ex-date. (b)
  **VTIP is structurally unusable as a level series**: 67 of 165 months non-positive, median
  monthly payout $0.0000, because a short-TIPS distribution tracks CPI accrual and is genuinely
  zero in low-inflation months. Drop VTIP from the fund list. Under a derived-from-the-curve
  model this constraint moves inside, where positivity can be imposed by construction.
- **Yahoo now downgrades `range=max&interval=1d` to monthly bars** (404 rows for SPY, against
  8436 with `period1=0&period2=<now>&interval=1d`). `sources.py` uses the former and
  `yahoo_adjusted_close_frame` requires >=1000 samples for SPY, so the next scraper refresh of
  that file would hard-fail. Unrelated to fixed income; recorded here because it was found here.

### B — price the ladder

Mark real bonds so a ladder can be a rebalanced sleeve and be sold pre-maturity, per the design
above. Costs the pricing model plus a forced carrier redesign: once bonds are BOUGHT during the
sim, rebalancing mints one per purchase and instrument count goes O(horizon).

### C — no fixed-income sleeve at all

Keep the ladder held-to-maturity and sweep its SIZE as a portfolio config rather than a weight.
Zero new work. This is the floor+surplus doctrine's own shape — the floor is pre-committed and
spent down, not rebalanced, and rebalancing is an equity-sleeve concept the construction never
asked for. What it gives up is comparability against the standard 60/40.

### Recommendation

**A, with C as the reference case.** C answers the doctrine's own question for free and should
run first regardless. A makes the standard comparison possible. B buys pre-maturity ladder
sales, which nothing has yet needed — and note that a rates process built for A prices a ladder
too, so B stops being a separate path and becomes a consumer of the same latent state.

A's cost estimate has moved: its engine half turned out to be small and is landed, and its
evidence half turned out to be a rates model rather than four fitted series. That is more work
than the first draft claimed, but it is work the rest of the study wants anyway — the fit window
it unblocks is currently degrading every existing factor, not just the fixed-income ones.

Two artifacts to record either way, both consequences of decisions right in general and awkward
for this sweep: **ruin means "could not pay from cash" and a held ladder cannot be sold**, so
long-dated rungs against near-term spending produce false ruins; and **with purchase slots
configured, a maturing rung that pushes cash above the ceiling gets invested in equities**,
which is wrong for money earmarked to be spent (the default of 0 slots is safe).

## What next

The engine is no longer the constraint. Equities, crypto and PE are modeled with a joint
fit; a held ladder including TIPS and muni tax character works; the funding policy raises,
invests and trims; federal and California tax including per-jurisdiction interest exemption
is in. **The remaining risk is that nobody has yet asked the model the actual question.**

So the order is outcome-first, and deliberately not phase-first:

1. **Build the real scenario and get a terminal-wealth distribution out of it.** Nothing
   blocks this — it is path C above, expressible today. It is also the only step that can tell
   us which of the rest is worth doing, and until it runs, every estimate of that is a guess.
   Expect it to surface plumbing gaps rather than modeling gaps.
2. **Spending as a policy** (D11, then D12). This is the one modeling gap that plausibly
   changes the ANSWER rather than its precision: flexibility is risk capacity, because a
   flexible spender is never a forced seller, so a working tier policy should REDUCE the
   floor the histogram calls for. Everything else on this list refines a number; this one
   moves it.
3. **A rates process inside the exogenous layer**, from which every bond-derived emission is
   derived. Path A's engine half is landed (#3834, #3837); what remains is the model, and it
   is the model rather than the instrument list because the coupling between a fund's price,
   its payout and a bond's price is the thing being modeled. Two prerequisites fall out of it
   and are worth naming separately, since either could be done first:
   - **The fit must stop requiring one common window across every factor.** `_align_inner`'s
     inner join lets crypto's 2017 start truncate a 73-year rates series to 96 months. Nothing
     about fixed income can be fitted credibly until that is fixed, and every existing factor
     is degraded by it today.
   - **Emitted level series must stop being the same set as fitted factors.** A structural
     model produces N emissions from K < N latent states; `FactorKey` and
     `evidence_data.factor_names` currently assume the two are one list.
4. **Whatever step 1 shows the answer is sensitive to.** Candidates, with the direction of
   their error where it is known: the tier/tax-residency bundle (reads optimistic, since a
   cheaper-location tier currently pays California tax); trading friction (recorded in
   `sim/TODO.md` with its direction); bond marking and pre-maturity sale, i.e. path B, which
   understates flexibility rather than risk and which path A mostly dissolves.
5. **C8 and B6**, which are hygiene. C8 unifies the last parallel execution path and B6 puts
   money in cents at both boundaries (#3741: 25 config fields, 77 decoded columns, 141
   wire/frontend references, ~1168 construction sites — a program, not a PR, whose decode
   half is separable and buys exact reconciliation on its own). Both make the code better
   and neither makes the answer better, which is why they sit below a question that is still
   unanswered.

The tempting order is 5, 2, 1 — finish the architecture, then add capability, then use it.
That order has been wrong at every previous step of this plan: C7b was unblocked by D10, and
the exogenous work above shrank by an order of magnitude the moment the question "do we need
a rate at all" was asked instead of assumed.

## Superseded

PRs #3745 (policy arithmetic and observation) and #3746 (decide/execute split) were closed
against this replan. Their content returned in D10: the arithmetic and the observation type
survived unchanged, the aggregate boundary did not.
