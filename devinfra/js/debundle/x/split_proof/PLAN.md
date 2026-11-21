# Plan

Goal: turn a minified production bundle into readable modules, with a machine-checked guarantee that the split program behaves like the original, resting on as few unproved assumptions as possible, each one stated.

The work was parked on 2026-11-21. This file is the hand-over: what exists, what to do next and in which order, and what is still owed. [`docs/lessons_learned.md`](docs/lessons_learned.md) has the experience behind it, [`SUPPORT.md`](SUPPORT.md) what a debundler has to support, [`experiments/translation/PLAN.md`](experiments/translation/PLAN.md) the plan of the translation checker, and [`TODO.md`](TODO.md) the smaller items.

## State when parked

The design is "untrusted search, trusted check": a planner proposes a split, and Lean checks it (`Valid`, nine checks; `Valid → Correct` is proved). The check trusts one input that is not proved, the footprint of each statement, which [`Effects.lean`](lean/Experiments/Effects.lean) computes by abstract interpretation.

- **The tool works on a real bundle.** On the large bundle (9,707 units, 2,793 reviewed modules) `lowersplit` accepts a split that keeps 2,366 modules whole and runs them in bundle order (`"sequential": true`), and one of 886 modules with no order kept. The first loaded in a browser like the original, before the last rule fixes; it was not loaded again after them.
- **The footprint analysis is tested, not proved.** Seven wrong rules were found by generators and a footprint oracle (F1 to F7); six are fixed, F7 is open. A `throw` that is reached and not caught is unknown code. 288 golden cases, the fuzzers, the footprint oracle and a mutation harness guard it ([`experiments/effects/README.md`](experiments/effects/README.md)).
- **A model of JavaScript exists to prove against**: `CoreJS` ([`experiments/README.md`](experiments/README.md) § 14), a total interpreter in Lean tested against Node. It has closures, objects with prototypes and accessors, plain classes, exceptions, modules, and a world as a parameter for host functions on primitives. `fuel_mono` makes "the program ends with `r`" well defined. A four-way test runs bundle and split, each in Node and in the model, on what `lowersplit` accepts.
- **One footprint fact is proved in the model**, for every world: a declaration whose initializer only allocates changes nothing that existed and binds only its name (`decl_allocOnly`). It is not connected to `Effects.lean`, and no theorem yet says that a split behaves like its bundle in the model.
- **The order-kept slice of the theorem is not landed.** It was being written when the work was parked; step N1 says where it stands.
- **Measured on the inputs** (3,363 files of the private repository): nearly the whole language and standard library occur ([`HOST_FEATURES.md`](HOST_FEATURES.md), [`SUPPORT.md`](SUPPORT.md)). Start-up of the large bundle enters 1% of the application chunk's functions and 10% of the vendor chunk's, and still uses 37 of 45 tracked language features and about 110 standard functions.

## Picking this up

- **Build and test.** `./run.sh` and its flags ([`README.md`](README.md)); CI runs `bbr test //devinfra/js/debundle/x/split_proof/...`. By hand, in `lean/`: `nix shell nixpkgs#lean4 nixpkgs#gcc --command lake build EsmSplit render Experiments lower lowersplit evalorder translate corejs` (about 2 minutes clean). The golden cases: `node ../effects/run.mjs` in `experiments/translation` (90 s). The model: `node differential.mjs --count 40 --seed 1` in `experiments/model` (20 s). Each `experiments/*/package.json.in` is copied to `package.json` before `npm ci`; never commit a `package.json`.
- **After any change to `Effects.lean`**: the golden cases, `node test.mjs`, `node fuzz.mjs --count 150 --seed <new>`, `footprint_oracle_sweep.mjs --samples 40 --seed <new>`, `experiments/debundle_cases` (its committed results must be regenerated if they change), and `fuzz_mutate.py` (about an hour; say so if it is skipped). Then measure the large bundle: the count of units that touch the world must be reported with the change.
- **The private evidence** is in the private repository `gaffer-private`, under the large bundle's `debug/translation-validation/`: `peel-probe/` (what peels, the bundle-order split, the browser load, the counts after the rule fixes), `startup-coverage/` (what runs at start-up) and the host inventory under `devinfra/debundle/host_inventory/`. Its scripts rebuild the inputs: the statement dump comes from [`experiments/lowering/dump.mjs`](experiments/lowering/dump.mjs) on the chunk, the footprints from `lower <dump> --premises <file>` (3 minutes, 1.5 GB), the partition from `scripts/peel_all.mjs`. The scratch copies of these inputs did not outlive the session that made them.
- **What reaches this repository from there** is counts and names that are public already. No spec names, no minified identifiers, no application names. Fixtures use neutral names.
- **Independent attempts pay.** Half an hour of a fresh agent told to break the analysis found dozens of wrong accepts each time. Repeat it after every change to the rules, and give the agent the generators and the oracle.
- **Review with programs the author did not write.** Every layer of the model was checked that way before it merged, on a seed its author had not used.

