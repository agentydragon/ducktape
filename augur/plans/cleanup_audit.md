# Augur Cleanup Audit

## Executive Summary

Augur is moving in the right direction: distribution-first simulator, sectioned
scenario state, ordered actor policy programs, private-equity opportunities
rather than liquidity, and ledger/accounting-backed reporting. The ordered
actor policy runtime has landed, but the implementation still carries several
old or parallel trace/detail paths that make that direction harder to enforce.

The highest-value remaining cleanup is not broad refactoring. Force the engine
to choose one source of truth for result detail: ordered policy programs plus
state/ledger/accounting rows, with monthly arrays as derived chart output.

Root `STYLE.md` and `augur/AGENTS.md` make several of these findings stronger:
Augur is pre-production, so compatibility shims need explicit justification;
API responses should not return trivially derived fields next to their source
collections; dynamic attribute probing should be rare; and config/source-data
errors should generally propagate to a real error boundary instead of being
hidden behind fallbacks.

## Suspicious/Needs-Design Review

1. Actions, decisions, ledger entries, balance snapshots, accounting details,
   and monthly arrays overlap heavily.

   Files/classes: `SimulationAction` rows at
   `augur/core/scenario_set.py:355-452`, policy decisions at lines 455-499,
   market observations at lines 502-530, ledger/snapshot/detail rows at lines
   534-599, and response exposure at lines 886-901. `run_scenario_vectorized()`
   builds all of them plus wide arrays in `ScenarioRunArrays` at
   `augur/core/scenario_engine.py:92-168` and returns them all at lines
   1100-1175.

   Why suspicious: these rows are valuable as a future trace model, but today
   the app primarily consumes fan/terminal/monthly columns. The row surfaces
   are mostly exercised by tests and emitted wholesale in every response. That
   creates a parallel public API before the source-of-truth boundary is clear.

   Replace with: make ledger/accounting/snapshot rows the canonical detail
   surface, then either delete `SimulationAction` rows or narrow them to
   user-visible commands only. Gate heavyweight detail rows behind an explicit
   report/detail option once the UI has a real consumer.

   Prove safe by: add reconciliation tests that every public monthly flow is
   derived from ledger/accounting rows. Then remove one action family, such as
   `PayMortgageAction`, and confirm no app or API behavior depends on it.

## Recent Work Risk Review

1. Private-equity opportunity tracing is directionally correct but too chatty
   for the current consumers.

   Files/functions: `PrivateEquitySaleOpportunityBatch` in
   `augur/core/policy_runtime.py:77-83`, opportunity ID construction at lines
   584-642, policy decisions in `augur/core/scenario_engine.py:622-629` and
   `_record_private_equity_sale_decisions()` at lines 1269-1321.

   Risk: opportunities are now exogenous observations, which matches the spec.
   But `PrivateEquitySaleDecision` rows are emitted for every rollout/month
   when a PE sale policy exists, including no-op/no-opportunity rows, and the
   app does not consume them. This can become provenance noise unless there is
   a concrete diagnostic view or invariant attached to each row.

   Keep if: a trajectory details view uses the rows to explain sale/no-sale
   reasons, or tests assert that each sale action joins to one opportunity and
   one decision by ID.

   Collapse if: no consumer appears soon. Keep the opportunity array and sale
   action cause IDs, but stop serializing full no-op decision rows by default.

2. Annual-tax parameter extraction should remain one source of truth, not become
   a JSON/YAML transition seam.

   The landed shape uses `augur/core/annual_tax_parameters.yaml` as the only
   parameter file, with `BUILD.bazel` data pointing at that file and the Python
   loader letting YAML/Pydantic exceptions propagate directly.

   Risk: extracting tax tables out of Python is aligned with the TODO to move
   pure data into parsed configuration. Keep this as one parameter file with no
   JSON/YAML probing or catch-and-reraise wrapper around the loader. Letting
   YAML/Pydantic errors propagate is better than wrapping them in generic "could
   not read config" exceptions that restate the traceback.

   Keep if: there is exactly one parameter file, `BUILD.bazel` points at that
   file, validation rejects missing statuses/brackets, and tax behavior tests
   still prove representative calculations.

   Collapse if: both JSON and YAML remain, or runtime code has to probe
   multiple filenames. Pick one format and delete the other before merging.

3. Avoid literal "checked-in data equals itself" tests for extracted tax tables.

   The landed test mutates `_ANNUAL_TAX_PARAMETERS.model_dump(mode="json")` and
   verifies that missing filing statuses are rejected. That is a useful schema
   guard. Do not add the lower-value variant: a test that loads
   `annual_tax_parameters.yaml` and asserts every checked-in bracket/deduction
   equals the same literal values copied into the test.

   Risk: such tests become change detectors for a data file, not semantic tax
   tests. They add maintenance friction while proving only that two checked-in
   literals were changed together.

   Keep if: tests validate invariants, boundary calculations, and tax behavior
   for representative incomes/statuses.

   Collapse if: tests merely mirror the parameter table. Use source citations in
   the data file comments or docs, and behavior tests around the calculator.

## Suggested Deletion/Replacement Sequence

1. Keep policy execution on the ordered dispatcher. Do not add new per-class
   monthly loops, and extend policy trace rows as trajectory inspection needs
   no-op/rejected/applied decision detail.

2. Pick one trace/source-of-truth path. Collapse one duplicated family, such as
   mortgage payment action rows, into ledger/accounting/snapshot detail.

3. Replace temporary tax timing with obligations. Keep `cash_negative` as a
   warning, but introduce failure only through an obligation settlement result,
   not through raw cash-path inspection.

## Suggested Tests/Guards

- Policy-order guard: two policies in one actor program should produce
  different results when their order is reversed.

- Detail-row reconciliation guard: for each monthly transaction array retained
  for charts, assert it equals a ledger/accounting/snapshot-derived matrix or
  document a deliberate exception.

- PE opportunity guard: every `SellPrivateEquityAction` should join to exactly
  one opportunity observation and one sale decision by
  `opportunity_id`/`opportunity_cause_id`; no-op decision rows should be absent
  from default responses unless a detail flag requests them.

- Inert-schema guard: unsupported asset and liability variants should be
  rejected at validation, or covered by an e2e test proving they affect runtime
  state.

- STYLE guard: a repo-local audit script or focused tests should catch new
  Augur compatibility shims, redundant derived response fields, dynamic metric
  `getattr()` dispatch, and config/data loaders that swallow errors without an
  explicit degraded-mode flag.
