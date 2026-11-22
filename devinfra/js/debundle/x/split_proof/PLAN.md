# Plan

Goal: turn a minified production bundle into readable modules, with a machine-checked guarantee that the split program behaves like the original, resting on as few unproved assumptions as possible, each one stated.

The work was parked on 2026-11-21. This file is the hand-over: what exists, what to do next and in which order, and what is still owed. [`docs/lessons_learned.md`](docs/lessons_learned.md) has the experience behind it, [`SUPPORT.md`](SUPPORT.md) what a debundler has to support, [`experiments/translation/PLAN.md`](experiments/translation/PLAN.md) the plan of the translation checker, and [`TODO.md`](TODO.md) the smaller items.

## State when parked

The design is "untrusted search, trusted check": a planner proposes a split, and Lean checks it (`Valid`, nine checks; `Valid → Correct` is proved). The check trusts one input that is not proved, the footprint of each statement, which [`Effects.lean`](lean/Experiments/Effects.lean) computes by abstract interpretation.

- **The tool works on a real bundle.** On the large bundle (9,707 units, 2,793 reviewed modules) `lowersplit` accepts a split that keeps 2,366 modules whole and runs them in an order that keeps every conflicting pair of statements as in the bundle (`"sequential": true`; it is called the bundle-order split, but it does not keep the order of all statements), and one of 886 modules with no order kept. These counts are under premise P3. Without it the conflict rule keeps 2,364 whole and 886, and `lowersplit` accepts the bundle-order split of 2,364 (all nine checks). The first loaded in a browser like the original, before the last rule fixes; it was not loaded again after them.
- **The footprint analysis is tested, not proved.** Seven wrong rules were found by generators and a footprint oracle (F1 to F7); six are fixed, and F7 is answered by not assuming premise P3 for real inputs. A `throw` that is reached and not caught is unknown code. 288 golden cases, the fuzzers, the footprint oracle and a mutation harness guard it ([`experiments/effects/README.md`](experiments/effects/README.md)).
- **A model of JavaScript exists to prove against**: `CoreJS` ([`experiments/README.md`](experiments/README.md) § 14), a total interpreter in Lean tested against Node. It has closures, objects with prototypes and accessors, plain classes, exceptions, modules, and a world as a parameter for host functions on primitives. `fuel_mono` makes "the program ends with `r`" well defined. A four-way test runs bundle and split, each in Node and in the model, on what `lowersplit` accepts.
- **One footprint fact is proved in the model**, for every world: a declaration whose initializer only allocates changes nothing that existed and binds only its name (`decl_allocOnly`). It is not connected to `Effects.lean`.
- **The first slice of the end-to-end theorem is proved**, for every world and every ending: two programs that hold the same statements in the same order, with scopes that agree on the names each statement mentions, end in equal states (`order_kept_preserves`, [`Split.lean`](lean/Experiments/CoreJS/Split.lean)). For a plan that `Valid` accepts and that keeps the order, the only other hypothesis is that the footprints say which names each statement declares and mentions (`valid_order_kept_preserves`, `FootprintsOfSyntax`). Both are in `corejs_axioms_test`.
- **The second slice moves code** (step N4, 2026-11-21): a split that `Valid` accepts ends normally in a state exactly when its bundle does, if each pair it runs out of order passes `MayMove`, a syntax check: function declarations, and statements that only allocate and do not read each other's names (`moves_preserve`, [`Move.lean`](lean/Experiments/CoreJS/Move.lean)). An opaque statement that only makes functions (a function declaration, or a declaration whose initializer is a function or an arrow, whatever its parameters and body) binds them without the world, so it counts too. On the large bundle `MayMove` covers 6,742 of 9,707 units (69%), against 73% predicted; the rest are opaque statements that run code: 1,333 declarations that call a function, 607 expression statements, 421 classes, 218 other declarations (§ 14 of [`experiments/README.md`](experiments/README.md)). The verdict of `lowersplit --corejs` decides its hypotheses (`movesOk`, equal to the last by `movesOk_iff`) when the order is not kept, and names it for a split of the large bundle that keeps 2,368 of its reviewed modules whole.
- **The slice covers real bundles** (step N2, 2026-11-21). A statement outside the model's syntax is an opaque statement that the world runs, and the world can reach only what it was handed ([`experiments/README.md`](experiments/README.md) § 14, stage two). The theorems hold unchanged with it. On the large bundle the front end takes 4,071 of 9,707 units into the model's syntax and makes 5,595 opaque; it refuses 41, for `import()`. `corejs --footprints` checks `FootprintsOfSyntax` for all its statements, and `lowersplit` accepts its bundle-order split of 2,364 whole modules without P3. That split does not keep the order, though: its modules run in the order their imports nest, and 9,708 of its 9,709 statements run out of place.
- **The verdict names the slice** (step N3). `lowersplit --corejs program.json` decides every hypothesis of `valid_order_kept_preserves` for the split it checked, on its own footprints, and prints the slice or what it lacks ([`README.md`](README.md)). On the large bundle it names the slice for an order-kept split of 4,091 modules (each a run of one reviewed module, joined where `AssignsHome` asks), with the 41 units with `import()` taken as opaque: faithful because every module stays in the chunk's directory. For the split of 2,364 whole modules it says none: the plan does not keep the order. The evidence is in the private repository (`peel-probe/`, § The verdict names the slice). What stays trusted: the model (of the language and of modules, including the meaning of an opaque statement), that `≈` is the right comparison (here it is equality of results), and the front end's reading of which names a statement declares and mentions (from `eslint-scope`).
- **Measured on the inputs** (3,363 files of the private repository): nearly the whole language and standard library occur ([`HOST_FEATURES.md`](HOST_FEATURES.md), [`SUPPORT.md`](SUPPORT.md)). Start-up of the large bundle enters 1% of the application chunk's functions and 10% of the vendor chunk's, and still uses 37 of 45 tracked language features and about 110 standard functions.

