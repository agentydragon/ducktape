---
name: debundle_stabilize
description: >-
  Author forward-compatible `source_match` selectors for a debundle spec,
  replacing fragile minified-name pins with structural anchors tied to each
  entity's purpose. Not for renaming symbols (debundle_mint_names) or moving
  module boundaries (debundle_architect).
---

# Debundle Stabilize

Replace fragile selectors with purpose-bearing structural anchors. Edit
selectors and documented selector debt only; naming belongs to
`debundle_mint_names`, module boundaries/taxonomy to `debundle_architect`, and
read-only worklists to `debundle_plan_work`. Never edit upstream source.

Read the [contract and ladder](references/docs/selectors.md#the-contract-and-the-ladder),
then [review one selector](references/docs/selectors.md#review-one-selector) or
use the [bulk conversion loop](references/docs/selectors.md#bulk-conversion-loop).
These guides own anchor choice, synthesis review, probes, and validation. The
[task chooser](references/README.md#choose-a-task) links to other operations.

Validate the exact current source/spec pair; uniqueness today does not prove
future rebuild stability. Return reviewed selector edits, actual adapter gate
results, debt change, and unresolved tooling gaps. If the tool cannot express an
honest anchor, retain documented debt and follow the
[bug reproduction workflow](references/docs/bug_reproductions.md).
