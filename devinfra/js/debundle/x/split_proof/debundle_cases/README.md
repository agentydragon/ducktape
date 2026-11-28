# debundle's e2e cases against the Lean checker

debundle moves the top-level statements of a bundle into ES modules, as this project does, but without Lean.
Its data-driven corpus is [`e2e/cases`](../../../e2e/cases): 184 case directories, 207 entries.
An entry is a bundle (`main.js` or `static/app.js`), a spec, and the verdict debundle must give: `accept` with the expected stdout, or `reject` as `atomic_conflict`, `cycle` or a stderr text.
`run_cases.mjs` turns each entry into a `lowersplit` run and compares the verdicts. A run takes about 10 s.

Results: [`results/summary.md`](results/summary.md) (generated tables) and [`results/cases.jsonl`](results/cases.jsonl) (one line per entry).
These reports record the current run and the per-case Lean coverage baseline. The historical analysis below describes the initial corpus run.

## Run it

From the repository root, set `out` to an absolute output directory:

```sh
p=//devinfra/js/debundle/x/split_proof
bb run "$p/debundle_cases:debundle_cases" -- --out "$out/cases.jsonl"
bb run "$p/debundle_cases:summarize_debundle_cases" -- "$out/cases.jsonl" > "$out/summary.md"
```

Add `--regression` to fail after writing the report on an empty selection, a runner error, an invalid baseline, or an accepted split with a different observation. A case that previously received a Lean verdict must still get a verdict, and a case that reached the Lean syntax checker must still reach Lean. A full run also detects missing cases. Filtered runs compare the cases selected. Use a separate output file; review coverage changes before replacing the checked-in JSONL and regenerating the summary. For a different corpus, pass its report with `--baseline FILE`. Subprocess failures report the command, elapsed time, and a bounded output tail in the report and in regression failures.

Use `--filter <substring>` for a smaller run.

`--keep <dir>` copies each translated entry (bundle, translated spec, emitted modules, checker output) to `<dir>/<id>`.

