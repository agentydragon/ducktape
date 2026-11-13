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

| Rule                | Registered actions                                         | Default output                                               |
| ------------------- | ---------------------------------------------------------- | ------------------------------------------------------------ |
| `debundle_analysis` | `DebundleProjectAnalysis`, `DebundleAnalyze`               | Canonical `.analysis_projection` and saved `.analysis` files |
| `debundle_apply`    | `DebundleApply`; optional on-demand `DebundleExportGraphs` | `<name>.out/` with emitted JavaScript and bounded reports    |

`analysis` requires a direct `debundle_analysis` target with its typed provider;
a filegroup or arbitrary artifact is not accepted. Building the analysis target
never applies a spec or emits JavaScript. Adding apply targets never registers
projection or Analyze actions. The ordinary Bazel dependency graph makes every
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

Full exported dependency graphs belong to apply: they include authored graph
options and module ownership. They consume the saved source facts and do not
repeat interpreter analysis. Keep source-fact computation and graph export
distinct when reading `aquery` or requesting outputs.

Both rules declare their complete spec, input data and package roots. A tree spec
uses `tree_config`, `tree_modules`, `tree_vendor_marks`, and `spec_tree_inputs`.
Config and vendor-mark labels can name generated files; `tree_modules` is a
package-relative directory. A flat YAML uses `spec` instead. `tree_source_root`
is the target holding the bundle: its files' root determines whether embedded
source paths resolve against the execroot or `bazel-bin`, including bundles
extracted from pinned upstream artifacts.

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
does not select `DebundleProjectAnalysis`, `DebundleAnalyze`, or
`DebundleApply`. The `analysis` group includes the projection and analysis
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

Save important runs under the consuming repo's `debug/perf/` directory with the
captured command, stdout/stderr and profiler artifacts.
