# Reclaiming retired lexical cells

## Ownership and boundary

Weak capture interning alone cannot release captures retained by lexical-cell
payloads. A dead cell can also contain a closure referring back to itself,
leaving a cycle that reference counts cannot identify as unreachable.

`RuntimeRules::execute_owner` collects only after the executor returns. At this
point invocation memos have retired and every synchronous transfer has consumed
its request and result. Earlier prefix states were consumed too. Completed
purity and synchronous-effect facts contain no runtime cell references.

The trace starts from every value-bearing persistent state field, including
heap and descriptor tables and revoked audited-result descriptions. It follows
captured snapshots, retained callback sets and current cell contents, deduplicating
the two capture identity namespaces separately. All existing heap tables remain
roots, even when an object might be unreachable at runtime. Weak interner
entries and arena contents alone do not keep cells live.

Sweeping drops unmarked boxed payloads. Empty identity slots remain and IDs are
never reused. Allocation-driven scheduling uses the preceding trace's graph
work to amortize root walks; the final owner forces collection. Live roots,
heap tables and slot metadata can still grow with the chunk. No memory or
timing measurement is claimed.

## F20: transfer payloads are roots until the caller unwinds

State moves through child transfers, but a suspended caller can still hold
arguments, captured contexts or returned values in its Rust frame. Collecting
whenever a child finishes would miss these roots. Descriptor-proof results and
invocation-memo outcomes also carry values outside `ExecutionState` while live.

The safe boundary requires reviewing both the state fields and the runtime
fact lifecycle. `RuntimeQueries` consumes single-consumer results;
`RuntimeRules` consumes requests; owner finalization retires invocation memos.
Keep collection outside transfers. If runtime facts become deferred or reusable,
extend root ownership before moving or retaining any value-bearing payload past
this boundary. When adding a value-bearing state field, update the root list.

## Review

Reviewed cell consumers, complete persistent roots, both capture namespaces,
cycles, identity lifetime and single-consumer runtime facts. Execution rules,
author premises and serialized formats are unchanged. No tests, builds, corpus
runs, profiling or benchmarks were added or run locally. Existing normal PR CI
supplies hosted checks; canonical acceptance and a whole-chunk memory bound
remain unestablished.
