# Translation validation: is this tree the bundle, renamed and reshuffled?

The verification frontend parses each module with Acorn and passes its annotated ESTree Program to Lean. The footprint dump, translation checker and CoreJS lowerer use the same Lean definition of a top-level unit. Each declarator in a `var`, `let` or `const` list gets its own unit, including inside a named export. A destructuring pattern stays in one unit. Imports and export lists occupy source ordinals in a bundle. The translation checker derives its links from these declarations and numbers the executable body units for pairing. Linked CoreJS modules handle imports separately and assign body IDs from the caller’s table. In the authored Excalidraw replay the module assignments and evaluation order come from the Rust debundler’s reports. Lean reconstructs that plan, checks it and renders the candidate modules. The synthetic split tests supply their own specs. [Excalidraw replay](../integration/excalidraw/authored.mjs)

[`Translate.lean`](../lean/EsmSplit/Translate.lean) takes a bundle and a tree of ES modules and decides whether the tree is the bundle with its top-level statements moved between modules and its names changed. It does not need to know how the tree was made, so it can referee a splitter, a debundler or an agent that edits names and comments.

## Run it

Use `:fixtures_test` for the checker fixtures and `:translation_fuzz` for the [fuzzer](#fuzzer). For standalone research commands in this directory:

```bash
cp package.json.in package.json && npm ci --no-audit --no-fund
node frontend.mjs bundle <bundle.js> --out bundle.json
node frontend.mjs tree <outRoot> --entry entry.js --out output.json
node pair.mjs bundle.json output.json --out cert.json
../lean/.lake/build/bin/translate bundle.json output.json cert.json [--premises premises.json] [--table table.json] [--obligations obligations.jsonl]
```

Exit code `0` means accepted. Code `1` means rejection during translation or
structural checking. Code `4` means structural checks passed and the output
reverses conflicting units (**obligations**), including conflicts on shared
bindings. Code `2` covers incorrect positional usage.

`--premises` names the assumptions the footprints rest on, and the checker prints the ones it used: see [Footprints](#footprints). `--table` writes the list of rewrites it accepted (below).

`--obligations` writes every obligation as `[runs first in the output, runs first in the bundle, reasons]`, one per line. `reasons` is the sum of 1 both units touch the world, 2 the later one binds a name the earlier one reads, 4 the earlier binds a name the later reads, 8 both bind a name. [`obligations_report.py`](obligations_report.py) counts them by set of reasons and, given the footprints `lower` printed, by how many names each unit may write. It then groups them by the directory of the output tree: units, effect units, obligations inside a directory and across its border, by reason, and the units in the most obligations. The report describes the order in the supplied output tree.

## Footprints

The footprint of a unit is what it touches: whether it reaches the world, which names it may change, which names it reads through functions it calls. [`Effects.lean`](../lean/EsmSplit/Effects.lean) computes it by running the bundle's top level over abstract values, and [`Lower.lean`](../lean/EsmSplit/Lower.lean) scans what the unit declares, mentions and assigns. How it works, the premises and the tests are in [`../effects/README.md`](../effects/README.md). `--premises <file>` is `{"vendor": …, "members": …, "noWorldHooks": true}`; every key is optional, and without a file the checker assumes nothing beyond the premises that are always on. Every verdict starts with the premises it rests on.

A callback can escape before a later `const`, `let`, class or other non-hoisted binding initializes. Any unit that runs code the analysis cannot see may invoke it, so such a unit writes every name that reaches an escaped function's closure, and conflicts with the declaration of a name the function reads. [`inert_escape_test.mjs`](inert_escape_test.mjs) holds the shapes (direct, through aliases in objects and arrays, through a module-local slot, a generated pair, an exported declaration seen by a cyclic importer): Node runs each forced split into a `ReferenceError`, and the checker refuses it. A unit that only builds and changes program objects is not an effect, so reordering two of them gives no obligation.

A source that throws when it runs is outside premise P4. `+1n` always throws, and a statement that reads a `const` before its declaration throws in the original: after a reorder the program may throw something else, or throw first. The tests do not expect a refusal there.

## What Lean checks

A _unit_ is one top-level statement of the bundle, or one declarator of a variable list.

1. The certificate pairs bundle units and output units one to one.
2. Paired units are equal up to renaming: same node types, literals and property names. Locals correspond by a bijection. Globals are equal. Module-scope names pair up.
3. Links. The output name that a bundle declaration pairs with is declared by the module that holds the unit. Every reference to a module-scope name resolves, through the output's imports and exports, to the declaration of the bundle name it pairs with.
4. The output entry exports every name the bundle exports, each meaning the same declaration. Extra exports are counted.
5. The plan the output describes, written in the bundle's own names, passes `Sound` ([`Correct.lean`](../lean/EsmSplit/Correct.lean)): unique definitions, reads closed, linkable, hygienic, `AssignsHome`, every unit once, conflicting units keep their order. The last check is where obligations come from.
   A unit _binds_ the names it declares and the names it may assign, its `writes`: reassigned variables, objects whose contents it changes, and what the functions it may call change. Two units conflict if both are effects, if one reads a name the other binds, or if both bind the same name. A unit reads the names it mentions and the names it reaches through functions it calls (`deps`). These definitions are in the verified model ([`Bundle.lean`](../lean/EsmSplit/Bundle.lean), [`Semantics.lean`](../lean/EsmSplit/Semantics.lean)); the footprints are computed outside it, see [Footprints](#footprints).
6. `AssignsHome`: code that assigns a module-scope name sits in the module that declares it. ES imports are read-only, so an assignment through an import throws a `TypeError` when it runs. It is a conjunct of `Sound`. What a _call_ of such code changes is `writes`, and is ordered by the conflict rule, wherever the call sits. The first version of this checker lacked the rule and accepted such a split.
7. Refused outright, because moving or renaming the unit changes what it does:
   - `import.meta`, whose value belongs to the module. The one accepted shape is `new URL("/absolute/path", import.meta.url)` (below).
   - a direct `eval`, which reads module-scope names by their spelling.
   - a top-level `await` (or `for await`), after which module evaluation is no longer a plain sequence. Inside an `async` function it is fine.

`Hygienic` is a second, independent look at the links: it recomputes what every name means from the imports alone.

## Rewrites accepted besides renaming

Each is a premise. The checker accepts it only in this exact shape and prints what it relied on (`--table` writes the list).

| Rewrite                                                                 | Premise                                                        |
| ----------------------------------------------------------------------- | -------------------------------------------------------------- |
| An import from outside the tree becomes a member chain of one           | the two are the same value (`ut(…)` and `Mobx.runInAction(…)`) |
| A dynamic import's path moves                                           | the new path names the same chunk                              |
| `new Worker("/p")` becomes `new Worker(new URL("/p", import.meta.url))` | an absolute path on the same origin starts the same worker     |
| `new URL("/p", import.meta.url)` stays as it is                         | it resolves against the origin only, the same in every module  |

Anything else that differs is a rejection. Modules outside the tree are assumed to be leaves whose order does not matter.

## Trusted

The checker trusts Acorn's parse and the frontend's annotations for identifier scope, local binders and literal values. Lean extracts units and resolves imports and exports from the Programs. It compares paired units after removing source positions and treating shorthand properties as explicit ones. The footprints are `Effects.lean`'s and `Lower.lean`'s, under the premises they print. The semantic theorem is about the plan and assumes that renaming preserves meaning. [Frontend](frontend.mjs) · [Lean input projection](../lean/EsmSplit/TranslateInput.lean)

The assumption has a known edge: a program can observe the spelling of a name through `fn.name` and `class.name`, `Function.prototype.toString`, `Error` stacks and messages, and `console.log` of a function (`[Function: f]`). A rename changes all of these, and the checker accepts the rename. The [fuzzer](#fuzzer) reproduces it with `--leaks` and reports it apart.

## Fuzzer

The fixtures hold the cases somebody thought of. The `:translation_fuzz` target asks whether `translate` ever accepts a tree that behaves differently from its bundle in Node. The generated bundles satisfy premise P3 (the world adds no accessors or hooks), so it runs with `{"noWorldHooks": true}`; bundles whose own run throws in Node are skipped, which is premise P4.

Each round builds a bundle of 8 to 21 units from a catalog of templates and splits it six times at random.

- Units: values and aliases, functions (with default parameters), arrows, async functions, classes (static fields, static blocks, `extends`), counters with `++`, `+=` and `=`, objects with getters, iterators, `toString` and `valueOf`, spreads, destructuring, template strings, `for…of`, global variables, a `var` read before its declaration, `{ var }` in a block, `try`, loops, property writes, `new URL("/abs", import.meta.url)`.
- Splits: units placed at random in up to five modules (or with the effects kept in the entry, or in contiguous ranges); units moved inside a module, often next to one that mentions the same name; shuffled import order; renamed names and locals; import aliases; sometimes an alias that a local variable captures, or a top-level name renamed to a global (`JSON`).
- The generator knows what each unit declares and mentions, so the imports and the certificate it hands over do not depend on the checker's scope analysis.

Node runs the bundle and each tree. A tree that the checker accepts must print the same and end the same way. One that does not is a **wrong accept**, and the run fails. A tree that the checker rejects although Node agrees is counted too (the checker is then stricter than needed; so far every such tree has an assignment through an import that never happens to run).

In the initial experiment, ten seeds of 150 bundles (8,958 trees): 1,737 accepted, all matching Node; 4,074 with obligations (266 match Node); 3,147 rejected (206 match Node); **0 wrong accepts**. About 900 trees take 20 seconds.

`--leaks` adds templates for `import.meta.url`, direct `eval`, top-level `await` and `fn.name` (see Trusted). The checker must refuse a split that changes the first three. For function-name probes, the fuzzer counts a separate discrepancy only when the plan renames declarations without introducing capture, both executions complete, and every output difference is in a tagged `fn.name` value. `fuzz_leaks_test` requires the fixed seed to produce at least one such discrepancy.

What it found:

1. In the first run: a pure read of a variable that something reassigns could move across the assignment (`inc(); const seen = count;` became `const seen = count; inc();`; Node printed 0 where the bundle printed 1). The model binds each name once, and the conflict rule had nothing about writes. A unit now binds the names it may assign, and conflicts follow them: named cells, in the verified model. I had found this by reading the conflict rule; the fuzzer then found it on its own, across modules (a read in a module that evaluates before the write), in 1 tree of 900.
2. With `--leaks`: `import.meta`, a direct `eval` and a top-level `await` were accepted when the unit moved or was renamed. Now refused (the `new URL("/abs", import.meta.url)` shape is accepted and listed). The real bundle this was written for contains only that shape.

Both have a fixture that fails on the checker before the fix.

### Does the fuzzer notice a weaker checker?

[`fuzz_mutate.py`](fuzz_mutate.py) removes one rule at a time in a scratch copy,
rebuilds `translate` and `lower`, then runs the Effects cases and the fuzzer.
A mutant is caught by a proof that stops compiling, an Effects case or the
fuzzer. [Recorded results and surviving mutants](../effects/README.md#tests)

The bounded CI write-footprint mutant is described in [Corpora](#corpora).

## Pairing is not trusted

[`pair.mjs`](pair.mjs) gives each unit a label (its shape) and refines it by the labels of the units it uses and the units that use it, on both sides. Units with equal labels pair up. Copies of one helper that nothing tells apart are interchangeable. Lean re-checks every pair, so a wrong pairing is rejected there.

## Tests

[`fixtures/basic`](fixtures/basic) is a small bundle with a split, renames, a vendor-style import, a dynamic import and a class. [`variants.json`](fixtures/basic/variants.json) lists edits of the output tree and the verdict each must get: a local rename is accepted; a changed literal, a reference to another declaration, a parameter that captures a module-scope name, a missing import, a declaration renamed but not its use, an import of a name the module does not export, one outside import standing for two things, a duplicated unit and a missing unit are rejected; two effects run in the other order give obligations.
[`fixtures/assign`](fixtures/assign) has a counter: a read that moves to another module is accepted, an assignment that moves away from its declaration is rejected, and a read of the counter that moves before the assignment gives obligations (it was accepted before the reassigned-variable rule).
`meta_absolute` accepts `new URL("/static/…", import.meta.url)` in another module and rejects it when the path turns relative; `meta_relative`, `eval_direct` and `top_level_await` hold a bundle that the checker refuses (an `expect.json` names the verdict); `async_function` has an `await` inside a function, which is accepted. `node test.mjs` runs them all.

[`mutate.mjs`](mutate.mjs) tampers with an accepted output (a literal, an operator, a reference, a local reference, a dropped statement, swapped arguments) and checks that `translate` rejects every mutant.

```sh
bbr test //devinfra/js/debundle/x/split_proof/translation:fixtures_test
```

The Bazel fixture driver uses Node to parse and pair each fixture and checks the Lean checker's verdict for each base case, output variant and generated mutation. The fixture suite checks verdicts. The differential fuzzer described above runs accepted trees in Node and compares their behavior with their bundles.

The package-local `pnpm-lock.yaml` pins the parsers, fuzzer dependencies, and public benchmark libraries. The standalone `package-lock.json` is used by research scripts outside Bazel.

Run a seeded sample of the differential fuzzer on demand:

```sh
bbr test //devinfra/js/debundle/x/split_proof/translation:fuzz_test
```

The target generates 40 bundles with four split attempts each at seed 3. It runs each valid bundle and tree in Node and compares exit status, stdout and error class. The test requires a matching acceptance and fails if the checker accepts a tree with different behavior. `fuzz_leaks_test` is the same sample with the `--leaks` templates. For a larger sample, use `bb run` with `:translation_fuzz` and pass your own count and seed.

The callback fixture exercises the initialization-order regression where a callback passed to `map` reads a
constant before its module initializes it. The checker reports obligations, and Node throws `ReferenceError`
when loading the unsafe output. The extractor also has a separate regression test for the callback's eager read.

## Corpora

The translation corpora compare the checker with Node completion status, stdout
and error type. Wrong accepts retain the source, split tree, pairing and
seed/round under Bazel undeclared outputs, or in the temporary work directory.
The AST corpus also retains the generated AST.

| Target                       | Required control and distinguishing case                                                                                                                                                                                                                                      |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `callback_fuzz_test`         | Accept order-preserving splits. Refuse invocation of an escaped callback before its captured initializer with an ordering obligation. The matrix varies factories, aliases, storage and shadowing. `callback_fuzz_second_seed_test` repeats the matrix with different values. |
| `ast_fuzz_test`              | Accept each same-module control and compare randomized plans, including a callback-before-initialization plan when generation supplies a suitable crossing. Report syntax, depth, alias and forward-reference coverage. `ast_fuzz_second_seed_test` adds a seed.              |
| `ast_write_mutation_test`    | Require an equivalent accepted reorder and a Node-divergent read moved before an increment. The real checker must refuse the latter and a mutant with erased `writes` must accept it.                                                                                         |
| `ast_indirect_callback_test` | Require a Node-divergent plan refused for ordering when a callback escapes through a wrapper or object/array cell. The argument excludes both deferred bindings so that an eager read cannot explain the refusal. Accept the original-order controls.                         |

The [AST generator](testing/ast_generator.mjs) tracks lexical bindings,
initialization and deferred captures directly. Placement and printing use
binding identity. Its validation target parses, scope-checks and runs generated
inheritance chains, constructs the leaf, and observes every member through it
or its constructor. [Validation test](ast_generator_validation_test.mjs)

### Shift emitter fuzzing

[`shift_fuzz_test.mjs`](shift_fuzz_test.mjs) tries randomized two-module
assignments with the real `lowersplit` emitter. Its ES2019 Shift module and
script grammars supply candidates, parsed by Acorn as modules. The harness skips
import/export declarations and early errors. It compares original and accepted
emitted code in separate bounded Node VM contexts with seeded randomness,
captured console output and primitive top-level observations. It counts accepted
plans whose source throws as `acceptedThrowing` and omits their comparison
under P4. The test requires an accepted reorder and retains the source, ASTs,
spec, emitted tree, seed and observations on mismatch.

`shift_fuzz_test` and `shift_script_fuzz_test` run routine module- and script-grammar
samples. `shift_fuzz_second_seed_test` is opt-in. Override the sample with:

```sh
bbr test //devinfra/js/debundle/x/split_proof/translation:shift_fuzz_test --test_arg=--seed --test_arg=4000 --test_arg=--count --test_arg=256
```

The undeclared output `syntax-coverage-<seed>.json` counts candidate exclusions
and accepted plans by syntax family. Each plan contributes once per feature.
`Ok` counts require successful baseline evaluation. Reordered counts use the
emitter's statement order. `acceptedSameInSide` means the syntax was placed in a
side module. These counters measure placement, and a zero describes the sample.

`shift_modern_fuzz_test` composes optional chaining, nullish coalescing and
class-field reads around generated literals. A fresh non-enumerable, read-only
VM global marks executed expressions and returns their values unchanged. The
test compares the markers separately from console output and requires each
modern form to execute in a successful accepted reorder. The harness skips
inputs with global-reflection or dynamic-code constructs and disables VM code
generation. The marker and wrapper allocations are trusted instrumentation.
[`modern_syntax.mjs`](modern_syntax.mjs)

The initial 96-seed run had no observed mismatches. Successful accepted
reorders executed optional chaining in three programs, nullish coalescing in
two and class-field reads in two.

The VM comparison covers the recorded observations. Arbitrary heap state and
object identity are outside that comparison.

## What the generators cover

[`COVERAGE.md`](COVERAGE.md) records a historical generator sample. New reports count the syntax and standard function calls of programs that complete. They also report how the compiled Lean analyzer classifies each standalone syntax witness during module initialization, including unknown operations and exhausted analysis budgets.

From `translation/` with its standalone dependencies installed:

```sh
(cd ../lean && lake build analysis_inventory)
node coverage_audit.mjs --samples 150 --seed 1 --out COVERAGE.md && prettier --write COVERAGE.md
```

[`syntax_inventory.mjs`](syntax_inventory.mjs) defines the forms and witness snippets. [`AnalysisInventory.lean`](../lean/AnalysisInventory.lean) runs each witness separately and exports the actual intrinsic keys and member paths. Pass `--analyzer FILE` to select a built executable. [`intrinsics_probe.mjs`](intrinsics_probe.mjs) counts the standard functions a sampled program calls from its own code. `node fuzz.mjs --emit DIR` writes the bundles of a corpus mode without running them.

### Protocol combinations outside the general fuzz corpus

The general generator produces standard array calls, iterable `Array.from` inputs, classes, regular expressions and program-defined hooks. Coverage of those individual forms does not establish that the generator can compose the effectful calls in the known-gap fixtures. [`implicit_calls.mjs`](testing/gen/implicit_calls.mjs) composes dispatch, hook installation and observation for the following families. `effects_generated_known_gaps_test` runs seed `20261121` through `translate` and Node with a safe control and a stdout-changing reorder per program. It requires an observed wrong accept in each listed family. Species descriptor variants and the assignment/`filter` variant must be refused, as must any other unmarked divergent reorder. To rerun the matrix with other generated values, append `--test_arg=--seed --test_arg=77`.

| Family                       | What generation exercises                                                                                                                                                                                     |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Array species                | `map`, `filter`, `slice`, `splice`, `concat`, `flat` and `flatMap` with a program constructor under `Symbol.species`, installed by assignment or descriptor                                                   |
| Inherited array factories    | Subclass construction through `Array.from` on iterable and array-like inputs and through `Array.of`                                                                                                           |
| Node inspection              | A custom inspector reached through nested object and array arguments to `console.log`                                                                                                                         |
| P1, RegExp legacy state      | An independent read of a legacy capture across `exec`, `test`, `replace`, `match` or `split`                                                                                                                  |
| P1, intrinsic patches        | Computed mutator selection, rest/spread forwarding and generator-body aliases across `Object`, `Array` and `Map` prototypes. Global descriptors replace `Array`, `Number` or `Reflect` before a static query. |
| P3, hidden hook installation | Getter, conversion and prototype installation through iterator callbacks, generators and `Reflect.apply`. The order check is also repeated with P3 off.                                                       |

The module generator in [`testing/gen/core.mjs`](testing/gen/core.mjs) assumes P1 and omits patches to built-ins. The template fuzzer emits direct static and prototype assignments that the checker already detects. The adversarial generator ties each P1 mutation to an independent read, and each execution uses its own Node child process. The footprint oracle treats built-ins as opaque, so the audit checks the program's output across the mutation. The minimal counterexamples remain in [the P1 audit](../effects/debug/p1_known_gaps/README.md).

## The host inventory

[`host_inventory.mjs`](host_inventory.mjs) counts which standard and host features a set of JavaScript files uses: the globals (names no declaration in the file binds), the static member paths that start at one, the names of members of standard objects on any other receiver, host modules and the keys of `import.meta`. It reads source only.

```sh
node host_inventory.mjs scan PATH... > inventory.json   # files, directories, or @LIST with one file per line
node host_inventory.mjs render inventory.json > report.md && prettier --write report.md
```

`render` prints only names that are public already: globals that a list of the [`globals`](https://www.npmjs.com/package/globals) package has, and member paths that resolve on Node's own objects. It counts the rest, so a report of private sources can be published; `render --full` prints every name. [`../HOST_FEATURES.md`](../HOST_FEATURES.md) is the report for the inputs this experiment has to support. The head of the script says what the scan does not see.

## The footprint oracle

The reorder oracle above sees a wrong footprint only when a reorder happens to change what Node prints. [`footprint_oracle.mjs`](footprint_oracle.mjs) checks the footprints themselves: it runs a program unit by unit in Node, and after every unit compares what each top-level binding reaches with the unit's footprint from [`lower`](../effects/README.md).

```sh
LOWER=../lean/.lake/build/bin/lower node footprint_oracle.mjs program.mjs [--premises p.json] [--runs 2]
node footprint_oracle_sweep.mjs [--only templates,ast,shift,…] [--samples 50] [--seed 1] [--keep DIR]    # the generators of the audit
```

A binding whose digest changed during unit `i` must be declared by the unit or be in its `writes`. A unit that changed a binding it does not bind is `unwritten` (a wrong footprint: a reader of the binding can cross it) or, when the unit is an effect, `unlisted` (the unit is ordered against other effects without naming the binding, but a pure reader still crosses it, so it counts the same). Also checked: a unit that printed or added a global is an effect, and a binding a unit initializes is one it declares. A `lower` that ends with an error or a signal on a valid program is a `crash` (exit code 1); `fuzz.mjs` reports a checker that ends with a signal as `CHECKER CRASHES` after the run. Exit codes: 0 no finding, 1 a finding, 2 not a valid input (premise P4: the program throws, does not finish, or its changes differ between two runs).

The digest of a binding covers everything it reaches: prototypes, own properties with their attributes, getters and setters by identity (never called), `Map`, `Set`, typed arrays, buffers, dates, regular expressions, boxed primitives, cycles. Objects are numbered by first sight, so a binding given a new object that looks the same changes. The standard objects, and everything the global object reaches, are opaque leaves; the ECMAScript ones are compared at the start and the end, and a difference is listed as a patched intrinsic (premise P1; the program is still checked).

What it cannot see: reads; closure-private state (a variable a function captured, a private class member, a generator's or iterator's cursor, the state of a promise, the contents of a `WeakMap`); what a `Proxy` does inside; a change that leaves the digest alone; and changes made by continuations after the last unit (listed apart as `tail`). A top-level `await` is refused. The reorder oracle covers what the digest cannot.

`footprint_oracle_test` covers the snapshot, the refusals, the comparison, and that an erased `writes` is found. The findings are in [`debug/generator_findings`](debug/generator_findings/README.md).

## The generator of the whole language

The [type-directed generator](testing/gen/index.mjs) composes families of
terminating, deterministic programs that share module state. Bindings carry
types and initialization state. Calls track writes so they cannot mutate a
loop's bound or collection mid-iteration. Destructuring patterns are generated
with values they can match. The output includes per-unit, final and, when
needed, post-continuation observations. Class observers make private fields
visible to Node output even when the footprint oracle cannot see them.

Use the [sampled coverage report](COVERAGE.md) for syntax and intrinsics and
the [plan](PLAN.md#7-a-generator-that-can-reach-any-program-and-a-footprint-oracle)
for open forms. [`gen_validity_test`](gen_validity_test.mjs) checks termination,
repeatable output and invariance under its name markers' renaming.
`full_fuzz_test` also checks splits through `translate` and Node.

```sh
node fuzz.mjs --full-corpus --families a,b
node footprint_oracle_sweep.mjs --only full
node footprint_reduce.mjs program.mjs
```

Omit `--families` for all families. The reducer keeps the units needed to
reproduce the oracle finding.
