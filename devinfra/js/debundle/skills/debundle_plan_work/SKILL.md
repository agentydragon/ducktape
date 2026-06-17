---
name: debundle_plan_work
description: Plan and inspect generic JS debundle spec work using read-only `debundle` queries. Use when an agent needs to turn owner_graph.json plus a modules tree into dispatchable module extraction work, query atomic-DAG and coverage status, inspect graph/source context, or decide what debundle spec edits should be made. Generic to any debundle target.
---

# Debundle Plan Work

Use this skill to plan read-only debundling work from the current
`owner_graph.json` and spec `modules/` tree. The output is evidence for
spec edits; this skill does not mutate YAML itself.

## Shared CLI Workflows

@references/cli_basics.md
@references/selectors.md
@references/spec_editing.md

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

- Start with `graph-summary` for orientation, then `modules propose`
  for dispatchable candidate work.
- For selector-stabilization planning, run
  `debundle spec selector-debt --group-module-depth N --min-score 70 --format json`
  before proposing worker lanes. Use the grouped rows to choose large,
  coherent module-family peels, then give workers explicit `--item` lists or
  scoped `--module` / `--module-prefix` selectors for
  `debundle spec synthesize-selectors`.
- Treat selector quality as a forward-compatibility problem, not only a
  current-build problem. A synthesized selector that copies a long exact
  function body, object literal, class body, or nested expression can be
  over-narrow even when it uniquely matches today's chunk. A landable selector
  should both match the current declaration and avoid pinning incidental
  bodies, argument lists, object values, and unrelated siblings. Prefer worker
  lanes that use minimized selectors with holes and stable anchors, and route
  oververbose synthesis output back to Ducktape minimization/tooling before
  scaling the pattern across many modules.
- Use `coverage` and `atoms` when current YAML or atomic-unit closure is
  the question.
- Use `describe` and `show-source` before recommending any assignment.
- Treat `modules propose` output as planning evidence. Only reviewed
  landable (`landable_today: true`) binding-only fresh/extension rows can
  be fed to `bindings assign --batch`; merge and anonymous-statement rows
  need the workflows in the shared guide, and `blocked_residual_dependency`
  rows need their closure grown or manual co-location first.

Prefer these commands over grepping generated output. The owner graph is
the source of truth for cycle gates and residual dependencies; the embedded
atomic DAG is the source of truth for indivisible move units. The proposal
queue is a heuristic projection from that DAG, not a serialized fact from
`debundle run`.

`peel <...>` invocations are deprecated aliases. Prefer the top-level
commands in all new docs, scripts, and reports.

Selector planning should also record tooling gaps. If `synthesize-selectors`
skips a large repeated shape, produces selectors that are too exact to be
forward-compatible, or cannot express a concise stable anchor, recommend a
Ducktape feature/fix before assigning many manual YAML edits. Do not plan lanes
whose work is simply to hand-transcribe exact long generated selectors; that is
minimization backlog, not finished selector stabilization.
