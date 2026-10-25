---
name: debundle_plan_work
description: Plan JS debundle extraction or existing-module reorganization using read-only graph and source queries. Use for residual proposals, gate-checked decomposition, atomic coverage, selector anchors, and reviewed owner-assignment handoffs. Generic to any debundle target; this skill does not edit the spec.
---

# Debundle Plan Work

If debundle tooling blocks the task, follow
`references/docs/bug_reproductions.md` before working around it.

Use this skill to plan read-only debundling work from the current
`owner_graph.json` and spec `modules/` tree. The output is evidence for
spec edits; this skill does not mutate YAML itself.

## Shared CLI Workflows

@references/docs/cli.md
@references/docs/selectors.md
@references/docs/spec_editing.md

## Setup Notes

Find the debundle output and spec modules directory for the target.
Export the standard env vars as shown in the shared guide. In a consuming
Bazel repo, use the external `@ducktape` label; inside the debundler repo,
drop the repository prefix.

If `bazelisk run @ducktape//...` has Bazel server or output-download
trouble in a consuming repo, build the CLI with an isolated output base
and run the built binary directly:

```bash
bazelisk --output_base=/tmp/debundle-cli-bazel \
  build @ducktape//devinfra/js/debundle:debundle \
  --remote_download_outputs=all
```

## Planning Loop

- Start with `graph-summary` and choose the planning task:
  - For fresh residual extraction, use `modules propose` and send reviewed
    proposal rows to intake for semantic naming and selector proof.
  - For existing-module reorganization, use `modules decompose --atomic`.
    Add `--preserve-selector-anchors --source-root` when module-local selector
    relationships must survive the split. Inspect the complete candidate's
    coverage, verdict, conflicts, and resolved names before assigning work.
    The CLI guide's "Refining existing modules" section describes optional
    atomic/SCC coarsening, original-home rejoining, and search budgets.
    Suggested paths are temporary: choose semantic destinations and hand the
    reviewed owner map to a lane worker for `modules refine`.
- For selector-stabilization planning, run
  `debundle spec selector-debt --group-module-depth N --min-score 70 --format json`
  before proposing worker lanes. Use the grouped rows to choose large,
  coherent module-family peels, then give workers explicit `--item` lists or
  scoped `--module` / `--module-prefix` selectors for
  `debundle spec synthesize-selectors`.
- Hold planned selector work to `references/docs/selectors.md` § The contract and
  the ladder: prefer lanes that land minimized selectors.
- Use `coverage` and `atoms` when current YAML or atomic-group closure is
  the question.
- Use `describe` and `inspect-source --id` before recommending any assignment.
- Keep planning reports distinct from edit batches. `modules refine` accepts
  reviewed owner-to-module rows for existing non-residual claims; it cannot
  apply residual groups from `--include-residual` or synthesize selectors.
  Fresh residual claims use `modules apply`; focused binding moves use
  `bindings assign`. See `references/docs/cli.md` for each batch format.

Prefer these commands over grepping generated output. The owner graph is
the source of truth for cycle gates and residual dependencies; the embedded
atomic DAG is the source of truth for sets of owners that must move together. The proposal
queue is a heuristic projection from that DAG, not a serialized fact from
`debundle run`.

Selector planning should also record tooling gaps. If `synthesize-selectors`
skips a large repeated shape, produces selectors that are too exact, or cannot
express a concise stable anchor, recommend a Ducktape feature/fix before
assigning many manual YAML edits; never plan lanes that hand-transcribe exact
long generated selectors.
