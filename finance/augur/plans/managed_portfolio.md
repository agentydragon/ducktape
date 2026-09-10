# Python-owned TLH portfolio migration

The representation and phase are agreed. Draft
[#6083](https://github.com/agentydragon/ducktape/pull/6083) implements the component
and driver migration; integration acceptance is still pending. The [roadmap](roadmap.md)
owns dispatch. Retain MA1/MA2 until those checks pass and the integration lands;
MA3 remains a future experiment, not a generic managed-account abstraction.

## Approved boundary

Use the concrete Python `TlhPortfolio` described in [the TLH contract](../docs/tlh.md).
Each rollout owns an isolated instance. The component owns private exposure,
adjusted basis, cohort ages and rounding cash; the investor observes value and
reported tax basis, then chooses contributions, gross withdrawals or liquidation.
No cohort table, mutable ledger or cumulative-harvest scalar crosses that public
boundary. Imported opening basis is already adjusted, exact total basis; it is
not reconstructed from historical loss estimates.

The reduced-form model estimates losses separately for each private cohort and
reduces that cohort's basis. Its declared character split is an approximation,
not constituent-level or wash-sale fidelity. Redemptions are internal FIFO, not
the superseded pro-rata service proposal. A new contribution does not inherit
old modeled losses. Full liquidation releases remaining basis; zero requests
are no-ops, including at a zero mark. The durable contract owns rounding details.

The financial engine settles returned cash and taxable realizations and can
retain immutable statements for capture. It never owns or updates a mirrored
cohort/basis book. Count the component once in household wealth; do not also
leave its exposure in ordinary public holdings. Statements describe outcomes,
not instructions to reconstruct a second portfolio. A distribution uses the
component's actual remaining exposure and declared income character.

## One monthly phase in every driver

Advance each active component exactly once before publishing investor observations
and before any contribution or redemption, including scheduled/configured TLH
sales. Scheduled sales must not bypass the component through public-lot execution.
Month zero retains the stipulated baseline harvest with zero opening drawdown;
post-observation contributions first enter the next month's harvest.

This deliberately changes the old sale-before-harvest and successful-payment-gated
timing. A later failed investor action or unpaid claim does not undo the month's
harvest. No stopped path receives later component updates. December's realized
facts enter the existing close only if that close actually runs; do not fabricate
a completed assessment for a stopped path. Snapshot valuation at a closing mark
must not advance the component or realize another month's losses.

The common public session remains Python-owned with one batch policy API. Its
private native-world calls accept the component's concrete financial effects;
there is no model callback, generic managed-step protocol or experiment-visible
handoff loop. A policy does not request monthly harvesting. Component investment
requests participate in the same caller-ordered list and successful-prefix rule
as other actions; there is no retry or special recovery channel.

Use candidate component state for settlement and adopt it only after the engine
accepts the associated effects. Preflight expected affordability failures;
component argument validation uses built-in exceptions, and unexpected model or
accounting errors are not counted as investment ruin. Cash/tax rejection must
not leave either side partly changed.

## Remaining landing slices

| Unit                                       | Implementation and completion evidence                                                                                                                                                                                                                                                                                                                                    | Needs                                                                     |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| MA1 — opaque Python component              | One component owns the curve, private state, observations and investor operations. Independent tests cover per-cohort loss/basis linkage, imported exact basis, fresh contributions, partial/full redemption, zero requests/marks, bounded rates, character and rejection atomicity. No native counterpart remains a supported alternative after MA2.                     | None; representation/phase settled                                        |
| MA2 — all drivers and canonical settlement | Compose the same component with the Python action session and every remaining configured driver, native binding and test reader. Apply one pre-investor monthly phase, settle effects atomically and capture statements. Delete native harvesting, give-back accumulators and any replaced stateless Python curve with their last readers; no TLH math waits for APP/P12. | MA1's concrete component contract                                         |
| MA3 — runnable paired comparison           | Same supplied paths and external investor flows, harvesting and no-harvest controls, contribution/withdrawal decisions, typed compact outcomes and selected replay. Run the documented CLI with generated financial inputs in Bazel CI. Financial reconciliation does not validate calibration.                                                                           | MA2's action-session integration, not every unrelated configured consumer |

MA2 includes the old native `HarvestPolicy` execution/state/readers and scheduled
TLH redemptions, not merely the new example. Convert existing declarations at one
typed composition boundary where necessary; do not preserve a native TLH runner
or parallel curve as a fallback. Remaining non-TLH configured strategy, housing
and PE can still await their named P12 migrations. Their deferral does not permit
native TLH state/formulas to remain.

The draft removes `rust/engine/tlh.rs`, native harvest/give-back state and lowering,
and `sim/tlh_harvest.py`. Scenario/compiler, portfolio-source configuration and
acceptance readers now use the component contract.
Verify actual configured sales, funding, distributions and failure paths under the
revised Python-owned session, request types and allocation proposer before closing
MA2; source deletion alone is not financial acceptance. Keep independent
cohort/basis assertions rather than merely deleting old give-back tests.

`sim/session.py` owns lifecycle and component state; `sim/configured.py` composes
the same session with Python allocation proposals and existing grouped claims.
Configured strategy-input and legacy output retirement remain P12, not missing TLH
implementation. Dense/forensic capture must retain the settled component effects
with explicit operation, signed cash, gains and basis change. Product timelines
must not lose redemptions or invent public-stock units; compact mode need not
retain that history.

MA3 and supported experiment consumers need not wait for the app's existing
housing/PE branches or performance work. BIND/TAX gate expanded product/statutory
claims, not clearly labeled synthetic controls.
Tax-aware investor rules need CAP's
[recorded-tax observation slice](policy_interfaces.md#observationspy); current
Python observations expose component value/basis but not the household's tax
facts. Fixed-flow harvesting/no-harvest comparisons need not wait for that slice.

## Decisive integration controls

- Opening value/basis 100/100; stipulated modeled loss 10 leaves value 100,
  basis 90 and loss 10, with no cash minted. Contribute another 100 unchanged:
  FIFO withdrawal of 100 consumes the old cohort's basis 90 and realizes gain
  10; the new cohort retains basis 100. Final liquidation realizes no additional
  gain. This replaces the superseded half-from-each-cohort expectation.
- Opening basis of one currency unit over three exposure units remains exact.
  Partial redemptions plus final liquidation exhaust that basis. Per-fill price
  rounding is tested separately; do not mint balancing cash to make separately
  rounded fills equal one full sale.
- Mixed-age cohorts and modeled ST/LT loss fractions reconcile harvest and sale
  character without sharing basis between cohorts or portfolios. Empty entry,
  zero-basis positions, cash overfill, zero-value liquidation and re-entry leave
  no stale deferral.
- A same-month scheduled redemption observes that month's prior harvest, just
  like an explicit investor withdrawal. A later failed payment retains both
  realized effects. An invalid investment request changes neither committed component nor
  engine state; prior successful actions survive and the remainder does not run.
- Contribution → withdrawal → payment and withdrawal → contribution preserve
  caller order and actual cash availability. Distributions use remaining exposure;
  compact/detailed statements agree and do not count component value twice.
- Same paths, selected/reordered IDs and fresh component instances give identical
  results. Stopped-month facts remain observable; future snapshots/harvests do
  not appear. Snapshot capture itself never runs the monthly model.

Keep empirical calibration, wash sales, fees and constituent modeling in
[future research](future_work.md#reduced-form-tlh-portfolios). They are not grounds
for a second financial implementation or prerequisites for this migration.
