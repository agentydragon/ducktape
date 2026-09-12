# Python financial execution

Augur has one canonical Python financial world, implemented in
<../world.py>. The common action session and the configured application runner
share that world; they retain their explicit policy and timing differences.
There is no native extension, backend selector, or alternate financial evaluator.

## Responsibilities

- <../money.py> implements checked integer money, quantity arithmetic, and exact
  half-away-from-zero rounding. <../ledger.py> owns balanced postings.
- <../accounting.py>, <../holdings.py>, <../payments.py>, and <../claims.py> own
  income recording, lot basis, trade/payment settlement, and due claims.
- <../tax.py> and <../tax_year.py> assess the supplied rules, record liabilities,
  and carry/reset annual state. Existing supported tax scope is unchanged.
- <../held_bonds.py>, <../property.py>, and <../private_equity.py> preserve the
  existing contract cashflows and lifecycle mechanics.
- <../tlh.py> owns private TLH cohorts and basis; <../mortgage.py> owns servicing
  state. Outstanding mortgage principal has one authority: the ledger.
- <../capture.py>, <../books.py>, <../results.py>, and <../events.py> project
  recorded financial facts, not a replayed or independently computed book.

## Preserved invariants

Every posted journal entry balances. Validation precedes compound mutation;
rejected investor requests do not partly change lots, cash, tax facts, or
component state. Earlier successful actions survive a later rejected action.
Lot disposals conserve units and consume exact residual basis at final liquidation.

Actor observations are account-scoped and use only the current mark. Independent
rollouts do not share mutable financial state. Stopped paths retain their actual
last observed books without reading future prices or receiving later actions.
Capture cannot execute an additional financial step.

The configured runner retains grouped funding and existing property/PE behavior.
The common action session accepts caller-ordered economic requests and stops a
path on rejection or unpaid due claims. Moving execution into Python does not
silently give that interface new housing/PE or multi-taxpayer capabilities.

## Evidence and entrypoints

The native behavior inventory and its independently checked Python counterparts
are in <../../plans/native_test_mapping.md>. Retained financial acceptance suites
live in <../testing/>. Product and experiment callers use the same Python world.

Use <../session.py> for ordinary batch actions, <../configured.py> for remaining
configured consumers, and <../artifacts.py> for prepared-file persistence.
See <execution_boundary.md>, <money_representation.md>, and <product_metrics.md>.
