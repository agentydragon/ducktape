# Splitting a bundle into ES modules, with a Lean proof

A flat JavaScript bundle goes in. A spec says which top-level statement goes to which module.
ES modules come out, and Lean checks that the split is a valid reshuffle.

This is an experiment. It does not use the `debundle` code; nothing here imports it.

The intended replacement must support the JavaScript syntax actually used across all configured `gaffer-private` debundle inputs. Broader syntax support is welcome. This is a requirement for the replacement, not a claim that the experiment supports it today.

The available inputs already use the following syntax, so it belongs in that requirement:

- `var`, `let` and `const`; array and object destructuring, default parameters, rest parameters and spread elements. The cached CLI extraction also contains `using` and `await using`.
- Function declarations and expressions, arrow functions, async functions, generators and async generators; calls, `new`, `this`, `super` and `new.target`.
- Classes with `extends`, computed methods and properties, getters and setters, instance and static fields, private fields and methods, and static initialization blocks; object literals with computed keys, methods, getters and setters.
- `if`, `switch`, `for`, `for...in`, `for...of`, `for await...of`, `while`, `do...while`, `try`/`catch`/`finally`, `throw`, labeled statements, `break`, `continue`, `await` and `yield`.
- Template literals and tagged templates, regular-expression and BigInt literals, optional chaining (`?.`), nullish coalescing (`??`), conditional (`?:`) and sequence expressions.
- Binary `+`, `-`, `*`, `/`, `%`, `**`, `<`, `<=`, `>`, `>=`, `==`, `!=`, `===`, `!==`, `&`, `|`, `^`, `<<`, `>>`, `>>>`, `in` and `instanceof`; `&&`, `||`, `??`; unary `!`, `+`, `-`, `~`, `typeof`, `void` and `delete`; `++`, `--`, and assignments `=`, `+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`, `>>>=`, `&&=`, `||=` and `??=`.
- Named, default and namespace imports; named and default exports; dynamic `import()` and `import.meta`.

This list is a lower bound, not a complete grammar or an ECMAScript-version claim. A census on 2026-11-20, using `gaffer-private` commit `3ce58b883656` for the committed manifests, parsed 1,107 entries from seven Tana, Claude web and Platform Sessions snapshots, plus 2,256 entries from the Claude CLI extraction, using Acorn 8.19.0 with `ecmaVersion: "latest"`; all parsed without a syntax error. A rerun of the pinned extractor on the cached pinned CLI release, with the configured aliases, produced a byte-identical manifest and files. The generated Tana vendor replacement was also built and parsed; its SHA-256 is `751f15172a28e5c9712535dd568eadedbefbec7b3afb148eb2406d08cb8f0ea9`, and it added no syntax categories to this census. Package-root inputs and other configured fixtures or inputs outside these manifests were not inventoried. All files happened to parse in module grammar; that does not establish their actual host or module semantics. The census did not inspect lexical or regular-expression subfeatures, or runtime builtins. The requirement also covers syntax in configured inputs beyond this measured sample.

Run everything with `./run.sh` (add `--controls`, `--fuzz`, `--experiments`, `--lowering`, `--es-order` or `--splits`).
It needs `nix`, `node` and `npm`. Lean 4.35 and gcc come from `nixpkgs`. A clean build takes about 20 s, the first `./run.sh` about a minute.
`run.sh` copies each `package.json.in` to `package.json` before `npm ci`; the repository's CI treats a committed `package.json` as a change to the whole build graph.

Bazel generates the example's Lean instance from the bundle and spec, checks the proof and the
axioms in `EsmSplit/Audit.lean`, checks that the committed instance is up to date, and runs the
rendered modules and controls in Node:

```sh
bbr test //devinfra/js/debundle/x/split_proof/lean:axioms_test //devinfra/js/debundle/x/split_proof/lean:example_test //devinfra/js/debundle/x/split_proof/lean:instance_freshness_test
```

Bazel and Lake both read the Lean version from `lean/lean-toolchain`. The other experiments in
`run.sh` are not yet Bazel targets.

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

Output, `example/output/` (written by `./run.sh`, not committed). Lean writes the files. Statements are copied verbatim.

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

