# Debundle source-tree structure audit

**Snapshot:** `devel` at `31620bedd1` (2026-10-01). This audits the debundler
implementation under `devinfra/js/debundle/`, not a downstream emitted
application tree. It refreshes the 2026-09-30 working-tree audit after the
structure cleanups merged.

## What changed since the first audit

The main directory-sprawl findings were already addressed in separate PRs:

- #8570 retired the historical `stage_one` layer name in favor of
  `chunk_analysis/`.
- #8574 grouped selector code under `selectors/` by matching, resolution,
  authoring, and diagnostics.
- #8578 grouped lowering import helpers under `lowering/imports/`.
- #8593 moved spec implementation files under `spec/`.
- #8594 grouped artifact layout and writers under `artifacts/`.

The Rust source files directly in the crate root fell from 60 to 26. The
README now describes the selector and import groupings. The initial suggestions
to make those moves should be considered complete, not future work.

## Current findings

### The remaining crate-root files are cross-cutting entry points, not a move list

The 26 root-level Rust files now cover crate entry points, orchestration,
preparation, gating, shared graph/partition types, and emitted-output
validation. That is still a sizeable root, but the inventory alone does not
show that those files share one stronger domain boundary. Moving them into
directories without a call-path or ownership reason would risk replacing one
flat list with a taxonomy that callers have to learn.

### Similar analysis names describe different data products

The broad term `ChunkAnalysis` remains on two different types:

- `chunk_analysis::ChunkAnalysisOutput` composes spec-independent statement
  facts with the owner graph and structural atomic units.
- `gate_chunk_analysis::ChunkAnalysis` holds owner-graph and logical-module
  lookup data used by gate operations.

`facts::ChunkFactAnalysis` and `selectors::matching::ChunkFacts` are also
distinct: the former carries semantic per-statement facts and purity results;
the latter is a relational projection of AST syntax for selector matching.
`ProgramAnalysis` is a shallow scan of module-level import/export/owner and
effect metadata. These names can make searches noisy, but the current evidence
does not establish duplicated models. If these interfaces need further work,
prefer names that communicate their consumer and contract, with references
updated atomically.

### Repeated AST work needs profile evidence before consolidation

The current preparation path parses source once and retains syntax-derived
metadata in the manifest/artifact indexes. Later passes still traverse the AST
for distinct questions such as selector relations, statement/effect facts,
admission checks, and rewrites. Some visits may overlap, but overlap in the
input tree is not by itself duplicated work: the results feed different
contracts, and mutation passes need their own visitor.

I attempted the repository's `perf_wrapper.sh` profile against the staged
Claude Code Web capture, but the host rejected `cycles:u` collection with
`Permission denied` (`perf_event_paranoid=2`). A local Callgrind profile was
used as a user-space fallback. The corpus stayed local and was not uploaded to
BuildBuddy.

#### Callgrind result

The optimized debundler completed one `run` over the Claude Code Web
`2026-09-29-c20643cea8` code-API input list (90 source files); it emitted 275
JavaScript files. Callgrind recorded 40.43 billion instruction references
(`Ir`). The run exited successfully. Valgrind reported a `brk segment overflow`
warning during allocation, so these instruction counts are comparative profile
evidence, not wall-clock measurements or hardware-cycle data.

One concrete hot path is in `purity/plain_data.rs`:
`PlainDataWriteScanner::visit_call_expr` loops over candidate binding names and
calls `is_ts_enum_iife_call_for_binding` for each unshadowed candidate. Across
the two emitted scanner call paths, Callgrind recorded about 59.5 million calls to that
predicate, which accounted for 5.33% of total instruction references (2.15
billion). This is repeated per-candidate recognition work at each call
expression, not evidence that the whole AST is needlessly walked by duplicate
analysis passes. A focused optimization could first derive the candidate name
from the argument shape, then run the full IIFE recognizer only for that name;
it would need purity-soundness tests before landing.

The profile therefore found one measurable candidate-check hotspot, but does
not support merging selector facts, purity facts, chunk analysis, or their
visitors into one shared AST pass. Keep that optimization separate from further
directory or naming changes.

## Conclusion

The suspected broad directory sprawl has been substantially reduced by the
merged grouping PRs. The measured issue is a purity-scanner predicate repeated
per candidate, not broad duplication between analysis subsystems. This audit
does not recommend another directory shuffle or a consolidation based only on
similar names.

`analysis_tests/` is test-only (`gate.rs` includes it under `#[cfg(test)]`),
so it is not counted as a production subsystem finding.
