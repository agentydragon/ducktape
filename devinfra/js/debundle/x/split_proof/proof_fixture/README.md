# A checked emitter plan and MiniJS

This small fixture asks whether an actual extractor-generated instance and the existing renderer can be connected to the MiniJS reordering theorem. It is not a proof of JavaScript or ES-module semantics.

The input has three `const` declarations; `copy` reads `one`. The generated assignment puts `one` in a separate module. The import causes the modeled order to be `[1, 0, 2]`, rather than `[0, 1, 2]`. The rejected control moves `copy` into the dependency of its own declaration: the checker rejects its order and forced output throws `ReferenceError` in Node.

Bazel runs `extract/extract.mjs` on the fixture and spec to generate `ProofFixture.Instance`. `Experiments.ProofFixture` kernel-checks the generated plan, its execution order, exact strings from `Module.render`, and footprint equality against a **hand-written** MiniJS witness. It applies `mjs_reorder` to that computed order at every fuel. The checked equality does not establish that the MiniJS program means the same thing as the JavaScript text. The axiom test audits these theorems.

The binary runs the same `report` and `writeModules` used by the example CLI, using that generated bundle and plan. The behavioral test executes the input and actual emitted modules in Node; test-only exports expose the final values. It checks that the unsafe assignment is refused without output and, when forced, throws. Node is a separate test oracle, not part of the Lean theorem.

The MiniJS witness is restricted to number literals and a global name read, with no reassignment, exceptions, host calls, or renaming. `cstep` models fuel exhaustion and failed evaluation by setting the declared names to `none` and leaving the world unchanged; the quantified theorem is equality in this model, not an assertion of JavaScript termination. The theorem is over MiniJS state, not parsed JavaScript or module graph executions. Footprint extraction, correspondence of JS source to the witness, ES module linking and evaluation, and filesystem/Node behavior remain outside the proof. Existing `Bundle.step` also gives every name in one statement the same value and abstracts away throwing and divergence; this fixture avoids multiple writes and does not prove those behaviors.

Run:

```sh
bbr test //devinfra/js/debundle/x/split_proof/lean:proof_fixture_axioms_test //devinfra/js/debundle/x/split_proof/lean:proof_fixture_test
```
