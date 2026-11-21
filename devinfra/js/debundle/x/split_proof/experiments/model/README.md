# CoreJS differential coverage

[`differential.mjs`](differential.mjs) runs JavaScript in Node and the Lean interpreter,
then sends split plans through the real `lowersplit` emitter. CI:

```sh
bbr test //devinfra/js/debundle/x/split_proof/experiments/translation:corejs_differential_test \
  //devinfra/js/debundle/x/split_proof/experiments/translation:corejs_binding_differential_test
```

The fixed sample uses seeds 1–12. Appended `--test_arg=--count --test_arg=N`,
`--test_arg=--seed --test_arg=S` and `--test_arg=--splits --test_arg=K` override its arguments.

| Landed model feature                                              | Hand-written oracle                                                                                                                                    | Generated execution                                                                                                                                  |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Primitive expressions, conversions, assignments, evaluation order | [`cases.mjs`](cases.mjs): numbers, conversions, assignment targets, calls                                                                              | Recursive bounded expressions; compound assignment and updates. A subset of operators.                                                               |
| Closures, captures, calls, `this`, hoisting                       | `cases.mjs`: loop captures, hoisting, methods, named function expressions                                                                              | Private captured state, method receiver, retained callback, per-iteration captures.                                                                  |
| Objects, descriptors, getters/setters, prototypes                 | `cases.mjs`: inherited accessors, descriptor validation, prototype changes                                                                             | Getter/setter mutation, deletion, inherited membership, prototype replacement.                                                                       |
| Arrays                                                            | `cases.mjs`: nested literals, reads, length, deletion                                                                                                  | Nested reads, identity, bounds and membership. Writes still end `unknown`.                                                                           |
| Plain classes                                                     | `cases.mjs`: constructor, fields, statics, methods, accessors, names, errors                                                                           | Instance/static fields, constructor, getters/setters, captured `this`.                                                                               |
| Control flow and exceptions                                       | `cases.mjs`: loops, jumps, abrupt `finally`, TDZ, engine errors, thrown values                                                                         | Finite loops; caught primitive, engine and host exceptions; `finally` on return.                                                                     |
| Primitive host calls                                              | `cases.mjs`: string/number/boolean methods, `Math`, parsing, throws, unknown boundaries                                                                | Primitive calls and chains; a caught host exception. Selected functions only.                                                                        |
| Linked modules                                                    | [`module_cases.mjs`](module_cases.mjs): live bindings, cycles, hoisting/TDZ, import assignment, aliases, defaults, missing exports, evaluation failure | Live-binding diamond, cycle with caught TDZ, uncaught dependency exception. Aliases execute; defaults and frozen import targets remain manual-only.  |
| Opaque statements and function bodies                             | [`opaque_cases.mjs`](opaque_cases.mjs): binding/property access, callbacks, explicit throws, allocation, construction, engine-error propagation        | Opaque parameter destructuring calls a captured model function, returning normally or throwing into a model catch.                                   |
| Opaque closure-only moves                                         | `opaque_cases.mjs`: constructor closure                                                                                                                | A closure captures an uninitialized binding, crosses an independent declaration, and is invoked after initialization. Both callback endings execute. |

[`generate.mjs`](generate.mjs) generates the eight whole-program families above without
discarding units or retrying after `unknown`. Every seed must complete in Node and the
model, and must execute in an accepted reordered split whose verdict names the proved
`moves` slice. The test requires every promised family and every generated linked graph
to run; removing a family cannot leave an empty successful coverage assertion.

The required reorder swaps independent prefix allocations: an object or array for those
families, a function closure for the others. These counters mean the **program** was
reordered and its selected family executed. They do not mean a class, loop, exception
handler or host call crossed another statement. Opaque closure cases move the opaque
declaration itself. Accepted normal `moves` runs also compare the final model-state
hash in the bundle's recorded world; the hash omits full function bodies and world state.
Order-kept splits retain the corresponding check for every ending.

The separate required `corejs_binding_differential_test` (`--bindings-only`) requires a different move: each assignment stays in its
declaration's module, while independent assignments change relative order. Every seed
runs four families: literals/bindings, binary operations with writes in both operands,
heap-free unary operations, and conditional/short-circuit branches. Each executes through the `moves (certified
bindings)` slice and supplies a dependent variant that the real checker must reject.
The handwritten cases also copy and compare object references without inspecting
their heap records during the moved statements, and check that `typeof` gains no permission. A mutation control obtains the actual Effects output, reloads it,
then removes a required write and requires the semantic certificate to reject it.
Successful certified splits receive the same four-way execution and exact final-state
comparisons as the other proved moves. The broad `corejs_differential_test` retains
the interpreter families, linked graphs, opaque cases, and original named splits.
Both targets run in CI; splitting their workloads keeps their coverage assertions
and execution independent as the certificate families grow.

The broad [`translation/gen`](../translation/gen/) sample remains separate: the harness
reports units accepted alone and units surviving dependency/unknown-code filtering.
Those counts do not measure which syntax executed. Random plans remain manual-first
and bounded by `--splits`; the dedicated generator's required moves bypass that budget.

Opaque scripts observe the requests `readVar`, `writeVar`, `initVar`, `get`, `set`,
`delete`, `has`, `call`, `construct`, `newObject`, `newFunction` and `prim` through Node
output and completion. `done` and `throw` are exercised exits. `keys` has a separate
Node-derived reply expectation, checked before replay continues. The scripted world
propagates request errors; it cannot catch one and continue its own script. An explicit
world `unknown` exit and invalid handles need kernel-level examples, not a faithful
Node execution. This is a bounded coverage inventory, not full JavaScript coverage or
a proof of interpreter conformance.
