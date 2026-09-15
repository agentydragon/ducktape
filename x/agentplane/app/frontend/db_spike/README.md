# TanStack DB collection spike

Test-only evaluation of `@tanstack/db` **0.9.2**, pinned in the workspace's
`devDependencies`. No production importer, protocol, UI, persistence, or transport
changes. `Snapshot`/`Changes`/`Entity` are deliberately incomplete local test sketches,
not proposed protobuf messages. The [layering design](../../../docs/thread_layering.md)
owns the application contract.

```bash
bbr test //x/agentplane/app/frontend/db_spike:test
```

## What is exercised

- Real custom collection `begin`/`write`/`commit` transactions, observed directly without
  React batching. One per-Thread collection holds independently addressable item,
  command, control, and coverage rows; this is the atomic boundary. Separate entity
  collections plus an external cursor are **not** established as atomic.
- Actual incremental live queries: an item subscriber stays quiet during unrelated
  item updates and coverage-only progress. Generated protobuf receipt values and
  cursor ordering remain exact above `2^53`.
- Explicitly requested history races live updates; older revisions cannot win or
  move coverage. Conflicting equal revisions and responses from a replaced bootstrap
  are rejected/ignored respectively. Long-gap replacement invalidates old followers,
  history, and evidence requests without silently replacing the source identity.
- Raw evidence is an independent collection, populated only by an explicit request.
  Exact generated envelopes are deduplicated with protobuf equality; loading them
  neither advances coverage nor notifies conversation subscribers.
- Window eviction keeps current controls and pending commands. Collection cleanup
  belongs to the view owner. No optimistic mutation handlers are supplied; local
  inserts fail instead of fabricating admitted work.

The adapter uses [custom sync primitives](https://tanstack.com/db/latest/docs/guides/collection-options-creator)
and [live queries](https://tanstack.com/db/latest/docs/guides/live-queries). Its commit
calls await the library's applied receipt. There is no public sync rollback primitive,
so batches are validated before `begin`. `rowUpdateMode: "full"` makes a changed row
an explicit replacement, including a nullable command outcome.

## Deliberate limits

This establishes a same-collection fit, not a production sync engine. Requests are
test-controlled promises; reconnect transport, protocol validation, command posting,
automatic `loadSubset` query pushdown, server checkpoint generation, and durable
client persistence are not implemented. History/control/command completeness remains
the server contract; TanStack DB does not establish it. Duplicate live ranges currently
request rebootstrap rather than implementing a retained range-overlap protocol.

The collection does not bound itself: the test supplies explicit window evictions.
Reading-anchor selection, page/evidence eviction and refetch, cancellation during
eviction, error UI, history availability, and exact grouping/turn/control row schemas
remain design work. Query subscribers are exercised; React render scheduling and
cross-collection joins are not. No throughput benchmark or browser-paint claim is made.
