# Pooled effect overlap and callback graph witnesses

The program-coordinate consumers mixing source census and runtime cells are
`program_statement_effects`, `emit_program_heap_content_order`, and
`premise_effects_for_statement`/`conflicting_cell` in `graph/build.rs`.
`ProgramAllocationId` keeps source census (`event: None`) distinct from runtime
events and keeps separate activations distinct. Whole-owner operation coverage
can replace the syntax census; other paths retain the syntax/effect frontier.
Array writes retain opacity. Legacy local-coordinate effect checks pool both
sides conservatively, and foreign projection adds an unavailable/opaque marker.
No activation identity change was justified by this audit.

`source_array_census_keeps_runtime_mutation_order` exercises six real CLI
variants: local/imported array factories and direct, binary, and array-wrapped
reads. Each source prints `0 1`; the proposed reordered output prints `1 1` and
is rejected with a dependency cycle. These probes establish the tested
boundaries, rather than a general proof for every effect producer.

The callback graph audit did find an incomplete cache witness: namespace walks
read inactive module cells/default exports and active initialization state,
which the previous export-surface witness omitted. `CallbackGraphReads` now
records those dependencies when namespace carriers occur and deduplicates live
closure cells. The old implementation fails the focused
`export_memo_rejects_changed_foreign_namespace_cells` invariant (RBE
`f28b505b-9271-478d-a637-cc0388d84632`); no unsafe CLI acceptance was demonstrated
for that omission. Prefix-value pointer witnesses also compare their binding
catalogue identity, so equal empty ordinal maps in different source banks cannot
validate each other's witness.

RBE `b9801064-eb43-4da3-b106-c229033107e6` passes analysis, namespace live carrier,
no-reentry callback, and ordered own-data-field suites, including these controls.
Canonical acceptance remains a separate parent-lane check.
