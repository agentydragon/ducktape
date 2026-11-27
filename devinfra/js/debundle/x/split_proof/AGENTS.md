@README.md

## Lean proof output

Do not commit `#print axioms` commands in split-proof Lean sources. Lean emits each
result as an info message whenever Bazel compiles that module, which pollutes routine
build output. When an axiom restriction is worth enforcing, use `lean_proof_test`
targets in `lean/BUILD.bazel` with `modules` and `required_theorems` instead of printing
axioms; the test fails when declarations depend on disallowed axioms.
