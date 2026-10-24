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

## Mutation boundary

A focused follow-up exposed a candidate soundness hole: changing the returned
wrapper's prototype to install a getter could hide an early read, because a
plain member read has no explicit Call node. In invocation
`069dca3b-94ce-4e6d-b621-a29bf7ccafaf`, the first thirteen cases passed and the
new rejection case incorrectly accepted the split.

Writes through the returned carrier, including nested receiver paths and aliases,
now invalidate the accessor-free proof unless they store a statically primitive
value in a known ordinary property. The mutation event conservatively includes
the installed value's full lazy effects and opaque reentry. Prototype changes
and unknown keys always take that path. The regression covers both prototype
replacement and a getter installed under an ordinary nested property; primitive
metadata writes retain callback deferral. The first mutation repair and analysis
tests passed in `68ef062d-b44c-4066-a516-f18f259af3aa`.

The final mutation safeguard passed all 165 full-suite Bazel test targets in
`4fdcaaa2-6531-4080-a3fe-9efab2b32b72`, including fifteen callback-flow cases.
Exact-head GitHub Bazel, Nix and pre-commit checks passed at `0108d44078`.
A real consumer run of the preceding candidate with an audited complex-provider
contract removed two callback-timing conflicts, leaving two independent conflicts.
It completed in 129.224 seconds wall time on RBE. The final consumer rerun checks
the mutation safeguard too.
