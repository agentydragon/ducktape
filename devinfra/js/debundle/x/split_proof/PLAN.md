# Plan

Goal: turn a minified production bundle into readable modules, with a machine-checked guarantee that the split program behaves like the original, resting on as few unproved assumptions as possible, each one stated.
[`docs/lessons_learned.md`](docs/lessons_learned.md) has the experience this plan follows from. [`experiments/translation/PLAN.md`](experiments/translation/PLAN.md) plans the translation checker, and [`TODO.md`](TODO.md) holds the smaller items.

## Direction

The design is "untrusted search, trusted check". The footprint analysis is the one place that breaks it: `Effects.lean` is unproved, and the checker believes it.

**Decided (2026-11-21).** The model of item 3 is built up as a longer-term investment beside fixing the known wrong rules. Its first slice exists, and the first rule proved against it is the allocation rule of item 4.

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

**Proposed, not decided.**

1. **No new precision rules in the unproved interpreter**, beyond the one or two that a measurement shows are blocking. Each such rule is one more unproved claim that a construct commutes. Fixing a wrong rule is always in scope: F1 to F6 are fixed, and F7 in [`generator_findings`](experiments/translation/debug/generator_findings/README.md) is open (premise P3 and code the world runs).
2. **When to stop chipping at the interpreter.** Two signals, measured after every round of fixes:
   - _Soundness does not converge._ Run the same independent attack and the footprint oracle again. If the count of new wrong footprints does not clearly fall, the analysis is not getting safer.
   - _Precision yield falls._ If the next rule unlocks only a few percent of the blocked peels, the rest is a long tail.

   The record so far (dozens per attack, a new finding for each part of the language the generator reached) says the first signal has fired.

3. **The alternative: footprints as a checked certificate.**
   - A JavaScript execution model in Lean for a core fragment, written as an executable interpreter, so it can be tested against Node and test262: a program has one right answer, a footprint does not.
   - A small footprint checker proved sound against that model, and the commutation of units with disjoint footprints proved once in it (today only the tiny `MiniJS` derives this).
   - `Effects.lean` stays, as the untrusted proposer. A wrong proposal is then rejected instead of accepted.
   - Anything outside the fragment is unknown code under one stated assumption: it reads and changes only what it can reach. The inputs use nearly the whole language and standard library, so the fragment cannot be "what the inputs use".
   - This does not decide program equivalence. The check stays a sufficient condition; the change is that the condition is proved.
   - **It can be gradual, because the rules are already Lean code.** A rule is proved against the model for the part of JavaScript the model covers; a rule that is not proved yet stays, as a named assumption. The soundness theorem is then "sound if these named rules are", and the list shrinks one rule at a time. Three things have to exist before the first rule can be proved:
     - what a footprint means in the model (the invariant that relates the interpreter's abstract values to real states); every rule's proof is a step of it, so it comes first;
     - rules that can be taken one at a time: a typed syntax tree and total functions instead of `partial` ones over JSON ([`TODO.md`](TODO.md)), with one definition per construct;
     - a ledger: the interpreter records which rules a unit's footprint used, and a verdict prints the unproved ones. "Units whose footprint used only proved rules" is then the measure of progress, and how often a rule fires on the large bundle orders the proofs.

4. **Run it as a spike with a kill rule**, so it cannot become open-ended.
   - _Exists:_ the model, `CoreJS` ([`experiments/README.md`](experiments/README.md) § 14): closures, objects with prototypes and accessors, plain classes and exceptions, as a total interpreter tested against Node, with unknown code as an outcome of its own.
   - _First rule, proved in part:_ a unit that only allocates has no footprint beyond the name it declares. Proved for one unit built from literals, names, functions, object and array literals: it changes nothing that existed and binds only its name. 7,123 of the 9,707 units of the large bundle only allocate ([`allocate_only_census.mjs`](experiments/translation/allocate_only_census.mjs)); that count includes plain classes, which the proof does not cover yet.
   - _Addresses are settled by the model:_ two such units in the other order create the same objects. The model gives each unit a region of its own to allocate in, so the two orders end in equal states and no renaming of object identities is needed.
   - _Next:_ the same rule for plain classes; that two such units commute (`AllocOnlyUnitsCommute` in [`Frame.lean`](lean/Experiments/CoreJS/Frame.lean), stated, not proved); then what a footprint means in this model, the invariant that relates `Effects.lean`'s abstract values to its states, including what unknown code may change.
   - _What the model covers next, from the measurement of start-up:_ host code that is handed objects (arrays first: their methods are what ends most runs of generated programs now); `class … extends`, which every start-up evaluates; then the syntax that runs rarely in an application chunk (spread, destructuring, optional chains, `for`-`of`). Modules and host functions on primitives are covered.
   - _Second rule:_ unknown code cannot write a name declared later, except through an object that escaped earlier. It is the largest lever the peel probe measured, and it needs a statement of what unknown code may change.
   - _Success:_ commutation proved for closures, objects with prototypes, accessors and exceptions, and that fragment covers most of the units that get precise footprints today.
   - _Kill:_ the proof is not done within the budget set at the start, or coverage is under about half. Then the tested analysis stays, labelled as trusted.

5. **Let footprints remove the order that a sequence imposes.** A spec with `sequential` keeps the bundle's order by imports, and `Valid` checks the plan ([`README.md`](README.md)). On the large bundle that makes 2,367 of the 2,793 modules of the reviewed tree valid whole, and `lowersplit` accepts the split. It is not yet what a person would write: 629 of its 3,049 modules get imports they need only for the order (2,744 bare imports), and the imports nest 466 deep. Each such import is a candidate to drop, and dropping one is a claim that two modules commute, which is where a footprint, or a proof, is needed. First candidates: a module that only declares functions can run early without harm, so a forward mention of one needs no ordering import. This turns "is the split valid" from the hard question into the easy one, and makes precision a matter of how natural the imports look. Step 5 below is a special case.

**Open decisions for the owner.** What budget the spike gets before its kill rule applies. Whether a reviewed vendor table is worth its review cost before the rule in item 4 exists: alone it does not change which modules peel.

## Order of work

1. F7: a rule that reads the text of escaped code for the constructs that add a hook to an existing object, measured on the large bundle before it is adopted.
2. In the model: the second stage of the world, host code that is handed objects. Modules, `fuel_mono` and the first stage of the world (host functions on primitives) exist.
3. Two slices of `split_preserves`, independent of each other: a split that keeps the bundle's order, which needs no footprint rule; and units that only allocate may move, which needs the allocation rule of item 4 for plain classes and the commutation of two units.
4. `import.meta` ([`SUPPORT.md`](SUPPORT.md)): the one common feature the tools refuse.
5. Which rules fire per unit at start-up: it orders the rules to prove after the allocation rule.
6. Then by measured yield: fewer ordering imports (item 5): per-unit reasons in `lower` ([`TODO.md`](TODO.md)), a vendor table by library family, a rule for decorator applications. A stack-safe interpreter comes before any analysis of library code.

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
