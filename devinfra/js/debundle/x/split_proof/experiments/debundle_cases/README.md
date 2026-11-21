# debundle's e2e cases against the Lean checker

debundle moves the top-level statements of a bundle into ES modules, as this project does, but without Lean.
Its data-driven corpus is [`e2e/cases`](../../../../e2e/cases): 184 case directories, 207 entries.
An entry is a bundle (`main.js` or `static/app.js`), a spec, and the verdict debundle must give: `accept` with the expected stdout, or `reject` as `atomic_conflict`, `cycle` or a stderr text.
`run_cases.mjs` turns each entry into a `lowersplit` run and compares the verdicts. A run takes about 10 s.

Results: [`results/summary.md`](results/summary.md) (generated tables) and [`results/cases.jsonl`](results/cases.jsonl) (one line per entry).
The numbers in this file are from the corpus as of the commit that added this directory. Re-run to refresh them.

## Run it

From the project root: `./run.sh --debundle-cases` (builds `lowersplit`, installs `acorn` and `yaml`, runs the corpus, writes the summary).
By hand, in this directory:

```bash
cp package.json.in package.json && npm ci --no-audit --no-fund
node run_cases.mjs [--filter <substring>] [--keep <dir>]   # writes results/cases.jsonl
node summarize.mjs results/cases.jsonl > results/summary.md
```

`--keep <dir>` copies each translated entry (bundle, translated spec, emitted modules, checker output) to `<dir>/<id>`.

## What the adapter does

