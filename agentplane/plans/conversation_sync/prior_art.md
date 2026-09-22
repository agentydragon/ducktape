# Prior art: how other systems solve this

**Unverified.** Written from general knowledge to map the space, not from documentation read
today. Treat every claim as a lead to check, not a fact to design against — the point is to know
what questions to ask, and which of these deserve an afternoon before we commit.

The useful axis is not "which library" but **who decides what is synced, and in what unit**:

| Unit of subscription             | Who picks the content | Examples                                        |
| -------------------------------- | --------------------- | ----------------------------------------------- |
| A table, filtered server-side    | Server                | Supabase Realtime, Firebase listeners           |
| A named partition (shape/bucket) | Server                | **ElectricSQL**, PowerSync                      |
| A client-declared query          | **Client**            | **Zero**, Convex, InstantDB, Triplit, LiveStore |
| A server-computed per-reader set | Server, per reader    | Replicache, Phoenix Channels, our SSE routes    |

**P8 points at row three.** A client-declared query is where the client says what it is looking at,
including which fields — which is why this axis, rather than the feature lists, should drive the
comparison. D1 cuts across it instead: rows two and three can both satisfy it, by never overlapping
or by stating what is held.

## Worth actually investigating

- **Zero (Rocicorp).** Client declares queries; the server maintains them incrementally and streams
  diffs. Explicitly built around the thing we keep hitting — a reader's view is a query, not a
  partition someone else chose. Questions: maturity, whether a query can select which columns/fields
  (P8), whether a live query's range can be **moved** without re-delivering the overlap (D1), how
  deep history paging behaves, and self-hosting.
- **Replicache (Rocicorp, earlier).** Client pulls a delta against a cookie; the **server** computes
  what changed for that client. Nothing to subscribe to and the client states its position, so D1
  holds by construction, and the pull
  endpoint is essentially option_poll.md § A1 with a well-specified protocol and a client cache. The
  closest prior art to the cheapest option here, and the one to read for how it handles P6 and S3.
- **PowerSync.** Postgres → SQLite, with server-side **sync rules** defining per-user buckets.
  Partition model like Electric's, so likely satisfies D1 the same way — by never overlapping —
  and inherits P8. Worth checking whether bucket membership
  can be parameterised per reader at subscribe time.
- **Phoenix Channels / LiveView.** The reference implementation of option_app_push.md, with a
  decade of operational experience. Read for failure modes and scaling limits, not adoption.

## Probably not

- **CRDTs (Yjs, Automerge).** Solve concurrent multi-writer editing. A conversation has one writer
  (the projector) and many readers; the conflict machinery is cost without benefit.
- **Materialize / ReadySet / incremental view maintenance in the DB.** The right shape of answer to
  "maintain this query incrementally", at an operational weight (O4) far past the problem — and the
  fold in Postgres (C1) is already our incremental view.
- **GraphQL subscriptions.** Worth stealing the _connection/cursor paging model_ from, which is the
  best-specified version of P4 in wide use. Not worth adopting the stack for.

## What to check first

1. **Zero** — does a client query select fields, and can it page backwards cheaply? If yes it is the
   only option on this list that satisfies P8, D1 and D2 without us building the mechanism.
2. **Replicache's pull protocol** — specifically its answers to S3 (caught up vs arriving) and P6
   (resume). Even if we build option A1 ourselves, the protocol is worth copying rather than
   inventing.
3. **Electric's `subset__where`** — our proxy pins it to `true = true`. If Electric can snapshot a
   _narrow_ subset of a _wide_ shape, then one stable shape per conversation with a tail-only
   snapshot may satisfy P1 and O1 together with far fewer shapes than the page partition needs. It
   would still carry changes for the whole conversation in its live log, which is wasteful rather
   than wrong, and it does not touch **P8**. Cheap to check.
