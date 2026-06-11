# Augur Cleanup Audit

## Executive Summary

Augur is moving in the right direction: distribution-first simulator, sectioned
scenario state, ordered actor policy programs, private-equity opportunities
rather than liquidity, and ledger/accounting-backed reporting. The ordered
actor policy runtime has landed, but the implementation still carries several
old or parallel trace/detail paths that make that direction harder to enforce.

The highest-value remaining work is no longer cleanup — the trace-surface
collapse (#1580), the Plan C unified obligation pipeline (#1586/#1592/#1593/
#1600/#1601 + foundation), and the `Action → Effect` rename (#1591) have all
landed. What remains is **stochastic modeling** of variance sources the
simulator still treats as flat: PE valuation, tender timing, crypto price, and
mortgage rate. See `plans/roadmap.md` Priority 3.

Root `STYLE.md` and `augur/AGENTS.md` make several of these findings stronger:
Augur is pre-production, so compatibility shims need explicit justification;
API responses should not return trivially derived fields next to their source
collections; dynamic attribute probing should be rare; and config/source-data
errors should generally propagate to a real error boundary instead of being
hidden behind fallbacks.

## Suspicious/Needs-Design Review

(Item 1 resolved — `SimulationAction` was narrowed to user-visible sale
commands via #1580, then renamed to `Effect` via #1591. A reconciliation
guard test in `test_e2e.py` proves every monthly flow metric reduces to
ledger/snapshot/accounting-detail rows.)

## Recent Work Risk Review

1. Private-equity opportunity tracing is directionally correct but too chatty
   for the current consumers.

   Files/functions: `PrivateEquitySaleOpportunityBatch` in
   `augur/core/policy_runtime.py:77-83`. The old core-engine decision emitters
   have since been removed; carry this review forward only if equivalent
   sim-native decision rows become noisy.

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

   The deleted legacy core shape used `augur/core/annual_tax_parameters.yaml`
   as the only parameter file, with `BUILD.bazel` data pointing at that file
   and the Python loader letting YAML/Pydantic exceptions propagate directly.
   If the tax layer is revived in sim, keep the same "one reference-data file
   plus typed validation" property rather than a JSON/YAML transition seam.

   Risk: extracting tax tables out of Python is aligned with the TODO to move
   pure data into parsed configuration. Keep this as one parameter file with no
   JSON/YAML probing or catch-and-reraise wrapper around the loader. Letting
   YAML/Pydantic errors propagate is better than wrapping them in generic "could
   not read config" exceptions that restate the traceback.

   Keep if: there is exactly one sim-owned reference-data file, Bazel points at
   that file, validation rejects missing statuses/brackets, and tax behavior
   tests still prove representative calculations.

   Collapse if: both JSON and YAML remain, or runtime code has to probe
   multiple filenames. Pick one format and delete the other before merging.

3. Avoid literal "checked-in data equals itself" tests for extracted tax tables.

   The deleted legacy test mutated parsed tax parameters and verified that
   missing filing statuses were rejected. That was a useful schema guard. Do
   not add the lower-value variant: a test that loads a checked-in tax data
   file and asserts every bracket/deduction equals the same literal values
   copied into the test.

   Risk: such tests become change detectors for a data file, not semantic tax
   tests. They add maintenance friction while proving only that two checked-in
   literals were changed together.

   Keep if: tests validate invariants, boundary calculations, and tax behavior
   for representative incomes/statuses.

   Collapse if: tests merely mirror the parameter table. Use source citations in
   the data file comments or docs, and behavior tests around the calculator.

## Ongoing Guardrails

1. Keep policy execution on the ordered dispatcher. Do not add new per-class
   monthly loops, and extend policy trace rows as trajectory inspection needs
   no-op/rejected/applied decision detail.

2. Keep `cash_negative` as a warning, not a failure trigger; failure flows
   through obligation settlement results. (Unified obligation pipeline
   complete; the design question of whether negative cash is allowed at
   all — vs forcing explicit borrowing — is still open.)

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
