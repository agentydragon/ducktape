# E2E support

The library's existing public test helpers are re-exported by `mod.rs`:

- `fixture_spec.rs` owns fixture inputs, selectors, annotations, and typed transform-spec construction.
- `ast_assertions.rs` owns JavaScript parsing and structural assertions independent of printer whitespace.
- `mod.rs` owns filesystem setup, CLI/Node execution, emitted-tree checks, and report
  fixtures. An unexpected CLI exit preserves the spec, available per-chunk reports,
  root program graph, and any emitted `app/` tree under undeclared outputs; outputs not
  reached before an early failure remain absent. Runtime-only `extra_files` are mirrored
  into `app/` after transformation and before exit handling; on a failed exit, mirroring
  happens only if the transform produced `app/`. `failed_fixture_dirs_for_current_test()`
  locates those retained artifacts without changing the shared output environment.

For valid vendor marks, use the production spec types (including `WrapperShape` and
`PartialSwapKind`) in the test's fixture builders. Keep raw JSON/YAML where a test
needs to express malformed wire input; a typed builder must not make those cases
unrepresentable. AST assertions complement, rather than replace, Node behavior checks.
