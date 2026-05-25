# Plan: Augur Simulator E2E Redesign

Sim purpose/boundaries/invariants and rollout failure semantics have moved
to <../sim/README.md>. This plan tracks remaining ledger/reconciliation
work for monthly result arrays.

## Active Step 7: Arrays Reconcile To Ledger

Goal: monthly columns remain charts, not truth. Keep shrinking bespoke
explanatory array math without changing monthly-column semantics.

Next slices:

1. Keep true state snapshots, such as cash, public asset value,
   private-equity mark value, tender-eligible private-equity value,
   property value, mortgage balance, home-equity claims, ownership
   percentage, and net-worth metrics, sourced from state snapshots rather
   than transaction ledger rows.
2. Derive remaining transaction-flow arrays from ledger rows where
   practical. The likely next targets are purchase-closing costs, property
   depreciation, and tax payment timing once the tax ledger/liability shape
   exists.
3. Move remaining explanatory arrays toward typed accounting detail once
   their semantics are explicit enough. These arrays explain calculations;
   they should not pretend to be cash movement unless there is a
   corresponding ledger row.
4. Generalize the ledger-derived matrix helper only when the next family
   needs multiple categories, actor filters, property filters, or balance
   snapshots.
5. Keep existing monthly columns stable and keep reconciliation tests as
   guardrails while the implementation source changes.
6. Add any missing causes/IDs needed by derivation. Do not add ad hoc
   string parsing to recover meaning from categories.

## Open Design Follow-Ups

Current open follow-ups live in `augur/TODO.md` and `augur/sim/TODO.md`.
Keep this plan focused on the Step 7 array-source inventory and the e2e
verification loop.
