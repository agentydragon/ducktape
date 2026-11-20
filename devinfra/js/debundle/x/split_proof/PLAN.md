# Plan: more of debundle's cases through the Lean checker

State: of 207 entries in `devinfra/js/debundle/e2e/cases`, 81 agree with debundle (66 accepts, 15 rejects), 10 disagree, 103 are not run.
Causes: [`experiments/debundle_cases/README.md`](experiments/debundle_cases/README.md); numbers: [`results/summary.md`](experiments/debundle_cases/results/summary.md).

Acceptance for every step, after `./run.sh --debundle-cases`:

- `unsound-accept` stays at 0. A step that unlocks entries is also a soundness test: the first run of the corpus found two accepted splits that fail to load.
- The step's entries leave `unsupported` and `strict-*`, or the PR says why one stays.
- `./run.sh --controls --fuzz` and the mutation test still pass.

Steps are independent PRs. Steps 2–5 touch different Lean files and can run in parallel. Order by entries unlocked per effort:

## 1. Take selector resolution from debundle (54 comparable entries; 12 more are refusal tests)

The adapter resolves `binding` selectors and hole-free templates by exact AST shape. debundle resolves holes, alpha-renaming, statement ranges and the dataflow selectors (`passed_to_call`, `member_of_module`, `cross_ref`, `reads_member`, `makes_decorate_call`).

- **Do not port the matcher.** 42 entries need holes, alpha-renaming and ranges. 24 need dataflow facts. debundle's resolver is a shape matcher plus a solver ([`docs/selector_resolution.md`](../../docs/selector_resolution.md)). A JS port would have to follow both.
- **Read debundle's answer instead.** Run `debundle run` per entry and read the owner of every source unit. `modules.json` has `source_unit_ids` per module and `dependency_graph.json.gz` has `unit:N` nodes ([`docs/wire_format.md`](../../docs/wire_format.md)). Check whether the nodes carry input ranges. If not, add one side output to debundle: unit → input range → owner path.
- The adapter maps units to statements (declarators after step 2) and writes the `lowersplit` spec as today.
- This makes `Valid` an independent second opinion on debundle's gate for every spec debundle resolved.
- The 12 refusal entries stay `refusal-test`.
- Open: how the harness runs the `//devinfra/js/debundle:debundle` binary, and what one call costs.

## 2. Declarator granularity and repeated `var` (6 comparable entries: 3 not run, 3 `strict-but-runs`; 2 more are refusal tests)

- Split `var`/`let`/`const a = …, b = …` into one statement per declarator before analysis. Both are sequential, so behavior is the same. debundle's units are declarators too. A destructuring declarator stays whole (debundle refuses to split one: `comma_list/destructure_split_across_modules_is_rejected`).
- `UniqueDefs` becomes "one module per name". Several `var` declarations of a name may sit in one module and keep their order, because they conflict. This changes `Semantics.lean`: the last declarer wins. `split_correct` needs a new proof.
- Entries: 2 of the 4 `statement claimed by two modules` (the other 2 are refusal tests), `sibling_declarator_ownership`, and the 3 `UniqueDefs` rejections.

## 3. Purity precision (3 of 5 `strict-but-runs` left)

[`Effects.lean`](lean/Experiments/Effects.lean) runs pure local calls and applies a table of standard-function summaries ([`experiments/effects/`](experiments/effects/README.md)); `initialization/frozen_callback` and `native_collections/duplicate_primitive_spread` pass now. Left:

- `emission/proxy_constructor/fresh_args`: `new Proxy` is a call into the world. A proxy runs its handler's traps, and an empty handler can gain one later, so a row needs a soundness argument ([`docs/purity_soundness.md`](../../docs/purity_soundness.md)).
- `callbacks/from_entries_map_does_not_run_stored_callback_during_construction`: the result is exported, and what is read from an exported object is the world's, since a module in a cycle may have changed it. A claim in the spec that no cyclic importer observes it would clear it.
- `initialization/frozen_record`: the declaration of `later` moves before the unit that gives `peek` to the world. Moving a declaration earlier is harmless when nothing in between can call `peek`; the model orders every reader and binder pair. A hint (step 2) says it.

Guard anything added with the corpus' rejections that must stay rejected (`proxy_constructor/effectful_proxy_keys`, `freeze_effectful_argument`, `freeze_proxy_receiver`), the fuzzer and the mutation test.

## 4. Model verbatim `export` names (2 entries, and the `ExportNames` guard goes)

- `Stmt` gains `exports : List (Name × Name)` (public name, local name).
- `mkPlan` reuses a verbatim export when the public name is the local name, and aliases the glue otherwise (`export { av as av$1 }`, `import { av$1 as av }`).
- `Valid` gets an `ExportsUnique` conjunct, and `Linkable` resolves imports through the export map. `Correct` adds: the entry's public surface (name → binding) is unchanged.
- Delete the `ExportNames` rule in `LowerSplit.lean`. The same step can also fold `Reassign` and `ExportOutsideEntry` into `Valid` (TODO.md).

## 5. Import-only edges for evaluation order (1 entry, likely more after step 1)

`mkPlan` adds a bare `import "./m.mjs"` when a statement reads a name at init time through its `reads` closure, so the declaring module runs first. `early_entry_importer…` needs it. The splitter is untrusted: `Valid` still checks the result.

## 6. Imports and several chunks (18 entries: 12 multi-chunk, 4 `ImportDeclaration`, 2 specifier selectors)

- Model `import { x } from "…"` as a hoisted definition of `x` plus a foreign module that evaluates first.
- Model several chunks as one bundle each, with import edges between them. `Plan.visit` and `OrderOk` run over the union.
- Oracle for the evaluation order: `experiments/es_order/check.mjs` (500 random import graphs against Node). Add cross-chunk graphs.
- Largest step. Do it last.

## 7. Audited premises (5 entries: 3 `annotations`, 2 `chunk_analysis_options`)

- Accept `pure` and `commuting` as explicit assumptions in the `lowersplit` spec: `effect := false`, or drop one conflict pair.
- The certificate prints them. The Lean theorem becomes conditional on them: `Valid B S P` holds under the assumption set. They are the author's claims, so Lean does not check them.

## Not planned

The 36 `reject: stderr` entries (13 run, 23 not run) test debundle's diagnostics (ambiguous selector, unknown anchor, parse errors, `import_order` contract). They have no counterpart in a validity check.

The 80 other entries that are not run today are covered by steps 1, 2, 6 and 7 (54 + 3 + 18 + 5). After those steps only the 36 are left, plus whatever new causes the unlocked entries show.
