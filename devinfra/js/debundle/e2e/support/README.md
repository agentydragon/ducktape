# E2E support

The library's existing public test helpers are re-exported by `mod.rs`:

- `fixture_spec.rs` owns fixture inputs, selectors, annotations, and typed transform-spec construction.
- `ast_assertions.rs` owns JavaScript parsing and structural assertions independent of printer whitespace.
- `saved_analysis.rs` (`debundle_e2e_support::saved_analysis`) owns staged-analysis fixtures, `spec project-analysis`/`analyze`/`run --analysis` command builders, and direct-versus-staged output comparison.
- `workspace.rs` owns tree input materialization and execution. `write_tree_fixture`
  prepares editable source/spec inputs without running the CLI; `TreeWorkspace::run`
  executes them. Staged tree fixtures analyze once and reuse that artifact for the
  initial graph-producing run.
- `serialized.rs` runs file-backed flat specs through the same execution,
  rejection, invariant, and failure-retention paths. Fixture conventions are in
  the [corpus runner](../corpus/README.md).
- `tree_assertions.rs` collects relative paths and raw bytes; exact-tree comparison
  and staged report normalization apply their separate policies to that collection.
- `process.rs` owns the mechanical subprocess boundary: it returns raw
  `std::process::Output` for either exit status, reports spawn/capture failures with
  command and working-directory context, and exposes the explicit Debundle invariant
  guard. It has no fixture or transcript state.
- `failure_workspace.rs` owns a case's lifetime. Flat, tree, graph and staged fixtures
  retain the whole workspace under `$TEST_UNDECLARED_OUTPUTS_DIR/failed-fixtures/`
  when an assertion unwinds through an owning handle, including assertions after a
  successful CLI run. Retention is best effort and does not replace the original
  panic. Per-invocation output directories share the input owner's lifetime; a
  returned staged invocation remains usable after its setup owner is dropped.
  `case.json` records the original root, and per-test names plus process/case IDs
  distinguish concurrent failures. Successful cases remove their temporary files.
- `case_commands.rs` records commands associated with a concrete path inside an
  owned case. Raw stdout/stderr, status, argv, cwd and child environment changes
  live in `.case-commands/<id>/`. Debundle commands also snapshot source/spec
  inputs, consumed analysis/graph files, external dependencies and the previous
  output state. The shared guard runs after raw output is recorded. Executable
  SHA-256 identities are computed when a failure is retained.
- `runtime.rs` owns Node/Bun execution checks, source-versus-output parity, and
  emitted-module behavior probes. Case-owned launches go through the evidence wrapper;
  the raw process boundary remains a separate lower-level API.
- `unescaped_local_callable.rs` owns the two-module spec and emitted-output checks for
  unescaped local callable reachability cases.
- `mod.rs` is the public harness facade and owns flat-fixture setup, CLI workflows, and report
  fixtures. Runtime-only `extra_files` are mirrored
  into `app/` after transformation and before exit handling; on a failed exit, mirroring
  happens only if the transform produced `app/`. `failed_fixture_dirs_for_current_test()`
  locates retained workspaces without changing the shared output environment.
- `defs.bzl` lets a test keep its existing Bazel target label while placing its Rust
  source in a contract directory. The `source` argument defaults to `<name>.rs`;
  representative sources live under `runtime/`, `cli/`, `reports/`, and
  `staged_identity/`.

For valid vendor marks, use the production spec types (including `WrapperShape` and
`PartialSwapKind`) in the test's fixture builders. Keep raw JSON/YAML where a test
needs to express malformed wire input; a typed builder must not make those cases
unrepresentable. AST assertions complement, rather than replace, Node behavior checks.

`graph_support.rs` is a separate crate (`debundle_e2e_graph_support`) for helpers that read typed graph reports; its `effect_validation.rs` module owns effect translation validation fixtures and operation-receipt assertions. Keep `analysis` out of `:support`: tests that read typed graphs depend on `:graph_support`, so an analysis-only edit recompiles and re-lints only tests that depend on `analysis` or `:graph_support`, not every driver.

## Replay a retained case

List the commands, then replay one into a fresh directory with its executable
supplied explicitly:

```bash
bb run //devinfra/js/debundle/e2e:replay_fixture -- list /path/to/retained-case
bb run //devinfra/js/debundle/e2e:replay_fixture -- run /path/to/retained-case 00000001 \
  --out /path/to/fresh-replay --binary /absolute/path/to/debundle
```

The same command works for recorded Node/Bun observations with their runtime
executable. The helper checks its hash against `tools.json`; use
`--allow-tool-change` deliberately when comparing tool versions. It forwards raw
output and the child exit status.

Replay restores the selected command's inputs and previous outputs, including
intentional nonempty-output refusals. Only spec path fields are relocated; the
retained originals and JS strings stay intact. Owned flat/staged commands run
from their case root so their canonical analysis projections use relative input
paths. Saved graph queries use the CLI's existing `--spec-root`,
`--graph-source-root` and package-root relocation support, preserving graph
compatibility checks. Each replay has its own output tree.