| Check                       | Says                                                                                               |
| --------------------------- | -------------------------------------------------------------------------------------------------- |
| `Faithful`                  | each statement sits in exactly the module the spec names; no selector is dangling                  |
| `Linkable`                  | each import names a real export of another module; no name is bound twice; each export is declared |
| `Hygienic`                  | each name a statement mentions resolves, through imports, to the declaration it had in the bundle  |
| `AssignsHome`               | code that assigns a name sits in the module that declares it (ES imports are read-only)            |
| `Permutes`                  | the split runs every statement exactly once                                                        |
| `OrderOk`                   | in the order the split runs them, conflicting statements keep their bundle order                   |
| `UniqueDefs`, `ReadsClosed` | side conditions: one flat scope; read sets are closed under calls                                  |

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

1. **Footprints.** [`Effects.lean`](lean/Experiments/Effects.lean) runs the bundle's top level once over abstract values and reports, per statement, whether it touches the world (`effect`), which names it may change (`writes`) and which names it reads through functions it calls but does not mention (`deps`).
   [`Lower.lean`](lean/Experiments/Lower.lean) scans the same statement for what it declares, mentions eagerly or only inside function bodies, and assigns.
   Code the interpreter can see is run, standard functions come from a table of summaries, and anything else is a call into the world: an effect, which keeps its order against every other effect.
   A statement that touches only objects the program made is not an effect. One that touches the world also counts everything its closures mention as eager, since the world may call them.
   The checker trusts the interpreter's rules and its printed premises ([`experiments/effects/`](experiments/effects/README.md): the intrinsics are not patched, evaluation completes, no other chunk runs meanwhile, and optionally that the world adds no accessors or hooks to program objects). Both are tested against Node, neither is proved:
   golden cases with reorders, fuzzers over random bundles and splits, a mutation harness that removes one rule at a time. A wrong rule shows up as a wrong accept.
   A `var` inside a top-level block declares a module-scope name ([`experiments/debundle_cases/`](experiments/debundle_cases/README.md)).
   [`extract/extract.mjs`](extract/extract.mjs), which writes the example's Lean instance, has footprint rules of its own (a call is an effect) and models a much smaller syntax: it rejects classes, destructuring, reassigning a top-level binding, `eval`, `import.meta` and async. Read [`Instance.lean`](lean/EsmSplit/Instance.lean) to audit its output by eye; it is 12 lines. `lowersplit --lean` writes a certificate from the interpreter's footprints instead.
2. **The frame assumption.** A statement without effects depends only on the values it may read. A statement with effects may also read and change the world. `Meaning` has exactly this type. Real JavaScript is assumed to fit.
   "May read" is the closure of reads through function bodies (`Bundle.reads`). Running a statement binds the names it declares and the names in `writes` to its value; a statement must list every name it may assign.
3. **The ES order model**, `Plan.visit`: 10 lines. It matches Node on 500 random import graphs, 262 of them with cycles ([`experiments/README.md`](experiments/README.md) § 10). That is a test, not a proof.
4. **Hygiene gives one flat environment.** The semantic theorem uses one environment keyed by name. `Hygienic` is the check that makes that fair. The join is argued here, not mechanized.
5. **The printer**, [`Render.lean`](lean/EsmSplit/Render.lean): imports, then statements, then exports.
6. **Two rules of the lowering front end that Lean does not check.** `lowersplit` rejects a split that puts an `export` statement outside the entry module (the entry's exports are the program's interface),
   and one where the entry's glue exports a name that the entry's own `export` statements already publish (`ExportNames`: a duplicate export, or one name for two bindings).
   The first was missing until the split experiment (`experiments/README.md` § 7) and the second until debundle's corpus (§ 11). When one fails, `lowersplit --lean` writes no certificate.
   A third rule, that code assigning a name stays with its declaration, used to live here. It is now `AssignsHome`, a conjunct of `Valid` and of `Sound`. It was missing from the translation checker until a probe showed Node throwing on an accepted split (`experiments/translation/fixtures/assign`).
   Moving the first into `Valid` is a small known step: a field per statement, one more conjunct, one more equivalence proof. The second needs a model of export names ([`PLAN.md`](PLAN.md) step 4).

