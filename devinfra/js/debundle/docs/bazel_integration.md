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
bazel build //path/to:debundle \
  --@ducktape//devinfra/js/debundle:debundler=@my_debundle_bin//file
```

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

Save important runs under the consuming repo's `debug/perf/` directory with the
captured command, stdout/stderr and profiler artifacts.
