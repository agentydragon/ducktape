# Debundle deferred architecture backlog

Unrelated follow-ups retained for a future decision. This is not the dispatch
queue; order active work in `TODO.md`. Remove an item when it is resolved or
rejected.

## Serde and Strum on protobuf-generated spec types

- Audit the Serde derives configured by `spec_wire_serde_enums` in `BUILD.bazel`.
  Generated types are re-exported by `spec/spec.rs` for authored YAML and used
  in JSON reports. Determine whether protobuf-aware serialization can replace
  that glue while preserving snake_case enum names, field presence and unknown
  field rejection. Remove redundant derives and configuration with their callers.
- Audit Strum's `Display` derives on `ReentryFreeOperation` and
  `InitializerExternalEffects`. Their diagnostic consumers include
  `cli/spec_lint.rs`, `frontend_elaboration/elaboration.rs` and
  `facts/statement_facts.rs`. Investigate deriving their snake_case spellings from
  the protobuf schema before removing the Strum annotations.

## A11 intrinsic integrity assumption

For purity rules still relying on A11 alone, migrate builtin rows to the existing
source-write census as described in [the abstract-interpreter plan](plans/abstract_interpreter.md).
The census already guards exact slots for migrated consumers. Hidden writes
and host or external mutations remain the author’s
[A11 obligation](docs/design.md#conditions-on-the-input-chunk-assumptions-a1a11).

## Defensive comment in linker ordering

`graph/linker_order.rs::chunk_source_import_order_from_adjacency` has a
`None`-after-`Some` clause described as protection against future filter changes
that might admit non-constraining members. If the filter shape changes, test the
resulting invariant or remove the defensive branch.
