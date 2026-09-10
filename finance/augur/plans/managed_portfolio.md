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
  state is isolated per account and rollout. An empty account can receive a first
  contribution without exposing a fictitious initial lot.
- **Approximation:** reuse the formula in `sim/tlh_harvest.py` as the initial
  Python batch calculation. Its input is current market information plus the
  account facts actually needed by the model. It proposes realized ST/LT losses
  and corresponding basis/deferral consequences, not arbitrary ledger postings or
  an independently evolving second book. Keep calibration parameters out of
  household actions. Do not add another copy of the formula.
- **Canonical product execution:** owns balances, position/cohort state,
  loss/basis linkage, realized gains, settlement and validation. Choose one
  authoritative representation of adjusted basis/remaining deferral; do not keep
  independently mutable original basis, adjusted basis and cumulative loss totals.
  Preserve the current model's cohort and give-back conventions explicitly rather
  than claiming statutory constituent-level fidelity.
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

## GH: bounded decisions before MA1

Ownership is settled: modeled managed-account service, not a household request to
manufacture losses. Resolve these remaining choices with small numerical timelines
and a concrete signature sketch before implementation:

1. **Monthly phase.** Proposed initial convention: update existing account
   holdings/market cashflows and modeled harvesting before the household observes;
   execute its ordered contributions/withdrawals/payments afterward; close taxes
   through the existing financial steps. New contributions first participate in
   the next period's harvesting. Specify month zero, liquidation and year-end
   treatment. This differs from the current harvest-after-successful-payments
   phase: show and label that difference, including a path that later stops.
2. **Model-to-execution seam.** Reuse the Python-controlled session to invoke one
   pure batch approximation at the product's fixed phase, then validate/apply its
   typed consequences canonically before producing policy observations. Sketch
   the minimum Rust step/input-output additions for that operation; no callback
   into household policy and no public arbitrary-tax-loss action. The model's
   inputs/effects are distinct from investor observations/actions. One caller
   advance still advances one month; no second driver or user-managed tax phases.
   If invoking Python model code requires a Python wrapper around native steps,
   replace the existing public session surface and its callers atomically; do not
   leave a second supported raw-native session API for other experiments.
3. **Initial financial scope.** Pin contribution basis/cohort creation,
   withdrawal liquidation/give-back selection and ST/LT conventions, full-exit
   rounding and the one authoritative deferral representation. Start with an
   explicitly declared immediate, gross-cash withdrawal approximation, not a
   claim about a provider's real settlement delays or after-tax spendable money.
   Existing imported cohorts need supported opening basis/deferral; reject a
   missing required opening fact rather than silently initializing it to zero.

Model effects must be tied to the current account/rollout/phase, bounded by
available positions and applicable basis, and applied once. Validation failure
must not partially mutate that account. Invalid household actions retain the
existing stopped-rollout/successful-prefix contract. Do not request a second
household decision after effects or an unsuccessful withdrawal.

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
