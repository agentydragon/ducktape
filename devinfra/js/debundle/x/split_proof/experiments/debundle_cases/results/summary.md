# debundle e2e cases against the Lean checker

207 spec entries from `devinfra/js/debundle/e2e/cases`. Definitions: [README](../README.md).

## Outcomes

| Outcome           | debundle: accept | debundle: reject:atomic_conflict | debundle: reject:cycle | debundle: reject:stderr | Total |
| ----------------- | ---------------- | -------------------------------- | ---------------------- | ----------------------- | ----- |
| agree-accept      | 69               | 0                                | 0                      | 0                       | 69    |
| agree-reject      | 0                | 8                                | 7                      | 0                       | 15    |
| strict-but-runs   | 6                | 0                                | 0                      | 0                       | 6     |
| strict-and-breaks | 1                | 0                                | 0                      | 0                       | 1     |
| refusal-test      | 0                | 0                                | 0                      | 13                      | 13    |
| unsupported       | 77               | 1                                | 2                      | 23                      | 103   |

## What the runs check

| Measure                                                                                   | Value      |
| ----------------------------------------------------------------------------------------- | ---------- |
| Accepted splits that agree with debundle                                                  | 69         |
| Of them, with at least one module besides the entry                                       | 61         |
| Statements moved out of the entry, of all statements in those cases                       | 159 of 323 |
| Of them, with assertions I do not compare (`exports`, `source`, `probes`, `check_all_js`) | 36         |
| Accept entries run whose original chunk prints the expected stdout                        | 76 of 76   |
| Expected rejections whose modules, written anyway, print other output or fail             | 14 of 15   |

## Cases the adapter does not run, by cause

| Cause                                              | Cases | debundle accepts | debundle rejects | Examples                                                                                                                                      |
| -------------------------------------------------- | ----- | ---------------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| source_matches with syntactic holes                | 26    | 25               | 1                | `emission/decorator_helpers/use_site`, `emission/makes_decorate/kind_constraint/spec`                                                         |
| several input chunks                               | 12    | 11               | 1                | `emission/catchall_export_consumer/spec`, `emission/cross_chunk_readable_imports/competing_targets/spec`                                      |
| source_matches matches no statement                | 8     | 8                | 0                | `emission/cross_ref/delegator/resolved_anchor_order`, `emission/global_selector_assignment/individual/spec`                                   |
| selector passed_to_call                            | 7     | 3                | 4                | `emission/passed_to_call/ambiguous/spec`, `emission/passed_to_call/argument_index/spec`                                                       |
| selector member_of_module                          | 6     | 3                | 3                | `emission/member_of_module/ambiguous/spec`, `emission/member_of_module/delegator/spec`                                                        |
| import_order omits a module                        | 5     | 0                | 5                | `admission/dynamic_import_of_own_chunk_is_rejected/spec`, `admission/top_level_import_meta_reflection_is_rejected/spec`                       |
| selector cross_ref                                 | 4     | 3                | 1                | `emission/cross_ref/delegator/spec`, `emission/cross_ref/delegator/unknown_anchor`                                                            |
| selector reads_member                              | 4     | 1                | 3                | `emission/reads_member/ambiguous/spec`, `emission/reads_member/helper/spec`                                                                   |
| source_matches template is not one statement       | 4     | 4                | 0                | `emission/syntactic_holes/commented_source_range/spec`, `emission/syntactic_holes/exported_declaration_range/spec`                            |
| statement claimed by two modules                   | 4     | 1                | 3                | `emission/comma_list/destructure_split_across_modules_is_rejected/spec`, `emission/hoisted_var/left_right_ownership/statement_binding_owners` |
| statement kind the model rejects                   | 4     | 4                | 0                | `emission/chunk_rename_peeled_import/spec`, `emission/chunk_renames/named_imports/spec`                                                       |
| annotations                                        | 3     | 3                | 0                | `emission/lowering/source_match_annotations/spec`, `emission/makes_decorate/shared_helper/annotation`                                         |
| anonymous_statements with syntactic holes          | 3     | 3                | 0                | `emission/syntactic_holes/anonymous_statement_hole/spec`, `emission/syntactic_holes/empty_statement_run/spec`                                 |
| input does not parse as a module                   | 3     | 0                | 3                | `admission/with_block_is_rejected_at_parse/spec`, `emission/duplicate_declaration/function_rejected/spec`                                     |
| selector makes_decorate_call                       | 3     | 3                | 0                | `emission/decorator_helpers/makes_decorate`, `emission/intrinsic_alias/source_match_split/spec`                                               |
| import specifier binding                           | 2     | 2                | 0                | `emission/cross_module/import_specifier_reimport/spec`, `emission/node_builtin_import/spec`                                                   |
| import_order names an unknown module               | 2     | 1                | 1                | `emission/hoisted_var/left_right_ownership/residual_conflict`, `emission/sibling_declarator_ownership/spec`                                   |
| spec option chunk_analysis_options                 | 2     | 1                | 1                | `initialization/imported_class_tdz/spec`, `realizability/commuting/commuting_a_writer_past_a_frontier_preserves_the_earlier_observer/spec`    |
| anonymous_statements template is not one statement | 1     | 1                | 0                | `emission/catchall_exports/claimed`                                                                                                           |

## Cases where my verdict differs from debundle's

| Outcome           | Case                                                                                            | Checks that failed | Emitted modules in Node                                 |
| ----------------- | ----------------------------------------------------------------------------------------------- | ------------------ | ------------------------------------------------------- |
| strict-but-runs   | `callbacks/from_entries_map_does_not_run_stored_callback_during_construction/spec`              | OrderOk            | expected output                                         |
| strict-but-runs   | `emission/duplicate_declaration/repeated_var/spec`                                              | UniqueDefs         | expected output                                         |
| strict-but-runs   | `emission/hoisted_var/repeated_statements/spec`                                                 | UniqueDefs         | expected output                                         |
| strict-but-runs   | `emission/proxy_constructor/fresh_args/spec`                                                    | OrderOk            | expected output                                         |
| strict-but-runs   | `initialization/frozen_record/spec`                                                             | OrderOk            | expected output                                         |
| strict-but-runs   | `initialization/repeated_var_accessor/colocated`                                                | UniqueDefs         | expected output                                         |
| strict-and-breaks | `realizability/initialization_order/early_entry_importer_does_not_pull_scc_in_wrong_order/spec` | OrderOk            | ReferenceError: Cannot access 'T' before initialization |

## By area

| Area               | Entries | Agree | Strict | Unsound or lenient | Refusal tests | Not run |
| ------------------ | ------- | ----- | ------ | ------------------ | ------------- | ------- |
| emission           | 140     | 43    | 3      | 0                  | 5             | 89      |
| realizability      | 26      | 14    | 1      | 0                  | 4             | 7       |
| initialization     | 24      | 21    | 2      | 0                  | 0             | 1       |
| admission          | 11      | 3     | 0      | 0                  | 4             | 4       |
| native_collections | 3       | 3     | 0      | 0                  | 0             | 0       |
| purity             | 2       | 0     | 0      | 0                  | 0             | 2       |
| callbacks          | 1       | 0     | 1      | 0                  | 0             | 0       |
