# Bazel integration

`pipeline.bzl` provides a Bazel rule for running `debundle run` as a normal
build action:

```python
load("@ducktape//devinfra/js/debundle:pipeline.bzl", "debundle_pipeline")

debundle_pipeline(
    name = "debundle",
    # Limit Rayon workers for this target's Analyze action; 0 keeps the
    # inherited RAYON_NUM_THREADS or Rayon's default.
    analysis_threads = 2,
    input_data = [
        "//path/to:bundle_inputs",
    ],
    package_roots = {
        "//:node_modules/react/dir": "react",
        "//:node_modules/zod/dir": "zod",
    },
    spec_tree_inputs = [":spec_data"],
    tree_config = "spec/spec_config.yaml",
    # Target holding the chunk the spec reads. Its files' own root becomes
    # the root `inputs.root` / `inputs.js_list_path` resolve against, so a
    # committed chunk resolves against the execroot and a build-extracted
    # one against bazel-bin -- no need to vendor the chunk into git.
    tree_source_root = "//path/to:bundle_inputs",
    tree_modules = "spec/modules",
    tree_vendor_marks = "spec/sources/vendor/vendor_marks.yaml",
)
```

The rule writes a tree artifact named `<target>.out` under `bazel-bin`. It
declares the spec, input data, package roots, and action tools as Bazel
inputs/tools, then runs them from `BAZEL_BINDIR` so source-relative spec paths
resolve the same way they do in ordinary builds. By default the rule uses
`@ducktape//devinfra/js/debundle:debundle` for the full pipeline and
`:debundle_project_analysis` for projection; consumers can select another full
CLI at repo or command-line scope with:

```sh
bazelisk build //path/to:debundle \
  --@ducktape//devinfra/js/debundle:debundler=@my_debundle_bin//file
```

`analysis_threads` controls only `DebundleAnalyze`: zero omits the override,
using inherited `RAYON_NUM_THREADS` when set or Rayon's default otherwise. A
positive value sets the tracked action environment `RAYON_NUM_THREADS` to that
value. Negative values fail during rule analysis. Projection, Pipeline, and
graph export keep their existing action environments.

`DebundleAnalyze` uses the separate `:debundle_analyze` executable, selected by
the `:analyzer` label flag or the rule's `analyzer` attribute. It accepts the
`analyze` command's options directly. `DebundleProjectAnalysis` uses the small
`:debundle_project_analysis` executable, selected by the `:projector` label flag
or the rule's `projector` attribute. It accepts the transform inputs and
`--out` directly, and calls the same `pipeline::project_analysis_cli` function
as `debundle spec project-analysis`. Only the Pipeline and optional graph-export
actions use the full CLI.
The analyzer depends on `:staged_analysis`, independently of `:pipeline`,
`:lowering` and `:cli`. Edits confined to those latter crates leave the Analyze
tool and its compiled identity unchanged. Source inputs and projection bytes
must also be unchanged to reuse its action. Shared dependencies, including
artifact and vendor code, remain conservative invalidation boundaries.

Custom CLI/analyzer pairs must embed the same generated configured analysis
identity, generated from the complete `:staged_analysis` closure; mismatches fail
before decoding facts. Both tools use the execution
configuration. Their native linking policy must match the shared identity rule;
binary-specific linking overrides are outside this pairing contract. The
identity includes Rust compiled outputs, build-script outputs and linker flags,
selected native libraries, and Rust/C++ toolchain files and link configuration.
It identifies this compiled closure, not runtime executable bytes or external
system libraries. The standalone public CLI still analyzes in process and needs
no companion, including after Nix rewrites its ELF interpreter. Saved graphs
retain their exact full-CLI executable digest and source/spec checks.

The analysis header uses `analysis_engine_sha256`; earlier artifacts with
`engine_sha256` must be regenerated. Source digests and canonical projection
checks remain mandatory.

A reading-only target sets `emit_on_rejection = True`: it passes
`--emit-on-rejection`, and the action succeeds on exit status 3 — the gate's
rejection is the only failure — as well as 0. Every other failure still fails
the build. A gate-rejected output is marked by `GATE_REJECTED` at its
`write_js_tree` root and is never a runnable bundle (<../SPEC.md> § Reading-only
output on gate rejection), so name the target for it, e.g. `debundle_readable`.

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

A graph-consuming filegroup selects `output_group = "dependency_graphs"`.
Adapters should request that group only for graph refresh/query work; normal
validation and source regeneration remain small. Gate-rejected reading trees
retain their `GATE_REJECTED` marker and exit-3 contract. Without an exported
graph their diagnostics explain how to request one instead of naming a
nonexistent graph file.

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
`DebundlePipeline`. The `analysis` group includes the projection and analysis
artifact and runs Analyze; `dependency_graphs` also depends on that shared
analysis artifact. `debundler` contains only the exact full CLI selected by the
rule's `debundler` attribute or label flag. `projector` contains only the exact
projection executable selected by its `projector` attribute or label flag.

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

Corpus integrations use `pipeline.bzl`'s `debundle_pipeline`, the public
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

Use the pipeline's `DebundlePipeline` command to identify its full CLI tool
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

`debundle_pipeline` has no profiling targets: `perf` needs the host kernel and
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
