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

`docs/design.md` records A11 (that analyzed chunks run with unmodified built-in
prototypes) as an assumption. Prototype pollution can invalidate purity-whitelist
admission and is not detected. A top-level scan for assignments such as
`<Builtin>.prototype.<x> = ...` could check the in-corpus portion; pollution from
host code or other bundles remains an external assumption. Decide whether that
check is worth its cost before changing the runtime contract.
