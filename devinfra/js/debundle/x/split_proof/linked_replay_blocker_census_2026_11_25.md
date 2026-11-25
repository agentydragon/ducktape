# Linked replay blocker census (2026-11-25)

BuildBuddy invocation [5524ef48-3fc6-4269-85a7-a9af30b1c02f](https://app.buildbuddy.io/invocation/5524ef48-3fc6-4269-85a7-a9af30b1c02f) passed the 18 targeted linked test targets used for this census. It found 47 inputs accepted only by `linked-programs`; all 47 still used generated proofs, and all 47 were assigned syntax categories below.

Categories are non-exclusive. Each row lists syntax found in the decoded schedules rejected by the current `ReplayableSchedule`; the inventory is not a minimal-cause analysis and does not establish that every listed branch or body ran. The earlier dynamic probe census found no `World.step`, `talk`, or unpremised observation in any of the 368 linked corpus inputs.

## Replay blockers on linked-only inputs

- Classified 47 of 47 generated-proof-only inputs.
- property-read: 22 input(s)
- call: 20 input(s)
- assignment: 11 input(s)
- logical-expression: 11 input(s)
- class: 10 input(s)
- derived-class: 9 input(s)
- method-call: 9 input(s)
- binary-operation: 8 input(s)
- construct: 8 input(s)
- direct-call: 8 input(s)
- property-write: 8 input(s)
- return: 8 input(s)
- block: 5 input(s)
- binding-write: 4 input(s)
- branch-statement: 4 input(s)
- object-accessor: 4 input(s)
- unary-operation: 4 input(s)
- class-accessor: 3 input(s)
- class-static-block: 3 input(s)
- conditional-expression: 3 input(s)
- class-field: 2 input(s)
- class-static-field: 2 input(s)
- loop: 2 input(s)
- prototype-property: 2 input(s)
- super-call: 2 input(s)
- super-property-read: 2 input(s)
- compound-assignment: 1 input(s)
- computed-class-key: 1 input(s)
- sequence-expression: 1 input(s)

| Input            | Test(s)                  | Replay blockers                                                                                                                                                                                                                                                     |
| ---------------- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 03cd6e4f57638970 | reordering_test          | call                                                                                                                                                                                                                                                                |
| 06f825f3312ea9c5 | super_members_test       | method-call, object-accessor, prototype-property                                                                                                                                                                                                                    |
| 08bd170f2d6e9da7 | arrays_test              | call                                                                                                                                                                                                                                                                |
| 09d5972d3b0d39bb | static_blocks_test       | assignment, binary-operation, call, class, class-static-block, class-static-field, derived-class, method-call, property-read, property-write, super-call, super-property-read                                                                                       |
| 0e40e7dce3b6db42 | reordering_test          | call                                                                                                                                                                                                                                                                |
| 0f62371ac3aa8bf1 | composition_test         | property-read                                                                                                                                                                                                                                                       |
| 141d09a733085a63 | host_profile_test        | call                                                                                                                                                                                                                                                                |
| 29b09f5c83ebee58 | object_elements_test     | object-accessor, property-read                                                                                                                                                                                                                                      |
| 2b1c89cf4e4926b4 | loops_test               | assignment, binding-write, block, branch-statement, direct-call, loop, return                                                                                                                                                                                       |
| 3326eccae6770f02 | record_fuel_test         | property-read, unary-operation                                                                                                                                                                                                                                      |
| 3465ed4b9202ea21 | loops_test               | assignment, binding-write, block, branch-statement, direct-call, loop, return                                                                                                                                                                                       |
| 3eea154ded990ad8 | logical_test             | logical-expression                                                                                                                                                                                                                                                  |
| 3ff04ab13b0932b6 | logical_test             | logical-expression                                                                                                                                                                                                                                                  |
| 58c85584a72fe913 | static_blocks_test       | assignment, binary-operation, call, class, class-static-block, class-static-field, derived-class, method-call, property-read, property-write, super-call, super-property-read                                                                                       |
| 5b677503cd2bca85 | new_target_test          | binary-operation, class, construct, derived-class, property-read                                                                                                                                                                                                    |
| 5bc92fb86fce68c3 | host_profile_test        | call                                                                                                                                                                                                                                                                |
| 6aa217a3a8d51c8a | logical_test             | direct-call, logical-expression, return                                                                                                                                                                                                                             |
| 6c83478480f883ac | super_members_test       | assignment, call, class, class-accessor, class-field, construct, derived-class, method-call, property-read, property-write                                                                                                                                          |
| 6cde7301f6c45b5b | table_lookups_test       | property-read                                                                                                                                                                                                                                                       |
| 7b7105d1959b90ac | object_elements_test     | object-accessor, property-read                                                                                                                                                                                                                                      |
| 7ceaf952a01c6547 | provider_programs_test   | call                                                                                                                                                                                                                                                                |
| 8057f256918728e7 | computed_class_keys_test | assignment, binary-operation, binding-write, class, class-accessor, class-static-block, compound-assignment, computed-class-key, construct, derived-class, direct-call, logical-expression, method-call, property-read, property-write, return, sequence-expression |
| 92e1a6c025cabe9b | super_members_test       | assignment, call, class, class-accessor, class-field, construct, derived-class, method-call, property-read, property-write                                                                                                                                          |
| 9367c37ea980e09b | provider_programs_test   | call                                                                                                                                                                                                                                                                |
| 96670a892ba09de8 | provider_programs_test   | call                                                                                                                                                                                                                                                                |
| a576d927c18dd225 | member_test              | call                                                                                                                                                                                                                                                                |
| abea01ed080f3a5d | record_prefix_test       | object-accessor, property-read                                                                                                                                                                                                                                      |
| b5638270b8975af9 | composition_test         | property-read                                                                                                                                                                                                                                                       |
| b858d61f56f888bf | record_fuel_test         | property-read, unary-operation                                                                                                                                                                                                                                      |
| bb5a11b4a69dbc79 | logical_test             | direct-call, logical-expression, return                                                                                                                                                                                                                             |
| c1cdaf2a3aa0defb | host_profile_test        | call                                                                                                                                                                                                                                                                |
| cc08caad1a27698d | default_parameters_test  | binary-operation, block, conditional-expression, direct-call, return                                                                                                                                                                                                |
| cc6438d09d22ef47 | provider_programs_test   | call                                                                                                                                                                                                                                                                |
| d6c6f9750876d3bb | wide_table_reversal_test | property-read, unary-operation                                                                                                                                                                                                                                      |
| dbc621497066416e | new_target_test          | assignment, binary-operation, block, branch-statement, class, conditional-expression, construct, derived-class, direct-call, logical-expression, method-call, property-read, property-write, return                                                                 |
| dbe1397ccc40478a | record_prefix_test       | property-read, prototype-property                                                                                                                                                                                                                                   |
| dd706c666f6632b8 | provider_programs_test   | call                                                                                                                                                                                                                                                                |
| e0436b718eb111ec | provider_programs_test   | call                                                                                                                                                                                                                                                                |
| e104daed84223acd | table_lookups_test       | property-read                                                                                                                                                                                                                                                       |
| e860c232ca8af149 | super_members_test       | assignment, class, construct, derived-class, logical-expression, method-call, property-read, property-write                                                                                                                                                         |
| ee6f23b5f422a230 | provider_programs_test   | call                                                                                                                                                                                                                                                                |
| ef1e5ad5a2fb2b67 | new_target_test          | binary-operation, call, class, construct, logical-expression, property-read                                                                                                                                                                                         |
| f71369fb109cd491 | new_target_test          | assignment, binary-operation, block, branch-statement, class, conditional-expression, construct, derived-class, direct-call, logical-expression, method-call, property-read, property-write, return                                                                 |
| f85e1741e9944183 | logical_test             | logical-expression                                                                                                                                                                                                                                                  |
| fa8aeaf15dce023b | wide_table_reversal_test | property-read, unary-operation                                                                                                                                                                                                                                      |
| fc19a9c30124d7ff | logical_test             | logical-expression                                                                                                                                                                                                                                                  |
| fce319da6fd835cf | host_profile_test        | assignment, binding-write, call                                                                                                                                                                                                                                     |
