# Plan: from "a valid translation" to nice code with a proof

State: `translate` accepts a tree that is the bundle renamed and reshuffled, and lists **obligations**: pairs of conflicting units whose order changed. Units bind named cells (`Stmt.writes`), so a statement that assigns a variable, or changes an object, conflicts only with the ones that read or write it. Footprints come from [`../effects/`](../effects/README.md), which runs the bundle's top level over abstract values. Almost every obligation left is an effect-effect pair of units that run code it cannot see.

Acceptance for every step: the fixtures and mutants of `./run.sh --translation` still pass, and the step's own check shows the change on a real bundle.

## 1. Effect summaries: obligations become justified

Delivered: [`../effects/`](../effects/README.md) computes footprints by abstract interpretation, and `translate`, `lower` and `lowersplit` use it. On the large bundle there are 865,930 obligations with no assumption beyond the printed ones (2,059 effect units) and 669,532 with premise P3 (1,583 effect units). Every remaining obligation is an effect-effect pair of units that run code the analysis cannot see ([experiments § 13](../experiments/README.md#13-footprints-by-abstract-interpretation)). Left:

- **A reviewed vendor table.** Imports from outside the tree are the world unless listed. One import is called by 511 effect units and 40 imports by about half of them; 40 entries (keyed by minified names, unreviewed) halve the obligations again, to 344,781. A real table is keyed by package and export path, never by a minified name; agents draft entries one package at a time and a person reviews them; the checker prints the entries it used. `translate --table` already lists the imports a tree relies on.
- **Decorators and registrations.** 527 units apply a decorator helper to a class or a prototype, and all run unknown code because the decorators are vendor functions. A decorator only touches its target and a registry, so two such units on different targets commute. That is a hint (step 2), checked from the footprints.
- **Cost and budget.** 114 s for 9,707 units; 135 units run out of the 100,000-step budget and fall back to unknown code. Profile with the real profilers before raising either.
- **Fewer rules to trust.** Prove each rule of the interpreter against a small semantics of the fragment it mentions, starting with the ones the audit found wrong (calls that are run later or not looked into). The tests are Node-checked reorders and a fuzzer, not proofs.
- **Premise review.** P3 (the world adds no accessors or conversion hooks to program objects) is what lets a property of an escaped object be read like any other; without it every such access is unknown code (865,930 obligations, 669,532 with it). Decide whether it holds for the libraries of each bundle (an observable-state library that defines accessors on instances would break it).

## 2. Hints for what is left

An obligation names two units. A hint says why they commute (`independent: they touch different objects`, `idempotent`) and is keyed by the units' content, not their position, so it survives a new version. The checker accepts a hint only if the facts it names hold.

## 3. Readability pass

Agents rename locals and write comments, one module cluster at a time. After each batch `translate` re-runs. Renames cost nothing to re-check because paired units only have to be equal up to renaming. Comments are not in the AST.

## 4. A new bundle version

The pairing tool matches the previous readable tree against the new bundle: units whose shape is unchanged carry over with their module, names and comments. What does not match goes to an agent, which assigns it or updates the tree. `translate` re-runs. The unmatched units are the drift report. Needs two versions of one app.

## 5. Chunks

Only the entry's export list is checked. Other chunks import from the tree. Check their imports against the tree's exports, and model several chunks as one plan (step 6 of the root [`PLAN.md`](../PLAN.md)).

## 6. Fewer things to trust

- A scope resolver in Lean, tested against `eslint-scope` on real bundles.
- A proof that applying a bijective renaming to a plan preserves `Correct`, so the translation check implies the semantic theorem.
- Re-exports and `export default` outside the entry (rejected today).

## 7. A generator that can reach any program, and a footprint oracle

The fuzzers use three generators, each narrow in its own way: 60 hand-written unit templates (`fuzz.mjs`), a scope-aware corpus of 20 AST node types (`ast_generator.mjs`), and a grammar fuzzer whose programs rarely do anything observable (`shift_fuzz_test.mjs`; premise P4 drops the ones that throw). The reorder oracle also sees a wrong footprint only when a reorder happens to change the output. The [footprint oracle](README.md#the-footprint-oracle) checks footprints directly; what is left is its input:

- **A generator whose range is the whole language.** [`gen/`](README.md#the-generator-of-the-whole-language) has the families `expressions`, `statements`, `functions`, `classes`, `patterns`, `protocols`, `modules` and `unmodeled`. Left: the export forms (`export default`, `export … from` and `export *` are refused by the translation checker, an aliased export needs the splitter to export by alias), `using`, a top-level `await` (the oracle refuses it) and a JSON import with attributes. Steer it by the inventory: every construct `Effects.lean` evaluates and every row of `Intrinsics.lean` gets a production.

A wrong footprint that the oracle or a generator finds is a runnable program in [`debug/generator_findings/`](debug/generator_findings/README.md), and a case in `effects/cases_general.mjs` once the analysis refuses it.
