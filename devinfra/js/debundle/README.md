# Debundle

`debundle` is a reverse-engineering tool for maintaining a repeatable mapping from
minified JavaScript bundles to readable, source-like modular code with equivalent
behavior.

## Choose a task

| Task                                 | Start here                                                                                                                                   | Result to review                                                                                            |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Run a spec against captured source   | [Getting started](#getting-started) and [pipeline validation](docs/cli.md#running-the-pipeline)                                              | Emitted modules, selector outcomes, and gate result                                                         |
| Understand a binding                 | [Inspect a binding end to end](docs/cli.md#workflow-investigating-a-binding-end-to-end)                                                      | Source, dependencies, and current assignment                                                                |
| Extract unassigned code              | [Exact residual placement](docs/module_proposals.md)                                                                                         | Inspect, construct and review one scoped graph placement, check/preview/apply it, then run behavior checks  |
| Inventory residual atomic groups     | [Residual atomic group report](docs/cli.md#residual-atomic-group-report)                                                                     | Source units, destinations, and bounded dependency evidence, including for ownership subsets                |
| Move existing bindings               | [Focused binding moves](docs/spec_editing.md#workflow-moving-a-binding-from-one-module-to-another)                                           | Reviewed move batch and gate verdict                                                                        |
| Review architecture and module shape | [Architecture review](docs/architecture_review.md)                                                                                           | Current architecture notes, measured conventions, and worker-ready reorganization tasks                     |
| Reorganize existing assignments      | [Check and apply a placement](docs/spec_editing.md#workflow-reviewing-existing-module-boundaries)                                            | Complete source-unit assignment and gate verdict                                                            |
| Recover readable binding names       | [Naming workflow](docs/naming.md)                                                                                                            | Source-grounded names, reviewed YAML changes, and naming verification                                       |
| Rename a binding                     | [Rename without moving](docs/spec_editing.md#workflow-renaming-a-binding-without-moving)                                                     | Name change and collision check                                                                             |
| Rename or merge module files         | [Rename a module](docs/spec_editing.md#renaming-or-disabling-a-module) or [merge modules](docs/spec_editing.md#workflow-merging-two-modules) | Gated YAML edit preserving claims and metadata                                                              |
| Stabilize selectors                  | [Bulk conversion loop](docs/selectors.md#bulk-conversion-loop)                                                                               | Reviewed anchors, exact-source validation, and remaining debt                                               |
| Prune inferable annotations          | [Spec lint](docs/cli.md#running-the-pipeline)                                                                                                | Typed `inferable`/`inconclusive` findings by authored identity; removal is a reviewed edit                  |
| Diagnose a rejected edit             | [Assignment diagnosis](skills/shared/workflow.md#diagnose-a-rejected-assignment)                                                             | Source-backed cause and justified spec, analysis or contract repair                                         |
| Break a sequenced-only cycle         | [Proposing commutations](docs/spec_editing.md#workflow-proposing-commutations)                                                               | Ranked `commuting_statements` draft with each inerted edge's explanation, audited and applied by the author |
| Plan, delegate, or integrate work    | [Shared work procedures](skills/shared/workflow.md)                                                                                          | Scoped plans, isolated copied inputs, completed execution evidence, and validated integration               |
| Extend or repair a partial spec      | [One useful increment](skills/shared/workflow.md#complete-one-useful-increment)                                                              | Passing candidate, readable emitted structure, relevant runtime observations and current handoff            |

Use the project adapter for concrete paths, gate, regeneration, and runtime
checks. See [saved graph compatibility](docs/cli.md#common-arguments-and-env-vars)
and [invocation records](docs/cli.md#resolved-invocation-records) for graph-backed
work. Use the installed `debundle_stabilize` skill when reviewing selectors for
a new capture.

## Getting started

```sh
debundle run \
  --tree-config spec/spec_config.yaml \
  --tree-modules spec/modules \
  --tree-vendor-marks spec/sources/vendor/vendor_marks.yaml \
  --tree-source-root . \
  --out-root bazel-bin/example/debundle.out
```

See [tree spec configuration](docs/spec_editing.md#tree-spec-configuration) for
an example of the YAML and input layout. The [CLI guide](docs/cli.md#running-the-pipeline)
covers validation and report output.

## Reference

- [Spec contract](SPEC.md) and `docs/selector_resolution.md`.
- `docs/design.md`: the design and correctness proof, § File references for implementation entry points,
  and § Conditionally-correct optimizations.
- `TODO.md` work queue, `plans/` active plans, [performance measurements](perf/),
  and `docs/lessons_learned/`.

## Bazel integration and profiling

See [analysis and apply rules](docs/bazel_integration.md#explicit-analysis-and-apply-stages),
[graph query launchers](docs/bazel_integration.md#graph-command-launchers), and
[profiling](docs/bazel_integration.md#profiling).
