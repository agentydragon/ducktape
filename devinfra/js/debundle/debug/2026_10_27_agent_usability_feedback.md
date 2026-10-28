# Debundle agent usability feedback

Updated against Ducktape [`7241917c`][reviewed-revision]. The remaining requests
come from integrator, ownership-planner, lane-worker and CLI-fix feedback.
Examples are generic; private source, identifiers and diagnostic payloads are
excluded. This round established usability gaps, not a gate correctness failure.

## A — Extend causal diagnostics beyond supported write witnesses

**Priority:** P1. **Owner:** diagnostics.

[`gate diagnose`](../docs/cli.md#gate-queries) links promoted dependencies to
matched callback contracts rejected by supported namespace writes. Other guard
families, taint/exposure causes, and ambiguous links remain inconclusive. This
is a known coverage limit, not evidence that the conservative gate is wrong.
`spec audit-contracts` requires a passing baseline and does not explain a
failing ownership assignment.

**Acceptance:** extend one supported causal path at a time, connecting the
constraining dependency, applicable contract/signature, failed obligation and
source witness with a runnable inspection command. Keep unsupported causes
explicitly inconclusive; do not relax acceptance rules or claim the globally
earliest cause from one deterministic witness.

## H — Reduce repetitive naming-only edits

**Priority:** P2. **Owner:** naming CLI. **Implementation:** [#12556][batch-naming] (open).

Fifteen names required fifteen rename previews and fifteen applies, each with
its own invocation record. `bindings rename` accepts one pair. Existing
`bindings assign --batch` can supply `readable`, but requires destinations and
runs the assignment gate; the [naming workflow](../docs/naming.md) deliberately
keeps naming separate from ownership. Direct `name:` edits followed by
source-only validation are already supported, so this is convenience work.

**Acceptance:** offer a names-only batch with whole-map collision preflight,
dry-run, and one aggregate result. Reject invalid mappings before writes and
preserve selectors, claims and metadata. Keep source-only selector validation
after the coherent batch; a names-only verdict does not prove reference
identities in other templates stayed unchanged.

## F — Suggest evidence-backed regroupings for human module boundaries

**Priority:** P2. **Owner:** proposals and architect workflow.

[Architecture review](../docs/architecture_review.md), graph-backed proposals
and [decompose/refine](../docs/spec_editing.md#workflow-reorganizing-existing-modules)
already support the review and application stages. Semantic discovery across
numbered fragments still needs source review. A numeric name or a small file
alone does not justify a merge.

**Acceptance:** prioritize candidates with concrete consumer evidence, such as
sole-use helpers or component/style ownership. Show retained initialization
seams and gate the complete reviewed assignment. Semantic boundaries and names
remain author decisions. This is an optional discovery aid, not a current
validation blocker.

## Workflow lessons and addressed feedback

- Snapshots supplied the engine, graph, relocated package roots and regeneration
  argv successfully. Graph-staleness warnings, batched inspection, complete
  assignment previews and explicit empty-module deletion worked. Keep the
  [existing adapter contract](../skills/shared/workflow.md#adapter-contract);
  another orchestration checklist would duplicate it.
- Audit progress and recovery are addressed by [#12549][audit-progress] and
  [#12565][audit-checkpoint]. Stderr shows baseline/probe progress; checkpoints
  retain verified definitive results, rerun a fresh full baseline on resume,
  and require exact provenance/selection before reuse. The cap remains the total
  deterministic prefix, and independent-removal semantics are preserved.
- Identifier guidance and naming inventory hints are addressed by
  [#12551][identifier-guidance]. The [identifier reference](../docs/cli.md#name-and-identifier-resolution)
  distinguishes graph input/export names from editable selector/template-local
  names. Lookup misses give the spec inventory command.
- The same PR promoted bounded-first inspection and comment/note review into the
  [delegated-edit recipe](../docs/spec_editing.md#workflow-executing-a-delegated-assignment).
- I is addressed by [#12555][edge-census]: `describe` reports a complete raw-edge
  census by kind/role after direction/reason filters and before detail limits.
  Counts are directional incidences; filtered inspection remains separate from
  gate acceptance.
- The former D request for inspecting a known set of owners is covered by
  [`describe --batch`](../docs/cli.md#describe-related-ids-together), which accepts
  module paths too. Family discovery remains the narrower F request above.
- Filename-shaped refine destinations are fixed by [#12515][extension-guard]:
  `.yaml`/`.yml` values now fail with the extensionless alternative. The synthetic
  CLI regression covered dry-run/apply without mutation; this is addressed.
- Stale downloaded runfiles, stale native invocation footers, billing failures
  and dangling Git alternates belong to build/checkout tooling, not Debundle.
  Keep their recovery guidance with that owner rather than widening this list.

[reviewed-revision]: https://github.com/agentydragon/ducktape/commit/7241917c397dc75ee1dede7c39e3775fc4360d75
[extension-guard]: https://github.com/agentydragon/ducktape/pull/12515
[audit-progress]: https://github.com/agentydragon/ducktape/pull/12549
[audit-checkpoint]: https://github.com/agentydragon/ducktape/pull/12565
[identifier-guidance]: https://github.com/agentydragon/ducktape/pull/12551
[batch-naming]: https://github.com/agentydragon/ducktape/pull/12556
[edge-census]: https://github.com/agentydragon/ducktape/pull/12555
