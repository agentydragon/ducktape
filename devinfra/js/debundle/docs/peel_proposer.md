# Peel Proposer

Current-state notes for `debundle modules propose`. This document keeps
the stable implementation shape; completed rollout history belongs in
git, not in long-lived plans.

## Pipeline

The proposer is a renderer over `peel::quotient::QuotientGraph`:

1. `build_seed_quotient` constructs the seed quotient from atomic
   units, active spec modules, and atomic-DAG reachability closures.
   Each forced contraction is gated by `merge_preserves_invariants`.
2. `greedy_merge_to_convergence` extends the quotient by merging
   classes that the gate still accepts. It can extend an existing
   module with residual owners, merge existing modules, or do both in
   one proposal when the output class contains absorbed residual
   owners.
3. `emit_proposals` walks the surviving quotient classes and renders
   `FactorizeProposal` rows plus diagnostics.

The old parallel cell IR is gone. `QuotientGraph` is the single source
of truth for which owners are in each proposed class.

## Seed Rejections

The seed phase is conservative. If a forced contraction would make the
quotient unrealizable, the proposer skips that contraction and records a
`SeedContractionRejected` diagnostic instead of emitting a cyclic
proposal.

Current rejection kinds:

- `atomic_unit`: an atomic unit could not be represented as one class.
- `spec_module`: an active spec module's declared owners could not be
  represented as one class.
- `atomic_reachability`: an atomic-DAG reachability closure edge would
  have created an unrealizable quotient.
- `post_seed_unrealizable_scc`: the final seed partition is rejected by
  the shared realizability gate.

Well-formed inputs normally have no seed rejections. When they appear,
they are spec-authoring diagnostics, not assignment rows that can be
landed directly.

## Greedy Driver

Production greedy uses a lazy priority queue. The queue is initialized
once with cross-class candidate pairs, ordered by the same deterministic
sort key as the former full-scan driver. Pop-time checks discard stale
classes, re-evaluate mergeability, and re-rank candidates whose coupling
score has drifted. After a successful contraction, the winner's current
neighborhood is pushed back into the queue.

The hidden `greedy_merge_to_convergence_full_scan` driver remains as the
test reference for
`lazy_pq_greedy_matches_full_scan_greedy_on_corpus`. That test asserts
the lazy-PQ contraction sequence is byte-identical to the full-scan
sequence across representative fixtures.

## Output Shapes

`FactorizeProposal` rows distinguish:

- direct member moves, usable by `bindings assign --batch` when the row
  is landable and contains only addressable bindings;
- extension rows, where residual owners can be moved into an existing
  module;
- `merge_into` rows, where two or more active modules should be merged
  or manually co-located before assignment.

Anonymous-statement addressability and residual dependency status still
control `landable_today`; `bindings assign --batch` refuses rows that
require merge/manual work.

## Where To Look

- `peel/factorize.rs`: report assembly and proposal rendering.
- `peel/quotient.rs`: quotient kernel, seed contraction, greedy driver.
- `peel/quotient_integration_test.rs`: seed, greedy, merge-output, and
  lazy-PQ equivalence coverage.
- `docs/cli.md` and `docs/spec_editing.md`: user-facing `modules propose`
  and `bindings assign --batch` workflow.
- `perf/proposer.md`: live proposer performance roadmap and profiling
  policy.
