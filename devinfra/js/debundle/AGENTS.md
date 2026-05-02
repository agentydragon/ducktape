# Debundler Implementation Constraints

## AST Requirement

JavaScript transformation work must use proper AST-based operations on the
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
bundle. Reach for the e2e harness in `e2e/` (`debundle_rust` invoked
through `support.rs`) before reaching into internals.

This applies even when you're chasing a bug or feature that lives in one
internal stage. If the symptom is observable in the unbundled output —
exported symbol set, runtime behavior, file layout, source shape — write
the regression as an e2e test driving the real CLI. Do not mock the
pipeline, do not snapshot the planner's intermediate state, and do not
construct internal types directly when an input fixture reaches the same
code path.

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
- Constructing `JsPipelineArtifact`, `AnalysisSummary`, planner snapshots,
  or other intermediate types by hand to drive a stage in isolation, when
  feeding an input fixture through the real pipeline reaches the same code
  path.
- Comparing serialized planner debug snapshots as the primary signal —
  these were parity-test scaffolding from when JS was a parallel impl. If
  a behavior matters, prove it by what comes out the far end.

## Native Rust over JS-Shoehorn Shapes

The earlier Rust port mirrored the JS implementation's data shapes (babel
AST node-type strings like `"VariableDeclarator"` / `"FunctionDeclaration"`,
camelCase JSON conventions for internal-only state, snake-shape parity
diagnostics). Those existed so the JS and Rust paths could be diffed
field-for-field. JS is gone; that diffing requirement is gone with it.

When you touch one of these layers, prefer native Rust shapes:

- Use Rust enums (often holding the SWC AST type) over stringly-typed kind
  fields.
- Drop `#[serde(rename_all = "camelCase")]` from internal-only types that
  are no longer compared against the JS pipeline.
- Replace `Map<String, Value>` / `Vec<Value>` payloads with concrete
  Pydantic-style structs where the serde value was only there to mirror
  what JS emitted.

The public spec format consumed by `--spec` is a separate concern —
external callers may still send that schema, so be deliberate about
changing it. Internal intermediate types have no such constraint.
