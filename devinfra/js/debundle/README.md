# Debundle

`debundle` is a JavaScript bundle restructuring tool. It reads a transform
spec, emits a decomposed module tree, and writes analysis artifacts that help
drive later module extraction and naming work.

## CLI

See `docs/cli.md` for the current command reference.
See `docs/guide.md` for step-by-step workflows.

Cheat sheet of the most-used commands:

- `debundle run` — execute the transform pipeline (parse + facts +
  owner_graph + realizability gate + lower + emit). Add `--dry-run`
  to run pipeline checks without writing emitted JS or reports.
- `debundle bindings assign <sym>:<module>[:<readable>]` — move a
  binding (single, multi-positional, or `--batch <file.json>`).
- `debundle bindings rename <original> <readable>` — rename without
  moving.
- `debundle modules propose` — factorizer-derived move proposals;
  `--source-root` annotates anonymous-statement addressability.
- `debundle modules merge --target <T> <sources...>` — splice module
  YAMLs.
- `debundle describe <id>` / `debundle show-source <id>` — graph +
  source context for any binding / module path or `logical:N` module id /
  atom / owner / proposal / diagnostic ID.
- `debundle bindings comment <sym>` / `debundle modules comment <module>` —
  edit `comment:` fields (see "Comments" below).

Mutating commands (`bindings assign`, `bindings rename`, `modules
merge`) run the realizability gate by default; `--dry-run` previews,
`--no-verify` skips. Read-only commands accept `--format
text|json|ndjson` (default `text` on tty, `json` on pipe).

Common arg paths accept env-var defaults (`DEBUNDLE_GRAPH`,
`DEBUNDLE_MODULES`, `DEBUNDLE_SOURCE_ROOT`, `DEBUNDLE_OUT`). Set them
once per session.

## Getting started

Run a tree-shaped authoring spec:

```sh
debundle run \
  --tree-config spec/spec_config.yaml \
  --tree-modules spec/modules \
  --tree-vendor-marks spec/sources/vendor/vendor_marks.yaml \
  --tree-source-root . \
  --out-root bazel-bin/example/debundle.out
```

(For other invocation shapes — flat spec, vendor package roots, etc. —
see `docs/cli.md`.)

## Bazel Integration

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

## Comments

Module YAMLs and `members:` entries may carry an optional
`comment:` field for reverse-engineering annotations. These fields
are authored once and emit into generated JS on every rebuild, so RE
notes survive `debundle run` invocations.

`anonymous_statements:` entries may also carry `comment:` or `note:`.
Both fields are preserved on round-trip. `comment:` emits into
generated JS immediately before the matched anonymous statement;
`note:` remains YAML metadata.

Debundle's output is meant to turn minified compiled chunks into nice
human-readable code. Treat emitted `comment:` text as part of that
readability surface: explain intent, invariants, and module
relationships. Provenance, owner IDs, and source-call trivia belong in
`note:` or should be omitted.

Use `note:` for scratch reverse-engineering notes that should survive
debundle edits but should not appear in generated JS, including
uncertainty, provenance, and call-site observations.

```yaml
# Module YAML
comment: |
  Coordinates foo registrations and lookup state.

members:
  - name: FooAccessor
    selector:
      binding: { name: a, kind: variable_declarator }
    comment: |
      Reads the active foo registry without mutating it.

anonymous_statements:
  - match: "Foo.prototype.bar = true;"
    comment: |
      Enables Foo.bar before consumers import Foo.
  - match: "registerFoo(foo);"
    note: "uncertain: looks like a registration side effect"
```

Edit module and member comments via the CLI (assumes
`DEBUNDLE_MODULES` is exported; pass `--modules <dir>` otherwise):

```sh
# Set a member's comment from a positional arg.
debundle bindings comment a "Accessor for foo state."

# Open $EDITOR pre-populated with the current comment.
debundle bindings comment a --edit

# Remove the comment entirely.
debundle bindings comment a --clear

# Read the current comment (plain text on tty, JSON on pipe).
debundle bindings comment a

# Same three modes for module-level comments.
debundle modules comment runtime/foo --edit
```

`bindings comment` accepts minified (`a`) or readable (`FooAccessor`)
names. `modules comment` takes the module path (`runtime/foo`)
relative to `$DEBUNDLE_MODULES`. Anonymous-statement comments are
authored directly in YAML.

CLI editing is live for module and member comments.

## Conditionally-correct optimizations

Some analyses in this crate are **conditionally correct**: they are sound
only when the input bundle avoids a small set of dynamic-dispatch shapes
that defeat static reasoning. Each such pass checks the precondition on
the statements it would fire on and falls back to a strictly-conservative
path when the check fails — see AGENTS.md → "Conditionally-correct
optimizations" for the soundness rule.

The first such pass is the dataflow-aware S-chain in `graph.rs`, opted
into per chunk via
`chunk_analysis_options.<chunk_id>.dataflow_aware_s_chain` in the spec.
It is sound when no top-level statement contains:

- direct `eval(...)` / `(0, eval)(...)`
- `with (obj) { ... }`
- `new Function(...)` / `Function(...)`
- computed-key `globalThis[<expr>]` / `window[<expr>]` / `self[<expr>]`
- `Object.defineProperty` / `Reflect.defineProperty` on a global
- `new Proxy(<global>, ...)`
- dynamic member-key reads/writes on outer-scope bindings the pass
  would otherwise track

Each impure top-level statement carries a `dataflow_summarizable` bit
(`facts/wire.rs`). Statements that fail the check fall back to the
strictly-conservative S-chain (every adjacent impure pair gets an
edge), so the optimization is safe to enable even on bundles that
mix audited and unaudited code — only the unsummarizable statements
pay the conservative cost.

See `docs/design.md` → "Emission modes" for the precise dataflow-aware
emission rule.
