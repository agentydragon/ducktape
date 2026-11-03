# Resolved-member premise projection preflight

`AnalysisProjection::new` previously used `filter_map` over
`native_operation_inputs`; a `resolved_member` absent from the collected
logical-module sites was silently omitted. `debundle spec project-analysis`
therefore succeeded for a missing module/member, and `debundle analyze` later
failed in cross-module lowering. A member present only in another provider
chunk was also rejected only during analysis.

Projection construction now rejects missing and foreign-provider references
using the same lookup as cross-module analysis. The error identifies the
requesting chunk, module, member and provider, and says that module names are
chunk-relative. Valid references retain the same `premise_members` map shape;
the serialized schema is unchanged.

The regression uses neutral chunks and names through the actual
`debundle spec project-analysis` command. On the base revision,
`nix develop --command bbr test //devinfra/js/debundle/e2e:native_own_data_read_test --test_filter=project_analysis_rejects_missing_and_foreign_resolved_members --test_output=errors`
failed because the missing-module case exited successfully. The same target
passes with the preflight, including missing module, missing member, foreign
provider and valid-reference controls.
