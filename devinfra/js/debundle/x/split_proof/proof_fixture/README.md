# A checked reordering of three declarations

The input declares `unused`, `one` and `copy`. The accepted placement puts `one` in a separate module, so its import changes the execution order to `[1, 0, 2]`. The unsafe placement evaluates `copy` before `one` has been initialized.

Bazel runs `split_instance_generator` to generate the Lean bundle, accepted plan and rejected control from the JavaScript and its specs, without rendering JavaScript modules. [ProofFixtureChecks.lean](../lean/ProofFixtureChecks.lean) checks the exact source spelling of all three statements against their fixture-specific CoreJS interpretations, verifies the generated footprints cover their declared and free names, and applies `CoreJS.moves_preserve_run` to the generated plan. For every world and fuel, a successful execution of either modeled program reaches the same final state as the other. Interpreting the three source strings as CoreJS is an explicit premise.

The kernel checks the rendered module strings and each inversion, including conflict lookup in both argument orders for hoisted bindings. A separate Node test compares the original and emitted values, checks that normal rejection writes no entry module, and forces the unsafe split to observe the expected `ReferenceError`.

Run:

```sh
bbr test //devinfra/js/debundle/x/split_proof/lean:proof_fixture_axioms_test //devinfra/js/debundle/x/split_proof/lean:proof_fixture_test
```
