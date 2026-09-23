# Option: one Electric shape per thread, windows as subset snapshots

The deployed design and <option_electric_pages.md> both put the window in a shape's `where`, and a
shape's `where` is fixed at creation, so moving the window means a new shape. Electric's subset
snapshots remove that constraint: the shape covers the whole thread, and the window is a one-shot
read inside it. Evidence for every verdict: <prior_art/electric_powersync.md>.

## The exchange

Per thread, all proxied by the app, which pins each shape's `where` to the authorized thread and
epoch (**C2**, **P7**):

- an **entity shape** over `thread_entity`, in `log=changes_only` mode, so creating it takes no
  snapshot;
- one **content shape per payload field** over the chunk table, so a reader subscribes to the
  fields it wants (**P8**); a subset cannot select columns;
- the existing control shape for commands.

Opening a thread on its tail:

```text
GET  /…/entities?offset=now                                  # the log's current position, no rows
POST /…/entities  {where: "true", order_by: "entity_index DESC", limit: 60}    # subset: the tail
GET  /…/entities?offset=<pos>&handle=<h>&live=true&live_sse=true               # follow the log
```

Each subset response ends with `snapshot-end {xmin, xmax, xip_list}`, which tells the client which
live-log changes the snapshot already contains. Scrolling back is one more subset for the range not
yet held (`entity_index < 40`, `limit 60`), and bodies for the rows now in view are a subset of the
content shape (`entity_id = ANY($1)`). Nothing re-sends the rows a client holds, and every change
to any row in the thread arrives on the one live log, whatever its position (**D1**, **E4**,
**S4**).

## Against the requirements

The full table is in <prior_art/electric_powersync.md>. What differs from the other columns:

- **O1 `+`**: shapes are per thread — the entity shape, one per field in use, the control shape —
  shared by every reader of the thread, whatever window each holds. Subsets are stateless queries.
- **E5 `~`**: the live log carries every change in the thread, including rows outside a reader's
  window. That is wasted traffic rather than wrong data, and for a reader on the tail most of it is
  rows the reader holds anyway.
- **P5 `~`**: a `409 must-refetch` (eviction, a schema change, a lost slot) makes the stock client
  discard the shape's rows. A client that keeps its rendered rows through the refetch avoids it;
  that is ours to build.
- **O2 `~`**: one active Electric per replication slot, with no shared storage for shapes.
- **S1 `~`**: entity and content shapes have independent logs, so a body can arrive ahead of or
  behind the metadata naming it. The `PayloadRef` extent (the chunk count a reference names) is
  still what keeps a body from rendering bytes its reference does not name.
- **E6 `+`**: the live log is followed over SSE.

## What it deletes

Against what is deployed: interest resolution and its rotation (`entity_interest`,
`ThreadInterestExpiredError`), the epoch-and-selection double buffer, per-body shapes and their
request count. Against <option_electric_pages.md>: the page partition, its landing pad, the
composite catch-up gate across pages, and `owner_segment_index` on chunks.

## To settle

- **The client.** TanStack DB's on-demand mode deduplicates only identical requests, so a live
  query whose `where` moves re-fetches the overlap. Cursor paging fetches only new rows; the raw
  `@electric-sql/client` with `requestSnapshot` avoids the question.
- **Subsets as `POST`**, with `queryable_columns` set on the shape, and a `limit` cap the proxy
  enforces.
- **What a reader does on `409`** without tearing down what it renders.
