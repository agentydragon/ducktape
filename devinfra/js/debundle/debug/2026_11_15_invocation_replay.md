# Conditional invocation replay

An invocation memo previously cached an outcome without replaying the world
transition or checking every readable heap/control premise. Source-local
allocation sites also pooled repeated factory objects and closures.

The ordered kernel now creates a runtime activation token for each actual
allocation event. Metadata queries retain source-site coordinates. Portable
allocation effects carry canonical module, scheduling owner, request path and
local occurrence; process-local tokens never enter the saved transport.

Replay requires equality of the complete world and unchanged COW control and
observation payloads, including inactive module banks. It also checks readable
query dependencies, query occurrence counters, recursion state and admission
inputs. A miss runs the same ordinary transfer. Recursion and resource bounds
apply before lookup, and a hit charges the recorded expansions and depth bound.

The memo stores the completed transition. One graph mapper renames fresh heap
allocations, lexical cells, captured contexts and retained callback sets across
the world, module bindings, results and qualified effect ledgers. Existing input
identities remain unchanged. Portable event paths and newly appended native
observation paths are rebased to the current request, and query counters and
proof dependencies advance with the replayed transition.

These dependencies are deliberately conservative. Mutating a covered control
payload invalidates reuse even when its semantic contents later become equal.
This can reduce foreign-call cache hits after a bank switch. Reuse does not
grant source readiness, purity or permission to omit fallback effects.

`analysis_test` tests real replay hits, budget/recursion refusal, changed heap,
descriptor, native, captured-cell and admission inputs, fresh alias/capture
graphs, and native report-path rebasing. The internal state seam is necessary
because CLI output cannot distinguish replay from equivalent body execution.
`ordered_own_data_field_test` exercises distinct factory objects and closures,
exact reads from repeated array factories, and refusal of layouts that reorder
later reads through mutated arguments, descriptors, native identities or live
captured cells. Its Node oracle checks source and emitted output independently.

The selected Fresh Set regression exposed a duplicate projection event: the
flattened call-effect pass allocated one Set handle, while a later initializer
value query allocated another handle for the same source operation. A spread
then read the second handle without a corresponding allocation effect. Native
resolution now distinguishes source projections from ordered execution.
Projections retain source coordinates without minting an activation; execution
uses one result handle for both its protocol receipt and native operation plan.
The graph lifts an existing one-shot source birth only for a recorded activation
in that source's matching scheduling owner. The unchanged CLI regression checks
the valid split; a timeline invariant keeps another activation of the same site
behind the opaque frontier and checks source/owner mismatches.