For direct Node runs, first [install the translation dependencies](../translation/README.md#run-it). Then run from this directory:

```sh
cp package.json.in package.json && npm ci --no-audit --no-fund
DEBUNDLE_RESOLVER=/absolute/path/to/debundle_resolve_placement node run_cases.mjs --out "$out/cases.jsonl"
```

## Which accepted cases have artifact certificates?

The ordinary splitter runs once for every saved `agree-accept` diagnostic case, including nested entries with fixed providers. A refusal records the CLI’s reason in `emission.blocker` and its reason code when available; the output directory must remain absent. After an acceptance the runner checks the published files from disk and runs the original and emitted entries in Node. Both must exit successfully and match the fixture’s expected stdout. Missing cases, adapter errors, and failed disk or runtime checks fail the test.

The certificate result and corpus observations are separate. Each row records `expectations` as `pass`, `fail`, `unchecked`, `inapplicable`, or `refused`, and `behavior` is `pass` only if every applicable authored observation passed. Export expectations use the authored entry and named owner routes to import the actual emitted modules. The probe also records the original unsplit namespace for comparison. Saved probes run on both the original and the published entry. When requested, `check_all_js` runs `node --check` on every emitted JavaScript file, including files the entry does not load. Unmapped module paths, source assertions, emitted-only probes, runtime file setup and unknown expectation fields remain unchecked until the adapter can represent and verify them. A refused request marks applicable observations refused; an absent assertion is inapplicable. The unchanged cohort remains the saved 122 diagnostic placements, including renames that the adapter must refuse. `--print-ledger` prints the rows as well as status totals. Disk readback separately verifies the certified export cells; runtime equality of primitive values alone does not. In `split_declarator_fragments`, the original namespace is `{c, z}`. The emitted `x.mjs` provides `c`, the residual namespace excludes it, and all three emitted files pass syntax checks. These are observations of the saved artifacts. The emission and readback verdicts record the proof guarantee separately.

```sh
p=//devinfra/js/debundle/x/split_proof/debundle_cases
bbr test "$p:ordinary_certification_test" "$p:corpus_certificate_test"
bb run "$p:debundle_cases" -- --certify --out "$out/certificates.jsonl"
```

The retained-tail constructor proves all modeled outcomes for the unchanged
comma-list and declaration-fragments placements. Their respective executable
orders change from `0,1,2,3,4,5` to `0,2,1,3,4,5` and from `0,1,2,3,4`
to `0,1,3,2,4`. Each is one adjacent swap of primitive declarations. For
declaration fragments, the checker proves the numeric operands read their
already initialized cells; the proof expression retains the original
arithmetic and its fuel cost.

The regular move checker relates successful final states only. For instance,
two reversed throws can have no successful states and still throw different
values ([control](../EsmSplit/CoreJS/LiteralSwapCheck.lean)). The exact route
checks a narrower move: if either initializer exhausts its bound, both
schedules return the same state-free CoreJS fuel result. If both finish, their
writes to distinct cells commute. The unchanged suffix then reaches the same
world conversation with equal state, receiver, ordinal and capture cells.
The proof holds for every world in the existing model and requires no console
profile or assumption that the call returns normally. It does not promise
the state lost at fuel exhaustion, or assert the host implementation of
JavaScript console. An unsupported move may still qualify for the existing
normal-completion rule. An explicit all-outcomes request requires the stronger
proof before publication.

Both saved placements are emitted and reread under an explicit all-outcomes
request. The model regression runs each parsed original and reread output
against worlds that read an actual capture, allocate and mutate an object,
and then return, throw, or report an unknown operation. It also checks low
fuel and rejects a changed capture or dependency-breaking order. Both placements pass all their authored observations, including declaration
fragments' two non-entry namespace checks and full-tree syntax check.

The separate `corpus_certificate_test` requires a compiled constructor to emit a supported placement. It checks its live export in Node, rechecks the files, and rejects a tampered provider and a wrong owner. The [historical structural report](https://github.com/agentydragon/ducktape/blob/c42cc83cf15aa5bf544ca998624af8932b2370ef/devinfra/js/debundle/x/split_proof/debundle_cases/results/certificates.jsonl) records earlier results under different proof rules.

## What the adapter does

1. `acorn` parses the case's one chunk and `eslint-scope` supplies scope annotations. For placement proposals, the adapter numbers the top-level source units, counting each declarator in a top-level list separately. A block's unit includes the names its nested `var` declarations bind.
2. The debundle spec becomes a `lowersplit` spec. The standalone Rust resolver solves the authored selectors. The adapter checks its source digest, chunk ID and unit census before using the returned owners. `import_order` becomes the import priority (`Spec.order`).
3. Unclaimed statements follow `unassigned_mode`. With `inline_in_entry` they stay in the entry. With `catchall_file` the catchall module takes the unclaimed declarations, the unclaimed anonymous statements before its last statement, and everything when it is the only module.
   Later anonymous statements stay in the entry (debundle's rule: [`docs/design.md`](../../../docs/design.md)). An `export { … }` without a declaration always stays in the entry.
4. `lowersplit --diagnostic … --force` derives its own source units from the annotated Program, checks the split, and writes an uncertified preview even when a check rejects it. This corpus compares diagnostic checker results with debundle's expected results; it does not certify the emitted files.
5. The adapter copies declared runtime files for the current chunk beside the emitted entry, without replacing emitted files. Node runs the original chunk and the emitted entry. The adapter compares verdict and stdout with the entry's expectation.
6. What the adapter cannot translate is `unsupported:<cause>` and is not forced.

## Initial outcomes

| Outcome             | Entries | Meaning                                                                                                  |
| ------------------- | ------- | -------------------------------------------------------------------------------------------------------- |
| `agree-accept`      | 69      | I accept. The emitted modules print the expected stdout.                                                 |
| `agree-reject`      | 15      | debundle expects `atomic_conflict` (8) or `cycle` (7). I reject too.                                     |
| `strict-but-runs`   | 6       | I reject. debundle accepts. My plan, written anyway, prints the expected stdout.                         |
| `strict-and-breaks` | 1       | I reject. debundle accepts. My plan, written anyway, does not load.                                      |
| `unsound-accept`    | 0       | I accept. The emitted modules print something else.                                                      |
| `lenient`           | 0       | I accept what debundle rejects, and the output is the same.                                              |
| `refusal-test`      | 7       | debundle must fail with a given stderr text. There is nothing to compare.                                |
| not run             | 109     | The adapter cannot translate the spec yet (below). 36 `reject: stderr` entries in all are refusal tests. |

- **No unsound accept in the final run.** The corpus found two before I fixed them (§ Soundness findings).
- **The agreeing accepts are real splits.** 61 of the 69 move statements out of the entry: 159 of 323 statements. The other 8 keep everything in the entry.
- **The rejections are not over-strict.** All 15 expected rejections fail my checks. 14 of them fail `OrderOk`. Written anyway, 14 of the 15 print something else or crash. The 15th, `initialization/nested_closure_rebind/separated`, fails `Reassign` and prints the same output on this run.
- **The premise holds.** For all 76 accept entries that ran, the original chunk prints the expected stdout, so "same as the original" and "as expected" mean the same.
- **I compare only stdout.** 36 of the 69 agreeing accepts also assert `exports`, `source`, `probes` or `check_all_js`. The adapter does not check those.

## Where my verdict differs from debundle's

| Entries                                                                                                                                                | I fail       | Cause                                                                                                                                                                                                                                                                                              | Owner                 |
| ------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| `initialization/frozen_record`, `emission/proxy_constructor/fresh_args`, `callbacks/from_entries_map_does_not_run_stored_callback_during_construction` | `OrderOk`    | `new Proxy` is a call into the world; what is read from an exported object is the world's; a declaration moves before the unit that gives a callback to the world, and the model orders every reader and binder pair ([`PLAN.md`](../PLAN.md) step 3). debundle's purity analysis lets these move. | footprint analysis    |
| `emission/duplicate_declaration/repeated_var`, `emission/hoisted_var/repeated_statements`, `initialization/repeated_var_accessor/colocated`            | `UniqueDefs` | Two `var` declarations of one name. That is legal inside one module, and debundle keeps such sites together. `UniqueDefs` wants one declaration per name.                                                                                                                                          | checker (`Plan.lean`) |
| `realizability/initialization_order/early_entry_importer_does_not_pull_scc_in_wrong_order`                                                             | `OrderOk`    | `mod_init` calls `readT`, which reads `T` from `mod_logger`. My plan has no import that makes `mod_logger` run first.                                                                                                                                                                              | splitter (`mkPlan`)   |

## Why 109 entries did not run in the initial baseline

| Family                          | Entries | What is missing                                                                                                                                                                                                                                                                                         |
| ------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Template language               | 42      | `source_matches` with holes (`ANYTHING`, `EXPR`, `STMT`, `ARGS`, …) 26, anonymous templates with holes 3, templates that match no whole statement 8 (alpha-renamed locals, one declarator of a comma list), multi-statement ranges 5. The adapter matches only hole-free templates, by exact AST shape. |
| Dataflow selectors              | 24      | `passed_to_call` 7, `member_of_module` 6, `cross_ref` 4, `reads_member` 4, `makes_decorate_call` 3. debundle resolves them from value flow between modules.                                                                                                                                             |
| Several input chunks            | 12      | One bundle per case is all the model knows. These cases have two or three chunks that import each other.                                                                                                                                                                                                |
| Imports                         | 6       | The adapter's dump has no literal values, which `lowersplit` needs for the source of an `import`, and it writes the modules away from the chunk's outside modules (4: [`PLAN.md`](../PLAN.md) step 6). Selectors on import specifiers (2).                                                              |
| Statement granularity           | 4       | Two modules claim parts of one statement: the declarators of a comma list, or a binding and an anonymous template. debundle's units are declarators. Mine are statements.                                                                                                                               |
| Audited premises                | 5       | `annotations` 3 and `chunk_analysis_options` 2: facts the spec author vouches for (purity, commuting statements, dataflow summaries).                                                                                                                                                                   |
| Spec-contract violations, parse | 10      | `import_order` omits a module 5, input does not parse 3, `import_order` names a generated residual that stays empty 2. Nine of the ten expect `reject: stderr`: they test debundle's diagnostics. The tenth, `emission/sibling_declarator_ownership`, is the granularity case again.                    |
| Outside the model               | 6       | `lowersplit` refuses the bundle: a direct `eval` 3, a top-level `await` 3 ([`SUPPORT.md`](../SUPPORT.md)). All six expect `reject: stderr`: debundle refuses them too. Before the refusal these ran, and `lowersplit` accepted them.                                                                    |

41 of the template entries and 13 of the dataflow entries expect `accept`. The other 12 expect `reject: stderr`.
In all, 36 entries (7 run, 29 not run) test debundle's error text and have no counterpart here.

## Soundness findings

The corpus found two accepted splits that do not run. Both were front-end gaps. The Lean proofs hold; their premises (footprints, front-end rules) were wrong.

1. **`var` inside a top-level block.** `if (c) { var x = 1; }` declares `x` in the module scope. `Lower.topDefs` ignored it, so the statement declared nothing and a reader could move away from it.
   Four entries were accepted and then failed with `ReferenceError` or `SyntaxError: Export 'shared' is not defined`: `emission/hoisted_var/{complete_statement/claimed,lazy_reader/spec,reader_ownership/claimed,repeated_statements/spec}`.
   I confirmed this by undoing the fix and re-running. Fixed: `topDefs` now collects nested `var` names. `repeated_statements` and `repeated_var_accessor/colocated` are now rejected by `UniqueDefs`.
2. **Export names.** `export { X as av }` publishes `X` as `av`. A module that needs the local `av` imports it from the entry, and the generated glue exported `av` again: `SyntaxError: Duplicate export of 'av'` (`emission/residual_export_alias_collision`).
   The model had no export names. It has them now (`Stmt.exports`): `mkPlan` exports the local as `av$1`, the importer renames it back, and `ExportsUnique` in `Valid` checks that a module's export names differ. The entry is accepted and prints the expected output.

## Not compared by the diagnostic run

- **Stderr text** of the 36 refusal entries.
- **`exports`, `source`, `probes`, `check_all_js`, `runtime_files`** assertions of accepted entries.
- **Names.** `chunk_renames` and `bindings[].name` do not change validity, so the adapter ignores them. Its entry module is always `entry.mjs`, whatever `entry:` says.
- **Order of debundle's own emission.** The adapter places statements by the spec and lets `mkPlan` derive imports. It does not read debundle's output.

## How debundle's gate and `Valid` relate

debundle decides realizability on a dependency graph of source units (statements, or the declarators of a comma list), quotiented by the requested modules.
Import edges, at-init read edges and side-effect order edges must leave an order that ES evaluation can follow ([`docs/design.md`](../../../docs/design.md)).
`Valid` asks for the same of a concrete plan. `OrderOk` is the order condition. `ReadsClosed`, `Linkable` and `Hygienic` are the import conditions.
`UniqueDefs`, `Faithful` and `Permutes` say that the plan is the spec applied to the bundle.

The data fits that reading: 14 of the 15 expected rejections fail `OrderOk`. The eight disagreements come from what debundle models better (purity, declarators, exports), not from the order logic.
