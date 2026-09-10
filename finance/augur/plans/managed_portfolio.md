# Managed portfolio composition

Planning only: proposed names below are not current APIs. The
[roadmap](roadmap.md) owns dependencies and dispatch. This plan supplies GH and
the MA1–MA3 acceptance cases; remove completed work as it lands.

## Destination

A managed portfolio is an account owned by the household, with a declared
investment service. It is not necessarily another deciding agent, an ordinary ETF
with unrelated tax losses, or a monthly household `Harvest` action. The household
policy contributes, withdraws and observes account statements. The service model
approximates internal investment activity; canonical execution applies its
position, basis, cash and tax consequences.

The first service uses the existing reduced-form index/TLH approximation. Its
estimated realized losses are model assumptions, not verified constituent sales
or estimated tax savings. Household tax accounting determines their consequences.
Calibration, constituent-level simulation, fees not currently modeled and richer
settlement products are separate changes, not migration prerequisites.

## Python composition sketch

```python
# Illustrative additions to Scenario and the common session, not another runner.
managed = ManagedPortfolio(
    account_id="direct-indexing",
    owner_id="household",
    opening_positions=opening_positions,  # basis and holding-period cohorts
    strategy=ReducedFormDirectIndexing(
        market=index_market,
        harvesting=harvest_model,
        withdrawals=immediate_gross_cash_terms,
    ),
)
scenario = Scenario(..., managed_portfolios=[managed])
paths = market_model.sample(...)
session = prepare_session(scenario, paths)

def policy(batch):
    responses = []
    for decision in batch:
        obs = decision.observation
        account = obs.managed_portfolios["direct-indexing"]
        actions = household_decisions(obs, account)
        # May include Contribute(source, account, amount) or
        # Withdraw(account, destination, gross_amount), then PayClaim(...).
        responses.append(decision.respond(actions=actions))
    return responses

batch = session.start()
while not isinstance(batch, Finished):
    batch = session.advance(policy(batch))
```

`household_decisions` is experiment code using the same batch interface as other
policies. Existing callable/session names should be reused where they fit; this
sketch does not justify renaming them. The market binding supplies only the
service's supported prices/payouts; the household never sees future paths.

## Responsibilities and representation

- **Scenario declarations:** account ownership, opening positions, selected
  service/model and concrete funding/settlement terms. Reuse account/asset IDs,
  money precision and opening lot types. A definition is shared configuration;
  state is isolated per account and rollout. MA1 consumes BASIS's shared exact
  total-opening-basis contract; no managed-only divide-and-round workaround.
  An empty account can receive a first
  contribution without exposing a fictitious initial lot.
- **Approximation:** reuse the formula in `sim/tlh_harvest.py` as the initial
  Python batch calculation. Its input is current market information plus the
  account facts actually needed by the model. It proposes realized ST/LT losses
  and corresponding basis/deferral consequences, not arbitrary ledger postings or
  an independently evolving second book. Keep calibration parameters out of
  household actions. Do not add another copy of the formula.
- **Canonical product execution:** owns balances, position/cohort state,
  loss/basis linkage, realized gains, settlement and validation. Use each cohort's
  adjusted remaining tax basis as authority; harvest history is reporting, not a
  second mutable basis accumulator. Preserve explicit cohort character assumptions
  without claiming statutory constituent-level fidelity. Count underlying managed
  positions once in wealth; the account summary is not an additional owned asset.
- **Observations and results:** expose current account value, available cash,
  relevant basis/cohorts and settlement terms to its owner. Report actual realized
  losses/gains, basis consequences and receipts from canonical state/events, with
  compact population capture and selected trace. Internal calibration state is
  not automatically an investor-observable fact.

Put declaration/state bindings with `sim` and execution, approximation with
model/calibration code, and investor allocation helpers in `policy`. Extend or
move existing modules only as needed by the first consumer, with brief responsibility
docstrings. No entity registry, general plugin protocol, nested simulator,
arbitrary posting action or parallel policy callable is required.

## GH: concrete proposal for review before MA1

Ownership is settled; these phase and accounting conventions are proposed model
choices, not implemented behavior or provider/statutory claims. Review them before
MA1. The synthetic amounts below are accounting controls, not yield estimates.

### Monthly phase

Prepare existing market cashflows and due claims → apply modeled harvesting →
observe once → execute household actions in order → existing month-end closing.
Current `actors::Session::prepare` already retains claims before publishing
observations; insert the managed step there, not another household decision.

Month zero is the first modeled monthly event, including one baseline harvest on
opening positions, as in the current reduced-form process. With no preceding
sampled price, its drawdown input is zero. This is an explicit coarse monthly
convention, not a claim that a month elapsed before the opening valuation. A new
contribution after observation first participates in the following month's
harvesting. Opening tax facts describe pre-simulation activity; never harvest that
history again. December's modeled losses reach that year's ordinary closing;
prior-year tax claims already due do not get retroactively rewritten.