## Picking this up

- **Build and test.** `./run.sh` and its flags ([`README.md`](README.md)); CI runs `bbr test //devinfra/js/debundle/x/split_proof/...`. By hand, in `lean/`: `nix shell nixpkgs#lean4 nixpkgs#gcc --command lake build EsmSplit render Experiments lower lowersplit evalorder translate corejs` (about 2 minutes clean). The golden cases: `node ../effects/run.mjs` in `experiments/translation` (90 s). The model: `node differential.mjs --count 40 --seed 1` in `experiments/model` (20 s). Each `experiments/*/package.json.in` is copied to `package.json` before `npm ci`; never commit a `package.json`.
- **Generated coverage for supported features.** When adding a language feature or a proved move rule, teach a program generator to compose it with varied operands or surrounding statements. Include a CI seed sample that executes it through the relevant checker or interpreter and asserts the observed behavior. For a move rule, require a reordered plan that uses the rule and a dependency-bearing refusal. Track remaining gaps in the [CoreJS differential inventory](experiments/model/README.md) and the [translation coverage audit](experiments/translation/COVERAGE.md).
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
- **`run P'` needs modules in the model.** They are in it: a program is a set of modules, linked before any runs, with imported names as the exporter's bindings and the specification's order of evaluation ([`experiments/README.md`](experiments/README.md) § 14). The model's order is `Plan.visit` (`split_visit` in [`Split.lean`](lean/Experiments/CoreJS/Split.lean)), and a split that `Valid` accepts and that keeps the order ends as its bundle does (`valid_order_kept_preserves`).
- **A run that reaches unknown code says nothing after that point.** The first version covers programs that stay inside the model. Real bundles do not: 1,751 of the large bundle's 9,707 units touch the world, without P3. The vendor code is in the bundle's chunks, so it can be part of the program; then only the host is unknown. Either way the model has to take the world as a parameter, a function that may read and change what it is given and nothing else, and the theorem holds for every world. Both stages exist ([`experiments/README.md`](experiments/README.md) § 14): host functions on primitives, and opaque code that is handed objects, which the order-kept slice uses. A frame lemma for opaque code, which a split that moves code needs, is not proved.
- **The standard library cannot be defined function by function.** Start-up runs little code and most of the language ([`docs/lessons_learned.md`](docs/lessons_learned.md) § The first slice of a model): about 110 standard functions by name, regular expressions among them. They enter the model as part of the world, each with a stated class of behaviour ("a function of its primitive arguments", "reads its receiver", "may call its argument"). That table stays an assumption about the engine, tested and not proved. 88 functions of the first two classes are in it (`worldFunction`), and the test replays Node's answers for them.

**Decided so far.**

