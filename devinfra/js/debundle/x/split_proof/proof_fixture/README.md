# A checked emitter plan and MiniJS

This small fixture asks whether an actual extractor-generated instance and the existing renderer can be connected to the MiniJS reordering theorem. It is not a proof of JavaScript or ES-module semantics.

The input has three `const` declarations; `copy` reads `one`. The generated assignment puts `one` in a separate module. The import causes the modeled order to be `[1, 0, 2]`, rather than `[0, 1, 2]`. The rejected control moves `copy` into the dependency of its own declaration: the checker rejects its order and forced output throws `ReferenceError` in Node.

Bazel runs `extract/extract.mjs` on the fixture and spec to generate `ProofFixture.Instance`. `Experiments.MiniJSSource` translates the generated bundle's exact statement source strings into MiniJS, refusing anything except a tightly formatted `const` declaration with an ASCII identifier and either a single-digit literal or another identifier as its initializer. `Experiments.ProofFixture` kernel-checks that translation, its footprints against the extractor's, the generated plan and its execution order, and exact strings from `Module.render`. It applies `mjs_reorder` to the computed MiniJS program and order at every fuel. Rejection checks cover calls, operators, large numbers, escapes, keywords, extra tokens, and non-`const` declarations. The axiom test audits these theorems.

`Bundle.conflict_symm` proves conflict is symmetric even for hoisted declarations. `inverted_conflict_rejected` connects an executable footprint conflict, checked in bundle order, to rejection of a plan that runs the pair in the opposite order. The fixture checks a non-conflicting inversion in the accepted plan, a conflicting inversion in the rejected generated plan, and hoisted read/write cases in both argument orders. This proves a given witness rejects the model's `OrderOk`; it does not prove that the translation diagnostic's enumeration or total count is correct, nor that a rejected JavaScript move actually changes behavior.

The binary runs the same `report` and `writeModules` used by the example CLI, using that generated bundle and plan. The behavioral test executes the input and actual emitted modules in Node; test-only exports expose the final values. It checks that the unsafe assignment is refused without output and, when forced, throws. Node is a separate test oracle, not part of the Lean theorem.

The MiniJS translation is restricted to the syntax above, with no reassignment, exceptions, host calls, or renaming. `cstep` models fuel exhaustion and failed evaluation by setting the declared names to `none` and leaving the world unchanged; the quantified theorem is equality in this model, not an assertion of JavaScript termination. The theorem is over MiniJS state, not parsed JavaScript or module graph executions. The Lean recognizer is not proved correct against ECMAScript: the extractor's Acorn parse, source slicing, and the meaning of the recognized JavaScript syntax remain assumptions, as do ES module linking and evaluation and filesystem/Node behavior. The check on footprints does not establish those semantic correspondences. Existing `Bundle.step` also gives every name in one statement the same value and abstracts away throwing and divergence; this fixture avoids multiple writes and does not prove those behaviors.

Run:

```sh
bbr test //devinfra/js/debundle/x/split_proof/lean:proof_fixture_axioms_test //devinfra/js/debundle/x/split_proof/lean:proof_fixture_test
```