## Direction

**The theorem to reach.** It does not mention footprints. `split` is the whole tool as one total function: it takes a program and a spec and returns the split program, or refuses.

```lean
-- More fuel never changes a run that finished.
theorem fuel_mono : run n P = r → r ≠ .fuel → n ≤ m → run m P = r
-- "P ends with r": some amount of fuel is enough. By `fuel_mono` there is at most one such r.
def Ends (P) (r) := ∃ n, run n P = r ∧ r ≠ .fuel
-- A split that runs the statements in the bundle's order: every ending.
theorem order_kept_preserves : … →
    (∀ r, Ends P r → ∃ r', Ends P' r' ∧ r ≈ r') ∧ (∀ r', Ends P' r' → ∃ r, Ends P r ∧ r ≈ r')
-- A split that moves code: the runs that end normally.
theorem split_preserves : InFragment P → split P spec = some P' →
    (∀ s, Ends P (.ok () s) → ∃ s', Ends P' (.ok () s') ∧ s ≈ s') ∧
    (∀ s', Ends P' (.ok () s') → ∃ s, Ends P (.ok () s) ∧ s ≈ s')
```

- **A split that keeps the order** runs the same statements in the same order, so it is compared for every ending: a value, a throw, unknown code. The two directions together say that one program runs forever exactly when the other does.
- **A split that moves code is compared only for runs that end normally.** That is premise P4 of the analysis ("evaluation completes", [`experiments/effects/README.md`](experiments/effects/README.md)) as a hypothesis of the theorem. Without it the statement is false, and the tool shows it: with `const o = { a: undefined }; console.log("printed"); const w = o.a.b;` the read that raises a `TypeError` may be split off so that it runs first, and then nothing is printed ([`experiments/README.md`](experiments/README.md) § 14). The analysis orders a `throw` statement it reaches; an exception that the language raises itself in pure code is ordered against nothing.
- **The converse needs the same hypothesis, on the split.** If the split ends with a throw, the bundle may end with another throw after other output, or not end at all: the statement that throws may sit after a loop that never ends, and the split may run it first. What is expected to hold, and is not proved: one ends normally exactly when the other does. The argument is that how a statement ends depends only on what it reads, and the checks keep every pair in order where one writes what the other reads; so the same statements throw in both, and a run ends normally exactly when none does. `Meaning` in [`Semantics.lean`](lean/EsmSplit/Semantics.lean) has no notion of a statement that does not finish, so this argument has no home in the proved part yet.
- A program that prints and then runs forever is not compared: the model keeps no output of an unfinished run.
- `≈` is "the same to an observer": the same interaction with the world and final states that later code cannot tell apart. For a reorder inside one program that is equality. For modules it is a relation, and it is part of what a reader has to trust.

`run` is the model, `CoreJS`. `InFragment` is a check on the program's text; it may be "the tool derives a footprint other than unknown code for these units". What the tool calls footprints, `Valid` and the conflict rule are then inside the proof, and a reader has to trust only the model, `≈` and this statement.

Inside the proof, two lemmas carry it, for programs in the fragment:

- _A derived footprint is true:_ a unit reads and changes only what its footprint lists. This needs no new definition. [`Meaning`](lean/EsmSplit/Semantics.lean) already has the shape (a statement without effects is a function of the values it reads), and "real JavaScript is assumed to fit" is assumption 2 of [`README.md`](README.md) § What is not proved. The work is to show that `CoreJS` fits it for the fragment.
- _True footprints justify the move:_ this is `Valid → Correct`, proved.

