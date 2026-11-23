# Bazel integration

## Explicit analysis and apply stages

Declare `debundle_analysis` once for one input bundle and one set of semantic
analysis premises. Each `debundle_apply` must name that target explicitly. There
is no combined rule or target-family macro that can silently start another
analysis when adding a spec variant.

```python
load("@ducktape//devinfra/js/debundle:pipeline.bzl", "debundle_analysis", "debundle_apply")

debundle_analysis(
    name = "analysis",
    analysis_threads = 2,
    input_data = ["//path/to:bundle_inputs"],
    package_roots = {"//:node_modules/react/dir": "react"},
    tree_source_root = "//path/to:bundle_inputs",
    tree_config = "spec/spec_config.yaml",
    tree_modules = "spec/modules",
    tree_vendor_marks = "spec/sources/vendor/vendor_marks.yaml",
    spec_tree_inputs = [":spec_data"],
)

debundle_apply(
    name = "debundle",
    analysis = ":analysis",
    input_data = ["//path/to:bundle_inputs"],
    package_roots = {"//:node_modules/react/dir": "react"},
    tree_source_root = "//path/to:bundle_inputs",
    tree_config = "spec/spec_config.yaml",
    tree_modules = "spec/modules",
    tree_vendor_marks = "spec/sources/vendor/vendor_marks.yaml",
    spec_tree_inputs = [":spec_data"],
)

debundle_apply(
    name = "debundle_passing",
    analysis = ":analysis",
    ownership_subset = "passing_subset.json",
    input_data = ["//path/to:bundle_inputs"],
    package_roots = {"//:node_modules/react/dir": "react"},
    tree_source_root = "//path/to:bundle_inputs",
    tree_config = "spec/spec_config.yaml",
    tree_modules = "spec/modules",
    tree_vendor_marks = "spec/sources/vendor/vendor_marks.yaml",
    spec_tree_inputs = [":spec_data"],
)

```

| Rule                | Registered actions                                                                            | Default output                                               |
| ------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `debundle_analysis` | `DebundleProjectAnalysis`, `DebundleAnalyze`                                                  | Canonical `.analysis_projection` and saved `.analysis` files |
| `debundle_apply`    | `DebundleApply`; `DebundleProjectApply` for a different spec; optional `DebundleExportGraphs` | `<name>.out/` with emitted JavaScript and bounded reports    |

`analysis` requires a direct `debundle_analysis` target with its typed provider;
a filegroup or arbitrary artifact is not accepted. Building the analysis target
never applies a spec or emits JavaScript. Its one projection action writes both
the analysis and apply projections from one parsed spec. Matching apply targets
reuse the latter; adding them registers no source-analysis projection or Analyze
action. The ordinary Bazel dependency graph makes every
consumer depend on the same saved artifact; caching and action deduplication need
no side cache or opt-in reuse flag.

A different module layout, rename, retained ownership subset, graph-only option,
or report sink can reuse analysis when its canonical semantic projection is
unchanged. A different bundle, vendor interpretation, host premise or
analysis-relevant annotation requires its own explicit analysis target. The native
engine still checks engine identity, source digests and the current spec's
projection before consuming saved facts. The same input file alone is not enough
to justify sharing facts computed under different assumptions. Never force a
shared target by weakening those checks.

The analysis target's `apply_projection` output group exposes its second
projection. An apply target reuses it when the flat spec path, or the tree
config, vendor marks, modules path, source root, and complete declared spec
input set are identical. The apply target's output root is excluded: tree
output paths are bound when the projection is consumed. Different source or
package input labels do not prevent projection sharing; each apply action still
declares its own source/package inputs and checks native analysis compatibility.
A distinct spec keeps its own `DebundleProjectApply` action, even when its
semantic analysis is compatible. `DebundleProjectApply` compiles the typed spec
without parsing JavaScript. The canonical apply projection
canonical output omits YAML comments, whitespace, and module-level `note` only.
The projection action reads the flat spec or tree config, vendor marks, and
declared module YAML files; source and package paths are serialized as paths,
without opening their contents. Package-root arguments are used by analysis
and apply, so they stay in those actions' input closures. The combined producer
also reads source/package inputs to calculate the analysis projection. Thus a
source or package edit may rerun both projections even if the apply projection
bytes are unchanged; `DebundleAnalyze` and each apply action retain their
separate input keys and cache behavior.
`DebundleApply` reads that projection, saved analysis, source/package inputs, and
the optional ownership subset. Metadata-only changes that preserve the projection
therefore reuse its cached output. Emitted `comment`, report notes, and
conflict-relevant annotation notes remain inputs. Paths under the action execroot
are stored relative to it and restored under the consumer's execroot. For tree
specs, generated output paths are stored relative to an output-root placeholder
and bound to each apply target's `--out-root`; matching authored YAML therefore
produces identical projection bytes across targets with different output roots.
Flat specs keep their authored output paths.

