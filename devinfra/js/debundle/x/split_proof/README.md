# Splitting a bundle into ES modules, with a Lean proof

A flat JavaScript bundle goes in. A spec says which top-level statement goes to which module.
The experimental splitter emits ES modules after Lean certifies the requested
CoreJS guarantee. [Certification commands and guarantees](#request-a-corejs-guarantee)

[`SUPPORT.md`](SUPPORT.md) inventories the syntax, standard library and host
features the intended inputs require. [CoreJS coverage](model/README.md) tracks
the fragment exercised by the model tests.

From the repository root, run the same checks as CI:

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/..." --config=ci
```

Bazel supplies Lean, Node, Python, and the JavaScript dependencies. For focused checks and longer experiments, use the following commands with the same `p` (`out` is an absolute output path):

| Task                                                         | Command                                                                               |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------- |
| Compare the rendered example and rejected controls with Node | `bbr test "$p/lean:example_test"`                                                     |
| Render the verified example                                  | `bb run "$p/lean:render" -- "$out"`                                                   |
| Audit axioms and check the lowered example certificate       | `bbr test "$p/lean:axioms_test" "$p/lean:lowered_example_certificate_test"`           |
| Compile the extra Lean proofs                                | `bbr test "$p/lean:experiments_test"`                                                 |
| Compare module evaluation order across 500 graphs            | `bbr test "$p/es_order:es_order_sweep_test"`                                          |
| Fuzz the example renderer                                    | `bb run "$p/fuzz:render_fuzz" -- 600 1`                                               |
| Check translation fixtures and effect cases                  | `bbr test "$p/translation:fixtures_test" "$p/effects:effects_cases_test"`             |
| Run a larger translation fuzz sample                         | `bb run "$p/translation:translation_fuzz" -- --count 150 --seed 1 --require-accepted` |
| Run the extended CoreJS comparison                           | `bbr test "$p/model:corejs_differential_sweep_test"`                                  |
| Run the debundle case corpus                                 | [Corpus commands](debundle_cases/README.md#run-it)                                    |
| Lower public library bundles and check their splits          | [Bundle commands](experiments/README.md#6-lower-the-ast-into-lean)                    |

To refresh the checked-in example instance after changing the inputs:

```sh
dir=devinfra/js/debundle/x/split_proof
bbr build "//$dir/lean/generated:generate_instance" --remote_build_event_upload=all --remote_download_outputs=toplevel
cp "bb-out/bazel-out/k8-fastbuild/bin/$dir/lean/generated/EsmSplit/Instance.lean" "$dir/lean/EsmSplit/Instance.lean"
bbr test "//$dir/lean:instance_freshness_test"
```

Acorn supplies an unsplit module AST and eslint-scope annotates its binding occurrences. Lean enumerates source ordinals from the module body. It gives each declarator of a top-level `var`, `let` or `const` its own ordinal, including under a named export, and keeps each destructuring pattern together. Lean assembles an ESTree `Program` for each output file, including the imports and exports that connect the modules. Astring prints these trees as JavaScript. Functions, classes and methods retain their original source spelling because JavaScript exposes it through `Function.prototype.toString`. Unversioned input dumps are upgraded by reparsing the included source and checking its annotations.

For a closed module containing only identifier declarations, the checker also handles `Declaration extraction`. Supported initializers include primitive literals, binding reads, unary minus on a number literal, and logical negation of any supported initializer (for example, `const offset = -42, visible = !!offset;`). It evaluates the original declaration groups by allocating the entire module’s bindings before running the initializers in order. Lean proves that evaluating these groups gives the same CoreJS result as evaluating their extracted units, including if an initializer throws. [Declaration certificate](lean/EsmSplit/CoreJS/Declaration/Certificate.lean)

## Request a CoreJS guarantee

Pass the source module, placement spec and a fresh output directory to the normal splitter command. Its default request is `normal-completion`; an order-preserving split may prove the stronger `all-outcomes` guarantee too.

```sh
bb run //devinfra/js/debundle/x/split_proof/lean:lowersplit -- bundle.mjs spec.json output
```

An ordinary call to ambient `Symbol.for` can be retained in deferred code without a host assumption. If initialization reaches the callee, the model reports an unknown result before evaluating the arguments.

For browser module scripts, the `browser-page` contract also permits `.js` output paths on emission and disk checking.

For a module loaded as a browser page entry, you can pass `--host-contract browser.json` with `{"profile":"browser-page"}` on emission and on `--check-output`. Under this profile, ambient `window`, `globalThis`, and `self` may return the same opaque page object, and their `typeof` may return `"object"`. The browser fallback `typeof global` may return `"undefined"`; a page with a Node-style shim can violate that premise. Each selected observation must preserve host state throughout initialization. A direct read of ambient `global` remains opaque and stops the proof if reached. Lexical and imported names use their actual bindings.

The [browser globals catalog](lean/EsmSplit/CoreJS/BrowserGlobals.lean) covers ECMAScript and browser APIs, including DOM bindings, timers, networking, storage, and static method references such as `Object.defineProperty` and `Date.now`. A reached lookup can assume a specific `typeof` or stable reference. For example, a reference to `FileReader` requires it to exist on that page. Reading `window.Error` and reading the bare `Error` binding can return the same opaque reference under separate premises. The checker records observations from both initializers and the generated Lean theorem checks them in every world satisfying the listed premises. A deferred or untaken lookup adds no premise. A reference to a method supplies no result for a call. Volatile page values, including scroll coordinates, and operations such as reading Error stacks or writing to the DOM stay opaque.

CoreJS allocates a fresh object for each regular-expression literal. It preserves the pattern, flags, and `lastIndex` descriptor. The shared prototype implements `source`, the individual flag getters, and `flags`, including reads of overridden flag properties. Calls to the intrinsic matching methods currently produce an unknown result.

The browser-page profile also allows deferred construction of selected ambient constructors (`Date`, `Error`, `RegExp`, `Set`, and `URL`). The linked model retains the constructor name and arguments. If initialization reaches that lookup, it reports an unknown result before evaluating the arguments, including inside a `try` block. Reading `Date.now` can certify under a stable-reference premise. Calling it during initialization requires a model of the clock. The caller is responsible for choosing a profile that holds in the deployment.

The command prints one JSON verdict. A successful split reports `status: "certified"` after Lean certifies the output module ASTs and the printer writes them to the requested directory. The default `normal-completion` request means that, for each modeled world and successful final state, the bundle reaches that state exactly when the split does. It does not say that either program completes normally; in the closed-declaration case, both may throw. The linked-program proof additionally establishes that both programs initialize normally. The checker reports the strongest available guarantee, so a default request may report `all-outcomes` for a supported order-preserving split. Pass `--require all-outcomes` to insist on that stronger guarantee. A rejected request exits nonzero with `status: "rejected"`, a null guarantee and a reason.

The experimental `--structural` checker selects a proof from the linked schedules. If the units keep their original order, it checks the exact unit sequence, source syntax, resolved capture cells, initial state and entry exports, then reports `scope: "linked-structural-order"`. For an order change, it brings units forward one at a time. A hoisted function is already in its declaration cell before either schedule runs. The initial states must agree on its object address, code, and captured cells; entry exports must resolve to the same live cells. Only the inert declaration step moves past earlier units, which may already use the function. Moving an allocating initializer still requires checks on the prefix and crossed units of the intermediate schedule. It reports `scope: "linked-structural-pivot"` for one move and `scope: "linked-structural-moves"` for multiple moves.

A regex literal can move as a fresh allocation, including as the value of a named data property. Each evaluation creates a distinct object with its own `lastIndex` and the realm's `%RegExp.prototype%`. The intrinsic constructor's `prototype` is a non-writable, non-configurable data property; literal evaluation does not consult the global `RegExp` binding or invoke getters on the prototype. This rule needs no browser-page premise. Calling `new RegExp(...)`, reading `/a/.flags`, and running a matcher during initialization remain outside the allocation rule.

Both proofs establish equality of normally terminating CoreJS states. Retained statements use the same opaque-code world on both sides, with access through their checked live cells and object handles. The verdict records the assumptions for interpreting the JavaScript artifacts.

A static browser deployment with retained `import.meta.url` and literal relative `import()` can supply `--deployment-origin ORIGIN`. The source entry keeps its effective URL, the split overlays its planned file paths, and the other assets keep their bytes and URLs. The checker parses the actual lazy-module dependency closure, accounts for its imports from linked modules, and checks the live cells those imports resolve to. The host uses native module caching and promise scheduling. Source positions and fetch timing are outside the modeled observations. Pass the same options to `--check-output` to repeat the proof after reparsing the emitted JavaScript.

Lean certifies the original annotated ESTree against the exact map of output module ASTs it hands to Astring, including generated imports and exports. Only then does the splitter print and publish the directory. The JavaScript guarantee assumes that Acorn and eslint-scope describe the source faithfully, that the printer preserves the certified trees, and that CoreJS models the supported syntax. To inspect an existing directory, pass `--check-output`. This parses the files and compares their syntax, callable source spelling and module names with the expected output before certifying the matched trees. Copied providers must match their source text. [Declaration correspondence theorems](lean/EsmSplit/CoreJS/Declaration/Artifacts.lean)

For a module that calls functions or imports named bindings from relative JavaScript files, use
the normal three-positional command with the original source path. The splitter parses
reachable `.mjs` and `.js` providers as ES modules and copies them alongside the split.
Lean lowers the original and generated module ASTs to CoreJS, checking statement placement, live binding addresses and the entry’s exports. The kernel checks a theorem about the two concrete CoreJS programs. The generated certificate then runs a proof-producing Lean check on the same in-memory AST data and plan to establish that they lower to those particular programs, including their deferred closure bodies. Both programs must initialize normally in the same final state for every modeled world satisfying the reported host premises.

For large certificates, the checker can elaborate AST correspondence in a separate Lean process. That check imports the execution theorem compiled for the current certificate and audits the result. For large linked programs that need neither host premises nor file-local write-batch rules, the checker also proves the source run first and records its complete final state. The candidate must reach that same state. If a checkpoint stage fails, the checker retries with the ordinary execution proof.

The linked checker can retain an ordinary function whose body fails lowering. It checks the callable's source spelling, original unit and lexical position, and captured bindings in both trees. The interpreter allocates the closure and its properties as usual. Calling or constructing it then yields an unknown result with the body’s lowering error. Retention requires that the callable can be represented without a module URL, method home object, enclosing implicit `arguments`, or direct `eval`.

A named export list such as `export { local as publicName }` can publish an imported binding.
Imports can follow a chain of these aliases to the declaration's live cell. A chain that
cycles without reaching a declaration fails to link. Lean checks that each entry export
resolves to the same live cell in both graphs. Source-bearing re-exports such as
`export { name } from "./provider.mjs"` are outside this fragment.
When a generated import needs a binding already published under an alias,
it imports that public name under the original local name. This adds no entry export.

Linked code can define async functions, arrows and methods. Each callable retains its source body and captured bindings, has the declared name and arity, and inherits from the realm’s `AsyncFunction.prototype`. A call reached during initialization returns an unknown verdict because CoreJS does not execute promises or schedule continuations. Async callables that need a home object, private names, direct eval or module context are rejected during lowering.

Synchronous generator declarations, expressions and methods retain their source and captures. Each function has a fresh prototype inheriting from the realm's shared generator prototype. Initialization can inspect those objects and their properties. Calling a generator during initialization yields an unknown verdict because parameter initialization and generator suspension are outside this model.

Ordinary top-level function declarations initialize when the modules link. A call can reach a declared function before its statement executes, while its closed-over lexical bindings still observe their own initialization order. When a supported parameter list precedes a body CoreJS cannot execute, the checker can retain the callable with its captures and declared arity. This includes functions with defaults, methods, accessors and base constructors. Array, rest and nested destructuring parameters can also be retained with their original captures and arity. Invoking the retained body during initialization reports its original lowering failure. Ordinary declarations directly in a function body also hoist, provided their names are distinct and do not overlap parameters or `var` bindings. Block-level function declarations and generators remain outside this fragment. [Linked lowering](lean/EsmSplit/CoreJS/FromESTree.lean)

Named parameters can have defaults such as `(x = 1, y = x) => y`. Missing and `undefined` arguments use the initializer. A `null` argument binds directly. Parameters initialize left to right and a read of the current or a later parameter before initialization throws, including inside `typeof`. Body lexical declarations are in a scope inside the parameter environment. The linked fragment refuses a body `var` whose name repeats a parameter or occurs free in an initializer. The function's `length` counts only parameters before the first default. CoreJS executes defaults on identifier parameters. It can retain other parameter patterns for deferred calls. Implicit `arguments` remains outside the linked fragment.

Linked ordinary functions and arrows accept nonempty flat object patterns in parameters and local `var`, `let`, and `const` declarations. For example, `function read({x, label: name})` takes one argument and reads `x` before `label`. A local declaration evaluates its initializer once. Both forms use CoreJS property lookup, including the receiver for a getter, and stop at the first abrupt read. Keys may be identifiers or string literals. Module-level patterns, nested targets, defaults, rest, and computed keys are outside this lowering. An empty pattern still checks its argument for null or undefined and is also excluded.

In linked programs, conditional expressions and `if` statements inside functions evaluate the test once and run the selected branch with the state left by the test. Both branches must lower to CoreJS, including code that initialization never executes. Branch blocks have their own lexical scope, and `var` declarations in either branch belong to the enclosing function. A false test with no `else` continues to the following statement.

Linked functions can also use `switch` cases ending in `return`, `throw`, or an unlabeled `break` before the next clause. The discriminant is evaluated once. Case tests run in source order and match by strict equality. A trailing `default` runs when no test matches. The checker refuses fallthrough, a nontrailing default, nested breaks, and lexical declarations shared by the clauses. Independently braced case bodies keep their own lexical scopes.

Linked sequence expressions evaluate their operands from left to right, stop at the first abrupt completion, and return the final operand's value. A sequence used as a call's callee supplies `undefined` as `this`, including when its last operand reads a member.

Linked member expressions support dot access and computed keys with modeled primitive
property names. A member call evaluates its receiver and key once, reads the callee before
evaluating arguments, and passes the receiver as `this`. Ordinary functions have the call's
receiver and arrows capture the surrounding `this`. Access to private members and to `super.property` is unsupported.

A derived constructor with plain parameters can compute local values before a direct `super(...)` call.
The prefix admits simple `let` and `const` bindings and expression statements. Prefixes and arguments
that read `this` or define a nested callable are rejected. The parent receives the evaluated arguments
in source order. The rest of the constructor sees the same local bindings and the receiver returned by
the parent, including a replacement object. If argument evaluation changes the active constructor’s
prototype, CoreJS returns unknown because Node and ECMAScript can select different parents.

Consecutive optional member reads such as `receiver?.field?.[key]` evaluate each reached receiver once. A null or undefined receiver yields `undefined` without computing a key or invoking a getter. A reached read keeps the original base as the getter's `this`, even if key evaluation changes the value of the receiver expression.

A single named method call `receiver?.method(args)` skips lookup and arguments when the receiver is null or undefined. The form `receiver.method?.(args)` performs ordinary property lookup first and skips the arguments if the returned method is null or undefined. Both forms evaluate the receiver once and preserve it as `this`. Bare optional calls, computed optional method calls, combined optional receiver and callee, chains with non-optional continuations, and chain expressions used directly as call targets still receive a refusal.

Linked `key in base` evaluates the key expression and then the base expression.
A primitive base throws before the key is converted. On a modeled object, CoreJS
converts the primitive key and looks for a property on the object or its prototype
chain without calling a getter. Reached object-to-primitive conversion stops with
an unknown result. Symbol keys, proxies, and unmodeled host objects are outside
the fragment.

Simple assignments to dot and computed properties, such as `record.name = value` and `record[key()] = value`, use CoreJS strict property writes. CoreJS evaluates the receiver, computed key expression, and right-hand side in that order, writes through the original receiver, and returns the assigned value. Object-key coercion, host writes, and array writes can produce an unknown result during initialization.

Plain `=` also writes local and module bindings. It evaluates the right-hand side once before writing and returns the assigned value. An anonymous function on the right-hand side takes the target identifier's name. An assignment through an existing import evaluates its right-hand side and then throws a `TypeError`. An assignment reached before its lexical binding initializes or after a `const` binding initializes also throws in CoreJS. Placement still requires writes to source module bindings to stay with their declarations. Destructuring and global assignments remain outside the linked lowering.

Arithmetic and bitwise compound assignments read the target before the right-hand side runs, apply the corresponding binary operator, and return the value they write. This includes `+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`, and `>>>=`. The logical assignments `&&=`, `||=`, and `??=` read the same kinds of targets and short-circuit both the right-hand side and the write. Their anonymous functions receive the same identifier name as with plain assignment. Prefix and postfix `++` and `--` convert a modeled primitive to a number, write the updated number, and return the new or old number respectively. Compound assignments and updates target local or module bindings or properties.

Linked `delete` accepts dot and computed member expressions. It evaluates the receiver and key, then removes an own configurable property. A missing own property succeeds, while a nonconfigurable property throws in strict code. Reached deletion on unmodeled values or a computed key that requires object coercion stops the model.

Linked object literals may contain static and computed data properties, static methods and accessors, and spread elements. Property values use the same linked expression lowering as other initializers. CoreJS creates a fresh ordinary object and initializes its own data properties in source order. If one property's value throws, evaluation stops before the following property. A colon-form `__proto__` sets the prototype. A computed data property evaluates the key before its value and defines an own property even when the key is `"__proto__"`. Computed methods, accessors and values that take their function or class name from the computed key still require runtime name inference. A directly anonymous function or arrow takes its name from the static key. An explicitly named function keeps its name.

Object spread evaluates its operand once and copies its enumerable own properties into the new object. Keys follow JavaScript enumeration order. Getters run with the source as their receiver and can change which later keys are copied. A nullish value, number, or boolean contributes no keys. A string contributes its indexed UTF-16 code units within the Basic Multilingual Plane. Copying a host value, an engine error, or a string containing a supplementary character yields an unknown result.

A logical expression in a static data property, such as `{mode: config.mode || "default"}`, evaluates its left operand first. Only the selected operand supplies the property value, and an abrupt completion stops construction of the record.

A linked `void` expression evaluates its operand and returns `undefined` on normal completion. Side effects and thrown exceptions from the operand are observable.

Linked dense array literals evaluate their elements left to right. At module scope and inside functions, the checker records array spread elements and the bindings their operands reference. If initialization reaches such a literal, CoreJS stops with an unknown result because iterator behavior is outside the model. Array holes are rejected during lowering.

Untagged template literals use their cooked text and evaluate substitutions from left to right. CoreJS converts each primitive substitution to a string before evaluating the next expression. A linked initialization that needs object-to-primitive conversion reaches an unknown result and cannot certify.

Linked binary `+`, `-`, `*`, `/`, and `%` evaluate both operands in source order and then apply CoreJS primitive coercion. Addition concatenates when either primitive operand is a string. Numeric operations use binary64, including signed zero, infinities, NaN, and JavaScript’s signed remainder. Linked unary `+` and `-` convert a primitive operand to a number. The bitwise operators `~`, `&`, `|`, `^`, `<<`, `>>`, and `>>>` reduce numeric operands to 32 bits and mask shift counts to five bits. Unsigned right shift returns the unsigned result. Object-to-primitive conversion and host values reach an unknown result when evaluated. BigInt, Symbol, and exponentiation remain outside the linked fragment.

Linked `<`, `<=`, `>`, and `>=` evaluate operands in source order. CoreJS compares two strings by UTF-16 code unit and converts other primitive pairs to numbers. A numeric comparison with NaN is false. String literals, template text and property keys can contain supplementary Unicode characters. String length and indexed reads use UTF-16 code units. For example, `"A😀B".length` is 4 and `"A😀B"[3]` is `"B"`. A read that would return an isolated surrogate gives an unknown result. The lowerer rejects literal and template text containing an isolated surrogate. Reached object-to-primitive conversion remains unknown.

Linked expressions support `typeof`, `===` and `!==` on modeled operands. Strict comparisons evaluate both operands in source order and compare without coercion. For `==` and `!=`, each operand must be a string literal or a `typeof` expression, so the comparison uses string contents. Looking up a host global still requires a model of that binding, including inside `typeof`.

The linked fragment handles `try` inside function bodies with `catch`, `finally`, or both. A catch can bind one identifier or omit the binding. Inside functions, grouped `var`, `let` and `const` declarations with identifier binders initialize in source order. Each block has its own lexical scope, and an abrupt completion from `finally` takes precedence over a pending return or throw.
The state includes the heap and captured bindings of exported closures. The checker
runs both programs in a concrete `World.probe` to select an interpreter fuel witness.
When both finish normally, it compares the complete final state: heap objects and
closure code/environments, binding cells, allocation region, logged output, handed
object handles, and the probe's unit world state. Floating-point values are compared
by their IEEE bit patterns. If these probe states differ, `Ends.unique` proves that
the programs cannot finish with one shared state in every world, so the checker
refuses before generating the all-world proof. Matching probe states are only a
necessary check; Lean still proves the guarantee for every modeled world.

When both linked graphs run their units in the same order, Lean can reuse the
source’s normal execution. It checks the state after hoisting and compares each
unit’s original ordinal, full statement, receiver and free-name binding cells.
The entry’s public names must resolve to the same cells in both graphs. A mismatch reports the counts of extra, missing, and retargeted names, with up to three examples of each. An address `@region.index` identifies a live cell: top-level unit `i` allocates its bindings in region `i + 1`. For a retargeted name, the two addresses show which declarations to inspect in the original and candidate graphs.

The checker starts at 16 fuel and retries at 32 and 64 on fuel exhaustion. For a
top-level record initializer it can make one more attempt with 64 plus the largest
property count, capped at 2048. Exhausting the budget, unsupported syntax or an
all-world proof that fails to reduce yields a rejection. A successful comparison
reports `"scope":"linked-programs"`.
[AST certificate](lean/EsmSplit/CoreJS/LinkedCertificate/Proof.lean) · [Linked behavior theorem](lean/EsmSplit/CoreJS/LinkedBehavior.lean)

### Read a linked refusal

Linked refusals from AST lowering and the concrete probe include a `reason_code`. A `linked_input` refusal means the input failed linked lowering or linkage. `probe_fuel` means the search exhausted its budget. `probe_mismatch` means both probe executions finished with different states. Its optional `observation` reports a differing exported value or object alias when one is visible in exported cells or their own data properties. Paths list the export name and, for an own property, the property name.

### Measure certification

Set `SPLIT_PROOF_TIMINGS=1` to write phase names and elapsed milliseconds as JSON lines on stderr. Stdout carries the certification verdict. One Lean process reports a combined execution and AST correspondence time. Staged proofs report the subprocesses individually, including any attempted checkpoint before a fallback.

For public comparisons, use the wide-table reversal and the 48-reader linked module graph:

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/translation/linked:wide_table_reversal_test" "$p/translation/linked:module_graph_scale_test" \
  --test_env=SPLIT_PROOF_TIMINGS=1 --test_output=all --nocache_test_results
```

Compare the same fixture, proof mode and runner across revisions. Record peak memory separately from the phase times.

### Inspect an uncertified preview

`lowersplit --diagnostic bundle.mjs spec.json output` runs the structural checks and prints model diagnostics. Use `--plan <file>` to inspect module order, statement placement and conflicting pairs, or `--force` to emit a preview of a rejected plan. Successful diagnostic runs also produce uncertified previews. Experiments can pass `--corejs <program.json>` here to supply an external lowering by statement ID. [Move rules and proof inventory](docs/footprint_soundness.md)

## The example

Input, [`example/input/bundle.mjs`](example/input/bundle.mjs). It has 12 top-level statements, numbered 0–11:

```js
const PI = 3.14159; // 0
function square(x) {
  return x * x;
} // 1
const area = (r) => PI * square(r); // 2
function isEven(n) {
  return n === 0 || isOdd(n - 1);
} // 3
function isOdd(n) {
  return n !== 0 && isEven(n - 1);
} // 4
const registry = []; // 5
function register(name, fn) {
  registry.push(name);
  return fn;
} // 6
register("area", area); // 7
const label = (s) => "<" + s + ">"; // 8
register("label", label); // 9
console.log(label("circle"), area(2), isEven(10)); // 10
console.log(registry.join(",")); // 11
```

Spec, [`example/spec.json`](example/spec.json). A selector is a declared name, or `#n` for a statement that declares nothing:

```json
{
  "entry": "main.mjs",
  "modules": {
    "utils.mjs": ["PI", "square", "area", "isEven", "isOdd", "label"],
    "registry.mjs": ["registry", "register"],
    "plugins.mjs": ["#7", "#9"],
    "main.mjs": ["#10", "#11"]
  }
}
```

Two optional keys set the order the modules run in. `"order": ["a.mjs", …]` is an import priority: the entry imports every module in that order. That does not fix the order of evaluation, because a module runs after the modules it imports. `"sequential": true` does: the modules run in exactly `order` (or, without `order`, in the order of their first statements), the entry last. The planner then makes a module that an earlier one mentions import that earlier module first, and every module between ([`Split.lean`](lean/EsmSplit/Split.lean), `nest`). `Valid` checks the resulting plan like any other.

The `render` target writes these files to the output directory you pass it. Statements are copied verbatim.

```js
// main.mjs
import { label, area, isEven } from "./utils.mjs";
import { registry } from "./registry.mjs";
import "./plugins.mjs";

console.log(label("circle"), area(2), isEven(10));
console.log(registry.join(","));

// plugins.mjs
import { area, label } from "./utils.mjs";
import { register } from "./registry.mjs";

register("area", area);
register("label", label);

// registry.mjs
const registry = [];
function register(name, fn) {
  registry.push(name);
  return fn;
}

export { register, registry };

// utils.mjs
const PI = 3.14159;
function square(x) {
  return x * x;
}
const area = (r) => PI * square(r);
function isEven(n) {
  return n === 0 || isOdd(n - 1);
}
function isOdd(n) {
  return n !== 0 && isEven(n - 1);
}
const label = (s) => "<" + s + ">";

export { area, label, isEven };
```

The bundle runs statements in order `0 1 2 … 11`. The split runs `0 1 2 3 4 8 5 6 7 9 10 11`.
`label` (8) now runs before `registry`, `register` and the first registration. The proof says why that is fine.
Both programs print `<circle> 12.56636 true` and `area,label`.

## What is proved

The structural theorem below applies to a plan and its stated footprints. For the
artifact guarantee of the normal splitter, see [CoreJS certification](#request-a-corejs-guarantee).

[`lean/EsmSplit/Correct.lean`](lean/EsmSplit/Correct.lean):

```lean
def Correct (B : Bundle) (P : Plan) : Prop :=
  (P.execOrder.Nodup ∧ ∀ x, x ∈ P.execOrder ↔ x < B.length)      -- every statement runs exactly once
  ∧ Hygienic B P                                                   -- every name means what it meant
  ∧ ∀ (V W : Type) (M : Meaning V W) (s0 : St V W),                -- whatever the statements mean:
      B.exec M s0 P.execOrder = B.exec M s0 (List.range B.length)  -- same values, same world

theorem split_correct {B : Bundle} {S : Spec} {P : Plan} (h : Valid B S P) : Correct B P
```

For any statement semantics satisfying `Meaning`, an accepted plan executes
each statement once, preserves binding resolution, and leaves the same
top-level values and world state.

`Valid` is a conjunction of decidable checks ([`Plan.lean`](lean/EsmSplit/Plan.lean)):

| Check                       | Says                                                                                                                  |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `Faithful`                  | each statement sits in exactly the module the spec names; no selector is dangling                                     |
| `Linkable`                  | each import names a binding another module declares and exports; no name is bound twice; each glue export is declared |
| `ExportsUnique`             | no module exports a name twice: its glue keeps clear of the names its own `export` statements publish                 |
| `Hygienic`                  | each name a statement mentions resolves, through imports, to the declaration it had in the bundle                     |
| `AssignsHome`               | code that assigns a name sits in the module that declares it (ES imports are read-only)                               |
| `Permutes`                  | the split runs every statement exactly once                                                                           |
| `OrderOk`                   | in the order the split runs them, conflicting statements keep their bundle order                                      |
| `UniqueDefs`, `ReadsClosed` | side conditions: one flat scope; read sets are closed under calls                                                     |

A statement **binds** the names it declares and the names it may assign (`writes`: reassigned variables, objects whose contents it changes,
including what the functions it calls change; unknown code lists them all). These named mutable things are _cells_.
A statement may also read names it does not mention, through a function it calls that it reached through an object (`deps`); they order it like `eager` names do.
Two statements **conflict** ([`Bundle.lean`](lean/EsmSplit/Bundle.lean)) if both have effects,
or one reads a name the other binds (either direction), or both bind the same name.
A hoisted `function` declaration never conflicts: it exists before any code runs.
Everything else may be reshuffled freely. `assigns` is a different list: where the code that assigns a name physically sits, which decides where it must live (`AssignsHome`).

The order the split runs in is computed by `Plan.visit`: a model of ES module evaluation.
It goes depth first over `import`s in source order, runs each module once, and skips a module that is already being entered.
That last rule cuts import cycles.

[`Semantics.lean`](lean/EsmSplit/Semantics.lean) proves pairwise commutation of
nonconflicting statements (`step_comm`) and lifts it to the plan's execution
order (`exec_reorder`). The generated [example certificate](lean/EsmSplit/Instance.lean)
uses `decide +kernel` for `Valid` and applies `split_correct`.
`lean:axioms_test` checks axiom dependencies;
`leanchecker` replays the declarations.

## What is not proved

The structural theorem assumes footprint soundness for the supplied program.
[`Lower.lean`](lean/EsmSplit/Lower.lean) scans declarations, eager and deferred
names, and assignment sites. [The Effects analysis](effects/README.md) supplies
writes, indirect reads and world effects under its printed premises.
`lowersplit --diagnostic … --lean` uses both for the checked-in example.

`Meaning` models one flat environment. Using that theorem for JavaScript assumes
that `Hygienic` justifies flattening and that `Plan.visit` agrees with module
evaluation. The [module-order experiment](experiments/README.md#10-does-the-models-module-order-match-node)
compares that order with Node. In a structural preview, external imports count as
preexisting bindings. The diagnostic admission check keeps source imports and
exports in the entry. The emitter writes source imports before generated imports. Preserving external initialization order assumes synchronous dependencies
and unchanged resolution of relative specifiers. Diagnostic glue exports may widen
the entry's public surface.

For the normal splitter, the [CoreJS artifact guarantee](#request-a-corejs-guarantee)
states the parser, binding annotations, printer and model premises. The
[footprint proof inventory](docs/footprint_soundness.md#current-proof-inventory)
lists the Effects summaries already covered by separate CoreJS certificates.
For validation of a separately authored tree, see the
[translation validator's renaming premise](translation/README.md#trusted).

## Controls

Specs and plans that must fail. Each failure is a Lean theorem (`¬ OrderOk …`, checked by the kernel) and a real failure in Node.
`lean:example_test` checks the Node behavior.

| Control                                                                                                      | Failing check | Node, when written anyway                                          |
| ------------------------------------------------------------------------------------------------------------ | ------------- | ------------------------------------------------------------------ |
| [`effect_order`](example/controls/effect_order.json): last `console.log` moved into `registry.mjs`           | `OrderOk`     | prints `"\n<circle> 12.56636 true\n"`                              |
| [`cycle_tdz`](example/controls/cycle_tdz.json): `label` in `main.mjs`, which `plugins.mjs` needs and imports | `OrderOk`     | `ReferenceError: Cannot access 'label' before initialization`      |
| [`claimed_twice`](example/controls/claimed_twice.json): two modules pick `label`                             | `Faithful`    | `SyntaxError: Identifier 'label' has already been declared`        |
| `missing_import` (a buggy splitter forgets one import)                                                       | `Hygienic`    | `ReferenceError: registry is not defined`                          |
| `dropped_module` (nobody imports `plugins.mjs`)                                                              | `Permutes`    | prints `"<circle> 12.56636 true\n\n"`; the registrations never run |

[`example/variants/cycle.json`](example/variants/cycle.json) must pass: `isEven` and `isOdd` in separate modules form a real import cycle, which is fine because function declarations exist before any module runs.
`cycle_tdz` is the same cycle with a `const` in it, and fails.

[`Controls.lean`](lean/EsmSplit/Controls.lean) also runs a concrete `Meaning` in the kernel. The accepted splits end in the bundle's state. The two order-violating controls end in a different one.

## Checking the trusted parts

These are tests, not proofs. The Bazel target `fuzz:render_fuzz` runs [`fuzz/fuzz.py`](fuzz/fuzz.py):
510 random placements of the 12 statements, each rendered and run in Node.

- 157 accepted. All 157 print what the bundle prints. 95 of them run statements in a different order than the bundle.
- 353 rejected. 325 really break in Node. 28 are harmless: the checker is conservative.

[`fuzz/mutate.py`](fuzz/mutate.py) weakens the trusted logic and reruns the fuzzer (400 placements per mutant):

| Mutant                                                     | Result                                                                                                |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| drop the effect-order conflict                             | caught (18 wrong accepts)                                                                             |
| drop the read-after-declare conflict                       | caught (87)                                                                                           |
| run imports in reverse order                               | caught (4)                                                                                            |
| drop the declare-after-read conflict                       | survives: this bundle reads nothing before its declaration. It matters for `var`.                     |
| no transitive reads (the read set is just the eager names) | caught by a proof: the build fails, because `reads_closed` no longer holds for the changed definition |
| un-mark a module after it runs                             | survives, still sound: `Permutes` rejects the double run                                              |

Running `example/output/` in Node also found a bug in the first printer (no `export` lines). The proof does not cover the printer.

## Further reading

- [Production acceptance and proof work](PLAN.md)
- [Structural checker experiments and Node comparisons](experiments/README.md)
- [Historical scale measurements](experiments/TIMINGS.md)
- [Lessons from the experiment](docs/lessons_learned.md)
- [Excalidraw replay and browser targets](integration/excalidraw/README.md#strict-certification-of-the-scroll-module)
