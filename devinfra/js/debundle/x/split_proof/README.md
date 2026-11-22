# Splitting a bundle into ES modules, with a Lean proof

A flat JavaScript bundle goes in. A spec says which top-level statement goes to which module.
ES modules come out, and Lean checks that the split is a valid reshuffle.

This is an experiment. It does not use the `debundle` code; nothing here imports it.

[`SUPPORT.md`](SUPPORT.md) lists what a debundler built on this has to support (the syntax and the standard and host features that the real inputs use) and what it does not (a top-level `await`, a name declared twice in one list, and what the tools refuse today). It is a requirement, not a claim that the experiment meets it.

Run everything with `./run.sh` (add `--controls`, `--fuzz`, `--experiments`, `--lowering`, `--es-order`, `--splits` or `--model`).
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

`lowersplit … --plan <file>` also writes the plan as JSON: the order in which the modules run and the statements of each module, by their numbers in the bundle. The test of the CoreJS model reads it ([`experiments/README.md`](experiments/README.md) § 14).

Acorn supplies an unsplit module AST and eslint-scope annotates its binding occurrences. Lean enumerates source ordinals from the module body. It gives each declarator of a top-level `var`, `let` or `const` its own ordinal, including under a named export, and keeps each destructuring pattern together. The renderer slices the original source using Acorn’s UTF-16 offsets.

`lowersplit` derives the CoreJS program from the same annotated AST for bundles of primitive literals, module-binding reads, simple declarations, expression and throw statements, and local named exports. The verdict identifies this input as `Lean ESTree lowering`. When it encounters unsupported syntax, it reports the statement index. Passing `--corejs <program.json>` supplies the remaining statements by ID. Even in a mixed bundle the checker derives every supported statement from the AST, and the move report counts the externally lowered statements. Produce that file with `node experiments/translation/core_program.mjs <bundle.mjs>`. A split that keeps the order preserves every modeled outcome. For reordered splits, the checker proves equivalence of successful executions using syntax-based moves, checked binding footprints, or a class-heritage schedule. The last rule establishes the superclass from an earlier class declaration and checks that each intervening statement preserves it. An accepted split reaches exactly the same final CoreJS states as its bundle on normal completion. Outside imports bind before any statement runs. The checker verifies that removing them leaves the rest of the plan intact. [Move rules and proof inventory](docs/footprint_soundness.md).

For a closed module containing only ordinary identifier declarations with primitive initializers or binding reads, the checker can also report `Declaration extraction`. It evaluates the original declaration groups by allocating the entire module’s bindings before running the initializers in order. Lean proves that evaluating these groups gives the same CoreJS result as evaluating their extracted units, including if an initializer throws. A checked move gives the same guarantee for normal completion as the existing move theorem. This result ends at the modeled plan; printed JavaScript and the namespace observed by outside importers have separate correctness obligations. [Declaration certificate](lean/Experiments/CoreJS/DeclarationCertificate.lean)

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
5. **The printer**, [`Render.lean`](lean/EsmSplit/Render.lean): imports, then statements, then exports. A binding that the glue exports under another name is imported back under its own.
6. **The lowering front end around the model.** Both paths trust the parser, scope annotations and agreement of the AST with the emitted source text. The external path also trusts the supplied CoreJS program to represent the same bundle. The [Lean lowering](lean/Experiments/CoreJS/FromESTree.lean) computes the CoreJS program directly for its supported syntax. Its agreement with JavaScript semantics is tested against Node.
   - **One statement per declarator.** [`dump.mjs`](experiments/lowering/dump.mjs) rewrites the source before the proof: a top-level `var`, `let` or `const` list of several declarators becomes one statement per declarator, also under `export`. This assumes the separate statements run as the list does.
   - **Imports and exports stay in the entry.** `lowersplit` rejects a split that puts an `export` or an `import` statement outside the entry module: the entry's exports are the program's interface, and its imports load the outside modules. When the rule fails, `lowersplit --lean` writes no certificate. Moving it into `Valid` is a small known step: a field per statement, one more conjunct, one more equivalence proof.
   - **Outside modules run first.** The model has no outside modules. An `import` statement declares nothing and does nothing in it, and an imported name is a global: the same binding in every module. The printer of `lowersplit` (`renderModule` in [`LowerSplit.lean`](lean/Experiments/LowerSplit.lean)) writes the entry's `import` and `export … from` statements before its glue imports, in their order, so the outside modules run before every statement and in the bundle's order. Another module gets an `import` line of its own for the outside names it mentions. That this keeps the order is argued, not proved. It assumes that no outside module awaits at its top level, and that the modules are written where the bundle was, so a relative specifier names the same file. `export * from` is rejected.
   - **What an `export` statement publishes** (`Stmt.exports`) is read from its AST by the front end. `ExportsUnique` and `Linkable` rest on it. A glue export adds a name to the entry's interface (`m$1` when `m` is taken); nothing says the interface is otherwise unchanged ([`PLAN.md`](PLAN.md) step 4).

   A rule that code assigning a name stays with its declaration used to live here. It is now `AssignsHome`, a conjunct of `Valid` and of `Sound`. It was missing from the translation checker until a probe showed Node throwing on an accepted split (`experiments/translation/fixtures/assign`).

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
11. debundle's 207 e2e cases through the checker, with causes for the ones that do not work ([`experiments/debundle_cases/`](experiments/debundle_cases/README.md), plan in [`PLAN.md`](PLAN.md)).
12. translation validation: is an output tree the bundle, renamed and reshuffled? ([`experiments/translation/`](experiments/translation/README.md)).
13. footprints by abstract interpretation ([`experiments/effects/`](experiments/effects/README.md)).
14. an executable model of a core of JavaScript, tested against Node, and the first footprint rule proved in it.

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
| `lean/Experiments/`            | experiments: mini-JS semantics, two tiers, lowering, strategy ladder, module order, translation checker, the CoreJS model                                                                                                    |
| `extract/`                     | JavaScript front end for the example (`extract.mjs`)                                                                                                                                                                         |
| `example/`                     | the 12-statement bundle, its spec, one variant that must pass, three controls that must fail                                                                                                                                 |
| `fuzz/`                        | fuzzer and mutation test                                                                                                                                                                                                     |
| `experiments/`                 | [experiments](experiments/README.md) and [timings](experiments/TIMINGS.md); scripts in `lowering/`, `scaling/`, `splits/`, `proof_search/`, `es_order/`, `debundle_cases/`, `translation/`; raw timing records in `records/` |
| `run.sh`                       | everything, end to end                                                                                                                                                                                                       |
