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
