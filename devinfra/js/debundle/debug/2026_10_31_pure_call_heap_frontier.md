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
that neither Tana nor Claude contains can be refused instead of modelled; that
roughly halves the surface to model.

## Why every `new Fx(...)` escapes its callbacks

`Fx` (Tana's `UICommand`) is the largest route into Tana's callback pool:
86 of 325 distinct escapes on devel `5dfe2142`, probed as in
[the pool-composition note](2026_10_30_havoc_reach_pool_composition.md).
The chain starts at the helper factories `hO`/`fO` (statements 2698–2705).
Each calls `Se({...})` with a literal record inside its own body.

1. The `retained_record_factory` premise is consulted only for a top-level
   `X = Se({...})` statement. Inside `hO` the construction checks the
   prototype itself, and `field_may_exist_on_object_prototype(checker)`
   rejects it: the census leaves `Object.prototype.checker` 63,089 possible
   writers.
2. With that check forced to pass (a diagnostic switch), the construction
   still fails, at `unchecked_property_source` (parameter 0). The `Fx`
   constructor reads `e.defaultWeight`, `e.setsFocus` and `e.subcommands`.
   The record was allocated inside the invoked helper, so it is not in
   `prefix_object_properties`, which tracks only top-level allocations. The
   read takes the `unsupported_local` path and taints the record. Every
   later call through the same allocation site then fails the plain-data
   check.
3. After the first failure, `Fx`'s class members are escaped. Every later
   `new Fx` is rejected as `class_members_escaped`, including top-level
   statements the premise would otherwise admit.

Step 2 needs property tracking for literals allocated inside a call. That
covers reads of own static keys before any write or escape. Even with every
`Fx` escape removed, the median pool event still has 240 members, against a
limit of 64.

The patch is kept outside the repository at
`/var/cache/agentydragon/tana-premises/pure-heap-frontier-wip.patch`.

## Barrier chain census on gaffer main + #686 (ducktape `e1f145dd`)

**Question:** do the heap barriers that close Tana's 963-module Pass-1 SCC
include edges a sound engine change could drop?

**Answer:** almost none. Under the current model the chain is justified:

- every write-capable barrier may write any tracked array held by foreign
  code;
- the foreign-held set is non-empty from statement 0.

The only unjustified edges are 419 read-read pairs between `pure_exports`
calls. Dropping them soundly needs read frontiers, and those add more edges
than they remove. No engine change was made. Pass 1 stays at 963 (the
emulated gate gives 966–967).

### Census

- **The barriers form a total order.**
  - 1,335 statements are heap barriers.
  - Each of the 1,196 `after_barrier` edges points to the previous barrier
    in source order. Out-degree is exactly 1, because
    `emit_cell_order_events` keeps one `opaque_barrier`.
  - 119 more edges are `frontier_after_cell`, ordering a barrier after an
    earlier array allocation.
  - The constraining cross-module heap edges are 958 `after_barrier` and 93
    `frontier_after_cell`.
- **Tracked contents are live from statement 0.** There are 1,698 eager and
  7,931 lazy allocation sites, and lazy allocations count as live from the
  start.
- **Escapes are everywhere.** A probe that recorded `record_escaped_expr`
  cells, not committed, found:
  - 6,380 sites escape inside function bodies, mostly JSX children arrays;
  - 1,216 sites escape at init;
  - the first eager escape is statement 37.
- **Almost nothing is observed precisely.** Only 4 statements make a precise
  non-barrier observation, touching 6 eager cells. Nearly every heap event
  is itself a barrier.

Barrier statements by the opaque operations that make them barriers. A
statement can count in several rows.

| Family                                                                     | Statements |
| -------------------------------------------------------------------------- | ---------: |
| mobx-react `observer` (`X = B(function …)`, `pure_exports`)                |        537 |
| synchronous execution incomplete (the fallback families)                   |        303 |
| UICommand factories `Se`/`ft`/`Zt`/`uf`/`fs` (`callee_heap_effects`)       |        248 |
| natives: `new Map`/`new Set`, `Object.freeze`/`defineProperty`, `Symbol.*` |         95 |
| React `forwardRef`/`memo`/`lazy`/`createContext`, namespace receivers      |         94 |
| Zod builders and chains                                                    |         67 |
| other                                                                      |        <40 |

### What each candidate change does

These are offline experiments on the saved graph. The chain is rebuilt from
the remaining barriers, using the realizability emulator from
[the Tana SCC note](2026_10_31_tana_scc_pessimism.md). Rebuilding the
unchanged chain gives Pass-1 SCCs of 970, 12 and 2; the engine gives 963
and 2.

| Change                                                                    | Pass-1 SCCs |
| ------------------------------------------------------------------------- | ----------- |
| Escape-gated frontier (only cells escaped earlier)                        | unchanged   |
| Read frontiers for `observer` calls (A9 `pure_exports`; 1,979 heap edges) | 967, 12, 2  |
| Read frontiers for every pure barrier statement (2,403 heap edges)        | 966, 12, 2  |
| `observer` calls touch only operand cells (not a frontier)                | 613, 11, 2  |
| + Zod, React, natives, UICommand                                          | 523, 11, 2  |
| Every complete barrier statement demoted                                  | 257         |
| No barrier ordering at all                                                | none        |

The escape-gated frontier changes nothing because the lazy escapes make the
foreign-held set non-empty from statement 0. The read/write split is sound
under A9 but has the effect seen above: each read frontier depends on the
latest write frontier, and each write frontier on every read since.

Demoting families never clears Pass 1. The remaining barriers re-chain, and
the 303 incomplete ones alone close a 257-module cycle. So Pass 1 needs both:

- heap-locality knowledge for the write-capable callees. This is an
  audited premise or vendor summary: "reads and writes only arrays
  reachable from its operands". Nothing in the engine provides it today.
- completeness of the incomplete barrier statements, which are the same
  loss families as the fallback edges.

The alternative is a policy change: extend `trusted_dataflow_summaries` to
array contents, so opaque operations stop ordering against each other the
way general heap objects already don't. SPEC § Dependency-graph array
ordering excludes this on purpose, and it is not sound.
