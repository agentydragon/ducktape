@README.md

## Lean proof output

Do not commit `#print axioms` commands in split-proof Lean sources. Lean emits each
result as an info message whenever Bazel compiles that module, which pollutes routine
build output. When an axiom restriction is worth enforcing, use `lean_proof_test`
targets in `BUILD.bazel` with `modules` and `required_theorems` instead of printing
axioms; the test fails when declarations depend on disallowed axioms.

## Lean targets

One `lean_library` per module in `BUILD.bazel`, `deps` = owners of the modules it imports. `rules_lean` compiles
a multi-module library in `srcs` order and feeds each compile every earlier module, so grouping serializes independent
modules: splitting cut one edit's critical path 502 s → 144 s (#20549). Group only what cannot stand alone (e.g. a
generated source). Missing deps fail the compile; extra deps only delay scheduling.

Name a target `corejs_<module>` for `EsmSplit/CoreJS/…`, else `<module>` (snake case). Module names follow file paths
(`rules_lean` and Lake), so moving a file renames its module.
