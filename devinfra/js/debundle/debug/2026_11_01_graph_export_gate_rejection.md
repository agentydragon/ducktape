# Graph export stops at a typed assignment-gate rejection

## Observed friction

Canonical private CI completed `DebundleAnalyze`, then failed
`DebundleExportGraphs` when an atomic assignment split was rejected. The action
did not complete its `dependency_graphs` output, so graph consumers could not
read the full graph inventory or the reports that had been written before the
failure. The normal pipeline is supposed to reject that assignment.

## Cause and bounded change

The graph action calls the same Starlark command builder as the normal
pipeline. Before this change, it inherited `--emit-on-rejection` only when the
target opted into readable rejected JavaScript. Its `--reports-only` mode
already has a typed gate-rejection path: it completes materialization, writes
the reports-only marker, emits no JavaScript, and exits 3. The graph action now
opts into that path directly and accepts only exit 3 in addition to success.
Every other CLI failure remains a failed action. The normal pipeline still
uses only its explicit `emit_on_rejection` attribute and retains its existing
runnable-output acceptance boundary.

Materialization gathers all selected chunk results before the final typed
rejection, so the reports-only export can finish the configured chunk set.
`graph_summary.json` preserves exact counts, and detailed rejection reports
retain the materializer's existing selection rules.

The route is in `pipeline.bzl` (`DebundleExportGraphs` and
`_pipeline_command`). `pipeline.rs` maps `--emit-on-rejection` to reports-only
marker emission and typed exit 3. `lowering/mod.rs` aggregates the per-chunk
results; detailed report selection is in `lowering/materialize/mod.rs`.

## Public cycle-report reproduction

A neutral fixture with `const alpha = beta; const beta = alpha;` assigned to two
modules produces one atomic-group conflict and one blocking SCC. Both the run
pipeline and a rejected edit now write the existing `atomic_group_conflicts.json`
and `cycles.json` shapes. The atomic conflict remains the verdict and primary
diagnostic. `gate list` and `gate describe` can read the cycle report beside the
atomic report; a missing cycle file means zero blocking SCCs only for a complete
current report set. The edit gate replaces earlier rejection artifacts after
graph, assignment and shared validation complete, while a preflight error keeps
the last completed gate result. For an edit-gate report, `gate describe` returns
the stored candidate modules and cut, while graph-derived `evidence` is
recomputed from the saved dependency graph's original module destinations. In
the neutral edit fixture those destinations still contain both bindings, so
the candidate cut is present and recomputed evidence is empty. The run-pipeline
fixture has a graph and report for the same assignment and retains nonempty
recomputed evidence.

The neutral CLI regressions run on RBE. A downstream private corpus is not part
of this public fixture and remains the separate integration proof.