1. `acorn` parses the case's one chunk. Each top-level statement gets the names it declares. A `var` nested in a block counts.
2. The debundle spec becomes a `lowersplit` spec. `binding` selectors and hole-free `source_matches` and `anonymous_statements` templates (matched by exact AST shape) become statement numbers. `import_order` becomes the import priority (`Spec.order`).
3. Unclaimed statements follow `unassigned_mode`. With `inline_in_entry` they stay in the entry. With `catchall_file` the catchall module takes the unclaimed declarations, the unclaimed anonymous statements before its last statement, and everything when it is the only module.
   Later anonymous statements stay in the entry (debundle's rule: [`docs/design.md`](../../../../docs/design.md)). An `export { … }` without a declaration always stays in the entry.
4. `lowersplit --force` checks the split and writes the modules even when a check rejects them.
5. Node runs the original chunk and the emitted entry. The adapter compares verdict and stdout with the entry's expectation.
6. What the adapter cannot translate is `unsupported:<cause>` and is not forced.

## Outcomes

| Outcome             | Entries | Meaning                                                                                                  |
| ------------------- | ------- | -------------------------------------------------------------------------------------------------------- |
| `agree-accept`      | 69      | I accept. The emitted modules print the expected stdout.                                                 |
| `agree-reject`      | 15      | debundle expects `atomic_conflict` (8) or `cycle` (7). I reject too.                                     |
| `strict-but-runs`   | 6       | I reject. debundle accepts. My plan, written anyway, prints the expected stdout.                         |
| `strict-and-breaks` | 1       | I reject. debundle accepts. My plan, written anyway, does not load.                                      |
| `unsound-accept`    | 0       | I accept. The emitted modules print something else.                                                      |
| `lenient`           | 0       | I accept what debundle rejects, and the output is the same.                                              |
| `refusal-test`      | 13      | debundle must fail with a given stderr text. There is nothing to compare.                                |
| not run             | 103     | The adapter cannot translate the spec yet (below). 36 `reject: stderr` entries in all are refusal tests. |

- **No unsound accept in the final run.** The corpus found two before I fixed them (§ Soundness findings).
- **The agreeing accepts are real splits.** 61 of the 69 move statements out of the entry: 159 of 323 statements. The other 8 keep everything in the entry.
- **The rejections are not over-strict.** All 15 expected rejections fail my checks. 14 of them fail `OrderOk`. Written anyway, 14 of the 15 print something else or crash. The 15th, `initialization/nested_closure_rebind/separated`, fails `Reassign` and prints the same output on this run.
- **The premise holds.** For all 76 accept entries that ran, the original chunk prints the expected stdout, so "same as the original" and "as expected" mean the same.
- **I compare only stdout.** 36 of the 69 agreeing accepts also assert `exports`, `source`, `probes` or `check_all_js`. The adapter does not check those.

## Where my verdict differs from debundle's

| Entries                                                                                                                                                | I fail       | Cause                                                                                                                                                                                                                                                                                                 | Owner                 |
| ------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| `initialization/frozen_record`, `emission/proxy_constructor/fresh_args`, `callbacks/from_entries_map_does_not_run_stored_callback_during_construction` | `OrderOk`    | `new Proxy` is a call into the world; what is read from an exported object is the world's; a declaration moves before the unit that gives a callback to the world, and the model orders every reader and binder pair ([`PLAN.md`](../../PLAN.md) step 3). debundle's purity analysis lets these move. | footprint analysis    |
| `emission/duplicate_declaration/repeated_var`, `emission/hoisted_var/repeated_statements`, `initialization/repeated_var_accessor/colocated`            | `UniqueDefs` | Two `var` declarations of one name. That is legal inside one module, and debundle keeps such sites together. `UniqueDefs` wants one declaration per name.                                                                                                                                             | checker (`Plan.lean`) |
| `realizability/initialization_order/early_entry_importer_does_not_pull_scc_in_wrong_order`                                                             | `OrderOk`    | `mod_init` calls `readT`, which reads `T` from `mod_logger`. My plan has no import that makes `mod_logger` run first.                                                                                                                                                                                 | splitter (`mkPlan`)   |

## Why 103 entries do not run

| Family                          | Entries | What is missing                                                                                                                                                                                                                                                                                         |
| ------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Template language               | 42      | `source_matches` with holes (`ANYTHING`, `EXPR`, `STMT`, `ARGS`, …) 26, anonymous templates with holes 3, templates that match no whole statement 8 (alpha-renamed locals, one declarator of a comma list), multi-statement ranges 5. The adapter matches only hole-free templates, by exact AST shape. |
| Dataflow selectors              | 24      | `passed_to_call` 7, `member_of_module` 6, `cross_ref` 4, `reads_member` 4, `makes_decorate_call` 3. debundle resolves them from value flow between modules.                                                                                                                                             |
| Several input chunks            | 12      | One bundle per case is all the model knows. These cases have two or three chunks that import each other.                                                                                                                                                                                                |
| Imports                         | 6       | `ImportDeclaration` is outside the model (4). Selectors on import specifiers (2).                                                                                                                                                                                                                       |
| Statement granularity           | 4       | Two modules claim parts of one statement: the declarators of a comma list, or a binding and an anonymous template. debundle's units are declarators. Mine are statements.                                                                                                                               |
| Audited premises                | 5       | `annotations` 3 and `chunk_analysis_options` 2: facts the spec author vouches for (purity, commuting statements, dataflow summaries).                                                                                                                                                                   |
| Spec-contract violations, parse | 10      | `import_order` omits a module 5, input does not parse 3, `import_order` names a generated residual that stays empty 2. Nine of the ten expect `reject: stderr`: they test debundle's diagnostics. The tenth, `emission/sibling_declarator_ownership`, is the granularity case again.                    |

41 of the template entries and 13 of the dataflow entries expect `accept`. The other 12 expect `reject: stderr`.
In all, 36 entries (13 run, 23 not run) test debundle's error text and have no counterpart here.

## Soundness findings

The corpus found two accepted splits that do not run. Both were front-end gaps. The Lean proofs hold; their premises (footprints, front-end rules) were wrong.

1. **`var` inside a top-level block.** `if (c) { var x = 1; }` declares `x` in the module scope. `Lower.topDefs` ignored it, so the statement declared nothing and a reader could move away from it.
   Four entries were accepted and then failed with `ReferenceError` or `SyntaxError: Export 'shared' is not defined`: `emission/hoisted_var/{complete_statement/claimed,lazy_reader/spec,reader_ownership/claimed,repeated_statements/spec}`.
   I confirmed this by undoing the fix and re-running. Fixed: `topDefs` now collects nested `var` names. `repeated_statements` and `repeated_var_accessor/colocated` are now rejected by `UniqueDefs`.
2. **Export names.** `export { X as av }` publishes `X` as `av`. A module that needs the local `av` imports it from the entry, and the generated glue exported `av` again: `SyntaxError: Duplicate export of 'av'` (`emission/residual_export_alias_collision`).
   The model had no export names. It has them now (`Stmt.exports`): `mkPlan` exports the local as `av$1`, the importer renames it back, and `ExportsUnique` in `Valid` checks that a module's export names differ. The entry is accepted and prints the expected output.

## Not compared

- **Stderr text** of the 36 refusal entries.
- **`exports`, `source`, `probes`, `check_all_js`, `runtime_files`** assertions of accepted entries.
- **Names.** `chunk_renames` and `bindings[].name` do not change validity, so the adapter ignores them. Its entry module is always `entry.mjs`, whatever `entry:` says.
- **Order of debundle's own emission.** The adapter places statements by the spec and lets `mkPlan` derive imports. It does not read debundle's output.

## How debundle's gate and `Valid` relate

debundle decides realizability on a dependency graph of source units (statements, or the declarators of a comma list), quotiented by the requested modules.
Import edges, at-init read edges and side-effect order edges must leave an order that ES evaluation can follow ([`docs/design.md`](../../../../docs/design.md)).
`Valid` asks for the same of a concrete plan. `OrderOk` is the order condition. `ReadsClosed`, `Linkable` and `Hygienic` are the import conditions.
`UniqueDefs`, `Faithful` and `Permutes` say that the plan is the spec applied to the bundle.

The data fits that reading: 14 of the 15 expected rejections fail `OrderOk`. The eight disagreements come from what debundle models better (purity, declarators, exports), not from the order logic.
