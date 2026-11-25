# debundle e2e cases against the Lean checker

207 spec entries from `devinfra/js/debundle/e2e/cases`. Definitions: [README](../README.md).

## Outcomes

| Outcome           | debundle: accept | debundle: reject:atomic_conflict | debundle: reject:cycle | debundle: reject:stderr | Total |
| ----------------- | ---------------- | -------------------------------- | ---------------------- | ----------------------- | ----- |
| agree-accept      | 122              | 0                                | 0                      | 0                       | 122   |
| agree-reject      | 0                | 7                                | 8                      | 0                       | 15    |
| strict-but-runs   | 12               | 0                                | 0                      | 0                       | 12    |
| strict-and-breaks | 1                | 0                                | 0                      | 0                       | 1     |
| refusal-test      | 0                | 0                                | 0                      | 21                      | 21    |
| unsupported       | 18               | 2                                | 1                      | 15                      | 36    |

## What the runs check

| Measure                                                                                   | Value      |
| ----------------------------------------------------------------------------------------- | ---------- |
| Accepted splits that agree with debundle                                                  | 122        |
| Of them, with at least one module besides the entry                                       | 113        |
| Statements moved out of the entry, of all statements in those cases                       | 332 of 618 |
| Of them, with assertions I do not compare (`exports`, `source`, `probes`, `check_all_js`) | 88         |
| Accept entries run whose original chunk prints the expected stdout                        | 135 of 135 |
| Expected rejections whose modules, written anyway, print other output or fail             | 14 of 15   |

## Cases the adapter does not run, by cause

| Cause                                | Cases | debundle accepts | debundle rejects | Examples                                                                                                                                   |
| ------------------------------------ | ----- | ---------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| several input chunks                 | 12    | 11               | 1                | `emission/catchall_export_consumer/spec`, `emission/cross_chunk_readable_imports/competing_targets/spec`                                   |
| statement kind the model rejects     | 6     | 0                | 6                | `admission/direct_eval_in_a_renamed_chunk_is_rejected/spec`, `admission/direct_eval_inside_function_body_is_rejected/spec`                 |
| import_order omits a module          | 5     | 0                | 5                | `admission/dynamic_import_of_own_chunk_is_rejected/spec`, `admission/top_level_import_meta_reflection_is_rejected/spec`                    |
| resolver refusal                     | 4     | 3                | 1                | `emission/duplicate_declaration/repeated_var/spec`, `emission/hoisted_var/complete_statement/claimed`                                      |
| annotations                          | 3     | 3                | 0                | `emission/lowering/source_match_annotations/spec`, `emission/makes_decorate/shared_helper/annotation`                                      |
| input does not parse as a module     | 3     | 0                | 3                | `admission/with_block_is_rejected_at_parse/spec`, `emission/duplicate_declaration/function_rejected/spec`                                  |
| spec option chunk_analysis_options   | 2     | 1                | 1                | `initialization/imported_class_tdz/spec`, `realizability/commuting/commuting_a_writer_past_a_frontier_preserves_the_earlier_observer/spec` |
| import_order names an unknown module | 1     | 0                | 1                | `emission/hoisted_var/left_right_ownership/residual_conflict`                                                                              |

## Cases where my verdict differs from debundle's

| Outcome           | Case                                                                                            | Checks that failed          | Emitted modules in Node                                 |
| ----------------- | ----------------------------------------------------------------------------------------------- | --------------------------- | ------------------------------------------------------- |
| strict-but-runs   | `callbacks/from_entries_map_does_not_run_stored_callback_during_construction/spec`              | OrderOk                     | expected output                                         |
| strict-but-runs   | `emission/catchall_exports/claimed`                                                             | ExportOutsideEntry          | expected output                                         |
| strict-but-runs   | `emission/chunk_rename_peeled_import/spec`                                                      | ImportOutsideEntry, OrderOk | expected output                                         |
| strict-but-runs   | `emission/cross_module/import_specifier_reimport/spec`                                          | ImportOutsideEntry          | expected output                                         |
| strict-but-runs   | `emission/global_selector_assignment/individual/spec`                                           | ExportOutsideEntry          | expected output                                         |
| strict-but-runs   | `emission/member_of_module/empty_subclasses/spec`                                               | ImportOutsideEntry          | expected output                                         |
| strict-but-runs   | `emission/node_builtin_import/spec`                                                             | ImportOutsideEntry          | expected output                                         |
| strict-but-runs   | `emission/proxy_constructor/fresh_args/spec`                                                    | OrderOk                     | expected output                                         |
| strict-but-runs   | `emission/syntactic_holes/exported_declaration_range/spec`                                      | ExportOutsideEntry          | expected output                                         |
| strict-but-runs   | `initialization/frozen_record/spec`                                                             | OrderOk                     | expected output                                         |
| strict-but-runs   | `initialization/repeated_var_accessor/colocated`                                                | UniqueDefs                  | expected output                                         |
| strict-and-breaks | `realizability/initialization_order/early_entry_importer_does_not_pull_scc_in_wrong_order/spec` | OrderOk                     | ReferenceError: Cannot access 'T' before initialization |
| strict-but-runs   | `realizability/translation_validation/pruned_import_keeps_its_load_order/spec`                  | OrderOk                     | expected output                                         |

## By area

| Area               | Entries | Agree | Strict | Unsound or lenient | Refusal tests | Not run |
| ------------------ | ------- | ----- | ------ | ------------------ | ------------- | ------- |
| emission           | 140     | 95    | 8      | 0                  | 19            | 18      |
| realizability      | 26      | 16    | 2      | 0                  | 1             | 7       |
| initialization     | 24      | 20    | 2      | 0                  | 0             | 2       |
| admission          | 11      | 3     | 0      | 0                  | 1             | 7       |
| native_collections | 3       | 3     | 0      | 0                  | 0             | 0       |
| purity             | 2       | 0     | 0      | 0                  | 0             | 2       |
| callbacks          | 1       | 0     | 1      | 0                  | 0             | 0       |
