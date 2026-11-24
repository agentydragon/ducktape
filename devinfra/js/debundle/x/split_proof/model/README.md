# CoreJS differential coverage

[`differential.mjs`](differential.mjs) runs JavaScript in Node and the Lean interpreter,
then sends split plans through the real `lowersplit` emitter in diagnostic mode. Those
test runs compare the model and Node; emitted files are uncertified previews. CI:

```sh
bbr test //devinfra/js/debundle/x/split_proof/model:corejs_differential_test \
  //devinfra/js/debundle/x/split_proof/model:corejs_binding_differential_test \
  //devinfra/js/debundle/x/split_proof/model:corejs_inheritance_differential_test
```

The broad CI suite runs the handwritten cases and required generated comparisons
once with seeds 1 and 2. The binding suite uses seeds 1–12. Run
`model:corejs_differential_sweep_test` explicitly for 40 seeds and
random split plans. Appended `--test_arg=--count --test_arg=N`,
`--test_arg=--seed --test_arg=S` and `--test_arg=--splits --test_arg=K` override
the target's arguments.

| Landed model feature                                              | Hand-written oracle                                                                                                                                    | Generated execution                                                                                                                                  |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Primitive expressions, conversions, assignments, evaluation order | [`cases.mjs`](cases.mjs): numbers, conversions, assignment targets, calls                                                                              | Recursive expressions including negation; every unary, binary and logical operator in the frontend tables executes on generated operands.            |
| Closures, captures, calls, `this`, hoisting                       | `cases.mjs`: loop captures, hoisting, methods, named function expressions                                                                              | Private captured state, method receiver, retained callback, per-iteration captures.                                                                  |
| Objects, descriptors, getters/setters, prototypes                 | `cases.mjs`: inherited accessors, descriptor validation, prototype changes                                                                             | Getter/setter mutation, deletion, inherited membership, prototype replacement.                                                                       |
| Arrays                                                            | `cases.mjs`: nested literals, reads, length, deletion                                                                                                  | Nested reads, identity, bounds and membership. Writes still end `unknown`.                                                                           |
| Plain classes                                                     | `cases.mjs`: constructor, fields, statics, methods, accessors, names, errors                                                                           | Instance/static fields, constructor, getters/setters, captured `this`.                                                                               |
| Default derived classes                                           | `cases.mjs`: prototype links, dynamic superclass, null heritage, TDZ, instance fields, returns/throws; `opaque_cases.mjs`: opaque base body            | Varied depth and arity, both prototype chains, base receiver and fields, method overrides, and retargeting, returns, and throws.                     |
| Control flow and exceptions                                       | `cases.mjs`: loops, jumps, abrupt `finally`, TDZ, engine errors, thrown values                                                                         | Finite loops; caught primitive, engine and host exceptions; `finally` on return.                                                                     |
| Primitive host calls                                              | `cases.mjs`: string/number/boolean methods, `Math`, parsing, throws, unknown boundaries                                                                | Primitive calls and chains; a caught host exception. Selected functions only.                                                                        |
| Linked modules                                                    | [`module_cases.mjs`](module_cases.mjs): live bindings, cycles, hoisting/TDZ, import assignment, aliases, defaults, missing exports, evaluation failure | Live diamond, caught TDZ, dependency throw. Default expression, alias, function or class. Import assignment, update and compound assignment.         |
| Opaque statements and function bodies                             | [`opaque_cases.mjs`](opaque_cases.mjs): binding/property access, callbacks, explicit throws, allocation, construction, engine-error propagation        | Opaque parameter destructuring calls a captured model function, returning normally or throwing into a model catch.                                   |
| Opaque closure-only moves                                         | `opaque_cases.mjs`: constructor closure                                                                                                                | A closure captures an uninitialized binding, crosses an independent declaration, and is invoked after initialization. Both callback endings execute. |

Generated opaque allocations build prototype chains of varying depths from a fresh ordinary object or a supplied handle, then observe inherited writes and prototype identity.

Flat, nonempty `var` object patterns with static keys and identifier targets use the
ordinary property-read interpreter. The model keeps the RHS value through all reads,
including getters and exceptions, and hoists the target bindings. The objects generator
destructures two to four renamed keys per seed and observes all the resulting values.
Handwritten cases cover null, throwing getters, rebinding the receiver and exports.

[`generate.mjs`](generate.mjs) generates the whole-program families above without
discarding units or retrying after `unknown`. Every seed must complete in Node and the
model, and must execute in an accepted reordered split whose model diagnostic finds the
`moves` condition. This model result does not certify the emitted JavaScript artifacts.
The test requires every promised family and every generated linked graph
to run; removing a family cannot leave an empty successful coverage assertion.

