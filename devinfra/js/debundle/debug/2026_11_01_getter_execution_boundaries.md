# Getter execution boundaries during cutover

Hosted invocation `19638a70-3be0-59bc-a057-8919df3dff03` exposed precision
conflations in the first conservative getter layer. All reproductions here use
synthetic source; the required safety property is that a later property read
cannot reuse a prefix value after skipped code may have changed its receiver.

## Causal repairs

| Failure or control                    | Cause                                                                                                                                                                                                        | Boundary retained                                                                                                                                                                                                               |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| DOM registration control              | Generic local method receiver taint made final value reevaluation lose the producer's known method.                                                                                                          | Existing canonical `implicit_capture`, opacity, call and argument facts prove a narrow unused implicit receiver; explicit alias writes still execute. Nested owner captures and dynamic scope decline this proof.               |
| Descriptor-copy argument ordering     | A proven silent primitive `push` used unknown-code taint solely to invalidate array contents. A later method lookup then invented getter installation.                                                       | `unknown_array_contents` invalidates values and snapshots separately from accessor/prototype uncertainty. No exact append or fresh identity is inferred.                                                                        |
| Constructor missing fields            | An unsupported missing-field read tainted an otherwise checked input; a later read promoted stored callbacks.                                                                                                | Missing-field admission during checked construction still requires a settled plain object and a property-specific prototype absence proof.                                                                                      |
| Escaped-record commuting control      | A changed receiver may now contain a getter as well as stored callbacks.                                                                                                                                     | The expected assumption is `possible_getter`; observed conflicts still reject.                                                                                                                                                  |
| Same-owner spread/tag/update controls | Cumulative incompleteness invalidated later modeled inputs, while a known spread call could incorrectly complete with unexpanded arguments. Blanket opacity invalidation also ran before the real operation. | Transfer-local completion controls invalidation; cumulative completion retains previous losses. Explicit canonical events cover skipped operations. Spread calls never use iterable capabilities as exact positional arguments. |
| Returned local scalar control         | Local assignments are pre-evaluated before flattened calls, caching a primitive that skipped code can replace with an object.                                                                                | Every actual incomplete transfer widens local aliases with Top, retaining previous capabilities and invalidating memos. The later separate read must record its own getter effects and ordering edge.                           |

## Canonical opacity coverage

`ExecutionIndex::mark_opaque` has seven producers. `with` enters an opaque event
after object evaluation and before its body; update emits one after operand
read/coercion and before subsequent operations; a non-expression/super callee
emits one after argument evaluation; tagged invocation emits one after callee
and substitution evaluation. Ordinary spread calls, optional calls and spread
construction already have typed call transfers. Owner/body flags retain their
completeness diagnostics but do not manufacture effects before those events.

## Knowledge states and consumers

- `tainted_objects` and `unsettled_objects` exclude settled accessor/prototype
  behavior. `unknown_array_contents` additionally excludes exact property values.
- Every existing exact-object consumer uses `object_properties_are_settled`:
  own descriptor projection/application, ordinary data copies, absent-field
  reads, dense sources and iteration results, mapped entries, private receiver
  and primitive-write proofs, local constructor inputs, reach classification,
  ordinary prototype proofs and settled element retention.
- The weaker accessor check has only three consumers: possible-getter admission
  at ordinary member read and callee lookup, and the separately guarded silent
  primitive push timing proof. Push still requires its dense-array origin,
  primitive arguments and intact Array slots. It cannot admit dense iteration,
  element/length reads, enumeration or descriptor snapshots afterward.
- Both unknown contents and local alias widening advance `prefix_revision` on
  new information. Callback-pool fixed-point observations include the contents
  state. Repeated abstract evaluation only joins unknownness; it cannot append
  fictitious exact elements.
- Native global own-data slot proofs can suppress lookup effects only. Shared
  builtin facts cover existing data properties and constructor `prototype`
  slots. Census integrity remains required; this grants no operation purity,
  exception freedom, result value or fresh identity.

A local or derived Top can hide a private graph and retains the broad available
accessor-owner fallback. A direct undeclared, unshadowed, unreassigned global
root has a narrower reach only before any uncertain/skipped execution, without
dynamic scope or a concrete ordinary-object receiver. Source callback
publication and the export/escaped pool still apply. Any actual incomplete
transfer widens native-effect state, preventing this exemption from masking a
getter installed by skipped local code. Direct global publication and reflective
installation controls retain the later read's own binding edge.

The pre-existing whole-lifetime enum `PlainData` proof remains unchanged. These
states concern ordinary-object allocations (including dense arrays); NativeSet,
Class and Instance keep separate state and receive no blanket settlement claim.

## Exact lookup certificates after hosted validation

Invocation `3e2890b4-f436-5e60-be09-af9e2ca241a5` found synthetic callee
lookups executing before existing exact call certificates could apply. The
canonical index now retains each ordinary member call behind its lookup event.
Completed namespace identity/publication/mutation checks already require
`namespace_independent_members`, whose assertion covers inert lookup separately
from call timing. Only those completed member maps discharge the synthetic
lookup. Standalone namespace reads retain their ordinary conservative transfer.

Native lookup can consume the original outer call's exact native slot premise,
including authored site evidence. The lookup records `NativeCallIdentity` or
`NativeOwnDataRead` with its dependencies; native operation use remains recorded
at invocation. `Promise.resolve` is a known ordinary own data slot even though
its host timing uses a separate lane. This grants no row, purity, input admission
or exception freedom, and the host assertion alone cannot certify a replaced
slot.

An initialized unique-writer Binding alias may preserve an external import or
unshadowed global root through the existing canonical declaration-write facts.
Reassignment, any other initializer, dynamic scope, prior skipped execution and
concrete object receivers decline this reach proof. The no-sync registration
control pins the import-alias positive; private, reassigned and post-skip aliases
pin later getter effects.

Two fixtures now isolate their original purpose. Own-name argument exception
propagation uses an exact primitive sentinel, avoiding an unrelated unsupported
catch-object member read while still checking the exact thrown value. The
empty-Set positive uses a direct neutral global write instead of an unrelated
`process.env` lookup; its opaque startup Set-slot pollution witness is retained.
A separate negative keeps the original host lookup, requiring rejection and a
MemberRead loss at that operation despite successful exact Set construction.
The rebound-factory negative pins its actual earlier `eager_rebind` rejection
and absence of a factory certificate; separate callback invocation/conflict
controls remain in that suite.