A proof for a narrow fragment is worth having, and two fragments are better than one: each is its own `InFragment`, and a verdict of the tool says which one covered the split, or that none did.

What the statement forces:

- **`split` has to be total Lean code.** `Effects.lean` is `partial` over JSON and Lean can prove nothing about it. So the fragment's footprints come from a new derivation over `CoreJS`'s syntax that answers "unknown code" except where a rule is proved, beginning with the allocation rule. `Effects.lean` stays in use outside the fragment, unproved, until the derivation covers what it covers. A unit where the derivation says "effect" and `Effects.lean` says "none" is a finding.
- **`run P'` needs modules in the model.** They are in it: a program is a set of modules, linked before any runs, with imported names as the exporter's bindings and the specification's order of evaluation ([`experiments/README.md`](experiments/README.md) § 14). The model's order agrees with `Plan.visit` on every program tested; that it is the same function is not proved yet.
- **A run that reaches unknown code says nothing after that point.** The first version covers programs that stay inside the model. Real bundles do not: 1,584 of the large bundle's 9,707 units touch the world. The vendor code is in the bundle's chunks, so it can be part of the program; then only the host is unknown. Either way the model has to take the world as a parameter, a function that may read and change what it is given and nothing else, and the theorem holds for every world. The first stage exists: host functions on primitives ([`experiments/README.md`](experiments/README.md) § 14). The second, host code that is handed objects, is designed there and not built.
- **The standard library cannot be defined function by function.** Start-up runs little code and most of the language ([`docs/lessons_learned.md`](docs/lessons_learned.md) § The first slice of a model): about 110 standard functions by name, regular expressions among them. They enter the model as part of the world, each with a stated class of behaviour ("a function of its primitive arguments", "reads its receiver", "may call its argument"). That table stays an assumption about the engine, tested and not proved. 88 functions of the first two classes are in it (`worldFunction`), and the test replays Node's answers for them.

**Decided so far.**

- **No new precision rule goes into the unproved interpreter** unless a measurement shows it is blocking. Fixing a wrong rule is always in scope, and its cost on the large bundle is measured before it is adopted.
- **The model is built up beside the tested analysis**, as a spike with a kill rule: stop if step N2 below does not work, or if the proved part covers under about half of the units that get precise footprints today. Then the tested analysis stays, labelled as trusted.
- **A proof for a narrow fragment is worth landing.** Each is its own slice of the theorem; the tool's verdict is to say which slice covered a split.
- **What is refused** ([`SUPPORT.md`](SUPPORT.md)): a top-level `await`, a top-level `using`, a direct `eval` anywhere, a name declared twice in one list. A `Proxy` is never covered by premise P3.
- **An exception that ends evaluation**: a `throw` statement keeps its place; an exception the language raises itself is covered by the hypothesis that the bundle's evaluation completes.

**Open decisions for the owner.**

- Whether premise P3 may assume that code of the program which the world runs adds no accessor or hook (finding F7), or the analysis gives up P3 where such code may do it. Step E1 measures the middle way first.
- How the four input files with a direct `eval("require")` are admitted: a check of the string, or an audited premise.
- Whether a reviewed vendor table is worth its review cost. Alone it does not change which modules peel.
- Whether the vendor chunk becomes part of the program in the model. It removes most unknown code, and the model then has to run what the vendor code runs at start-up, regular expressions included.

## Next steps

Two tracks. The proof track is ordered: each step needs the one before. The engineering track is independent of it and of itself, so its steps can run in parallel. Every step ends with its checks run and its numbers in the PR.

### Proof track

**N1. Finish the order-kept slice.** `order_kept_preserves`: a split whose plan runs the statements in the bundle's order ends like the bundle, for every world and every ending. It needs a Lean function from the bundle's statements and an `EsmSplit.Plan` to the model's module program (the mirror of `Module.render` on syntax trees), a definition of `≈` (prefer equal heaps and cells, with only the maps from names to cells differing), and hypotheses that the tool can check or that `Valid` gives. Done when the theorem is proved with no `sorry`, is in `corejs_axioms_test`, and [`experiments/README.md`](experiments/README.md) § 14 says exactly what `≈` is.

**N2. Make the order-kept slice cover real bundles. This is the decision point of the spike.**

