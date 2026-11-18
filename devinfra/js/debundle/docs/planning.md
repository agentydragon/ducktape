# Plan assignments and seed worker tasks

Use the project's [adapter inputs](../skills/shared/workflow.md#adapter-contract)
and the [task chooser](../README.md#choose-a-task). Planning and intake are
read-only; workers apply reviewed plans and validate the emitted result.

For a broadly rejecting spec, use a reproducible
[passing residual baseline and focused probes](../skills/shared/workflow.md#start-from-passing-residual-output-and-isolate-failing-assignments).
Retain useful passing claims and isolate conflicting additions. A failed normal
residual-only baseline needs a tool/input investigation.

## Plan an assignment

For “what can leave the residual?”, begin with unseeded
[`modules propose`](module_proposals.md). Review the ownership map, source
meaning, destinations and final import priority, then check, preview and apply
that same plan. See [placement guarantees and composition](module_proposals.md).

Use `graph-summary`, `coverage`, `describe`, `inspect-source --id` and `atoms` to
understand discovered groups. Atoms are required co-location groups, not a list
of independently extractable modules. `describe` defaults to 20 rows per section;
check truncation metadata and use `--all` when deriving a complete closure.

For a `possible_getter` reason, inspect `lookup.lexical_owner` and its byte span,
then `promotion.source_owner` when the promotion kind is `lazy_owner`. A promoted
binding read does not make that binding an accessor. `raw_callbacks` identifies
no single body, and a witness need not enumerate every contributor. Check all
reasons and reciprocal paths before claiming a lookup change breaks a cycle;
see [effect provenance](wire_format.md#binding-ordering-effect-provenance).

For a reviewed explicit assignment, use `modules check --batch changes.json`.
Include anonymous units, required companions and the actual existing or new
destinations. Omitted units retain their owners. Keep its returned `.plan` for
review/application. Existing-claim reorganization uses the same
[check and apply workflow](spec_editing.md#workflow-reorganizing-existing-modules).
Changing destinations or combining independently accepted plans needs another
complete check. Default checks preflight selector realization before returning
a plan; explicit `--graph-only` checks report graph validity without a plan.
Normal materialization and relevant behavioral evidence remain required.

For selector work, use the [bulk conversion loop](selectors.md#bulk-conversion-loop).
Give workers explicit items or coherent module scopes. Report repeated synthesis
gaps as tooling work rather than assigning exact-body transcription.

Return the graph/input identity, complete plan, source evidence, destination
review, selector anchors and unresolved questions. Keep diagnostic reports
separate from the edit-batch format.

## Prepare seeds

Intake consumes one exact placement plan plus matching graph/source and project
conventions. Use focused source slices, callers, stable strings/properties and
source proximity to infer purpose. Route broad architecture questions to the
architect. A valid peel need not be a good semantic module boundary.

Keep a jointly validated plan as one worker packet, including all companion
atoms and destinations. Add source evidence and semantic naming recommendations
separately. Changing its destination map or combining plans returns to planning
for another complete check. Do not split the plan into independently writable
lanes or invent proposal IDs to replay later.

Write the adapter's `<scratch>/seeds.json`, for example:

```json
[
  {
    "chunk_id": "main",
    "plan_path": "checked-plan.json",
    "graph_path": "dependency_graph.json.gz",
    "destination_review": {
      "helpers/arithmetic": "Numeric conversion helpers used by formatting."
    },
    "readable_names": { "a": "normalizeNumber" },
    "notes": "Keep the complete plan together; review synthesized selectors.",
    "confidence": "high"
  }
]
```

The plan itself carries graph/program identity, the exact assignment map and
the complete final import priority.
Source-unit and binding identities belong to that captured program; a refreshed
graph cannot safely reuse an old plan. Intake does not edit YAML, run gates or
regeneration, or integrate branches. Workers retain the normal edit and runtime
checks, and may report a selector or architecture problem before applying.
