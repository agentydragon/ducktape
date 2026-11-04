# Execution graphs and OTel phase traces

Use this procedure for build or test long poles, scheduling bubbles, and multistage
processes such as environment startup, guest boot, native session opening and
teardown. Analyze the dependencies and waits that determine elapsed time, rather
than stopping at a duration ranking or waterfall.

## Connect the graph layers

Start with the exact invocation, tested source, selected workload and cache state.
Connect required workflow gates to Bazel invocations, build actions and TestRunner
actions, then to fixture, scenario, service and subprocess stages inside those
actions. Include upstream compilation, packaging and image construction that delay
a test, and cleanup or output handling after its assertions finish. Distinguish the
test verdict from the completion of the action and its enclosing required gate.

Use Bazel's recorded critical path, profile and execution records first. Obtain
action dependency data for the tested configuration when a fuller graph would
change the decision. Bazel's
[`aquery`](https://bazel.build/query/aquery) exposes configured actions and their
input/output artifacts; use the repository's Bazel wrapper, the tested build
options and machine-readable output such as `--output=jsonproto`. Resolve artifact
and input-depset references to reconstruct producer/consumer edges, then join
recorded executions/profile events by their action identities or unambiguous
outputs and configuration. Keep cached and shared actions distinct from fresh work;
the configured graph alone has no measured durations. Account for shared actions
and unexpanded tree artifacts rather than inventing missing producers.

Target dependencies alone do not establish action timing;
overlapping timestamps or adjacent bars do not establish dependency edges. Record
which edges come from declared inputs, observed synchronization, propagated trace
context or inspected source. Retain unknown edges and missing phases explicitly.

For each material node, distinguish prerequisite completion, readiness/admission,
queueing, worker/input preparation, execution and output/finalization where those
clocks exist. Preserve physical retries and failure recovery as separate work.
Recompute which chain becomes limiting after a proposed change; inspect parallel
chains close enough to become the next long pole.

## Audit which changes trigger expensive work

For representative PR change classes, ask which tests should depend on the changed
behavior and which actually rerun. Include narrow frontend, backend, test-helper
and infrastructure changes where relevant. A broad integration suite may need a
dependency that a unit test does not; establish the assertions and real integration
boundaries before calling an edge unnecessary.

Keep these mechanisms separate:

| Layer                          | Evidence to inspect                                                                              | What a broad dependency can cause                                              |
| ------------------------------ | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| Workflow and target selection  | Changed paths, filters, full-sweep fallbacks, affected-target implementation and selected labels | Extra analysis/build work; selected tests can still cache-hit                  |
| Build and test action inputs   | Configured actions, runtime/runfiles closure, generated outputs and action/cache records         | Changed inputs invalidate a test even when its exercised behavior is unrelated |
| Retained fixture compatibility | Runner routing/recycling keys, fixture fingerprints, compatibility checks and reuse receipts     | A required test may unnecessarily rebuild infrastructure it could safely reuse |

Start from a changed input and show its dependency path to an expensive consumer.
Use `query` for declared paths and reverse dependencies; use `cquery` with the CI
configuration to check effective dependencies and runfiles. Loading-phase `query`
can overapproximate `select()` branches and includes build tools that are not test
runtime inputs. Inspect `aquery` to resolve action inputs, then join actual execution
records to distinguish fresh work from cache reuse. A `TestRunner` input may be an
aggregate runfiles tree: inspect its configured members before concluding that a
file is absent. Likewise, a renderer can rerun while emitting identical output,
allowing the downstream test to remain cached.

For example, these read-only queries locate consumers of the app frontend and
inspect selected tests' configured runtime inputs:

```bash
bbr query 'kind("py_test rule", rdeps(//agentplane/app/..., //agentplane/app/frontend/serving:static_image))'
bbr query 'somepath(//agentplane/app:test_bridge, //agentplane/app/frontend/serving:static_image)'
bbr cquery 'set(//agentplane/app:test_bridge //agentplane/app:test_settings //agentplane/app:test_thread_browser)' \
  --config=rbe --config=ci --output=starlark \
  '--starlark:expr=str(target.label) + " " + str([f.short_path for f in providers(target)["DefaultInfo"].default_runfiles.files.to_list() if "frontend/serving/static_image" in f.short_path])'
```

Choose consumers and input patterns for the inspected revision; the query is a
diagnostic, not an assertion that a particular dependency must remain present.
Follow every path that keeps an unwanted input reachable, including shared
`conftest` imports, optional fixture code, helper modules carrying OCI data, common
renderer executables and packaging targets. A harmless utility import can pull an
entire service or frontend image into unrelated tests. Read the current selection
driver before proposing affected-target selection: it may already exist while
global fallbacks or an overly broad target graph defeat it.

Prefer splitting code/data by the behavior or lifetime they own. Keep genuine
integration consumers attached to production artifacts; do not replace them with
mocks, delete assertions, hide imports or exclude tests just to narrow the graph.
For retained setup, separate baseline-mutating code from per-action orchestration
and VM-only extensions. Compatibility, identity, cleanliness and cleanup checks
must still reject incompatible or dirty state; removing hashes alone does not
establish safe reuse.

Validate a proposed split with a before/after change-to-test impact matrix: show
which unnecessary runtime inputs or invalidations disappear and which required
consumers remain. Use existing behavioral tests and meaningful compatibility
checks, not source-text or literal dependency-roster change detectors. Corroborate
with actual selected/executed/cached actions for representative changes when
measuring the result. Rank opportunities by how often the change class occurs,
avoidable work, required-gate critical path and next bottleneck. Removing a branch
that already finishes early may reduce compute without improving feedback time;
graph evidence alone does not quantify a CI speedup.

## Explain bubbles and opportunities for overlap

A quiet interval is a question about a particular resource or dependency chain,
not proof that the whole system is idle. Determine whether runnable work existed:

- **Dependencies not ready:** identify the blocking producer and what prevents it
  from completing. Inspect expensive inputs, late launches, unnecessary ordering
  and barriers that wait for a whole batch when a consumer needs only one result.
- **Ready but waiting:** distinguish executor/resource admission, worker queueing,
  locks, concurrency limits and scheduler dispatch. Measure readiness-to-start only
  when readiness is established; a queue timestamp is not automatically that clock.
- **Inside an action:** follow the state producer and observer through readiness
  checks, polling, backoff, service calls and subprocesses. Distinguish necessary
  work from delayed observation of work already completed.
- **Tail or drain:** inspect shard imbalance, serialized cleanup, outstanding child
  processes, exporter shutdown and output transfer. Establish completion and cleanup
  ownership before overlapping or shortening those phases.

For a sufficiently observed DAG, use prerequisite finish times and remaining
downstream work to identify critical paths and slack. State assumptions about
resource contention and observed scheduling; a dependency-only longest path is a
lower bound, not a forecast under fixed capacity. Do not manufacture slack or a
parallelization saving from incomplete edges. Explain whether changing a target
boundary, starting work earlier, removing a barrier, reusing setup or changing
placement removes work, overlaps it, or just transfers it to another resource.

## Use persisted traces, then instrument the missing phases

Read the component's tracing documentation and producer code. Retrieve existing
driver and service/process traces for the exact target, shard, run and physical
attempt. In Ducktape k3s tests, the
[phase-tracing contract](https://github.com/agentydragon/ducktape/blob/devel/agentplane/system_tests/README.md#phase-tracing)
describes `test.outputs/test-spans.jsonl` and
`test.outputs/process-spans.jsonl`. The second contains the service and native
harness work needed to explain many waits in the first. Historical artifacts can
have different names; list the actual invocation's outputs.

For long poles whose important phases are missing or too coarse, add or extend
OpenTelemetry spans in the owning test helpers, fixtures, services or subprocess
lifecycle. Use the existing tracer/export path rather than introducing stopwatch
logs or a parallel timeline format. Cover setup, readiness, useful work, diagnostics,
cleanup and shutdown where they explain the measured envelope; put child spans
around the operation or wait that needs attribution. Capture predicate transitions
and outcomes as events rather than emitting a span for every polling iteration.
Use stable stage names and bounded identifying attributes, without credentials or
request/model content.

Propagate context across async tasks, worker threads and process/service boundaries
where supported. Use span links or explicit operation identities for independently
started asynchronous work; identity correlation alone does not prove a dependency.
In Python tests, `util.testing.otel_tracing.configure_tracing` provides the existing
streaming JSONL export pattern. Reuse an already configured provider. Persist spans
to Bazel undeclared outputs and list/download them through the execution artifact
tooling. Account for batching, export/shutdown cost and unfinished spans lost on a
kill; missing spans do not prove no work happened.

Run the instrumented workload through its normal Bazel/RBE path, retrieve the new
artifacts, and continue the analysis. Record source and input identity, cache/reuse
state and physical attempt. Check that instrumentation preserves behavior and
does not introduce an export wait mistaken for application latency.

## Turn traces into an optimization and verify it

Reconstruct the span hierarchy, linked work and concurrent branches. Parentage
shows containment/context, not necessarily serial execution or a required ordering
between siblings. Compute exclusive time by subtracting the union of child
intervals, only where trace completeness and clocks support it; never subtract a
sum of overlapping children. Correlate across hosts with clock-skew limits.

Explain the longest causal chain and the material untraced envelopes before,
between and after spans. A readiness wait must lead to the producer's process trace
or other runtime evidence, or an explicit visibility boundary, not another
recommendation to "profile startup" while that evidence is available.
Use a real CPU/off-CPU profiler when spans locate a costly phase but cannot explain
its computation or blocking; OTel wall time alone is not CPU attribution.

Choose a concrete change supported by that mechanism: remove repeated work, overlap
independent stages, change batching/placement, shorten delayed observation, or
retain necessary work with an explanation. Test promising changes on equivalent
inputs and assertions, preserving fresh-state and cleanup guarantees. Compare the
changed phase, the complete action and required-gate critical path, including the
next bottleneck, resource use and failures. Do not add overlapping phase savings or
promote an ideal graph calculation into measured CI improvement. Retain the trace
artifacts and conclusions so later runs can revisit long poles that persist.
