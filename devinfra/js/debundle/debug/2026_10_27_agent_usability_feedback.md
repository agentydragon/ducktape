# Debundle agent usability feedback

Remaining requests from the integrator and structural-ownership,
selector/contract-authoring, and callback/effect-analysis workers, reviewed
against Ducktape [`7f8ce44b91`](https://github.com/agentydragon/ducktape/commit/7f8ce44b914250e92cf637ae02ce0c01c51a1999).
Examples are generic; no private source or diagnostic payload is included.

## A — Extend causal diagnostics beyond supported write witnesses

**Priority:** P0. **Owner:** Debundle diagnostics.

[`gate diagnose`](../docs/cli.md#gate-queries) now traces promoted dependencies
to matched callback-factory contracts rejected by supported static namespace
member writes or direct computed-key writes through an imported namespace or
recognized alias. Other receiver shapes and guard families remain inconclusive;
the query also does not explain taint or exposure causes. Its deterministic
write witness does not establish the globally earliest cause.
`spec audit-contracts` separately probes individual chunk-export contracts only
after a passing full-pipeline baseline; a failing ownership baseline stops
before those removal probes, so the audit does not replace this causal trace.

**Acceptance:** extend the supported traces to additional guard and taint
families. Connect the constraining dependency to the matched contract/signature
when applicable, the failed obligation or prior exposure, and its source
witness with a runnable inspection command. Start with one supported causal
path and permit expansion. Keep unsupported causes explicitly inconclusive and
preserve the conservative fallback; diagnostics must not relax acceptance rules.

## Contract-audit progress and recovery

**Priority:** P1. **Owner:** audit CLI.

The [automated-workflow plan](../plans/automated_spec_workflows.md#performance-plan)
expects commands beyond the interactive budget to stream progress and write
resumable intermediate plans. One observed multi-probe `spec audit-contracts`
run exceeded 15 minutes without intermediate output: the implementation holds
probe results in memory and emits one report after all selected probes finish.
`--max-probes` limits the number of probes, not elapsed time, so interruption
leaves no completed per-probe results to collect or resume.

**Acceptance:** report the current typed target and completed/total count on
stderr while keeping stdout as one valid JSON report, or persist provenance-keyed
per-probe checkpoints with a documented resume/collection path. Preserve the
existing conditional verdict scope: each removal is tested independently with
all other contracts retained; passing removals are not jointly removable or
independently inferred.

## D — Discover related bindings across a module family

**Priority:** P1. **Owner:** inspection CLI.

[`describe <module-path>`](../docs/cli.md#name-and-identifier-resolution)
reports one owner's claims and direct graph neighbors, while
[`cluster <binding>`](../docs/cli.md#name-and-identifier-resolution) reports
adjacent module neighbors for one binding. [`describe --batch`](../docs/cli.md#describe-related-ids-together)
can inspect a caller-supplied ID list with one graph load and verification.
These cover one-hop exploration and already-known IDs, but discovering a
reviewer-defined family across multiple owners still requires separate queries
and manual ID curation.

**Acceptance:** add a query that discovers bindings across a selected set of
module owners or a supported semantic family and returns the group in one
bounded result with explicit total, emitted, and truncation counts. Keep the
existing batch path available for reviewers who already have an explicit ID
list.

## F — Suggest evidence-backed regroupings for human module boundaries

**Priority:** P2. **Owner:** proposals, authoring CLI and architect workflow.

The [architecture review](../docs/architecture_review.md) and
[decompose/refine workflow](../docs/spec_editing.md#workflow-reorganizing-existing-modules)
support reviewing and applying reorganizations. The remaining tooling gap is
finding semantic regroupings across numbered fragments. Useful candidates may
include related style fragments or thin wrappers around a shared implementation;
other numbered fragments can contain substantial classes with sensible
individual seams. A numeric name alone is insufficient evidence to merge them.

**Acceptance:** offer regrouping candidates with dependency evidence for
sole-use helpers, constants consumed by one export map, wrapper families, and
component/style ownership. Show retained initialization seams and gate the
complete reviewed assignment. Keep author judgment over semantic boundaries
and names; candidates remain suggestions.