7. **The translation checker's front end.** [`experiments/translation/frontend.mjs`](experiments/translation/frontend.mjs) trusts `acorn` for the parse and `eslint-scope` for which variable each identifier names.
   Lean checks everything after that, but "renaming preserves meaning" is assumed, not proved, and it has an edge: a program that reads `fn.name`, calls `Function.prototype.toString` or prints a function sees the new spelling.
   The checker refuses what a move or a rename visibly changes (`import.meta` except `new URL("/abs", import.meta.url)`, a direct `eval`, a top-level `await`).
   Its soundness rests on a list of the ways a move can change behavior, so it is tested against Node by a [fuzzer](experiments/translation/README.md#fuzzer), which found two holes the fixtures had not: a pure read moving across a write, and `import.meta`.

## Controls

Specs and plans that must fail. Each failure is a Lean theorem (`¬ OrderOk …`, checked by the kernel) and a real failure in Node.
`./run.sh --controls` shows the Node side.

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

These are tests, not proofs. `./run.sh --fuzz` runs [`fuzz/fuzz.py`](fuzz/fuzz.py):
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
- **Exceptions.** The model has none. Order is kept for effects, TDZ reads and reads of undeclared globals. Those are the only throws the front end allows an inert statement.

## Going further

[`experiments/README.md`](experiments/README.md):

1. the `Sound` plan predicate and its correctness theorem;
2. a real semantics for a tiny Turing-complete JS-like language that derives the frame assumption (337 lines);
3. a two-tier scheme for automatic and hand-written commutation proofs;
4. what `grind` can write;
5. how far the check scales, compiled and in the kernel;
6. lowering the AST into Lean;
7. 2,740 real splits (halves, single cuts, blocks, random placements), each run in Node;
8. how a proof is produced, with a strategy ladder;
9. what already exists and what I would reuse;
10. the module-order model against Node;
11. debundle's 207 e2e cases through the checker, with causes for the ones that do not work ([`experiments/debundle_cases/`](experiments/debundle_cases/README.md), plan in [`PLAN.md`](PLAN.md)).
12. translation validation: is an output tree the bundle, renamed and reshuffled? ([`experiments/translation/`](experiments/translation/README.md)).

## Files

| Path                           | Role                                                                                                                                                                                                                         |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `lean/EsmSplit/Bundle.lean`    | statement footprints, read closure (worklist over a `TreeMap` index, proved least and closed), conflict                                                                                                                      |
| `lean/EsmSplit/Semantics.lean` | abstract semantics and the reorder theorem                                                                                                                                                                                   |
| `lean/EsmSplit/Plan.lean`      | spec, plan, ES order model, the checks as plain statements                                                                                                                                                                   |
| `lean/EsmSplit/Fast.lean`      | how each check is decided: table-based algorithms, each proved equal to its statement                                                                                                                                        |
| `lean/EsmSplit/Split.lean`     | the splitter `mkPlan` (untrusted)                                                                                                                                                                                            |
| `lean/EsmSplit/Correct.lean`   | `Correct` and `split_correct`                                                                                                                                                                                                |
| `lean/EsmSplit/Render.lean`    | prints a module                                                                                                                                                                                                              |
| `lean/EsmSplit/Instance.lean`  | generated: footprints, spec, kernel-checked theorems                                                                                                                                                                         |
| `lean/EsmSplit/Controls.lean`  | buggy-splitter plans, semantic non-vacuity                                                                                                                                                                                   |
| `lean/EsmSplit/Audit.lean`     | `#print axioms`                                                                                                                                                                                                              |
| `lean/Main.lean`               | the `render` executable                                                                                                                                                                                                      |
| `lean/Experiments/`            | experiments: mini-JS semantics, two tiers, lowering, strategy ladder, module order, translation checker                                                                                                                      |
| `extract/`                     | JavaScript front end for the example (`extract.mjs`)                                                                                                                                                                         |
| `example/`                     | the 12-statement bundle, its spec, one variant that must pass, three controls that must fail                                                                                                                                 |
| `fuzz/`                        | fuzzer and mutation test                                                                                                                                                                                                     |
| `experiments/`                 | [experiments](experiments/README.md) and [timings](experiments/TIMINGS.md); scripts in `lowering/`, `scaling/`, `splits/`, `proof_search/`, `es_order/`, `debundle_cases/`, `translation/`; raw timing records in `records/` |
| `run.sh`                       | everything, end to end                                                                                                                                                                                                       |
