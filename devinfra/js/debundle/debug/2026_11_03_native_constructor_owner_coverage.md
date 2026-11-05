# Empty native collection initializer coverage

The owner certificate applies only to an original simple variable initializer
whose AST is a bare zero-argument `new Map()` or `new Set()`. The dedicated
root `EvaluateInitializer` request temporarily installs that exact owner and
call-site candidate on its `Executor`. In the existing `ValueExpr::New`
dispatch, after `resolve_native_call` returns the selected row, the root records
a typed transfer witness only for the matching Construct/Fresh/Never row. The
candidate ties the original callee Id to the lowered Binding Id and exact site;
the dispatch's native resolver proves the unshadowed global identity through
the intact census or accepted exact-site input. It also requires no arguments,
writes, invocations, or possible abrupt operand. Child executor requests
receive no candidate.
The final initializer check also requires the sole successful value to be
`Top`; generic values, resolver queries, and invocation memo results cannot
close the owner.

The opaque `Top` result preserves all later Map/Set uses as unmodeled. The
constructor's internal fresh allocation has no generic allocation effect cell
in this report domain. Its identity is not available to user code before the
constructor returns and the initializer writes the lexical binding. That
binding write and the global Map/Set read remain in the projected effects;
later reads, method calls, and publication keep their existing opaque or
operation-specific proofs. `map_empty` and `set_empty` retain their existing
exact-site shape, identity, and known-write checks, while the existing
NewTarget/fixed-prototype boundary remains unchanged.

Focused synthetic validation uses separately extracted Map/Set initializers
that move before residual comma expressions containing harmless
`globalThis.Map` / `globalThis.Set` reads. The output remains unchanged, and
later `.size` reads plus the console call remain uncovered. A separate
completing `var` read-before-initialization fixture observes `undefined` before
both constructions, confirms binding read/write conflicts remain observed, and
rejects that reorder. Controls exercise shadowing, global
writes, dynamic scope, nonempty/undefined/spread arguments, aliases, qualified
names, sequence and assignment wrappers, and wrong Map/Set input kinds.

Both focused Bazel targets passed in invocation
[496a1d07](https://app.buildbuddy.io/invocation/496a1d07-248a-4cc7-a54e-f5d3488a2205).
The subsequent edit only removed a redundant throwing lexical-read control.
No corpus payoff or runtime/browser result is established here.

Fixture friction: a full conflict stream lists conflicting reordered pairs,
not every retained read/write. An assertion expecting a Binding conflict must
make that read observable and confirm the actual source snapshot. A discarded
comma operand is a poor control, and replacing a completing `var` example
with a throwing TDZ example changes its premise. Use the completing observable
case before diagnosing missing effects or changing the implementation.

## Follow-up: Empty WeakMap effects (2026-11-05)

The exact root-operation route now admits a bare zero-argument
`new WeakMap()` alongside `new Map()`. It reuses the checked
`keyed_collection_new` effect fragment and keeps the result `Top`; it does not
track collection contents or certify later methods. `weak_map_empty` supplies
the selected site’s original global constructor identity when the shared
census is dirty, while unselected constructors and later method calls retain
their existing conservative analysis.

The neutral local-array regression failed before this route was added because
the WeakMap initializer constrained the split with an opaque heap barrier. The
repaired candidate passed the focused public RBE targets
`local_array_push_order_test`, `native_operation_input_test`,
`native_construction_row_test`, and `effect_translation_validation_test` in
invocation `d7963806-2421-413d-aa91-54fbb04766e5`. These fixtures verify only
the stated constructor/effect boundary; they do not establish a corpus payoff
or canonical gate result.