A failed later payment cannot undo already-applied harvesting. A stopped path
retains those realized facts/basis changes but receives no subsequent monthly
steps. Preserve existing stopped-path closing rules, including no fabricated
year-end assessment when failure prevented it. Results must distinguish recorded
facts from an assessment never performed. This intentionally replaces current
harvesting's dependency on successful grouped payments.

### Single Python session, one private model handoff

Put the public `ActionSession` in a Python orchestration module, keeping its
`start/advance/close` contract and one batch policy shape. The existing native
session becomes its private financial-step implementation; migrate all Python
callers atomically, without a second supported public raw-native session.

The native preparation result gains one concrete internal alternative,
`ManagedStep`, containing current inputs keyed by rollout/account/month. Inputs
include invested market value, remaining adjusted basis, current/prior index
marks and declared approximation parameters. They contain no future path.
The pure Python model returns one nonnegative ST/LT loss estimate per input.
Native application checks exact pending identity/coverage, bounds losses by basis,
updates basis and tax facts together, then exposes ordinary policy observations.
No arbitrary tax-write action or general effect language is needed.

```python
# Private implementation sketch, not another experiment-visible loop.
def estimate_harvest(inputs: list[ManagedInput]) -> list[HarvestEstimate]:
    """Estimate nonnegative ST/LT losses in money quanta; retain each input key."""
    ...

def _finish_preparation(self, pending):
    if isinstance(pending, ManagedStep):
        estimates = self._harvest_model(pending.inputs)
        return self._native.apply_managed(estimates)
    return pending  # ordinary decision batch or Finished

def start(self):
    return self._finish_preparation(self._native.start())

def advance(self, responses):
    return self._finish_preparation(self._native.advance(responses))
```

There is at most one managed handoff per month, never a retry loop. Zero managed
accounts bypass it. Invalid model output is a model/programming error that aborts
the session, not investment ruin to include in a success-rate estimate; validate
the batch before applying its effects. Investor action rejection retains the
existing per-rollout successful-prefix rule. Product models cannot mutate books.

### Basis and investor operations

The old pool-wide deferred-loss scalar moves gains between cohorts after new
contributions: start with one $100-cost unit and $20 deferred loss, add a new $100
unit, then sell only the new unit for $100. The existing per-unit give-back
recognizes $10 gain on that new unit. Full liquidation conserves the total $20,
but timing and ST/LT character can move. Do not preserve this as a compatibility
requirement (`rust/engine/tlh.rs::tlh_give_back_for_pool_sale`).

Proposed initial service rules:

- **Opening state:** import already-adjusted broker basis and explicit cohort
  ages directly. Historical cumulative harvesting is not required and must not be
  subtracted a second time. Preserve exact total basis through lowering: $1 of
  basis over three units must not require a cent-representable per-unit basis.
  Missing basis/age needs an explicit supported
  approximation or rejection, not a silent zero/default. The proxy's cohort ages
  remain declared approximations, not reconstructed constituent trade history.
- **Harvest:** estimate total loss with the existing curve, then allocate its
  basis reduction across cohorts in proportion to their remaining adjusted basis.
  Exact residual quanta follow stable cohort-ID order. Total basis reduction equals
  the booked ST+LT loss; no cohort goes negative. The declared ST/LT split remains
  the reduced-form model parameter, not a claim about actual constituent sales.
- **Contribution:** debit the specified outside cash account, acquire proxy units
  at the current mark, and create a new current-period cohort with basis equal to
  acquisition cost. Fractional-unit rounding leaves residual cash inside the
  managed account. It does not inherit an older cohort's deferred gains.
- **Withdrawal:** deliver the requested gross cash, not an after-tax budget.
  Use managed cash first; any liquidation redeems proxy units pro rata across
  cohorts. Apportion representable units deterministically in cohort-ID order;
  verify actual per-cohort rounded proceeds and retain excess as managed cash.
  An aggregate inverse price calculation is not proof of funding or insufficiency.
  Full exit is an explicit request, not inferred from a cash-first gross amount:
  it consumes all units and their remaining basis, including rounded-zero positions.
  Realized gains
  use each disposed cohort's adjusted basis and existing holding-period machinery.
  Disposition receipts and tax gains must reconcile to that same basis.
  This is an explicit approximate service term, not an investor's hidden FIFO rule
  or a reverse callback to Python during action execution.
- **Access:** underlying managed positions are not independently tradable through
  ordinary public `Sell/Buy` actions or transferable around the service rules.
  An unfundable contribution/withdrawal rejects without partial mutations; earlier
  successful actions survive. Immediate cash availability is the initial declared
  approximation, not a real-provider settlement guarantee.