- _Goal:_ `order_kept_preserves` holds for a bundle whose statements are mostly outside the model's syntax.
- _Idea:_ a split that keeps the order runs the same statements in the same order, so the theorem does not need to know what a statement does. A statement outside the fragment becomes an opaque step that the world takes: it may read and change what it can reach and call what it was handed.
- _Needs:_ the second stage of the world, designed at the end of [`experiments/README.md`](experiments/README.md) § 14: the model keeps the list of addresses the world was handed, and the world names objects only by position in that list. And a front end that turns a statement it cannot translate into an opaque one, with the names it declares and mentions taken from the scope analysis.
- _First actions:_ write the opaque step and restate the theorem with it; check on paper that the proof of N1 does not look inside a statement; then build the world's second stage.
- _Checks:_ the model's tests; the four-way test with opaque statements mixed in; the front end accepts the large bundle's dump.
- _Done when:_ the theorem's hypotheses hold for the 2,366-module split of the large bundle, checked by the tool, and the PR lists what stays trusted: the model of modules, `≈`, and the front end's reading of which names a statement declares.
- _Stop if:_ an opaque step cannot be given a meaning that keeps the theorem true and non-trivial. Then the proof track cannot reach real bundles without most of the language in the model: record why, and go to the owner.

**N3. The verdict names the slice.** `lowersplit` prints which proved slice covered the accepted split, or that none did, next to the premises it prints today. Done when a verdict on the large bundle's order-kept split says so. This keeps the proofs attached to the tool.

**N4. The first slice that moves code: units that only allocate.**

- _Goal:_ `split_preserves` for a split that moves such units past each other and past function declarations. That is the case of the 886 modules that peel with no order kept; of the 873 that peel alone, 812 hold only function declarations.
- _Needs, in order:_ the allocation rule for plain classes (the model runs them; `Expr.allocOnly` does not include them, and the proof needs a lemma that the field initializers `evalMembers` returns come from the members); that such an expression never ends `unknown` (a well-formedness invariant of the state); that its value depends only on the bindings it reads (`Expr.readsNow`); then `AllocOnlyUnitsCommute` ([`Frame.lean`](lean/Experiments/CoreJS/Frame.lean), stated, not proved); then a derivation `derive : Stmt → Footprint` as total Lean code that knows this one rule and answers "unknown code" otherwise, and the instance of [`Meaning`](lean/EsmSplit/Semantics.lean) that connects it to `Valid → Correct`.
- _Checks:_ `derive` against `Effects.lean` on the large bundle: every unit where `derive` gives a footprint, `Effects.lean` must give the same or a weaker one. A unit where `derive` says "effect" and `Effects.lean` says "none" is a finding.
- _Done when:_ the theorem is proved, and the share of the large bundle's units with a proved footprint is reported. The census predicts 73% ([`allocate_only_census.mjs`](experiments/translation/allocate_only_census.mjs)); far less means the shape is wrong.
- _Then delete `MiniJS`_ ([`lean/Experiments/MiniJS.lean`](lean/Experiments/MiniJS.lean) and its two companions): `CoreJS` replaces it once commutation is proved there.

**N5. Remove the imports that exist only for order.** The order-kept split of the large bundle has 2,744 bare imports in 629 modules, nested 466 deep. Each is a candidate to drop, and dropping one is a claim that two modules commute. First candidates: a forward mention of a module that only declares functions. The count of ordering imports going down is the measure of how readable the output is. The second rule to prove belongs here: unknown code cannot write a name declared later, except through an object that escaped earlier. The peel probe measured it as the largest lever (873 modules that peel alone would become about 1,624, an optimistic count).

### Engineering track

**E1. Finding F7** ([`generator_findings`](experiments/translation/debug/generator_findings/README.md), five programs that are wrong accepts with P3 on). Code of the program that the world runs adds a getter, a `toString` or a prototype to an object the world holds, and P3 reads the object as plain. The simple sound rule, P3 off once program code has escaped, costs 30% more units that touch the world on the large bundle. First measure the cheaper rule without building it: scan the code that escapes for the constructs that add a hook to an existing object (`defineProperty`, `defineProperties`, `__defineGetter__`, `setPrototypeOf`, `__proto__`, `Reflect`, a store under `toString`, `valueOf` or a computed key) and count the units that would lose P3. Build it if the cost is a few percent; otherwise it is the owner's decision above.

