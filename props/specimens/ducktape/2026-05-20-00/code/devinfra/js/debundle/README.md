# Debundle

`debundle` is a JavaScript bundle restructuring tool. It reads a transform
spec, emits a decomposed module tree, and writes analysis artifacts that help
drive later module peel and naming work.

The command has two public surfaces:

- `debundle run`: execute the transform pipeline.
- `debundle peel`: query generated owner graphs and spec modules for planning
  module extraction work.

## CLI

Run a flat spec:

```sh
debundle run --spec transform-spec.yaml --force
```

Run a tree-shaped authoring spec:

```sh
debundle run \
  --tree-config spec/spec_config.yaml \
  --tree-modules spec/modules \
  --tree-vendor-marks spec/sources/vendor/vendor_marks.yaml \
  --tree-source-root . \
  --out-root bazel-bin/example/debundle.out \
  --force
```

Vendor-package source lookup can be supplied either as repeated explicit roots:

```sh
debundle run ... \
  --package-root react=/path/to/node_modules/react \
  --package-root zod=/path/to/node_modules/zod
```

or as a package tree:

```sh
debundle run ... --packages-root /path/to/node_modules
```

The package-tree form resolves package names as paths under `node_modules`,
including scoped names such as `@scope/pkg`.

## Bazel Integration

`pipeline.bzl` provides a Bazel rule for running `debundle run` as a normal
build action:

```python
load("@ducktape//devinfra/js/debundle:pipeline.bzl", "debundle_pipeline")

debundle_pipeline(
    name = "debundle",
    debundler = "@ducktape//devinfra/js/debundle:debundle",
    force = True,
    input_data = [
        "//path/to:bundle_inputs",
    ],
    package_roots = {
        "//:node_modules/react/dir": "react",
        "//:node_modules/zod/dir": "zod",
    },
    spec_tree_inputs = [":spec_data"],
    tree_config = "spec/spec_config.yaml",
    tree_modules = "spec/modules",
    tree_vendor_marks = "spec/sources/vendor/vendor_marks.yaml",
)
```

The rule writes a tree artifact named `<target>.out` under `bazel-bin`. It
declares the spec, input data, package roots, and debundler binary as Bazel
inputs/tools, then runs the debundler from `BAZEL_BINDIR` so source-relative
spec paths resolve the same way they do in ordinary builds.

## Profiling Actions

For recurring performance work, prefer `debundle_pipeline_with_profiles`.
It creates the normal pipeline target plus local profiling sibling targets that
reuse the exact same action command, inputs, package roots, working directory,
and debundler binary.

```python
load(
    "@ducktape//devinfra/js/debundle:pipeline.bzl",
    "debundle_pipeline_with_profiles",
)

debundle_pipeline_with_profiles(
    name = "debundle",
    debundler = "@ducktape//devinfra/js/debundle:debundle",
    # Same attrs as debundle_pipeline.
)
```

Generated targets:

- `:debundle`
- `:debundle_profile_time`
- `:debundle_profile_perf`
- `:debundle_profile_massif_heap`
- `:debundle_profile_heaptrack`

Profile actions are tagged `manual` and use local/no-remote/no-cache/no-sandbox
execution requirements. Build them with full output downloads when remote
execution is configured:

```sh
nix develop --command bazelisk build //path/to:debundle_profile_perf \
  --config=nolint \
  --remote_download_outputs=all
```

Each profile target writes a `<target>.profile` tree artifact. Common files:

- `command.sh`: replayable command with the Bazel action cwd and argv.
- `stdout.txt`: debundler stage timings.
- `debundle.out/`: the debundle output tree produced by the profiled run.

Mode-specific files:

- `time`: `stderr_time.txt` from `/usr/bin/time -v`.
- `perf`: `perf.data`, `perf_report_children.txt`,
  `perf_report_no_children.txt`, `perf_script_stacks.txt`, `perf_header.txt`,
  and `perf_evlist.txt`.
- `massif_heap`: `massif_heap.out`, `massif_heap_stderr.txt`, and
  `ms_print_heap.txt` when `ms_print` is available.
- `heaptrack`: `heaptrack*`, `heaptrack_stderr.txt`, and
  `heaptrack_print.txt` when `heaptrack_print` is available.

Save important runs before cleaning Bazel outputs:

```sh
mkdir -p debug/perf/YYYY-MM-DD-<short-name>
cp -a bazel-bin/path/to/debundle_profile_perf.profile/. \
  debug/perf/YYYY-MM-DD-<short-name>/
```

If `perf` is blocked by host kernel settings, use `time`, `massif_heap`, or
`heaptrack` first and rerun `perf` on a host where userspace sampling is
available.

## Peel Queries

Generated owner graphs can be queried with `debundle peel`:

```sh
debundle peel plan-work --graph "$GRAPH" --modules "$MODULES" --limit 25
debundle peel patch-status --graph "$GRAPH" --modules "$MODULES" --limit 50
debundle peel candidates --graph "$GRAPH" --modules "$MODULES" --limit 100
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --proposal-id <id>
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --binding-id <binding>
debundle peel explain --graph "$GRAPH" --modules "$MODULES" --owner-id <owner>
debundle peel source-slice --graph "$GRAPH" --modules "$MODULES" \
  --proposal-id <id> --source-root "$SOURCE_ROOT" --context-lines 40
```

`explain` and `source-slice` select exactly one object with `--proposal-id`,
`--owner-id`, or `--binding-id`; there is no `--binding` shorthand.

Typical adapter bindings:

```sh
GRAPH=<debundle-output>/analysis/logical_modules/.../owner_graph.json
MODULES=<spec-root>/modules
SOURCE_ROOT=<upstream-or-emitted-js-root>
DEBUNDLE_OUT=<debundle-output-root>
```
