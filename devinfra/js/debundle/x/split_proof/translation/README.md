# Translation validation: is this tree the bundle, renamed and reshuffled?

The verification frontend parses each module with Acorn and passes its annotated ESTree Program to Lean. The footprint dump, translation checker and CoreJS lowerer use the same Lean definition of a top-level unit. Each declarator in a `var`, `let` or `const` list gets its own unit, including inside a named export. A destructuring pattern stays in one unit. Imports and export lists occupy source ordinals in a bundle. The translation checker derives its links from these declarations and numbers the executable body units for pairing. Linked CoreJS modules handle imports separately and assign body IDs from the caller’s table. In the authored Excalidraw replay the module assignments and evaluation order come from the Rust debundler’s reports. Lean reconstructs that plan, checks it and renders the candidate modules. The synthetic split tests supply their own specs. [Excalidraw replay](debug/excalidraw_certificates/authored.mjs)

[`Translate.lean`](../lean/EsmSplit/Translate.lean) takes a bundle and a tree of ES modules and decides whether the tree is the bundle with its top-level statements moved between modules and its names changed. It does not need to know how the tree was made, so it can referee a splitter, a debundler or an agent that edits names and comments.

## Run it

`./run.sh --translation` from the project root builds the checker and runs the fixtures and a mutation test; `./run.sh --translation-fuzz` runs the [fuzzer](#fuzzer). By hand, in this directory:

```bash
cp package.json.in package.json && npm ci --no-audit --no-fund
node frontend.mjs bundle <bundle.js> --out bundle.json
node frontend.mjs tree <outRoot> --entry entry.js --out output.json
node pair.mjs bundle.json output.json --out cert.json
../lean/.lake/build/bin/translate bundle.json output.json cert.json [--premises premises.json] [--table table.json] [--obligations obligations.jsonl]
```

Exit codes: `0` accepted. `1` rejected: the tree is not the bundle renamed and reshuffled. `4` the translation is faithful, but the output runs some effectful units in a different order, and nothing justifies it yet (a list of **obligations**).

`--premises` names the assumptions the footprints rest on, and the checker prints the ones it used: see [Footprints](#footprints). `--table` writes the list of rewrites it accepted (below).

`--obligations` writes every obligation as `[runs first in the output, runs first in the bundle, reasons]`, one per line. `reasons` is the sum of 1 both units touch the world, 2 the later one binds a name the earlier one reads, 4 the earlier binds a name the later reads, 8 both bind a name. [`obligations_report.py`](obligations_report.py) counts them by set of reasons and, given the footprints `lower` printed, by how many names each unit may write. It then groups them by the directory of the output tree: units, effect units, obligations inside a directory and across its border, by reason, and the units in the most obligations. A directory with no obligation can be moved freely.

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

The fixtures hold the cases somebody thought of. `node fuzz.mjs` (or `./run.sh --translation-fuzz`) asks whether `translate` ever accepts a tree that behaves differently from its bundle in Node. The generated bundles satisfy premise P3 (the world adds no accessors or hooks), so it runs with `{"noWorldHooks": true}`; bundles whose own run throws in Node are skipped, which is premise P4.

Each round builds a bundle of 8 to 21 units from a catalog of templates and splits it six times at random.

- Units: values and aliases, functions (with default parameters), arrows, async functions, classes (static fields, static blocks, `extends`), counters with `++`, `+=` and `=`, objects with getters, iterators, `toString` and `valueOf`, spreads, destructuring, template strings, `for…of`, global variables, a `var` read before its declaration, `{ var }` in a block, `try`, loops, property writes, `new URL("/abs", import.meta.url)`.
- Splits: units placed at random in up to five modules (or with the effects kept in the entry, or in contiguous ranges); units moved inside a module, often next to one that mentions the same name; shuffled import order; renamed names and locals; import aliases; sometimes an alias that a local variable captures, or a top-level name renamed to a global (`JSON`).
- The generator knows what each unit declares and mentions, so the imports and the certificate it hands over do not depend on the checker's scope analysis.

Node runs the bundle and each tree. A tree that the checker accepts must print the same and end the same way. One that does not is a **wrong accept**, and the run fails. A tree that the checker rejects although Node agrees is counted too (the checker is then stricter than needed; so far every such tree has an assignment through an import that never happens to run).

On the final checker, ten seeds of 150 bundles (8,958 trees): 1,737 accepted, all matching Node; 4,074 with obligations (266 match Node); 3,147 rejected (206 match Node); **0 wrong accepts**. About 900 trees take 20 seconds.

`--leaks` adds templates for `import.meta.url`, direct `eval`, top-level `await` and `fn.name` (see Trusted). The checker must refuse a split that changes the first three. For function-name probes, the fuzzer counts a separate discrepancy only when the plan renames declarations without introducing capture, both executions complete, and every output difference is in a tagged `fn.name` value. `fuzz_leaks_test` requires the fixed seed to produce at least one such discrepancy.

What it found:

1. In the first run: a pure read of a variable that something reassigns could move across the assignment (`inc(); const seen = count;` became `const seen = count; inc();`; Node printed 0 where the bundle printed 1). The model binds each name once, and the conflict rule had nothing about writes. A unit now binds the names it may assign, and conflicts follow them: named cells, in the verified model. I had found this by reading the conflict rule; the fuzzer then found it on its own, across modules (a read in a module that evaluates before the write), in 1 tree of 900.
2. With `--leaks`: `import.meta`, a direct `eval` and a top-level `await` were accepted when the unit moved or was renamed. Now refused (the `new URL("/abs", import.meta.url)` shape is accepted and listed). The real bundle this was written for contains only that shape.

Both have a fixture that fails on the checker before the fix.

### Does the fuzzer notice a weaker checker?

[`fuzz_mutate.py`](fuzz_mutate.py) removes one rule of the model or of the analysis at a time from a scratch copy, rebuilds `translate` and `lower`, runs the golden cases of [`../effects/run.mjs`](../effects/run.mjs) and then the fuzzer on it. Each of its 59 mutants is caught by a proof (the build fails), by the cases, by the fuzzer, or listed as a survivor with the reason it is harmless. The last run is in [`../effects/README.md`](../effects/README.md#tests).

The catalog is the list of ways a move can go wrong that somebody wrote down. A way nobody generated stays invisible, so a new construct in a bundle means a new template. `ast_write_mutation_test` is the one mutant that runs in CI: the checker with every unit's computed `writes` erased, against the AST corpus.

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

Bazel runs the same fixture driver as `./run.sh --translation`. The driver uses Node to parse and pair each fixture and checks the Lean checker's verdict for each base case, output variant and generated mutation. The fixture suite checks verdicts. The differential fuzzer described above runs accepted trees in Node and compares their behavior with their bundles.

The package-local `pnpm-lock.yaml` gives Bazel the same parser versions and integrity hashes as the standalone `package-lock.json`. Refresh it with `pnpm import` in a scratch directory containing `package-lock.json` and a copy of `package.json.in` named `package.json`.

Run a seeded sample of the differential fuzzer on demand:

```sh
bbr test //devinfra/js/debundle/x/split_proof/translation:fuzz_test
```

The target generates 40 bundles with four split attempts each at seed 3. It runs each valid bundle and tree in Node and compares exit status, stdout and error class. The test requires a matching acceptance and fails if the checker accepts a tree with different behavior. `fuzz_leaks_test` is the same sample with the `--leaks` templates. Standalone `./run.sh --translation-fuzz` runs the larger default sample.

The callback fixture exercises the initialization-order regression where a callback passed to `map` reads a
constant before its module initializes it. The checker reports obligations, and Node throws `ReferenceError`
when loading the unsafe output. The extractor also has a separate regression test for the callback's eager read.

## Corpora

The translation differential fuzzer also has a deterministic `--callback-corpus` mode. Sixteen rounds cover identity and closure-factory calls, direct results and plain, object or array aliases, callbacks saved either on `globalThis` or in a module-local mutable slot, and a shadowed binding. Each generates an accepted order-preserving control (in one or two modules) and a two-module ordering that calls the saved callback before initialization. Node must distinguish the latter, the checker must reject it, and the safe control must be accepted. The seed varies values; the shape is selected by the round number. Two CI targets use distinct seeds and run 32 safe and 32 unsafe controls in total, covering all 16 combinations. The unsafe cases must reach an ordering obligation rather than a structural rejection.

The differential fuzzer's `--ast-corpus` mode builds JavaScript as bounded random ASTs, rather than assembling source templates. Its lexical environments track initialized values, numeric and callable bindings, local shadows and callbacks that depend on later declarations. The generator permits a deferred closure to capture a reserved result or second binding before initialization, but does not invoke it until the bindings exist. The printer emits each original or split unit directly from the AST with binding-aware names. The supported subset is deliberately small: numeric expressions, conditionals, blocks, functions and returned arrows, calls, arrays and objects, mutation, logging, and field-free class expressions. One quarter of AST rounds build a chain of two to four classes. Their literal-key methods and getters use expressions from the generated environment. Each program constructs the leaf and observes every member through it or its constructor. The validation target parses, scope-checks, and executes 128 programs across four seeds and requires at least 24 distinct inheritance shapes.

A same-module control must be accepted and match Node for each bundle. Other plans use seeded random placements; when a generated early-saved callback is later invoked and no intervening direct use makes the crossing trivial, one plan biases the invocation before initialization. Verdicts are checked against Node status, output, and error type. Wrong accepts retain the original, AST, tree, pairing, and seed/round information in the undeclared outputs directory (or retain the temporary work directory when no output directory was supplied). Fixed-seed CI runs print node types, maximum depth, callback/alias/forward-reference features, plan strategies, and checker-versus-Node outcomes. These samples and mutation tests can find counterexamples; they do not prove the checker sound.

The AST grammar can also snapshot a previously incremented module binding and later log that snapshot. A write-guided plan keeps all units in one module but moves the generated read ahead of its chosen increment. The `ast_write_mutation_test` checks seeds 17 and 2026 against both the actual checker and a test-only checker with its computed write footprints erased. These seeds were selected because they generate observable write/read pairs. It requires a nontrivial accepted equivalent plan, a differing write-guided plan refused by the real checker, and an accepted write-guided plan from the mutant whose successful Node output changes at the logged snapshot. These are fixed samples of one mutation, not coverage of all writes or JavaScript behavior.

The random AST callback grammar also stores functions through wrappers and object or array cells. A guided same-module plan moves a later callback invocation before an initializer captured by the earlier saved callback; seed 17's `ast_indirect_callback_test` requires at least one Node-divergent placement rejected by an ordering obligation, alongside accepted original-order controls. The seed is selected for this scenario. The callback argument excludes the two deferred bindings, and the second numeric binding can be either an alias or an independent literal, so the corpus exercises indirect observation without an incidental eager argument read. These bounded samples do not prove JavaScript equivalence.

The separate `shift_fuzz_test` and `shift_fuzz_second_seed_test` sample 128 seeds each from the external [Shift fuzzer](https://shift-ast.org/fuzzer.html), whose ES2019 grammar generates broader AST shapes than the targeted generator. Acorn parses the generated modules; the real `lowersplit` emitter receives randomized two-module assignments. Original and accepted emitted modules run in separate bounded Node VM module contexts, with a deterministic random source, captured console output and an added observation of primitive top-level values. The tests require at least one accepted reordered split and preserve the source, Shift AST, lowered AST, spec, emitted tree, seed, and observations on a mismatch. They print all attempted AST node types, types in accepted programs, and counts of parser failures, import/export skips, small programs, timeouts, runtime throws, nondeterminism, lowering failures and emitter rejections. An accepted split whose original throws is counted (`acceptedThrowing`) and not compared: premise P4.

`shift_script_fuzz_test` adds a second source of candidate programs using Shift's script grammar; it still parses and runs each accepted candidate as an ECMAScript module. This avoids generating top-level imports and exports that the current emitter harness skips, while module early errors can still reject script-grammar output. The statistics distinguish accepted and reordered splits whose original program completed successfully from those whose original threw. The original module-grammar seed ranges remain enabled.

This is a broad grammar sample, not a semantic-validity generator: early errors, unresolved names, runtime exceptions and nontermination can occur. Programs with import/export declarations are currently skipped, module execution is in a restricted Node VM rather than a browser or a production host, and object identity and arbitrary heap state are not comprehensively observed. Shift's last supported grammar is ES2019. The existing targeted callback, write and regression corpora remain required; this mode does not replace them or prove JavaScript equivalence.

For a larger on-demand sample, override the fixed test arguments, for example:

```sh
bbr test //devinfra/js/debundle/x/split_proof/translation:shift_fuzz_test --test_arg=--seed --test_arg=4000 --test_arg=--count --test_arg=256
```

The Shift grammar emitter fuzz tests also write `syntax-coverage-<seed>.json` to their
undeclared test outputs. For each named source syntax family, the report counts parsed programs, exclusion
reasons, accepted plans with matching observations, and accepted plans with that
syntax in a side-module unit. Reordered counts use the emitter-reported
statement order and do not imply that a particular construct executed or crossed
another statement. The `Ok` counts additionally require successful
baseline evaluation, so matching exceptions do not inflate them. A zero exposes a gap in this fixed sample, not a claim that the syntax is
unsupported. Each plan contributes once per feature, regardless of how many
occurrences it contains. `acceptedSameInSide` records placement, not whether that
syntax ran or crossed another statement. The comparison observes evaluation status and the captured console trace; it is not a proof of equivalence. The list is a curated
syntax inventory, not a complete ECMAScript grammar; generated import/export
glue, lexical and regexp subfeatures, and runtime APIs are outside these counts.

`shift_modern_fuzz_test` supplements the ES2019 grammar by using Acorn AST ranges to compose optional chains, nullish coalescing and class-field reads around generated primitive literal expressions. The rewritten source is parsed again and passed to the same real emitter and Node differential oracle; the existing Shift seed ranges are retained. In the 96-seed CI sample, successful accepted reorderings executed nullish coalescing in two programs, optional chaining in three and class fields in two, with no observed mismatches. Per-feature runtime counters require a successful, accepted reordered program to evaluate each form, independently; syntactic presence alone is reported separately by the coverage table.

Runtime coverage uses a seed-specific marker binding absent from the original AST, installed as a non-enumerable, non-writable VM global. The marker records a bounded set after an expression has produced its value and returns that value unchanged; the original and emitted marker sets are compared separately from their console traces. Modern-mode inputs referring to `this`, global-object names, eval, Function, Reflect, Proxy or dynamic import are conservatively skipped, and VM string/wasm code generation is disabled. The marker and the wrapper allocations are trusted instrumentation: the restricted VM and filters do not prove that no JavaScript program could observe them, and the sampled syntax does not cover all forms of these features. `instrumentationRejected` records filtered inputs.

## What the generators cover

[`COVERAGE.md`](COVERAGE.md) says, for each generator above, which ES2024 node types and syntax forms its programs contain, and which standard functions they call, next to how [`Effects.lean`](../lean/EsmSplit/Effects.lean) treats each (an arm of `eval`, `exec`, `evalClass` or `bindPattern`, or a give-up) and which rows [`Intrinsics.lean`](../lean/EsmSplit/Intrinsics.lean) has. Only programs that complete count, because premise P4 drops the others. The audit is a script, not a test of the checker; it reads the Lean sources, so the table is out of date after a change to them:

```sh
node coverage_audit.mjs --samples 150 --seed 1 --out COVERAGE.md && prettier --write COVERAGE.md
```

[`syntax_inventory.mjs`](syntax_inventory.mjs) lists the node types and forms (each with a witness snippet that `coverage_audit_test` parses), [`analysis_inventory.mjs`](analysis_inventory.mjs) reads the analysis's inventory from the Lean sources, and [`intrinsics_probe.mjs`](intrinsics_probe.mjs) counts the standard functions a program calls from its own code (`node --import`). `node fuzz.mjs --emit DIR` writes the bundles of a corpus mode without running them.

### Protocol combinations outside the general fuzz corpus

The general generator produces standard array calls, iterable `Array.from` inputs, classes, regular expressions and program-defined hooks. Coverage of those individual forms does not establish that the generator can compose the effectful calls in the known-gap fixtures. [`implicit_calls.mjs`](gen/implicit_calls.mjs) composes dispatch, hook installation and observation for the following families. `effects_generated_known_gaps_test` runs seed `20261121` through `translate` and Node with a safe control and a stdout-changing reorder per program. It requires an observed wrong accept in each listed family and records siblings that are correctly refused. To rerun the matrix with other generated values, append `--test_arg=--seed --test_arg=77`.

| Family                       | What generation exercises                                                                                                                                   |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Array species                | `map`, `filter`, `slice`, `splice`, `concat`, `flat` and `flatMap` with a program constructor under `Symbol.species`, installed by assignment or descriptor |
| Inherited array factories    | Subclass construction through `Array.from` on iterable and array-like inputs and through `Array.of`                                                         |
| Node inspection              | A custom inspector reached through nested object and array arguments to `console.log`                                                                       |
| P1, RegExp legacy state      | An independent read of a legacy capture across `exec`, `test`, `replace`, `match` or `split`                                                                |
| P3, hidden hook installation | Getter, conversion and prototype installation through iterator callbacks, generators and `Reflect.apply`. The order check is also repeated with P3 off.     |

The module generator in [`gen/core.mjs`](gen/core.mjs) assumes P1 and omits patches to built-ins. The template fuzzer emits direct static and prototype assignments that the checker already detects. Computed mutator selection, spread forwarding, a generator-body alias and defining an intrinsic global remain explicit counterexamples in [the P1 audit](../effects/debug/p1_known_gaps/README.md). Shift supplies grammar coverage, without a production pairing these mutations with an initialized intrinsic and an observation of the changed state. The footprint oracle treats built-ins as opaque, so the P1 audit uses the Node output from an independent read across the mutation.

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

[`gen/`](gen) builds programs from the productions of the language instead of from a catalog of templates, so that every construct has a chance. A program is 12 to 25 top-level units that share state, because that is what a split moves, and it is made to complete, to print the same every time and to leave its state in view:

- Generation is type-directed. A binding has a type (`num`, `str`, `bool`, an object with the fields it has, an array, a map, a set, a function that records which module-scope names it may change), and an expression is built for a type from the bindings that are initialized where it runs, so it does not throw. A number that may be `undefined` (a deleted field, a hole) is converted before a method is called on it. Strings stored in state are cut to 24 characters, so that no loop can double one.
- A loop counts to a constant and never writes its counter (a `for (x = 0; …)` over a module-scope variable freezes it in the body), a loop over a collection does not change it, and a call is not made where the callee would change what a loop iterates or counts with (`type.writes`). A function calls only functions declared before it; a recursion is cut to depth 5 after the statements of its body. A `throw` is inside a `try` that catches it, no `break`, `continue` or `return` leaves a `finally`, and generators and async functions do not loop over collections that code between their steps may grow.
- Nothing reads a clock or a random number, and nothing patches a standard object (premises P1 and P4). Observers print each binding between the units (`o3 …`), the last unit prints all of them (`final …`), and a program with an async function prints them once more from a timer after the continuations ran (`late …`).
- Module-scope names and a unit's own names are marked in the text, as the splitter of `fuzz.mjs` wants them, so that it can rename them and add imports. `gen_validity_test` renames every marked name and checks that the output does not change.

Families are installed one by one (`gen/index.mjs`): `expressions` (every operator, the standard functions that have a row and some that have none), `statements` (declarations, every assignment operator, writes to objects, arrays, maps and sets, `if`, the six loops, `switch` with fall-through, `try`, labels, jumps), `functions` (declarations, expressions and arrows with default, rest and `arguments` parameters, recursion, closures, objects with methods and `this`, generators, async functions, callbacks of array and collection methods, `call`, `apply`, `bind`, tagged templates), `classes` (declarations, expressions, anonymous, named and local to a block; instance, private, static and computed fields; arrow-function fields; methods that are plain, private, static, generator, async or computed; accessors, also private and static; static blocks; `extends` an identifier, an expression or `null`; `super(…)`, `super.m(…)`, `new.target`, `#x in o`, `instanceof`; a member calls only members written before it, so there is no recursion and no override), `patterns` (destructuring in declarations, assignments, parameters, callbacks and loop heads with defaults, rest, holes, computed keys and nesting; spread in calls and arrays; values that may be `null` and the optional chains over them; logical assignment on elements and members; `for … in` and `for … of` into existing targets; several declarators; block-level functions). A pattern is built together with a value that matches it, so what it binds is typed and the match cannot throw. `protocols` (object literals with `valueOf`, `toString`, `Symbol.toPrimitive`, `toJSON`, `Symbol.iterator` as a generator or as a hand-written iterator with `return`, getters, setters, generator, async and computed methods, a prototype, shorthand properties; what calls them implicitly: coercion, `JSON.stringify`, spread, `for … of`, `yield*`, array patterns; a `Proxy` whose traps count in an object that observers print, and a function behind one; `Object.defineProperty` that adds an accessor that counts; `Object` and `Reflect` queries and changes; frozen, sealed, null-prototype and cloned objects), `modules` (Node's built-in modules imported in the five import forms, the lines repeated in every module a program is split into; `import()` from a continuation and `await import()`; `import.meta`; an `EventEmitter` whose handlers are fixed where it is made) and `unmodeled` (typed arrays and the views that alias them through `subarray`, `buffer` and `DataView`; a global regular expression whose `lastIndex` moves; `Date` setters; `WeakRef`, `WeakMap`; `eval`, indirect `eval` and `Function` of code made from numbers only; continuations that run after the program: `queueMicrotask`, `process.nextTick`, `Promise.withResolvers`, `setTimeout`; `Intl`, `BigInt`, URI coding, `Symbol.for`, copies of arrays, the `console` methods that print). `node fuzz.mjs --full-corpus [--families a,b]` runs the end-to-end oracle on them. `node footprint_oracle_sweep.mjs --only full` runs the footprint oracle on them, and `node footprint_reduce.mjs program.mjs` cuts a program with a finding down to the units that matter.

`gen_validity_test` generates 40 programs for each set of families and checks that each parses, declares a name before every use, completes in Node with no output on stderr, prints the same twice and with every name renamed, and that the families produced the forms they are for. `full_fuzz_test` is the end-to-end oracle on 16 programs. The export forms are what is left ([`PLAN.md`](PLAN.md)): the translation checker refuses `export default`, `export … from` and `export *`, and the splitter exports by name, so an aliased export needs a change there. The oracle cannot see a private class member or a closure's variable (see above), so `classes` has the observers print them (a pure `peek` method reads the private fields), and the reorder oracle and the translation fuzzer see them in the output.