`apply_projection` exposes this canonical input. The hidden
`spec project-apply` and `run --apply-projection` commands are an internal Bazel
action interface. The projection is the authoritative spec; context flags identify
report locations and the tree output root, and do not check current YAML freshness. Bazel's declared
producer inputs enforce that freshness. Native runs should use ordinary `run`
with authored YAML. Full graphs refuse projected input and use the original YAML
capture, including exact bytes and directory membership.

Full exported dependency graphs belong to apply: they include authored graph
options and module ownership. They consume the saved source facts and do not
repeat interpreter analysis. Keep source-fact computation and graph export
distinct when reading `aquery` or requesting outputs.

The analysis and apply rules declare their complete source and package inputs.
A tree spec uses `tree_config`, `tree_modules`, `tree_vendor_marks`, and
`spec_tree_inputs`; config and vendor-mark labels can name generated files, and
`tree_modules` is a package-relative directory. A flat YAML uses `spec` instead.
`tree_source_root` is the target holding the bundle: its files' root determines
whether embedded source paths resolve against the execroot or `bazel-bin`,
including bundles extracted from pinned upstream artifacts. A separate
`DebundleProjectApply` action, when needed for a distinct spec, depends on spec
authoring files only; the real apply action retains source and package inputs.

`analysis_threads` belongs only to `debundle_analysis`: zero uses the inherited
Rayon setting, a positive value sets the tracked `RAYON_NUM_THREADS` environment
on Analyze only, and negative values fail. The analysis rule owns the `analyzer`
and `projector` tool attributes; apply owns `debundler` and all emission/report
controls. Label flags select the source-built tools in the execution
configuration. The analyzer and CLI must have the same configured analysis
identity, derived from the complete `staged_analysis` closure. Edits confined to
assignment, lowering or the CLI do not invalidate that identity; shared analysis
dependencies remain conservative invalidation boundaries.

To check a many-spec integration, query its actual actions:

```sh
bazelisk aquery 'mnemonic("DebundleAnalyze", deps(set(//path/to:debundle //path/to:debundle_passing)))'
```

There should be one Analyze action per deliberately distinct semantic analysis,
regardless of the number of apply targets. Rule tests also execute two different
spec layouts over one shared analyzed bundle and check both emitted behavior and
module boundaries.

## Output groups and gate rejection

A reading-only target sets `emit_on_rejection = True`: it passes
`--emit-on-rejection`, and the action succeeds on exit status 3 — the gate's
rejection is the only failure — with a `GATE_REJECTED` marker, as well as 0.
Every other failure still fails
the build. A gate-rejected output is marked by `GATE_REJECTED` at its
`write_js_tree` root and is never a runnable bundle (<../SPEC.md> § Reading-only
output on gate rejection); name such targets `<name>_readable`.

Default builds write small reports and emitted modules without full graph
serialization, including when `split_dependency_graphs = True`. That attr
registers a separate `DebundleExportGraphs` action and the `dependency_graphs`
output group (`--output_groups=+dependency_graphs`). The action consumes the
same projection/Analyze artifact as the normal pipeline, with the same source,
spec and census inputs; it does not rerun interpreter/fact analysis. Its
`<target>.dependency_graphs/reports/tree/` contains full graphs and run diagnostics.
It uses `--emit-on-rejection` only with `--reports-only`, so a typed gate-only
rejection still lets the action finish exporting every configured chunk. The
reports-only marker is retained, no JavaScript is emitted, and other failures
still fail the action. Exact gate counts remain in each `graph_summary.json`;
the detailed rejection report follows the materializer's existing selection.
Graph selection does not enable full effect-conflict JSONL or other full-detail
evidence. The default output group never depends on this export action.

A filegroup can select that group for graph refresh/query work; normal
validation and source regeneration remain small. Gate-rejected reading trees
retain their `GATE_REJECTED` marker and exit-3 contract. Without an exported
graph their diagnostics explain how to request one instead of naming a
nonexistent graph file.

### Graph command launchers

Use `debundle_cli.bzl` to run graph-backed commands with the graph output and
relocation roots in runfiles (the family declares these as `<name>_cli*`):

