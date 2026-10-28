# Debundle agent usability feedback

Remaining requests from the integrator and structural-ownership,
selector/contract-authoring, and callback/effect-analysis workers, reviewed
against Ducktape [`04ab7fe252`](https://github.com/agentydragon/ducktape/commit/04ab7fe252c3b11587ee20f8f61fd90131ab4506).
Examples are generic; no private source or diagnostic payload is included.

## A — Extend causal diagnostics beyond supported write witnesses

**Priority:** P0. **Owner:** Debundle diagnostics.

[`gate diagnose`](../docs/cli.md#gate-queries) now traces promoted dependencies
to matched callback-factory contracts rejected by supported static namespace
member writes or direct computed-key writes through an imported namespace or
recognized alias. Other receiver shapes and guard families remain inconclusive;
the query also does not explain taint or exposure causes. Its deterministic
write witness does not establish the globally earliest cause.

**Acceptance:** extend the supported traces to additional guard and taint
families. Connect the constraining dependency to the matched contract/signature
when applicable, the failed obligation or prior exposure, and its source
witness with a runnable inspection command. Start with one supported causal
path and permit expansion. Keep unsupported causes explicitly inconclusive and
preserve the conservative fallback; diagnostics must not relax acceptance rules.

## D — Discover related bindings for one inspection

**Priority:** P1. **Owner:** inspection CLI.

[`describe --batch`](../docs/cli.md#describe-related-ids-together) accepts a
caller-supplied list of identifiers and reuses one graph load and verification
for those selections. A module-path selection resolves claims for one owner;
the CLI has no query that discovers related bindings by module or semantic
family for a review. Collecting a group's identifiers still requires a
separate discovery step.

**Acceptance:** add a query that discovers bindings related by a selected
module or supported semantic family, with bounded results and explicit total,
emitted, and truncation counts. Keep the existing batch path available for
reviewers who already have an explicit ID list.

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
