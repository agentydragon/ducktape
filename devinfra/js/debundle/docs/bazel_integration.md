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

The rule declares `@ducktape//devinfra/js/debundle:ortools_cpsat_solver` as an
action tool and passes its execroot path to the debundler. The materializer uses
that OR-Tools CP-SAT sidecar for global selector assignment. Consumers can
override the solver tool with the matching label flag when needed.

## Solver sidecar build

`//devinfra/js/debundle/solver_backends/ortools_cpsat:selector_cpsat_solver`, the
default of the `ortools_cpsat_solver` label flag, is always built with
`--compilation_mode=opt`, whatever mode the invoking command selects. CP-SAT
compiled without `NDEBUG` runs extra checks on every solve and logs
`CP-SAT is running in debug mode`. On a 216-target request one `fastbuild`
presolve took 31 s regardless of `DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_MAX_TIME_SECONDS`,
while `opt` honored a 5 s limit and finished the whole localization in 83 s
(1 search worker, 4-core host, 2026-09-30).

`optimized_binary` re-exports the `cc_binary` `:selector_cpsat_solver_impl`
through a configuration transition, so OR-Tools, protobuf, Abseil and the solver
sources are all compiled `opt` (a whole-subtree switch: `NDEBUG`-dependent inline
code must not mix across libraries within one binary). Every consumer depends on
`:selector_cpsat_solver`, never the `_impl`: `debundle_pipeline`'s exec-config
tool attr, the `debundle` binary's `data`, and the Rust and e2e tests. The
re-export keeps the `selector_cpsat_solver` runfiles path that
`selector_runtime.rs` resolves.

`solver_test` links `:solver` in the test's own configuration, so its assertions
stay enabled. Deviation: a downstream repo that points `ortools_cpsat_solver` at
its own binary chooses that binary's build mode itself.

## Profiling

`debundle_pipeline` creates the normal pipeline target plus local profiling
sibling targets that reuse the exact same action command, inputs, package
roots, working directory, and debundler binary. For `name = "debundle"`:

- `:debundle`
- `:debundle_profile_time`
- `:debundle_profile_perf`
- `:debundle_profile_massif_heap`
- `:debundle_profile_heaptrack`

Profile actions are tagged `manual` and use local/no-remote/no-cache/no-sandbox
execution requirements. Build them with full output downloads when remote
execution is configured:

```sh
bazel build //path/to:debundle_profile_perf --remote_download_outputs=all
```

The standalone `perf_wrapper.sh` helper post-processes `perf` output for
ad-hoc local runs; its header lists the report files it writes and the `PERF_*`
knobs:

```sh
PERF_RECORD_FREQ=49 \
  devinfra/js/debundle/perf_wrapper.sh --output-dir /tmp/debundle-profile -- \
  <debundler> run <debundle args...>
```

Save important runs under the consuming repo's `debug/perf/` directory with the
captured command, stdout/stderr and profiler artifacts.
