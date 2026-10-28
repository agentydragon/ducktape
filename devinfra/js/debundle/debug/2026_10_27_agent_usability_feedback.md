# Debundle agent usability feedback

Remaining requests from the integrator and structural-ownership,
selector/contract-authoring, and callback/effect-analysis workers, reviewed against
Ducktape [`b2af78e8e0`](https://github.com/agentydragon/ducktape/commit/b2af78e8e06dc3ba3e887cdab409abde246dd14f).
Examples are generic; no private source or diagnostic payload is included.

## A — Extend causal diagnostics beyond the supported write guard

**Priority:** P0. **Owner:** Debundle diagnostics.

[`gate diagnose`](../docs/cli.md#gate-queries) links promoted dependencies to
matched callback-factory contracts and static namespace-member write witnesses.
The remaining gap is explaining other rejected guards and earlier callback
taint or exposure without temporary interpreter logging and repeated builds.
The current query explicitly leaves those causes inconclusive; its deterministic
write witness does not establish the globally earliest cause.

**Acceptance:** extend the supported traces to those guard and taint families.
Connect the constraining dependency to the matched contract/signature when
applicable, the failed obligation or prior exposure, and its source witness with
a runnable inspection command. Start with one supported causal path and permit
expansion. Keep unsupported causes explicitly inconclusive and preserve the
conservative fallback; diagnostics must not relax acceptance rules.

## D — Finish compact inspection and structured failure reporting

**Priority:** P1. **Owner:** inspection CLI and report interface.

`describe --limit` already bounds report sections with total/emitted counts;
`inspect-source --id` resolves graph IDs to source; `gate diagnose` is bounded
by default. Graph-compatibility and realizability-gate rejections already have
structured JSON results.

Remaining friction on the approximately 690 MB corpus graph:

- `describe` defaults to unlimited output, and `cluster` emits every neighbor.
  Finding the relevant constraint still requires filtering thousands of rows.
- Related binding queries reload the graph. Module-path selection helps with
  one owner, but there is no grouped binding/module-family query for a review.
- Unsupported query prefixes fall through to binding lookup (see E), and
  errors outside the structured rejection paths can still leave redirected
  JSON output empty with only a text error on stderr.
- `spec validate` reports outcome counts, but zero reported issues does not
  state how many selectors were checked or matched.

**Acceptance:** provide compact defaults with an explicit exhaustive mode,
direction/reason filters, and grouped queries with truncation counts. Reuse
loaded indexes across related queries where practical. Extend structured JSON
failure results to lookup and input errors with actionable reason codes and
nonzero exit status. Validation should explicitly report success and
checked/matched counts, including an empty selection.

Shared fixture report readers should also expose CLI stderr and the case/report
path when an expected conflict artifact is absent; a file-read panic hides the
pipeline rejection that prevented the artifact from being written.

## E — Reject unsupported identifier prefixes before graph work

**Priority:** P2. **Owner:** authoring CLI.

The gated [whole-module merge batch](../docs/cli.md#whole-module-merge-batches)
provides exact-case file moves and previews writes/deletions. The
[`modules refine` guide](../docs/cli.md#applying-reviewed-source-unit-assignments-modules-refine)
now distinguishes logical IDs from YAML filenames. The remaining identifier
gotcha is that `module:` looks like a plausible query prefix, but the accepted
module ID prefix is `logical:`. Unsupported prefixes currently fall through to
binding lookup after graph loading and compatibility work.

**Acceptance:** reject an unsupported typed prefix before expensive graph work,
suggest the accepted prefix, and show a valid example such as `logical:7`.

## F — Suggest evidence-backed regroupings for human module boundaries

**Priority:** P2. **Owner:** proposals, authoring CLI and architect workflow.

The [architecture review](../docs/architecture_review.md) and
[decompose/refine workflow](../docs/spec_editing.md#workflow-reorganizing-existing-modules)
support reviewing and applying reorganizations. The remaining tooling gap is
finding semantic regroupings across numbered fragments. Concrete recovery tasks
included thirteen fragments forming one stylesheet and twenty tiny wrappers
around one shared implementation. Other numbered fragments contained substantial
classes with sensible individual seams; a numeric name alone is insufficient
evidence to merge them.

**Acceptance:** offer regrouping candidates with dependency evidence for sole-use
helpers, constants consumed by one export map, wrapper families, and
component/style ownership. Show retained initialization seams and gate the
complete reviewed assignment. Keep author judgment over semantic boundaries and
names; candidates remain suggestions.
