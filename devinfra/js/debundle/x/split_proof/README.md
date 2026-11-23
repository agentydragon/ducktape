# Splitting a bundle into ES modules, with a Lean proof

A flat JavaScript bundle goes in. A spec says which top-level statement goes to which module.
ES modules come out, and Lean checks that the split is a valid reshuffle.

This is an experiment. It does not use the `debundle` code; nothing here imports it.

[`SUPPORT.md`](SUPPORT.md) lists what a debundler built on this has to support (the syntax and the standard and host features that the real inputs use) and what it does not (a top-level `await`, a name declared twice in one list, and what the tools refuse today). It is a requirement, not a claim that the experiment meets it.

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
| Fuzz the example renderer                                    | `bb run "$p:render_fuzz" -- 600 1`                                                    |
| Check translation fixtures and effect cases                  | `bbr test "$p/translation:fixtures_test" "$p/translation:effects_cases_test"`         |
| Run a larger translation fuzz sample                         | `bb run "$p/translation:translation_fuzz" -- --count 150 --seed 1 --require-accepted` |
| Run the extended CoreJS comparison                           | `bbr test "$p/translation:corejs_differential_sweep_test"`                            |
| Run the debundle case corpus                                 | [Corpus commands](debundle_cases/README.md#run-it)                                    |
| Lower public library bundles and check their splits          | [Bundle commands](experiments/README.md#6-lower-the-ast-into-lean)                    |

To refresh the checked-in example instance after changing the inputs:

```sh
dir=devinfra/js/debundle/x/split_proof
bb build "//$dir:generated_instance" --remote_download_outputs=toplevel
cp "bazel-bin/$dir/generated/Instance.lean" "$dir/lean/EsmSplit/Instance.lean"
bbr test "//$dir/lean:instance_freshness_test"
```

Acorn supplies an unsplit module AST and eslint-scope annotates its binding occurrences. Lean enumerates source ordinals from the module body. It gives each declarator of a top-level `var`, `let` or `const` its own ordinal, including under a named export, and keeps each destructuring pattern together. Lean assembles an ESTree `Program` for each output file, including the imports and exports that connect the modules. Astring prints these trees as JavaScript. Functions, classes and methods retain their original source spelling because JavaScript exposes it through `Function.prototype.toString`. Unversioned input dumps are upgraded by reparsing the included source and checking its annotations.

For a closed module containing only identifier declarations, the checker also handles `Declaration extraction`. Supported initializers include primitive literals, binding reads, unary minus on a number literal, and logical negation of any supported initializer (for example, `const offset = -42, visible = !!offset;`). It evaluates the original declaration groups by allocating the entire module’s bindings before running the initializers in order. Lean proves that evaluating these groups gives the same CoreJS result as evaluating their extracted units, including if an initializer throws. [Declaration certificate](lean/EsmSplit/CoreJS/DeclarationCertificate.lean)

## Request a CoreJS guarantee

Pass the source module, placement spec and a fresh output directory to the normal splitter command. Its default request is `normal-completion`; an order-preserving split may prove the stronger `all-outcomes` guarantee too.

```sh
bb run //devinfra/js/debundle/x/split_proof/lean:lowersplit -- bundle.mjs spec.json output
```

For a module loaded as a browser page entry, you can pass `--host-contract browser.json` with `{"profile":"browser-page"}` on emission and on `--check-output`. This lets the linked proof assume that evaluating an ambient `typeof window` returns `"object"` without observable effects throughout initialization. Lean still resolves a local or imported binding named `window` through its actual cell. The verdict lists the selected profile and premises. A bare read of the ambient `window`, a member access, or a host call requires additional semantics. The browser-page profile also allows deferred construction of selected ambient constructors (`Date`, `Error`, `RegExp`, `Set`, and `URL`). The linked model retains the constructor name and arguments. If initialization reaches that lookup, it reports an unknown result before evaluating the arguments, including inside a `try` block. Carrying the operation in an unexecuted branch uses no assumption about constructor behavior. The caller is responsible for choosing a profile that holds in the deployment.

The command prints one JSON verdict. A successful split reports `status: "certified"` after Lean certifies the output module ASTs and the printer writes them to the requested directory. The default `normal-completion` request means that, for each modeled world and successful final state, the bundle reaches that state exactly when the split does. It does not say that either program completes normally; in the closed-declaration case, both may throw. The linked-program proof additionally establishes that both programs initialize normally. The checker reports the strongest available guarantee, so a default request may report `all-outcomes` for a supported order-preserving split. Pass `--require all-outcomes` to insist on that stronger guarantee. A rejected request exits nonzero with `status: "rejected"`, a null guarantee and a reason.

Lean certifies the original annotated ESTree against the exact map of output module ASTs it hands to Astring, including generated imports and exports. Only then does the splitter print and publish the directory. The JavaScript guarantee assumes that Acorn and eslint-scope describe the source faithfully, that the printer preserves the certified trees, and that CoreJS models the supported syntax. To inspect an existing directory, pass `--check-output`. This certifies the same trees, then parses the files and compares their syntax, callable source spelling and module names with the expected output. Copied providers must match their source text. [Declaration correspondence theorems](lean/EsmSplit/CoreJS/DeclarationArtifacts.lean)

For a module that calls functions or imports named bindings from relative JavaScript files, use
the normal three-positional command with the original source path. The splitter parses
reachable `.mjs` and `.js` providers as ES modules and copies them alongside the split.
Lean lowers the original and generated module ASTs to CoreJS, checking statement placement, live binding addresses and the entry’s exports. The kernel checks a theorem about the two concrete CoreJS programs. The generated certificate then runs a proof-producing Lean check on the same in-memory AST data and plan to establish that they lower to those particular programs, including their deferred closure bodies. Both programs must initialize normally in the same final state for every modeled world.
A named export list such as `export { local as publicName }` can publish an imported binding.
Imports can follow a chain of these aliases to the declaration's live cell. A chain that
cycles without reaching a declaration fails to link. Lean checks that each entry export
resolves to the same live cell in both graphs. Source-bearing re-exports such as
`export { name } from "./provider.mjs"` are outside this fragment.

Ordinary top-level function declarations initialize when the modules link. A call can reach a declared function before its statement executes, while its closed-over lexical bindings still observe their own initialization order. The checker uses the same scope correspondence for recursive references and deferred function bodies. Nested declarations and async and generator functions remain outside this fragment.

Named parameters can have defaults such as `(x = 1, y = x) => y`. Missing and `undefined` arguments use the initializer. A `null` argument binds directly. Parameters initialize left to right and a read of the current or a later parameter before initialization throws, including inside `typeof`. Body lexical declarations are in a scope inside the parameter environment. The linked fragment refuses a body `var` whose name repeats a parameter or occurs free in an initializer. The function's `length` counts only parameters before the first default. Functions with defaults require identifier parameters, and implicit `arguments` remains outside the linked fragment.

Linked ordinary functions and arrows accept nonempty flat object patterns in parameters and local `var`, `let`, and `const` declarations. For example, `function read({x, label: name})` takes one argument and reads `x` before `label`. A local declaration evaluates its initializer once. Both forms use CoreJS property lookup, including the receiver for a getter, and stop at the first abrupt read. Keys may be identifiers or string literals. Module-level patterns, nested targets, defaults, rest, and computed keys are outside this lowering. An empty pattern still checks its argument for null or undefined and is also excluded.

In linked programs, conditional expressions and `if` statements inside functions evaluate the test once and run the selected branch with the state left by the test. Both branches must lower to CoreJS, including code that initialization never executes. Branch blocks have their own lexical scope, and `var` declarations in either branch belong to the enclosing function. A false test with no `else` continues to the following statement.

Linked functions can also use `switch` cases ending in `return`, `throw`, or an unlabeled `break` before the next clause. The discriminant is evaluated once. Case tests run in source order and match by strict equality. A trailing `default` runs when no test matches. The checker refuses fallthrough, a nontrailing default, nested breaks, and lexical declarations shared by the clauses. Independently braced case bodies keep their own lexical scopes.

Linked sequence expressions evaluate their operands from left to right, stop at the first abrupt completion, and return the final operand's value. A sequence used as a call's callee supplies `undefined` as `this`, including when its last operand reads a member.

Linked member expressions support dot access and computed keys with modeled primitive
property names. A member call evaluates its receiver and key once, reads the callee before
evaluating arguments, and passes the receiver as `this`. Ordinary functions have the call's
receiver and arrows capture the surrounding `this`. Private names and `super` are outside the linked fragment.

A single optional member read `binding?.key` or `binding?.[key]` can use a resolved lexical binding, including an import. A null or undefined base yields `undefined` without computing a key or invoking a getter. Otherwise the read captures the base before evaluating the key and keeps that base as the getter's receiver. A single named method call `receiver?.method(args)` skips lookup and arguments when the receiver is null or undefined. The form `receiver.method?.(args)` performs ordinary property lookup first and skips the arguments if the returned method is null or undefined. Both forms evaluate the receiver once and preserve it as `this`. Bare optional calls, computed optional method calls, combined optional receiver and callee, continued chains, and chain expressions used directly as call targets still receive a refusal.

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

Linked object literals may contain distinct identifier or string keys. Property values use the same linked expression lowering as other initializers. CoreJS creates a fresh ordinary object and initializes its own data properties in source order. If one property's value throws, evaluation stops before the following property. Computed property definitions, `__proto__`, methods, accessors, and spread are outside this fragment. A directly anonymous function or arrow takes its name from the static key. An explicitly named function keeps its name.

A logical expression in a static data property, such as `{mode: config.mode || "default"}`, evaluates its left operand first. Only the selected operand supplies the property value, and an abrupt completion stops construction of the record.

A linked `void` expression evaluates its operand and returns `undefined` on normal completion. Side effects and thrown exceptions from the operand are observable.

Linked dense array literals evaluate their elements left to right. Inside function bodies the checker also records array spread elements and the bindings their operands capture. If initialization reaches such a literal, CoreJS stops with an unknown result because iterator behavior is outside the model. Top-level array spread and array holes are rejected during lowering.

Untagged template literals use their cooked text and evaluate substitutions from left to right. CoreJS converts each primitive substitution to a string before evaluating the next expression. A linked initialization that needs object-to-primitive conversion reaches an unknown result and cannot certify.

Linked binary `+`, `-`, `*`, `/`, and `%` evaluate both operands in source order and then apply CoreJS primitive coercion. Addition concatenates when either primitive operand is a string. Numeric operations use binary64, including signed zero, infinities, NaN, and JavaScript’s signed remainder. Linked unary `+` and `-` convert a primitive operand to a number. The bitwise operators `~`, `&`, `|`, `^`, `<<`, `>>`, and `>>>` reduce numeric operands to 32 bits and mask shift counts to five bits. Unsigned right shift returns the unsigned result. Object-to-primitive conversion and host values reach an unknown result when evaluated. BigInt, Symbol, and exponentiation remain outside the linked fragment.

Linked `<`, `<=`, `>`, and `>=` evaluate operands in source order. CoreJS compares two strings by UTF-16 code unit and converts other primitive pairs to numbers. A numeric comparison with NaN is false. Supplementary Unicode characters are supported in string literals used directly as comparison operands. Isolated UTF-16 surrogates are refused. Reached object-to-primitive conversion remains unknown.

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

The checker starts at 16 fuel and retries at 32 and 64 on fuel exhaustion. For a
top-level record initializer it can make one more attempt with 64 plus the largest
property count, capped at 1024. Exhausting the budget, unsupported syntax or an
all-world proof that fails to reduce yields a rejection. A successful comparison
reports `"scope":"linked-programs"`.
[AST certificate](lean/EsmSplit/CoreJS/LinkedCertificate.lean) · [Linked behavior theorem](lean/EsmSplit/CoreJS/LinkedBehavior.lean)

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

[`lean/EsmSplit/Correct.lean`](lean/EsmSplit/Correct.lean):

```lean
def Correct (B : Bundle) (P : Plan) : Prop :=
  (P.execOrder.Nodup ∧ ∀ x, x ∈ P.execOrder ↔ x < B.length)      -- every statement runs exactly once
  ∧ Hygienic B P                                                   -- every name means what it meant
  ∧ ∀ (V W : Type) (M : Meaning V W) (s0 : St V W),                -- whatever the statements mean:
      B.exec M s0 P.execOrder = B.exec M s0 (List.range B.length)  -- same values, same world

theorem split_correct {B : Bundle} {S : Spec} {P : Plan} (h : Valid B S P) : Correct B P
```

"Equivalent-ish" means this: for every way of giving the statements a meaning that respects their footprints,
the split leaves the same value in every top-level name and the same world (heap, console, …) as the bundle.
It is not "the programs are equivalent". JavaScript is never formalized.

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

The semantic core is [`Semantics.lean`](lean/EsmSplit/Semantics.lean). The proofs take about 100 lines.
`step_comm`: two statements that do not conflict can run in either order.
`exec_reorder`: take the last statement, slide it to the end past everything it does not conflict with, and induct.
No trace theory is needed.

The example itself is checked by the kernel, in [`Instance.lean`](lean/EsmSplit/Instance.lean) (generated):

```lean
theorem plan_valid : Valid bundle spec plan := by decide +kernel
theorem plan_correct : Correct bundle plan := split_correct plan_valid
```

`decide +kernel` runs the decision procedure inside Lean's kernel, in about a second.
The main development does not use `native_decide`, so the compiler is not trusted there. (`experiments/` has it as the last rung of a strategy ladder, § 8.)
`lake env lean EsmSplit/Audit.lean` shows every theorem rests on at most `propext`, `Classical.choice` and `Quot.sound`: Lean's three standard axioms. No `sorry`.
(Before the checks were indexed with `Std.TreeMap`, two axioms were enough. The `TreeMap` lemmas in Lean's library use `Classical.choice`.)
`leanchecker` replays all declarations through the kernel again. It does not audit axioms; `#print axioms` does.

## What is not proved

1. **Footprints.** [`Effects.lean`](lean/EsmSplit/Effects.lean) runs the bundle's top level once over abstract values and reports, per statement, whether it touches the world (`effect`), which names it may change (`writes`) and which names it reads through functions it calls but does not mention (`deps`).
   [`Lower.lean`](lean/EsmSplit/Lower.lean) scans the same statement for what it declares, mentions eagerly or only inside function bodies, and assigns.
   Code the interpreter can see is run, standard functions come from a table of summaries, and anything else is a call into the world: an effect, which keeps its order against every other effect.
   A statement that touches only objects the program made is not an effect. One that touches the world also counts everything its closures mention as eager, since the world may call them.
   The checker trusts the interpreter's rules and its printed premises ([`effects/`](effects/README.md): the intrinsics are not patched, evaluation completes, no other chunk runs meanwhile, and optionally that the world adds no accessors or hooks to program objects). Both are tested against Node, neither is proved:
   golden cases with reorders, fuzzers over random bundles and splits, a mutation harness that removes one rule at a time. A wrong rule shows up as a wrong accept.
   A `var` inside a top-level block declares a module-scope name ([`debundle_cases/`](debundle_cases/README.md)).
   [`extract/extract.mjs`](extract/extract.mjs), which writes the example's Lean instance, has footprint rules of its own (a call is an effect) and models a much smaller syntax: it rejects classes, destructuring, reassigning a top-level binding, `eval`, `import.meta` and async. Read [`Instance.lean`](lean/EsmSplit/Instance.lean) to audit its output by eye; it is 12 lines. The legacy `lowersplit --diagnostic … --lean` instance writer uses the interpreter's footprints, but does not certify emitted artifacts.
2. **The frame assumption.** A statement without effects depends only on the values it may read. A statement with effects may also read and change the world. `Meaning` has exactly this type. Real JavaScript is assumed to fit.
   "May read" is the closure of reads through function bodies (`Bundle.reads`). Running a statement binds the names it declares and the names in `writes` to its value; a statement must list every name it may assign.
3. **The ES order model**, `Plan.visit`: 10 lines. It matches Node on 500 random import graphs, 262 of them with cycles ([`experiments/README.md`](experiments/README.md) § 10). That is a test, not a proof.
4. **Hygiene gives one flat environment.** The semantic theorem uses one environment keyed by name. `Hygienic` is the check that makes that fair. The join is argued here, not mechanized.
5. **Emission.** Lean builds the output module ASTs and Astring prints them. A binding that the glue exports under another name is imported back under its own. The standalone kernel example renders its embedded statement strings with [`FixtureRender.lean`](lean/Experiments/FixtureRender.lean).
6. **The lowering front end around the model.** Lean checks the generated module trees before printing. The shared parser, scope annotations and printer are part of the trusted boundary. The [Lean lowering](lean/EsmSplit/CoreJS/FromESTree.lean) computes CoreJS directly from supported source syntax. Its agreement with JavaScript semantics is tested against Node. An external `--corejs` program and the legacy `--lean` instance writer are available only under `--diagnostic`; neither certifies a split.
   - **One statement per declarator.** Lean splits a top-level `var`, `let` or `const` list into one AST node per declarator, including under `export`. The declaration extraction theorem relates these units to the original lists for the supported CoreJS subset.
   - **Imports and exports stay in the entry.** The normal command rejects a split that puts an `export` or an `import` statement outside the entry module: the entry's exports are the program's interface, and its imports load the outside modules. Diagnostic mode may still write a preview after this rejection, but it cannot certify it. Moving the rule into `Valid` is a small known step: a field per statement, one more conjunct, one more equivalence proof.
   - **Outside modules run first.** The model has no outside modules. An `import` statement declares nothing and does nothing in it, and an imported name is a global: the same binding in every module. [The output AST builder](lean/EsmSplit/ESTreeEmit.lean) places the entry's `import` and `export … from` statements before its glue imports, in their order, so the outside modules run before every statement and in the bundle's order. Another module gets an `import` line of its own for the outside names it mentions. That this keeps the order is argued, not proved. It assumes that no outside module awaits at its top level, and that the modules are written where the bundle was, so a relative specifier names the same file. `export * from` is rejected.
   - **What an `export` statement publishes** (`Stmt.exports`) is read from its AST by the front end. `ExportsUnique` and `Linkable` rest on it. A glue export adds a name to the entry's interface (`m$1` when `m` is taken); nothing says the interface is otherwise unchanged ([`PLAN.md`](PLAN.md) step 4).

   A rule that code assigning a name stays with its declaration used to live here. It is now `AssignsHome`, a conjunct of `Valid` and of `Sound`. It was missing from the translation checker until a probe showed Node throwing on an accepted split (`translation/fixtures/assign`).

7. **The translation checker's front end.** [`translation/frontend.mjs`](translation/frontend.mjs) trusts `acorn` for the parse and `eslint-scope` for which variable each identifier names.
   Lean checks everything after that, but "renaming preserves meaning" is assumed, not proved, and it has an edge: a program that reads `fn.name`, calls `Function.prototype.toString` or prints a function sees the new spelling.
   The checker refuses what a move or a rename visibly changes (`import.meta` except `new URL("/abs", import.meta.url)`, a direct `eval`, a top-level `await`).
   Its soundness rests on a list of the ways a move can change behavior, so it is tested against Node by a [fuzzer](translation/README.md#fuzzer), which found two holes the fixtures had not: a pure read moving across a write, and `import.meta`.

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

These are tests, not proofs. The Bazel target `:render_fuzz` runs [`fuzz/fuzz.py`](fuzz/fuzz.py):
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

## Limits

- **Syntax.** The front end handles a small subset. It fails loudly outside it.
- **Conservative.** Any call is an effect. `const banner = shout("x")` cannot move past a `register(...)`. A `/* @__PURE__ */` convention would lift this.
- **Sufficient, not necessary.** A rejected spec may be fine.
- **Scale.** Compiled, the whole check takes 0.04 s for lodash (670 statements), 0.2 s for zod (1,072) and 10 s for 21,440 statements
  (32 renamed copies of lodash, 119 MB). Before the checks were indexed, lodash took 54 s and minified zod did not finish in 240 s.
  The pair loop of `OrderOk` is still quadratic: 7 of those 10 s.
  In the kernel it is slow and needs memory: `decide +kernel` takes 23 s for 96 statements, 45 s for 144; at 288 it hit my 12 GB limit (numeric names would make it 1.7× faster, not 10×).
  `native_decide` runs the same decision procedure compiled and trusts Lean's compiler (2.3 s for lodash). The theorem does not depend on size.
  All times and conditions: [`experiments/TIMINGS.md`](experiments/TIMINGS.md).
- **Reassignment.** The JavaScript extractor `extract.mjs` rejects reassigned `let`/`var` top-level bindings. The Lean front end accepts them: writers are ordered as cells and must stay with the declaration (`AssignsHome`), because ES imports are read-only.
- **Outside the accepted subset:** [`SUPPORT.md`](SUPPORT.md) § Not supported.
- **Exceptions.** The model has none. Order is kept for effects, TDZ reads and reads of undeclared globals. Those are the only throws the front end allows an inert statement.

## Going further

[`docs/lessons_learned.md`](docs/lessons_learned.md) records what broke and what the measurements said; [`PLAN.md`](PLAN.md) has the direction that follows from it.

[`experiments/README.md`](experiments/README.md):

1. the `Sound` plan predicate and its correctness theorem;
2. a real semantics for a tiny Turing-complete JS-like language that derives the frame assumption (337 lines);
3. a two-tier scheme for automatic and hand-written commutation proofs;
4. what `grind` can write;
5. how far the check scales, compiled and in the kernel;
6. lowering the AST into Lean;
7. 2,892 real splits (halves, single cuts, blocks, random placements), each run in Node;
8. how a proof is produced, with a strategy ladder;
9. what already exists and what I would reuse;
10. the module-order model against Node;
11. debundle's 207 e2e cases through the checker, with causes for the ones that do not work ([`debundle_cases/`](debundle_cases/README.md), plan in [`PLAN.md`](PLAN.md)).
12. translation validation: is an output tree the bundle, renamed and reshuffled? ([`translation/`](translation/README.md)).
13. footprints by abstract interpretation ([`effects/`](effects/README.md)).
14. an executable model of a core of JavaScript, tested against Node, and the first footprint rule proved in it.

## Files

| Path                                        | Role                                                                                                                                                                                       |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `lean/EsmSplit/`                            | active checker, operational Lean code, and the CoreJS model                                                                                                                                |
| `lean/Experiments/`                         | historical Lean experiments: mini-JS semantics, two tiers, and proof automation                                                                                                            |
| `lean/EsmSplit/Bundle.lean`                 | statement footprints, read closure (worklist over a `TreeMap` index, proved least and closed), conflict                                                                                    |
| `lean/EsmSplit/Semantics.lean`              | abstract semantics and the reorder theorem                                                                                                                                                 |
| `lean/EsmSplit/Plan.lean`                   | spec, plan, ES order model, the checks as plain statements                                                                                                                                 |
| `lean/EsmSplit/Fast.lean`                   | how each check is decided: table-based algorithms, each proved equal to its statement                                                                                                      |
| `lean/EsmSplit/Split.lean`                  | the splitter `mkPlan` (untrusted)                                                                                                                                                          |
| `lean/EsmSplit/Correct.lean`                | `Correct` and `split_correct`                                                                                                                                                              |
| `lean/EsmSplit/ESTreeEmit.lean`             | builds output module ASTs                                                                                                                                                                  |
| `lean/EsmSplit/Instance.lean`               | generated: footprints, spec, kernel-checked theorems                                                                                                                                       |
| `lean/EsmSplit/Controls.lean`               | buggy-splitter plans, semantic non-vacuity                                                                                                                                                 |
| `lean/EsmSplit/Audit.lean`                  | `#print axioms`                                                                                                                                                                            |
| `lean/Main.lean`                            | the `render` executable                                                                                                                                                                    |
| `extract/`                                  | JavaScript front end for the example (`extract.mjs`)                                                                                                                                       |
| `example/`                                  | the 12-statement bundle, its spec, one variant that must pass, three controls that must fail                                                                                               |
| `fuzz/`                                     | fuzzer and mutation test                                                                                                                                                                   |
| `lowering/`, `es_order/`, `debundle_cases/` | JavaScript front ends and regression suites used by the Bazel commands above                                                                                                               |
| `translation/`, `effects/`, `model/`        | Translation validator, footprint analysis cases, and the CoreJS differential harness                                                                                                       |
| `experiments/`                              | [research notes](experiments/README.md) and [timings](experiments/TIMINGS.md); research scripts in `scaling/`, `splits/`, `proof_search/`, and `esmeta/`; raw timing records in `records/` |