```python
load("@ducktape//devinfra/js/debundle:debundle_cli.bzl", "debundle_cli")

debundle_cli(
    name = "inspect_graph",
    debundle = ":debundle",
    graph_relpath = "reports/tree/main/dependency_graph.json.gz",
    modules_path = "spec/modules",
    # Relative to graph_source_data when that tree is set.
    source_root = "inputs",
    spec_root = "spec",
    graph_source_data = ":relocatable_inputs",
    package_roots = {
        "//:node_modules/example_package/dir": "example_package",
    },
)
```

`debundle` must expose the `dependency_graphs` output group, normally by setting
`split_dependency_graphs = True`. `graph_relpath` is within that tree.
`modules_path` and `spec_root` are workspace-relative. Set either
`source_root` or `source_data`: `source_root` is workspace-relative unless
`graph_source_data` is provided, while `source_data` is a generated directory
used directly as `DEBUNDLE_SOURCE_ROOT`. Each generated source, graph-source,
and package root must provide exactly one directory artifact. `package_roots`
uses the same label-to-package-name mapping as `debundle_apply`.
The default `debundler` is the public label flag used by the pipeline, so a
command-line override selects the same executable. If the pipeline target sets
its `debundler` attribute directly, pass that same label to `debundle_cli`.
Set `testonly = True` when the runner depends on test-only graph or root targets.

Run a command by appending it after `bazel run`'s `--` separator:

```sh
bazel run //path/to:inspect_graph -- graph-summary --format json
```

The runner uses `rules_shell`'s Bash launcher and `rlocation` for both
`@ducktape` tools and generated trees, so the same macro works from a consuming
workspace.

The `native_inputs` output group contains the exact rule inputs needed for a
native replay: the flat spec or complete tree-shaped spec inputs, `input_data`,
`tree_source_root`, and every `package_roots` target's `DefaultInfo` files, plus
`ownership_subset` when set. Request it alone to materialize source, package,
and spec artifacts without selecting analysis or pipeline actions:

```sh
bazelisk build //path/to:debundle \
  --output_groups=native_inputs \
  --remote_download_outputs=all
```

This can still run actions that produce source or package inputs. `native_inputs`
does not select `DebundleProjectAnalysis`, `DebundleAnalyze`,
`DebundleProjectApply`, or `DebundleApply`. The `analysis` group includes the projection and analysis
artifact and runs Analyze; `dependency_graphs` also depends on that shared
analysis artifact. `debundler` contains only the exact full CLI selected by the
rule's `debundler` attribute or label flag. `projector` contains only the exact
projection executable selected by the analysis target's `projector` attribute or label flag.

For a complete native replay packet containing inputs, analysis artifacts, and
both action executables, request all four groups:

```sh
bazelisk build //path/to:debundle \
  --output_groups=analysis,debundler,projector,native_inputs \
  --remote_download_outputs=all
```

This runs Analyze but not the normal Pipeline action. `run --analysis` still
checks the engine, source digests, and canonical projection. Preserve those
checks during replay, and point `--tree-source-root` and every `--package-root`
at the valid roots in the relocated input tree.

## Integration boundary

Corpus integrations use `pipeline.bzl`'s explicit `debundle_analysis` and
`debundle_apply` rules, `debundle_cli.bzl`, the public
`:debundler`, `:analyzer`, and `:projector` label flags, or the public CLI
executables.
`README.md`, `SPEC.md`, and `perf_wrapper.sh` are exported resources.

The Rust libraries in the root BUILD are internal to this subtree (including
`e2e/`). They are dependency boundaries, not a supported Rust SDK. A corpus
should not import selector, graph, or lowering internals to duplicate part of
`debundle run`; extend the CLI or pipeline rule when an integration needs a new
capability. Diagnostic binaries can still be run directly as Bazel targets.

## Standalone CLI on captured bundles

Build the native CLI optimized for large captured programs and contract audits:

```sh
bbr build -c opt //devinfra/js/debundle:debundle
```

The Rust executable follows the requested Bazel compilation mode. The optimized
solver subtree described below does not optimize the surrounding Rust pipeline;
`fastbuild` runs can spend minutes inside a full audit baseline. Use the
optimized executable when running the CLI outside a Bazel pipeline action.

