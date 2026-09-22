# Option: the app pushes, over SSE or WebSocket

A1's delta, but the server keeps the connection and writes to it rather than answering a re-issued
poll. The app already does exactly this for the inventory: `/live/sandboxes` is SSE, `LiveIndex`
holds the state, and `changes.py` is the Postgres wake-up behind it.

The reader opens one stream and declares its interest — a cursor window and a content selection.
The app watches the fold and writes the entities and bodies that fall inside it.

## Where it differs from A1

The wire is the only real difference, and it buys two things:

- **Latency.** No re-issue round trip between a change committing and the byte arriving.
- **Server-decided relevance.** The app knows each reader's window, so it can push _only_ what that
  reader needs — which is the thing the owner named directly ("the client could receive updates just
  for stuff that's relevant to it"). Under a shape model the server cannot do this: the shape is
  shared, so its log is everyone's.

It costs the mirror of that: **per-reader server state (O1)**, which a shape model deliberately
avoids by making the partition shared. A shape is a cache many readers hit; a push subscription is
bookkeeping per reader. Which is better depends on reader count against conversation count, and
this deployment has few readers and few conversations.

## What it does and does not solve

- **P8/W2, W1, P4/E4, E2/E5**: as A1 — one declared interest, content selection as a parameter of
  it.
- **P6** is _harder_ than A1, not easier. A resumable stream needs a last-event id and a server able
  to answer "everything after this" — SSE's `Last-Event-ID` gives the protocol half; the app owes
  the replay half. A1's poll carries its position in every request and so has nothing to resume.
- **O2**: a reader is pinned to the replica holding its stream. The inventory already lives with
  this; a rolling deploy drops streams and clients reconnect. Whether that is acceptable for a
  conversation is the same question answered for the inventory, and the answer there was yes.

## Prior art worth reading before building this

Phoenix Channels and LiveView solve exactly this shape (per-reader server process, diffs over a
socket, resumption) and are the reference for how far it scales and what it costs. Not because we
would adopt Elixir, but because the failure modes are documented.
