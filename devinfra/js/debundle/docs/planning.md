# Plan assignments and seed worker tasks

Use the project's [adapter inputs](../skills/shared/workflow.md#adapter-contract)
and the [task chooser](../README.md#choose-a-task) to scope the work. Planning
and intake are read-only; each hands evidence to a worker who validates the edit.

## Plan an assignment

Use the adapter's current graph, matching modules tree, source, and reports.
Planning is read-only; a query may evaluate a candidate's gate without applying it.
Start with `graph-summary`, then use `describe` and `inspect-source --id` before
recommending an assignment. Use `coverage` for ownership and `atoms` for the
complete source-unit groups that must move together.
`describe` defaults to 20 rows per section. Check its truncation metadata and use
`--all` when deriving a complete assignment or dependency closure from its rows.

Choose the operation from the task chooser:

- For residual extraction, follow [propose and claim residuals](spec_editing.md#workflow-proposing-new-modules).
  Send reviewed proposals to intake. The proposal queue is a heuristic projection
  of the atomic DAG; graph readiness does not prove selectors or post-edit acceptance.
- For existing claims, follow [decompose and refine](spec_editing.md#workflow-reorganizing-existing-modules).
  Preserve selector anchors when needed, inspect the complete candidate's coverage,
  verdict, conflicts, and resolved names, and choose semantic destinations rather
  than accepting suggested paths mechanically. Hand the reviewed source-unit map
  directly to a worker. Residual groups from `--include-residual` cannot be applied
  through `modules refine`.
- For selector work, follow the [bulk conversion loop](selectors.md#bulk-conversion-loop).
  Give workers explicit items or coherent module scopes. Record repeated synthesis
  gaps as tooling work instead of assigning exact-body transcription.

Return the graph/input identity, reviewed units or proposals, source evidence,
destinations, selector anchors, candidate verdict, and unresolved questions.
Keep planning reports distinct from the operation's edit-batch format; the linked
CLI recipe defines that format.

## Prepare seeds

Intake consumes residual proposals, not existing-module decomposition reports.
Use short source slices to infer purpose from implementations, callers, stable
strings/properties, and source proximity. If choosing a destination requires
broad architectural discovery, mark low confidence and route to the architect.

For each `proposals[]` row with `landable_today: true`, classify it as `fresh`,
`extend_active`, or `side_effect_cell`, keep its complete source-unit set, and
choose a tentative destination/name. Skip diagnostic rows. A row with
`landable_today: false` is dispatchable only as a combined dependency closure;
otherwise report it as blocked. Unaddressable anonymous units remain advisory
until their selector question is resolved.

Write the adapter's `<scratch>/seeds.json`:

```json
[
  {
    "proposal_id": "auto_partition_0042",
    "kind": "fresh",
    "source_unit_ids": ["..."],
    "binding_ids": ["..."],
    "extends_module_id": null,
    "proposed_destination": "path/to/module.yaml",
    "proposed_names": { "minifiedBinding": "readableName" },
    "notes": "Why this appears to be one module.",
    "confidence": "high"
  }
]
```

Blocked dependency-closure seeds use `status: blocked_residual_dependency`.
Preserve planner order, preferring coherent high-confidence active-module
extensions and ordinary binding cells before riskier side-effect cells nearby.
Carry proposal IDs, units, reviewed destination and names into the worker packet;
the worker reviews the operation's dry-run selectors and can still reject or skip
it. Intake does not edit YAML, run gates/regeneration, or integrate branches.

Track source units by the current graph's unit IDs and bindings by chunk/name,
with the source/spec revisions that produced them. These identities are scoped
to one captured input program; proposal IDs may renumber after integration.
