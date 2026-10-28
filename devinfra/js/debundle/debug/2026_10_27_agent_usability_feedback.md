# Debundle agent usability feedback

Reviewed against Ducktape [`a4a0178b7f`][reviewed-revision], with fresh feedback
from the integrator, ownership planner, lane worker, and CLI-fix worker. Examples
are generic; private source, identifiers and diagnostic payloads are excluded.
No unresolved Debundle correctness failure was established in this round.

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

## Contract-audit progress and recovery

**Priority:** P1. **Owner:** audit CLI.

A multi-probe `spec audit-contracts` run exceeded 15 minutes without intermediate
output. The current [implementation](../cli/contract_audit.rs) still accumulates
results in memory and emits its report after all probes. `--max-probes` bounds
probe count, not elapsed time. Even NDJSON currently emits only the completed
audit; interruption loses completed probe results.

**Acceptance:** show baseline/probe phase, current typed target and
completed/total count on stderr while preserving stdout's report contract.
Persist provenance-keyed completed probes with a documented resume/collection
path. Keep the current conditional verdict: independent successful removals do
not prove joint removability or independent purity. This is the highest-impact
remaining execution friction.

## G — Bridge graph, source-template and edit identifiers

**Priority:** P1. **Owner:** CLI help, identifier diagnostics and naming workflow.

The [CLI guide](../docs/cli.md#name-and-identifier-resolution) says binding
commands accept input-bundle or readable names. The two actual lookups differ:
[graph queries](../peel/plan.rs) use graph binding/export names, whereas
[`bindings rename`](../cli/binding.rs) resolves names from the current spec;
for `source_matches` it uses the template's `local` token and optional `name`.
A graph-visible bundle alias can therefore fail a rename, while a template-local
alias can fail `describe`. Four of fifteen rename previews needed this correction;
the error only said that no binding was found. This was recoverable invocation
friction, not a corrupting rename.

**Acceptance:** replace the blanket identifier promise with a small table showing
input binding, template `local`, and exported `name`, and which command resolves
each. Link it from [naming](../docs/naming.md). On a failed lookup, suggest the
appropriate inventory command, such as `bindings list --in <owner>`, or a known
current token when the available metadata supports it. Do not guess an
ambiguous identity or require graph inputs for spec-only naming.

## H — Reduce repetitive naming-only edits

**Priority:** P2. **Owner:** naming CLI.

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

## I — Make ownership inspection progressively bounded

**Priority:** P2. **Owner:** inspection CLI and planner recipe.

Exhaustive `describe --batch --all` calls produced roughly 8 MB and 42 MB in two
reviews; a high-fanout selection dominated the latter. Both callers explicitly
requested exhaustive output. Existing `--limit`, `--direction` and `--reason`
work and should remain the first choice; this is not a broken default limit.
The remaining ergonomic gap is distinguishing direct consumers from promoted
initialization dependencies without expanding every edge. Current reason
filtering selects `edge_kind`, not the separate dependency role.

**Acceptance:** add a compact edge census by kind/role, or a role filter that
keeps direct and promoted-at-init evidence distinguishable. Promote a two-step
planner recipe: bounded family/module inspection first, then focused incoming
consumer queries; reserve `--all` for an intentional exhaustive artifact. Keep
omitted counts visible and never treat a filtered ownership view as a gate.

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
- Promote one local step into the [delegated-edit recipe](../docs/spec_editing.md#workflow-executing-a-delegated-assignment):
  review attached comments/notes when moving claims. A stale ownership/gate
  explanation can survive a mechanically correct move. Recheck it against
  current source and gate evidence, transfer meaningful module comments before
  deleting shells, and preserve selector debt and actual author contracts.
- The former D request for inspecting a known set of owners is covered by
  [`describe --batch`](../docs/cli.md#describe-related-ids-together), which accepts
  module paths too. Family discovery remains the narrower F request above.
- Filename-shaped refine destinations are fixed by [#12515][extension-guard]:
  `.yaml`/`.yml` values now fail with the extensionless alternative. The synthetic
  CLI regression covered dry-run/apply without mutation; this is addressed.
- Stale downloaded runfiles, stale native invocation footers, billing failures
  and dangling Git alternates belong to build/checkout tooling, not Debundle.
  Keep their recovery guidance with that owner rather than widening this list.

[reviewed-revision]: https://github.com/agentydragon/ducktape/commit/a4a0178b7ff6885458a609679528de7bbd48d675
[extension-guard]: https://github.com/agentydragon/ducktape/pull/12515
