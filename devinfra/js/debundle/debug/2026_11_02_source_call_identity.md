# Ordinary source call targets after publication

## Candidate transfer change

An ordered record initializer can call a published local factory whose body
only creates a callback wrapper. The ordered target guard previously refused
the singleton source function if its allocation site was in `tainted_functions`.
The source-summary invocation path did not impose that condition.

Publication can change a function's properties and reachable state, but cannot
replace an ordinary source function's internal `[[Call]]`. The ordered guard
now admits the singleton indexed source target. It still rejects an unknown
alternative, including one introduced by a possible binding reassignment.
It does not reuse a source summary to recover a failed ordered result.

`taint_objects` still havocs mutable closure cells and traverses reachable
objects; `unsettle` keeps its descriptor and capture invalidation. Invocation
materializes current captured cells, checks supported bodies and bounds, and
retains the normal fallback on incomplete execution. Property lookup and
callback reachability retain their existing tainted-function checks. Imported
export values are primitive kinds, so no foreign source offset becomes a local
callable through this admission.

Tainted functions neither consume nor publish invocation memos. The existing
signature carries prefix and reachability revisions, while heap taints can
change without advancing either revision. Reusing a result from before that
change would not establish the newly admitted call's current heap proof.
This avoids a new cache or copying heap observations into every signature.

No synthetic fixture or local test was added or run under the current user
constraint. Normal hosted PR CI can check the existing suite; canonical impact
requires a subsequent reviewed downstream pin. The observed downstream
`opaque_execution` losses establish a precision regression, but do not prove
that this guard is its cause.

## F35: ordered target refusals lack the rejected condition

A saved graph's `opaque_execution` loss identifies the ordered call span but
does not retain whether the evaluated target had unknown alternatives, lacked
a local body or failed another admission condition. The same source shape can
therefore suggest different repairs that the graph cannot distinguish.

Follow-up: retain a typed operand/target refusal at the shared call boundary
and expose it in existing precision-loss explanations. Keep target admission,
missing result transfer and an incomplete invocation distinct. Do not rerun
source expressions or add a source-call result cache to fill this evidence gap.

## F36: memo signatures and heap invalidation need a shared contract

The callback-pool observation already records heap maps, native state and
taint-set sizes separately from the prefix revision. Invocation memo signatures
carry only prefix/reachability revisions, inputs and top owner. This candidate
disables memo reuse for tainted functions as required by its new admission.
The existing memo callers for other functions need an audit of each reachable
heap/descriptor/native invalidation against the signature; do not assume that
the binding-prefix revision covers every state mutation.
