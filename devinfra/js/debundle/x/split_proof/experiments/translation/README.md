# Translation validation: is this tree the bundle, renamed and reshuffled?

[`Translate.lean`](../../lean/Experiments/Translate.lean) takes a bundle and a tree of ES modules and decides whether the tree is the bundle with its top-level statements moved between modules and its names changed. It does not need to know how the tree was made, so it can referee a splitter, a debundler or an agent that edits names and comments.

## Run it

`./run.sh --translation` from the project root builds the checker and runs the fixtures and a mutation test; `./run.sh --translation-fuzz` runs the [fuzzer](#fuzzer). By hand, in this directory:

```bash
cp package.json.in package.json && npm ci --no-audit --no-fund
node frontend.mjs bundle <bundle.js> --out bundle.json
node frontend.mjs tree <outRoot> --entry entry.js --out output.json
node pair.mjs bundle.json output.json --out cert.json
../../lean/.lake/build/bin/translate bundle.json output.json cert.json [--table premises.json]
```

Exit codes: `0` accepted. `1` rejected: the tree is not the bundle renamed and reshuffled. `4` the translation is faithful, but the output runs some effectful units in a different order, and nothing justifies it yet (a list of **obligations**).

## Escaped callbacks and initialization

A callback can escape before a later `const`, `let`, class, or other non-hoisted binding initializes, and an unrelated effect can invoke it indirectly. An otherwise inert initializer is therefore kept effectful when its binding is exported, used in deferred code, read by an inert initializer that can form an alias, or mentioned in a fallback-containing statement. This deliberately conservative rule preserves ordering against unknown effects. A single `const` initialized with a direct primitive literal is modeled separately: its initialization remains ordered against unknown effects, while two independent such initializers may commute. A direct `const` arrow initializer with plain identifier parameters is recognized as non-reentrant: when already effectful it remains so, and its deferred reads and writes remain in its footprint, but it can cross an independent observed literal initializer. It still conflicts with other effects, so the model does not equate fresh closure identities. The same barrier can be waived for a static object literal whose values are primitive literals; computed keys, accessors, spreads, `__proto__`, and identifier reads are excluded. These classifications assume normal completion apart from resource exhaustion. The rule assumes synchronous module evaluation and a trusted source-to-footprint analysis; binding reads and TDZ dependencies remain ordering constraints. Hoisted function declarations remain available from instantiation. Syntactically inert initializers that eagerly read a name not known initialized earlier in source order are also kept effectful: the read can throw in the temporal dead zone, and a previously escaped callback can observe which bindings were initialized after the exception is caught. Hoisted declarations are available from instantiation; non-hoisted read/definition order remains checked in the output. The source footprint analysis and its correspondence to JavaScript are still trusted rather than proved.

Unary numeric coercion is not generally inert even when its operand is a literal: unary `+` throws on BigInt, and coercing a regular-expression literal can invoke user-defined conversion methods. The frontend treats those cases as effects. Logical `!` does not perform numeric coercion; safe primitive-literal arithmetic remains eligible for the inert classification.

## Local identity calls

The source analyzer infers that a `const` initialized by `f(1)` is inert only for a deliberately narrow case: `f` is a unique module-scope, non-async, non-generator function declaration whose sole statement returns its one plain parameter; the call has exactly one single-digit numeric literal argument; and the entire bundle contains exactly two identifier occurrences named `f` (the declaration and the call), with no identifier named `eval`. The parameter and return reference must have the same frontend-resolved binder. Any extra occurrence, including a shadow, alias, exported reference or assignment even inside a deferred body, disables the inference. The result must not be exported or referenced from deferred code, an inert statement that could form an alias, or a statement with unsupported syntax; otherwise an already-escaped callback could read it before initialization. The read of `f` and all binding-order checks remain. Unsupported syntax falls back to effectful.

The checker prints the unit and binding whenever it uses this inference. It is part of the **trusted source-to-footprint analysis**, not an automatically kernel-proved JavaScript semantic certificate: Acorn, scope resolution, and correspondence between the inferred footprint and actual JavaScript semantics remain trusted. The Lean plan theorem is conditional on those footprints. The lowering frontend now adds the same scope and literal annotations to the original, unsplit statements, so `lowersplit` can accept and emit this case without `--force`. Unannotated input stays conservative. The emitted tree is also checked by the translation checker and run alongside the original in Node.

The identity name and `eval` scan is a total Lean function with a 256-level JSON depth budget; if it runs out, it infers no identities. Kernel-checked examples exercise the production scan and its inference decision, including an extra use, `eval`, and exhaustion. A fixture generates annotated units using this frontend and a MiniJS program from the same source file, then requires both inferences to identify the same callee before applying the MiniJS plan theorem. This concrete fixture does not prove that the scan or the MiniJS model accurately describe JavaScript, or establish a general frontend-to-model correspondence. The other source traversal (`visit`) is still partial and trusted.

## What Lean checks

A _unit_ is one top-level statement of the bundle, or one declarator of a variable list.

1. The certificate pairs bundle units and output units one to one.
2. Paired units are equal up to renaming: same node types, literals and property names. Locals correspond by a bijection. Globals are equal. Module-scope names pair up.
3. Links. The output name that a bundle declaration pairs with is declared by the module that holds the unit. Every reference to a module-scope name resolves, through the output's imports and exports, to the declaration of the bundle name it pairs with.
4. The output entry exports every name the bundle exports, each meaning the same declaration. Extra exports are counted.
5. The plan the output describes, written in the bundle's own names, passes `Sound` ([`Correct.lean`](../../lean/EsmSplit/Correct.lean)): unique definitions, reads closed, linkable, hygienic, `AssignsHome`, every unit once, conflicting units keep their order. The last check is where obligations come from.
   A unit _binds_ the names it declares and the names it may assign, its `writes`: reassigned variables, and what the functions it may call assign. Two units conflict if both are effects, if an effect can reenter while the other is an observed initializer, if one reads a name the other binds, or if both bind the same name. A unit is an effect unless its shape is on a short list of inert ones ([`Lower.lean`](../../lean/Experiments/Lower.lean)) or it reads an undeclared name. These definitions are in the verified model ([`Bundle.lean`](../../lean/EsmSplit/Bundle.lean), [`Semantics.lean`](../../lean/EsmSplit/Semantics.lean)); the front end computes the footprints.
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

`acorn` (the parse), `eslint-scope` (which variable an identifier names) and `convert` in [`frontend.mjs`](frontend.mjs) (flattens the tree: identifiers get a class and a binder id, literals one canonical string, positions go, shorthand properties are written out). The footprints are `Lower.lean`'s, as in the rest of the project. The semantic theorem is about the plan; that renaming preserves meaning is assumed, not proved.

The assumption has a known edge: a program can observe the spelling of a name through `fn.name` and `class.name`, `Function.prototype.toString`, `Error` stacks and messages, and `console.log` of a function (`[Function: f]`). A rename changes all of these, and the checker accepts the rename. The [fuzzer](#fuzzer) reproduces it with `--leaks` and reports it apart.

## Fuzzer

The fixtures hold the cases somebody thought of. `node fuzz.mjs` (or `./run.sh --translation-fuzz`) asks whether `translate` ever accepts a tree that behaves differently from its bundle in Node.

Each round builds a bundle of 8 to 21 units from a catalog of templates and splits it six times at random.

- Units: values and aliases, functions (with default parameters), arrows, async functions, classes (static fields, static blocks, `extends`), counters with `++`, `+=` and `=`, objects with getters, iterators, `toString` and `valueOf`, spreads, destructuring, template strings, `for…of`, global variables, a `var` read before its declaration, `{ var }` in a block, `try`, loops, property writes, `new URL("/abs", import.meta.url)`.
- Splits: units placed at random in up to five modules (or with the effects kept in the entry, or in contiguous ranges); units moved inside a module, often next to one that mentions the same name; shuffled import order; renamed names and locals; import aliases; sometimes an alias that a local variable captures, or a top-level name renamed to a global (`JSON`).
- The generator knows what each unit declares and mentions, so the imports and the certificate it hands over do not depend on the checker's scope analysis.

Node runs the bundle and each tree. A tree that the checker accepts must print the same and end the same way. One that does not is a **wrong accept**, and the run fails. A tree that the checker rejects although Node agrees is counted too (the checker is then stricter than needed; so far every such tree has an assignment through an import that never happens to run).

On the final checker, ten seeds of 150 bundles (8,958 trees): 1,737 accepted, all matching Node; 4,074 with obligations (266 match Node); 3,147 rejected (206 match Node); **0 wrong accepts**. About 900 trees take 20 seconds.

`--leaks` adds four templates for what a move or a rename changes behind the checker's back: `import.meta.url`, direct `eval`, top-level `await` (the checker must refuse these) and `fn.name` (assumed: see Trusted). With it, there are still 0 wrong accepts apart from bundles that read `fn.name`, which are counted separately (21 trees in three seeds).

What it found:

1. In the first run: a pure read of a variable that something reassigns could move across the assignment (`inc(); const seen = count;` became `const seen = count; inc();`; Node printed 0 where the bundle printed 1). The model binds each name once, and the conflict rule had nothing about writes. A unit now binds the names it may assign, and conflicts follow them: named cells, in the verified model. I had found this by reading the conflict rule; the fuzzer then found it on its own, across modules (a read in a module that evaluates before the write), in 1 tree of 900.
2. With `--leaks`: `import.meta`, a direct `eval` and a top-level `await` were accepted when the unit moved or was renamed. Now refused (the `new URL("/abs", import.meta.url)` shape is accepted and listed). The real bundle this was written for contains only that shape.

Both have a fixture that fails on the checker before the fix.

### Does the fuzzer notice a weaker checker?

[`fuzz_mutate.py`](fuzz_mutate.py) removes one rule at a time from a scratch copy of the checker, rebuilds `translate`, and runs the fuzzer on it (300 bundles per mutant).

| Rule removed from the checker                                      | Wrong accepts the fuzzer finds                                                                                                                                                              |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| conflict between two effects                                       | 57                                                                                                                                                                                          |
| conflict between a read and a name the other unit binds (declares) | 82                                                                                                                                                                                          |
| conflict between a declaration and an earlier read of it (`var`)   | 8                                                                                                                                                                                           |
| imports evaluated in reverse order                                 | 4                                                                                                                                                                                           |
| conflicts ignore the names a unit assigns (`writes`)               | 5                                                                                                                                                                                           |
| a call that assigns through a function writes nothing              | 4                                                                                                                                                                                           |
| a class is hoisted like a function                                 | 8                                                                                                                                                                                           |
| array spread is inert                                              | 2 (700 bundles; 300 missed it)                                                                                                                                                              |
| template string with expressions is inert                          | 2 (700 bundles; 300 missed it)                                                                                                                                                              |
| member read is inert                                               | 1                                                                                                                                                                                           |
| destructuring declaration is inert                                 | 4                                                                                                                                                                                           |
| class with a static block is inert                                 | 2                                                                                                                                                                                           |
| assignment expression is inert                                     | 2: a property write is then invisible, because only names are cells so far                                                                                                                  |
| `AssignsHome`                                                      | 47                                                                                                                                                                                          |
| `{ var x }` in a block declares nothing                            | 0: harmless here. The tree's links come from the tree, and a name the footprints do not know makes its readers effects. It mattered to the splitter, which computes imports from footprints |
| `Hygienic`                                                         | 0: redundant. It repeats the link check                                                                                                                                                     |

300 bundles per mutant unless noted.

The first run of the harness (150 bundles, an older catalog) left six mutants uncaught. Three were explained by the checker (`{ var x }`, `Hygienic`, and the assignment rule, which then was equivalent). Three were the generator's fault: a spread needs an iterator and a template string needs a `toString` object, so a dependent template needed two units to meet, and now builds its own helper; a static block that acts through a declared object needed its own template (`classStaticWrite`), because a block that calls `console.log` is an effect anyway (it reads an undeclared global). Destructuring was caught at first and missed in the final run, so it got an object-pattern form and more weight.

The catalog is the list of ways a move can go wrong that somebody wrote down. A way nobody generated stays invisible, so a new construct in a bundle means a new template.

## Pairing is not trusted

[`pair.mjs`](pair.mjs) gives each unit a label (its shape) and refines it by the labels of the units it uses and the units that use it, on both sides. Units with equal labels pair up. Copies of one helper that nothing tells apart are interchangeable. Lean re-checks every pair, so a wrong pairing is rejected there.

## Tests

[`fixtures/basic`](fixtures/basic) is a small bundle with a split, renames, a vendor-style import, a dynamic import and a class. [`variants.json`](fixtures/basic/variants.json) lists edits of the output tree and the verdict each must get: a local rename is accepted; a changed literal, a reference to another declaration, a parameter that captures a module-scope name, a missing import, a declaration renamed but not its use, an import of a name the module does not export, one outside import standing for two things, a duplicated unit and a missing unit are rejected; two effects run in the other order give obligations.
[`fixtures/assign`](fixtures/assign) has a counter: a read that moves to another module is accepted, an assignment that moves away from its declaration is rejected, and a read of the counter that moves before the assignment gives obligations (it was accepted before the reassigned-variable rule).
`meta_absolute` accepts `new URL("/static/…", import.meta.url)` in another module and rejects it when the path turns relative; `meta_relative`, `eval_direct` and `top_level_await` hold a bundle that the checker refuses (an `expect.json` names the verdict); `async_function` has an `await` inside a function, which is accepted. `node test.mjs` runs them all.

[`mutate.mjs`](mutate.mjs) tampers with an accepted output (a literal, an operator, a reference, a local reference, a dropped statement, swapped arguments) and checks that `translate` rejects every mutant.

```sh
bbr test //devinfra/js/debundle/x/split_proof/experiments/translation:fixtures_test
```

Bazel runs the same fixture driver as `./run.sh --translation`. The driver uses Node to parse and pair each fixture and checks the Lean checker's verdict for each base case, output variant and generated mutation. The fixture suite checks verdicts. The differential fuzzer described above runs accepted trees in Node and compares their behavior with their bundles.

The package-local `pnpm-lock.yaml` gives Bazel the same parser versions and integrity hashes as the standalone `package-lock.json`. Refresh it with `pnpm import` in a scratch directory containing `package-lock.json` and a copy of `package.json.in` named `package.json`.

Run a seeded sample of the differential fuzzer on demand:

```sh
bbr test //devinfra/js/debundle/x/split_proof/experiments/translation:fuzz_test
```

The target generates 16 bundles with four split attempts each at seed 1. It runs each valid bundle and tree in Node and compares exit status, stdout and error class. The test requires a matching acceptance and fails if the checker accepts a tree with different behavior. The sample uses the ordinary templates. Standalone `./run.sh --translation-fuzz` runs the larger default sample.

The callback fixture exercises the initialization-order regression where a callback passed to `map` reads a
constant before its module initializes it. The checker reports obligations, and Node throws `ReferenceError`
when loading the unsafe output. The extractor also has a separate regression test for the callback's eager read.

## Closure factories

The trusted source-to-footprint analysis also recognizes a direct call to a hoisted ordinary function with one plain parameter whose entire body returns an arrow function. The call must initialize a `const` and pass one uniquely defined, unassigned hoisted function binding. Every textual occurrence of the factory must be its declaration or a recognized direct initializer call; any `eval` identifier disables the rule. As with identity calls, an exported result, a deferred read, an inert alias, or a reference in unsupported syntax disables inference: otherwise a previously saved callback can read the result before initialization. Deferred bodies are not executed at factory invocation. Eager reads of the factory and argument are retained, and the dependency closure preserves references of the returned arrow and callback.

Creating the returned function allocates a fresh identity. This inference assumes allocation and identity are unobservable except through values subsequently made reachable by the program; it does not preserve an entire concrete JavaScript heap, finalization timing, resource exhaustion, or debugger observations. It is not a Lean semantic theorem about JavaScript: parser annotations, name and write analysis, and the footprint correspondence are trusted.

The translation differential fuzzer also has a deterministic `--callback-corpus` mode. Sixteen rounds cover identity and closure-factory calls, direct results and plain, object or array aliases, callbacks saved either on `globalThis` or in a module-local mutable slot, and a shadowed binding. Each generates an accepted order-preserving control (in one or two modules) and a two-module ordering that calls the saved callback before initialization. Node must distinguish the latter, the checker must reject it, and the safe control must be accepted. The seed varies values; the shape is selected by the round number. Two CI targets use distinct seeds and run 32 safe and 32 unsafe controls in total, covering all 16 combinations. The unsafe cases must reach an ordering obligation rather than a structural rejection.

The differential fuzzer's `--ast-corpus` mode builds JavaScript as bounded random ASTs, rather than assembling source templates. Its lexical environments track initialized values, numeric and callable bindings, local shadows and callbacks that depend on later declarations. The generator permits a deferred closure to capture a reserved result or second binding before initialization, but does not invoke it until the bindings exist. The printer emits each original or split unit directly from the AST with binding-aware names. The supported subset is deliberately small: numeric expressions, conditionals, blocks, functions and returned arrows, calls, arrays and objects, mutation, and logging; it does not model arbitrary JavaScript or host APIs.

A same-module control must be accepted and match Node for each bundle. Other plans use seeded random placements; when a generated early-saved callback is later invoked and no intervening direct use makes the crossing trivial, one plan biases the invocation before initialization. Verdicts are checked against Node status, output, and error type. Wrong accepts retain the original, AST, tree, pairing, and seed/round information in the undeclared outputs directory (or retain the temporary work directory when no output directory was supplied). Fixed-seed CI runs print node types, maximum depth, callback/alias/forward-reference features, plan strategies, actual inference counts, and checker-versus-Node outcomes. These samples and mutation tests can find counterexamples; they do not prove the checker sound.

The AST grammar can also snapshot a previously incremented module binding and later log that snapshot. A write-guided plan keeps all units in one module but moves the generated read ahead of its chosen increment. The `ast_write_mutation_test` checks seeds 17 and 2026 against both the actual checker and a test-only checker with its inferred write footprints erased. These seeds were selected because they generate observable write/read pairs. It requires a nontrivial accepted equivalent plan, a differing write-guided plan refused by the real checker, and an accepted write-guided plan from the mutant whose successful Node output changes at the logged snapshot. These are fixed samples of one mutation, not coverage of all writes or JavaScript behavior.

The random AST callback grammar also stores functions through wrappers and object or array cells. A guided same-module plan moves a later callback invocation before an initializer captured by the earlier saved callback; seed 17's `ast_indirect_callback_test` requires at least one Node-divergent placement rejected by an ordering obligation, alongside accepted original-order controls. The seed is selected for this scenario. The callback argument excludes the two deferred bindings, and the second numeric binding can be either an alias or an independent literal. This lets the corpus exercise indirect observation without an incidental eager argument read or an effectful alias initializer masking the observed-initializer fence. Two selected seeds run against both the real checker and a test-only mutant that drops this fence; the tests inspect the generated literal, the reordered unit mapping and Node's resulting ReferenceError before requiring a wrong accept from the mutant. The larger manual `callback_indirect_probe` samples the same guard. These bounded samples do not prove JavaScript equivalence.

`observed_fence_sweep` is a manual wider sample over eight fixed seeds, 32 bundles and two plans per bundle against both checkers; pass a seed with `--test_arg` to run just that seed. A measured run of these 256 bundles had no production wrong accepts and five mutant wrong accepts across four seeds; the other four seeds did not expose the mutation. Seven parallel seed tests took 17–35 seconds each on RBE. One was interrupted by SIGKILL under concurrent load and passed when rerun alone. The default sweep runs seeds sequentially to limit simultaneous checker processes.

The separate `shift_fuzz_test` and `shift_fuzz_second_seed_test` sample 128 seeds each from the external [Shift fuzzer](https://shift-ast.org/fuzzer.html), whose ES2019 grammar generates broader AST shapes than the targeted generator. Acorn parses the generated modules; the real `lowersplit` emitter receives randomized two-module assignments. Original and accepted emitted modules run in separate bounded Node VM module contexts, with a deterministic random source, captured console output and an added observation of primitive top-level values. The tests require at least one accepted reordered split and preserve the source, Shift AST, lowered AST, spec, emitted tree, seed, and observations on a mismatch. They print all attempted AST node types, types in accepted programs, and counts of parser failures, import/export skips, small programs, timeouts, runtime throws, nondeterminism, lowering failures and emitter rejections.

`shift_script_fuzz_test` adds a second source of candidate programs using Shift's script grammar; it still parses and runs each accepted candidate as an ECMAScript module. This avoids generating top-level imports and exports that the current emitter harness skips, while module early errors can still reject script-grammar output. The statistics distinguish accepted and reordered splits whose original program completed successfully from those whose original threw. The original module-grammar seed ranges remain enabled.

This is a broad grammar sample, not a semantic-validity generator: early errors, unresolved names, runtime exceptions and nontermination can occur. Programs with import/export declarations are currently skipped, module execution is in a restricted Node VM rather than a browser or a production host, and object identity and arbitrary heap state are not comprehensively observed. Shift's last supported grammar is ES2019. The existing targeted callback, write and regression corpora remain required; this mode does not replace them or prove JavaScript equivalence.

For a larger on-demand sample, override the fixed test arguments, for example:

```sh
bbr test //devinfra/js/debundle/x/split_proof/experiments/translation:shift_fuzz_test --test_arg=--seed --test_arg=4000 --test_arg=--count --test_arg=256
```

The Shift grammar emitter fuzz tests also write `syntax-coverage-<seed>.json` to their
undeclared test outputs. For each named source syntax family, the report counts parsed programs, exclusion
reasons, accepted plans with matching observations, and accepted plans with that
syntax in a side-module unit. The `Ok` counts additionally require successful
baseline evaluation, so matching exceptions do not inflate them. A zero exposes a gap in this fixed sample, not a claim that the syntax is
unsupported. Each plan contributes once per feature, regardless of how many
occurrences it contains. `acceptedSameInSide` records placement, not whether that
syntax ran or crossed another statement. The comparison observes evaluation status and the captured console trace; it is not a proof of equivalence. The list is a curated
syntax inventory, not a complete ECMAScript grammar; generated import/export
glue, lexical and regexp subfeatures, and runtime APIs are outside these counts.