**E2. `import.meta`.** `translate` rejects a unit that reads it, except `new URL("/absolute", import.meta.url)`; 71 input files read it at the top level. What moving code changes is `import.meta.url` and what is resolved against it. Supporting it means rewriting or pinning the base when a unit moves to a module in another directory. Done when the fixtures `meta_relative` and `meta_absolute` have accepted counterparts that Node agrees with, and the count of input files refused for it is reported.

**E3. `class … extends` in the model.** Every start-up evaluates every class: 162 with `extends` in the application chunk of the large bundle. Needs the prototype chain of constructors, `super(…)` and `super.m()`, and the dead zone of `this` before `super` returns. Then, by how often they ran at start-up there: spread (70), regular expression literals as allocation (38), destructuring (12), optional chains (12), `for`-`of` (5).

**E4. The mutants that survive.** `fuzz_mutate.py` leaves 7 of 155. `no Hygienic` is known, and `nested hooks are unbounded` is harmless since the hook depth is counted. Five survived before the last fixes too and were never looked into: `a replayed call drops what it threw`, `memoized calls replay no writes`, `an unseen constructor does not escape`, `Intl reads no options`, `Error reads no options`. For each: a case that kills it, or the reason it is equivalent. The harness was not run after the `throw` rule; run it first.

**E5. Evidence that went stale.** After the rule fixes and the `throw` rule only the counts of units and of whole modules were measured again. Not measured again: the order obligations of the reviewed tree (670,997 before), the census of single peels (873), the vendor ladder, and the browser load of the accepted split (3,052 files now). The lessons file says which numbers are from before.

**E6. Library code.** `lower` aborted on the 20,032-unit vendor chunk with a stack overflow. The cause of F5 (a hook that reaches itself) fits that symptom better than deep syntax, and F5 is fixed: run the chunk again before anything else is built for it.

## Debts

What is owed beyond the steps above. Each is a known gap, not a plan.

**In the model and its proofs**

- `Program.evalOrder` is the same function as `Plan.visit` of the proved checker: tested on every program of the model's test, not proved.
- The table of 88 host functions (`worldFunction` in [`Value.lean`](lean/Experiments/CoreJS/Value.lean)) is an assumption about the engine: called on primitives, each returns a primitive or throws and reads nothing the program can change. It was checked from memory of the specification, not against its text.
- A world with one state orders every pair of host calls. The model cannot yet say that `Math.abs` does not read or change the world, which a split that moves code needs.
- Read-only imports rest on the front end: [`to_core.mjs`](experiments/model/to_core.mjs) decides from the text which assignment targets are imports (`Target.frozen`).
- The front end is trusted and is JavaScript: it decides what a host global is by asking the Node that runs it, and which declaration a name means.
- Not in the model: `**` (the specification leaves it to the implementation, and the C library's result differed from V8's in the last bit), `instanceof`, conversion of an object to a primitive (`valueOf`, `toString`), writes to arrays, namespace imports, re-exports.
- A run that ends out of fuel keeps no output, so a program that prints and then runs forever is not compared.
- The generated programs barely reach the model: 17% of their units run. The hand-written cases carry the object model.
- That a split ends normally exactly when its bundle does is expected and not proved ("The theorem to reach").

**In the footprint analysis**

