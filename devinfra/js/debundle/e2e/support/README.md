# E2E support

The library's existing public test helpers are re-exported by `mod.rs`:

- `fixture_spec.rs` owns fixture inputs, selectors, annotations, and typed transform-spec construction.
- `ast_assertions.rs` owns JavaScript parsing and structural assertions independent of printer whitespace.
- `mod.rs` owns filesystem setup, CLI/Node execution, emitted-tree checks, and report fixtures.

For valid vendor marks, use the production spec types (including `WrapperShape` and `PartialSwapKind`) in the test's
fixture builders. Keep raw JSON/YAML where a test needs to express malformed wire input; a typed builder must not make
those cases unrepresentable. AST assertions complement, rather than replace, Node behavior checks.