- **No new precision rule goes into the unproved interpreter** unless a measurement shows it is blocking. Fixing a wrong rule is always in scope, and its cost on the large bundle is measured before it is adopted.
- **The model is built up beside the tested analysis**, as a spike with a kill rule: stop if opaque statements do not keep the order-kept theorem, or if the proved part covers under about half of the units that get precise footprints today. Step N2 passed it on 2026-11-21: the theorem holds with opaque statements, and on the large bundle the order-kept slice covers the 99.6% of units the front end accepts. Counted strictly, the units in the model's syntax (4,071) are 51% of the 7,956 that get precise footprints.
- **A proof for a narrow fragment is worth landing.** Each is its own slice of the theorem, and the tool's verdict says which slice covered a split (`sliceLines` in [`LowerSplit.lean`](lean/Experiments/LowerSplit.lean)); a new slice adds its check there.
- **What is refused** ([`SUPPORT.md`](SUPPORT.md)): a top-level `await`, a top-level `using`, a direct `eval` anywhere, a name declared twice in one list. A `Proxy` is never covered by premise P3.
- **Premise P3 is not assumed for real inputs** (finding F7, 2026-11-21). Code of the program that the world runs can add a getter or a `toString` to an object the world holds, and the Claude bundles do it. Without P3 the analysis is sound for that, and the cost is small: 3 to 6% more units touch the world on three production bundles, and peeling barely moves ([`generator_findings`](experiments/translation/debug/generator_findings/README.md)). The cases and fuzzers still set P3, to test the analysis under it.
- **An exception that ends evaluation**: a `throw` statement keeps its place; an exception the language raises itself is covered by the hypothesis that the bundle's evaluation completes.

**Open decisions for the owner.**

