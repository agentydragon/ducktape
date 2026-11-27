# Automated Debundle Spec Workflows

Remaining work for single-input spec authoring. The dispatch order and active
selector tasks live in <../TODO.md>; selector syntax, resolution, and current
commands are specified in <../docs/selectors.md>,
<../docs/selector_resolution.md>, and <../SPEC.md>.

Tools work inside the one-spec, one-configured-input-program boundary
(<../SPEC.md> § Input and spec scope): a prior spec may be an author's manual
starting point, but tools never compare source snapshots or generate
migrations. Candidate selectors stay suggestions — the author decides semantic
intent, and the shared resolver proves whether a candidate uniquely selects its
target.

## Remaining work

### Repair diagnostics and proposals

Follow the [selector diagnostics backlog](../TODO.md#automation-product-flows-over-the-solver)
for mismatch and relational-contradiction work. For truncated ambiguous results,
show useful differentiators. For duplicate claims, distinguish binding-group
collapse, cross-module group support, and genuine ownership conflicts.

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
<explicit_import_order.md>; naming conventions are in <../docs/naming.md>.

### Bulk stabilization follow-through

Use the [read-off minimizer backlog](../TODO.md#read-off-minimizer-polish) and
[fixture follow-ups](../TODO.md#test-infrastructure). Measure selector debt before
and after each reviewed batch.

## Acceptance contract

- A structural candidate matches only the declared syntax and alpha-renaming
  rules. Similarity to another source snapshot is not evidence.
- A candidate is accepted only when the shared resolver proves the intended
  binding, group, or anonymous statement unique in the exact configured input.
- Preserve the [reviewed bulk conversion workflow](../docs/selectors.md#bulk-conversion-loop),
  including skipped-candidate reasons, uniqueness proof, YAML validation and
  atomic writes.
- Keep-going validation reports the complete selector backlog. For a real spec
  conversion, verify the resolved binding is unchanged and emitted output stays
  byte-identical under the target gate.
- Stay within the interactive budget in
  <../docs/selector_resolution.md#interactive-budget>; use fresh profiles before
  choosing expensive matcher or index rewrites.
