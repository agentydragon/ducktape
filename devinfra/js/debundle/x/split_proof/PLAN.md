# Plan

Goal: turn a minified production bundle into readable modules, with a machine-checked guarantee that the split program behaves like the original, resting on as few unproved assumptions as possible, each one stated.
[`docs/lessons_learned.md`](docs/lessons_learned.md) has the experience this plan follows from. [`experiments/translation/PLAN.md`](experiments/translation/PLAN.md) plans the translation checker, and [`TODO.md`](TODO.md) holds the smaller items.

## Direction

The design is "untrusted search, trusted check". The footprint analysis is the one place that breaks it: `Effects.lean` is unproved, and the checker believes it.

**Decided (2026-11-21).** Fix the two blockers that are not about footprints, before any precision work: glue exports that collide with the entry's own export names (step 4 below), and bundles with import statements and several declarators in a statement (parts of steps 2 and 6). In progress.

**Proposed, not decided.**

1. **No new precision rules in the unproved interpreter**, beyond the one or two that a measurement shows are blocking. Each such rule is one more unproved claim that a construct commutes. Fixing a wrong rule is always in scope: F1 to F3 in [`generator_findings`](experiments/translation/debug/generator_findings/README.md) are open, and so are F4 and F5 of ducktape#18429.
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
   - _First measurement:_ which constructs run during module start-up on the large bundle. Only that code needs a precise model, and it may be much smaller than the syntax the inputs contain.
   - _First rule to prove:_ unknown code cannot write a name declared later, except through an object that escaped earlier. It is the largest lever the peel probe measured, and it is exactly the kind of rule that is easy to get wrong in the interpreter.
   - _Success:_ commutation proved for closures, objects with prototypes, accessors and exceptions, and that fragment covers most of the units that get precise footprints today.
   - _Kill:_ the proof is not done within the budget set at the start, or coverage is under about half. Then the tested analysis stays, labelled as trusted.

5. **Keep the bundle's order explicitly, and let footprints remove it.** A planner that chains the modules by bare imports realizes any module order, and `Valid` checks its output as it checks `mkPlan`'s. On the large bundle that makes 2,372 of 2,793 modules of the reviewed tree valid whole, against 886 with today's planner ([`docs/lessons_learned.md`](docs/lessons_learned.md)). The chain is not what a person would write: each edge of it is then a candidate to drop, and dropping one is a claim that two modules commute, which is where a footprint, or a proof, is needed. This turns "is the split valid" from the hard question into the easy one, and makes precision a matter of how natural the imports look. Step 5 below is a special case.

**Open decisions for the owner.** Whether to build the order-keeping planner of item 5 next: it is small, and it changes what the precision work is for. Whether to start the spike, and with what budget. Whether to fix F1 to F5 before or beside it. Whether a reviewed vendor table is worth its review cost before the rule in item 4 exists: alone it does not change which modules peel.

## Order of work

1. Glue export names and chunk input (decided, in progress).
2. The order-keeping planner of item 5, if agreed.
3. F1 to F5.
4. The spike of item 4, if started.
5. Then by measured yield: per-unit reasons in `lower` ([`TODO.md`](TODO.md)), a vendor table by library family, a rule for decorator applications. A stack-safe interpreter comes before any analysis of library code.

## More of debundle's cases through the Lean checker

State: of 207 entries in `devinfra/js/debundle/e2e/cases`, 83 agree with debundle (68 accepts, 15 rejects), 8 disagree, 103 are not run.
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

### 4. Model verbatim `export` names (2 entries, and the `ExportNames` guard goes)

- `Stmt` gains `exports : List (Name × Name)` (public name, local name).
- `mkPlan` reuses a verbatim export when the public name is the local name, and aliases the glue otherwise (`export { av as av$1 }`, `import { av$1 as av }`).
- `Valid` gets an `ExportsUnique` conjunct, and `Linkable` resolves imports through the export map. `Correct` adds: the entry's public surface (name → binding) is unchanged.
- Delete the `ExportNames` rule in `LowerSplit.lean`. The same step can also fold `Reassign` and `ExportOutsideEntry` into `Valid` (TODO.md).

### 5. Import-only edges for evaluation order (1 entry, likely more after step 1)

`mkPlan` adds a bare `import "./m.mjs"` when a statement reads a name at init time through its `reads` closure, so the declaring module runs first. `early_entry_importer…` needs it. The splitter is untrusted: `Valid` still checks the result.

### 6. Imports and several chunks (18 entries: 12 multi-chunk, 4 `ImportDeclaration`, 2 specifier selectors)

- Model `import { x } from "…"` as a hoisted definition of `x` plus a foreign module that evaluates first.
- Model several chunks as one bundle each, with import edges between them. `Plan.visit` and `OrderOk` run over the union.
- Oracle for the evaluation order: `experiments/es_order/check.mjs` (500 random import graphs against Node). Add cross-chunk graphs.
- Largest step. Do it last.

### 7. Audited premises (5 entries: 3 `annotations`, 2 `chunk_analysis_options`)

- Accept `pure` and `commuting` as explicit assumptions in the `lowersplit` spec: `effect := false`, or drop one conflict pair.
- The certificate prints them. The Lean theorem becomes conditional on them: `Valid B S P` holds under the assumption set. They are the author's claims, so Lean does not check them.

### Not planned

The 36 `reject: stderr` entries (13 run, 23 not run) test debundle's diagnostics (ambiguous selector, unknown anchor, parse errors, `import_order` contract). They have no counterpart in a validity check.

The 80 other entries that are not run today are covered by steps 1, 2, 6 and 7 (54 + 3 + 18 + 5). After those steps only the 36 are left, plus whatever new causes the unlocked entries show.
