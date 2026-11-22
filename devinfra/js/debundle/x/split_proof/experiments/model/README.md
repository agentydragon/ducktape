# CoreJS differential coverage

[`differential.mjs`](differential.mjs) runs JavaScript in Node and the Lean interpreter,
then sends split plans through the real `lowersplit` emitter. CI:

```sh
bbr test //devinfra/js/debundle/x/split_proof/experiments/translation:corejs_differential_test \
  //devinfra/js/debundle/x/split_proof/experiments/translation:corejs_binding_differential_test \
  //devinfra/js/debundle/x/split_proof/experiments/translation:corejs_inheritance_differential_test
```

The fixed sample uses seeds 1–12. The broad suite partitions those seeds across
two Bazel shards; each shard also runs the handwritten cases and retains the
configured random-plan budget. Appended `--test_arg=--count --test_arg=N`,
`--test_arg=--seed --test_arg=S` and `--test_arg=--splits --test_arg=K` override its arguments.

| Landed model feature                                              | Hand-written oracle                                                                                                                                    | Generated execution                                                                                                                                  |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Primitive expressions, conversions, assignments, evaluation order | [`cases.mjs`](cases.mjs): numbers, conversions, assignment targets, calls                                                                              | Recursive bounded expressions; compound assignment and updates. A subset of operators.                                                               |
| Closures, captures, calls, `this`, hoisting                       | `cases.mjs`: loop captures, hoisting, methods, named function expressions                                                                              | Private captured state, method receiver, retained callback, per-iteration captures.                                                                  |
| Objects, descriptors, getters/setters, prototypes                 | `cases.mjs`: inherited accessors, descriptor validation, prototype changes                                                                             | Getter/setter mutation, deletion, inherited membership, prototype replacement.                                                                       |
| Arrays                                                            | `cases.mjs`: nested literals, reads, length, deletion                                                                                                  | Nested reads, identity, bounds and membership. Writes still end `unknown`.                                                                           |
| Plain classes                                                     | `cases.mjs`: constructor, fields, statics, methods, accessors, names, errors                                                                           | Instance/static fields, constructor, getters/setters, captured `this`.                                                                               |
| Default derived classes                                           | `cases.mjs`: prototype links, dynamic superclass, null heritage, TDZ, returns/throws; `opaque_cases.mjs`: opaque base body                             | Multi-level forwarding, base fields, inherited methods, instance prototype, constructor length.                                                      |
| Control flow and exceptions                                       | `cases.mjs`: loops, jumps, abrupt `finally`, TDZ, engine errors, thrown values                                                                         | Finite loops; caught primitive, engine and host exceptions; `finally` on return.                                                                     |
| Primitive host calls                                              | `cases.mjs`: string/number/boolean methods, `Math`, parsing, throws, unknown boundaries                                                                | Primitive calls and chains; a caught host exception. Selected functions only.                                                                        |
| Linked modules                                                    | [`module_cases.mjs`](module_cases.mjs): live bindings, cycles, hoisting/TDZ, import assignment, aliases, defaults, missing exports, evaluation failure | Live-binding diamond, cycle with caught TDZ, uncaught dependency exception. Aliases execute; defaults and frozen import targets remain manual-only.  |
| Opaque statements and function bodies                             | [`opaque_cases.mjs`](opaque_cases.mjs): binding/property access, callbacks, explicit throws, allocation, construction, engine-error propagation        | Opaque parameter destructuring calls a captured model function, returning normally or throwing into a model catch.                                   |
| Opaque closure-only moves                                         | `opaque_cases.mjs`: constructor closure                                                                                                                | A closure captures an uninitialized binding, crosses an independent declaration, and is invoked after initialization. Both callback endings execute. |

[`generate.mjs`](generate.mjs) generates the whole-program families above without
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
bindings)` slice. Each seed also supplies a dependency-bearing binding-assignment
variant that the real checker must reject.
The handwritten cases also copy and compare object references without inspecting
their heap records during the moved statements, and check that `typeof` gains no permission. A mutation control obtains the actual Effects output, reloads it,
then removes a required write and requires the semantic certificate to reject it.
Successful certified splits receive the same four-way execution and final model-state
hash comparisons as the other proved moves. Exact final-state equality is the Lean
theorem’s guarantee; the runtime hash comparison is a regression oracle with the
omissions described above. The broad `corejs_differential_test` retains
the interpreter families, linked graphs, opaque cases, and original named splits.
All three targets run in CI; splitting their workloads keeps their coverage assertions
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

## Plain class creation with opaque bodies

In opaque mode the frontend retains a plain class's creation structure when a
constructor, method, or accessor body is outside the executable fragment. Each
such function captures its lexical free cells; only an invocation enters the
world conversation, with the original callee, receiver, and arguments. Its code
identity is the preserved source-unit ID and its part is the one-based lexical
function index within that unit (including modeled and nested functions).

Class constructors still reject ordinary calls; methods and accessors still
reject construction. Instance fields execute before the constructor body, and
modeled eager keys/static initializers remain explicit. New body fallback does
not handle `super`, `new.target`, async/generator functions, direct eval, or relocation-sensitive
imports. A refused body cannot silently erase those restrictions.

The plain-class allocation proof admits only fixed-string method/accessor keys and no fields.
It captures method bodies without executing them, writes only fresh objects and
the class's own binding, and composes with the existing allocation move theorem.

Handwritten cases cover deferred execution, receivers/arguments, class self names,
nested lexical captures, constructor returns/throws, invocation restrictions,
accessors, stable part identities, and eager static/field behavior. Each generated
seed also moves a class across an independent declaration, then constructs it and
calls an opaque method. Every such split must use a proved move and agree in all
four executions and in the final model-state hash.

## Default derived classes

The frontend models `class Derived extends Base { … }` when it has no explicit
constructor or fields. Fixed-name methods/accessors may have opaque bodies under
the same restrictions as plain classes. Heritage evaluates in the class-name TDZ;
constructor validation precedes the `prototype` read. Null heritage and a null
superclass prototype remain distinct.

The implicit constructor follows its current internal prototype and forwards the
original arguments and `newTarget` through the chain. The eventual base constructor
allocates the receiver using `newTarget.prototype`, initializes its fields, and runs
its body. Opaque bodies still receive the actual callee; body fallback refuses
`new.target` because the world protocol does not carry it. Opaque/host superclass
capabilities not represented by the model end `unknown`.

Handwritten cases cover chains, both prototype links, constructor-prototype mutation,
invalid heritage/prototypes, null heritage, TDZ, returned objects, exceptions,
invocation restrictions, and opaque base bodies. The separate `corejs_inheritance_differential_test` (`--inheritance-only`) runs
these cases and the generated `inheritance` family, executing a default-constructor
chain for each of the same 12 seeds. The chain cases reorder independent prefix
allocations.

The guarded heritage cases move an anonymous subclass declaration across local
allocations. An anonymous plain base class and an earlier subclass remain in the
prefix. The handwritten plan and every generated seed must reach
`moves (certified class heritage)` with a normal-equivalence verdict, agree in
the four Node and CoreJS executions, and match the final model-state hash.
Negative cases exercise a superclass reassignment, a function superclass, and
a crossed declaration that reads the moving subclass.

The harness writes `progress.log` to Bazel's undeclared outputs, recording Node
case labels, CoreJS batch boundaries, and module-order/split subprocess boundaries for timeout diagnosis. The inheritance
suite has its own test budget; the original broad suite retains its seeds and
split-plan budget.
