# Remaining assignment vocabulary decisions

The advisory planner now lives in `peel/propose.rs`, with `ModuleProposal`, `ProposalOptions`, and `ProposalsReport`.
Gate inputs are `FactorizationInputs`, distinct from the semantic `ChunkAnalysisOutput`. These internal renames leave
CLI commands, serialized fields, and Bazel target names unchanged.

This document records the remaining decisions, not a second dispatch queue. Active priorities live in <../TODO.md>.

## Authoritative assignment names

`factor_assembly.rs` builds the owner-to-module `Partition`; `ChunkFactorization` combines that assignment with its
graph-derived state; `validate_factorization` returns a `FactorizationReport`.

Possible names are `assignment_assembly`, `ModuleAssignment`, and `AssignmentReport`. Do not call an unchecked
`ChunkFactorization` a `ValidatedChunk`: construction does not guarantee a passing verdict. Rename these only when the
benefit exceeds the churn across the gate and lowerer.

Keep `OwnerGraph`, `OwnerId`, `AtomicUnit`, and `ModuleQuotient`. They describe different mathematical objects. The
quotient can contain lazy-only or rejected import cycles, so calling it a DAG would be incorrect.

## Wire-facing vocabulary needs a deliberate cutover

Internal type renames do not rename serde struct fields. Remaining wire-facing names include:

- `factorize_proposals` and `factorize_diagnostics` in explain reports and their limits sections.
- `edges_to_other_residual_cells`, `other_residual_cells_referenced`, and the associated landability-note text. The
  proposer now operates on quotient classes, but clients still consume these older cell names.
- `UnassignedMode::MiniFactors`, serialized as `mini_factors`. A possible replacement is `PerAtomicUnit` /
  `per_atomic_unit`.

Any such change must update the spec/report documentation and actual downstream consumers together. It must not be an
incidental consequence of an internal rename or use compatibility shims to leave two vocabularies behind.
