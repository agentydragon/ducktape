# Imported callback factories lose return and escape information

A named import of a function returning a closure was unknown to synchronous
execution analysis even when the purity oracle knew its body was pure. The
unknown-call path invoked callback arguments at factory creation and put every
callback into the global escape pool. A second factory invocation could then
spuriously invoke callbacks retained by the first factory. Treating the call
as pure or only suppressing its current arguments does not model the return.

The neutral CLI reproduction imports `factory(callback)`, whose body is only
`return () => callback()`. The consumer creates a wrapper around a read of a
later binding and invokes the wrapper after that binding initializes. Splitting
wrapper and value is valid. On the base revision, BuildBuddy invocation
`e27c6099-08da-413d-a39a-c41d25d64ef2` rejects it with an atomic conflict from
invented eager-use and sequencing edges. The immediate-callback control rejects
as expected in the same run.

The model keeps an immutable callback set on the returned abstract value. Factory
creation does not publish that set. Later invocation, opaque escape, exports,
and writes through the result retain conservative callback reachability. Opaque
wrapper invocation includes the original callbacks' full lazy read/rebind closure,
including callbacks returned by those callbacks. Captures and callback sets are
interned to keep value comparisons bounded.

Source inference recognizes only stable exported function declarations with one
plain parameter, whose sole statement returns a zero-argument function/arrow
whose sole expression calls that parameter with no arguments. Whole-chunk value
flow must contain exactly that function and dynamic scopes disable inference.
This does not infer callback timing from purity assertions.

Complex providers can use the separate `callback_factory_exports` author contract,
with an explicit source/helper audit. The importer checks for one non-spread
inline function/arrow and an ordinary call. Existing callback variables, other
argument counts, spread and construction retain the unknown-call fallback.
The contract promises no synchronous application callback reentry or global/host
publication; callbacks are exposed only through a fresh ordinary returned graph
without proxies/accessors. It conveys no purity claim. See SPEC.md and the design
and purity soundness documents for the maintained contract.

Initial integrated checks passed in
`129ae8d9-57a4-4ef1-a597-a7299e89316d`: the analysis tests and the first ten
cross-module callback cases passed. Final checks also include source-proof limits,
import/local aliases, purity-only assertions, and mutation of the returned value.