The required reorder swaps independent prefix allocations: an object or array for those
families, a function closure for the others. These counters mean the **program** was
reordered and its selected family executed. They do not mean a class, loop, exception
handler or host call crossed another statement. Opaque closure cases move the opaque
declaration itself. Accepted normal `moves` runs also compare the final model-state
hash in the bundle's recorded world; the hash omits full function bodies and world state.
Order-kept splits retain the corresponding check for every ending.

The `corejs_binding_differential_test` (`--bindings-only`) requires
`moves (certified bindings)` for reordered independent assignments. Each
assignment stays with its declaration. Seeds cover literals and bindings, binary
expressions with two writes, heap-free unary operations, and conditional or
short-circuit branches. The corresponding dependency-bearing plan must be refused.
The cases also check object-reference copies, the `typeof` boundary and refusal
after removing an actual Effects write summary. Accepted diagnostic splits
retain the four-way comparison and the final-state hash check described above.
[Binding certificates](../docs/footprint_soundness.md#initial-certificate-fragment)

The separate [general generator](../translation/testing/gen/) sample reports
units accepted alone and units surviving dependency/unknown-code filtering.
Those counts measure admission. Use the required family executions above for
runtime coverage. The sweep's `--splits` argument bounds random plans. The
dedicated generator's required moves bypass that budget.

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
seed also moves a class across an independent declaration before its captured
bindings are initialized. The generated class varies member order, receiver,
captures, and whether it has accessors. Its constructor and each selected method
or accessor call back into modeled JavaScript after initialization. The harness
checks that each body runs in the expected order and that the proved split
preserves both the Node observations and the final model-state hash.

## Default derived classes

The standalone frontend models default derived constructors with public instance
fields. Fixed-name methods and accessors may have opaque bodies under the same
restrictions as plain classes. Heritage evaluates in the class-name TDZ and
constructor validation precedes the `prototype` read. Null heritage and a null
superclass prototype remain distinct.

The implicit constructor follows its current internal prototype and forwards the
original arguments and `newTarget` through the chain. The eventual base constructor
allocates the receiver using `newTarget.prototype`, initializes its fields, and runs
its body. Each derived constructor then initializes its own fields on the object
returned by its superclass, in declaration order. A throw from the superclass
skips those initializers. Opaque bodies still receive the actual callee. Body fallback refuses
`new.target` because the world protocol does not carry it. Opaque/host superclass
capabilities not represented by the model end `unknown`.

Handwritten cases cover chains, both prototype links, constructor-prototype mutation,
invalid heritage/prototypes, null heritage, TDZ, derived field order, returned objects, exceptions,
invocation restrictions, and opaque base bodies. The separate
`corejs_inheritance_differential_test` (`--inheritance-only`) also generates
default-constructor chains with varying depth, argument count, and override
position. Each seed retargets a constructor in the chain, observes a replacement
object from the base constructor, or catches a value the base throws. Exact
Node and CoreJS output checks witness argument order, both prototype chains,
the receiver and field value during base construction, inherited getter dispatch,
the override, and the selected outcome. The harness counts these generated
observations separately from handwritten cases. The chain cases also execute
in a split with independent prefix allocations reordered. The CI target runs 24 seeds over two shards.

The guarded heritage cases move an anonymous subclass declaration across local
allocations. Each seed composes an anonymous base and subclass from methods,
getters, setters and subclass statics. It varies lexical blocks, the intervening
prefix of inert, allocating or derived declarations, and the number and kinds of
crossed allocations. The observations exercise the selected class members,
prefix statements and crossed initializers. They check constructor values with
positive and negative inputs and both prototype links. At the default seed
count, each shard checks that successful runs cover multiple shapes in every
dimension.

The handwritten plan and every generated positive must reach
`moves (certified class heritage)` with a normal-equivalence verdict, agree in
the four Node and CoreJS executions, and match the final model-state hash. A
seeded mutation of each positive adds a base static, aliases or writes the
superclass, names the pivot, or inserts a crossed assignment. These controls
reach the heritage guard with valid structural plans and checked footprints.
They must get a negative guard result while continuing to match runtime
observations and the final model-state hash. A handwritten control checks the
structural rejection of a crossed read of the moving subclass.

For a timeout, inspect `progress.log` in Bazel's undeclared outputs. It records
Node case labels, CoreJS batches, and module-order and split subprocess boundaries.
