# Synchronous-execution fallback

The synchronous interpreter reports `HavocReach`, but the report is not yet an
authority for graph construction. `HavocReach` is documented as diagnostic-only
in `reports/schema.rs`; the conservative graph fallback still promotes the
read closure for incomplete at-init calls.

Binding effects have a separate authority path. An exact ordered root
initializer can carry an `OperationReceipt` for its represented lexical reads
and rebinds. `LegacyExecution { complete }` remains a compatibility authority
for other owners; its completion flag is not an operation receipt. Neither path
proves property, heap, global or host-effect coverage.

The ordered family includes a single ordinary identifier-call initializer
with zero actual arguments. Its existing child read/rebind receipts and actual
return value compose before exact root sealing. Callable selection can also
travel from an actual own-data read or closure creation through arguments,
parameter reads, returns and exact initialized-binding publication. Ordinary
simple/default parameter execution and ordinary noncaptured local `let`/`const`
declarations supply actual creation/initialization/read receipts. Initializer
effects compose independently, and TDZ or abrupt initialization cannot seal a
normal root. Captured cells, `var`/function hoisting, later assignments and
separate body-var environments remain open.
An alias without an actual selection producer remains uncovered and retains
closure fallback; normal completion alone cannot restore legacy authority.

## Remaining effect-authority boundary

`graph/build.rs` emits known synchronous reads and rebinds according to their
binding-effect authority. Incomplete owners still fall through to a fallback
that expands unresolved roots through `SourceUnitReadClosure` and adds closure
reads as eager uses. The fallback is
not conditioned on `HavocReach`.

Replace remaining `LegacyExecution` owners with exact lexical receipts before
retiring completion-flag authority. Missing coverage retains fallback, and the
source-only author assertion stays separate from inferred receipts.

The next safe expansion is to let bounded, source-known incomplete execution
replace fallback constraints for additional effect domains only after the
retained closure covers every behavior whose omission could make a dropped
constraint unsound: source reads and rebinds, initialization order, property
lookups and accessors/getters, heap mutation, global reads and writes, and
host-triggered callbacks or reentry. The closure must include effects of every
callback and accessor the statement can run. Preserve separate ordering
constraints for uninitialized observations. Keep the fallback for `Global`
reach and whenever any effect domain remains unrepresented. `HavocReach` and a
list of precision-loss sites classify possible code; neither proves this effect
closure complete.

A legacy acceptance rejected by an effect-based validator is differential
evidence, not by itself a defect in the validator: it can expose an unsound
legacy acceptance or conservative/incorrect new effects. Classify the original
program behavior and the new effect evidence before choosing which side needs a
change.

## Required invariant

The split is safe only when every dropped fallback edge is redundant with the
complete modeled execution effects, with emitted initialization behavior
preserved. An `OperationReceipt` accounts for the lexical binding effects it
records, not other effect domains of the same execution. Neutral cases must
cover a bounded local call whose represented read remains ordered correctly
and a global/opaque call that keeps conservative promotion. Existing negative
controls for borrowed native receivers, escaped callbacks, and dynamic scope
must keep conservative promotion where their effects are not proved;
uninitialized observations must retain their separate ordering constraint.

The current reach classifier and precision-loss report are in
`facts/sync_effects/interpreter.rs` and `e2e/synchronous_precision_report_test.rs`;
the remaining promotion fallback is in `graph/build.rs`. See
<abstract_interpreter.md> for the broader effect-domain migration.
