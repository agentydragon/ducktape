# Bazel integration

`pipeline.bzl` provides a Bazel rule for running `debundle run` as a normal
build action:

```python
load("@ducktape//devinfra/js/debundle:pipeline.bzl", "debundle_pipeline")

debundle_pipeline(
    name = "debundle",
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
declares the spec, input data, package roots, and debundler binary as Bazel
inputs/tools, then runs the debundler from `BAZEL_BINDIR` so source-relative
spec paths resolve the same way they do in ordinary builds. By default the rule
uses `@ducktape//devinfra/js/debundle:debundle`; consumers can select a
different binary at repo or command-line scope with:

```sh
bazelisk build //path/to:debundle \
  --@ducktape//devinfra/js/debundle:debundler=@my_debundle_bin//file
```

A reading-only target sets `emit_on_rejection = True`: it passes
`--emit-on-rejection`, and the action succeeds on exit status 3 — the gate's
rejection is the only failure — as well as 0. Every other failure still fails
the build. A gate-rejected output is marked by `GATE_REJECTED` at its
`write_js_tree` root and is never a runnable bundle (<../SPEC.md> § Reading-only
output on gate rejection), so name the target for it, e.g. `debundle_readable`.

## Integration boundary

Corpus integrations use `pipeline.bzl`'s `debundle_pipeline`, the public
`:debundler` label flag, or the public `:debundle` executable.
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