- How the four input files with a direct `eval("require")` are admitted: a check of the string, or an audited premise.
- Whether a reviewed vendor table is worth its review cost. Alone it does not change which modules peel.
- How readable names reach certified output. Near term: debundle emits its tree even unproved, and `translate` with the slice verdict referees it. The end state should not keep two referees (debundle's gate and Lean); which one remains is open.
- Whether the vendor chunk becomes part of the program in the model. It removes most unknown code, and the model then has to run what the vendor code runs at start-up, regular expressions included.

## Next steps

Two tracks. The proof track is ordered: each step needs the one before. The engineering track is independent of it and of itself, so its steps can run in parallel. Every step ends with its checks run and its numbers in the PR.

### Excalidraw

Defer modeling or rewriting `import.meta.url`. Start with the editor prefix split in the [Excalidraw census](experiments/translation/debug/excalidraw_certificates/README.md). Its 888 candidate units include the worker factory (unit 872), which reads `import.meta.url` when its static method runs. Keep that unit in the editor entry at the original chunk URL and try extracting the other 887.

Run the structural checks and the CoreJS checker on each complete candidate split. Record how many units move, which splits pass the structural checks, and which receive a CoreJS equivalence certificate under the stated premises. If the retained factory still blocks a whole-split certificate, record that verdict and try other placements or chunks whose certification does not depend on module URLs. Report the first blocker for each unsuccessful probe.

Seven of the editor's eight flat destructuring declarations read keys present in
four allocation-only object literals. Units 95 (`s6`), 103 (`Ui`), 201 (`I2`)
and 299 (`x5`) have 5, 28, 5 and 5 distinct own keys. This accounts for 938
candidate crossings. The remaining 134 involve unit 325 reading `Math`.
[`literalVar_origin`](lean/Experiments/CoreJS/RecordOrigin.lean) establishes the
binding and own data descriptors immediately after each qualifying declaration.
For the first `Ui` read at 105, establish that the pattern at 104 preserves
that record. Unit 104 reads `s6`, created at 95, and unit 99 executes an opaque
`new Uint32Array(f6())` between creation and use. The next proof obligation is
to show which bindings and heap records survive that intervening execution.
[Editor move census](experiments/translation/debug/excalidraw_certificates/README.md)

### Proof track

**Derive CoreJS from the annotated AST.** The checker and interpreter share the first Lean lowering rules for declarations, primitive literals, binding reads and local named exports. Extend expression and statement coverage against the existing JavaScript lowerer and Node. Before migrating opaque fallback, check exactly which cells a deferred body captures, its part number and the handling of direct eval, module identity and top-level await. Replay Excalidraw on the AST alone before retiring the external checker input.

**Declaration extraction and the output boundary.** For closed modules consisting of identifier declarations with primitive initializers or binding reads (including inline named exports), Lean relates evaluation of the original lists to the checked CoreJS plan. It instantiates the whole module before the first initializer and preserves abrupt completion on an order-kept plan. The checker compares the complete extracted statements before reporting coverage. Extend this to mixed modules as their source semantics becomes available. The next boundary is showing that parsing the emitted modules gives the checked plan, including its renames and links. Parser correctness and binding resolution remain premises. [Declaration certificate](lean/Experiments/CoreJS/DeclarationCertificate.lean)

**N4. Widen the slice that moves code.** `moves_preserve` is proved; what `MayMove` covers is 69% of the large bundle's units.

The active [footprint soundness work](docs/footprint_soundness.md) establishes a compositional contract for checked Effects output, starting with binding-only statements and keeping unsupported analysis rules outside the proved slice.

- _Opaque-code frame proof:_ [`OpaqueFrame.lean`](lean/Experiments/CoreJS/OpaqueFrame.lean) proves conversation framing from per-request conditions and commutation with allocation-only declarations in both directions. [`Reachability.lean`](lean/Experiments/CoreJS/Reachability.lean) now defines distinct object and binding nodes, rooted in handed objects and supplied bindings, following binding values, properties, accessors, prototypes and closure captures (including `this` and opaque captured cells). A closed envelope contains this least reachable set and excludes the protected region and initializer read bindings. [`RequestFrame.lean`](lean/Experiments/CoreJS/RequestFrame.lean) derives framing for the actual binding read, initialization, assignment and own-key requests from that invariant. It preserves the envelope on normal, thrown and unknown outcomes, including handing out an object and publishing an alias through another binding. Kernel-checked examples cover separated mutation, caught TDZ errors, immutable writes, forged handles and hidden captures.
- [Object allocation requests](lean/Experiments/CoreJS/RequestAllocation.lean) preserve the protected region and read bindings. The reachable-object envelope can grow by the newly allocated address. An explicit prototype comes from a supplied handle; default construction requires the intrinsic Object.prototype in the envelope. In the Excalidraw editor, unit 99 names only `Uint32Array` and `f6`, and the closure initialized at 98 has no free names. The frame for unit 99 must also cover callbacks and handles already handed to the world.
- _Next for opaque code:_ prove function allocation and callback requests preserve the invariant, add roots for other operations that introduce built-in objects, and handle property traversal (whose interpreter bound uses the total object count). Then establish the invariant after linking and through the prefixes used by reordering. The restricted request theorem does not certify a whole arbitrary opaque conversation, and `MayMove` is unchanged. Its current `Commute` theorem quantifies over arbitrary states: an opaque statement can call a previously handed closure that reads a moved binding absent from its direct free names. A footprint/name-disjointness test alone does not establish separation. Measure newly certified whole modules and failing pairs when a proved condition reaches the verdict.
- _Needs:_ classes in `Expr.allocOnly` (`allocOnly_keeps` and `allocOnly_local` for `evalMembers` and the static field initializers): 421 opaque units, most of them blocked by a standard global (251) or `extends` (162).
- _Measured_ (2026-11-21, the verdict's moves case): the 886-module split runs 5,420,546 pairs out of order and 134,859 fail `MayMove`; the bundle-order split of 2,364 runs 2,571,190 and 1,395 fail. A split built with `MayMove` as one more conflict keeps 2,368 modules whole and the verdict names the moves slice for it.
- _What blocks the reviewed tree_ (2026-11-21, against the tree's own module order; scripts in gaffer-private `certified-split/`): 1,961,504 of its 7,611,199 out-of-order pairs fail `MayMove`.
  - About 1,110,000 pair a statement that only allocates with an opaque one whose footprint never reaches its names. A frame lemma for opaque code (a unit the world runs commutes with an allocation it cannot reach) covers them. The count is gated on footprints, not proved. On the bundle-order split it covers 889 of the 1,395.
  - 141,696 reorder two statements that the footprint analysis calls effects. No `MayMove` rule covers these. The world calls commute, or the spec is wrong: decide case by case.
  - Of the 718,365 failing pairs where neither statement is an effect, greedy by pairs resolved: classes 213,764; calls of bundle functions and IIFEs +119,877; pure expressions (operators, property reads, templates) +282,425; standard built-ins (`Map`, `Set`, `RegExp`, `Object.*`) +101,547.
  - On the bundle-order split, by reviewed modules freed: classes 15, built-ins +23, pure expressions +27, calls of bundle functions +57, the world +3 (125 in all).
- _Done when:_ the share is near the census's 73%, or the shortfall is explained by counted statement kinds.
- _Then delete `MiniJS`_ ([`lean/Experiments/MiniJS.lean`](lean/Experiments/MiniJS.lean) and its two companions): `CoreJS` replaces it now that commutation is proved there.

**N5. Remove the imports that exist only for order.** The bundle-order split of the large bundle has 2,744 bare imports in 629 modules, nested 466 deep. Each is a candidate to drop, and dropping one is a claim that two modules commute. First candidates: a forward mention of a module that only declares functions. The count of ordering imports going down is the measure of how readable the output is. The second rule to prove belongs here: unknown code cannot write a name declared later, except through an object that escaped earlier. The peel probe measured it as the largest lever (873 modules that peel alone would become about 1,624, an optimistic count).

### Engineering track

**E2. `import.meta`.** `translate` rejects a unit that reads it, except `new URL("/absolute", import.meta.url)`; 71 input files read it at the top level. What moving code changes is `import.meta.url` and what is resolved against it. Supporting it means rewriting or pinning the base when a unit moves to a module in another directory. Done when the fixtures `meta_relative` and `meta_absolute` have accepted counterparts that Node agrees with, and the count of input files refused for it is reported.

**E3. The language features that keep units out of the model**, ranked by how many of the large bundle's 5,636 opaque units each one blocks, counting every feature a unit holds, in function bodies too (the first number), and the units it alone blocks (the second). The rank is static presence, not what runs at start-up: a statement is opaque for a feature anywhere in its text.

- Imported names as bindings of other modules (cross-chunk imports): 2,832 / 1,194. Alone it takes the units in the model's syntax from 42% to 54%.
- Optional chains: 1,739 / 218. Destructuring or default parameters: 1,641 / 159. Standard globals beyond the host table (`Object`, `Map`, `Error`, …): 1,443 / 367. Destructuring elsewhere: 1,160 / 64. `async` and generators: 1,028 / 65. Spread: 939 / 42. `for`-`of`: 842 / 59. With these eight, 86% of the units are in the model's syntax.
- Late: browser globals (727 / 107), regular expressions and BigInt (187 / 81), `class … extends` (162 / 68), `switch` (150 / 21), `for` with several declarations (66 / 66). `extends` needs the prototype chain of constructors, `super(…)` and `super.m()`, and the dead zone of `this` before `super` returns.
- For the slice that moves code these matter less than classes: 1,333 of the opaque units that are left are declarations that call a function, and no feature makes those movable under `MayMove`.

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
- `ScopesAgree` has no instance proved in Lean, so `order_kept_preserves` has no worked example inside Lean. The evidence that its hypotheses hold for real splits is the test's comparison of final states, which is by a hash of a text of the state: the name, the parameters and the kept bindings of each function, with the bodies compared apart as tokens.
- `AssignsHome` has to hold for `splitProgram` to be what the front end reads from the files the tool writes, and it is not a hypothesis in Lean. The test checks per statement that the tokens are the bundle's.
- Only 19 to 28 of the accepted splits of a run of the four-way test run statements in another order than the bundle, so that test says little about reordering.
- The recorder of host calls takes a call for the program's when a stack frame's file is under the test's work directory. A change in how Node reports frames breaks it loudly, by a replay that does not match.
- [`Scope.lean`](lean/Experiments/CoreJS/Scope.lean) needs a raised heartbeat limit for the expression case and takes about 35 s to check.

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

**TODO (eventual authoring workflow).** Let authors keep the same spec across bundle rebuilds that rename bindings or shift statement ordinals. Rust debundle already resolves stable selectors such as AST templates with holes, distinctive string literals and relationships to other declarations. Consider a substring anchor if an exact literal is too brittle. Reuse the Rust resolver to turn the authored spec into concrete unit-to-module assignments for each input. For now the Lean splitter takes a declared binding name or a zero-based `#n` and its destination module. Selector resolution is a separate authoring problem and can wait while we prove the correctness of a given placement. [Rust selector syntax](../../docs/selectors.md) · [How Rust resolves selectors](../../docs/selector_resolution.md)

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
