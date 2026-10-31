# Asserted pure calls as heap-frontier barriers (2026-10-31)

Investigation status: parked. On Tana web `78d928dca7`, gaffer main plus the
`runFunc` premise (gaffer #680), a read-only frontier for asserted pure calls
does not shrink the 965-module SCC. It only pays off with an escape analysis,
and the ceiling is still far from acceptance.

## Where `unresolved_callee` comes from

43 of the ~75 commuting pairs a greedy search needs for the 965-module SCC
sit between two `X = B(function …)` declarations. `B` is vendor `o`
(mobx-react `observer`), listed in `pure_exports` and
`callback_factory_exports`. The graph already rates both statements pure and
complete. They are ordered only by a `cell_order` `after_barrier` edge whose
two endpoints are `heap_frontier` with causes `array_content_effects` and
`unresolved_callee`.

Both causes come from `facts/array_content_effects.rs`, which never sees
cross-module purity. An imported callee has no local function body, so
`visit_call_expr` records `record_opaque_call` (`array_content_effects`) and
`record_call_target` adds the import to `calls.eager`. Then
`graph/build.rs::emit_heap_content_order` cannot resolve that name to a
source unit (`unresolved_callee`).

Graph-wide, 735 of the 1,577 `cell_order` edges join two statements the
engine rates pure and complete.

## Why statement purity cannot trigger it

SPEC § Dependency-graph array ordering says purity and binding-effect
completeness do not prove heap locality. Since #12598, checked local
construction can stay `pure` while it touches a live array, and tests rely on
it. Only an author `pure_exports` assertion (A9: no observable side effect)
excludes heap writes. Even that allows reads of anything the callee holds a
reference to.

## What was tried

The parked change projects `pure_exports` onto importing local bindings
(matched by the import declaration's `Id`). An eager direct call through one
records read-only opacity instead of a full barrier. The graph emits a
`HeapReadFrontier`, which:

- follows the current frontier, live allocations and last writers;
- precedes every later write;
- neither resets the frontier nor orders other reads.

Measured with `-c opt` on the same spec:

| Engine                          | `cell_order` edges | SCCs        |
| ------------------------------- | -----------------: | ----------- |
| devel `a7520fb8`                |              1,577 | [965, 1805] |
| `ff1e93a2` + read-only frontier |              2,173 | [962, 1805] |

The 509 read frontiers lose their mutual edges. Without a reset, though, each
depends on every live allocation and writer since the last write-capable
frontier. The transitive order through those allocations remains: a foreign
pure callee could read any array it holds a reference to.

## What would pay off

A pure foreign callee can read only cells reachable from its arguments, or
cells handed to foreign code earlier. With a chunk-wide escaped-cell set, a
read frontier would depend only on escaped cells. A writer would follow
readers only when it writes an escaped cell.

The upper bound treats read frontiers as touching no cell. Computed offline
from the measured graph, it shrinks the SCC from 962 to 616 modules. A greedy
search then needs about 21 commuting pairs to clear the rest. The 1,805-module
SCC does not change.

The escape set must cover every handoff to foreign code:

- call and `new` arguments, and member-call receivers;
- member-write values and exported bindings;
- pure-call arguments, since the callee may retain them;
- closure captures, through the existing reachability index.

Optional calls and tagged templates do not currently route their arguments
through the escape recorder. Under the detect-and-refuse rule, rarer handoffs
that neither Tana nor Claude contains can be refused instead of modelled.

The patch is kept outside the repository at
`/var/cache/agentydragon/tana-premises/pure-heap-frontier-wip.patch`.
