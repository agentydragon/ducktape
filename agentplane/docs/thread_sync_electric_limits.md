# Where Electric stops fitting thread sync

Electric is the first thread-sync implementation (<thread_view_sync.md>). It is the one we reached
for first; nothing has settled it as the answer. It is developed as far as Electric goes without
working against it: stock shapes, subset snapshots and the published client, with the app's proxy
for authorization. Where following an agent wants something Electric cannot do that way, the
implementation stops there and the mismatch is recorded here. A later choice — accept the cost,
adopt another engine, or write a protocol for this use case — then starts from what was learned.
IDs are from <thread_sync_requirements.md>.

## What fits

- **A change anywhere reaches a reader holding the row** (P3, S4): one shape per thread carries
  every change on one live log.
- **Scrolling back loads only what the reader lacks** (D1, E4): the window is read as subset
  snapshots of a shape whose predicate never moves.
- **Resuming after a disconnect** (P6) is an offset and a handle.
- **Streaming costs the batch** (D4): a body is chunk rows, so an append is one insert.
- **A transaction arrives whole**, before one `up-to-date`, so a client never shows half of it.
- **Readers of a thread share its shapes** (O1).

## What does not

1. **The live log belongs to the shape, not to the reader.** Electric does not filter live changes
   per reader. Every follower of the thread's entity shape receives every change in the thread,
   and every follower of a payload field receives every chunk of every body in that field,
   including bodies it never loaded (`test_electric_chunk_compaction.py`). To give a reader less,
   the shape's predicate must be narrower, and a new predicate is a new shape that replays its
   rows: the rejected window in a shape's `where` (<thread_view_sync.md> § Rejected designs).
   The reader therefore pays **E5** for the whole thread. When a reader follows an active tail it
   holds, most of that traffic is rows it holds anyway. It is waste when the thread is long and
   busy and the reader is scrolled away, and on fields with many bodies. A server that knows
   what each reader holds would send only that.
2. **A delta exists only as a committed row.** Electric replicates PostgreSQL, so a streamed batch
   reaches a reader only after it is written, committed, logged to WAL, read by Electric and
   pushed.
   - **Storage:** every append leaves a chunk row and a manifest row behind. That is the O(n)
     storage **D5** wants gone.
   - **Traffic:** each append also changes the entity row that names the body (its reference's
     chunk count), so it re-sends that row too.
   - **Latency:** each append's latency includes the commit and Electric's replication hop. No
     measurement covers it yet.

   A stream written for following an agent could forward batches as they arrive, and persist only
   the result.

3. **Compaction cannot be silent to followers.** Replacing a finished body's chunks is itself a
   change on the field's shape, so every follower of the field receives it: about twice the body
   under the proxy's `replica=full`, about once without it (`test_electric_chunk_compaction.py`).
   A reader already holding the body needs none of it, but Electric cannot know that.
4. **The client needs working around in two places, and a refetch is ours to keep.** The
   published `@electric-sql/client`:
   - **moves a live stream to a subset response's offset**, skipping changes to rows outside the
     subset. The store's fetch client rewrites that response header, the one place the store
     interprets Electric's protocol itself.
   - **withholds `up-to-date` for a minute after a shape is reopened**, so the store never waits
     on it.
   - **drops a shape's rows on `409 must-refetch`**. Keeping them on screen (P5) is the store's
     own code.

   `thread_store.test.tsx` covers the store's handling of each.

5. **One Electric instance per replication slot** (O2). Shape logs live in memory or on local disk,
   and there is no shared storage backend. Scaling out means a CDN, or separate instances with
   their own slots behind sticky sessions.
