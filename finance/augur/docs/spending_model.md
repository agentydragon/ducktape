# Spending Model

How augur represents what an agent spends, what it must pay, and what it means for a plan to
fail. The engine does not yet carry the spending policy described here: discretionary spend is
authored as an indexed obligation (`_monthly_spend_amount` in `product/scenarios.py`) until #5482
lands, and this paragraph goes with it.

## Three inputs, one engine

Augur exists so that every question asked against it shares one correct view of accounting, tax,
securities and macro paths, instead of each study re-deriving them. That fixes the boundary:
**anything two independent authors would re-derive differently in ways that change the answer
lives in the engine.** Accounting, lots, tax, settlement and failure semantics are the obvious
cases. So are the _semantics_ of spending rules — what a ladder means, what a guardrail means, and
in which order a spending cut and an asset sale absorb a shortfall — because those are exactly
what two authors wire differently and then read the difference as a result.

What stays outside the engine is parameters. Every run is one engine applied to three inputs:

| input         | holds                                                                                                                                              | in a study           |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| **world**     | sampled exogenous paths: macro states, security prices, price levels                                                                               | one path per rollout |
| **situation** | facts that hold whatever the agent decides: initial holdings and lots, contracts (a mortgage, a lease to its end date, insurance), the tax profile | fixed                |
| **strategy**  | the decisions under study: a spending policy and an allocation policy                                                                              | swept                |

A study sweeps strategy against a fixed world and situation. One strategy is one object, so a
sweep cell is one object, and a spending policy is bound to the strategy it belongs to rather
than to the situation: the same ladder runs against a different set of contracts unchanged.

A Trinity-style replay is not a mode. It is the degenerate strategy — one tier, no switch rule, no
flex, a constant allocation — against a situation with holdings and no contracts. The same engine
settles it and the same tax code applies, which is what makes its result comparable to a study
with a ladder.

## Obligations are contracts

An obligation is a cash demand the agent cannot decide away that month: a mortgage payment, rent
under a lease, insurance, property tax, a tax bill. It belongs to the situation. It is hard while
it exists — unpayable is failure — and what varies between one obligation and another is only
whether some action ends it and what that costs, never whether it must be paid.

Discretionary spending is never an obligation. Authoring it as one is wrong in both directions at
once: the plan "fails" for not affording a spend it could have cut, and no rollout can spend
differently from another, because an obligation's amount is a function of configuration and the
world path alone (`amount_value` has no branch that reads portfolio state).

## The spending policy

Spending is a policy the engine consults once per month, after the month's obligations are known
and before any cash is raised to pay them.

It **reads** the month's contractual demand, the agent's cash, the marked value of what the
allocation policy could liquidate that month, and the index series its tiers name. It **holds**
the live tier index. It **emits** one consumption demand for the month (from the agent's cash
account to the rest-of-world account, as a purchase does), a one-off cost in a month it switches
tier, and the live tier index on the per-month row — which is what time-in-tier and "held the top
tier" are computed from.

Its configuration:

- **A ladder** of tiers, most expensive first. A tier is a whole standard of living at a cost: a
  name, a fixed core, a discretionary part, and the index series that scales both. With one
  `InflationKey` every tier follows one price level; a lower tier in a different economy names a
  different one (#5489). The ladder's invariants — unique names, strictly descending cost, a
  positive floor — are checked at configuration on base amounts: a per-path cost is not known
  until the run, and with more than one price level the order on a path is a fact to report, not
  an invariant to enforce.
- **One-way traversal.** The live tier index never decreases. Climbing back is a favourable
  assumption, and the model does not make it silently; a study that wants it changes the type.
- **A switch rule**, from a typed union — funded years at the next tier down, a withdrawal-rate
  guardrail, a funded ratio against the floor's present value. Every member reads only state as
  of the current month, returns an index no lower than the current one, and exposes its
  parameters as strategy parameters, so a new rule shape never touches the engine.
- **A flex rule**, optional. It scales the discretionary part by a factor in `[0, 1]` and is
  reversible; guardrails with an upper and a lower threshold carry hysteresis by construction.
  The fixed core is what the flex rule cannot cut. Cutting below it is a tier change.

**The backstop.** Whatever the switch rule says, the policy never emits a demand it can see is
unpayable: if cash plus what can be liquidated this month does not cover contracts plus the
current tier, it steps to the highest tier they do cover. Tax on what it sold arrives later as an
obligation like any other. With no switch rule configured the backstop is the whole policy, and
that is a legitimate strategy to measure: rigid until forced.

**Failure, precisely.** A rollout fails when a month's demand cannot be settled. Under this model
the policy has already stepped to the floor before that can happen, so failure means the
situation's contracts plus the cheapest funded life cannot be met. That is a statement about the
plan, not about how much discretionary spend was configured, and it is what lets a ruin
probability sit next to time-in-tier as a comparable number.

**Order within a month.** A spending cut and an asset sale are substitutes: in a drawdown a deeper
cut means selling less and a larger sale means cutting less, so whichever is decided first absorbs
the shortfall, and a study credits the benefit to it. The order is therefore fixed and pinned by a
test:

1. The month's obligations are listed.
2. The spending policy commits a tier, sized on marked state.
3. The allocation policy raises cash for obligations plus consumption, across its sleeves.
4. Settlement, all-or-nothing.
5. Purchases and harvesting.

Flexibility is credited to the spending policy and ballast to the allocation policy, never to
call sequence.

The one-tier policy with no switch rule and no flex emits, month for month, the demand a
CPI-indexed obligation emits today. Moving the Trinity replay from the one to the other must
reproduce its numbers exactly, and that equality is the regression test for the move.

## What it does not express

- **A change of tax residence.** A tier is a spend level within one tax profile. Where a real
  lower tier would move jurisdictions, its result is optimistic in a known direction, and
  anything displaying it says so.
- **Retiring a contract.** A tier switch cannot end a situation contract; a mortgage is paid in
  every tier. A lower tier that requires selling a property models the sale as a scheduled
  `PropertySaleEvent`, not as a policy decision.
- **Climbing back**, by construction of the ladder.

## Rejected shapes

Three shapes were costed and lost. Each is recorded as the constraint that killed it.

**Spending as an obligation** — the shape in the tree before this design. Killed twice over: an
obligation's amount depends on configuration and the world path only, so no rollout can spend
differently from another; and an unpayable discretionary spend fails the rollout, which makes
failure mean "could not afford something it could have cut" and empties the ruin probability of
content.

**The policy owns all expense accounting**, contracts included. Killed because it dissolves the
distinction between what must be paid and what was chosen, so failure has no definition left and
has to be rebuilt inside the policy under another name; and because a policy that re-decides rent
every month is a costlessly relocating agent, favourable in exactly the direction the one-way
ladder exists to forbid.

**Tiers as engine-side bundles** — each tier a set of obligations plus a price level, declared in
the scenario and activated by the policy through policy-driven obligation windows. Killed because
the policy needs a bundle's cost to decide anyway, so scenario-level bundles bind the policy to
the scenario without buying anything; because a compiled obligation menu cannot carry a
state-dependent amount, so the engine would grow a second activation mechanism where computing a
tier's cost at runtime from base and series needs none; and because the composition argument
that motivated it — a lower tier is a different set of costs, not the same set made smaller — is
met by the fixed core plus contracts staying in the situation, for every composition that does
not retire a contract. Retiring one is the limitation stated above, and a bundle of obligations
could not carry a property sale either. The one thing composition genuinely needed, a price
level per tier, is a series reference on the tier.
