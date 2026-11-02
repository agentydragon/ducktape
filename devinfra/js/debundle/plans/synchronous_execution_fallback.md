# Synchronous-execution fallback

The synchronous interpreter reports `HavocReach`, but the report is not yet an
authority for graph construction. `HavocReach` is documented as diagnostic-only
in `reports/schema.rs`; the conservative graph fallback still promotes the
read closure for incomplete at-init calls.

## Remaining effect-authority boundary

`graph/build.rs` emits known synchronous reads and rebinds, then lets incomplete
statements fall through to `UnresolvedCallFallback`. That fallback expands
unresolved roots through `SourceUnitReadClosure` and adds closure reads as eager
uses. The code does not condition this promotion on `HavocReach`.

The smallest safe next slice is to let a bounded, source-known incomplete
statement use its represented effects only after the effect closure covers all
behavior whose omission could make a dropped constraint unsound: source reads
and rebinds, initialization order, property lookups and accessors/getters, heap
mutation, global reads and writes, and host-triggered callbacks or reentry. The
closure must include effects of every callback and accessor the statement can
run. Preserve the separate ordering constraints for uninitialized
observations. Keep the fallback for `Global` reach and whenever any effect
domain remains unrepresented. `HavocReach` and a list of precision-loss sites
classify possible code; neither proves this effect closure complete.

A legacy acceptance rejected by an effect-based validator is differential
evidence, not by itself a defect in the validator: it can expose an unsound
legacy acceptance or conservative/incorrect new effects. Classify the original
program behavior and the new effect evidence before choosing which side needs a
change.

## Required invariant

The split is safe only when every dropped fallback edge is redundant with the
complete modeled execution effects, with emitted initialization behavior
preserved. Neutral cases should cover both a bounded local call whose represented
read remains ordered correctly and a global/opaque call that keeps conservative
promotion. Existing negative controls for borrowed native receivers, escaped
callbacks, and dynamic scope must keep conservative promotion where their
effects are not proved; uninitialized observations must retain their separate
ordering constraint.

The current reach classifier and precision-loss report are in
`facts/sync_effects/interpreter.rs` and `e2e/synchronous_precision_report_test.rs`;
the remaining promotion fallback is in `graph/build.rs`. See
<abstract_interpreter.md> for the broader effect-domain migration.