| Control                                                             | Expected result under the proposed convention                                                                                                          |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Opening value/basis $100/$100, estimated ST loss $10                | Value remains $100; adjusted basis $90; realized ST loss $10; no cash minted.                                                                          |
| After that harvest, contribute $100 at an unchanged price           | Old cohort basis $90, new cohort basis $100; neither additional same-month harvest nor redistribution of old deferral.                                 |
| Withdraw $100 from those equal-sized cohorts, unchanged price       | Half of each cohort redeemed: $95 basis disposed, $5 gain, $95 remaining basis; withdrawal is $100 cash, not $100 net of taxes.                        |
| Liquidate the remaining positions at the same price                 | Additional $5 gain; no positions or stranded basis/deferral; total realized gains offset the earlier $10 modeled loss before character/timing effects. |
| Opening $100 position, loss $10, then an unfundable $150 withdrawal | Withdrawal changes nothing; retain value $100, basis $90 and the already-recorded $10 loss; stop that trajectory.                                      |
| Same loss during December; path completes the month                 | Include the loss once in that year's canonical assessment. A failed December path instead preserves facts without inventing a completed assessment.    |

MA1 pins the passive cases; MA2 adds funding, cohort and rounding cases. Add
zero-loss, mixed-age, zero-basis and nonrepresentable-quantity controls alongside
these exact arithmetic anchors. The gate stays open until these proposed service
conventions are accepted; no managed-account implementation is dispatched here.

One rounding control: three cohorts each hold 0.5 units at a price of one money
quantum/unit. With per-cohort half-up proceeds they each liquidate for one quantum;
selling all can fund a two-quantum withdrawal and retain one quantum cash. An
aggregate calculation demanding two whole units would incorrectly reject it.
This is a fixed-point acceptance case, not a change to real product valuation.

The old Rust harvest curve and give-back readers remain only for configured
consumers until their migration. MA1 reuses the existing Python curve, not a new
twin; APP/P12 removes the old native formula, `HarvestPolicy` declaration and
policy-indexed accumulator with their last consumers. No new consumer may use the
legacy harvesting interface. If that deletion can land earlier, delete it then.

## Independently reviewable PRs

| Unit                                       | Implementation and required evidence                                                                                                                                                                                                                                                                                                                                                                | Needs |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| MA1 — passive managed account              | Add the concrete declaration, Python approximation step, canonical validated consequences and scoped observations/capture. An opening account can be held without a monthly harvest action. Real-session tests cover zero-loss control, nonzero loss/basis linkage, bounded basis, ST/LT character, year crossing, account/path isolation and stopping after product processing.                    | GH    |
| MA2 — investor contribution and withdrawal | Add explicit economic requests using the same action batch. Canonical execution funds contributions and liquidates withdrawals under declared terms. Test empty-account entry, original/new cohorts, partial and full withdrawal, proportional deferral release, exact rounding, rejected-action non-mutation and withdraw → pay ordering; prior successful actions survive a later failure.        | MA1   |
| MA3 — runnable managed-account comparison  | Add a small example comparing the declared managed approximation with its no-harvest control on identical supplied paths and external investor flows. Exercise contribution/withdrawal policies, compact outcome reductions and selected replay through the documented CLI in Bazel CI. Include loss, basis, sale gain, tax and cash reconciliation; synthetic results do not validate calibration. | MA2   |

MA3 provides the managed-account consumer needed for subsequent household-study
integration; it need not wait for full APP migration or REPORT's app-specific
projection. Prefer extending a relevant existing example to inventing a study
framework. Keep initial comparisons on supported price/payout and tax assumptions;
BIND/TAX gate expanded distribution/statutory claims, not the synthetic controls.

For APP, migrate portfolio-source configuration and all callers atomically to the
managed declaration and common session, preserving supported imported cohorts and
explicit assumptions. Do not map it back to the old harvest-policy runner as a
permanent adapter. Housing/PE remain deferred; complete legacy-runner deletion
remains incomplete until those existing consumers migrate.

## Checks against misleading success

- Paired harvest/no-harvest cases use identical market paths, external flows,
  valuation and applicable fees. A nonzero harvest cannot itself mint cash or
  increase portfolio value. Later realized gains reconcile with the retained
  basis/deferral state; no free loss with untouched basis.
- Partial withdrawals and subsequent contributions cannot share or consume another
  account's deferral. Full liquidation releases the appropriate residual exactly;
  reopening an emptied account cannot inherit stale deferral.
- Batch reordering/chunking and selected replay preserve each path's decisions,
  state and effects. Stopped paths have no later model or policy steps; an observed
  zero and an unobserved post-stop month remain distinct.
- A first-year calibration anchor is not validation of drawdown behavior, mature
  accounts, wash-sale interactions or a different funding history. Compare model
  assumptions separately before using the approximation for a consequential
  allocation recommendation; neither source-formula parity nor tax reconciliation
  closes that research question.
