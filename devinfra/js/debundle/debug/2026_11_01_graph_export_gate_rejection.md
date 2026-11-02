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

## Remaining evidence limit

A canonical summary reported both one atomic-group conflict and two cycles,
while only `atomic_group_conflicts.json` was present. The gate report writer
selects the atomic-conflict report ahead of the cycle report; an absent
`cycles.json` therefore does not mean a zero cycle count. This change preserves
the exact summary and existing detailed-report behavior. Exporting both detailed
reports is a separate follow-up if spec authors need the secondary witness set.

The change was not run against the private canonical corpus. The delegated
scope prohibited tests, builds and corpus jobs; hosted PR CI remains the next
runtime validation.
