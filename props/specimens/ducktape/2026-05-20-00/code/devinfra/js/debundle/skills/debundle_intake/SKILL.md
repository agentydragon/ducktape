---
name: debundle_intake
description: Turn `debundle peel plan-work` output into named, dispatchable seed clusters for debundle lane workers. Use for reading factorizer proposals, lightly grounding source meaning, choosing tentative destinations, and producing seeds.json without editing the spec or running gates.
---

# Debundle Intake

Use this role after `debundle_plan_work` has produced planner output. Intake
translates certified structural proposals into work packets for lane workers.

Read bundled references as needed:

- `references/workflow.md` for handoffs and scratch-state conventions
- `references/debundle_user_guide.md` for common graph/source query surfaces
- `references/module_shape.md` for destination and cohesion heuristics

## Inputs

The orchestrator or project adapter provides:

- `<factor-json>` from `debundle peel plan-work`
- `<graph>` and `<modules-dir>` for follow-up `explain` or `source-slice`
- `<source-root>` or emitted JS root for short body reads
- `<conventions-docs>` and any current architecture notes
- a scratch directory for `seeds.json`, `inflight.json`, `landed.json`, and
  `notes.md`

## Job

For each `proposals[]` entry with `landable_today: true`:

- classify it as `fresh`, `extend_active`, or `side_effect_cell`
- read enough source to choose a tentative name and destination
- keep whole owner sets together; do not split certified peel sets
- flag ambiguous or oversized proposals for the architect
- skip `diagnostics[]` as dispatchable work

Use `debundle_plan_work` commands for `explain` and `source-slice`. Do not
reimplement graph parsing by grepping generated output unless the graph lacks
the needed evidence.

## Source Reading Budget

Read small slices, usually 10-50 lines around the proposal or owner. Look for
visible strings, exported readable names, API names, component or class shapes,
registry keys, schema names, command IDs, and source proximity.

If destination selection requires broad cross-reference reading, mark
`confidence: low` or hand the question to the architect.

## Output

Write `<scratch>/seeds.json`:

```json
[
  {
    "factor_id": "auto_partition_0042",
    "kind": "fresh",
    "owner_ids": ["..."],
    "binding_ids": ["..."],
    "extends_module_id": null,
    "proposed_destination": "path/to/module.yaml",
    "proposed_names": {
      "minifiedBinding": "readableName"
    },
    "notes": "Why this appears to be one module.",
    "confidence": "high",
    "size_members": 5
  }
]
```

Sort seeds by:

1. planner proposal order
2. larger coherent landings within nearby proposals
3. high-confidence active-module extensions before fresh modules
4. lower-risk side-effect cells after ordinary binding cells

Track inflight and landed work by stable `binding_ids` / `owner_ids`, not only
auto-generated proposal IDs, because proposal IDs may renumber after each
integration.

## Boundaries

- Do not author spec edits.
- Do not run gate or regen commands.
- Do not integrate worker branches.
- Do not spend many tool calls resolving one proposal; produce useful seeds
  and let lane workers be the precision layer.
