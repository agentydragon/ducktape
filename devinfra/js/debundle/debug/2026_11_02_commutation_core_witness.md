# Commutation proposal reports omitted residual component identity

When `spec propose-commutations` reported `pairs_cannot_break`, JSON and text
showed the aggregate residual module count and blocker categories but not which
cyclic components remained. The reports now list the canonical member paths
for each remaining component in deterministic order. `residual_modules` stays
the sum of those component sizes; `pairs_break` has no residual-components
field. This changes report visibility only. The synthetic proposer CLI suite
checks both output formats; no canonical graph was rerun.

## Hosted budget failure

PR #13936 at `68aca1b07e` passed the focused proposer CLI suite, but its
ordinary hosted run failed only `propose_commutations_budget_test` in
BuildBuddy invocation `561c12a2-a922-5690-afc6-6041877bfec0`. The timed command
took 32.767157338 seconds against the unchanged 30-second budget; the whole
test took 112.36 seconds. This fixture has no residual cyclic components
after inerting its proposals, so the new component-member serialization is
not exercised. This does not establish the cause of the timing failure.

The target-history query returned 370 passing rows, many sharing cached
timings, and omitted this known failing invocation. Those rows do not prove
independent passing executions or classify the failure as a flake. See
`2026_11_01_commutation_budget_ci.md` for the earlier failures and parser
improvement. Preserve the budget; a subsequent ordinary CI execution remains
separate evidence, not a claimed performance repair.