- It is `partial` code over JSON, so nothing about it can be proved as it is.
- Premise P4: an exception that the language raises itself in pure code is not ordered. `lowersplit` accepts a split that moves `const w = o.a.b` across a `console.log` when `o.a` is `undefined` (a named case of the model's test).
- The `throw` rule does not follow which branch is taken (`if (n > 5) throw …` counts when `n` is 1). Calls under a `try` are remembered apart from the same call outside one, and no test tells that from a shared memory: three attempts to make a caught call be replayed for an uncaught one failed, because a run that grows the heap is not remembered.
- Three precise rules went in where the fail-safe one cost too much: `Array(n)` is an array, iterating a collection yields pairs, and the `prototype` of a class is not the world's. Each has a case and a mutant.
- `catch` binds the world's value only when the unit ran code the analysis did not see. This rests on every skipped piece of code setting that flag.
- 55 of the 69 units that the fix of F6 made effects on the large bundle come from "every row converts its arguments". A flag per row would win them back.
- `lower` alone gives a unit with a direct `eval` a footprint without the names the string assigns; `lowersplit` and `translate` refuse such a bundle.
- A top-level `await` in a module that the bundle imports is not detected: the tools do not read those modules.
- The libraries (`lodash`, `acorn`, `semver`, `zod`) were not run through the footprint oracle after the fixes.
- The known gaps of premise P1 and of the general rules, five programs each ([`TODO.md`](TODO.md)).

**In the checker and the planner**

- The entry's public surface is not in `Valid`, and the rule that `import` and `export` statements stay in the entry is outside it (step 4 of the next section, and [`TODO.md`](TODO.md)).
- The order-keeping planner nests imports 466 deep on the large bundle. Node loaded a chain 3,000 deep and failed at 10,000.
- [`README.md`](README.md) § What is not proved lists the rest: the ES order model, hygiene, the printer, the lowering front end.

**In the evidence**

- The browser load checks start-up and the first render of a signed-out page with the network cut off. Nothing was logged in or used.
- The start-up coverage is one such load. A signed-in start runs more.
- The host inventory did not scan one vendor module that could not be rebuilt in the session that made it.

## More of debundle's cases through the Lean checker

State: of 207 entries in `devinfra/js/debundle/e2e/cases`, 84 agree with debundle (69 accepts, 15 rejects), 7 disagree, 109 are not run. Six of the 109 are bundles `lowersplit` refuses for a direct `eval` or a top-level `await`; debundle rejects them too.
Causes: [`experiments/debundle_cases/README.md`](experiments/debundle_cases/README.md); numbers: [`results/summary.md`](experiments/debundle_cases/results/summary.md).

Acceptance for every step, after `./run.sh --debundle-cases`:

- `unsound-accept` stays at 0. A step that unlocks entries is also a soundness test: the first run of the corpus found two accepted splits that fail to load.
- The step's entries leave `unsupported` and `strict-*`, or the PR says why one stays.
- `./run.sh --controls --fuzz` and the mutation test still pass.

Steps are independent PRs. Steps 2–5 touch different Lean files and can run in parallel. Order by entries unlocked per effort:

### 1. Take selector resolution from debundle (54 comparable entries; 12 more are refusal tests)

The adapter resolves `binding` selectors and hole-free templates by exact AST shape. debundle resolves holes, alpha-renaming, statement ranges and the dataflow selectors (`passed_to_call`, `member_of_module`, `cross_ref`, `reads_member`, `makes_decorate_call`).

- **Do not port the matcher.** 42 entries need holes, alpha-renaming and ranges. 24 need dataflow facts. debundle's resolver is a shape matcher plus a solver ([`docs/selector_resolution.md`](../../docs/selector_resolution.md)). A JS port would have to follow both.
- **Read debundle's answer instead.** Run `debundle run` per entry and read the owner of every source unit. `modules.json` has `source_unit_ids` per module and `dependency_graph.json.gz` has `unit:N` nodes ([`docs/wire_format.md`](../../docs/wire_format.md)). Check whether the nodes carry input ranges. If not, add one side output to debundle: unit → input range → owner path.
- The adapter maps units to statements (declarators, as in step 2) and writes the `lowersplit` spec as today.
- This makes `Valid` an independent second opinion on debundle's gate for every spec debundle resolved.
- The 12 refusal entries stay `refusal-test`.
- Open: how the harness runs the `//devinfra/js/debundle:debundle` binary, and what one call costs.

### 2. Declarator granularity in the adapter, and repeated `var` (6 comparable entries: 3 not run, 3 `strict-but-runs`; 2 more are refusal tests)

- `lowering/dump.mjs` writes one statement per declarator of a `var`/`let`/`const` list, and a destructuring declarator stays whole (debundle refuses to split one: `comma_list/destructure_split_across_modules_is_rejected`). The adapter still parses on its own, a statement per list: take its statements from `lowerDump`, and let a template that matches a whole list claim each of its declarators.
- `UniqueDefs` becomes "one module per name". Several `var` declarations of a name may sit in one module and keep their order, because they conflict. This changes `Semantics.lean`: the last declarer wins. `split_correct` needs a new proof.
- Entries: 2 of the 4 `statement claimed by two modules` (the other 2 are refusal tests), `sibling_declarator_ownership`, and the 3 `UniqueDefs` rejections.

### 3. Purity precision (3 of 5 `strict-but-runs` left)

[`Effects.lean`](lean/Experiments/Effects.lean) runs pure local calls and applies a table of standard-function summaries ([`experiments/effects/`](experiments/effects/README.md)); `initialization/frozen_callback` and `native_collections/duplicate_primitive_spread` pass now. Left:

- `emission/proxy_constructor/fresh_args`: `new Proxy` is a call into the world. A proxy runs its handler's traps, and an empty handler can gain one later, so a row needs a soundness argument ([`docs/purity_soundness.md`](../../docs/purity_soundness.md)).
- `callbacks/from_entries_map_does_not_run_stored_callback_during_construction`: the result is exported, and what is read from an exported object is the world's, since a module in a cycle may have changed it. A claim in the spec that no cyclic importer observes it would clear it.
- `initialization/frozen_record`: the declaration of `later` moves before the unit that gives `peek` to the world. Moving a declaration earlier is harmless when nothing in between can call `peek`; the model orders every reader and binder pair. A hint (step 2) says it.

Guard anything added with the corpus' rejections that must stay rejected (`proxy_constructor/effectful_proxy_keys`, `freeze_effectful_argument`, `freeze_proxy_receiver`), the fuzzer and the mutation test.

### 4. The entry's public surface

- `Valid` knows what each `export` statement publishes (`Stmt.exports`) and keeps a module's export names apart (`ExportsUnique`). It does not say that the entry still exports what the bundle exported: glue adds names to the entry (`av$1` beside `av`), and nothing checks that the old names keep their bindings.
- `Correct` adds: the entry's public surface (name → binding) is the bundle's, plus glue names. `mkPlan` could reuse `export { X as av }` for an importer of `X` (`import { av as X }`) and add fewer names.
- The same step can fold the entry-only rule for `import` and `export` statements into `Valid` (TODO.md).

### 5. Import-only edges for evaluation order (1 entry, likely more after step 1)

`mkPlan` adds a bare `import "./m.mjs"` when a statement reads a name at init time through its `reads` closure, so the declaring module runs first. `early_entry_importer…` needs it. The splitter is untrusted: `Valid` still checks the result.

### 6. Imports and several chunks (18 entries: 12 multi-chunk, 4 `ImportDeclaration`, 2 specifier selectors)

- `lowersplit` takes a chunk with imports from outside modules: the `import` statements stay in the entry, which writes them first, and the order of evaluation is argued, not proved ([`README.md`](README.md) § What is not proved). Model it: `import { x } from "…"` as a hoisted definition of `x` plus a foreign module that evaluates first, so that `OrderOk` covers the order and a module that awaits at its top level is refused or handled.
- The 4 `ImportDeclaration` entries need three things of the adapter: literal values in its dump (`lowersplit` reads an import's source from `lit`; `lowerDump` writes it), the modules written beside the chunk, where its relative imports resolve, and `import` statements left in the entry when debundle's spec moves one. A probe with the first two: `return_object_provider_import` is accepted and prints the chunk's output, `chunk_renames/named_imports` agrees, `chunk_rename_peeled_import` is rejected for an `import` outside the entry, `pruned_import_keeps_its_load_order` fails `OrderOk`.
- Model several chunks as one bundle each, with import edges between them. `Plan.visit` and `OrderOk` run over the union.
- Oracle for the evaluation order: `experiments/es_order/check.mjs` (500 random import graphs against Node). Add cross-chunk graphs.
- Largest step. Do it last.

### 7. Audited premises (5 entries: 3 `annotations`, 2 `chunk_analysis_options`)

- Accept `pure` and `commuting` as explicit assumptions in the `lowersplit` spec: `effect := false`, or drop one conflict pair.
- The certificate prints them. The Lean theorem becomes conditional on them: `Valid B S P` holds under the assumption set. They are the author's claims, so Lean does not check them.

### Not planned

The 36 `reject: stderr` entries (7 run, 29 not run) test debundle's diagnostics (ambiguous selector, unknown anchor, parse errors, `import_order` contract). They have no counterpart in a validity check.

The 80 other entries that are not run today are covered by steps 1, 2, 6 and 7 (54 + 3 + 18 + 5). After those steps only the 36 are left, plus whatever new causes the unlocked entries show.
