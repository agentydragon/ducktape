# Option: the app serves the conversation over HTTP, the client polls

No sync engine. The app reads the materialized fold (C1) and answers ordinary requests. Two rungs,
and the first is deliberately the stupidest thing that could work.

## A0 — poll the whole conversation

`GET /threads/{id}/conversation` returns every entity and every body as one JSON document. The
client replaces its state and re-renders. Repeat every second.

Worth stating properly rather than as a joke, because of what it gets right for free: **P5, P6, P7,
S1, S2, S3, S4, O1, O2, O3, O4 all hold trivially.** There is no subscription to lose, no partition
to cross, no per-reader server state, nothing to resume, and every response is internally consistent
because it is one snapshot of one transaction. A disconnect is a failed request; the next one
succeeds. An epoch replacement is just different bytes.

It fails **E2** (bytes are the whole conversation), **E5** (re-sends everything each time), and
**P2** at any poll interval a reader would notice. It does _not_ fail E1 or E3 — one request per
second is `O(1)`, and streaming costs nothing extra because it is the same request.

A conditional `If-None-Match` makes the idle case free and changes none of that.

**What it is for:** a floor. Any option that costs more complexity than this owes the difference in
requirements it satisfies that this does not — which for a long conversation is E2 and E5, and not
much else. It is also the fastest thing to build if the current implementation needs to be replaced
before its replacement is designed (**D3**).

## A1 — poll a delta

`GET /threads/{id}/conversation?since={position}` returns entities changed after a position, plus
the bodies the client asked for. The client merges by key and keeps its own order. Long-poll rather
than fixed interval, so the server holds the request open until something changes or a timeout.

- **E2, E5**: bytes are proportional to what changed, not to the conversation. The open path is
  still one request for the tail plus its bodies.
- **E3**: a long poll that returns and is re-issued is the same shape as a live shape subscription —
  ~0 requests per item, one connection held.
- **P8/D2 fall out naturally.** `since` and the content selection are _query parameters_. "Give me
  text now and reasoning only when I ask" is two values of one parameter, not two mechanisms. This
  is what the spec's `ContentSelection` was always describing.
- **D1: no.** A1 as written has no `have`, so moving the window re-downloads its overlap. The fix
  is one parameter, written up as its own option (<option_moving_window.md>); A1 is the step before
  it, not a destination.
- **P4/E4**: scrolling back is `GET …?before={cursor}&limit=30`, and the reader keeps what it has.
  Nothing is redefined, so **P5** holds.
- **P6**: a reconnect re-issues the long poll with the last position. Nothing on screen is touched.

**What it costs.** The app now owns the hard parts Electric currently owns: computing a delta from a
position without scanning, holding a long poll without pinning a connection per reader per replica
(**O2**), and deciding what "changed since" means for a row that was revised twice. `changes.py`
already gives the app a Postgres wake-up, and `LiveIndex`/SSE already hold open streams for the
inventory, so the machinery is not foreign — but **O1** becomes the app's problem rather than a
tuned engine's.

The delta query is the crux: `revision_cursor > $since` over the entity table, bounded to the
reader's window, is an index range scan, and the fold already stores `revision_cursor`. That looks
tractable, and it is the thing to prototype before betting on this.

## Unknowns to settle before either

- How many concurrent long polls one app replica holds comfortably, and what happens at a rolling
  deploy (**O2**). The SSE routes already answer some of this in production.
- Whether a delta by `revision_cursor` is correct under the projector's batching — specifically
  whether an item can be revised without its `revision_cursor` advancing.
