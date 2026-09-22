# Option: one watch over a moving range

The protocol **P10** describes, written out. The client states what range it wants and what it
already holds; the server sends the difference and keeps it informed of that range. There is one
subscription and it moves.

## The exchange

Using the owner's numbers — the client holds segments 100–200 at revision 9932 and scrolls up:

```text
GET /threads/{id}/conversation
      ?want=50:150              # the range I care about now
      &have=100:200             # what I already hold
      &since=9932               # …as of this revision
      &content=text,confirmed_input
```

The server answers with three things, from one query against the materialized fold:

```text
{ "backfill": [ …segments 50–99, whole… ],          # want \ have
  "changed":  [ …segments 100–150 with revision_cursor > 9932… ],   # want ∩ have, stale part
  "through":  10014 }                                # the new watermark
```

The client drops 151–200 itself. Then it watches:

```text
GET /threads/{id}/conversation?want=50:150&since=10014&content=…     # long poll
```

which blocks until something in 50–150 changes, returns it, and is re-issued.

**The watch is the outstanding request.** There is no subscription object on the server to create,
update or expire — "moving the watch" is issuing the next request with a different `want`. That is
what makes P10 fall out rather than needing machinery: the client carries its own state, so the
server has nothing per-reader to keep in step with it.

## Against the requirements

- **P10** by construction. `want`, `have` and `since` are the whole mechanism.
- **P8 / D1** by construction. `content` is a parameter of the same request. "Text now, reasoning
  when I expand it" is two values of one parameter, and a reader that wants everything streamed says
  so. Nothing about laziness is in the protocol.
- **E4, E5**: the overlap is never re-sent — only its changes. This is the property Electric cannot
  offer at all.
- **P5**: nothing is redefined underneath the reader, so nothing is withdrawn. No rotation, no
  window replacement, no scroll restoration.
- **P6**: a dropped connection is a failed request. The next one carries `have` and `since`, so
  reconnect is the _same operation_ as any other poll — there is no resume path to get wrong,
  because there is no server-side position to resynchronise.
- **O1, O2**: **no per-reader server state between requests.** Any replica can serve any poll; a
  rolling deploy drops in-flight polls and the client re-issues. This is strictly better than both
  the shape model (which keeps shared-but-churning state) and the SSE model (which pins a reader to
  a replica).
- **S3**: `through` is the caught-up signal, and it is one number rather than a per-partition
  composite.

## What it costs, honestly

- **The server owes the delta query.** `segment_index BETWEEN 50 AND 99` (whole) `OR
(segment_index BETWEEN 100 AND 150 AND revision_cursor > 9932)` — one index range scan over the
  fold. Tractable, and the load-bearing assumption is that **every mutation advances the entity's
  `revision_cursor`**, including a body change. In the current fold it does, because a body's
  reference is materialized into the entity row and carries the revision. That wants a test pinning
  it rather than a reading of the projector.
- **The client is trusted about `have`.** A client that claims 100–150 it does not hold gets a
  silently incomplete view. Not a security question (the range is still authorized), but a client
  bug becomes missing data rather than an error. Cheap mitigations: return a count or checksum for
  the overlap so the client can detect the disagreement, or let `have` be omitted to force a full
  read.
- **A long poll per change batch.** One round trip to re-establish the watch after each return.
  Electric's `live=true` is the same mechanism, so this is not a regression — but it is a round trip
  where a pushed stream would have none. If that ever matters, option_app_push.md is the upgrade,
  and it does not change this protocol, only its transport.
- **Ranges are contiguous here.** A deep link followed by scrolling could want two disjoint ranges.
  `want` and `have` generalise to lists at the cost of a messier query; not needed on day one, worth
  not designing out.

## Why it is not just A1

option_poll.md § A1 is the same family and lacks the one idea that matters: **`have`**. A1 polls a
delta against a fixed window; this moves the window and pays only for the move. Without `have`, a
reader that scrolls up re-downloads its whole new window, which is the defect that started this.

## What it needs that does not exist yet

- `segment_index` on the fold, so a range is a range. This is the same stored monotone index
  option_electric_pages.md needs, and it is the one piece of that design worth keeping whatever
  wins — cursors cannot express "messages 50–150" because how many segments a cursor range covers
  depends on how densely a turn packs them.
- A `content` selection parameter honoured end to end, which the spec already specifies and no
  implementation currently has.
