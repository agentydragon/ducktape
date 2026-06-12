# Code Scan Prompts

Prompt definitions for targeted code-quality scans: each file defines one scan —
what to look for, what counts as a finding, and what to leave alone. Run one by
pointing an agent at the scan file and a target tree. House style for findings
lives in <../../STYLE.md>; these scans operationalize specific rules from it.

| Scan                                | Looks for                                          |
| ----------------------------------- | -------------------------------------------------- |
| <api_model_design.md>               | API model design antipatterns                      |
| <asyncio_antipatterns.md>           | Asyncio antipatterns                               |
| <code_duplication.md>               | Duplicated code patterns                           |
| <denormalized_computed_fields.md>   | Denormalized and computed fields                   |
| <error_swallowing.md>               | Error swallowing (let it crash)                    |
| <fastmcp_documentation_patterns.md> | FastMCP documentation patterns                     |
| <functional_over_imperative.md>     | Imperative loops that should be functional         |
| <identifier_naming.md>              | Identifier naming                                  |
| <legacy_aliases.md>                 | Legacy backward-compatibility aliases              |
| <library_type_misuse.md>            | Library type misuse                                |
| <manual_serde_needs_pydantic.md>    | Manual serialization that should use Pydantic      |
| <methods_vs_freestanding.md>        | Methods vs freestanding functions                  |
| <missing_dataclass_pydantic.md>     | Classes that should be dataclasses/Pydantic models |
| <mypy_appeasing_code.md>            | Mypy-appeasing code antipatterns                   |
| <overly_loose_typing.md>            | Overly loose input/output typing                   |
| <pydantic_antipatterns.md>          | Pydantic antipatterns                              |
| <pygit2_patterns.md>                | Non-idiomatic pygit2 usage                         |
| <pytest_fixtures_antipatterns.md>   | Pytest antipatterns                                |
| <stringly_typed.md>                 | Stringly-typed code                                |
| <suspicious_defaults.md>            | Suspicious default values                          |
| <suspicious_nullability.md>         | Suspicious nullability                             |
| <test_assertions.md>                | Test assertion antipatterns                        |
| <timestamp_naming.md>               | Timestamp field naming                             |
| <trivial_forwarder_methods.md>      | Trivial forwarder methods                          |
| <trivial_forwarders.md>             | Functions that should be inlined                   |
| <type_ignore_suppressions.md>       | Type-checker suppression comments                  |
| <unnecessary_verbosity.md>          | Unnecessary verbosity                              |
| <useless_comments_and_docs.md>      | Useless comments and documentation                 |
| <useless_documentation.md>          | Useless documentation                              |
| <useless_test_classes.md>           | Useless test classes                               |
| <walrus_get_pattern.md>             | Walrus operator (`:=`) opportunities               |
