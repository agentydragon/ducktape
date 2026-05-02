# Debundler Implementation Constraints

## AST Requirement

JavaScript-source transformations must use proper AST operations on the
SWC-parsed input. Do not use raw text rewrites, string scanning, regex
rewriting, ad hoc source patching, or other text-based mutation as a
substitute for AST transformations.

## Working Rule

If a proposed change improves a test result without improving real
correctness, do not make that change. If the easiest fix is not the deepest
correct fix, do the deeper correct fix or stop and explain the blocker.

## Testing Philosophy

**Default to end-to-end tests that drive the real pipeline.** A debundler
bug almost always manifests as something an outside observer can see — the
emitted JS is not executable, an expected exported symbol is missing, the
extracted module shape is wrong, runtime behavior differs from the input
bundle. Reach for the e2e harness in `e2e/` (the `debundle` CLI invoked
through `support.rs`) before reaching into internals.

This applies even when you're chasing a bug or feature that lives in one
internal stage. If the symptom is observable in the unbundled output —
exported symbol set, runtime behavior, file layout, source shape — write
the regression as an e2e test driving the real CLI. Do not mock the
pipeline and do not construct internal types directly when an input
fixture reaches the same code path.

### Assertion helpers

`e2e/support.rs` provides primitives like:

- `assert_module_exports(out_root, "foo/bar.js", &["abc"], &[])` — assert
  module exports include `abc` and exclude listed names.
- `assert_module_source(out_root, "foo/bar.js", &["needle"], &["antineedle"])`
  — substring-match against the emitted source. Useful for shape checks
  ("`class A` appears", "no `__dt_generated_init__` wrapper").
- `assert_entry_output(fixture, "expected stdout\n")` — runs the entire
  emitted tree under node and asserts the bundle's runtime behavior is
  preserved.
- `assert_generated_module_after_entry_script(...)` — runs a probe script
  after the emitted entry has executed; useful for asserting on lazy-loaded
  modules' runtime shape.

Add new helpers when you find yourself repeating an assertion shape across
tests. A test that says "this file exports `abc` as a function and that
file does not, and the whole tree still runs and prints `X`" should be one
or two helper calls, not a wall of `fs::read` and substring matches.

### When unit tests are appropriate

A small number of focused unit tests reaching into a specific module are
fine — combinatorial corner cases for a pure function (e.g. path
resolution), or invariants that are awkward to expose via the CLI.
**They should be the minority.** If a behavior or bug can be demonstrated
by checking the shape of debundled output, the test belongs in `e2e/`.

### Forbidden test shapes

- Mocking the pipeline or any of its stages.
- Constructing `JsPipelineArtifact`, `ChunkManifest`, or other intermediate
  pipeline types by hand to drive a stage in isolation, when feeding an
  input fixture through the real pipeline reaches the same code path.

## Native Rust Shapes

Internal pipeline types are pure Rust. Stringly-typed `node_type` fields,
`Vec<serde_json::Value>` payloads with known shape, and `Map<String,
Value>` blobs that mirror an external JSON shape are smells — replace
them with typed structs and enums.

- Use Rust enums (frequently a thin wrapper over an SWC AST variant) over
  stringly-typed kind fields.
- Drop `#[serde(rename_all = "camelCase")]` on types that aren't actually
  serialized to disk or sent over the wire — internal manifests that the
  pipeline orchestrator only reads `kind` from don't need to derive
  `Serialize` at all.
- Replace `Vec<Value>` / `Map<String, Value>` payloads with typed structs
  whenever the shape is known. `serde_json::Value` is appropriate only
  when the value is genuinely polymorphic (spec args, `#[serde(flatten)]`
  extension slots) or comes straight from an external JSON input.

The public spec format consumed by `--spec` and the on-disk artifact
manifests are external contracts — be deliberate about changing them.
Internal intermediate types have no such constraint.

## Path Resolution Contract

The CLI accepts relative or absolute paths for `--spec`, `--package-root
<pkg>=<dir>`, and `--packages-root`. When the binary runs inside a Bazel
runfiles context (`bazel run`, `bb run`, or otherwise with `RUNFILES_DIR` /
`RUNFILES_MANIFEST_FILE` set), each relative path is first resolved through
runfiles via the standard `runfiles` crate; if the resolution points at an
existing file the runfiles path is used, otherwise the path is left as-is for
the caller's filesystem semantics. Outside Bazel the binary behaves as a
plain CLI — runfiles resolution is opt-in by environment, not a build-time
mode.

This lets downstream Bazel targets compose absolute-equivalent paths with
just `$(rlocationpath <label>)` (no shell wrappers, no `$$RUNFILES_DIR`
substitutions) while keeping the binary usable as a standalone tool outside
the Bazel tree.

## Materialize logical-modules `targetDir`

`materialize_logical_modules` accepts an optional `targetDir`. Absent or
empty means "no subdirectory" — lowered files land directly under their
chunk root (`<out_dir>/<chunkId>/<target.path>.js`). A non-empty value adds
that prefix (`<out_dir>/<chunkId>/<targetDir>/<target.path>.js`). Tests that
want the legacy `modules/` layout pass `"targetDir": "modules"` explicitly.