Stage the project's generated source/vendor targets and package-root mappings
through its [adapter](../skills/shared/workflow.md#adapter-contract). Source-only
validation checks selectors; full runs and audit baselines also resolve vendor
and package inputs. For finer baseline phase markers, set
`DUCKTAPE_DEBUNDLE_PROGRESS=1` on the standalone invocation.

### Preserve the pipeline CLI from RBE

For saved-graph queries, retain the exact full CLI built in the pipeline's
execution configuration. Its completed compile action is available while
Analyze is still running. Substitute the task's scratch directory for `/tmp/TASK`:

```sh
bbapi execution INVOCATION --json > /tmp/TASK/executions.json
bb execution get EXECUTION_ID --output=json > /tmp/TASK/cli-action.json
bbapi blob download --manifest /tmp/TASK/downloads.json
```

Use the pipeline's `DebundleApply` command to identify its full CLI tool
path, then choose the completed `Rustc` execution with that exact
`primaryOutputPath`, ending in `/devinfra/js/debundle/debundle`. This matters
when the invocation builds several configurations. In its response,
`result.outputFiles` gives the executable's path and
`digest.hash` / `digest.sizeBytes`. Use those values
in the download manifest:

```json
[
  {
    "uri": "bytestream://remote.buildbuddy.io/blobs/HASH/SIZE",
    "destination": "/tmp/TASK/debundle-cli",
    "invocation_id": "INVOCATION"
  }
]
```

Verify the downloaded size and SHA-256, then restore executable permission.
Keep the binary, selected digest and invocation receipt with the matching
analysis and complete graph-report tree, including the sibling program graph.
Native queries still verify the graph's CLI and source/spec provenance. A
local `bazel-bin` executable can belong to another configuration or earlier
build; its presence alone establishes no match. Execution command snippets can
be truncated; do not replay them as saved commands. Keep raw action responses
and BEP data in scratch; record selected identities and results in Git.

For complete arguments, take `actionDigest` from the execution listing and
download it with `bb download HASH/SIZE --type=Action`. Its `commandDigest`
identifies the `bb download HASH/SIZE --type=Command` record, whose `arguments`
include any shell wrapper. Keep environment records in scratch.

A live invocation's artifact index can omit completed action outputs, and local
declared output directories can still be empty. To inspect one completed
analysis before the invocation finishes, download its `result.outputFiles`
artifact and reuse it with the matching CLI's `run --analysis ... --reports-only
...`. Before native replay, check that every supplied package-root directory
and generated source file is materialized and readable. `cquery --output=files`
lists paths but does not run their producing actions or materialize their
outputs. Native `analyze` and `run --analysis` need the same complete
source/package closure supplied through `input_data`, `tree_source_root`, and
every `package_roots` target. `run` also needs its matching spec inputs. The
`native_inputs` group collects this closure; the complete replay command above
also requests the matching analysis artifact and CLI. The local Bazel output
directory's presence does not prove its inputs have been downloaded.

When serializing a warm Bazel client with a file lock, invoke it through
`flock --close -x <lock-file> <bazel-command>`. `--close` keeps the persistent
Bazel server from inheriting the lock descriptor. A lock wait shows contention,
not that the Analyze action is active; inspect the invocation's action progress
before treating it as ongoing analysis.

## Solver build

The debundler links OR-Tools' CP-SAT for selector assignment, so its binary is
the action's only tool. The solver library, `//devinfra/js/debundle:ortools_cp_solver`,
is always built with `--compilation_mode=opt`: `optimized_cc_library.bzl` applies
a configuration transition to it and its whole dependency subtree (OR-Tools,
protobuf, Abseil), because `NDEBUG`-dependent inline code must not mix across the
libraries of one binary. Every consumer links it through that target, in
whatever mode it builds; linking `@or-tools//ortools/sat/c_api:cp_solver_c`
directly gives a debug-mode solver, which logs `CP-SAT is running in debug mode`.
<selector_resolution.md> § The solver has the rest.

Deviation: a consumer that replaces `debundler` runs that binary's own solver.

## Profiling

`debundle_apply` has no profiling targets: `perf` needs the host kernel and
massif/heaptrack need their own binaries on `PATH`, so sandboxed profile actions
produced empty or misleading output. Run an `-c opt` debundler binary under
`perf_wrapper.sh` directly. It writes the reports next to a rerunnable command
stub; its header lists the report files and the `PERF_*` knobs:

```sh
PERF_RECORD_FREQ=49 \
  devinfra/js/debundle/perf_wrapper.sh --output-dir /tmp/debundle-profile -- \
  <debundler> run <debundle args...>
```

A rejected gate can still produce a complete profile. The wrapper generates
diagnostic reports after the recorded run and preserves its nonzero exit
status; inspect the artifacts even when the command fails.

Run one `perf record` session per host at a time: concurrent unprivileged
sessions exhaust `perf_event_mlock_kb`. Deep inference recursion can outrun the
default `PERF_CALL_GRAPH=dwarf,8192` stack copy: 21% of the samples in one large
web-corpus profile had no engine frames. They still count in
`perf_report_flat_symbols.txt`; attribute by family only from samples that have
engine frames. Raise the per-report `PERF_POSTPROCESS_TIMEOUT` (default `120s`)
for large DWARF profiles; a 25,000-sample profile ran with `7200s`.

Save important runs under the consuming repo's `debug/perf/` directory with the
captured command, stdout/stderr and profiler artifacts.

## Public performance workflow

Run the public Excalidraw workload directly with Bazel:

```sh
bbr test -c opt --nocache_test_results --remote_build_event_upload=all \
  --runs_per_test=3 \
  //devinfra/js/debundle/perf:excalidraw_pipeline_bench
```

The test executes fresh project, analyze and apply commands. GNU `time -v`
output and each command's stdout/stderr go to `TEST_UNDECLARED_OUTPUTS_DIR`
for attachment to the BuildBuddy test invocation. Inputs are read directly from
Bazel runfiles; intermediate pipeline outputs stay in `TEST_TMPDIR`. The benchmark
does not produce aggregate reports or automatic revision comparisons.

For a raw Analyze CPU profile or heap allocation profile, run the benchmark
separately with `DEBUNDLE_BENCH_PROFILE=cpu` or `heap`. Profiling builds use the
same optimized settings plus Rust line tables and frame pointers. The workflow
sets these flags automatically. Rust normally strips debug information in opt
mode independently of Bazel's `--strip` setting, so both strip flags are needed.
For a direct Bazel invocation add:

```sh
--@rules_rust//rust/settings:extra_rustc_flag=-Cdebuginfo=1 \
--@rules_rust//rust/settings:extra_rustc_flag=-Cstrip=none \
--@rules_rust//rust/settings:extra_rustc_flag=-Cforce-frame-pointers=yes \
--strip=never --test_env=DEBUNDLE_BENCH_PROFILE=cpu
```

The CPU run writes `analyze/perf.data`; the heap run writes
`analyze/heaptrack.raw.zst` (or `.raw.gz`). Each also attaches the matching
`analyze/debundle_analyze` executable, including its debug information. To
symbolize the raw allocation trace, make that executable available at its
recorded path before running `heaptrack --interpret`; use the exact executable
for CPU symbols as well. Run each mode separately: Heaptrack changes CPU costs,
and `perf` should not be wrapped around it. Perf compresses its records inside
`perf.data`; Heaptrack compresses its raw trace as it writes. Use `none` for
native timing. Both profiled modes also pass `--metrics-file` to the project,
analyze and apply commands. Their raw OpenTelemetry JSONL files are written as
`project/metrics.jsonl`, `analyze/metrics.jsonl` and `apply/metrics.jsonl` beside
the corresponding phase logs. The benchmark sets the standard
`OTEL_METRIC_EXPORT_INTERVAL=1000` environment variable so short phases flush
before exit; it performs no metrics postprocessing.

The Excalidraw pipeline benchmark runs locally on the outer BuildBuddy runner,
whose pinned image supplies the profiling tools. Tool builds and the synthetic
benchmark tests use RBE. Each native command has a timeout, a 16 GiB per-process
address-space limit and a 512 MiB per-file limit.

The optional
[Debundle performance workflow](https://github.com/agentydragon/ducktape/actions/workflows/debundle-performance.yml)
selects targets and runs the same Bazel test command. `suite=excalidraw` selects
Excalidraw, `suite=bench` selects the synthetic `e2e/bench/` tests, and
`suite=all` queries all Debundle tests tagged `bench`. Explicit query results
include manual tests, which wildcard test expansion would omit.
`additional_test_flags` passes standard Bazel arguments, including
`--runs_per_test` and `--test_timeout`. Scheduled runs select `all` with
`--runs_per_test=3`.

Find these invocations in BuildBuddy's build history using the `debundle-bench`
tag. Additional tags identify `suite=excalidraw|bench|all` and
`profile=none|cpu|heap`. The runner uses additive `TAG_` build metadata,
preserving existing session tags.

```sh
bbapi invocation list --tag debundle-bench
```

```sh
gh workflow run debundle-performance.yml --ref devel \
  -f candidate_ref="$revision" -f suite=excalidraw \
  -f additional_test_flags=--runs_per_test=3 -f profile=none
```

Use the BuildBuddy invocation's native tool outputs for subsequent analysis.
Run each revision separately when comparing changes.
