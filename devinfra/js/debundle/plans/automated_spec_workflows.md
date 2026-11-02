# Automated Debundle Spec Workflows

Remaining work for single-input spec authoring. The dispatch order and active
selector tasks live in <../TODO.md>; selector syntax, resolution, and current
commands are specified in <../docs/selectors.md>,
<../docs/selector_resolution.md>, and <../SPEC.md>.

Every invocation resolves one exact spec against its configured input program.
A prior spec may be an author's manual starting point, but tools do not compare
source snapshots or generate migrations. Candidate selectors are suggestions:
the author decides semantic intent, and the shared resolver proves whether a
candidate uniquely selects its target.

## Remaining work

### Repair diagnostics and proposals

Improve keep-going selector reports and offer proven repairs for outcomes that
can be repaired mechanically. Remaining diagnostic gaps are listed under
[Automation product flows](../TODO.md#automation-product-flows-over-the-solver):
first mismatch details for declaration ranges, identifier and expression
bindings, parameter patterns, and list-hole bindings; near misses for
multi-statement templates; name-pin debt for `note:` annotations; and readable
`alpha_all` names for free references. Ambiguous outcomes also need useful
differentiators when the candidate list is truncated. For duplicate claims,
identify whether the repair is a binding-group collapse, cross-module group
support, or a genuine ownership conflict. Preserve relational premises and
domain reductions when explaining contradictions.

A repair proposal must retain the current target and proposed selector, explain
its evidence and skip reason, and pass the production resolver against the same
source and spec before application. A reusable plan-file/apply-plan interface
has not been established; decide whether it improves the current
`--apply`/dry-run workflow before adding another command layer. Any stored plan
must be re-resolved before it is applied, since its source or spec may have
changed since review.

### New-spec bootstrap

Compose module proposals, naming output, and selector synthesis into a
reviewable first spec. Keep the generated output explicit about low-confidence
names, unaddressable anonymous statements, and candidates needing manual
context. The workflow must not make semantic ownership decisions on the
author's behalf. Module proposal follow-ups are in
<module_proposals.md>; naming conventions are in <../docs/naming.md>.

### Bulk stabilization follow-through

Continue the unresolved ranking, minimization, and fixture work in
[selector authoring](../TODO.md#automation-product-flows-over-the-solver) and
[read-off minimizer polish](../TODO.md#read-off-minimizer-polish). In
particular, source-aware ranking for multi-statement windows and stable literal
values, cross-module binding groups, and grounded stabilization examples remain
open. Keep batches reviewable and report the debt before and after a batch.

## Acceptance contract

- A structural candidate matches only the declared syntax and alpha-renaming
  rules. Similarity to another source snapshot is not evidence.
- A candidate is accepted only when the shared resolver proves the intended
  binding, group, or anonymous statement unique in the exact configured input.
- The one-shot `synthesize-selectors --apply` path writes only candidates that
  already have a uniqueness proof, validates the rewritten YAML, and uses atomic
  replacement. Dry-run output lists proposed changes and skipped candidates with
  their reasons. Any future stored plan must re-resolve before writing.
- Keep-going validation reports the complete selector backlog. For a real spec
  conversion, verify the resolved binding is unchanged and emitted output stays
  byte-identical under the target gate.
- Stay within the interactive budget in
  <../docs/selector_resolution.md#interactive-budget>; use fresh profiles before
  choosing expensive matcher or index rewrites.
