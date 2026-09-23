# Option: the client polls a window of positions

No sync engine. The client asks for a fixed count of consecutive `entity_index` positions and
re-asks for the same range on a timer. Bodies are fetched separately, by reference, and never
refetched.

It sits between `A0` and `A1` in <option_poll.md>'s family: more than "poll everything", less than
"poll a delta". Referred to as **the window poll**, not a letter — `A2` would read as a rung past
`A1`, and it is a rung before it.

## The protocol

```text
GET  /threads/{id}/conversation/window?end={index}&limit=60&fields=text,reasoning
POST /threads/{id}/conversation/bodies        # [{ref, have_chunks}, …]
```

The window returns entity rows — metadata and the four payload references — for
`entity_index` in `(end - limit, end]`, plus the `view_state` singleton whatever the range. No
content. The client keeps a map keyed by `(entity_kind, entity_id)`, renders it sorted by
`entity_index`, and re-issues the same request every second. `ETag`/`If-None-Match` makes an idle
conversation a 304.

The body request is a batch: the client names the references it wants resolved and how many chunks
of each it already holds, and gets the chunks it is missing. It is a `POST` because the reference
list is unbounded, not because it writes anything.

Scrolling up moves `end` back. The re-issued request then carries the overlap the client already
holds, so **E4** fails as written — the same way `A1` fails it, and for the same missing `have`. A
client that instead keeps the old range polled and adds a second, adjacent one reaches **E4**,
paying one request per held range; whether that is worth it is a measurement, not a design
question.

**Two requests per cycle, and the second is skipped when no reference changed.** That is what makes
the open path `O(1)` (**E1**) rather than `O(bodies)`: a batch endpoint, not one request per body.
A per-body fetch would reproduce the measured defect at half the request count, which is not a fix.

## Why it is cheap

The fold already stores bodies immutably and addresses them by content revision, and this option is
mostly the observation that nothing else is needed.

- `conversation_payload_manifest` is insert-only, keyed by `(…, field, generation, revision_cursor)`.
  A reference therefore names an exact, frozen revision: **if a body changed, the entity row carries
  a different reference**, so a resolved reference is never stale and never refetched.
- `conversation_payload_chunk` is keyed by generation with a dense `chunk_index`, and an append
  writes one new chunk at the prior chunk count. A growing body transfers only its new chunks:
  the manifest gives `chunk_count`, the client holds _k_, it fetches `[k, chunk_count)`.

So the polled window carries only entity rows. Sixty of them is tens of kilobytes; an idle
conversation is a 304 with no body at all.

## Against the requirements

**What it gets for free, and the trap it avoids.** The window is re-read **wholesale, by position**.
Recency plays no part in deciding what is returned, so an edit to a segment in the middle of the
held range arrives on exactly the same terms as an edit to the last one. **S4** holds by
construction rather than by care. Every design that filters by `revision_cursor` has to keep
position and freshness as separate axes and can get that wrong; this one has no freshness axis to
conflate.

- **P1/E2**: the range is a fixed count of positions. This is what `entity_index` is for, and why a
  cursor cannot substitute — how many rows a cursor range spans depends on how densely a turn packs
  them.
- **P2**: `~` on a timer, `+` long-polled. On a timer, updates land at the poll interval —
  chunky streaming at 1 Hz. Long-polled (rung 2, the entry point under **E6**), a change is
  answered as soon as the thread's wake-up fires.
- **P5/P6**: nothing is ever redefined or withdrawn — rows only merge into a map by key. A
  disconnect is a failed request, and the next one succeeds with what is on screen untouched.
- **P7**: a stale-epoch window read answers `410` and the client reloads. Nothing rebuilds a
  selection, so the pending-selection double buffer is not needed anywhere.
- **P8/D2**: `fields` is a query parameter of the window and the batch alike. "Text now, output when
  a disclosure opens" is two values of one parameter rather than two mechanisms.
- **S1**: a body renders at exactly the revision its reference names, provided the client bounds its
  chunk read by **that revision's** `chunk_count` and not by whatever the generation has grown to.
  Chunks are owned by the generation, manifests by the revision; the bound comes from the manifest.
- **S3**: `view_state` rides in every window response regardless of range, carrying
  `through_cursor` and `unresolved_count`. A reader scrolled into history still learns the
  conversation moved, which is the one-row form of the tail subscription <requirements.md> § S4
  permits, and what a "jump to latest" affordance renders from.
- **O1/O2/O3**: there is no per-reader server state to bound, share, or lose. Any replica answers
  any request, and what a client is subscribed to is in the access log.

## What it costs

- **E4, E5, D1: `−`.** The held window's metadata is re-sent every poll — roughly 30–60 KB/s per
  open tab while a conversation is moving, ~0 when idle — and a scroll re-sends the overlap. There
  is no `have`, so nothing tells the server what the client already holds. This is the option's one
  real inefficiency and it is deliberate: `have` is the next rung, not this one.
- A constant request floor of one or two per second per open tab, whether or not anything changed.
  Conditional requests make the idle case nearly free, but the floor is real and is what a long poll
  removes.

## Why it is simpler than what is deployed

It deletes rather than adds. Gone: shape identity and its `where`-predicate cache keys,
`ELECTRIC_MAX_SHAPES` pressure, server-side interest resolution, `must-refetch`/409, handle-and-offset
resume, and the pending-selection double buffer — about 460 lines of proxy
(<../../app/electric.py>) and most of a 663-line `conversation_store.tsx`. The
`source_id`/`projection_epoch` scope params stop mattering with it, since they exist in the deployed
design as part of a shape's cache key.

Nothing here is a new engine, so **O4** costs nothing to argue.

## The increments

1. **The window poll**, as above. **Excluded by E6**, since it re-issues on a timer; kept here
   because the rung above is defined against it.
2. **Long-poll the same URL** — now the entry point. The server holds the request until the thread's
   Postgres wake-up (`ThreadUpdates.changes`) fires, re-reads the window and answers only if its
   `ETag` differs from the client's `If-None-Match`; otherwise it keeps waiting. Same endpoint, same
   client path, no request floor. Comparing whole-window ETags still re-reads wholesale, so this
   rung does not need `revision_cursor` to be correct either. Filtering by revision instead of
   re-reading is where that proof becomes necessary.
3. **`since` and `have` parameters**, reaching **E5** and **D1** — <option_moving_window.md> — only
   if measurement says the metadata re-send matters.

Each rung is shippable and strictly better than the one below, which is **D3** in its strongest
form: the first rung is also the fallback if the deployed design has to go before its replacement
is designed.

## What to settle

- Whether the batch body endpoint should cap its response, and what a client does with a partial
  answer. An unbounded reference list is an unbounded read, which **C2** forbids.
- Whether 60 positions is the right window, given that a position is any row and not only a rendered
  one — `view_state` and `command` rows occupy positions too, so a 60-position window renders fewer
  than 60 segments.
